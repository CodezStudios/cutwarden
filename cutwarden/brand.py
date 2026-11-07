"""Brand kit: the only source of colors, fonts, logo and layout rules the model may use."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import ImageFont

FALLBACK_FONTS = [
    "/usr/share/fonts/opentype/inter/Inter-Black.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

DEFAULT_PLATFORMS = {
    "tiktok": {"max_duration": 600, "aspect": [9, 16]},
    "reels": {"max_duration": 90, "aspect": [9, 16]},
    "shorts": {"max_duration": 60, "aspect": [9, 16]},
    "feed": {"max_duration": 120, "aspect": [4, 5]},
}


class BrandError(ValueError):
    pass


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise BrandError(f"bad hex color '{h}'")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


@dataclass
class BrandKit:
    name: str
    colors: dict
    fonts: dict
    logo: str | None = None
    safe: dict = field(default_factory=lambda: {"top": 0.12, "bottom": 0.22, "left": 0.06, "right": 0.10})
    loudness_target: float = -14.0
    allow_custom_colors: bool = False
    platforms: dict = field(default_factory=lambda: dict(DEFAULT_PLATFORMS))
    root: Path = Path(".")
    _font_cache: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path) -> "BrandKit":
        p = Path(path)
        data = json.loads(p.read_text())
        kit = cls(
            name=data["name"], colors=data["colors"], fonts=data["fonts"], logo=data.get("logo"),
            safe={**{"top": 0.12, "bottom": 0.22, "left": 0.06, "right": 0.10}, **data.get("safe", {})},
            loudness_target=data.get("loudness_target", -14.0),
            allow_custom_colors=data.get("allow_custom_colors", False),
            platforms={**DEFAULT_PLATFORMS, **data.get("platforms", {})},
            root=p.parent,
        )
        for need in ("bg", "fg", "accent"):
            if need not in kit.colors:
                raise BrandError(f"brand kit must define color '{need}'")
        if "display" not in kit.fonts:
            raise BrandError("brand kit must define font role 'display'")
        return kit

    # ---- colors -------------------------------------------------------
    def color(self, ref: str, alpha: int = 255):
        if ref in self.colors:
            return (*hex_to_rgb(self.colors[ref]), alpha)
        if isinstance(ref, str) and ref.startswith("#"):
            if not self.allow_custom_colors:
                raise BrandError(f"custom color '{ref}' is not allowed by this brand kit. use one of {sorted(self.colors)}")
            return (*hex_to_rgb(ref), alpha)
        raise BrandError(f"unknown brand color '{ref}'. use one of {sorted(self.colors)}")

    # ---- fonts --------------------------------------------------------
    def font(self, role: str, size_px: int):
        if role not in self.fonts:
            raise BrandError(f"unknown font role '{role}'. use one of {sorted(self.fonts)}")
        key = (role, size_px)
        if key not in self._font_cache:
            cands = [str((self.root / self.fonts[role]).resolve()), self.fonts[role], *FALLBACK_FONTS]
            for c in cands:
                if os.path.exists(c):
                    self._font_cache[key] = ImageFont.truetype(c, size_px)
                    break
            else:
                self._font_cache[key] = ImageFont.load_default(size_px)
        return self._font_cache[key]

    def logo_path(self):
        if not self.logo:
            return None
        p = self.root / self.logo
        return str(p) if p.exists() else None

    def safe_rect(self, w: int, h: int):
        return (int(w * self.safe["left"]), int(h * self.safe["top"]),
                int(w * (1 - self.safe["right"])), int(h * (1 - self.safe["bottom"])))

    def describe(self) -> dict:
        return {"name": self.name, "colors": self.colors, "font_roles": list(self.fonts),
                "has_logo": bool(self.logo_path()), "safe_zone_fractions": self.safe,
                "custom_colors_allowed": self.allow_custom_colors,
                "loudness_target_lufs": self.loudness_target, "platforms": self.platforms}
