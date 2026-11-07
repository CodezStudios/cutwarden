"""Ducking test: music should dip while a voice track plays."""
import subprocess, tempfile, os, re
from pathlib import Path
from cutwarden.project import ProjectStore, Editor
from cutwarden.brand import BrandKit
from cutwarden.render import _audio_graph

w = Path(tempfile.mkdtemp())
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
sh("ffmpeg","-y","-f","lavfi","-i","sine=frequency=220:duration=8",str(w/"m.wav"))
sh("ffmpeg","-y","-f","lavfi","-i","sine=frequency=880:duration=3",str(w/"v.wav"))
store = ProjectStore(w/"p"); ed = Editor(store)
ed.create("t", str(Path(__file__).resolve().parent.parent/"brands/example/brand.json"), 540, 960, 30, "reels")
ed.add_layer("t","audio","music",0,8,{"path":str(w/"m.wav"),"volume":0.5,"fade_in":0,"fade_out":0},None,None)
ed.add_layer("t","audio","voice",3,3,{"path":str(w/"v.wav")},None,None)
proj = store.load("t"); brand = BrandKit.load(proj.brand)
inputs, mix = _audio_graph(proj, brand, 8.0)
mix = re.sub(r"loudnorm=[^,]*,", "", mix)   # isolate ducking from loudness normalization
cmd = ["ffmpeg","-y","-f","lavfi","-i","color=c=black:s=64x64:d=8"]
for i in inputs: cmd += ["-i", i]
cmd += ["-filter_complex", mix, "-map","[aout]", str(w/"o.wav")]
sh(*cmd)
def level(a, b, f):   # mean level of a band-passed window (atrim is exact; -ss on input is not)
    r = subprocess.run(["ffmpeg","-i",str(w/"o.wav"),"-af",f"atrim=start={a}:end={b},{f},volumedetect","-f","null","-"],capture_output=True,text=True)
    return float(re.search(r"mean_volume: (-?[\d.]+)", r.stderr).group(1))
before = level(0.5, 2.5, "lowpass=f=400")
during = level(4.0, 5.5, "lowpass=f=400")
print(f"music level before voice {before} dB, during voice {during} dB")
assert during < before - 6, "music did not duck"
print("duck test OK")
