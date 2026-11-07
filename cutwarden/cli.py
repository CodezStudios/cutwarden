"""Command line twin of the MCP tools: render a project file without an agent."""
import argparse
import sys

from . import render
from .project import Project


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cutwarden")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("frame", help="render one frame to PNG")
    f.add_argument("project"); f.add_argument("time", type=float); f.add_argument("out")
    f.add_argument("--scale", type=float, default=0.5)
    r = sub.add_parser("render", help="render the project to MP4")
    r.add_argument("project"); r.add_argument("out")
    r.add_argument("--scale", type=float, default=1.0); r.add_argument("--workers", type=int)
    i = sub.add_parser("import", help="import media (file or URL) into the asset library, or SRT/VTT captions as JSON words")
    i.add_argument("source"); i.add_argument("--captions", action="store_true", help="treat source as SRT/VTT and print words JSON")
    i.add_argument("--workdir", default=None)
    sub.add_parser("serve", help="start the MCP server (stdio; add --http for remote)")
    a, rest = ap.parse_known_args(argv)
    if a.cmd == "serve":
        from .server import main as serve
        sys.argv = ["cutwarden-mcp", *rest]
        return serve()
    if a.cmd == "import":
        import json, os
        from pathlib import Path
        from . import imports
        if a.captions:
            print(json.dumps(imports.read_captions(a.source)))
        else:
            wd = Path(a.workdir or os.environ.get("CUTWARDEN_WORKDIR", "~/cutwarden-projects")).expanduser()
            print(json.dumps(imports.import_media(wd, a.source), indent=2))
        return
    proj = Project.model_validate_json(open(a.project).read())
    if a.cmd == "frame":
        open(a.out, "wb").write(render.render_frame_png(proj, a.time, a.scale))
        print(f"wrote {a.out}")
    else:
        print(render.render_video(proj, a.out, a.scale, a.workers))


if __name__ == "__main__":
    main()
