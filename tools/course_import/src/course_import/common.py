from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


class ImportProblem(Exception):
    """Only a stable code, never third-party exceptions with private paths/content."""


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 100 * 1024 * 1024
    max_pages: int = 1000
    max_zip_bytes: int = 256 * 1024 * 1024
    max_zip_entries: int = 10000
    timeout_seconds: int = 300

    def to_dict(self):
        return asdict(self)


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_text(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def safe_source(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(x in {"..", "."} for x in rel.parts):
        raise ImportProblem("unsafe_relative_path")
    target = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ImportProblem("symlink_not_allowed")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ImportProblem("outside_source_root")
    return target


def save_asset(out: Path, data: bytes, extension: str) -> dict:
    # Never use filenames or MIME strings supplied by a document as an output path.
    ext = extension.lower().lstrip(".")
    if ext not in {"png", "jpg", "jpeg", "gif", "tif", "tiff", "bmp", "webp"}:
        ext = "bin"
    sha = hashlib.sha256(data).hexdigest()
    relative = f"assets/{sha}.{ext}"
    path = out / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return {"path": relative, "sha256": sha, "bytes": len(data), "previewable": ext != "bin"}


def bbox(x, y, width, height):
    return [round(float(v), 5) for v in (x, y, x + width, y + height)]
