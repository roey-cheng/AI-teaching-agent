import http.client
import json
import socket
import subprocess
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from pypdf import PdfWriter

from course_import.classify import classify, looks_like_code
from course_import.cli import directories, main
from course_import.common import (
    ImportProblem,
    Limits,
    atomic_json,
    digest_file,
    read_json,
    safe_source,
)
from course_import.inventory import inventory, preflight
from course_import.parsers import element, pdf_native, pptx_native
from course_import.pipeline import parse_manifest, run_worker, valid_cache
from course_import.report import fenced, report
from course_import.review import ReviewStore, _page_routing, handler_for
from course_import.worker import disable_network


def pdf(path, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if encrypted:
        writer.encrypt("test")
    writer.write(path)


@pytest.fixture
def roots(tmp_path):
    source, out = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    out.mkdir()
    return source.resolve(), out.resolve()


def document(elements=None, **page_fields):
    doc = {"pages": [{"number": 1, "elements": elements or [], **page_fields}]}
    classify(doc)
    return doc


def successful_worker(source, out, expected, limits):
    atomic_json(out / "docling.json", {})
    atomic_json(
        out / "result.json",
        document([element("p1-e1", "text", [0, 0, 1, 1], '<script>alert("x")</script>\n```')]),
    )
    return {"ok": True}


def test_inventory_duplicates_rejected_and_privacy(roots):
    source, _ = roots
    pdf(source / "one.pdf")
    (source / "copy.pdf").write_bytes((source / "one.pdf").read_bytes())
    pdf(source / "encrypted.pdf", encrypted=True)
    (source / "invalid.pdf").write_bytes(b"broken")
    (source / "a.txt").write_text("unsupported")
    (source / "link.pdf").symlink_to(source / "one.pdf")
    result = inventory(source, "test", Limits())
    rows = {r["source"]: r for r in result["files"]}
    assert rows["one.pdf"]["duplicate_of"] == "copy.pdf"
    assert rows["encrypted.pdf"]["error"] == "encrypted_pdf"
    assert rows["link.pdf"]["error"] == "symlink_not_allowed"
    assert rows["a.txt"]["error"] == "unsupported_format"
    assert rows["invalid.pdf"]["status"] == "rejected"
    assert rows["copy.pdf"]["status"] == "ready"
    assert str(source) not in json.dumps(result)


@pytest.mark.parametrize("relative", ["../secret", "/etc/passwd", "a/../../b"])
def test_path_escape_rejected(roots, relative):
    with pytest.raises(ImportProblem):
        safe_source(roots[0], relative)


def test_output_overlap_and_links(roots):
    source, out = roots
    with pytest.raises(ImportProblem, match="source_output"):
        directories(source, source / "derived")
    (out / "unsafe").symlink_to(source)
    with pytest.raises(ImportProblem, match="symlink"):
        directories(source, out)


@pytest.mark.parametrize(
    "entry,code",
    [("../escape.xml", "unsafe_zip_entry"), ("ppt/vbaProject.bin", "macro_content_not_allowed")],
)
def test_pptx_archive_safety(roots, entry, code):
    path = roots[0] / "bad.pptx"
    with ZipFile(path, "w") as archive:
        archive.writestr(entry, "x")
    with pytest.raises(ImportProblem, match=code):
        preflight(path, Limits())


def test_limits(roots):
    path = roots[0] / "one.pdf"
    pdf(path)
    with pytest.raises(ImportProblem, match="file_size"):
        preflight(path, Limits(max_bytes=1))
    with pytest.raises(ImportProblem, match="page_count"):
        preflight(path, Limits(max_pages=0))
    path = roots[0] / "large.pptx"
    with ZipFile(path, "w") as archive:
        archive.writestr("file", "a" * 100)
    with pytest.raises(ImportProblem, match="zip_expanded"):
        preflight(path, Limits(max_zip_bytes=50))


def test_routing_is_conservative_and_preserves_text():
    code = "class Example {\n    int value = 1;\n}"
    assert looks_like_code(code)
    assert not looks_like_code("A class is a template for objects.")
    elements = [
        element("code", "code", [0, 0, 0.4, 0.4], code),
        element("picture", "image", [0.5, 0.5, 1, 1]),
        element(
            "white",
            "text",
            [0.1, 0.1, 0.2, 0.2],
            "hidden?",
            warnings=["white_text_visibility_uncertain"],
        ),
        element("note", "speaker_notes", [0, 0, 0, 0], "notes"),
    ]
    doc = document(elements, native_stats={"text_chars": 20, "largest_image_coverage": 0.9})
    assert elements[0]["text"] == code
    assert elements[2]["route"] == "visibility_review"
    assert elements[3]["route"] == "excluded_by_default"
    assert {s["route"] for s in doc["pages"][0]["suggestions"]} == {
        "vision_candidate",
        "scan_review",
    }
    assert doc["publishable"] is False
    assert all(e["review_status"] == "unreviewed" for e in elements)


def test_composite_hidden_empty_and_decorations():
    empty_table = document([element("table", "table", [0, 0, 1, 1])])
    table = empty_table["pages"][0]["elements"][0]
    assert table["route"] == "visibility_review"
    assert "empty_table_text_requires_review" in table["reasons"]
    group = [
        element("image", "image", [0, 0, 1, 1], group="g"),
        element("text", "text", [0, 0, 0.1, 0.1], "label", group="g"),
    ]
    doc = document(group)
    assert doc["pages"][0]["suggestions"][0]["route"] == "needs_render_review"
    doc = document(group, hidden=True)
    assert all(e["route"] == "excluded_by_default" for e in group)
    assert not doc["pages"][0]["suggestions"]
    assert document()["pages"][0]["suggestions"][0]["route"] == "empty_page_review"
    repeated = {
        "pages": [
            {
                "number": i,
                "elements": [element("logo", "image", [0, 0, 0.1, 0.1], asset={"sha256": "same"})],
            }
            for i in range(3)
        ]
    }
    classify(repeated)
    assert all(
        "repeated_small_image_possible_decoration_not_discarded" in p["elements"][0]["reasons"]
        for p in repeated["pages"]
    )


def test_cache_restart_invalidation_and_source_change(roots):
    source, out = roots
    pdf(source / "one.pdf")
    manifest = inventory(source, "test", Limits())
    calls = []

    def counted(*args):
        calls.append(True)
        return successful_worker(*args)

    parse_manifest(source, out, manifest, worker=counted)
    row = manifest["files"][0]
    folder = out / row["object"]
    assert row["status"] == "parsed" and valid_cache(folder)
    parse_manifest(source, out, manifest, worker=counted)
    assert row["status"] == "cached" and len(calls) == 1
    (folder / "result.json").write_text("broken")
    parse_manifest(source, out, manifest, worker=counted)
    assert row["status"] == "parsed" and len(calls) == 2
    assert list((out / "objects").glob(".invalid-*"))
    (source / "one.pdf").write_bytes(b"changed")
    parse_manifest(source, out, manifest, worker=counted)
    assert row["error"] == "source_changed_rescan_required"
    assert "object" not in row


def test_failure_isolation_and_retry(roots):
    source, out = roots
    pdf(source / "a.pdf")
    pdf(source / "b.pdf")
    manifest = inventory(source, "test", Limits())

    def worker(path, *args):
        return (
            {"ok": False, "error": "test_failure"}
            if path.name == "a.pdf"
            else successful_worker(path, *args)
        )

    parse_manifest(source, out, manifest, worker=worker)
    assert [r["status"] for r in manifest["files"]] == ["failed", "parsed"]
    parse_manifest(source, out, manifest, worker=successful_worker)
    assert all(r["status"] == "cached" for r in manifest["files"])


def test_missing_assets_break_cache(roots):
    source, out = roots
    pdf(source / "one.pdf")
    manifest = inventory(source, "test", Limits())

    def worker(path, target, expected, limits):
        from course_import.common import save_asset

        asset = save_asset(target, b"synthetic", "png")
        atomic_json(
            target / "result.json", document([element("img", "image", [0, 0, 1, 1], asset=asset)])
        )
        atomic_json(target / "docling.json", {})
        return {"ok": True}

    parse_manifest(source, out, manifest, worker=worker)
    folder = out / manifest["files"][0]["object"]
    assert valid_cache(folder)
    next((folder / "assets").iterdir()).unlink()
    assert not valid_cache(folder)


def test_reports_escape_and_no_private_paths(roots):
    source, out = roots
    pdf(source / "one.pdf")
    manifest = inventory(source, "test", Limits())
    parse_manifest(source, out, manifest, worker=successful_worker)
    totals = report(out, manifest)
    content = (out / "document-001.html").read_text()
    assert "<script>" not in content and "&lt;script&gt;" in content
    assert "default-src 'none'" in content
    assert str(source) not in content
    assert totals["pages"] == 1
    assert fenced("```").startswith("````text")


def test_review_routing_decisions_and_pdf_preview(roots):
    source, out = roots
    pdf(source / "one.pdf")
    manifest = inventory(source, "test", Limits())
    parse_manifest(source, out, manifest, worker=successful_worker)
    store = ReviewStore(source, out)
    index = store.index()
    assert len(index["documents"]) == 1
    assert index["documents"][0]["pages"][0]["categories"] == ["no_rule_flags"]
    assert index["documents"][0]["page_decision_count"] == 0
    decision = store.decide(
        {
            "doc_id": "d001",
            "page": 1,
            "target_type": "page",
            "target_id": "",
            "action": "use",
            "note": "checked",
        }
    )
    assert decision["decision"]["label"] == "直接使用"
    assert (
        read_json(out / "review-decisions.json")["decisions"]["d001/p1/page"]["note"] == "checked"
    )
    assert store.index()["documents"][0]["page_decision_count"] == 1
    preview = store.preview("d001", 1)
    assert preview.is_file() and preview.read_bytes().startswith(b"\xff\xd8")
    store.decide(
        {
            "doc_id": "d001",
            "page": 1,
            "target_type": "page",
            "target_id": "",
            "action": "reset",
            "note": "",
        }
    )
    assert not store.page("d001", 1)["page_decision"]
    assert store.index()["documents"][0]["page_decision_count"] == 0


def test_review_priority_categories():
    page = document(
        [
            element("table", "table", [0, 0, 1, 1]),
            element(
                "white", "text", [0, 0, 0.1, 0.1], "x", warnings=["white_text_visibility_uncertain"]
            ),
        ],
        native_stats={"text_chars": 1, "largest_image_coverage": 0.8},
    )["pages"][0]
    categories, priority, reasons = _page_routing(page)
    assert {"scan", "empty", "visibility"}.issubset(categories)
    assert priority == 100
    assert "empty_table_text_requires_review" in reasons


def test_review_page_decision_count_is_not_an_approval_count(roots):
    source, out = roots
    pdf(source / "one.pdf")
    manifest = inventory(source, "test", Limits())
    parse_manifest(source, out, manifest, worker=successful_worker)
    store = ReviewStore(source, out)
    store.decide(
        {
            "doc_id": "d001",
            "page": 1,
            "target_type": "element",
            "target_id": "p1-e1",
            "action": "use",
        }
    )
    index = store.index()
    assert index["decision_count"] == 1
    assert index["documents"][0]["page_decision_count"] == 0
    assert index["documents"][0]["pages"][0]["categories"] == ["no_rule_flags"]
    assert index["documents"][0]["pages"][0]["decision"] is None
    store.decide({"doc_id": "d001", "page": 1, "target_type": "page", "action": "ocr"})
    persisted = read_json(out / "review-decisions.json")
    assert persisted["schema_version"] == 1
    assert persisted["decisions"]["d001/p1/page"]["action"] == "ocr"
    reopened = ReviewStore(source, out).index()
    assert reopened["decision_count"] == 2
    assert reopened["documents"][0]["page_decision_count"] == 1
    assert reopened["documents"][0]["pages"][0]["decision"]["action"] == "ocr"
    assert "reviewed_pages" not in reopened["documents"][0]


def test_review_http_requires_token_and_never_executes_document_html(roots):
    source, out = roots
    pdf(source / "one.pdf")
    manifest = inventory(source, "test", Limits())
    parse_manifest(source, out, manifest, worker=successful_worker)
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(ReviewStore(source, out), "secret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        connection.request("GET", "/api/index")
        assert connection.getresponse().status == 403
        connection.request("GET", "/api/index", headers={"X-Review-Token": "secret"})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["documents"]
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode()
        assert response.status == 200 and "课件审核工作台" in html
        assert 'value="no_rule_flags"' in html and "未触发检查规则" in html
        assert 'id="without-page-decision"' in html and "只看未作整页决定" in html
        connection.request("GET", "/app.js")
        script = connection.getresponse().read().decode()
        assert "innerHTML" not in script and "textContent" in script
        assert "page_decision_count" in script and "已作整页决定" in script
        assert "reviewed_pages" not in script and "无异常" not in script
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_worker_timeout_kills_process_group(roots, monkeypatch):
    class Process:
        pid = 100
        calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("worker", 1)
            return -9

    kills = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr("course_import.pipeline.os.killpg", lambda *args: kills.append(args))
    assert run_worker(roots[0], roots[1], "hash", Limits())["error"] == "parse_timeout"
    assert kills


def test_network_guard(monkeypatch):
    # monkeypatch records originals so this unit test cannot affect other tests.
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    disable_network()
    with pytest.raises(ImportProblem, match="network_disabled"):
        socket.create_connection(("example.invalid", 443))


def test_cli_inventory_and_missing_include(roots):
    source, out = roots
    pdf(source / "one.pdf")
    args = ["inventory", "--source", str(source), "--output", str(out)]
    assert main(args) == 0
    assert read_json(out / "manifest.json")["files"][0]["sha256"] == digest_file(source / "one.pdf")
    assert main(args + ["--include", "missing.pdf"]) == 1


def test_pdf_native_image_supplement_when_layout_omits_picture(roots):
    from PIL import Image

    source, out = roots
    path = source / "scan.pdf"
    Image.new("RGB", (100, 100), "navy").save(path, "PDF")
    doc = SimpleNamespace(pages={1: SimpleNamespace()}, iterate_items=lambda: iter([]))
    result = pdf_native(path, doc, out)
    page = result["pages"][0]
    assert page["native_stats"]["image_objects"] == 1
    assert len(page["native_images"]) == len(page["elements"]) == 1
    item = page["elements"][0]
    assert "native_image_supplement_not_retained_by_docling" in item["warnings"]
    assert (out / item["asset"]["path"]).is_file()
    classify(result)
    assert item["route"] == "visibility_review"
    assert "scan_review" in {s["route"] for s in page["suggestions"]}


@pytest.mark.parametrize("grouped", [False, True])
def test_pptx_xml_text_geometry_and_visibility(roots, grouped):
    # Minimal synthetic OOXML fixture, not a user-authored presentation.
    path, out = roots[0] / "fixture.pptx", roots[1]
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    shape = """<p:sp><p:nvSpPr><p:cNvPr id="2" name="code"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
    <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1200" cy="200"/></a:xfrm>
    <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/>
    <a:p><a:r><a:rPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:rPr><a:t>class A {</a:t></a:r></a:p>
    <a:p><a:r><a:rPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:rPr><a:t> int x = 1; }</a:t></a:r></a:p>
    </p:txBody></p:sp>"""
    if grouped:
        shape = (
            """<p:grpSp><p:nvGrpSpPr><p:cNvPr id="3" name="group"/>
        <p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>
        <a:off x="100" y="200"/><a:ext cx="500" cy="500"/>
        <a:chOff x="0" y="0"/><a:chExt cx="1000" cy="1000"/>
        </a:xfrm></p:grpSpPr>"""
            + shape
            + "</p:grpSp>"
        )
    with ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
        <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
        <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
        <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>""",
        )
        z.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{rel}"><Relationship Id="rId1" Type="{r}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
        )
        z.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="1000" cy="1000"/></p:presentation>',
        )
        z.writestr(
            "ppt/_rels/presentation.xml.rels",
            f'<Relationships xmlns="{rel}"><Relationship Id="rId1" Type="{r}/slide" Target="slides/slide1.xml"/></Relationships>',
        )
        z.writestr(
            "ppt/slides/slide1.xml",
            f'<p:sld xmlns:p="{p}" xmlns:a="{a}" show="0"><p:cSld><p:spTree>{shape}</p:spTree></p:cSld></p:sld>',
        )
    assert preflight(path, Limits())["page_count"] == 1
    result = pptx_native(path, out)
    item = result["pages"][0]["elements"][-1]
    assert result["pages"][0]["hidden"]
    assert item["kind"] == "code" and "\n int x" in item["text"]
    assert "white_text_visibility_uncertain" in item["warnings"]
    if grouped:
        assert item["group"] == "p1-shape3"
        assert item["bbox"] == [0.1, 0.2, 0.7, 0.3]
        assert "outside_page_bounds" not in item["warnings"]
    else:
        assert "outside_page_bounds" in item["warnings"]
