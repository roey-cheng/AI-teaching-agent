"""One offline parser per process; never import application configuration or credentials."""

import json
import os
import socket
import sys
from pathlib import Path

from .common import ImportProblem, Limits, atomic_json, digest_file


def disable_network():
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")

    def deny(*args, **kwargs):
        raise ImportProblem("network_disabled_models_must_be_cached")

    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny


def main():
    source, output, expected, limits_json = sys.argv[1:]
    out = Path(output)
    disable_network()
    try:
        from .inventory import preflight
        from .parsers import parse_document

        limits = Limits(**json.loads(limits_json))
        preflight(Path(source), limits)
        if digest_file(Path(source)) != expected:
            raise ImportProblem("source_changed_rescan_required")
        result = parse_document(Path(source), out, limits)
        if digest_file(Path(source)) != expected:
            raise ImportProblem("source_changed_during_parse")
        atomic_json(out / "result.json", result)
        atomic_json(out / "worker-status.json", {"ok": True})
    except Exception as exc:  # noqa: BLE001 - sanitize the subprocess boundary
        code = str(exc) if isinstance(exc, ImportProblem) else "parser_failed"
        if type(exc).__name__ in {"LocalEntryNotFoundError", "OfflineModeIsEnabled"}:
            code = "local_model_cache_missing"
        # Deliberately omit exception text, source paths and third-party logs.
        atomic_json(
            out / "worker-status.json",
            {"ok": False, "error": code, "error_type": type(exc).__name__},
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
