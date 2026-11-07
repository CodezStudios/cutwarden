"""Easing curves. Every curve maps 0..1 -> 0..1 (back/elastic may overshoot)."""
import math


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def linear(x): return x
def in_cubic(x): return x ** 3
def out_cubic(x): return 1 - (1 - x) ** 3
def in_out_cubic(x): return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2
def out_expo(x): return 1.0 if x >= 1 else 1 - 2 ** (-10 * x)
def in_expo(x): return 0.0 if x <= 0 else 2 ** (10 * x - 10)


def out_back(x, s=1.70158):
    x -= 1
    return x * x * ((s + 1) * x + s) + 1


def out_elastic(x):
    if x in (0, 1):
        return x
    return 2 ** (-10 * x) * math.sin((x * 10 - 0.75) * (2 * math.pi) / 3) + 1


EASINGS = {
    "linear": linear, "in_cubic": in_cubic, "out_cubic": out_cubic,
    "in_out_cubic": in_out_cubic, "out_expo": out_expo, "in_expo": in_expo,
    "out_back": out_back, "out_elastic": out_elastic,
}


def ease(name: str, x: float) -> float:
    if name not in EASINGS:
        raise ValueError(f"unknown easing '{name}'. valid: {sorted(EASINGS)}")
    return EASINGS[name](clamp(x))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t
