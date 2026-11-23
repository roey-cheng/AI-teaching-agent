# 离线课件导入器 · 第一阶段

只做「盘点 → 原生解析 → 规则分流 → 人工检查报告」。不是完整 RAG：没有执行 OCR、视觉 API、Embedding、数据库写入或自动发布。`publishable` 始终为 `false`。

从仓库根目录运行（macOS / Linux，Python 3.12–3.14）：

```bash
uv sync --project tools/course_import --locked
uv run --project tools/course_import course-import run \
  --source data/course_materials \
  --output data/course_imports/phase1 \
  --library course-materials
```

安装依赖需要网络。解析子进程关闭网络和远程模型服务，不读取项目 `.env`，不继承 API Key。PDF 使用本地 Docling 布局和表格模型，必须事先准备模型缓存；缺少缓存会失败，不会偷偷联网下载。初次在新机器准备缓存时，可单独执行官方工具（这一步会下载模型，不是付费推理）：

```bash
uv run --project tools/course_import docling-tools models download layout tableformer
```

不安装/调用 Keynote、PowerPoint、LibreOffice，不执行课件代码、宏或外链。PPTX 不转换整页图片；PDF 的 Docling 图片提取会使用内部 PDF 页面图像，这不需要桌面应用，也不执行 OCR。

## 小批试跑

```bash
uv run --project tools/course_import course-import run \
  --output data/course_imports/phase1-samples \
  --include DesignPatterns_CompositePattern.pptx \
  --include Java_GUI_MouseHandling_AdapterClasses.pptx \
  --include TemplateMethodDesignPattern.pdf
```

`--include` 是相对于输入目录的精确路径，可以重复。未找到指定文件时退出，不会悄悄扩大处理范围。

## 分步与重跑

```bash
# 只扫描、计算哈希、记录页数和拒绝原因
uv run --project tools/course_import course-import inventory --output data/course_imports/phase1
# 按现有清单解析；失败的文件重试，完整且匹配的缓存复用
uv run --project tools/course_import course-import parse --output data/course_imports/phase1
# 不重新解析，仅重建报告
uv run --project tools/course_import course-import report --output data/course_imports/phase1
```

输入文件发生修改后，用 `run` 或 `inventory` 更新清单；直接 `parse` 会拒绝哈希变化。清单中的 `--timeout` 在 `inventory/run` 时确定，默认每份文件 300 秒；此超时覆盖解析子进程，不覆盖清单扫描。串行处理，不同文件失败相互隔离；同一输出目录禁止两个导入进程同时运行。退出码 1 表示失败或存在拒绝文件。

缓存由文件内容、格式、依赖版本、导入器源码与配置共同决定。完成标记保存 JSON/图片的哈希，缺失或损坏则重建；旧损坏结果保存在 `.invalid-*`，中断/失败残留保存在 `.partial-*`，不会当作完整结果使用。`objects/` 是派生缓存而非原始资料备份。

本地 Docling 模型权重不包含在 Git 或缓存指纹中；如果手动更换了权重缓存，请使用新的 `--output` 目录重新解析，避免沿用旧模型结果。

## 看哪里

| 输出 | 用途 |
|---|---|
| `manifest.json` | 相对文件名、SHA-256、页数、状态、错误码、缓存位置 |
| `report.html` | 用浏览器打开；点击文档，再展开页码检查 |
| `report.md` | VS Code 用 `⇧⌘V` 预览（Windows/Linux：`Ctrl+Shift+V`） |
| `document-*.html/md` | 原文、代码块、图片、坐标、元素 ID、警告和分流理由 |
| `objects/<key>/result.json` | 规范化结果，每页/每元素可定位；未审核 |
| `objects/<key>/docling.json` | Docling 原始结构（去掉内嵌图片、URI；图片单独存储） |
| `objects/<key>/assets/` | 按内容哈希命名的图片；不是模型解释 |

PDF 的 `native_images` 另外保留文件里的原始图像流。若 Docling 没把它保留为 picture，报告会补出图片并标记；图片型表格因此不会只剩一个空表格。原始图像流可能不包含页面裁剪、遮罩或组合变换，不能直接当作最终页面画面。已匹配到 Docling 图片的原始流保存在 JSON 中，不重复展开显示；位置匹配也只是来源线索，不代表内容已审核。

