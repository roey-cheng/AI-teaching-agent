from __future__ import annotations

import io
from pathlib import Path

from .classify import classify, looks_like_code
from .common import ImportProblem, Limits, atomic_json, bbox, save_asset

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def element(identifier, kind, box, text="", **extra):
    return {
        "id": identifier,
        "kind": kind,
        "bbox": box,
        "text": text,
        "provenance": "native",
        "warnings": [],
        **extra,
    }


def pptx_native(source: Path, out: Path) -> dict:
    from pptx import Presentation
    from pptx.enum.dml import MSO_COLOR_TYPE
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    deck = Presentation(source)
    width, height = float(deck.slide_width), float(deck.slide_height)
    pages = []

    def walk(shapes, page, group=None, transform=(1.0, 1.0, 0.0, 0.0)):
        sx, sy, tx, ty = transform
        for shape in shapes:
            identifier = f"p{page['number']}-shape{shape.shape_id}"
            box = bbox(
                (float(shape.left) * sx + tx) / width,
                (float(shape.top) * sy + ty) / height,
                float(shape.width) * sx / width,
                float(shape.height) * sy / height,
            )
            warnings = []
            if min(box) < 0 or box[2] > 1.001 or box[3] > 1.001:
                warnings.append("outside_page_bounds")
            if shape.rotation:
                warnings.append("rotated_shape_geometry_requires_review")
            if any(n.get("hidden") in {"1", "true"} for n in shape._element.iter(P + "cNvPr")):
                warnings.append("hidden_shape")
            common = {"group": group, "warnings": warnings, "shape_type": str(shape.shape_type)}
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                page["elements"].append(element(identifier, "group", box, **common))
                xf = shape._element.find(P + "grpSpPr/" + A + "xfrm")
                if xf is None:
                    raise ImportProblem("unsupported_group_transform")
                off, ext = xf.find(A + "off"), xf.find(A + "ext")
                ch_off, ch_ext = xf.find(A + "chOff"), xf.find(A + "chExt")
                if any(x is None for x in (off, ext, ch_off, ch_ext)):
                    raise ImportProblem("unsupported_group_transform")
                cx, cy = float(ch_ext.get("cx", 0)), float(ch_ext.get("cy", 0))
                if not cx or not cy:
                    raise ImportProblem("invalid_group_transform")
                xs, ys = float(ext.get("cx")) / cx, float(ext.get("cy")) / cy
                child_transform = (
                    sx * xs,
                    sy * ys,
                    tx + sx * (float(off.get("x")) - xs * float(ch_off.get("x"))),
                    ty + sy * (float(off.get("y")) - ys * float(ch_off.get("y"))),
                )
                walk(shape.shapes, page, identifier, child_transform)
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                asset = save_asset(out, shape.image.blob, shape.image.ext)
                page["elements"].append(
                    element(
                        identifier,
                        "image",
                        box,
                        asset=asset,
                        crop=[shape.crop_left, shape.crop_top, shape.crop_right, shape.crop_bottom],
                        **common,
                    )
                )
                if any((shape.crop_left, shape.crop_top, shape.crop_right, shape.crop_bottom)):
                    warnings.append("cropped_image_original_contains_unshown_regions")
            elif shape.has_table:
                table = shape.table
                rows = [[c.text for c in row.cells] for row in table.rows]
                cells = [
                    {
                        "row": r,
                        "column": c,
                        "text": cell.text,
                        "merged": cell.is_merge_origin,
                        "spanned": cell.is_spanned,
                        "row_span": cell.span_height,
                        "column_span": cell.span_width,
                    }
                    for r, row in enumerate(table.rows)
                    for c, cell in enumerate(row.cells)
                ]
                page["elements"].append(
                    element(
                        identifier,
                        "table",
                        box,
                        "\n".join("\t".join(row) for row in rows),
                        rows=rows,
                        cells=cells,
                        **common,
                    )
                )
            elif shape.has_text_frame and shape.text.strip():
                # Vertical tab denotes a soft line break in python-pptx; preserve as newline.
                text = shape.text.replace("\v", "\n")
                runs = [r for p in shape.text_frame.paragraphs for r in p.runs if r.text.strip()]
                if runs and all(
                    r.font.color.type == MSO_COLOR_TYPE.RGB and str(r.font.color.rgb) == "FFFFFF"
                    for r in runs
                ):
                    warnings.append("white_text_visibility_uncertain")
                if len(text.strip()) == 1 and shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                    warnings.append("isolated_symbol_in_shape")
                kind = "code" if looks_like_code(text) else "text"
                if shape.is_placeholder and shape.placeholder_format.type in (1, 3):
                    kind = "title"
                    page["title"] = text
                if text.strip().isdigit() and box[1] > 0.85:
                    warnings.append("possible_page_number")
                page["elements"].append(element(identifier, kind, box, text, **common))
            else:
                connector = shape.shape_type in (MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.FREEFORM)
                page["elements"].append(
                    element(identifier, "drawing", box, connector_like=connector, **common)
                )

    for n, slide in enumerate(deck.slides, 1):
        page = {
            "number": n,
            "title": "",
            "hidden": slide._element.get("show") in {"0", "false"},
            "coordinate_system": "normalized_top_left",
            "elements": [],
        }
        walk(slide.shapes, page)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                page["elements"].append(
                    element(f"p{n}-notes", "speaker_notes", [0, 0, 0, 0], notes)
                )
        if not page["title"]:
            page["title"] = next(
                (e["text"].split("\n")[0] for e in page["elements"] if e["kind"] == "text"), ""
            )
        pages.append(page)
    return {"format": "pptx", "pages": pages}


