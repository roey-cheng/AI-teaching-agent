from __future__ import annotations

import os
import warnings
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET
from pypdf import PdfReader

from .common import ImportProblem, Limits, digest_file, safe_source

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def pptx_metadata(path: Path, limits: Limits):
    try:
        with ZipFile(path) as z:
            entries = z.infolist()
            if len(entries) > limits.max_zip_entries:
                raise ImportProblem("zip_entry_limit")
            if sum(i.file_size for i in entries) > limits.max_zip_bytes:
                raise ImportProblem("zip_expanded_size_limit")
            for i in entries:
                name = PurePosixPath(i.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in i.filename:
                    raise ImportProblem("unsafe_zip_entry")
                if i.flag_bits & 1:
                    raise ImportProblem("encrypted_archive")
                if i.filename.lower().endswith("vbaproject.bin"):
                    raise ImportProblem("macro_content_not_allowed")
            root = ET.fromstring(z.read("ppt/presentation.xml"))
            slides = root.find(P + "sldIdLst")
            count = len(slides) if slides is not None else 0
            external = 0
            for i in entries:
                if i.filename.endswith(".rels"):
                    rels = ET.fromstring(z.read(i.filename))
                    external += sum(r.get("TargetMode") == "External" for r in rels)
            return count, {"external_relationships": external}
    except ImportProblem:
        raise
    except (BadZipFile, KeyError, ValueError):
        raise ImportProblem("invalid_pptx") from None


def preflight(path: Path, limits: Limits) -> dict:
    if path.stat().st_size > limits.max_bytes:
        raise ImportProblem("file_size_limit")
    extension = path.suffix.lower()
    if extension == ".pdf":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise ImportProblem("encrypted_pdf")
            pages = len(reader.pages)
        metadata = {}
    elif extension == ".pptx":
        pages, metadata = pptx_metadata(path, limits)
    else:
        raise ImportProblem("unsupported_format")
    if not 0 < pages <= limits.max_pages:
        raise ImportProblem("page_count_limit")
    return {"page_count": pages, **metadata}


def inventory(source: Path, library: str, limits: Limits) -> dict:
    rows = []
    # Never follow links, including links to directories. Include rejections in the report.
    for base, directories, filenames in os.walk(source, followlinks=False):
        directories.sort()
        filenames.sort()
        links = [d for d in directories if (Path(base) / d).is_symlink()]
        directories[:] = [d for d in directories if d not in links and not d.startswith(".")]
        for name in sorted(filenames + links):
            if name.startswith("."):
                continue
            path = Path(base) / name
            relative = path.relative_to(source).as_posix()
            row = {"source": relative, "format": path.suffix.lower().lstrip(".")}
            try:
                path = safe_source(source, relative)
                row["bytes"] = path.stat().st_size
                row.update(preflight(path, limits))
                row["sha256"] = digest_file(path)
                row["status"] = "ready"
            except ImportProblem as e:
                row.update(status="rejected", error=str(e))
            except Exception:  # noqa: BLE001 - isolate malformed documents; never expose raw errors
                row.update(status="rejected", error="unreadable_or_invalid_file")
            rows.append(row)
    seen = {}
    for row in rows:
        sha = row.get("sha256")
        if sha:
            if sha in seen:
                row["duplicate_of"] = seen[sha]
            else:
                seen[sha] = row["source"]
    return {"schema_version": 1, "library": library, "limits": limits.to_dict(), "files": rows}
