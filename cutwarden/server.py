"""MCP server. Works with any MCP client (Claude, ChatGPT, Gemini, Cursor, goose...).

Env:
  CUTWARDEN_WORKDIR  where projects/renders live (default ~/cutwarden-projects)
  CUTWARDEN_BRAND    default brand kit path (default: bundled example brand)
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import functools

try:                                    # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server, Image
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image
    from mcp.server.fastmcp.exceptions import ToolError

from . import analyze, export, imports, qa, render
from .brand import BrandKit
from .presets import PRESETS
from .project import Editor, ProjectStore

WORKDIR = Path(os.environ.get("CUTWARDEN_WORKDIR", "~/cutwarden-projects")).expanduser()
DEFAULT_BRAND = os.environ.get("CUTWARDEN_BRAND") or str(Path(__file__).resolve().parent.parent / "brands" / "example" / "brand.json")

store = ProjectStore(WORKDIR)
editor = Editor(store)

INSTRUCTIONS = """\
You edit brand videos with a timeline of layers. You never generate pixels or write animation code: you choose presets
and fill their parameters, and the renderer does the rest.

Workflow:
1. get_brand, then list_presets. Only brand colors and fonts are allowed.
2. If there is source footage: probe_media, source_contact_sheet, detect_scenes, detect_silence, transcribe_audio.
3. create_project, then add_layer for footage (kind=footage; images and animated GIFs use the `image` preset), overlay (stickers, GIFs,
   product shots placed at x/y/width), text, logo, effect, audio. Bring files in first with import_media (URL) or upload_begin/chunk/finish. Times are seconds.
   Effects act on everything below them (z order). Center a 'whip' on a cut: start = cut_time - duration/2.
4. LOOK before you finish: render_frame at key moments (just after each entrance, mid-hold) and render_contact_sheet.
   Fix what you see with update_layer.
5. validate_project and fix every error and warning. Then render_video, then qa_video.
Pace: change something on screen every 1-2 seconds. Keep text to 2-5 words per beat. Hold text long enough to read.
"""

mcp = _Server("cutwarden", instructions=INSTRUCTIONS)


def tool():
    """Register an MCP tool. Domain errors become ToolError so the model sees the full, actionable message
    (the SDK hides the text of unexpected exceptions)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except (ValueError, RuntimeError, OSError, KeyError) as e:      # ProjectError/PresetError/BrandError are ValueErrors
                raise ToolError(f"{type(e).__name__}: {e}") from None
        return mcp.tool()(wrapper)
    return deco


def _brand(project: str | None = None) -> BrandKit:
    return BrandKit.load(store.load(project).brand if project else DEFAULT_BRAND)


# ------------------------------------------------------------------ discovery
@tool()
def get_brand(project: str | None = None) -> dict:
    """The brand kit: allowed color names, font roles, safe zone, loudness target, platform limits."""
    return _brand(project).describe()


@tool()
def list_presets(kind: str | None = None) -> list[dict]:
    """All presets with their parameters. kind: footage, overlay, text, logo, effect, audio (omit for all)."""
    return [p.describe() for p in PRESETS.values() if kind in (None, p.kind)]


# ------------------------------------------------------------------ project
@tool()
def create_project(name: str, platform: str | None = "reels", width: int = 1080, height: int = 1920,
                   fps: int = 30, brand_path: str | None = None) -> dict:
    """Create an empty project. platform sets length/aspect limits used by validate_project (tiktok, reels, shorts, feed)."""
    return editor.create(name, brand_path or DEFAULT_BRAND, width, height, fps, platform).summary()


@tool()
def list_projects() -> list[str]:
    """Names of existing projects."""
    return store.list()


@tool()
def get_timeline(project: str) -> dict:
    """The full project: every layer with times and parameters, plus the current revision."""
    return store.load(project).summary()


