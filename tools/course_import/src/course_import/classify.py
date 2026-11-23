"""Conservative, explainable routing suggestions; never automatic approval."""

import re
from collections import defaultdict


def looks_like_code(text: str) -> bool:
    strong = bool(re.search(r"\b(class|interface|enum|def)\s+\w+|\b(import|package)\s+[\w.]", text))
    syntax = sum(token in text for token in ("{", "}", ";", "(", ")", "="))
    return (strong and syntax >= 2) or (text.count("\n") >= 2 and syntax >= 5)


def overlaps(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def classify(document: dict):
    repeats = defaultdict(set)
    for page in document["pages"]:
        for e in page["elements"]:
            if e.get("asset"):
                repeats[e["asset"]["sha256"]].add(page["number"])
    for page in document["pages"]:
        elements = page["elements"]
        for e in elements:
            reasons = list(e.get("warnings", []))
            if e["kind"] == "table" and not e["text"].strip():
                reasons.append("empty_table_text_requires_review")
            e["review_status"] = "unreviewed"
            if page.get("hidden") or e["kind"] == "speaker_notes":
                e["route"] = "excluded_by_default"
                reasons.append("hidden_slide" if page.get("hidden") else "speaker_notes")
            elif reasons:
                e["route"] = "visibility_review"
            elif e["kind"] in {"text", "title", "code", "table"}:
                e["route"] = "native_text"
                reasons.append("text_from_document_not_ocr")
            elif e["kind"] == "image":
                e["route"] = "image_review"
                reasons.append("image_content_not_yet_understood")
            else:
                e["route"] = "structure_review"
                reasons.append("native_drawing_or_unsupported_object")
            if e.get("asset") and len(repeats[e["asset"]["sha256"]]) >= 3:
                area = max(0, e["bbox"][2] - e["bbox"][0]) * max(0, e["bbox"][3] - e["bbox"][1])
                if area < 0.05:
                    reasons.append("repeated_small_image_possible_decoration_not_discarded")
            e["reasons"] = reasons
        suggestions = []
        images = [e for e in elements if e["kind"] == "image"]
        meaningful = [e for e in elements if e["kind"] in {"text", "code", "table"}]
        connectors = [e for e in elements if e.get("connector_like")]
        if not page.get("hidden"):
            for e in images:
                if e["route"] != "image_review":
                    continue
                related = [t for t in meaningful if overlaps(e["bbox"], t["bbox"])]
                grouped = [
                    t
                    for t in elements
                    if e.get("group") and t.get("group") == e["group"] and t["id"] != e["id"]
                ]
                if related or grouped:
                    suggestions.append(
                        {
                            "route": "needs_render_review",
                            "elements": [e["id"]] + [t["id"] for t in related + grouped],
                            "reason": "image_overlaps_text_or_shares_group",
                        }
                    )
                else:
                    suggestions.append(
                        {
                            "route": "vision_candidate",
                            "elements": [e["id"]],
                            "reason": "potential_standalone_image_confirm_before_api_call",
                        }
                    )
            if connectors:
                suggestions.append(
                    {
                        "route": "needs_render_review",
                        "elements": [e["id"] for e in connectors],
                        "reason": "native_lines_or_freeforms_may_connect_content",
                    }
                )
            stats = page.get("native_stats", {})
            if stats.get("text_chars", 999) < 80 and stats.get("largest_image_coverage", 0) >= 0.55:
                suggestions.append(
                    {
                        "route": "scan_review",
                        "elements": [],
                        "reason": "little_native_text_large_image_could_be_scan_or_diagram",
                    }
                )
            if stats.get("vector_paths", 0) >= 8 and stats.get("text_chars", 0) > 0:
                suggestions.append(
                    {
                        "route": "needs_render_review",
                        "elements": [],
                        "reason": "pdf_vector_paths_with_text_may_be_diagram_or_decoration",
                    }
                )
            if not elements:
                suggestions.append(
                    {
                        "route": "empty_page_review",
                        "elements": [],
                        "reason": "no_extracted_elements_not_proof_of_blank_page",
                    }
                )
        page["suggestions"] = suggestions
    document["review_status"] = "unreviewed"
    document["publishable"] = False
