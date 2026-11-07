"""Quality gates. The model runs these and fixes what they report before delivering."""
from __future__ import annotations

import os
import re
import subprocess

from .analyze import probe
from .brand import BrandKit, contrast_ratio
from .presets import PRESETS
from .project import Project
from .render import make_ctx


def _issue(sev, layer, code, msg):
    return {"severity": sev, "layer": layer, "code": code, "message": msg}


def _overlap(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return inter / area


def validate_project(proj: Project) -> dict:
    """Static checks on the timeline: files, safe zones, contrast, readability, overlaps, platform limits."""
    brand = BrandKit.load(proj.brand)
    ctx = make_ctx(proj, 1.0)
    issues = []
    dur = proj.duration()
    if not proj.layers:
        issues.append(_issue("error", None, "empty", "project has no layers"))
    if proj.platform:
        plat = brand.platforms.get(proj.platform)
        if not plat:
            issues.append(_issue("error", None, "platform", f"unknown platform '{proj.platform}'. known: {sorted(brand.platforms)}"))
        else:
            if dur > plat["max_duration"]:
                issues.append(_issue("error", None, "too_long", f"{dur:.1f}s exceeds the {plat['max_duration']}s limit for {proj.platform}"))
            aw, ah = plat["aspect"]
            if abs(proj.width / proj.height - aw / ah) > 0.01:
                issues.append(_issue("warn", None, "aspect", f"canvas {proj.width}x{proj.height} is not {aw}:{ah} (recommended for {proj.platform})"))
    footage = [l for l in proj.layers if l.kind == "footage"]
    safe = brand.safe_rect(ctx.W, ctx.H)
    texts = []
    for l in proj.layers:
        for k, v in l.params.items():
            if k == "path" and not os.path.exists(v):
                issues.append(_issue("error", l.id, "missing_file", f"file not found: {v}"))
        if l.kind == "footage" and l.preset == "clip" and os.path.exists(l.params["path"]):
            src = probe(l.params["path"])
            if l.params["source_in"] + l.duration > src["duration"] + 0.05:
                issues.append(_issue("warn", l.id, "past_end", f"clip needs {l.params['source_in'] + l.duration:.2f}s of source but file is {src['duration']:.2f}s; last frame will freeze"))
        if l.kind in ("text", "logo"):
            bb = PRESETS[l.preset].bbox(ctx, l) if PRESETS[l.preset].bbox else None
            if bb:
                if bb[0] < 0 or bb[1] < 0 or bb[2] > ctx.W or bb[3] > ctx.H:
                    issues.append(_issue("error", l.id, "off_canvas", "text/logo extends beyond the canvas; lower 'size' or widen 'max_width'"))
                elif bb[0] < safe[0] or bb[1] < safe[1] or bb[2] > safe[2] or bb[3] > safe[3]:
                    issues.append(_issue("warn", l.id, "safe_zone", f"outside the brand safe zone (platform UI may cover it). safe rect px: {safe}"))
                texts.append((l, bb))
        if l.kind == "text" and "color" in l.params and l.preset != "word_captions":
            col = brand.color(l.params["color"])
            under = [f for f in footage if f.start < l.end and f.end > l.start]
            if l.params.get("backing", "none") != "none":
                bg = brand.color("bg" if l.params["backing"] == "dark" else "accent")
                if contrast_ratio(col, bg) < 3:
                    issues.append(_issue("warn", l.id, "contrast", f"low contrast ({contrast_ratio(col, bg):.1f}:1) against the backing plate"))
            elif under:
                if l.params.get("stroke", 0) < 0.04:
                    issues.append(_issue("info", l.id, "legibility", "text over footage with no backing or stroke; check legibility with render_frame or set backing='dark'"))
            elif contrast_ratio(col, brand.color("bg")) < 3:
                issues.append(_issue("warn", l.id, "contrast", f"low contrast ({contrast_ratio(col, brand.color('bg')):.1f}:1) against the background"))
            words = len(l.params.get("text", "").split())
            if words >= 3 and words / max(l.duration, 0.01) > 4.5:
                issues.append(_issue("warn", l.id, "too_fast", f"{words} words in {l.duration:.2f}s is too fast to read (aim for <= 4 words/sec)"))
            if l.duration < l.params.get("in_time", 0) + 0.3 and words:
                issues.append(_issue("warn", l.id, "too_short", "layer ends right after its entrance finishes; hold it longer or shorten in_time"))
    for i, (a, ba) in enumerate(texts):
        for b, bb in texts[i + 1:]:
            if a.start < b.end and b.start < a.end and a.kind == b.kind == "text" and _overlap(ba, bb) > 0.25:
                issues.append(_issue("warn", a.id, "overlap", f"overlaps '{b.id}' on screen at the same time"))
    first = [l for l in proj.layers if l.kind in ("text", "footage", "logo", "overlay") and l.start <= 0.5]
    if proj.layers and not first:
        issues.append(_issue("warn", None, "weak_hook", "nothing visible in the first 0.5s; add a hook on screen at t=0"))
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in ("error", "warn", "info")}
    return {"ok": counts["error"] == 0, "counts": counts, "issues": issues}


def qa_video(path: str, brand: BrandKit | None = None, expect_wh: tuple[int, int] | None = None) -> dict:
    """Checks on the rendered file: resolution, loudness, true peak, black frames."""
    info = probe(path)
    issues = []
    if expect_wh and (info.get("width"), info.get("height")) != tuple(expect_wh):
        issues.append(_issue("warn", None, "resolution", f"rendered {info.get('width')}x{info.get('height')}, expected {expect_wh[0]}x{expect_wh[1]}"))
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"], capture_output=True, text=True)
    lufs = re.findall(r"I:\s+(-?[0-9.]+) LUFS", r.stderr)
    peak = re.findall(r"Peak:\s+(-?[0-9.]+) dBFS", r.stderr)
    loud = {"integrated_lufs": float(lufs[-1]) if lufs else None, "true_peak_dbfs": float(peak[-1]) if peak else None}
    target = brand.loudness_target if brand else -14.0
    if not info["has_audio"]:
        issues.append(_issue("info", None, "no_audio", "no audio track"))
    elif loud["integrated_lufs"] is not None and abs(loud["integrated_lufs"] - target) > 2:
        issues.append(_issue("warn", None, "loudness", f"{loud['integrated_lufs']} LUFS vs target {target}"))
    if loud["true_peak_dbfs"] is not None and loud["true_peak_dbfs"] > -1:
        issues.append(_issue("warn", None, "clipping", f"true peak {loud['true_peak_dbfs']} dBFS risks clipping"))
    b = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf", "blackdetect=d=0.4:pix_th=0.08", "-an", "-f", "null", "-"], capture_output=True, text=True)
    black = [{"start": float(s), "end": float(e)} for s, e in re.findall(r"black_start:([0-9.]+) black_end:([0-9.]+)", b.stderr)]
    if black and (brand is None or brand.colors.get("bg", "#000000").lower() in ("#000000", "#000")):
        pass  # brand bg is black: black frames may be intentional, so only report as info
    for seg in black:
        issues.append(_issue("info", None, "black_frames", f"black from {seg['start']}s to {seg['end']}s (intentional?)"))
    return {"ok": not any(i["severity"] == "error" for i in issues), "file": info, "loudness": loud, "issues": issues}