@tool()
def add_layer(project: str, kind: str, preset: str, start: float, duration: float,
              params: dict | None = None, z: int | None = None, expected_revision: int | None = None) -> dict:
    """Add a layer. kind: footage|overlay|text|logo|effect|audio. params are validated against the preset (see list_presets).
    Pass expected_revision (from the last response) to avoid editing a stale timeline."""
    proj, layer = editor.add_layer(project, kind, preset, start, duration, params, z, expected_revision)
    return {"revision": proj.revision, "layer": layer.model_dump()}


@tool()
def update_layer(project: str, layer_id: str, start: float | None = None, duration: float | None = None,
                 z: int | None = None, params: dict | None = None, expected_revision: int | None = None) -> dict:
    """Change timing, stacking or parameters of a layer. params are merged into the existing ones."""
    proj, layer = editor.update_layer(project, layer_id, start, duration, z, params, expected_revision)
    return {"revision": proj.revision, "layer": layer.model_dump()}


@tool()
def remove_layer(project: str, layer_id: str, expected_revision: int | None = None) -> dict:
    """Delete a layer."""
    return {"revision": editor.remove_layer(project, layer_id, expected_revision).revision}


@tool()
def undo(project: str) -> dict:
    """Revert the last change."""
    return store.undo(project).summary()


# ------------------------------------------------------------------ import
@tool()
def import_media(source: str, name: str | None = None) -> dict:
    """Bring a video, audio or image file into the asset library from an http(s) URL (or a local path when the server allows it).
    Returns its probe info and the stored absolute `path` to use in clip/music/voice/image params."""
    return imports.import_media(WORKDIR, source, name)


@tool()
def import_media_base64(filename: str, data_base64: str) -> dict:
    """Import a small file (max 25 MB) sent inline as base64, for clients that cannot expose a URL. Larger files: use import_media."""
    return imports.import_media_base64(WORKDIR, filename, data_base64)


@tool()
def upload_begin(filename: str) -> dict:
    """Push a file in pieces. 1) upload_begin(filename) 2) upload_chunk(upload_id, 0, base64), then 1, 2, ... in order
    (max ~750 KB raw per chunk) 3) upload_finish(upload_id). Use for files too big for import_media_base64 when no URL exists."""
    return imports.upload_begin(WORKDIR, filename)


@tool()
def upload_chunk(upload_id: str, index: int, data_base64: str) -> dict:
    """Append chunk number `index` (starting at 0, strictly in order) to an upload."""
    return imports.upload_chunk(WORKDIR, upload_id, index, data_base64)


@tool()
def upload_finish(upload_id: str) -> dict:
    """Finish an upload: verifies the file and adds it to the asset library. Returns the usable `path`."""
    return imports.upload_finish(WORKDIR, upload_id)


@tool()
def list_assets() -> list[dict]:
    """Media already imported into the asset library (path, duration, size, audio)."""
    return imports.list_assets(WORKDIR)


@tool()
def import_captions(source: str, offset: float = 0.0) -> list[list]:
    """SRT or WebVTT (pasted text, path, or URL) -> [[word, start, end], ...] ready for word_captions `words`.
    offset shifts all times (seconds), e.g. the start of the layer you will place the captions in."""
    return imports.read_captions(source, offset)


@tool()
def import_brand(name: str, colors: dict, fonts: dict, logo: str | None = None, safe: dict | None = None,
                 loudness_target: float = -14.0, allow_custom_colors: bool = False) -> dict:
    """Create a brand kit. colors: {"bg": "#0A0A0C", "fg": "#FFF", "accent": "#E5262D", ...} (bg, fg, accent required).
    fonts: {"display": url_or_path_to_otf_ttf, "body": ...} (display required). logo: url or path to a PNG. Returns brand_path for create_project."""
    return imports.import_brand(WORKDIR, name, colors, fonts, logo, safe, loudness_target, allow_custom_colors)


