"""Preset library. The model picks presets and fills parameters; it never writes animation code.

Every overlay/effect preset is `fn(ctx, layer, t, frame) -> frame` where `t` is seconds since the layer
started and `frame` is an HxWx3 uint8 array. Sizes are fractions of canvas width so presets are
resolution independent (preview at 360p, export at 1080p).
"""
from __future__ import annotations

import math
import os
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .brand import BrandKit, BrandError
from .easing import clamp, ease, lerp, out_cubic, in_cubic


class PresetError(ValueError):
    pass


# ---------------------------------------------------------------- parameter specs
@dataclass
class P:
    type: str                       # float int bool str color path words
    default: Any = None
    doc: str = ""
    required: bool = False
    choices: list | None = None
    lo: float | None = None
    hi: float | None = None

    def coerce(self, key, v, brand, pname):
        try:
            if self.type in ("float", "int"):
                v = float(v) if self.type == "float" else int(v)
                if self.lo is not None and v < self.lo or self.hi is not None and v > self.hi:
                    raise PresetError(f"'{key}' must be between {self.lo} and {self.hi}, got {v}")
            elif self.type == "bool":
                if isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "yes", "on")
                v = bool(v)
            elif self.type == "str":
                v = str(v)
                if self.choices and v not in self.choices:
                    raise PresetError(f"'{key}' must be one of {self.choices}, got '{v}'")
            elif self.type == "color":
                v = str(v)
                if brand is not None:
                    brand.color(v)
            elif self.type == "path":
                v = os.path.expanduser(str(v))
                if not os.path.exists(v):
                    raise PresetError(f"file not found for '{key}': {v} (use an absolute path)")
            elif self.type == "words":
                v = normalize_words(v)
        except BrandError as e:
            raise PresetError(f"'{key}': {e}") from None
        except (TypeError, ValueError) as e:
            if isinstance(e, PresetError):
                raise
            raise PresetError(f"'{key}' for '{pname}' expects {self.type}: {e}") from None
        return v


def normalize_words(v) -> list:
    out = []
    for w in v:
        if isinstance(w, dict):
            out.append([str(w.get("w") or w.get("word") or w["text"]), float(w.get("s", w.get("start"))), float(w.get("e", w.get("end")))])
        else:
            out.append([str(w[0]), float(w[1]), float(w[2])])
    return out


@dataclass
class Preset:
    name: str
    kind: str
    doc: str
    params: dict
    fn: Callable | None = None
    bbox: Callable | None = None     # (ctx, layer) -> (x0, y0, x1, y1) at rest, for QA
    special: bool = False            # handled by the renderer (footage / audio)

    def describe(self):
        return {"name": self.name, "kind": self.kind, "doc": self.doc,
                "params": {k: {"type": s.type, "default": s.default, "required": s.required,
                               **({"choices": s.choices} if s.choices else {}),
                               **({"range": [s.lo, s.hi]} if s.lo is not None else {}), "doc": s.doc}
                           for k, s in self.params.items()}}


PRESETS: dict[str, Preset] = {}


def preset(name, kind, doc, params, bbox=None, special=False):
    def deco(fn):
        PRESETS[name] = Preset(name, kind, doc, params, fn, bbox, special)
        return fn
    return deco


def validate_params(p: Preset, given: dict, brand: BrandKit | None = None) -> dict:
    unknown = [k for k in given if k not in p.params]
    if unknown:
        raise PresetError(f"unknown param(s) {unknown} for '{p.name}'. valid: {sorted(p.params)}")
    out = {}
    for k, spec in p.params.items():
        if k in given:
            out[k] = spec.coerce(k, given[k], brand, p.name)
        elif spec.required:
            raise PresetError(f"'{p.name}' requires param '{k}' ({spec.doc})")
        else:
            out[k] = spec.default
    return out


# ---------------------------------------------------------------- render context
class Ctx:
    def __init__(self, brand: BrandKit, W: int, H: int, fps: int):
        self.brand, self.W, self.H, self.fps = brand, W, H, fps
        self.cache: dict = {}

    def px(self, frac: float) -> int:
        return max(1, int(round(frac * self.W)))


# ---------------------------------------------------------------- text layout
def layout_words(ctx, text, font_role, size_px, max_w_px, align, line_spacing):
    font = ctx.brand.font(font_role, size_px)
    space = font.getlength(" ")
    lines = []
    for para in text.split("\n"):
        cur, cw = [], 0.0
        for w in para.split():
            ww = font.getlength(w)
            if cur and cw + space + ww > max_w_px:
                lines.append((cur, cw))
                cur, cw = [], 0.0
            cw += (space if cur else 0) + ww
            cur.append((w, ww))
        lines.append((cur, cw))
    block_w = max((cw for _, cw in lines), default=0.0)
    lh = size_px * line_spacing
    words = []
    for li, (ws, cw) in enumerate(lines):
        x = {"left": 0.0, "center": (block_w - cw) / 2, "right": block_w - cw}[align]
        for w, ww in ws:
            words.append({"text": w, "x": x, "y": li * lh, "w": ww, "line": li})
            x += ww + space
    return {"font": font, "words": words, "w": block_w, "h": len(lines) * lh, "lh": lh, "size": size_px, "space": space}


def _backing(ctx, p):
    b = p.get("backing", "none")
    if b == "dark":
        return ctx.brand.color("bg", 205)
    if b == "accent":
        return ctx.brand.color("accent")
    return None


def get_layout(ctx, p):
    key = ("lay", p["text"], p["font"], p["size"], p["align"], p["max_width"], p["line_spacing"])
    if key not in ctx.cache:
        size = ctx.px(p["size"])
        ctx.cache[key] = layout_words(ctx, p["text"], p["font"], size, ctx.W * p["max_width"], p["align"], p["line_spacing"])
    return ctx.cache[key]


