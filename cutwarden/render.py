"""Deterministic renderer. Same project + same time = same pixels. No browser, no GPU, no clock."""
from __future__ import annotations

import io
import json
import math
import sys
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from .brand import BrandKit, hex_to_rgb
from .presets import Ctx, PRESETS
from .project import Layer, Project


class RenderError(RuntimeError):
    pass


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def make_ctx(proj: Project, scale: float = 1.0) -> Ctx:
    brand = BrandKit.load(proj.brand)
    return Ctx(brand, _even(proj.width * scale), _even(proj.height * scale), proj.fps)


def _ordered(proj: Project) -> list[Layer]:
    return sorted((l for l in proj.layers if l.kind != "audio"), key=lambda l: (l.z, l.start))


# ------------------------------------------------------------------ footage
def _fit_filter(W, H, fit, bg_hex):
    if fit == "contain":
        return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x{bg_hex.lstrip('#')}")
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"


class FootageReader:
    """Streams decoded, canvas-sized RGB frames from ffmpeg. frame_at(i) is sequential and holds the last frame at EOF."""

    def __init__(self, path, src_start, ctx: Ctx, fit, t0, frames=None):
        vf = f"fps={ctx.fps},{_fit_filter(ctx.W, ctx.H, fit, ctx.brand.colors['bg'])}"
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, src_start):.3f}", "-i", path, "-vf", vf, "-an",
               "-f", "rawvideo", "-pix_fmt", "rgb24"]
        if frames:
            cmd += ["-frames:v", str(frames)]
        self.p = subprocess.Popen(cmd + ["-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.shape = (ctx.H, ctx.W, 3)
        self.size = ctx.W * ctx.H * 3
        self.idx, self.last, self.eof, self.t0, self.fps = -1, None, False, t0, ctx.fps

    def _read(self):
        buf = self.p.stdout.read(self.size)
        while buf is not None and len(buf) < self.size:
            more = self.p.stdout.read(self.size - len(buf))
            if not more:
                return None
            buf += more
        return buf if buf and len(buf) == self.size else None

    def frame_at_time(self, t):
        want = int(round((t - self.t0) * self.fps))
        while self.idx < want and not self.eof:
            buf = self._read()
            if buf is None:
                self.eof = True
                break
            self.last = np.frombuffer(buf, np.uint8).reshape(self.shape)
            self.idx += 1
        return self.last

    def close(self):
        try:
            self.p.stdout.close()
            self.p.kill()
            self.p.wait(timeout=2)
        except Exception:
            pass


def _zoom(frame: np.ndarray, s: float) -> np.ndarray:
    if s <= 1.0005:
        return frame
    H, W = frame.shape[:2]
    w, h = W / s, H / s
    box = ((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2)
    return np.array(Image.fromarray(frame).resize((W, H), Image.BILINEAR, box=box))


def _image_frame(ctx: Ctx, path: str, fit: str, t: float = 0.0, loop: bool = True, speed: float = 1.0) -> np.ndarray:
    """Canvas-sized RGB frame of an image; animated GIF/WebP are sampled at time t (seconds since the layer started)."""
    from . import media
    fkey = ("frames", path)
    if fkey not in ctx.cache:
        ctx.cache[fkey] = media.load_frames(path)
    frames, starts, total = ctx.cache[fkey]
    idx = media.frame_index(starts, total, t, loop, speed)
    key = ("img", path, fit, idx)
    if key not in ctx.cache:
        im = frames[idx]
        bg = Image.new("RGBA", im.size, (*hex_to_rgb(ctx.brand.colors["bg"]), 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
        sc = (max if fit == "cover" else min)(ctx.W / im.width, ctx.H / im.height)
        im = im.resize((max(1, round(im.width * sc)), max(1, round(im.height * sc))), Image.LANCZOS)
        canvas = Image.new("RGB", (ctx.W, ctx.H), hex_to_rgb(ctx.brand.colors["bg"]))
        canvas.paste(im, ((ctx.W - im.width) // 2, (ctx.H - im.height) // 2))
        ctx.cache[key] = np.asarray(canvas)
        if len([k for k in ctx.cache if k[0] == "img"]) > 400:      # bound memory on long GIFs
            ctx.cache.pop(next(k for k in ctx.cache if k[0] == "img" and k != key))
    return ctx.cache[key]


def _draw_footage(ctx, layer: Layer, t, frame, readers, single):
    p, lt = layer.params, t - layer.start
    prog = lt / layer.duration
    zoom = 1 + (p.get("zoom", 1.0) - 1) * (prog * prog * (3 - 2 * prog))
    if layer.preset == "image":
        src = _image_frame(ctx, p["path"], p["fit"], lt, p.get("loop", True), p.get("speed", 1.0))
        return _zoom(src.copy(), zoom)
    if single:
        r = FootageReader(p["path"], p["source_in"] + lt, ctx, p["fit"], t, frames=1)
        try:
            src = r.frame_at_time(t)
        finally:
            r.close()
    else:
        r = readers.get(layer.id)
        if r is None:
            r = readers[layer.id] = FootageReader(p["path"], p["source_in"] + lt, ctx, p["fit"], t)
        src = r.frame_at_time(t)
    if src is None:
        return frame
    return _zoom(src.copy(), zoom)


# ------------------------------------------------------------------ compose
def compose(proj: Project, ctx: Ctx, t: float, readers: dict, single: bool = False) -> np.ndarray:
    frame = np.empty((ctx.H, ctx.W, 3), np.uint8)
    frame[:] = hex_to_rgb(ctx.brand.colors["bg"])
    for layer in _ordered(proj):
        if not (layer.start - 1e-9 <= t < layer.end - 1e-9):
            continue
        if layer.kind == "footage":
            frame = _draw_footage(ctx, layer, t, frame, readers, single)
        else:
            frame = PRESETS[layer.preset].fn(ctx, layer, t - layer.start, frame)
    return frame


def render_frame_png(proj: Project, t: float, scale: float = 0.5) -> bytes:
    ctx = make_ctx(proj, scale)
    frame = compose(proj, ctx, t, {}, single=True)
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, "PNG")
    return buf.getvalue()


def contact_sheet_png(proj: Project, times: list[float], cols: int = 4, scale: float = 0.22) -> bytes:
    ctx = make_ctx(proj, scale)
    tiles = [Image.fromarray(compose(proj, ctx, t, {}, single=True)) for t in times]
    rows = math.ceil(len(tiles) / cols)
    gap = 6
    sheet = Image.new("RGB", (cols * (ctx.W + gap) + gap, rows * (ctx.H + gap) + gap), (30, 30, 30))
    from PIL import ImageDraw
    for i, tile in enumerate(tiles):
        x, y = gap + (i % cols) * (ctx.W + gap), gap + (i // cols) * (ctx.H + gap)
        sheet.paste(tile, (x, y))
        ImageDraw.Draw(sheet).text((x + 4, y + 4), f"{times[i]:.2f}s", fill=(255, 255, 0))
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ video
def _encoder(path, ctx: Ctx, fps):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ctx.W}x{ctx.H}", "-r", str(fps),
         "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-g", str(fps * 2), str(path)],
        stdin=subprocess.PIPE)


def _render_chunk(args):
    proj_json, scale, f0, f1, out = args
    proj = Project.model_validate_json(proj_json)
    ctx = make_ctx(proj, scale)
    readers: dict = {}
    enc = _encoder(out, ctx, proj.fps)
    try:
        for i in range(f0, f1):
            enc.stdin.write(compose(proj, ctx, i / proj.fps, readers).tobytes())
    finally:
        for r in readers.values():
            r.close()
        enc.stdin.close()
        enc.wait()
    return out


def _has_audio(path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _audio_graph(proj: Project, brand: BrandKit, total: float):
    inputs, chains = [], []
    speech, music = [], []                    # labels; music entries are (label, duck)
    others = []
    for l in sorted(proj.layers, key=lambda l: l.start):
        p = l.params
        if l.kind == "audio":
            vol, src_in = p["volume"], p["source_in"]
        elif l.kind == "footage" and l.preset == "clip" and p["audio"] and _has_audio(p["path"]):
            vol, src_in = p["volume"], p["source_in"]
        else:
            continue
        n = len(inputs)
        inputs.append(p["path"])
        ms = int(l.start * 1000)
        f = (f"[{n + 1}:a]atrim=start={src_in:.3f}:duration={l.duration:.3f},asetpts=PTS-STARTPTS,"
             f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol}")
        if l.preset == "music":
            if p["fade_in"] > 0:
                f += f",afade=t=in:st=0:d={p['fade_in']}"
            if p["fade_out"] > 0:
                f += f",afade=t=out:st={max(0.0, l.duration - p['fade_out']):.3f}:d={p['fade_out']}"
        f += f",adelay={ms}|{ms}[a{n}]"
        chains.append(f)
        lab = f"[a{n}]"
        if l.preset == "music":
            music.append((lab, p.get("duck", 0.0)))
        else:
            speech.append(lab)
    if not inputs:
        return [], None
    final = list(others)
    ducked = [(lab, d) for lab, d in music if d > 0] if speech else []
    final += [lab for lab, d in music if (lab, d) not in ducked]
    if ducked:
        # speech bus -> one copy per ducked music track (sidechain) + one copy for the final mix
        k = len(ducked)
        bus = "".join(speech) + (f"amix=inputs={len(speech)}:normalize=0:duration=longest," if len(speech) > 1 else "anull,")
        outs = "".join(f"[sc{i}]" for i in range(k)) + "[spout]"
        chains.append(f"{bus}apad=whole_dur={total:.3f},asplit={k + 1}{outs}")
        for i, (lab, d) in enumerate(ducked):
            ratio = 1 + d * 19
            chains.append(f"{lab}[sc{i}]sidechaincompress=threshold=0.01:ratio={ratio:.2f}:attack=15:release=450:makeup=1[d{i}]")
            final.append(f"[d{i}]")
        final.append("[spout]")
    else:
        final += speech
    mix = (";".join(chains) + f";{''.join(final)}amix=inputs={len(final)}:normalize=0:duration=longest,"
           f"loudnorm=I={brand.loudness_target}:TP=-1.5:LRA=11,atrim=duration={total:.3f}[aout]")
    return inputs, mix


def render_video(proj: Project, out_path: str, scale: float = 1.0, workers: int | None = None) -> dict:
    total = proj.duration()
    if total <= 0:
        raise RenderError("project has no layers")
    fps = proj.fps
    n = int(math.ceil(total * fps))
    workers = max(1, min(workers or os.cpu_count() or 1, 8, max(1, n // 45)))
    brand = BrandKit.load(proj.brand)
    tmp = Path(tempfile.mkdtemp(prefix="cutwarden_"))
    try:
        size = math.ceil(n / workers)
        pj = tmp / "project.json"
        pj.write_text(proj.model_dump_json())
        jobs = [(str(pj), scale, i * size, min(n, (i + 1) * size), str(tmp / f"chunk_{i:03d}.mp4")) for i in range(workers) if i * size < n]
        if len(jobs) == 1:
            _render_chunk((open(pj).read(), *jobs[0][1:]))
        else:                                        # one OS process per chunk; no __main__ re-import pitfalls
            procs = [subprocess.Popen([sys.executable, "-m", "cutwarden.render", *map(str, j)], stderr=subprocess.PIPE, text=True) for j in jobs]
            for pr in procs:
                err = pr.communicate()[1]
                if pr.returncode:
                    for q in procs:
                        q.kill()
                    raise RenderError(f"render worker failed: {err[-500:]}")
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{j[4]}'\n" for j in jobs))
        silent = tmp / "video.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)], check=True)
        a_inputs, mix = _audio_graph(proj, brand, total)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(silent)]
        for a in a_inputs:
            cmd += ["-i", a]
        if mix:
            cmd += ["-filter_complex", mix, "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd += ["-c:v", "copy", "-movflags", "+faststart", "-t", f"{total:.3f}", str(out_path)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"ffmpeg mux failed: {r.stderr[-600:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"path": str(out_path), "frames": n, "fps": fps, "duration": round(total, 3), "workers": len(jobs),
            "size": f"{_even(proj.width * scale)}x{_even(proj.height * scale)}"}


if __name__ == "__main__":      # worker entry: python -m cutwarden.render <project.json> <scale> <f0> <f1> <out.mp4>
    _pj, _scale, _f0, _f1, _out = sys.argv[1:6]
    _render_chunk((open(_pj).read(), float(_scale), int(_f0), int(_f1), _out))