@tool()
def import_project(name: str, timeline: dict, brand_path: str | None = None) -> dict:
    """Create a project from an exported timeline (the `get_timeline` output or a project JSON). Every layer is re-validated,
    so renamed presets or missing files are reported with fixes."""
    cfg = timeline
    m = re.fullmatch(r"(\d+)x(\d+)@(\d+)", str(cfg.get("canvas", "")))
    w, h, fps = (int(x) for x in m.groups()) if m else (cfg.get("width", 1080), cfg.get("height", 1920), cfg.get("fps", 30))
    editor.create(name, brand_path or DEFAULT_BRAND, w, h, fps, cfg.get("platform"))
    try:
        for l in sorted(cfg.get("layers", []), key=lambda l: (l.get("z", 0), l["start"])):
            end = l["end"] - l["start"] if "end" in l and "duration" not in l else l["duration"]
            editor.add_layer(name, l["kind"], l["preset"], l["start"], end, l.get("params"), l.get("z"), None)
    except Exception:
        (store.path(name)).unlink(missing_ok=True)
        raise
    return store.load(name).summary()


# ------------------------------------------------------------------ understanding footage
@tool()
def probe_media(path: str) -> dict:
    """Duration, resolution, fps, audio presence of a media file."""
    return analyze.probe(path)


@tool()
def source_contact_sheet(path: str, count: int = 12) -> Image:
    """A grid of evenly spaced thumbnails with timestamps, so you can see what is in a source video."""
    return Image(data=analyze.source_contact_sheet(path, count), format="png")


@tool()
def detect_scenes(path: str, threshold: float = 0.3) -> list[float]:
    """Timestamps where the picture changes sharply (candidate cut points)."""
    return analyze.detect_scenes(path, threshold)


@tool()
def detect_silence(path: str, noise_db: float = -35, min_len: float = 0.4) -> list[dict]:
    """Silent spans in the audio (for removing dead air)."""
    return analyze.detect_silence(path, noise_db, min_len)


@tool()
def transcribe_audio(path: str, model: str = "base") -> list[list]:
    """Word-level transcript [[word, start, end], ...] for word_captions and for cutting on speech. Needs faster-whisper."""
    return analyze.transcribe(path, model)


# ------------------------------------------------------------------ look and check
@tool()
def render_frame(project: str, time: float, scale: float = 0.5) -> Image:
    """Render one frame as a PNG so you can SEE the result at `time` seconds. Always check key moments before finishing."""
    return Image(data=render.render_frame_png(store.load(project), time, scale), format="png")


@tool()
def render_contact_sheet(project: str, count: int = 8, start: float = 0.0, end: float | None = None, scale: float = 0.22) -> Image:
    """A grid of frames across the timeline (or a time range) with timestamps, to review pacing at a glance."""
    proj = store.load(project)
    end = proj.duration() if end is None else end
    times = [start + (end - start) * (i + 0.5) / count for i in range(count)]
    return Image(data=render.contact_sheet_png(proj, times, scale=scale), format="png")


@tool()
def validate_project(project: str) -> dict:
    """Static quality checks: files exist, text inside the safe zone, contrast, readability speed, overlaps, platform limits."""
    return qa.validate_project(store.load(project))


# ------------------------------------------------------------------ output
@tool()
def render_video(project: str, output_path: str | None = None, scale: float = 1.0) -> dict:
    """Render the final MP4 (H.264 + AAC, loudness-normalized). Use scale=0.5 for a fast preview. Blocks until done."""
    proj = store.load(project)
    out = output_path or str(WORKDIR / "renders" / f"{project}_r{proj.revision}.mp4")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    return render.render_video(proj, out, scale)


@tool()
def qa_video(path: str, project: str | None = None) -> dict:
    """Check a rendered file: resolution, integrated loudness, true peak, black frames."""
    brand = _brand(project) if project else BrandKit.load(DEFAULT_BRAND)
    wh = None
    if project:
        p = store.load(project)
        wh = (p.width, p.height)
    return qa.qa_video(path, brand, wh)


