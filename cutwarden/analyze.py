"""Footage understanding. Lets the model search footage instead of guessing timestamps."""
from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


class AnalyzeError(RuntimeError):
    pass


def _run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path: str) -> dict:
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    if r.returncode:
        raise AnalyzeError(f"cannot read '{path}': {r.stderr.strip()[:300]}")
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    out = {"path": path, "duration": round(float(j["format"].get("duration", 0)), 3), "has_audio": a is not None}
    if v:
        num, _, den = v.get("avg_frame_rate", "0/1").partition("/")
        out.update(width=v["width"], height=v["height"], fps=round(float(num) / float(den or 1), 2) if float(den or 1) else None,
                   codec=v["codec_name"])
    return out


def detect_scenes(path: str, threshold: float = 0.3) -> list[float]:
    """Timestamps (s) where the picture changes sharply. Good candidate cut points."""
    r = _run(["ffmpeg", "-hide_banner", "-i", path, "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"])
    return [round(float(x), 3) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def detect_silence(path: str, noise_db: float = -35, min_len: float = 0.4) -> list[dict]:
    r = _run(["ffmpeg", "-hide_banner", "-i", path, "-af", f"silencedetect=noise={noise_db}dB:d={min_len}", "-vn", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.\-]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.\-]+)", r.stderr)]
    return [{"start": round(s, 3), "end": round(e, 3)} for s, e in zip(starts, ends)]


def transcribe(path: str, model: str = "base") -> list[list]:
    """Word-level transcript as [[word, start, end], ...]. Needs: pip install faster-whisper"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise AnalyzeError("transcription needs faster-whisper: pip install faster-whisper (or pip install cutwarden[transcribe])") from None
    segs, _ = WhisperModel(model, compute_type="int8").transcribe(path, word_timestamps=True)
    return [[w.word.strip(), round(w.start, 3), round(w.end, 3)] for s in segs for w in s.words]


def source_contact_sheet(path: str, n: int = 12, cols: int = 4, width: int = 200) -> bytes:
    """Evenly spaced thumbnails of a source video with timestamps, so the model can 'see' its footage."""
    from PIL import ImageDraw
    dur = probe(path)["duration"]
    times = [dur * (i + 0.5) / n for i in range(n)]
    tiles = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        if r.stdout:
            tiles.append((t, Image.open(io.BytesIO(r.stdout)).convert("RGB")))
    if not tiles:
        raise AnalyzeError("could not extract any frames")
    tw, th = tiles[0][1].size
    rows = -(-len(tiles) // cols)
    sheet = Image.new("RGB", (cols * (tw + 6) + 6, rows * (th + 6) + 6), (30, 30, 30))
    for i, (t, im) in enumerate(tiles):
        x, y = 6 + (i % cols) * (tw + 6), 6 + (i // cols) * (th + 6)
        sheet.paste(im, (x, y))
        ImageDraw.Draw(sheet).text((x + 4, y + 4), f"{t:.1f}s", fill=(255, 255, 0))
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()
