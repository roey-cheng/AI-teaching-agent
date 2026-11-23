from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from . import VERSION
from .common import ImportProblem, Limits, atomic_json, digest_file, read_json, safe_source


def fingerprint() -> dict:
    packages = (
        "docling",
        "docling-core",
        "docling-ibm-models",
        "python-pptx",
        "pypdf",
        "pypdfium2",
        "pillow",
        "torch",
        "transformers",
    )
    return {
        "importer": VERSION,
        "packages": {p: importlib.metadata.version(p) for p in packages},
        "code_sha256": hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest(),
    }


def cache_key(row, engine, limits):
    material = {
        "sha256": row["sha256"],
        "format": row["format"],
        "engine": engine,
        "limits": limits.to_dict(),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def valid_cache(folder: Path) -> bool:
    try:
        marker = read_json(safe_source(folder, "complete.json"))
        files = marker["files"]
        if not {"result.json", "docling.json"}.issubset(files):
            return False
        for relative, sha in files.items():
            if digest_file(safe_source(folder, relative)) != sha:
                return False
        result = read_json(folder / "result.json")
        for page in result["pages"]:
            for element in page["elements"]:
                asset = element.get("asset")
                if asset and files.get(asset["path"]) != asset["sha256"]:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError, ImportProblem):
        return False


def run_worker(source: Path, output: Path, expected: str, limits: Limits) -> dict:
    # Environment whitelist: model/cache paths are needed, API credentials are not.
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "HF_HOME",
        "HF_HUB_CACHE",
        "XDG_CACHE_HOME",
    }
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(
        HF_HUB_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        DO_NOT_TRACK="1",
        TOKENIZERS_PARALLELISM="false",
        OMP_NUM_THREADS="2",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "course_import.worker",
            str(source),
            str(output),
            expected,
            json.dumps(limits.to_dict()),
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return_code = proc.wait(timeout=limits.timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return {"ok": False, "error": "parse_timeout"}
    try:
        status = read_json(output / "worker-status.json")
        if return_code and status.get("ok"):
            return {"ok": False, "error": "worker_exit_failed"}
        return status
    except (OSError, ValueError):
        return {"ok": False, "error": "worker_exit_without_result"}


def parse_manifest(source: Path, out: Path, manifest: dict, worker=run_worker) -> dict:
    limits = Limits(**manifest["limits"])
    engine = fingerprint()
    manifest["engine"] = engine
    objects = out / "objects"
    objects.mkdir(exist_ok=True)
    if objects.is_symlink():
        raise ImportProblem("symlink_output_not_allowed")
    for row in manifest["files"]:
        if row["status"] == "rejected":
            continue
        row.pop("error", None)
        row.pop("error_type", None)
        row.pop("object", None)
        try:
            path = safe_source(source, row["source"])
            if digest_file(path) != row["sha256"]:
                raise ImportProblem("source_changed_rescan_required")
            key = cache_key(row, engine, limits)
            destination = objects / key
            if destination.is_symlink():
                raise ImportProblem("symlink_output_not_allowed")
            if valid_cache(destination):
                row.update(status="cached", object=f"objects/{key}")
            else:
                temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=objects))
                status = worker(path, temporary, row["sha256"], limits)
                if not status.get("ok"):
                    row.update(status="failed", error=status.get("error", "parser_failed"))
                    if status.get("error_type"):
                        row["error_type"] = status["error_type"]
                else:
                    if digest_file(path) != row["sha256"]:
                        raise ImportProblem("source_changed_during_parse")
                    result = read_json(temporary / "result.json")
                    if len(result["pages"]) != row["page_count"]:
                        raise ImportProblem("page_count_mismatch")
                    files = {
                        p.relative_to(temporary).as_posix(): digest_file(p)
                        for p in temporary.rglob("*")
                        if p.is_file()
                    }
                    atomic_json(temporary / "complete.json", {"files": files})
                    if not valid_cache(temporary):
                        raise ImportProblem("incomplete_worker_result")
                    if destination.exists():
                        # Keep damaged/old output recoverable instead of deleting it.
                        quarantine = Path(tempfile.mkdtemp(prefix=".invalid-", dir=objects))
                        destination.rename(quarantine / "previous")
                    temporary.rename(destination)
                    row.update(status="parsed", object=f"objects/{key}")
        except ImportProblem as exc:
            row.update(status="failed", error=str(exc))
        except Exception:  # noqa: BLE001 - per-file failure isolation
            row.update(status="failed", error="unreadable_source_or_output")
        atomic_json(out / "manifest.json", manifest)
        print(
            f"{row['status']}: {row['source']}"
            + (f" ({row['error']})" if row.get("error") else ""),
            flush=True,
        )
    return manifest
