"use strict";

const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const launchDocument = new URLSearchParams(location.search).get("doc");
const launchPage = Number(new URLSearchParams(location.search).get("page") || 0);
history.replaceState(null, "", location.pathname + location.search);

const state = { index: null, queue: [], current: -1, page: null };
const categoryLabels = {
  scan: "扫描候选", empty: "空结果/空表格", visibility: "可见性",
  layout: "组合布局", vision: "图片理解", excluded: "隐藏页/备注",
  decoration: "重复小图", no_rule_flags: "未触发检查规则"
};
const routeLabels = {
  native_text: "原生文字", image_review: "图片待理解", visibility_review: "可见性存疑",
  structure_review: "结构待检查", excluded_by_default: "默认排除"
};
const kindLabels = {
  text: "文字", title: "标题", code: "代码", table: "表格", image: "图片",
  drawing: "绘图对象", group: "组合对象", speaker_notes: "演讲备注"
};
const reasonLabels = {
  image_content_not_yet_understood: "图片内容尚未理解",
  little_native_text_large_image_could_be_scan_or_diagram: "原生文字很少且大图占比高，可能是扫描页或教学图",
  native_image_supplement_not_retained_by_docling: "Docling 没保留这张 PDF 原图，导入器已补出",
  original_stream_appearance_requires_review: "原始图像流可能缺少裁剪、遮罩或页面组合效果",
  potential_standalone_image_confirm_before_api_call: "疑似独立教学图片，调用视觉模型前需确认",
  repeated_small_image_possible_decoration_not_discarded: "多页重复小图，可能是校徽或装饰；尚未自动删除",
  text_from_document_not_ocr: "文字来自文档原生结构，没有经过 OCR",
  empty_table_text_requires_review: "识别到表格区域但没有单元格文字",
  image_overlaps_text_or_shares_group: "图片与文字重叠或属于同一组合，需要结合画面理解",
  native_drawing_or_unsupported_object: "原生绘图或暂不支持的对象",
  native_lines_or_freeforms_may_connect_content: "线条或自由图形可能表示内容之间的关系",
  possible_page_number: "疑似页码",
  white_text_visibility_uncertain: "检测到白色文字，最终是否可见需要核对",
  outside_page_bounds: "对象超出页面边界",
  isolated_symbol_in_shape: "形状中只有一个孤立字符，可能是残留内容",
  cropped_image_original_contains_unshown_regions: "图片经过裁剪，原图含未显示区域",
  rotated_shape_geometry_requires_review: "旋转对象的边界需要核对",
  hidden_shape: "对象被标记为隐藏",
  hidden_slide: "隐藏幻灯片默认不用于检索",
  speaker_notes: "演讲备注默认不用于检索",
  native_images_not_retained_as_pictures_review_page: "PDF 有原始图片，但版面解析没有将其保留为图片元素",
  native_text_not_retained_review_page: "PDF 有文字层，但版面解析没有保留文字元素",
  no_extracted_elements_not_proof_of_blank_page: "没有提取到元素，但不能据此断定空白页"
};
const actionLabels = {
  use: "直接使用", exclude: "排除", ocr: "需要 OCR",
  vision: "视觉理解", full_page: "整页理解"
};
const suspiciousRoutes = new Set(["visibility_review", "structure_review", "image_review", "excluded_by_default"]);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"X-Review-Token": token, "Content-Type": "application/json", ...(options.headers || {})}
  });
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  return response.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function imageUrl(path) {
  return `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 1800);
}

function humanReason(reason) {
  return reasonLabels[reason] || `未翻译检查标记：${reason}`;
}

function selectedCategories() {
  return new Set([...document.querySelectorAll('input[name="category"]:checked')].map(x => x.value));
}

function buildQueue() {
  const categories = selectedCategories();
  const high = document.querySelector("#high").checked;
  const withoutPageDecision = document.querySelector("#without-page-decision").checked;
  const queue = [];
  for (const doc of state.index.documents) {
    for (const page of doc.pages) {
      if (!page.categories.some(value => categories.has(value))) continue;
      if (high && page.priority < 90) continue;
      if (withoutPageDecision && page.decision) continue;
      queue.push({doc, page});
    }
  }
  queue.sort((a, b) => b.page.priority - a.page.priority || a.doc.source.localeCompare(b.doc.source) || a.page.number - b.page.number);
  const active = state.queue[state.current];
  state.queue = queue;
  state.current = active ? queue.findIndex(x => x.doc.id === active.doc.id && x.page.number === active.page.number) : -1;
  renderQueue();
}

function renderQueue() {
  const root = document.querySelector("#queue-list");
  root.replaceChildren();
  document.querySelector("#queue-count").textContent = `${state.queue.length} 页`;
  let lastDoc = "";
  state.queue.forEach((item, index) => {
    if (item.doc.id !== lastDoc) {
      root.append(el("div", "queue-doc", item.doc.source));
      lastDoc = item.doc.id;
    }
    const button = el("button", `queue-item${index === state.current ? " active" : ""}`);
    button.type = "button";
    const title = el("strong", "", `第 ${item.page.number} 页`);
    if (item.page.decision) title.append(el("span", "status-mark", "✓"));
    button.append(title);
    button.append(el("small", "", item.page.categories.map(x => categoryLabels[x]).join(" · ")));
    button.addEventListener("click", () => select(index));
    root.append(button);
  });
  if (!state.queue.length) root.append(el("p", "empty-state", "当前筛选没有待审核页面。"));
}

async function select(index) {
  if (index < 0 || index >= state.queue.length) return;
  state.current = index;
  const item = state.queue[index];
  state.page = await api(`/api/document/${item.doc.id}?page=${item.page.number}`);
  renderQueue();
  renderPage();
}

function renderPage() {
  const page = state.page;
  document.querySelector("#page-title").textContent = `${page.source} · 第 ${page.number} 页`;
  document.querySelector("#page-meta").textContent = `${page.categories.map(x => categoryLabels[x]).join(" · ")} · 优先级 ${page.priority}`;
  document.querySelector("#decision-note").value = page.page_decision?.note || "";
  renderPreview(page);
  renderReasons(page);
  renderActions();
  renderElements(page);
  document.querySelector("#previous").disabled = state.current <= 0;
  document.querySelector("#next").disabled = state.current >= state.queue.length - 1;
}

function renderPreview(page) {
  const root = document.querySelector("#preview");
  root.className = "preview";
  root.replaceChildren();
  if (page.format === "pdf") {
    const img = el("img", "pdf-page");
    img.alt = `${page.source} 第 ${page.number} 页预览`;
    img.src = imageUrl(page.preview_url);
    root.append(img);
    document.querySelector("#preview-note").textContent = "PDF 本地页面预览，用于对照提取结果；没有执行 OCR。";
    return;
  }
  const slide = el("div", "slide-structure");
  for (const item of page.elements) {
    if (item.kind === "speaker_notes") continue;
    const box = el("div", `slide-element kind-${item.kind}${suspiciousRoutes.has(item.route) ? " suspicious" : ""}`);
    const [left, top, right, bottom] = item.bbox;
    box.style.left = `${Math.max(0, left) * 100}%`;
    box.style.top = `${Math.max(0, top) * 100}%`;
    box.style.width = `${Math.max(0.5, (Math.min(1, right) - Math.max(0, left)) * 100)}%`;
    box.style.height = `${Math.max(0.5, (Math.min(1, bottom) - Math.max(0, top)) * 100)}%`;
    box.title = `${item.id}: ${item.reasons.join(", ")}`;
    if (item.kind === "image" && item.asset_url) {
      const img = el("img");
      img.alt = item.id;
      img.src = imageUrl(item.asset_url);
      box.append(img);
    } else {
      box.textContent = item.text || item.kind;
    }
    slide.append(box);
  }
  root.append(slide);
  document.querySelector("#preview-note").textContent = "PPTX 结构预览：按原生坐标摆放文字和图片，不代表 PowerPoint/Keynote 的最终渲染效果。橙框表示存疑元素。";
}

function renderReasons(page) {
  const root = document.querySelector("#routing-reasons");
  root.replaceChildren();
  const reasons = page.routing_reasons.length ? page.routing_reasons : ["本页没有异常规则，仅供抽查"];
  for (const reason of reasons) root.append(el("li", "", humanReason(reason)));
}

function actionButton(action, targetType, targetId = "") {
  const button = el("button", "", actionLabels[action]);
  button.type = "button";
  button.dataset.action = action;
  button.addEventListener("click", () => decide(action, targetType, targetId));
  return button;
}

function renderActions() {
  const root = document.querySelector("#page-actions");
  root.replaceChildren();
  for (const action of Object.keys(actionLabels)) root.append(actionButton(action, "page"));
  document.querySelector("#reset-page").onclick = () => decide("reset", "page");
  if (state.page.page_decision) {
    root.prepend(el("div", "decision-badge", `当前：${state.page.page_decision.label}`));
  }
}

function isIssue(item) {
  return suspiciousRoutes.has(item.route) || item.reasons.some(r => !["text_from_document_not_ocr", "possible_page_number"].includes(r));
}

function renderElements(page) {
  const root = document.querySelector("#elements");
  root.replaceChildren();
  const issuesOnly = document.querySelector("#issues-only").checked;
  for (const item of page.elements) {
    if (issuesOnly && !isIssue(item)) continue;
    const card = el("article", "element-card");
    const header = el("header");
    header.append(el("strong", "", `${item.id} · ${kindLabels[item.kind] || item.kind}`));
    header.append(el("small", "", routeLabels[item.route] || item.route));
    card.append(header);
    for (const reason of item.reasons) card.append(el("span", "category", humanReason(reason)));
    if (item.text) card.append(el("pre", "", item.text));
    if (item.asset_url) {
      const img = el("img");
      img.alt = `${item.id} 提取图片`;
      img.loading = "lazy";
      img.src = imageUrl(item.asset_url);
      card.append(img);
    }
    if (item.decision) card.append(el("div", "decision-badge", `元素决定：${item.decision.label}`));
    if (item.asset_decision) card.append(el("div", "decision-badge", `同图决定：${item.asset_decision.label}`));
    const actions = el("div", "actions");
    for (const action of ["use", "exclude", "ocr", "vision", "full_page"]) actions.append(actionButton(action, "element", item.id));
    const reset = el("button", "", "撤销元素决定");
    reset.addEventListener("click", () => decide("reset", "element", item.id));
    actions.append(reset);
    if (item.asset?.sha256) {
      const repeated = el("button", "", "相同图片全部排除");
      repeated.addEventListener("click", () => decide("exclude", "asset", item.asset.sha256));
      actions.append(repeated);
    }
    card.append(actions);
    root.append(card);
  }
  if (!root.children.length) root.append(el("p", "empty-state", "本页没有符合当前元素筛选的项目。取消“只看存疑元素”可查看全部原生文字。"));
}

async function decide(action, targetType, targetId = "") {
  const previousIndex = state.current;
  const body = {
    doc_id: state.page.doc_id,
    page: state.page.number,
    target_type: targetType,
    target_id: targetId,
    action,
    note: document.querySelector("#decision-note").value
  };
  await api("/api/decision", {method: "POST", body: JSON.stringify(body)});
  state.index = await api("/api/index");
  state.page = await api(`/api/document/${body.doc_id}?page=${body.page}`);
  updateProgress();
  buildQueue();
  if (targetType === "page" && action !== "reset" && document.querySelector("#without-page-decision").checked && state.queue.length) {
    await select(Math.min(Math.max(previousIndex, 0), state.queue.length - 1));
  } else {
    renderPage();
  }
  toast(action === "reset" ? "决定已撤销" : "审核决定已保存到本地");
}

function updateProgress() {
  const total = state.index.documents.reduce((n, d) => n + d.page_count, 0);
  const pageDecisionCount = state.index.documents.reduce((n, d) => n + d.page_decision_count, 0);
  document.querySelector("#progress").textContent = `已作整页决定 ${pageDecisionCount} / ${total} 页 · 已保存 ${state.index.decision_count} 项决定`;
}

async function start() {
  if (!token) throw new Error("审核链接缺少本地访问令牌，请从终端重新打开链接。");
  state.index = await api("/api/index");
  updateProgress();
  buildQueue();
  const requested = state.queue.findIndex(item => item.doc.id === launchDocument && item.page.number === launchPage);
  if (requested >= 0) {
    await select(requested);
  } else if (launchDocument && launchPage) {
    state.page = await api(`/api/document/${launchDocument}?page=${launchPage}`);
    renderPage();
  } else if (state.queue.length) {
    await select(0);
  }
  for (const input of document.querySelectorAll(".filters input")) input.addEventListener("change", buildQueue);
  document.querySelector("#issues-only").addEventListener("change", () => state.page && renderElements(state.page));
  document.querySelector("#previous").addEventListener("click", () => select(state.current - 1));
  document.querySelector("#next").addEventListener("click", () => select(state.current + 1));
  document.addEventListener("keydown", event => {
    if (event.target.matches("textarea,input")) return;
    if (event.key === "j") select(state.current + 1);
    if (event.key === "k") select(state.current - 1);
  });
}

start().catch(error => {
  document.querySelector("#preview").textContent = error.message;
  toast(error.message);
});
