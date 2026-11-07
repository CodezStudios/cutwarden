"""A 6-second brand teaser built only from cutwarden tool calls (synthetic footage)."""
import subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cutwarden.project import Editor, ProjectStore
from cutwarden import render, qa
from cutwarden.brand import BrandKit

work = Path(tempfile.mkdtemp(prefix="bc_edit_"))
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
for i, src in enumerate(("gradients=s=1080x1920:d=4:speed=0.04:r=30", "gradients=s=1080x1920:d=4:speed=0.05:r=30:c0=0x0A0A0C:c1=0xE5262D")):
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", src, "-pix_fmt", "yuv420p", str(work / f"c{i}.mp4"))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=7", "-f", "lavfi", "-i", "sine=frequency=165:duration=7",
   "-filter_complex", "amix=inputs=2,tremolo=f=2:d=0.5", str(work / "music.wav"))

brand = ROOT / "brands" / "example" / "brand.json"
ed = Editor(ProjectStore(work / "p"))
ed.create("edit", str(brand), platform="reels")
A = lambda *a, **k: ed.add_layer("edit", *a, **k)
A("footage", "clip", 0, 3.1, {"path": str(work / "c0.mp4"), "zoom": 1.1, "audio": False})
A("footage", "clip", 3.1, 2.9, {"path": str(work / "c1.mp4"), "zoom": 1.1, "audio": False})
A("effect", "vignette", 0, 6, {"strength": 0.5})
A("text", "slam_in", 0.2, 1.3, {"text": "ONE PROMPT.", "size": 0.14, "y": 0.42, "backing": "dark"})
A("effect", "shake", 0.2, 0.3, {})
A("text", "highlight_box", 1.6, 1.4, {"text": "ZERO\nEDITING", "highlight": "ZERO", "size": 0.14, "y": 0.42, "backing": "dark"})
A("effect", "whip", 2.95, 0.3, {"direction": "left"})
A("text", "glitch_letters", 3.3, 1.5, {"text": "ALWAYS\nON BRAND", "size": 0.115, "y": 0.42, "backing": "dark"})
A("effect", "rgb_split", 3.3, 0.35, {})
A("logo", "logo_reveal", 4.9, 1.1, {"width": 0.7, "y": 0.5})
A("effect", "flash", 4.9, 0.25, {})
A("audio", "music", 0, 6, {"path": str(work / "music.wav"), "volume": 0.4})
proj, _ = A("effect", "zoom_punch", 0.2, 0.3, {})
print("validate:", qa.validate_project(proj))
open(work / "sheet.png", "wb").write(render.contact_sheet_png(proj, [i * 0.5 + 0.3 for i in range(12)]))
out = work / "short_edit.mp4"
info = render.render_video(proj, str(out), scale=0.5)
print(info)
print(qa.qa_video(str(out), BrandKit.load(brand), tuple(map(int, info["size"].split("x")))))
print(work)
