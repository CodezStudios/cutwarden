"""Getting things OUT of cutwarden: OpenTimelineIO / EDL for finishing in a normal editor, SRT/VTT captions,
a portable project bundle (and its import), extra video formats, and chunked download for remote clients.

All files are written under WORKDIR/exports/<project>/ (or WORKDIR/renders). Download only serves those folders.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .brand import BrandKit
from .project import Project
from .render import _has_audio, render_video, render_frame_png


class ExportError(ValueError):
    pass


def out_dir(workdir: Path, project: str) -> Path:
    d = workdir / "exports" / project
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- shared helpers
def _fr(t: float, fps: int) -> int:
    return int(round(t * fps))


def _pack(layers):
    """Greedy-pack layers into tracks so items in one track never overlap."""
    tracks: list[list] = []
    for l in sorted(layers, key=lambda l: (l.start, l.z)):
        for tr in tracks:
            if l.start >= tr[-1].end - 1e-6:
                tr.append(l)
                break
        else:
            tracks.append([l])
    return tracks


def _audio_layers(proj: Project):
    out = []
    for l in proj.layers:
        if l.kind == "audio":
            out.append(l)
        elif l.kind == "footage" and l.preset == "clip" and l.params.get("audio") and os.path.exists(l.params["path"]) and _has_audio(l.params["path"]):
            out.append(l)
    return out


# ---------------------------------------------------------------- OpenTimelineIO
def _rt(v, rate):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate), "value": float(v)}


def _tr(start, dur, rate):
    return {"OTIO_SCHEMA": "TimeRange.1", "duration": _rt(dur, rate), "start_time": _rt(start, rate)}


def _clip(name, start, dur, rate, ref, meta):
    return {"OTIO_SCHEMA": "Clip.2", "name": name, "source_range": _tr(start, dur, rate), "enabled": True,
            "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA",
            "markers": [], "effects": [], "metadata": {"cutwarden": meta}}


def _gap(dur, rate):
    return {"OTIO_SCHEMA": "Gap.1", "name": "", "source_range": _tr(0, dur, rate), "enabled": True,
            "markers": [], "effects": [], "metadata": {}}


def _ext(path):
    return {"OTIO_SCHEMA": "ExternalReference.1", "name": Path(path).name, "target_url": str(path),
            "available_range": None, "metadata": {}}


def _gen(kind, params):
    return {"OTIO_SCHEMA": "GeneratorReference.1", "name": kind, "generator_kind": kind, "parameters": params,
            "available_range": None, "metadata": {}}


def _track(name, kind, layers, fps, make):
    children, cursor = [], 0
    for l in layers:
        s = _fr(l.start, fps)
        if s > cursor:
            children.append(_gap(s - cursor, fps))
        d = max(1, _fr(l.end, fps) - s)
        children.append(make(l, d))
        cursor = s + d
    return {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": children, "source_range": None,
            "markers": [], "effects": [], "metadata": {}, "enabled": True}


def to_otio(proj: Project) -> dict:
    fps = proj.fps
    media = lambda l: l.kind == "footage" or (l.kind == "overlay")

    def make_media(l, d):
        src_in = _fr(l.params.get("source_in", 0.0), fps)
        meta = {"layer_id": l.id, "preset": l.preset, "params": l.params}
        return _clip(f"{l.id} {Path(l.params['path']).name}", src_in, d, fps, _ext(l.params["path"]), meta)

    def make_generated(l, d):
        meta = {"layer_id": l.id, "preset": l.preset}
        return _clip(f"{l.id} {l.params.get('text', l.preset) if isinstance(l.params.get('text'), str) else l.preset}"[:80], 0, d, fps,
                     _gen(f"cutwarden.{l.kind}.{l.preset}", l.params), meta)

    def make_audio(l, d):
        meta = {"layer_id": l.id, "preset": l.preset, "volume": l.params.get("volume", 1.0)}
        return _clip(f"{l.id} {Path(l.params['path']).name}", _fr(l.params.get("source_in", 0.0), fps), d, fps, _ext(l.params["path"]), meta)

    tracks = []
    for i, grp in enumerate(_pack([l for l in proj.layers if l.kind == "footage"]), 1):
        tracks.append(_track(f"V{i} footage", "Video", grp, fps, make_media))
    for i, grp in enumerate(_pack([l for l in proj.layers if l.kind == "overlay"]), 1):
        tracks.append(_track(f"Overlay {i}", "Video", grp, fps, make_media))
    for i, grp in enumerate(_pack([l for l in proj.layers if l.kind in ("text", "logo")]), 1):
        tracks.append(_track(f"Titles {i}", "Video", grp, fps, make_generated))
    for i, grp in enumerate(_pack([l for l in proj.layers if l.kind == "effect"]), 1):
        tracks.append(_track(f"FX {i}", "Video", grp, fps, make_generated))
    for i, grp in enumerate(_pack(_audio_layers(proj)), 1):
        tracks.append(_track(f"A{i}", "Audio", grp, fps, make_audio))
    return {"OTIO_SCHEMA": "Timeline.1", "name": proj.name, "global_start_time": _rt(0, fps),
            "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": tracks, "source_range": None,
                       "markers": [], "effects": [], "metadata": {}, "enabled": True},
            "metadata": {"cutwarden": {"canvas": f"{proj.width}x{proj.height}@{proj.fps}", "platform": proj.platform,
                                      "revision": proj.revision, "note": "Text, logo and effect layers are placeholders "
                                      "(GeneratorReference) with the full cutwarden parameters in metadata."}}}


def export_otio(workdir: Path, proj: Project) -> dict:
    p = out_dir(workdir, proj.name) / f"{proj.name}.otio"
    p.write_text(json.dumps(to_otio(proj), indent=2))
    tl = to_otio(proj)["tracks"]["children"]
    return {"path": str(p), "tracks": [t["name"] for t in tl], "items": sum(len(t["children"]) for t in tl)}


# ---------------------------------------------------------------- EDL (CMX 3600)
def _tc(frames: int, fps: int) -> str:
    f = frames % fps
    s = frames // fps
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def export_edl(workdir: Path, proj: Project) -> dict:
    fps = proj.fps
    lines = [f"TITLE: {proj.name}", "FCM: NON-DROP FRAME", ""]
    n = 0
    for track, kind in (([l for l in proj.layers if l.kind == "footage"], "V"), (_audio_layers(proj), "A")):
        for l in sorted(track, key=lambda l: l.start):
            if "path" not in l.params:
                continue
            n += 1
            si = _fr(l.params.get("source_in", 0.0), fps)
            d = max(1, _fr(l.end, fps) - _fr(l.start, fps))
            name = Path(l.params["path"]).name
            lines.append(f"{n:03d}  {re.sub(r'[^A-Za-z0-9]', '', Path(name).stem)[:8].upper() or 'AX':<8} {kind:<5} C        "
                         f"{_tc(si, fps)} {_tc(si + d, fps)} {_tc(_fr(l.start, fps), fps)} {_tc(_fr(l.start, fps) + d, fps)}")
            lines.append(f"* FROM CLIP NAME: {name}")
            lines.append(f"* COMMENT: {l.id} {l.preset}")
            lines.append("")
    p = out_dir(workdir, proj.name) / f"{proj.name}.edl"
    p.write_text("\n".join(lines))
    return {"path": str(p), "events": n, "note": "video and audio source clips only; text/effects are not representable in an EDL (use export_otio)"}


# ---------------------------------------------------------------- captions
def _ts(t: float, vtt: bool) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{'.' if vtt else ','}{ms:03d}"


def collect_cues(proj: Project, include_titles: bool = False, max_words: int = 6) -> list[tuple[float, float, str]]:
    cues = []
    for l in sorted(proj.layers, key=lambda l: l.start):
        if l.kind != "text":
            continue
        if l.preset == "word_captions":
            words = l.params.get("words") or []
            cur = []
            for w in words:
                cur.append(w)
                if len(cur) >= max_words or re.search(r"[.!?]$", str(w[0])):
                    cues.append((l.start + cur[0][1], l.start + cur[-1][2], " ".join(str(x[0]) for x in cur)))
                    cur = []
            if cur:
                cues.append((l.start + cur[0][1], l.start + cur[-1][2], " ".join(str(x[0]) for x in cur)))
        elif include_titles and isinstance(l.params.get("text"), str):
            cues.append((l.start, l.end, l.params["text"].replace("\n", " ")))
    return sorted(cues)


def export_captions(workdir: Path, proj: Project, fmt: str = "srt", include_titles: bool = False) -> dict:
    if fmt not in ("srt", "vtt"):
        raise ExportError("format must be 'srt' or 'vtt'")
    cues = collect_cues(proj, include_titles)
    if not cues:
        raise ExportError("no caption text found. add a word_captions layer, or pass include_titles=true to export title text too")
    vtt = fmt == "vtt"
    body = "WEBVTT\n\n" if vtt else ""
    for i, (a, b, t) in enumerate(cues, 1):
        body += (f"{i}\n" if not vtt else "") + f"{_ts(a, vtt)} --> {_ts(b, vtt)}\n{t}\n\n"
    p = out_dir(workdir, proj.name) / f"{proj.name}.{fmt}"
    p.write_text(body, encoding="utf-8")
    return {"path": str(p), "cues": len(cues)}


# ---------------------------------------------------------------- portable bundle
def export_bundle(workdir: Path, proj: Project) -> dict:
    """A zip with project.json, every media file, and the brand kit. Paths inside are relative, so it opens anywhere."""
    brand_path = Path(proj.brand)
    brand_root = brand_path.parent
    data = json.loads(proj.model_dump_json())
    files: dict[str, str] = {}

    def rel(path: str) -> str:
        if path not in files:
            name = f"{hashlib.sha256(path.encode()).hexdigest()[:6]}_{Path(path).name}"
            files[path] = f"media/{name}"
        return files[path]

    for l in data["layers"]:
        if isinstance(l["params"].get("path"), str):
            if not os.path.exists(l["params"]["path"]):
                raise ExportError(f"missing file for {l['id']}: {l['params']['path']}. run validate_project")
            l["params"]["path"] = rel(l["params"]["path"])
    data["brand"] = "brand/brand.json"
    p = out_dir(workdir, proj.name) / f"{proj.name}_bundle.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as z:
        z.writestr("project.json", json.dumps(data, indent=2))
        z.writestr("manifest.json", json.dumps({"format": "cutwarden-bundle", "version": 1, "project": proj.name,
                                                "revision": proj.revision, "media": len(files)}))
        for src, dst in files.items():
            z.write(src, dst)
        for f in brand_root.rglob("*"):
            if f.is_file():
                z.write(f, f"brand/{f.relative_to(brand_root).as_posix()}")
    return {"path": str(p), "bytes": p.stat().st_size, "media_files": len(files)}


def import_bundle(workdir: Path, editor, source: str, name: str | None = None) -> dict:
    """Open a bundle made by export_bundle: media goes into the asset library, the brand kit is restored, layers are re-validated."""
    from . import imports
    with tempfile.TemporaryDirectory(prefix="bc_bundle_") as t:
        t = Path(t)
        zp = imports._fetch(source, t, "bundle.zip")
        ex = t / "x"
        with zipfile.ZipFile(zp) as z:
            for info in z.infolist():
                target = (ex / info.filename).resolve()
                if not str(target).startswith(str(ex.resolve())):
                    raise ExportError(f"unsafe path in bundle: {info.filename}")
            if sum(i.file_size for i in z.infolist()) > imports.MAX_MB * 1024 * 1024 * 2:
                raise ExportError("bundle is too large")
            z.extractall(ex)
        if not (ex / "project.json").exists():
            raise ExportError("not a cutwarden bundle (no project.json)")
        data = json.loads((ex / "project.json").read_text())
        name = name or data["name"]
        brand_name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{name}_brand")[:40]
        bdir = workdir / "brands" / brand_name
        if bdir.exists():
            shutil.rmtree(bdir)
        shutil.copytree(ex / "brand", bdir)
        brand_json = bdir / "brand.json"
        BrandKit.load(brand_json)
        paths = {}
        for l in data["layers"]:
            rp = l["params"].get("path")
            if isinstance(rp, str) and rp.startswith("media/"):
                if rp not in paths:
                    paths[rp] = str(imports._store(ex / rp, workdir / "assets", Path(rp).name.split("_", 1)[-1], imports.MEDIA_EXT))
                l["params"]["path"] = paths[rp]
        editor.create(name, str(brand_json), data["width"], data["height"], data["fps"], data.get("platform"))
        try:
            for l in sorted(data["layers"], key=lambda l: (l["z"], l["start"])):
                editor.add_layer(name, l["kind"], l["preset"], l["start"], l["duration"], l["params"], l["z"], None)
        except Exception:
            editor.store.path(name).unlink(missing_ok=True)
            raise
        return editor.store.load(name).summary()


# ---------------------------------------------------------------- video formats
FORMATS = {
    "mp4": ("mp4", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]),
    "mov_prores": ("mov", ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s16le"]),
    "webm": ("webm", ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "128k"]),
    "gif": ("gif", []),
    "mp3": ("mp3", ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]),
    "wav": ("wav", ["-vn", "-c:a", "pcm_s16le"]),
}


def export_video(workdir: Path, proj: Project, fmt: str = "mp4", scale: float = 1.0, gif_width: int = 480, gif_fps: int = 15) -> dict:
    if fmt not in FORMATS:
        raise ExportError(f"format must be one of {sorted(FORMATS)}")
    ext, args = FORMATS[fmt]
    d = out_dir(workdir, proj.name)
    final = d / f"{proj.name}_r{proj.revision}{'_' + fmt if fmt != 'mp4' else ''}.{ext}"
    with tempfile.TemporaryDirectory(prefix="bc_exp_") as t:
        master = Path(t) / "master.mp4"
        render_video(proj, str(master), scale)
        if fmt == "mp4":
            shutil.copyfile(master, final)
        else:
            if fmt == "gif":
                vf = f"fps={gif_fps},scale={gif_width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
                cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(master), "-an", "-filter_complex", vf, "-loop", "0", str(final)]
            else:
                cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(master), *args, str(final)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise ExportError(f"ffmpeg failed: {r.stderr[-400:]}")
    return {"path": str(final), "format": fmt, "bytes": final.stat().st_size, "duration": round(proj.duration(), 3)}


def export_poster(workdir: Path, proj: Project, time: float = 0.0, scale: float = 1.0) -> dict:
    p = out_dir(workdir, proj.name) / f"{proj.name}_poster_{time:.2f}.png"
    p.write_bytes(render_frame_png(proj, time, scale))
    return {"path": str(p)}


# ---------------------------------------------------------------- chunked download (remote clients cannot read the server disk)
CHUNK = 600 * 1024


def download_chunk(workdir: Path, path: str, index: int = 0) -> dict:
    """Read a file the server exported or rendered, in base64 chunks of ~600 KB. Only files under WORKDIR/exports and WORKDIR/renders."""
    p = Path(os.path.expanduser(path)).resolve()
    ok = [(workdir / "exports").resolve(), (workdir / "renders").resolve()]
    if not any(str(p).startswith(str(o) + os.sep) for o in ok) or not p.is_file():
        raise ExportError("can only download files from the exports/ and renders/ folders. use an export_* tool or render_video first")
    size = p.stat().st_size
    total = max(1, -(-size // CHUNK))
    if index < 0 or index >= total:
        raise ExportError(f"index out of range: file has {total} chunk(s), numbered 0..{total - 1}")
    with open(p, "rb") as f:
        f.seek(index * CHUNK)
        raw = f.read(CHUNK)
    out = {"file": p.name, "index": index, "total_chunks": total, "bytes_in_chunk": len(raw), "data_base64": base64.b64encode(raw).decode()}
    if index == total - 1:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while b := f.read(1 << 20):
                h.update(b)
        out["sha256"] = h.hexdigest()
        out["size"] = size
    return out