def docling_document(source: Path, limits: Limits):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
        do_picture_description=False,
        do_picture_classification=False,
        do_code_enrichment=False,
        do_formula_enrichment=False,
        enable_remote_services=False,
        generate_picture_images=True,
        images_scale=1.5,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2),
        layout_batch_size=1,
        table_batch_size=1,
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.PPTX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    result = converter.convert(
        source, max_num_pages=limits.max_pages, max_file_size=limits.max_bytes
    )
    if result.status.value != "success":
        raise ImportProblem("docling_incomplete_conversion")
    return result.document


def export_structure(doc, out: Path):
    # Images live in explicit assets; do not embed base64 into the diagnostic JSON.
    raw = doc.export_to_dict()

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in {"image", "uri"}}
        if isinstance(value, list):
            return [scrub(x) for x in value]
        return value

    raw = scrub(raw)
    if isinstance(raw.get("origin"), dict):
        raw["origin"]["filename"] = Path(raw["origin"].get("filename", "")).name
    raw["name"] = "source_document"
    atomic_json(out / "docling.json", raw)


def pdf_native(source: Path, doc, out: Path) -> dict:
    import pypdfium2 as pdfium
    from pypdfium2 import raw

    pages = {
        n: {
            "number": n,
            "title": "",
            "hidden": False,
            "coordinate_system": "normalized_top_left",
            "elements": [],
        }
        for n in doc.pages
    }
    with pdfium.PdfDocument(source) as pdf:
        for n, p in pages.items():
            page = pdf[n - 1]
            w, h = page.get_size()
            images, paths = [], 0
            p["native_images"] = []
            for obj in page.get_objects():
                if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                    left, bottom, right, top = obj.get_bounds()
                    from PIL import Image

                    buffer = io.BytesIO()
                    obj.extract(buffer)
                    buffer.seek(0)
                    with Image.open(buffer) as original:
                        image_format = original.format or "bin"
                    p["native_images"].append(
                        {
                            "id": f"p{n}-native-image{len(images) + 1}",
                            "bbox": [left / w, (h - top) / h, right / w, (h - bottom) / h],
                            "asset": save_asset(out, buffer.getvalue(), image_format),
                            "provenance": "pdf_image_stream",
                            "appearance_note": "original_stream_masks_clipping_or_form_transforms_may_differ",
                        }
                    )
                    images.append(
                        max(0, min(w, right) - max(0, left))
                        * max(0, min(h, top) - max(0, bottom))
                        / (w * h)
                    )
                elif obj.type == raw.FPDF_PAGEOBJ_PATH:
                    paths += 1
            tp = page.get_textpage()
            text = tp.get_text_range()
            p["native_stats"] = {
                "text_chars": len(text.strip()),
                "image_objects": len(images),
                "largest_image_coverage": max(images, default=0),
                "vector_paths": paths,
            }
            tp.close()
            page.close()
    for i, (item, _) in enumerate(doc.iterate_items()):
        label = str(item.label.value)
        if not getattr(item, "prov", None):
            continue
        for prov_index, prov in enumerate(item.prov):
            page = pages[prov.page_no]
            size = doc.pages[prov.page_no].size
            b = prov.bbox.to_top_left_origin(page_height=size.height)
            box = [b.l / size.width, b.t / size.height, b.r / size.width, b.b / size.height]
            text = getattr(item, "text", "")
            kind = "code" if label == "code" or looks_like_code(text) else "text"
            extra = {"docling_ref": item.self_ref, "docling_label": label}
            if label in {"title", "section_header"}:
                kind = "title"
                if not page["title"]:
                    page["title"] = text
            if label == "table":
                kind = "table"
                text = item.export_to_markdown(doc=doc)
                extra["cells"] = [c.model_dump(mode="json") for c in item.data.table_cells]
            elif label == "picture":
                kind = "image"
                image = item.get_image(doc)
                if image is not None:
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    extra["asset"] = save_asset(out, buffer.getvalue(), "png")
                else:
                    extra["warnings"] = ["picture_pixels_unavailable_needs_render"]
            elif not text:
                continue
            if label in {"page_header", "page_footer"}:
                extra.setdefault("warnings", []).append("possible_repeated_header_or_footer")
            page["elements"].append(
                element(f"p{prov.page_no}-item{i}-{prov_index}", kind, box, text, **extra)
            )
    for page in pages.values():
        stats = page["native_stats"]
        pictures = [e for e in page["elements"] if e["kind"] == "image"]
        for native in page["native_images"]:
            a = native["bbox"]
            area = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
            matches = []
            for picture in pictures:
                b = picture["bbox"]
                intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
                    0, min(a[3], b[3]) - max(a[1], b[1])
                )
                if area and intersection / area >= 0.95:
                    picture.setdefault("native_image_ids", []).append(native["id"])
                    matches.append(picture["id"])
            native["matching_picture_ids"] = matches
            if not matches:
                page["elements"].append(
                    element(
                        native["id"],
                        "image",
                        a,
                        asset=native["asset"],
                        provenance="pdf_image_stream",
                        warnings=[
                            "native_image_supplement_not_retained_by_docling",
                            "original_stream_appearance_requires_review",
                        ],
                    )
                )
        extracted_images = sum(e["kind"] == "image" for e in page["elements"])
        page["warnings"] = []
        if stats["image_objects"] and not extracted_images:
            page["warnings"].append("native_images_not_retained_as_pictures_review_page")
        if stats["text_chars"] and not any(e["text"].strip() for e in page["elements"]):
            page["warnings"].append("native_text_not_retained_review_page")
    return {"format": "pdf", "pages": list(pages.values())}


def parse_document(source: Path, out: Path, limits: Limits) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    doc = docling_document(source, limits)
    export_structure(doc, out)
    if source.suffix.lower() == ".pptx":
        parsed = pptx_native(source, out)
        parsed["docling_page_count"] = len(doc.pages)
        if parsed["docling_page_count"] != len(parsed["pages"]):
            raise ImportProblem("page_count_mismatch")
    else:
        parsed = pdf_native(source, doc, out)
    parsed.update(
        schema_version=1,
        engine="docling-native-v1",
        ocr_executed=False,
        vision_executed=False,
        office_render_executed=False,
    )
    classify(parsed)
    return parsed
