"""GIF playback, overlay placement, every new effect, and chunked upload."""
import base64, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from PIL import Image
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cutwarden import imports, render
from cutwarden.project import Editor, ProjectStore

w = Path(tempfile.mkdtemp(prefix="bc_gif_"))
# 3-frame animated GIF (red, green, blue squares with transparent corner), 0.2s per frame
fr = []
for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
    im = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (100, 100), (*c, 255)), (10, 10))
    fr.append(im)
fr[0].save(w / "anim.gif", save_all=True, append_images=fr[1:], duration=200, loop=0, disposal=2)

ed = Editor(ProjectStore(w / "p"))
ed.create("g", str(ROOT / "brands/example/brand.json"), 540, 960, 30, "reels")
A = lambda *a, **k: ed.add_layer("g", *a, **k)
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=3", "-pix_fmt", "yuv420p", str(w / "bg.mp4"))
A("footage", "clip", 0, 3, {"path": str(w / "bg.mp4")}, None, None)
A("overlay", "overlay", 0, 3, {"path": str(w / "anim.gif"), "width": 0.4, "animation": "none", "shadow": True}, None, None)
proj, _ = A("footage", "image", 0, 0, {"path": str(w / "anim.gif")}, None, None) if False else (ed.store.load("g"), None)

def center_rgb(t):
    ctx = render.make_ctx(proj, 1.0)
    f = render.compose(proj, ctx, t, {}, single=True)
    return f[ctx.H // 2, ctx.W // 2]
c0, c1, c2 = center_rgb(0.05), center_rgb(0.25), center_rgb(0.45)
print("overlay gif centre pixels over time:", c0, c1, c2)
assert c0[0] > 200 and c0[1] < 60 and c1[1] > 200 and c1[0] < 60 and c2[2] > 200 and c2[0] < 60, "gif frames not advancing"
assert tuple(center_rgb(0.65)) == tuple(c0), "gif should loop (0.6s cycle)"

# full-frame GIF via image preset
ed.create("g2", str(ROOT / "brands/example/brand.json"), 540, 960, 30, "reels")
ed.add_layer("g2", "footage", "image", 0, 2, {"path": str(w / "anim.gif"), "fit": "contain"}, None, None)
p2 = ed.store.load("g2"); ctx2 = render.make_ctx(p2, 1.0)
a = render.compose(p2, ctx2, 0.05, {}, single=True)[480, 270]; b = render.compose(p2, ctx2, 0.25, {}, single=True)[480, 270]
assert a[0] > 200 and b[1] > 200, (a, b); print("ok image preset animates GIF")

# every new effect renders without error and changes the frame
effects = {"color_grade": {"saturation": 0.0, "tint_amount": 0.3}, "film_grain": {}, "blur_pulse": {}, "pixelate": {},
           "letterbox": {}, "bloom": {}, "light_leak": {}}
for name, params in effects.items():
    ed.create("e_" + name, str(ROOT / "brands/example/brand.json"), 540, 960, 30, "reels")
    ed.add_layer("e_" + name, "footage", "clip", 0, 2, {"path": str(w / "bg.mp4")}, None, None)
    ed.add_layer("e_" + name, "effect", name, 0, 2, params, None, None)
    pe = ed.store.load("e_" + name); ctx = render.make_ctx(pe, 1.0)
    base = render.compose(type(pe)(**{**pe.model_dump(), "layers": [l for l in pe.layers if l.kind != "effect"]}), ctx, 1.0, {}, single=True)
    out = render.compose(pe, ctx, 1.0, {}, single=True)
    diff = np.abs(out.astype(int) - base.astype(int)).mean()
    print(f"{name:12s} mean change {diff:6.2f}")
    assert out.shape == base.shape and diff > 0.3, f"{name} had no visible effect"

# chunked upload round trip
data = (w / "anim.gif").read_bytes()
u = imports.upload_begin(w, "pushed.gif")
half = len(data) // 2
imports.upload_chunk(w, u["upload_id"], 0, base64.b64encode(data[:half]).decode())
try: imports.upload_chunk(w, u["upload_id"], 5, "AAAA"); raise SystemExit("out of order accepted")
except imports.ImportError_ as e: print("ok order enforced:", str(e)[:50])
imports.upload_chunk(w, u["upload_id"], 1, base64.b64encode(data[half:]).decode())
info = imports.upload_finish(w, u["upload_id"]); assert info["width"] == 120, info
print("ok chunked upload", info["asset"])
print("ALL GIF/EFFECT TESTS OK")
