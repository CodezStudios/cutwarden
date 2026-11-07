"""Getting things INTO cutwarden: footage/audio/images (local path, URL, base64), brand kits, captions, projects.

Everything imported lands in WORKDIR/assets (media) or WORKDIR/brands (kits) so remote MCP clients can use it.
Safety: URL imports block private/loopback addresses and cap size; local-path imports can be switched off
(automatic in --http mode) so a remote client cannot copy arbitrary files off the server.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from . import analyze
from .brand import BrandKit, BrandError

MEDIA_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
             ".png", ".jpg", ".jpeg", ".webp", ".gif"}
FONT_EXT = {".otf", ".ttf"}
MAX_MB = float(os.environ.get("CUTWARDEN_MAX_MB", "1000"))
B64_MAX_MB = 25


class ImportError_(ValueError):
    pass


def _local_allowed() -> bool:
    return os.environ.get("CUTWARDEN_ALLOW_LOCAL", "1") == "1"


def _check_url(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ImportError_(f"only http(s) URLs can be imported, got '{url}'")
    if os.environ.get("CUTWARDEN_ALLOW_PRIVATE_URLS") == "1":
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError as e:
        raise ImportError_(f"cannot resolve host '{u.hostname}': {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ImportError_(f"refusing to fetch from a private/internal address ({ip}). "
                               "set CUTWARDEN_ALLOW_PRIVATE_URLS=1 on the server if you really need this")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, dest: Path) -> None:
    _check_url(url)
    opener = urllib.request.build_opener(_Redirect)
    req = urllib.request.Request(url, headers={"User-Agent": "cutwarden/0.5.0"})
    limit = int(MAX_MB * 1024 * 1024)
    n = 0
    try:
        with opener.open(req, timeout=30) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if n > limit:
                    raise ImportError_(f"download exceeds {MAX_MB:.0f} MB limit (CUTWARDEN_MAX_MB)")
                f.write(chunk)
    except ImportError_:
        dest.unlink(missing_ok=True)
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise ImportError_(f"download failed: {e}") from None


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()[:10]


def _clean(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return name[:80]


def _fetch(source: str, tmpdir: Path, hint: str | None = None) -> Path:
    """Bring `source` (URL or local path) to a temp file and return it."""
    if re.match(r"^https?://", source):
        name = hint or Path(urllib.parse.urlparse(source).path).name or "download"
        dest = tmpdir / _clean(name)
        _download(source, dest)
        return dest
    if not _local_allowed():
        raise ImportError_("importing from local paths is disabled on this server. pass an http(s) URL, "
                           "or use import_media_base64 for small files")
    p = Path(os.path.expanduser(source))
    if not p.is_file():
        raise ImportError_(f"file not found: {source} (use an absolute path or an http(s) URL)")
    if p.stat().st_size > MAX_MB * 1024 * 1024:
        raise ImportError_(f"file exceeds {MAX_MB:.0f} MB limit (CUTWARDEN_MAX_MB)")
    return p


def _store(src: Path, dest_dir: Path, name: str | None, exts: set[str]) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _clean(name or src.name)
    ext = Path(name).suffix.lower() or src.suffix.lower()
    if ext not in exts:
        raise ImportError_(f"unsupported file type '{ext}'. allowed: {sorted(exts)}")
    stem = Path(name).stem
    dest = dest_dir / f"{stem}-{_sha(src)}{ext}"
    if not dest.exists():
        shutil.copyfile(src, dest)
    return dest


def import_media(workdir: Path, source: str, name: str | None = None) -> dict:
    """Import a video/audio/image from a URL or local path into the asset library and probe it."""
    with tempfile.TemporaryDirectory(prefix="bc_imp_") as t:
        src = _fetch(source, Path(t), name)
        dest = _store(src, workdir / "assets", name, MEDIA_EXT)
    try:
        info = analyze.probe(str(dest))
    except Exception:
        dest.unlink(missing_ok=True)
        raise ImportError_(f"'{source}' is not a readable media file") from None
    info["asset"] = dest.name
    return info


def import_media_base64(workdir: Path, filename: str, data_base64: str) -> dict:
    """Import a small file sent inline (max 25 MB). For remote clients that cannot expose a URL."""
    if len(data_base64) > B64_MAX_MB * 1024 * 1024 * 4 // 3:
        raise ImportError_(f"inline upload is limited to {B64_MAX_MB} MB. host the file and use import_media with a URL")
    try:
        raw = base64.b64decode(data_base64, validate=False)
    except Exception:
        raise ImportError_("data_base64 is not valid base64") from None
    with tempfile.TemporaryDirectory(prefix="bc_imp_") as t:
        src = Path(t) / _clean(filename)
        src.write_bytes(raw)
        dest = _store(src, workdir / "assets", filename, MEDIA_EXT)
    try:
        info = analyze.probe(str(dest))
    except Exception:
        dest.unlink(missing_ok=True)
        raise ImportError_(f"'{filename}' is not a readable media file") from None
    info["asset"] = dest.name
    return info


def list_assets(workdir: Path) -> list[dict]:
    out = []
    for p in sorted((workdir / "assets").glob("*")) if (workdir / "assets").exists() else []:
        try:
            i = analyze.probe(str(p))
            out.append({"path": str(p), "duration": i["duration"], "size": f"{i.get('width', '-')}x{i.get('height', '-')}",
                        "has_audio": i["has_audio"]})
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- captions
_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.fullmatch(s.strip())
    if not m:
        raise ImportError_(f"bad timestamp '{s}'")
    h, mi, se, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def parse_captions(text: str, offset: float = 0.0) -> list[list]:
    """SRT or WebVTT text -> [[word, start, end], ...] for word_captions. Cue time is split across its words by length."""
    words: list[list] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", "").strip())
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, _, rest = lines[ti].partition("-->")
        start, end = _ts(a.split()[0]), _ts(rest.split()[0])
        body = re.sub(r"<[^>]+>|\{[^}]*\}", "", " ".join(lines[ti + 1:]))
        toks = body.split()
        if not toks or end <= start:
            continue
        total = sum(len(t) + 1 for t in toks)
        t0 = start
        for tok in toks:
            d = (end - start) * (len(tok) + 1) / total
            words.append([tok, round(t0 + offset, 3), round(t0 + d + offset, 3)])
            t0 += d
    if not words:
        raise ImportError_("no cues found. expected SRT or WebVTT text")
    return words


def read_captions(source: str, offset: float = 0.0) -> list[list]:
    """`source` is SRT/VTT text itself, a local file path, or an http(s) URL."""
    if "-->" in source:
        return parse_captions(source, offset)
    with tempfile.TemporaryDirectory(prefix="bc_imp_") as t:
        p = _fetch(source, Path(t), "captions.srt")
        return parse_captions(p.read_text(encoding="utf-8", errors="replace"), offset)


# ---------------------------------------------------------------- brand kit
def import_brand(workdir: Path, name: str, colors: dict, fonts: dict, logo: str | None = None,
                 safe: dict | None = None, loudness_target: float = -14.0, allow_custom_colors: bool = False) -> dict:
    """Create a brand kit from colors plus font files and a logo (each a URL or path). Returns the brand.json path."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ImportError_("brand name may only contain letters, digits, '_' and '-'")
    root = workdir / "brands" / name
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    font_rel = {}
    with tempfile.TemporaryDirectory(prefix="bc_imp_") as t:
        for role, src in fonts.items():
            f = _fetch(src, Path(t), f"{role}{Path(urllib.parse.urlparse(src).path).suffix or '.otf'}")
            dest = root / "fonts" / f"{_clean(role)}{f.suffix.lower()}"
            if f.suffix.lower() not in FONT_EXT:
                raise ImportError_(f"font '{role}' must be .otf or .ttf, got '{f.suffix}'")
            shutil.copyfile(f, dest)
            font_rel[role] = f"fonts/{dest.name}"
        logo_rel = None
        if logo:
            lf = _fetch(logo, Path(t), "logo.png")
            if lf.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
                raise ImportError_("logo must be png, webp or jpg (a transparent PNG is best)")
            shutil.copyfile(lf, root / f"logo{lf.suffix.lower()}")
            logo_rel = f"logo{lf.suffix.lower()}"
    data = {"name": name, "colors": colors, "fonts": font_rel, "logo": logo_rel, "safe": safe or {},
            "loudness_target": loudness_target, "allow_custom_colors": allow_custom_colors}
    if not safe:
        data.pop("safe")
    p = root / "brand.json"
    p.write_text(json.dumps(data, indent=2))
    try:
        kit = BrandKit.load(p)
        from .brand import hex_to_rgb
        for k, v in kit.colors.items():
            hex_to_rgb(v)
    except (BrandError, KeyError, ValueError) as e:
        shutil.rmtree(root, ignore_errors=True)
        raise ImportError_(f"invalid brand kit: {e}") from None
    return {"brand_path": str(p), "colors": sorted(kit.colors), "fonts": sorted(kit.fonts), "logo": logo_rel}


