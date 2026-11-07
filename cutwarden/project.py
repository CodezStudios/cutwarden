"""Versioned JSON project: the single source of truth. Every mutation is validated and revision-checked."""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .brand import BrandKit
from .presets import PRESETS, PresetError, validate_params

KINDS = ("footage", "overlay", "text", "logo", "effect", "audio")


class ProjectError(ValueError):
    pass


class Layer(BaseModel):
    id: str
    kind: str
    preset: str
    start: float
    duration: float
    z: int = 0
    params: dict = Field(default_factory=dict)

    @property
    def end(self) -> float:
        return self.start + self.duration


class Project(BaseModel):
    name: str
    brand: str                      # path to brand.json
    width: int = 1080
    height: int = 1920
    fps: int = 30
    platform: str | None = None
    revision: int = 0
    layers: list[Layer] = Field(default_factory=list)

    def duration(self) -> float:
        return max((l.end for l in self.layers), default=0.0)

    def get(self, layer_id: str) -> Layer:
        for l in self.layers:
            if l.id == layer_id:
                return l
        raise ProjectError(f"no layer '{layer_id}'. existing: {[l.id for l in self.layers]}")

    def summary(self) -> dict:
        return {
            "name": self.name, "revision": self.revision, "canvas": f"{self.width}x{self.height}@{self.fps}",
            "duration": round(self.duration(), 3), "platform": self.platform,
            "layers": [
                {"id": l.id, "kind": l.kind, "preset": l.preset, "start": round(l.start, 3),
                 "end": round(l.end, 3), "z": l.z, "params": l.params}
                for l in sorted(self.layers, key=lambda l: (l.z, l.start))
            ],
        }


class ProjectStore:
    """Projects live as JSON files in a workdir, with a snapshot per revision for undo."""

    def __init__(self, workdir: str | Path):
        self.dir = Path(workdir).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / ".history").mkdir(exist_ok=True)

    @staticmethod
    def _safe(name: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", name):
            raise ProjectError("project name may only contain letters, digits, '_' and '-'")
        return name

    def path(self, name: str) -> Path:
        return self.dir / f"{self._safe(name)}.json"

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def load(self, name: str) -> Project:
        p = self.path(name)
        if not p.exists():
            raise ProjectError(f"no project '{name}'. existing: {self.list()}")
        return Project.model_validate_json(p.read_text())

    def save(self, proj: Project) -> None:
        self.path(proj.name).write_text(proj.model_dump_json(indent=2))
        (self.dir / ".history" / f"{proj.name}.r{proj.revision:04d}.json").write_text(proj.model_dump_json())

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def undo(self, name: str) -> Project:
        proj = self.load(name)
        if proj.revision <= 0:
            raise ProjectError("nothing to undo")
        prev = self.dir / ".history" / f"{name}.r{proj.revision - 1:04d}.json"
        if not prev.exists():
            raise ProjectError("history for the previous revision is missing")
        old = Project.model_validate_json(prev.read_text())
        old.revision = proj.revision + 1          # revisions only move forward
        self.save(old)
        return old


class Editor:
    """Validated mutations. Each call bumps the revision; pass expected_revision to guard against stale edits."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def create(self, name: str, brand_path: str, width=1080, height=1920, fps=30, platform=None) -> Project:
        if self.store.exists(name):
            raise ProjectError(f"project '{name}' already exists")
        BrandKit.load(brand_path)  # fail early on a bad brand kit
        if not (16 <= fps <= 60):
            raise ProjectError("fps must be between 16 and 60")
        if width % 2 or height % 2:
            raise ProjectError("width and height must be even (H.264 requirement)")
        proj = Project(name=name, brand=str(Path(brand_path).resolve()), width=width, height=height,
                       fps=fps, platform=platform)
        self.store.save(proj)
        return proj

    def _begin(self, name: str, expected_revision: int | None) -> Project:
        proj = self.store.load(name)
        if expected_revision is not None and expected_revision != proj.revision:
            raise ProjectError(
                f"stale edit: project is at revision {proj.revision}, you sent {expected_revision}. "
                "Call get_timeline to refresh, then retry.")
        return proj

    def _commit(self, proj: Project) -> Project:
        proj.revision += 1
        self.store.save(proj)
        return proj

    def add_layer(self, name: str, kind: str, preset: str, start: float, duration: float,
                  params: dict | None = None, z: int | None = None, expected_revision: int | None = None):
        proj = self._begin(name, expected_revision)
        if kind not in KINDS:
            raise ProjectError(f"kind must be one of {KINDS}")
        if preset not in PRESETS or PRESETS[preset].kind != kind:
            valid = sorted(p.name for p in PRESETS.values() if p.kind == kind)
            raise ProjectError(f"unknown {kind} preset '{preset}'. valid {kind} presets: {valid}")
        if start < 0 or duration <= 0:
            raise ProjectError("start must be >= 0 and duration > 0 (seconds)")
        try:
            full = validate_params(PRESETS[preset], params or {}, BrandKit.load(proj.brand))
        except PresetError as e:
            raise ProjectError(str(e)) from None
        n = 1 + sum(1 for l in proj.layers if l.kind == kind)
        ids = {l.id for l in proj.layers}
        while f"{kind}_{n}" in ids:
            n += 1
        default_z = {"footage": 0, "audio": 0, "overlay": 5, "text": 10, "logo": 20, "effect": 30}[kind]
        layer = Layer(id=f"{kind}_{n}", kind=kind, preset=preset, start=start, duration=duration,
                      z=default_z if z is None else z, params=full)
        proj.layers.append(layer)
        self._commit(proj)
        return proj, layer

    def update_layer(self, name: str, layer_id: str, start=None, duration=None, z=None,
                     params: dict | None = None, expected_revision: int | None = None):
        proj = self._begin(name, expected_revision)
        layer = proj.get(layer_id)
        if start is not None:
            layer.start = start
        if duration is not None:
            layer.duration = duration
        if z is not None:
            layer.z = z
        if layer.start < 0 or layer.duration <= 0:
            raise ProjectError("start must be >= 0 and duration > 0")
        if params:
            try:
                layer.params = validate_params(PRESETS[layer.preset], {**layer.params, **params}, BrandKit.load(proj.brand))
            except PresetError as e:
                raise ProjectError(str(e)) from None
        self._commit(proj)
        return proj, layer

    def remove_layer(self, name: str, layer_id: str, expected_revision: int | None = None):
        proj = self._begin(name, expected_revision)
        proj.layers.remove(proj.get(layer_id))
        return self._commit(proj)

    def set_meta(self, name: str, platform=None, fps=None, expected_revision: int | None = None):
        proj = self._begin(name, expected_revision)
        if platform is not None:
            proj.platform = platform
        if fps is not None:
            proj.fps = fps
        return self._commit(proj)
