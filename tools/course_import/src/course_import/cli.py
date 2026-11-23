import argparse
import fcntl
import re
from pathlib import Path

from .common import ImportProblem, Limits, atomic_json, read_json
from .inventory import inventory
from .pipeline import parse_manifest
from .report import report
from .review import serve_review


def directories(source, output):
    for path in (source, output):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ImportProblem("symlink_root_not_allowed")
    source, output = source.resolve(), output.resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ImportProblem("source_output_must_be_separate")
    if not source.is_dir():
        raise ImportProblem("source_directory_missing")
    output.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in output.rglob("*")):
        raise ImportProblem("symlink_output_not_allowed")
    return source, output


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline, review-first PDF/PPTX importer")
    parser.add_argument("command", choices=["inventory", "parse", "report", "run", "review"])
    parser.add_argument("--source", type=Path, default=Path("data/course_materials"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library", default="course-materials")
    parser.add_argument("--include", action="append", default=[], help="Exact relative source path")
    parser.add_argument(
        "--timeout", type=int, default=300, help="Per-document seconds (scan excluded)"
    )
    parser.add_argument("--port", type=int, default=8765, help="Local review server port")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.library) or args.timeout <= 0:
            raise ImportProblem("invalid_library_or_timeout")
        source, out = directories(args.source.absolute(), args.output.absolute())
        with (out / ".import.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ImportProblem("output_in_use") from None
            if args.command in {"inventory", "run"}:
                manifest = inventory(source, args.library, Limits(timeout_seconds=args.timeout))
                if args.include:
                    names = {r["source"] for r in manifest["files"]}
                    if not set(args.include).issubset(names):
                        raise ImportProblem("include_source_not_found")
                    manifest["files"] = [
                        r for r in manifest["files"] if r["source"] in args.include
                    ]
                atomic_json(out / "manifest.json", manifest)
            else:
                manifest = read_json(out / "manifest.json")
                if manifest.get("schema_version") != 1:
                    raise ImportProblem("unsupported_manifest_version")
            if args.command == "review":
                if not 0 <= args.port <= 65535:
                    raise ImportProblem("invalid_review_port")
                serve_review(source, out, port=args.port, open_browser=not args.no_open)
                return 0
            if args.command in {"parse", "run"}:
                manifest = parse_manifest(source, out, manifest)
            totals = report(out, manifest)
            print(f"Files: {len(manifest['files'])}; extracted pages: {totals.get('pages', 0)}")
            print("Reports: report.html and report.md in the chosen output directory")
            return int(any(r["status"] in {"failed", "rejected"} for r in manifest["files"]))
    except ImportProblem as exc:
        print(f"Import error: {exc}")
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("Import error: unreadable_or_invalid_manifest_or_output")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