## 人工审核工作台

不要在编辑器里阅读 `report.html` 源代码。启动本地工作台：

```bash
uv run --project tools/course_import course-import review \
  --source data/course_materials \
  --output data/course_imports/phase1
```

命令会打开浏览器并一直运行；终端按 `Ctrl+C` 停止。默认只列出未作整页决定且优先级至少 90 的页面，当前整批是 18 页，不要求先翻完 652 页。取消“只看高优先级”后可处理组合布局、普通图片候选、重复装饰和未触发检查规则的抽查页。未触发规则不代表内容无误或已审核。快捷键 `j` / `k` 切换下一项/上一项。

PDF 左侧显示本地生成的页面预览。PPTX 显示根据原生坐标重建的结构预览，不启动任何 Office 软件，也不声称等同于 PowerPoint 的最终画面。右侧可以对整页或单个元素选择“直接使用、排除、需要 OCR、视觉理解、整页理解”。“相同图片全部排除”适合确认后的重复校徽/装饰，决定按图片哈希作用于所有引用。

决定实时保存到 `review-decisions.json`，只存文档 ID、页码、元素 ID、决定、备注和时间；不改解析结果。审核工作台只绑定本机回环地址，使用临时访问令牌，不提供公网服务。它仍是本地管理员工具，不适合作为公开上传/多人协作系统。

“已作整页决定”只统计有整页决定的页数，选择“需要 OCR/视觉理解/整页理解”也计入，不表示后续处理已完成；只对元素或重复图片作决定不会增加该页数。“已保存 … 项决定”则统计所有层级的决定。现有 `review-decisions.json` 格式及解析清单中的状态不变。

HTML 是静态离线报告，转义文本、禁止脚本和远程资源；原始课件和所有报告默认 Git 忽略，不提交、不上传。不记录设备信息或本地绝对路径。报告包含课程原文，仍应视为私有资料。

## 分类是怎么判断的

- 原生文字/表格：直接取文件里的结构，不用 OCR。疑似代码根据关键词和语法符号识别，只加标签，不改源码。
- 独立图片候选：有图片、未发现文字重叠/同组对象；**不表示已理解图或已确认可独立使用**。
- 组合画面候选：图片与文字重叠、同组，或有连接线/自由线条；PDF 中较多矢量线条也会触发。装饰线可能误报。
- 扫描候选：PDF 原生文字少于 80 字符、最大图片覆盖至少 55% 页面积；也可能只是整页示意图，需要人工分辨。
- 可见性存疑：白色文字、越界、旋转、隐藏对象、图片裁剪等。保留原文并警告；不会悄悄作为正文使用。
- 隐藏页/演讲备注：保存但默认排除。重复小图只标记可能是装饰，不自动删除。

这些规则不是准确率承诺，也不互斥。只有人工检查/后续步骤才能决定 OCR、看图或重建画面。页码从 1 开始；坐标为归一化左上角 `[left, top, right, bottom]`。PPTX 保留文本框边界和组层级，**元素顺序不等于已验证的阅读顺序**。

## 已知边界

本阶段不还原 PPT 动画步骤、SmartArt 语义、复杂公式、主题字体的最终可见性或所有 Office 绘图效果。白色文字不一定不可见，因此只标记不删除。PDF 表格结构可能识别错误；图片型表格未 OCR，不保证有单元格文本。Docling 未保留的 PDF 图像/文字会产生页级检查提示，但提示不等于已找全遗漏。

文件大小/页数/ZIP 展开量有限制，禁止符号链接和宏，子进程有超时与网络保护，但这不是操作系统级安全沙箱。当前只用于本地可信课件，不应直接作为公网任意文件上传入口；未来线上化需额外隔离、资源配额和内容审核。

## 开发验证

```bash
uv run --project tools/course_import pytest tools/course_import/tests -q
uv run --project tools/course_import ruff check tools/course_import
uv run --project tools/course_import ruff format --check tools/course_import
```

测试使用临时合成文件和模拟解析结果，不需要课程原件或模型服务。实际课件验证结果保存在忽略目录，不属于测试固件。