def render_block(ctx, p, lay, word_fn=None, pre=None):
    pad = int(lay["size"] * 0.3)
    img = Image.new("RGBA", (int(lay["w"]) + 2 * pad, int(lay["h"]) + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    br = _backing(ctx, p)
    if br:
        d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=int(lay["size"] * 0.3), fill=br)
    if pre:
        pre(d, lay, pad)
    sw = int(round(p["stroke"] * lay["size"]))
    base = ctx.brand.color(p["color"])
    for i, w in enumerate(lay["words"]):
        r = word_fn(i, w) if word_fn else (w["text"], base)
        if r is None:
            continue
        txt, fill = r
        d.text((pad + w["x"], pad + w["y"]), txt, font=lay["font"], fill=fill, anchor="la",
               stroke_width=sw, stroke_fill=ctx.brand.color("bg"))
    return img, pad


def blit(ctx, frame, sprite, states):
    """Composite `sprite` at each state (cx, cy, scale, rot, opacity); average = motion blur."""
    W, H = ctx.W, ctx.H
    placed = []
    for s in states:
        img, sc = sprite, s.get("scale", 1.0)
        if abs(sc - 1) > 1e-3:
            img = img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))), Image.BILINEAR)
        if s.get("rot"):
            img = img.rotate(s["rot"], expand=True, resample=Image.BILINEAR)
        placed.append((img, int(round(s["cx"] - img.width / 2)), int(round(s["cy"] - img.height / 2)), s.get("opacity", 1.0)))
    x0 = max(0, min(p[1] for p in placed)); y0 = max(0, min(p[2] for p in placed))
    x1 = min(W, max(p[1] + p[0].width for p in placed)); y1 = min(H, max(p[2] + p[0].height for p in placed))
    if x1 <= x0 or y1 <= y0:
        return frame
    bw, bh = x1 - x0, y1 - y0
    acc = np.zeros((bh, bw, 4), np.float32)
    for img, x, y, op in placed:
        if op <= 0:
            continue
        reg = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        reg.paste(img, (x - x0, y - y0))
        a = np.asarray(reg, np.float32) / 255.0
        a[..., :3] *= a[..., 3:4]
        acc += a * op
    acc /= len(placed)
    alpha = acc[..., 3:4]
    region = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = np.clip(region * (1 - alpha) + acc[..., :3] * 255, 0, 255).astype(np.uint8)
    return frame


def _out_env(t, dur, out_t):
    return clamp((dur - t) / out_t) if out_t > 0 else 1.0


def _center(ctx, p):
    return ctx.W * p["x"], ctx.H * p["y"]


def text_bbox(ctx, layer):
    p = layer.params
    lay = get_layout(ctx, p)
    cx, cy = _center(ctx, p)
    pad = lay["size"] * 0.3 if p.get("backing", "none") != "none" else 0
    return (cx - lay["w"] / 2 - pad, cy - lay["h"] / 2 - pad, cx + lay["w"] / 2 + pad, cy + lay["h"] / 2 + pad)


