"""Animated GIF sticker + new effects. Run: python examples/gif_demo.py  (synthetic footage and a generated GIF)"""
import subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cutwarden.project import Editor, ProjectStore
from cutwarden import render, qa
from cutwarden.brand import BrandKit

work = Path(tempfile.mkdtemp(prefix="bc_gifdemo_"))
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "gradients=s=1080x1920:d=6:speed=0.04:r=30", "-pix_fmt", "yuv420p", str(work / "bg.mp4"))
frames = []                                           # a bouncing red ball with transparent background
for i in range(12):
    im = Image.new("RGBA", (300, 300), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    y = 190 - abs(((i / 12) * 2 - 1)) * 0 - 120 * (1 - abs(2 * i / 12 - 1) ** 2)
    d.ellipse((75, y, 225, y + 150), fill=(229, 38, 45, 255), outline=(255, 255, 255, 255), width=8)
    frames.append(im)
frames[0].save(work / "ball.gif", save_all=True, append_images=frames[1:], duration=60, loop=0, disposal=2)

brand = ROOT / "brands/example/brand.json"
ed = Editor(ProjectStore(work / "p")); ed.create("d", str(brand), platform="reels")
A = lambda *a, **k: ed.add_layer("d", *a, **k)
A("footage", "clip", 0, 6, {"path": str(work / "bg.mp4"), "zoom": 1.08, "audio": False})
A("effect", "color_grade", 0, 6, {"contrast": 1.15, "saturation": 0.9, "tint_amount": 0.15})
A("effect", "film_grain", 0, 6, {"amount": 0.05})
A("effect", "letterbox", 0, 6, {"size": 0.08})
A("text", "slam_in", 0.3, 1.6, {"text": "GIFS\nNOW WORK", "size": 0.13, "y": 0.3, "backing": "dark"})
A("overlay", "overlay", 0.6, 4.5, {"path": str(work / "ball.gif"), "x": 0.5, "y": 0.62, "width": 0.5, "animation": "pop", "shadow": True})
A("effect", "light_leak", 2.4, 1.2, {})
A("effect", "pixelate", 3.0, 0.5, {"blocks": 20})
A("text", "fade_up", 3.6, 2.0, {"text": "Overlays. Stickers.\nAnimated.", "size": 0.07, "y": 0.3, "backing": "dark"})
A("overlay", "overlay", 4.2, 1.8, {"path": str(work / "ball.gif"), "x": 0.82, "y": 0.8, "width": 0.18, "animation": "slide_left", "speed": 1.6, "rotation": -12})
proj, _ = A("effect", "bloom", 0, 6, {"strength": 0.35})
print("validate:", qa.validate_project(proj)["counts"])
open(work / "sheet.png", "wb").write(render.contact_sheet_png(proj, [0.4 + i * 0.5 for i in range(12)]))
out = work / "gif_demo.mp4"
print(render.render_video(proj, str(out), scale=0.5))
print(qa.qa_video(str(out), BrandKit.load(brand), (540, 960))["issues"], work)
