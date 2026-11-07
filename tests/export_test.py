"""Export tests: OTIO (validated with the real opentimelineio library if installed), EDL, SRT/VTT, bundle round-trip,
video formats, and chunked download integrity."""
import base64, hashlib, json, subprocess, sys, tempfile, zipfile
from pathlib import Path
from PIL import Image
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cutwarden import export, imports
from cutwarden.project import Editor, ProjectStore

w = Path(tempfile.mkdtemp(prefix="bc_exp_test_"))
sh = lambda *a: subprocess.run(a, check=True, capture_output=True)
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=3", "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(w / "a.mp4"))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=6", str(w / "m.wav"))
Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(w / "s.png")

ed = Editor(ProjectStore(w / "p")); brand = ROOT / "brands/example/brand.json"
ed.create("x", str(brand), 540, 960, 30, "reels")
A = lambda *a, **k: ed.add_layer("x", *a, **k)
A("footage", "clip", 0, 2, {"path": str(w / "a.mp4"), "source_in": 0.5}, None, None)
A("footage", "clip", 2, 2, {"path": str(w / "a.mp4")}, None, None)
A("overlay", "overlay", 0.5, 2, {"path": str(w / "s.png"), "width": 0.2}, None, None)
A("text", "slam_in", 0.2, 1.5, {"text": "HELLO\nWORLD", "size": 0.1}, None, None)
A("text", "word_captions", 2.0, 2.0, {"words": [["Brand", 0, 0.4], ["safe", 0.4, 0.8], ["edits.", 0.8, 1.2], ["Really", 1.3, 1.6], ["good", 1.6, 2.0]], "y": 0.7}, None, None)
A("effect", "flash", 2.0, 0.3, {}, None, None)
A("audio", "music", 0, 4, {"path": str(w / "m.wav")}, None, None)
proj, _ = A("effect", "vignette", 0, 4, {}, None, None)

# --- OTIO: structural + real library round-trip
r = export.export_otio(w, proj); print("otio", r["tracks"], r["items"])
try:
    import opentimelineio as otio
    tl = otio.adapters.read_from_file(r["path"])
    vids = tl.video_tracks(); auds = tl.audio_tracks()
    assert len(auds) >= 2, f"expected music + clip audio tracks, got {len(auds)}"
    v1 = vids[0]; clips = [c for c in v1 if isinstance(c, otio.schema.Clip)]
    assert len(clips) == 2 and clips[0].source_range.start_time.value == 15 and clips[0].source_range.duration.value == 60, clips[0].source_range
    assert abs(tl.duration().to_seconds() - 4.0) < 0.05, tl.duration()
    assert clips[0].metadata["cutwarden"]["preset"] == "clip"
    print("ok opentimelineio read it:", len(vids), "video tracks,", len(auds), "audio tracks, duration", tl.duration().to_seconds())
except ImportError:
    print("opentimelineio not installed: structural check only")
    d = json.load(open(r["path"])); assert d["OTIO_SCHEMA"] == "Timeline.1"

# --- EDL
e = export.export_edl(w, proj); txt = Path(e["path"]).read_text()
assert "FCM: NON-DROP FRAME" in txt and "00:00:00:15 00:00:02:15 00:00:00:00 00:00:02:00" in txt, txt
print("ok edl", e["events"], "events")

# --- captions
c = export.export_captions(w, proj, "srt"); s = Path(c["path"]).read_text()
assert "00:00:02,000 --> 00:00:03,200" in s and "Brand safe edits." in s, s
assert "WEBVTT" in Path(export.export_captions(w, proj, "vtt", True)["path"]).read_text()
# captions round trip through the importer
back = imports.parse_captions(s); assert back[0][0] == "Brand" and abs(back[0][1] - 2.0) < 0.01; print("ok captions", c["cues"], "cues, re-importable")

# --- bundle round trip
b = export.export_bundle(w, proj)
with zipfile.ZipFile(b["path"]) as z:
    names = z.namelist(); assert "project.json" in names and "brand/brand.json" in names and any(n.startswith("media/") for n in names)
    assert str(w) not in z.read("project.json").decode(), "absolute paths leaked into bundle"
w2 = Path(tempfile.mkdtemp(prefix="bc_exp_test2_")); ed2 = Editor(ProjectStore(w2 / "p"))
summ = export.import_bundle(w2, ed2, b["path"], "copy")
assert len(summ["layers"]) == len(proj.layers) and summ["duration"] == round(proj.duration(), 3), summ
assert all(str(w2) in l["params"]["path"] for l in summ["layers"] if "path" in l["params"])
print("ok bundle round trip:", len(summ["layers"]), "layers, media restored under", w2.name)
# zip-slip rejected
evil = w / "evil.zip"
with zipfile.ZipFile(evil, "w") as z: z.writestr("../evil.txt", "x")
try: export.import_bundle(w2, ed2, str(evil), "evil"); raise SystemExit("zip-slip accepted")
except export.ExportError as ex: print("ok zip-slip blocked")

# --- video formats (small scale for speed)
for fmt in ("mp4", "webm", "mov_prores", "gif", "mp3"):
    v = export.export_video(w, proj, fmt, scale=0.25)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", v["path"]], capture_output=True, text=True).stdout.strip()
    assert v["bytes"] > 1000 and abs(float(info) - 4.0) < 0.3, (fmt, v, info)
    print(f"ok {fmt:10s} {v['bytes']:>8} bytes {float(info):.2f}s")
export.export_poster(w, proj, 1.0, 0.5)

# --- chunked download integrity (+ path restriction)
f = Path(export.export_video(w, proj, "mp4", scale=0.25)["path"])
got = b""; i = 0
while True:
    ch = export.download_chunk(w, str(f), i); got += base64.b64decode(ch["data_base64"])
    if i == ch["total_chunks"] - 1: sha = ch["sha256"]; break
    i += 1
assert hashlib.sha256(got).hexdigest() == sha == hashlib.sha256(f.read_bytes()).hexdigest() and len(got) == f.stat().st_size
print("ok download", i + 1, "chunk(s), sha256 matches")
for bad in (str(w / "a.mp4"), "/etc/passwd", str(w / "exports" / ".." / "a.mp4")):
    try: export.download_chunk(w, bad, 0); raise SystemExit(f"downloaded {bad}")
    except export.ExportError: pass
print("ok download restricted to exports/renders")
print("ALL EXPORT TESTS OK")
