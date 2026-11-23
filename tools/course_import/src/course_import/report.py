"""Static local reports: document contents are data, never HTML or executable links."""

import html
import re
from collections import Counter
from pathlib import Path

from .common import atomic_text, read_json, safe_source
from .pipeline import valid_cache

LABELS = {
    "native_text": "原生文字/代码/表格",
    "image_review": "图片尚未理解",
    "visibility_review": "文字/图像可见性存疑",
    "structure_review": "结构需检查",
    "excluded_by_default": "默认排除（隐藏页/备注）",
    "vision_candidate": "独立图片候选",
    "needs_render_review": "组合画面候选",
    "scan_review": "扫描/整页图片候选",
    "empty_page_review": "空提取结果需检查",
}


def esc(value):
    return html.escape(str(value), quote=True)


def fenced(value):
    fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", value)), default=0))
    return f"{fence}text\n{value}\n{fence}\n"


def shell(title, body):
    return (
        "<!doctype html><html lang='zh'><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'\">"
        f"<title>{esc(title)}</title><style>body{{max-width:1100px;margin:32px auto;"
        "padding:0 20px;font:16px/1.6 system-ui;color:#253047;background:#fafafa}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef1f5;padding:12px}"
        "article,details{border:1px solid #ccd4df;padding:14px;margin:12px 0}"
        "img{max-width:100%;max-height:420px}small{color:#536178}a{color:#245fbb}"
        "</style><body>" + body + "</body></html>"
    )


def report(out: Path, manifest: dict):
    status_counts = Counter(r["status"] for r in manifest["files"])
    totals = Counter()
    summary = [
        (
            "<h1>课件导入检查报告</h1><p>解析成功 ≠ 内容完整 ≠ 审核通过。"
            "所有内容尚未批准用于 RAG。未执行 OCR、视觉 API、向量化或发布。</p>"
        ),
        f"<pre>{esc(dict(status_counts))}</pre>",
    ]
    md = [
        "# 课件导入检查报告\n",
        "解析成功不等于审核通过。所有内容均未发布。\n",
        fenced(str(dict(status_counts))),
    ]
    for index, row in enumerate(manifest["files"], 1):
        summary.append(f"<h2>{esc(row['source'])}</h2><p>{esc(row['status'])}</p>")
        md.append(fenced(row["source"] + " — " + row["status"]))
        if row.get("error"):
            summary.append(f"<pre>{esc(row['error'])}</pre>")
            md.append(fenced(row["error"]))
        if not row.get("object"):
            continue
        folder = safe_source(out, row["object"])
        if not valid_cache(folder):
            summary.append("<p>缓存校验失败，请重新 parse；本次报告不展示损坏内容。</p>")
            continue
        document = read_json(folder / "result.json")
        body = [
            f"<h1>{esc(row['source'])}</h1><p>来源 SHA-256: {esc(row['sha256'])}</p>",
            (
                "<p>坐标为页面左上角起算的归一化 [左, 上, 右, 下]。"
                "这里显示原始提取元素，并非原页面截图；白色/隐藏文字可能出现在此，须看标记。</p>"
            ),
        ]
        pages_md = [
            "# 逐页原生提取\n",
            fenced(row["source"]),
            "未审核；代码、文字保持提取内容，不代表视觉阅读顺序。\n",
        ]
        for page in document["pages"]:
            totals["pages"] += 1
            title = f"第 {page['number']} 页 · {page.get('title', '')}"
            body.append(f"<details><summary>{esc(title)}</summary>")
            pages_md += [f"## 第 {page['number']} 页\n", fenced(page.get("title", ""))]
            for warning in page.get("warnings", []):
                body.append(f"<p>⚠ {esc(warning)}</p>")
                pages_md.append(fenced(warning))
                totals["page_warnings"] += 1
            for suggestion in page.get("suggestions", []):
                totals[suggestion["route"]] += 1
                message = (
                    LABELS.get(suggestion["route"], suggestion["route"])
                    + " — "
                    + suggestion["reason"]
                    + " "
                    + ", ".join(suggestion["elements"])
                )
                body.append(f"<p>候选：{esc(message)}</p>")
                pages_md.append(fenced(message))
            for item in page["elements"]:
                totals[item["kind"] + "_elements"] += 1
                totals[item["route"]] += 1
                label = f"{item['id']} · {item['kind']} · {LABELS[item['route']]}"
                body.append(
                    f"<article><strong>{esc(label)}</strong><small> "
                    f"{esc(item['bbox'])}</small><p>{esc(', '.join(item['reasons']))}</p>"
                )
                pages_md += [
                    f"### {item['id']} · {item['kind']}\n",
                    fenced(label + "\n" + str(item["bbox"]) + "\n" + ", ".join(item["reasons"])),
                ]
                if item["text"]:
                    body.append(f"<pre>{esc(item['text'])}</pre>")
                    pages_md.append(fenced(item["text"]))
                asset = item.get("asset")
                if asset:
                    # Only validated, content-addressed raster assets are displayed.
                    if asset["previewable"] and re.fullmatch(
                        r"assets/[a-f0-9]{64}\.(png|jpg|jpeg|gif|tif|tiff|bmp|webp)", asset["path"]
                    ):
                        body.append(
                            f"<img loading='lazy' src='{esc(row['object'] + '/' + asset['path'])}' "
                            f"alt='{esc(item['id'])} 原始图片'>"
                        )
                    pages_md.append(fenced("图片: " + row["object"] + "/" + asset["path"]))
                body.append("</article>")
            body.append("</details>")
        html_name, md_name = f"document-{index:03}.html", f"document-{index:03}.md"
        atomic_text(out / html_name, shell(row["source"], "\n".join(body)))
        atomic_text(out / md_name, "\n".join(pages_md))
        summary.append(
            f"<p>{len(document['pages'])} 页 · "
            f"<a href='{html_name}'>逐页 HTML</a> · <a href='{md_name}'>Markdown</a></p>"
        )
        md.append(f"[逐页 HTML]({html_name}) · [Markdown]({md_name})\n")
    summary.insert(
        2, "<h2>提取及候选计数（候选可重叠，不是准确率）</h2>" + f"<pre>{esc(dict(totals))}</pre>"
    )
    md.insert(3, fenced(str(dict(totals))))
    atomic_text(out / "report.html", shell("课件导入检查报告", "\n".join(summary)))
    atomic_text(out / "report.md", "\n".join(md))
    return dict(totals)