# ------------------------------------------------------------------ export
@tool()
def export_otio(project: str) -> dict:
    """Export the timeline as OpenTimelineIO (.otio) so a human can finish it in DaVinci Resolve, Premiere, Final Cut (via adapters), etc.
    Footage, overlays and audio become real clips on tracks; text, logo and effects are placeholder clips carrying their cutwarden parameters."""
    return export.export_otio(WORKDIR, store.load(project))


@tool()
def export_edl(project: str) -> dict:
    """Export a CMX 3600 EDL of the footage and audio source clips (the cut). Opens in virtually every editor."""
    return export.export_edl(WORKDIR, store.load(project))


@tool()
def export_captions(project: str, format: str = "srt", include_titles: bool = False) -> dict:
    """Export word_captions (and optionally title text) as an SRT or VTT subtitle file."""
    return export.export_captions(WORKDIR, store.load(project), format, include_titles)


@tool()
def export_bundle(project: str) -> dict:
    """A portable zip (project JSON + all media + brand kit). Reopen on any machine with import_bundle."""
    return export.export_bundle(WORKDIR, store.load(project))


@tool()
def import_bundle(source: str, name: str | None = None) -> dict:
    """Open a bundle made by export_bundle (URL or path). Restores media, brand kit and every layer."""
    return export.import_bundle(WORKDIR, editor, source, name)


@tool()
def export_video(project: str, format: str = "mp4", scale: float = 1.0, gif_width: int = 480, gif_fps: int = 15) -> dict:
    """Render to a delivery format. format: mp4 (H.264), mov_prores (ProRes 422 HQ for editors), webm (VP9), gif, mp3, wav (audio only).
    scale<1 for previews. Use download_chunk to fetch the file if you cannot read the server's disk."""
    return export.export_video(WORKDIR, store.load(project), format, scale, gif_width, gif_fps)


@tool()
def export_poster(project: str, time: float = 0.0, scale: float = 1.0) -> dict:
    """Save one frame as a PNG (thumbnail / cover image)."""
    return export.export_poster(WORKDIR, store.load(project), time, scale)


@tool()
def download_chunk(path: str, index: int = 0) -> dict:
    """Fetch an exported or rendered file as base64 chunks (~600 KB each). Call with index 0, 1, ... until total_chunks is reached;
    the last chunk includes a sha256 of the whole file. Only works for files under exports/ and renders/."""
    return export.download_chunk(WORKDIR, path, index)


def main():
    ap = argparse.ArgumentParser(prog="cutwarden-mcp")
    ap.add_argument("--http", action="store_true", help="serve over streamable HTTP (needed for ChatGPT / Gemini remote MCP)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    a = ap.parse_args()
    if a.http:
        os.environ.setdefault("CUTWARDEN_ALLOW_LOCAL", "0")      # remote clients must not copy server files
    if not a.http:
        mcp.run()
        return
    secret = os.environ.get("CUTWARDEN_GATEWAY_SECRET", "")
    if secret:
        _serve_gated(a.host, a.port, secret)
        return
    try:
        mcp.run(transport="streamable-http", host=a.host, port=a.port)
    except TypeError:
        mcp.settings.host, mcp.settings.port = a.host, a.port
        mcp.run(transport="streamable-http")


def _serve_gated(host: str, port: int, secret: str):
    """Serve over HTTP but only answer requests that carry the shared secret in the
    X-Cutwarden-Gateway header (set by the Cloudflare Worker). "/" and "/health" stay open for host health checks."""
    import hmac
    import uvicorn
    try:
        inner = mcp.streamable_http_app(host=host)
    except TypeError:
        inner = mcp.streamable_http_app()
    want = secret.encode()

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        if scope["path"] in ("/", "/health"):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": b"cutwarden ok"})
        got = dict(scope["headers"]).get(b"x-cutwarden-gateway", b"")
        if not hmac.compare_digest(got, want):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": b"unauthorized"})
        return await inner(scope, receive, send)

    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
