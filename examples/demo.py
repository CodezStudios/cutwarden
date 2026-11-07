"""Build and render a short kinetic-type brand video from synthetic footage. Run: python examples/demo.py"""
import subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cutwarden.project import Editor, ProjectStore
from cutwarden import render, qa
from cutwarden.brand import BrandKit

work = Path(tempfile.mkdtemp(prefix="cutwarden_demo_"))
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
for i, src in enumerate(("testsrc2=size=1080x1920:rate=30:duration=6", "gradients=s=1080x1920:d=6:speed=0.03:r=30")):
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", src, "-pix_fmt", "yuv420p", str(work / f"clip{i}.mp4"))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=10", str(work / "music.wav"))

brand = ROOT / "brands" / "example" / "brand.json"
ed = Editor(ProjectStore(work / "projects"))
ed.create("demo", str(brand), platform="reels")
A = lambda *a, **k: ed.add_layer("demo", *a, **k)
A("footage", "clip", 0, 4.5, {"path": str(work / "clip0.mp4"), "zoom": 1.08})
A("footage", "clip", 4.5, 4.5, {"path": str(work / "clip1.mp4"), "zoom": 1.08})
A("effect", "vignette", 0, 9, {"strength": 0.55})
A("text", "slam_in", 0.2, 1.4, {"text": "STOP EDITING\nBY HAND", "size": 0.13, "y": 0.4, "backing": "dark"})
A("effect", "shake", 0.2, 0.35, {})
A("text", "highlight_box", 1.7, 1.5, {"text": "EDITED NOT\nGENERATED", "highlight": "EDITED", "size": 0.12, "y": 0.4})
A("text", "glitch_letters", 3.3, 1.2, {"text": "ON BRAND.\nEVERY FRAME.", "size": 0.12, "y": 0.4, "color": "fg", "backing": "dark"})
A("effect", "rgb_split", 3.3, 0.4, {})
A("effect", "whip", 4.35, 0.3, {"direction": "left"})
A("text", "typewriter", 4.9, 2.2, {"text": "Make a 15-second ad\nfrom my product clips", "size": 0.07, "y": 0.35, "backing": "dark", "align": "left"})
A("effect", "scanline", 5.0, 3.0, {"y": 0.5})
A("text", "word_captions", 5.2, 3.0, {"words": [["Brand", 0.0, 0.4], ["safe", 0.4, 0.8], ["edits", 0.8, 1.2], ["by", 1.4, 1.6], ["AI", 1.6, 2.0]], "y": 0.72})
A("logo", "logo_reveal", 7.0, 2.0, {"width": 0.55, "y": 0.5})
A("effect", "flash", 7.0, 0.25, {})
A("audio", "music", 0, 9, {"path": str(work / "music.wav"), "volume": 0.3})
proj, _ = A("effect", "zoom_punch", 0.2, 0.3, {})
print("validate:", qa.validate_project(proj)["counts"])
for t in (0.6, 2.4, 4.0, 4.5, 6.0, 7.6):
    open(work / f"f_{t}.png", "wb").write(render.render_frame_png(proj, t, 0.4))
open(work / "sheet.png", "wb").write(render.contact_sheet_png(proj, [i * 9 / 12 + 0.3 for i in range(12)]))
t0 = time.time()
info = render.render_video(proj, str(work / "demo.mp4"), scale=float(sys.argv[1]) if len(sys.argv) > 1 else 1.0)
print(info, f"{time.time() - t0:.1f}s ->", f"{(time.time() - t0) / info['frames'] * 1000:.0f} ms/frame wall")
print(qa.qa_video(str(work / "demo.mp4"), BrandKit.load(brand), (info['size'].split('x')[0] and tuple(map(int, info['size'].split('x'))))))
print("output dir:", work)
