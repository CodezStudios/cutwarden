"""Import tests: media (path, URL, base64), SSRF guard, captions, brand kit, project round-trip."""
import base64, http.server, os, subprocess, sys, tempfile, threading
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cutwarden import imports
from cutwarden.project import Editor, ProjectStore

w = Path(tempfile.mkdtemp(prefix="bc_imptest_"))
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=1", "-pix_fmt", "yuv420p", str(w / "a.mp4"))
(w / "notes.txt").write_text("not media")

# local path + dedupe
a = imports.import_media(w, str(w / "a.mp4")); b = imports.import_media(w, str(w / "a.mp4"))
assert a["path"] == b["path"] and a["width"] == 320, a
# non-media and wrong type rejected
for bad in (str(w / "notes.txt"),):
    try: imports.import_media(w, bad); raise SystemExit("accepted bad file")
    except imports.ImportError_ as e: print("ok rejected:", e)
# base64
r = imports.import_media_base64(w, "b64.mp4", base64.b64encode((w / "a.mp4").read_bytes()).decode()); assert r["duration"] > 0.9
# local disabled (remote mode)
os.environ["CUTWARDEN_ALLOW_LOCAL"] = "0"
try: imports.import_media(w, str(w / "a.mp4")); raise SystemExit("local allowed")
except imports.ImportError_ as e: print("ok local blocked:", str(e)[:60])
os.environ["CUTWARDEN_ALLOW_LOCAL"] = "1"
# SSRF guard blocks loopback; allowed with the override
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(w), **k))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/a.mp4"
try: imports.import_media(w, url); raise SystemExit("loopback allowed")
except imports.ImportError_ as e: print("ok ssrf blocked:", str(e)[:60])
os.environ["CUTWARDEN_ALLOW_PRIVATE_URLS"] = "1"
assert imports.import_media(w, url)["height"] == 568; print("ok url download")
os.environ.pop("CUTWARDEN_ALLOW_PRIVATE_URLS")
# captions
srt = "1\n00:00:00,000 --> 00:00:01,000\nHello brand\n\n2\n00:00:01,200 --> 00:00:02,000\n<i>safe</i> edits\n"
words = imports.parse_captions(srt, offset=5)
assert [x[0] for x in words] == ["Hello", "brand", "safe", "edits"] and words[0][1] == 5.0 and words[-1][2] == 7.0, words
vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\nhi there\n"
assert imports.parse_captions(vtt)[0][1] == 1.0; print("ok captions", words[:2])
# brand import
font = ROOT / "brands/example/fonts/Inter-Black.otf"
k = imports.import_brand(w, "acme", {"bg": "#000000", "fg": "#ffffff", "accent": "#ff3300"}, {"display": str(font)}, None)
print("ok brand", k)
try: imports.import_brand(w, "bad", {"bg": "#000"}, {"display": str(font)}); raise SystemExit("bad brand accepted")
except imports.ImportError_ as e: print("ok bad brand:", e); assert not (w / "brands/bad").exists()
# project round trip through the same validation add_layer uses
ed = Editor(ProjectStore(w / "p")); ed.create("x", k["brand_path"], platform="reels")
ed.add_layer("x", "footage", "clip", 0, 1, {"path": a["path"]}, None, None)
ed.add_layer("x", "text", "slam_in", 0.2, 0.8, {"text": "HI", "color": "accent"}, None, None)
summ = ed.store.load("x").summary()
print("ok project summary layers:", len(summ["layers"]))
print("ALL IMPORT TESTS OK")
