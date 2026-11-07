"""Still and animated images (png/jpg/webp/gif). Animated files are decoded once and sampled by time, so renders stay deterministic."""
from __future__ import annotations

from PIL import Image, ImageSequence


def load_frames(path: str):
    """-> (frames: list[RGBA Image], starts: list[float], total: float). Static images give one frame."""
    im = Image.open(path)
    frames, durs = [], []
    for fr in ImageSequence.Iterator(im):
        frames.append(fr.convert("RGBA"))
        durs.append(max(0.02, (fr.info.get("duration", 100) or 100) / 1000.0))
    if not frames:
        frames, durs = [im.convert("RGBA")], [1.0]
    starts, acc = [], 0.0
    for d in durs:
        starts.append(acc)
        acc += d
    return frames, starts, acc


def frame_index(starts: list[float], total: float, t: float, loop: bool = True, speed: float = 1.0) -> int:
    if len(starts) == 1:
        return 0
    t = max(0.0, t * speed)
    t = t % total if loop else min(t, total - 1e-6)
    i = 0
    for k, s in enumerate(starts):
        if s <= t:
            i = k
        else:
            break
    return i
