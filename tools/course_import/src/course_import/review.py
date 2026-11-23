"""Local review workbench for parsed course materials.

The server binds to loopback only. Document text is always returned as JSON and
rendered with textContent by the browser; it is never interpreted as HTML.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import webbrowser
from collections import Counter
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .common import ImportProblem, atomic_json, read_json, safe_source
from .pipeline import valid_cache

ACTIONS = {"use", "exclude", "ocr", "vision", "full_page"}
ACTION_LABELS = {
    "use": "直接使用",
    "exclude": "排除",
    "ocr": "需要 OCR",
    "vision": "需要视觉理解",
    "full_page": "需要整页理解",
}

LOW_SIGNAL_REASONS = {
    "possible_page_number",
    "repeated_small_image_possible_decoration_not_discarded",
}


def _page_routing(page: dict) -> tuple[list[str], int, list[str]]:
    """Return explainable filters and priority; never marks content approved."""
    categories: set[str] = set()
    reasons: list[str] = list(page.get("warnings", []))
    routes = {suggestion["route"] for suggestion in page.get("suggestions", [])}
    reasons.extend(suggestion["reason"] for suggestion in page.get("suggestions", []))
    if "scan_review" in routes:
        categories.add("scan")
    if "needs_render_review" in routes:
        categories.add("layout")
    if "empty_page_review" in routes:
        categories.add("empty")
    if "vision_candidate" in routes:
        categories.add("vision")
    for element in page["elements"]:
        element_reasons = set(element.get("reasons", []))
        reasons.extend(element_reasons)
        if "empty_table_text_requires_review" in element_reasons:
            categories.add("empty")
        if element["route"] == "structure_review":
            categories.add("layout")
        if element["route"] == "image_review":
            categories.add("vision")
        significant_visibility = element_reasons - LOW_SIGNAL_REASONS
        if element["route"] == "visibility_review" and significant_visibility:
            if "native_image_supplement_not_retained_by_docling" in significant_visibility:
                categories.add("vision")
            else:
                categories.add("visibility")
        if element["route"] == "excluded_by_default":
            categories.add("excluded")
        if "repeated_small_image_possible_decoration_not_discarded" in element_reasons:
            categories.add("decoration")
    if not categories:
        categories.add("no_rule_flags")
    score = max(
        (
            {
                "scan": 100,
                "empty": 95,
                "visibility": 90,
                "layout": 80,
                "vision": 60,
                "excluded": 30,
                "decoration": 20,
                "no_rule_flags": 0,
            }[c]
            for c in categories
        ),
        default=0,
    )
    return sorted(categories), score, sorted(set(reasons))


class ReviewStore:
    def __init__(self, source: Path, output: Path):
        self.source = source
        self.output = output
        self.preview_root = output / "review-previews"
        self.preview_root.mkdir(exist_ok=True)
        self.decision_path = output / "review-decisions.json"
        self.lock = threading.Lock()
        manifest = read_json(output / "manifest.json")
        self.documents: dict[str, dict] = {}
        for index, row in enumerate(manifest["files"], 1):
            if not row.get("object"):
                continue
            folder = safe_source(output, row["object"])
            if not valid_cache(folder):
                continue
            document = read_json(folder / "result.json")
            doc_id = f"d{index:03}"
            self.documents[doc_id] = {"row": row, "folder": folder, "document": document}
        if self.decision_path.exists():
            decisions = read_json(self.decision_path)
            if decisions.get("schema_version") != 1:
                raise ImportProblem("unsupported_review_decisions_version")
            self.decisions = decisions
        else:
            self.decisions = {"schema_version": 1, "decisions": {}}

    @staticmethod
    def page_key(doc_id: str, page_number: int) -> str:
        return f"{doc_id}/p{page_number}/page"

    @staticmethod
    def element_key(doc_id: str, page_number: int, element_id: str) -> str:
        return f"{doc_id}/p{page_number}/element/{element_id}"

    @staticmethod
    def asset_key(sha256: str) -> str:
        return f"asset/{sha256}"

    def index(self) -> dict:
        documents = []
        totals = Counter()
        for doc_id, entry in self.documents.items():
            row, document = entry["row"], entry["document"]
            pages = []
            for page in document["pages"]:
                categories, score, reasons = _page_routing(page)
                decision = self.decisions["decisions"].get(self.page_key(doc_id, page["number"]))
                pages.append(
                    {
                        "number": page["number"],
                        "title": page.get("title", ""),
                        "categories": categories,
                        "priority": score,
                        "reasons": reasons,
                        "elements": len(page["elements"]),
                        "decision": decision,
                    }
                )
                totals.update(categories)
            documents.append(
                {
                    "id": doc_id,
                    "source": row["source"],
                    "format": row["format"],
                    "pages": pages,
                    "page_count": len(pages),
                    "page_decision_count": sum(bool(p["decision"]) for p in pages),
                }
            )
        return {
            "documents": documents,
            "totals": totals,
            "decision_count": len(self.decisions["decisions"]),
            "action_labels": ACTION_LABELS,
        }

    def page(self, doc_id: str, page_number: int) -> dict:
        entry = self.documents.get(doc_id)
        if not entry:
            raise ImportProblem("review_document_not_found")
        pages = entry["document"]["pages"]
        if not 1 <= page_number <= len(pages):
            raise ImportProblem("review_page_not_found")
        page = pages[page_number - 1]
        categories, score, reasons = _page_routing(page)
        result = dict(page)
        result.update(
            doc_id=doc_id,
            source=entry["row"]["source"],
            format=entry["row"]["format"],
            categories=categories,
            priority=score,
            routing_reasons=reasons,
            page_decision=self.decisions["decisions"].get(self.page_key(doc_id, page_number)),
        )
        for element in result["elements"]:
            element["decision"] = self.decisions["decisions"].get(
                self.element_key(doc_id, page_number, element["id"])
            )
            asset = element.get("asset")
            if asset:
                element["asset_decision"] = self.decisions["decisions"].get(
                    self.asset_key(asset["sha256"])
                )
                element["asset_url"] = f"/api/asset/{doc_id}/{asset['path']}"
        result["preview_url"] = (
            f"/api/preview/{doc_id}/{page_number}" if result["format"] == "pdf" else None
        )
        return result

    def _target_key(self, body: dict) -> str:
        doc_id = body.get("doc_id", "")
        page_number = body.get("page")
        target_type = body.get("target_type")
        if doc_id not in self.documents or not isinstance(page_number, int):
            raise ImportProblem("invalid_review_target")
        page = self.page(doc_id, page_number)
        if target_type == "page":
            return self.page_key(doc_id, page_number)
        target_id = body.get("target_id", "")
        if target_type == "element" and any(e["id"] == target_id for e in page["elements"]):
            return self.element_key(doc_id, page_number, target_id)
        if (
            target_type == "asset"
            and re.fullmatch(r"[a-f0-9]{64}", target_id)
            and any(e.get("asset", {}).get("sha256") == target_id for e in page["elements"])
        ):
            return self.asset_key(target_id)
        raise ImportProblem("invalid_review_target")

    def decide(self, body: dict) -> dict:
        key = self._target_key(body)
        action = body.get("action")
        note = body.get("note", "")
        if action not in ACTIONS | {"reset"} or not isinstance(note, str) or len(note) > 500:
            raise ImportProblem("invalid_review_decision")
        with self.lock:
            if action == "reset":
                self.decisions["decisions"].pop(key, None)
            else:
                self.decisions["decisions"][key] = {
                    "action": action,
                    "label": ACTION_LABELS[action],
                    "note": note,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            self.decisions["updated_at"] = datetime.now(UTC).isoformat()
            atomic_json(self.decision_path, self.decisions)
        return {"ok": True, "key": key, "decision": self.decisions["decisions"].get(key)}

    def asset(self, doc_id: str, relative: str) -> Path:
        entry = self.documents.get(doc_id)
        if not entry or not re.fullmatch(
            r"assets/[a-f0-9]{64}\.(png|jpg|jpeg|gif|tif|tiff|bmp|webp)", relative
        ):
            raise ImportProblem("asset_not_found")
        path = safe_source(entry["folder"], relative)
        if not path.is_file():
            raise ImportProblem("asset_not_found")
        return path

    def preview(self, doc_id: str, page_number: int) -> Path:
        entry = self.documents.get(doc_id)
        if not entry or entry["row"]["format"] != "pdf":
            raise ImportProblem("preview_not_available")
        if not 1 <= page_number <= entry["row"]["page_count"]:
            raise ImportProblem("review_page_not_found")
        folder = self.preview_root / entry["row"]["sha256"]
        folder.mkdir(exist_ok=True)
        path = folder / f"page-{page_number:04}.jpg"
        if path.exists():
            return path
        import pypdfium2 as pdfium

        source = safe_source(self.source, entry["row"]["source"])
        with pdfium.PdfDocument(source) as pdf:
            page = pdf[page_number - 1]
            image = page.render(scale=1.6).to_pil().convert("RGB")
            image.thumbnail((1400, 1800))
            fd, temporary = tempfile.mkstemp(prefix=".preview-", suffix=".jpg", dir=folder)
            os.close(fd)
            try:
                image.save(temporary, "JPEG", quality=82, optimize=True)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            page.close()
        return path


def _content_type(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "application/octet-stream")


def handler_for(store: ReviewStore, token: str):
    static_root = files("course_import").joinpath("review_static")

    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "CourseReview/1"

        def log_message(self, format, *args):
            # Avoid logging user material names, notes or query strings.
            return

        def _headers(self, status=HTTPStatus.OK, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )

        def _json(self, value, status=HTTPStatus.OK):
            data = json.dumps(value, ensure_ascii=False).encode()
            self._headers(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _file(self, path: Path, content_type=None):
            data = path.read_bytes()
            self._headers(content_type=content_type or _content_type(path))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            supplied = self.headers.get("X-Review-Token", "") or query_token
            return secrets.compare_digest(supplied, token)

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._file(
                        Path(str(static_root.joinpath("index.html"))), "text/html; charset=utf-8"
                    )
                elif parsed.path in {"/app.js", "/styles.css"}:
                    self._file(Path(str(static_root.joinpath(parsed.path[1:]))))
                elif parsed.path == "/api/index":
                    if not self._authorized():
                        self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                    else:
                        self._json(store.index())
                elif parsed.path.startswith("/api/document/"):
                    if not self._authorized():
                        self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                    else:
                        doc_id = parsed.path.rsplit("/", 1)[-1]
                        page = int(parse_qs(parsed.query).get("page", ["0"])[0])
                        self._json(store.page(doc_id, page))
                elif parsed.path.startswith("/api/preview/"):
                    if not self._authorized():
                        self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                    else:
                        _, _, _, doc_id, page = parsed.path.split("/")
                        self._file(store.preview(doc_id, int(page)), "image/jpeg")
                elif parsed.path.startswith("/api/asset/"):
                    if not self._authorized():
                        self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                    else:
                        parts = parsed.path.split("/", 4)
                        self._file(store.asset(parts[3], unquote(parts[4])))
                elif parsed.path == "/api/decisions":
                    if not self._authorized():
                        self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                    else:
                        self._json(store.decisions)
                else:
                    self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            except (ImportProblem, OSError, ValueError, IndexError):
                self._json({"error": "invalid_or_missing_resource"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if self.path != "/api/decision":
                self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            if not self._authorized():
                self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_384:
                    raise ImportProblem("invalid_request_size")
                body = json.loads(self.rfile.read(length))
                self._json(store.decide(body))
            except (ImportProblem, ValueError, TypeError, KeyError):
                self._json({"error": "invalid_review_decision"}, HTTPStatus.BAD_REQUEST)

    return ReviewHandler


def serve_review(source: Path, output: Path, host="127.0.0.1", port=8765, open_browser=True):
    if host not in {"127.0.0.1", "localhost"}:
        raise ImportProblem("review_server_loopback_only")
    store = ReviewStore(source, output)
    if not store.documents:
        raise ImportProblem("no_parsed_documents_for_review")
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), handler_for(store, token))
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/#token={token}"
    print(f"Review workbench: {url}")
    print("Press Ctrl+C to stop. Decisions are saved to review-decisions.json.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