# ---------------------------------------------------------------- chunked upload (AI pushes a file in pieces)
CHUNK_MAX_MB = 0.75        # raw bytes per chunk; keeps each tool call a sane size


def _up_dir(workdir: Path) -> Path:
    d = workdir / ".uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def upload_begin(workdir: Path, filename: str) -> dict:
    """Start a chunked upload. Then call upload_chunk for index 0,1,2,... and finally upload_finish."""
    ext = Path(_clean(filename)).suffix.lower()
    if ext not in MEDIA_EXT:
        raise ImportError_(f"unsupported file type '{ext}'. allowed: {sorted(MEDIA_EXT)}")
    uid = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    (_up_dir(workdir) / f"{uid}.json").write_text(json.dumps({"filename": _clean(filename), "next": 0, "bytes": 0}))
    (_up_dir(workdir) / f"{uid}.part").write_bytes(b"")
    return {"upload_id": uid, "next_index": 0, "max_chunk_bytes": int(CHUNK_MAX_MB * 1024 * 1024)}


def upload_chunk(workdir: Path, upload_id: str, index: int, data_base64: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", upload_id):
        raise ImportError_("bad upload_id")
    meta_p = _up_dir(workdir) / f"{upload_id}.json"
    if not meta_p.exists():
        raise ImportError_(f"unknown upload '{upload_id}'. call upload_begin first")
    meta = json.loads(meta_p.read_text())
    if index != meta["next"]:
        raise ImportError_(f"chunks must be sent in order: expected index {meta['next']}, got {index}")
    try:
        raw = base64.b64decode(data_base64, validate=False)
    except Exception:
        raise ImportError_("data_base64 is not valid base64") from None
    if len(raw) > CHUNK_MAX_MB * 1024 * 1024:
        raise ImportError_(f"chunk too large ({len(raw)} bytes). send at most {int(CHUNK_MAX_MB * 1024 * 1024)} bytes per chunk")
    if meta["bytes"] + len(raw) > MAX_MB * 1024 * 1024:
        raise ImportError_(f"upload exceeds {MAX_MB:.0f} MB limit (CUTWARDEN_MAX_MB)")
    with open(_up_dir(workdir) / f"{upload_id}.part", "ab") as f:
        f.write(raw)
    meta["next"] += 1
    meta["bytes"] += len(raw)
    meta_p.write_text(json.dumps(meta))
    return {"upload_id": upload_id, "received_bytes": meta["bytes"], "next_index": meta["next"]}


def upload_finish(workdir: Path, upload_id: str) -> dict:
    """Assemble the chunks, verify it is real media, and add it to the asset library."""
    meta_p = _up_dir(workdir) / f"{upload_id}.json"
    part = _up_dir(workdir) / f"{upload_id}.part"
    if not re.fullmatch(r"[0-9a-f]{12}", upload_id) or not meta_p.exists():
        raise ImportError_(f"unknown upload '{upload_id}'")
    meta = json.loads(meta_p.read_text())
    try:
        dest = _store(part, workdir / "assets", meta["filename"], MEDIA_EXT)
        try:
            info = analyze.probe(str(dest))
        except Exception:
            dest.unlink(missing_ok=True)
            raise ImportError_(f"'{meta['filename']}' is not a readable media file (were all chunks sent?)") from None
    finally:
        meta_p.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    info["asset"] = dest.name
    return info