def _text_params(**extra):
    base = {
        "text": P("str", required=True, doc="the text. use \\n for manual line breaks"),
        "font": P("str", "display", "brand font role: display or body"),
        "size": P("float", 0.11, "font size as a fraction of video width", lo=0.02, hi=0.4),
        "color": P("color", "fg", "brand color name"),
        "align": P("str", "center", "text alignment", choices=["left", "center", "right"]),
        "x": P("float", 0.5, "horizontal centre as fraction of width", lo=0, hi=1),
        "y": P("float", 0.5, "vertical centre as fraction of height", lo=0, hi=1),
        "max_width": P("float", 0.84, "wrap width as fraction of video width", lo=0.2, hi=1),
        "line_spacing": P("float", 1.05, "line height multiplier", lo=0.8, hi=2),
        "in_time": P("float", 0.35, "seconds for the entrance animation", lo=0.05, hi=3),
        "out_time": P("float", 0.2, "seconds for the fade-out at the end", lo=0, hi=3),
        "backing": P("str", "none", "plate behind text for legibility over footage", choices=["none", "dark", "accent"]),
        "stroke": P("float", 0.0, "outline width as fraction of font size (uses bg color)", lo=0, hi=0.2),
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------- text presets
@preset("slam_in", "text", "Big text slams in from oversize with motion blur. The signature punchy kinetic-type entrance.",
        _text_params(scale_from=P("float", 1.9, "starting scale", lo=1, hi=4),
                     blur=P("bool", True, "motion blur during the slam")), bbox=text_bbox)
def slam_in(ctx, layer, t, frame):
    p, dur = layer.params, layer.duration
    key = ("spr", layer.id, repr(sorted(p.items())))
    if key not in ctx.cache:
        ctx.cache[key] = render_block(ctx, p, get_layout(ctx, p))[0]
    cx, cy = _center(ctx, p)

    def state(tt):
        pi = clamp(tt / p["in_time"])
        po = _out_env(tt, dur, p["out_time"])
        return {"cx": cx, "cy": cy, "scale": lerp(p["scale_from"], 1.0, ease("out_expo", pi)) * (1 + 0.04 * (1 - po)),
                "opacity": clamp(pi * 5) * po}
    n = 5 if (p["blur"] and t < p["in_time"]) else 1
    shutter = 1.6 / ctx.fps
    return blit(ctx, frame, ctx.cache[key], [state(t - k * shutter / n) for k in range(n)])


@preset("fade_up", "text", "Text rises and fades in. Calm, clean, good for secondary lines.", _text_params(
    rise=P("float", 0.03, "rise distance as fraction of height", lo=0, hi=0.2)), bbox=text_bbox)
def fade_up(ctx, layer, t, frame):
    p, dur = layer.params, layer.duration
    key = ("spr", layer.id, repr(sorted(p.items())))
    if key not in ctx.cache:
        ctx.cache[key] = render_block(ctx, p, get_layout(ctx, p))[0]
    cx, cy = _center(ctx, p)
    e = ease("out_cubic", t / p["in_time"])
    return blit(ctx, frame, ctx.cache[key], [{"cx": cx, "cy": cy + (1 - e) * p["rise"] * ctx.H, "opacity": e * _out_env(t, dur, p["out_time"])}])


@preset("typewriter", "text", "Characters type out with a blinking accent cursor.", _text_params(
    chars_per_sec=P("float", 22, "typing speed", lo=2, hi=120)), bbox=text_bbox)
def typewriter(ctx, layer, t, frame):
    p, dur = layer.params, layer.duration
    lay = get_layout(ctx, p)
    n = int(t * p["chars_per_sec"])
    base = ctx.brand.color(p["color"])
    seen = [0]

    def word_fn(i, w):
        start = seen[0]
        seen[0] += len(w["text"]) + 1
        k = n - start
        if k <= 0:
            return None
        return w["text"][:k], base
    img, pad = render_block(ctx, p, lay, word_fn)
    total = sum(len(w["text"]) + 1 for w in lay["words"])
    if n < total + 40 and int(t * 2) % 2 == 0:       # blinking cursor
        shown = [w for i, w in enumerate(lay["words"]) if sum(len(x["text"]) + 1 for x in lay["words"][:i]) < n] or lay["words"][:1]
        w = shown[-1]
        k = min(len(w["text"]), max(0, n - sum(len(x["text"]) + 1 for x in lay["words"][:lay["words"].index(w)])))
        cxp = pad + w["x"] + lay["font"].getlength(w["text"][:k]) + lay["size"] * 0.06
        ImageDraw.Draw(img).rectangle([cxp, pad + w["y"] + lay["size"] * 0.1, cxp + lay["size"] * 0.07, pad + w["y"] + lay["size"] * 1.0],
                                      fill=ctx.brand.color("accent"))
    cx, cy = _center(ctx, p)
    return blit(ctx, frame, img, [{"cx": cx, "cy": cy, "opacity": _out_env(t, dur, p["out_time"])}])


_SCRAMBLE = "#%&*+=<>/\\|0123456789XZ"


def _hash01(*vals) -> float:
    return float(np.random.default_rng(zlib.crc32(repr(vals).encode())).random())


@preset("glitch_letters", "text", "Letters decode from scrambled characters, then the text glitches (slice shifts) now and then.",
        _text_params(glitch_rate=P("float", 0.06, "chance per 1/8s of a glitch burst while held", lo=0, hi=1)), bbox=text_bbox)
def glitch_letters(ctx, layer, t, frame):
    p, dur = layer.params, layer.duration
    lay = get_layout(ctx, p)
    base, acc_c = ctx.brand.color(p["color"]), ctx.brand.color("accent")
    total = max(1, sum(len(w["text"]) for w in lay["words"]))
    stagger = p["in_time"] / total
    fidx = int(t * ctx.fps)
    counter = [0]
    sprite = Image.new("RGBA", (int(lay["w"]) + 2 * int(lay["size"] * 0.3), int(lay["h"]) + 2 * int(lay["size"] * 0.3)), (0, 0, 0, 0))
    pad = int(lay["size"] * 0.3)
    d = ImageDraw.Draw(sprite)
    br = _backing(ctx, p)
    if br:
        d.rounded_rectangle([0, 0, sprite.width - 1, sprite.height - 1], radius=int(lay["size"] * 0.3), fill=br)
    rng = np.random.default_rng(fidx * 9973 + 17)
    for w in lay["words"]:
        for k, ch in enumerate(w["text"]):
            r = counter[0] * stagger
            counter[0] += 1
            if t < r - 0.12:
                continue
            x = pad + w["x"] + lay["font"].getlength(w["text"][:k])
            if t < r:
                ch, fill = _SCRAMBLE[int(rng.integers(len(_SCRAMBLE)))], acc_c
            else:
                fill = base
            d.text((x, pad + w["y"]), ch, font=lay["font"], fill=fill, anchor="la")
    glitching = t < p["in_time"] + 0.1 or _hash01("g", layer.id, int(t * 8)) < p["glitch_rate"]
    if glitching:                                    # horizontal slice displacement
        arr = np.asarray(sprite).copy()
        H = arr.shape[0]
        for _ in range(3):
            y0 = int(rng.integers(0, max(1, H - 4))); h = int(rng.integers(max(2, H // 12), max(3, H // 4)))
            arr[y0:y0 + h] = np.roll(arr[y0:y0 + h], int(rng.integers(-1, 2) * ctx.W * 0.035 * rng.random()), axis=1)
        sprite = Image.fromarray(arr, "RGBA")
    cx, cy = _center(ctx, p)
    return blit(ctx, frame, sprite, [{"cx": cx, "cy": cy, "opacity": _out_env(t, dur, p["out_time"])}])


def _norm(w: str) -> str:
    return "".join(c for c in w.lower() if c.isalnum())


@preset("highlight_box", "text", "Text with chosen words highlighted by an accent box that wipes in behind them.", _text_params(
    highlight=P("str", required=True, doc="comma-separated words to highlight, e.g. 'VIDEO,FREE'"),
    box_color=P("color", "accent", "box color (brand color)"),
    box_text_color=P("color", "fg", "text color inside the box"),
    wipe_time=P("float", 0.25, "seconds for the box wipe", lo=0.05, hi=2),
    delay=P("float", 0.2, "seconds before the wipe starts", lo=0, hi=3)), bbox=text_bbox)
def highlight_box(ctx, layer, t, frame):
    p, dur = layer.params, layer.duration
    lay = get_layout(ctx, p)
    targets = {_norm(x) for x in p["highlight"].split(",") if x.strip()}
    prog = ease("out_expo", (t - p["delay"]) / p["wipe_time"])
    base, boxc, boxt = ctx.brand.color(p["color"]), ctx.brand.color(p["box_color"]), ctx.brand.color(p["box_text_color"])
    font, size = lay["font"], lay["size"]
    ct, cb = font.getbbox("Hg", anchor="la")[1::2]

    def pre(d, lay_, pad):
        runs, cur = [], None
        for i, w in enumerate(lay_["words"]):
            if _norm(w["text"]) in targets:
                if cur and cur["line"] == w["line"] and cur["end"] == i - 1:
                    cur["x1"] = w["x"] + w["w"]; cur["end"] = i
                else:
                    cur = {"line": w["line"], "x0": w["x"], "x1": w["x"] + w["w"], "y": w["y"], "end": i}
                    runs.append(cur)
        for r in runs:
            px_ = size * 0.12
            x0, x1 = pad + r["x0"] - px_, pad + r["x1"] + px_
            d.rectangle([x0, pad + r["y"] + ct - size * 0.08, x0 + (x1 - x0) * prog, pad + r["y"] + cb + size * 0.04], fill=boxc)

    def word_fn(i, w):
        hit = _norm(w["text"]) in targets
        return w["text"], (boxt if hit and prog > 0.35 else base)
    img, _ = render_block(ctx, p, lay, word_fn, pre)
    cx, cy = _center(ctx, p)
    e = ease("out_cubic", t / p["in_time"])
    return blit(ctx, frame, img, [{"cx": cx, "cy": cy + (1 - e) * 0.025 * ctx.H, "opacity": e * _out_env(t, dur, p["out_time"])}])


@preset("word_captions", "text", "Word-by-word captions synced to timestamps; the spoken word pops in accent color.", {
    "words": P("words", required=True, doc="list of [word, start_s, end_s] relative to this layer's start (from transcribe_audio)"),
    "words_per_line": P("int", 3, "words shown together", lo=1, hi=6),
    "font": P("str", "display", "brand font role"),
    "size": P("float", 0.085, "font size as fraction of width", lo=0.03, hi=0.3),
    "color": P("color", "fg", "inactive word color"),
    "active_color": P("color", "accent", "spoken word color"),
    "y": P("float", 0.70, "vertical centre as fraction of height", lo=0, hi=1),
    "uppercase": P("bool", True, "force uppercase"),
    "stroke": P("float", 0.08, "outline as fraction of font size", lo=0, hi=0.2),
})
def word_captions(ctx, layer, t, frame):
    p = layer.params
    words = p["words"]
    if not words:
        return frame
    gi = None
    for i, (_, s, e) in enumerate(words):
        if s <= t:
            gi = i
    if gi is None:
        return frame
    n = p["words_per_line"]
    g0 = (gi // n) * n
    group = words[g0:g0 + n]
    if t > group[-1][2] + 0.4:
        return frame
    size = ctx.px(p["size"])
    font = ctx.brand.font(p["font"], size)
    txt = [(w.upper() if p["uppercase"] else w) for w, _, _ in group]
    space = font.getlength(" ")
    widths = [font.getlength(w) for w in txt]
    total = sum(widths) + space * (len(txt) - 1)
    pad = int(size * 0.4)
    img = Image.new("RGBA", (int(total) + 2 * pad, int(size * 1.4) + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = pad
    sw = int(round(p["stroke"] * size))
    for k, (w, ww) in enumerate(zip(txt, widths)):
        active = (g0 + k) == gi
        fill = ctx.brand.color(p["active_color"] if active else p["color"])
        if active:
            pop = 1 + 0.12 * (1 - clamp((t - group[k][1]) / 0.12))
            wi = Image.new("RGBA", (int(ww) + 2 * pad, int(size * 1.4) + 2 * pad), (0, 0, 0, 0))
            ImageDraw.Draw(wi).text((pad, pad), w, font=font, fill=fill, anchor="la", stroke_width=sw, stroke_fill=ctx.brand.color("bg"))
            wi = wi.resize((int(wi.width * pop), int(wi.height * pop)), Image.BILINEAR)
            img.alpha_composite(wi, (max(0, int(x - pad - (wi.width - ww - 2 * pad) / 2)), max(0, int(-(wi.height - img.height) / 2))))
        else:
            d.text((x, pad), w, font=font, fill=fill, anchor="la", stroke_width=sw, stroke_fill=ctx.brand.color("bg"))
        x += ww + space
    return blit(ctx, frame, img, [{"cx": ctx.W * 0.5, "cy": ctx.H * p["y"], "opacity": 1.0}])


# ---------------------------------------------------------------- logo
def _logo_sprite(ctx, p):
    key = ("logo", p["width"])
    if key in ctx.cache:
        return ctx.cache[key]
    target_w = ctx.px(p["width"])
    path = ctx.brand.logo_path()
    if path:
        im = Image.open(path).convert("RGBA")
        im = im.resize((target_w, max(1, int(im.height * target_w / im.width))), Image.LANCZOS)
    else:                                            # no logo file: fall back to a wordmark in the display font
        size = int(target_w / max(4, len(ctx.brand.name)) * 1.9)
        font = ctx.brand.font("display", size)
        w = int(font.getlength(ctx.brand.name)) + 4
        im = Image.new("RGBA", (w, int(size * 1.3)), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((2, 0), ctx.brand.name, font=font, fill=ctx.brand.color("fg"), anchor="la")
    ctx.cache[key] = im
    return im


def logo_bbox(ctx, layer):
    im = _logo_sprite(ctx, layer.params)
    cx, cy = ctx.W * layer.params["x"], ctx.H * layer.params["y"]
    return (cx - im.width / 2, cy - im.height / 2, cx + im.width / 2, cy + im.height / 2)


@preset("logo_reveal", "logo", "Brand logo (or wordmark) pops in with an accent glow and an underline sweep.", {
    "width": P("float", 0.5, "logo width as fraction of video width", lo=0.05, hi=1),
    "x": P("float", 0.5, "centre x fraction", lo=0, hi=1),
    "y": P("float", 0.5, "centre y fraction", lo=0, hi=1),
    "glow": P("float", 0.8, "accent glow strength", lo=0, hi=1),
    "underline": P("bool", True, "accent underline sweeping in"),
    "in_time": P("float", 0.5, "entrance seconds", lo=0.1, hi=3),
    "out_time": P("float", 0.25, "fade-out seconds", lo=0, hi=3),
}, bbox=logo_bbox)
def logo_reveal(ctx, layer, t, frame):
    p, dur = layer.params, layer.duration
    logo = _logo_sprite(ctx, p)
    cx, cy = ctx.W * p["x"], ctx.H * p["y"]
    po = _out_env(t, dur, p["out_time"])
    e = ease("out_back", t / p["in_time"])
    if p["glow"] > 0:
        key = ("glow", p["width"], p["glow"])
        if key not in ctx.cache:
            pad = int(logo.width * 0.25)
            g = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
            mask = logo.split()[3]
            tint = Image.new("RGBA", logo.size, ctx.brand.color("accent"))
            g.paste(tint, (pad, pad), mask)
            ctx.cache[key] = g.filter(ImageFilter.GaussianBlur(max(2, logo.width * 0.05)))
        pulse = 0.75 + 0.25 * math.sin(t * 5)
        blit(ctx, frame, ctx.cache[key], [{"cx": cx, "cy": cy, "scale": lerp(0.7, 1.0, e), "opacity": clamp(t / p["in_time"]) * po * p["glow"] * pulse}])
    blit(ctx, frame, logo, [{"cx": cx, "cy": cy, "scale": lerp(0.6, 1.0, e), "opacity": clamp(t / (p["in_time"] * 0.5)) * po}])
    if p["underline"]:
        prog = ease("out_expo", (t - 0.15) / 0.5)
        half = logo.width * 0.55 * prog
        y = int(cy + logo.height * 0.62)
        th = max(2, int(ctx.H * 0.003))
        x0, x1 = int(max(0, cx - half)), int(min(ctx.W, cx + half))
        if x1 > x0 and 0 <= y < ctx.H - th:
            c = np.array(ctx.brand.color("accent")[:3], np.float32)
            seg = frame[y:y + th, x0:x1].astype(np.float32)
            frame[y:y + th, x0:x1] = (seg * (1 - po) + c * po).astype(np.uint8)
    return frame


# ---------------------------------------------------------------- effects (post-process everything below them)
def _shift_x(frame, dx):
    W = frame.shape[1]
    idx = np.clip(np.arange(W) + int(dx), 0, W - 1)
    return frame[:, idx]


@preset("whip", "effect", "Whip-pan transition: the picture slides off with horizontal motion blur and slides back in. "
        "Center it on a cut (start = cut_time - duration/2) to hide the cut.", {
    "direction": P("str", "left", "pan direction", choices=["left", "right"]),
    "intensity": P("float", 0.8, "blur strength", lo=0, hi=1.5),
})
def whip(ctx, layer, t, frame):
    p = layer.params
    pr = clamp(t / layer.duration)
    amount = math.sin(math.pi * pr)
    if amount < 0.01:
        return frame
    sign = 1 if p["direction"] == "left" else -1
    s = in_cubic(pr / 0.5) * 0.55 if pr < 0.5 else -in_cubic((1 - pr) / 0.5) * 0.55
    shift = sign * s * ctx.W
    blur = amount * p["intensity"] * ctx.W * 0.12
    n = 9 if blur > 3 else 1
    acc = np.zeros(frame.shape, np.float32)
    for k in range(n):
        off = shift + ((k / (n - 1) - 0.5) * blur if n > 1 else 0)
        acc += _shift_x(frame, off)
    return (acc / n).astype(np.uint8)


@preset("flash", "effect", "Quick brightness flash that decays. Good on beats and cuts.", {
    "color": P("color", "fg", "flash color (brand color)"),
    "strength": P("float", 0.8, "peak opacity", lo=0, hi=1),
})
def flash(ctx, layer, t, frame):
    p = layer.params
    a = p["strength"] * (1 - clamp(t / layer.duration)) ** 2
    c = np.array(ctx.brand.color(p["color"])[:3], np.float32)
    return (frame.astype(np.float32) * (1 - a) + c * a).astype(np.uint8)


@preset("shake", "effect", "Camera shake that decays. Pair with slam_in for impact.", {
    "amount": P("float", 0.014, "peak shake as fraction of width", lo=0, hi=0.1),
    "decay": P("bool", True, "fade the shake out over the duration"),
})
def shake(ctx, layer, t, frame):
    p = layer.params
    amp = p["amount"] * ctx.W * ((1 - clamp(t / layer.duration)) ** 1.5 if p["decay"] else 1)
    m = int(amp) + 2
    rng = np.random.default_rng(int(round(t * ctx.fps)) + 7919)
    dx, dy = (rng.uniform(-1, 1, 2) * amp).astype(int)
    padded = np.pad(frame, ((m, m), (m, m), (0, 0)), mode="edge")
    return np.ascontiguousarray(padded[m + dy:m + dy + ctx.H, m + dx:m + dx + ctx.W])


@preset("zoom_punch", "effect", "Fast push-in that settles back. Adds energy on a beat.", {
    "amount": P("float", 0.12, "extra zoom at the start", lo=0, hi=0.5),
})
def zoom_punch(ctx, layer, t, frame):
    s = 1 + layer.params["amount"] * (1 - out_cubic(clamp(t / layer.duration)))
    if s < 1.003:
        return frame
    w, h = ctx.W / s, ctx.H / s
    box = ((ctx.W - w) / 2, (ctx.H - h) / 2, (ctx.W + w) / 2, (ctx.H + h) / 2)
    return np.array(Image.fromarray(frame).resize((ctx.W, ctx.H), Image.BILINEAR, box=box))


@preset("vignette", "effect", "Darkens the edges to focus attention.", {
    "strength": P("float", 0.5, "edge darkness", lo=0, hi=1),
})
def vignette(ctx, layer, t, frame):
    s = layer.params["strength"]
    key = ("vig", s)
    if key not in ctx.cache:
        y, x = np.mgrid[0:ctx.H, 0:ctx.W].astype(np.float32)
        r = np.sqrt(((x - ctx.W / 2) / (ctx.W / 2)) ** 2 + ((y - ctx.H / 2) / (ctx.H / 2)) ** 2) / 1.414
        ctx.cache[key] = (1 - s * np.clip(r, 0, 1) ** 2.2)[..., None].astype(np.float32)
    return (frame.astype(np.float32) * ctx.cache[key]).astype(np.uint8)


@preset("rgb_split", "effect", "Chromatic aberration (red/blue channel split). Digital, glitchy feel.", {
    "amount": P("float", 0.012, "channel offset as fraction of width", lo=0, hi=0.06),
    "decay": P("bool", True, "fade out over the duration"),
})
def rgb_split(ctx, layer, t, frame):
    p = layer.params
    d = int(p["amount"] * ctx.W * ((1 - clamp(t / layer.duration)) if p["decay"] else 1))
    if d < 1:
        return frame
    out = frame.copy()
    out[..., 0] = _shift_x(frame, d)[..., 0]
    out[..., 2] = _shift_x(frame, -d)[..., 2]
    return out


@preset("scanline", "effect", "Glowing accent line that sweeps out from the center. Tech/cinematic accent.", {
    "y": P("float", 0.62, "vertical position fraction", lo=0, hi=1),
    "thickness": P("float", 0.003, "line thickness as fraction of height", lo=0.001, hi=0.02),
    "color": P("color", "accent", "brand color"),
})
def scanline(ctx, layer, t, frame):
    p = layer.params
    prog = ease("out_expo", t / 0.4)
    po = _out_env(t, layer.duration, 0.2)
    yc = int(ctx.H * p["y"])
    th = max(2, int(ctx.H * p["thickness"]))
    glow = int(ctx.H * 0.03)
    y0, y1 = max(0, yc - glow), min(ctx.H, yc + glow)
    half = ctx.W * 0.5 * prog
    x0, x1 = int(ctx.W / 2 - half), int(ctx.W / 2 + half)
    if x1 <= x0 or y1 <= y0:
        return frame
    ys = np.arange(y0, y1)
    prof = np.exp(-(((ys - yc) / (glow * 0.35)) ** 2)) * 0.6 + (np.abs(ys - yc) <= th // 2) * 0.9
    a = np.clip(prof, 0, 1)[:, None, None] * po
    c = np.array(ctx.brand.color(p["color"])[:3], np.float32)
    seg = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = np.clip(seg * (1 - a) + c * a, 0, 255).astype(np.uint8)
    return frame


# ---------------------------------------------------------------- overlay (image / GIF / sticker placed on the frame)
@preset("overlay", "overlay", "An image, PNG with transparency, sticker or animated GIF placed at a position and size on top of the footage "
        "(product shot, emoji, reaction GIF, picture-in-picture). Enters with pop/slide/fade and leaves with a fade.", {
    "path": P("path", required=True, doc="absolute path to a png/jpg/webp/gif (import it first with import_media)"),
    "x": P("float", 0.5, "center x, fraction of width", lo=-0.5, hi=1.5),
    "y": P("float", 0.5, "center y, fraction of height", lo=-0.5, hi=1.5),
    "width": P("float", 0.5, "width as a fraction of canvas width", lo=0.02, hi=2.0),
    "animation": P("str", "pop", "entrance", choices=["pop", "fade", "slide_up", "slide_left", "slide_right", "none"]),
    "in_time": P("float", 0.35, "entrance seconds", lo=0, hi=3),
    "out_time": P("float", 0.2, "fade-out seconds", lo=0, hi=3),
    "rotation": P("float", 0.0, "degrees", lo=-180, hi=180),
    "opacity": P("float", 1.0, "overall opacity", lo=0, hi=1),
    "shadow": P("bool", False, "soft drop shadow (good for stickers on busy footage)"),
    "loop": P("bool", True, "loop an animated GIF/WebP"),
    "speed": P("float", 1.0, "GIF playback speed", lo=0.1, hi=4),
})
def overlay(ctx, layer, t, frame):
    from . import media
    p = layer.params
    fkey = ("frames", p["path"])
    if fkey not in ctx.cache:
        ctx.cache[fkey] = media.load_frames(p["path"])
    frames, starts, total = ctx.cache[fkey]
    idx = media.frame_index(starts, total, t, p["loop"], p["speed"])
    wpx = max(2, int(ctx.W * p["width"]))
    key = ("ov", p["path"], idx, wpx)
    if key not in ctx.cache:
        im = frames[idx]
        ctx.cache[key] = im.resize((wpx, max(2, round(im.height * wpx / im.width))), Image.LANCZOS)
        if len([k for k in ctx.cache if k[0] == "ov"]) > 300:
            ctx.cache.pop(next(k for k in ctx.cache if k[0] == "ov" and k != key))
    sprite = ctx.cache[key]
    pin = clamp(t / p["in_time"]) if p["in_time"] > 0 else 1.0
    op = p["opacity"] * _out_env(t, layer.duration, p["out_time"])
    cx, cy, sc, anim = ctx.W * p["x"], ctx.H * p["y"], 1.0, p["animation"]
    e = ease("out_back", pin) if anim == "pop" else out_cubic(pin)
    if anim == "pop":
        sc = max(0.01, e)
    elif anim == "fade":
        op *= pin
    elif anim == "slide_up":
        cy += (1 - e) * ctx.H * 0.12; op *= pin
    elif anim in ("slide_left", "slide_right"):
        cx += (1 - e) * ctx.W * 0.15 * (1 if anim == "slide_left" else -1); op *= pin
    if op <= 0.001:
        return frame
    if p["shadow"]:
        sh = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
        a = sprite.getchannel("A").point(lambda v: int(v * 0.55))
        sh.putalpha(a)
        pad = max(4, int(wpx * 0.06))
        big = Image.new("RGBA", (sprite.width + 2 * pad, sprite.height + 2 * pad), (0, 0, 0, 0))
        big.paste(sh, (pad, pad))
        big = big.filter(ImageFilter.GaussianBlur(pad * 0.7))
        frame = blit(ctx, frame, big, [{"cx": cx, "cy": cy + pad * 0.6, "scale": sc, "rot": p["rotation"], "opacity": op}])
    return blit(ctx, frame, sprite, [{"cx": cx, "cy": cy, "scale": sc, "rot": p["rotation"], "opacity": op}])


# ---------------------------------------------------------------- more effects
def _env(t, dur, in_t, out_t):
    return min(clamp(t / in_t) if in_t > 0 else 1.0, _out_env(t, dur, out_t))


@preset("color_grade", "effect", "Color grade everything beneath: contrast, saturation, brightness, warmth, plus an optional brand-color tint. "
        "Put one at the top for a consistent look, or time it for a mood shift.", {
    "contrast": P("float", 1.12, "1 = unchanged", lo=0.3, hi=2.5),
    "saturation": P("float", 1.1, "0 = black and white, 1 = unchanged", lo=0, hi=2.5),
    "brightness": P("float", 0.0, "-1..1 added", lo=-1, hi=1),
    "warmth": P("float", 0.0, "-1 cool .. 1 warm", lo=-1, hi=1),
    "tint": P("color", "accent", "brand color used for tinting"),
    "tint_amount": P("float", 0.0, "0..1 blend toward the tint color", lo=0, hi=1),
    "fade_time": P("float", 0.3, "seconds to ease in and out", lo=0, hi=3),
})
def color_grade(ctx, layer, t, frame):
    p = layer.params
    k = _env(t, layer.duration, p["fade_time"], p["fade_time"])
    f = frame.astype(np.float32) / 255.0
    c = 1 + (p["contrast"] - 1) * k
    f = (f - 0.5) * c + 0.5 + p["brightness"] * 0.5 * k
    g = (f * np.array([0.299, 0.587, 0.114], np.float32)).sum(-1, keepdims=True)
    f = g + (f - g) * (1 + (p["saturation"] - 1) * k)
    w = p["warmth"] * 0.08 * k
    f[..., 0] += w; f[..., 2] -= w
    if p["tint_amount"] > 0:
        tc = np.array(ctx.brand.color(p["tint"])[:3], np.float32) / 255.0
        f = f * (1 - p["tint_amount"] * k) + tc * p["tint_amount"] * k * (0.4 + g)
    return (np.clip(f, 0, 1) * 255).astype(np.uint8)


@preset("film_grain", "effect", "Animated film grain for a tactile, analog look. Subtle by default.", {
    "amount": P("float", 0.06, "grain strength", lo=0, hi=0.4),
    "size": P("float", 1.0, "grain size multiplier", lo=0.5, hi=4),
})
def film_grain(ctx, layer, t, frame):
    p = layer.params
    rng = np.random.default_rng(int(round(t * ctx.fps)) * 7919 + 13)
    s = max(1, int(round(p["size"] * ctx.W / 540)))
    h, w = -(-ctx.H // s), -(-ctx.W // s)
    n = rng.standard_normal((h, w)).astype(np.float32)
    if s > 1:
        n = np.repeat(np.repeat(n, s, 0), s, 1)[:ctx.H, :ctx.W]
    out = frame.astype(np.float32) + (n * p["amount"] * 255)[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


@preset("blur_pulse", "effect", "Gaussian blur that rises and falls (or only releases, with mode=reveal). "
        "Great for a soft focus pull into a title or hiding a cut.", {
    "amount": P("float", 0.02, "peak blur radius as fraction of width", lo=0, hi=0.08),
    "mode": P("str", "pulse", "pulse = in and out, reveal = start blurred and focus in", choices=["pulse", "reveal"]),
})
def blur_pulse(ctx, layer, t, frame):
    p = layer.params
    pr = clamp(t / layer.duration)
    k = math.sin(math.pi * pr) if p["mode"] == "pulse" else (1 - out_cubic(pr))
    r = p["amount"] * ctx.W * k
    if r < 0.5:
        return frame
    return np.asarray(Image.fromarray(frame).filter(ImageFilter.GaussianBlur(r)))


@preset("pixelate", "effect", "Pixelation that builds to a peak and resolves. A digital transition, great on a cut.", {
    "blocks": P("int", 24, "block count across the width at peak (lower = chunkier)", lo=6, hi=200),
})
def pixelate(ctx, layer, t, frame):
    k = math.sin(math.pi * clamp(t / layer.duration))
    if k < 0.05:
        return frame
    blocks = layer.params["blocks"]
    bw = max(blocks, int(ctx.W * (blocks / ctx.W) ** k))        # full res -> `blocks` across, geometric
    bh = max(6, int(bw * ctx.H / ctx.W))
    im = Image.fromarray(frame)
    return np.asarray(im.resize((bw, bh), Image.BILINEAR).resize((ctx.W, ctx.H), Image.NEAREST))


@preset("letterbox", "effect", "Cinematic black bars that slide in (and out). Makes any footage feel filmic.", {
    "size": P("float", 0.11, "bar height as fraction of canvas height", lo=0.02, hi=0.3),
    "in_time": P("float", 0.5, "seconds to slide in", lo=0, hi=3),
    "out_time": P("float", 0.4, "seconds to slide out", lo=0, hi=3),
    "color": P("color", "bg", "bar color (brand color)"),
})
def letterbox(ctx, layer, t, frame):
    p = layer.params
    k = ease("out_expo", _env(t, layer.duration, p["in_time"], p["out_time"]))
    h = int(ctx.H * p["size"] * k)
    if h < 1:
        return frame
    c = np.array(ctx.brand.color(p["color"])[:3], np.uint8)
    frame[:h] = c
    frame[ctx.H - h:] = c
    return frame


@preset("bloom", "effect", "Soft glow around bright areas (dreamy highlights). Good with logo reveals and bright text.", {
    "strength": P("float", 0.6, "glow amount", lo=0, hi=1.5),
    "radius": P("float", 0.025, "glow radius as fraction of width", lo=0.005, hi=0.08),
    "threshold": P("float", 0.6, "only pixels brighter than this glow", lo=0, hi=1),
})
def bloom(ctx, layer, t, frame):
    p = layer.params
    k = _env(t, layer.duration, 0.2, 0.2)
    f = frame.astype(np.float32) / 255.0
    lum = (f * np.array([0.299, 0.587, 0.114], np.float32)).sum(-1, keepdims=True)
    bright = np.clip((lum - p["threshold"]) / max(1e-3, 1 - p["threshold"]), 0, 1) * f
    small = Image.fromarray((bright * 255).astype(np.uint8)).resize((ctx.W // 4, ctx.H // 4), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(max(1.0, p["radius"] * ctx.W / 4)))
    glow = np.asarray(small.resize((ctx.W, ctx.H), Image.BILINEAR), np.float32) / 255.0
    return (np.clip(f + glow * p["strength"] * k, 0, 1) * 255).astype(np.uint8)


@preset("light_leak", "effect", "A warm brand-color light wash sweeping across the frame. Cinematic accent on a cut or beat.", {
    "color": P("color", "accent", "leak color (brand color)"),
    "strength": P("float", 0.55, "peak intensity", lo=0, hi=1),
    "direction": P("str", "right", "sweep direction", choices=["left", "right"]),
})
def light_leak(ctx, layer, t, frame):
    p = layer.params
    pr = clamp(t / layer.duration)
    k = math.sin(math.pi * pr) ** 1.5 * p["strength"]
    if k < 0.01:
        return frame
    x = np.linspace(0, 1, ctx.W, dtype=np.float32)
    cx = pr if p["direction"] == "right" else 1 - pr
    cx = -0.2 + cx * 1.4
    prof = np.exp(-((x - cx) / 0.28) ** 2)[None, :, None]
    c = np.array(ctx.brand.color(p["color"])[:3], np.float32)
    add = prof * k * c[None, None, :]
    out = 255 - (255 - frame.astype(np.float32)) * (255 - add) / 255      # screen blend
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- footage / audio (handled by the renderer)
PRESETS["clip"] = Preset("clip", "footage", "A video clip from a source file, scaled to fill the canvas (cover). Optional slow push-in.", {
    "path": P("path", required=True, doc="absolute path to a video file"),
    "source_in": P("float", 0.0, "start offset inside the source file, seconds", lo=0),
    "zoom": P("float", 1.0, "end scale for a slow push-in (1.0 = none, 1.08 = subtle)", lo=1.0, hi=1.5),
    "audio": P("bool", True, "use the clip's own audio, if it has any"),
    "volume": P("float", 1.0, "audio volume", lo=0, hi=2),
    "fit": P("str", "cover", "cover crops to fill; contain letterboxes on the brand bg", choices=["cover", "contain"]),
}, special=True)
PRESETS["image"] = Preset("image", "footage", "A full-frame image or animated GIF/WebP for the duration (GIFs loop), with optional slow push-in.", {
    "path": P("path", required=True, doc="absolute path to a png/jpg/webp/gif"),
    "loop": P("bool", True, "loop an animated GIF/WebP (false = hold the last frame)"),
    "speed": P("float", 1.0, "playback speed of an animated GIF/WebP", lo=0.1, hi=4),
    "zoom": P("float", 1.06, "end scale for a slow push-in", lo=1.0, hi=1.5),
    "fit": P("str", "cover", "cover or contain", choices=["cover", "contain"]),
}, special=True)
PRESETS["music"] = Preset("music", "audio", "Background music bed, trimmed and faded to length.", {
    "path": P("path", required=True, doc="absolute path to an audio file"),
    "source_in": P("float", 0.0, "start offset in the file, seconds", lo=0),
    "volume": P("float", 0.25, "music level (voice stays at 1.0)", lo=0, hi=2),
    "fade_in": P("float", 0.5, "seconds", lo=0, hi=10),
    "fade_out": P("float", 1.0, "seconds", lo=0, hi=10),
    "duck": P("float", 0.6, "how much the music dips under voice and clip audio (0 = off, 1 = strong)", lo=0, hi=1),
}, special=True)
PRESETS["voice"] = Preset("voice", "audio", "Voiceover or dialogue track.", {
    "path": P("path", required=True, doc="absolute path to an audio file"),
    "source_in": P("float", 0.0, "start offset in the file, seconds", lo=0),
    "volume": P("float", 1.0, "level", lo=0, hi=2),
}, special=True)
