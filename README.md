# AI Teaching Assistant

用于教学场景的 AI Assistant 项目。第一版先实现网页聊天，使用 React + TypeScript、FastAPI、MySQL 和 Deep Agents；后续在同一个项目中扩展课程、作业等功能。

## 项目目录

```text
AI-teaching-agent/
├── compose.yaml         本地 MySQL 启动配置
├── .env.example         Docker 数据库密码模板，不含真实密码
├── backend/
│   ├── app/             后端入口、/health、数据库配置及独立连接检查代码
│   ├── alembic/         数据库迁移环境与版本脚本
│   ├── alembic.ini      迁移目录配置，不含密码
│   ├── tests/           后端接口测试
│   ├── .env.example     后端配置模板，不含真实密码或密钥
│   ├── .python-version  后端使用的 Python 版本
│   ├── pyproject.toml   后端依赖清单
│   ├── uv.lock          后端依赖版本锁定文件
│   └── README.md        后端目录与配置说明
├── frontend/
│   ├── src/             React 账号、聊天、SSE 与记忆页面
│   ├── index.html       网页入口
│   ├── package.json     前端依赖与运行命令
│   ├── package-lock.json 前端依赖锁定文件
│   ├── vite.config.ts   开发服务和代理配置
│   ├── tsconfig.json    TypeScript 检查配置
│   ├── .env.example     可公开的前端配置模板
│   └── README.md        前端目录与配置说明
├── docs/                设计文档
└── .gitignore           Git 忽略规则
```

后端将按账号、Assistant 等功能划分模块，模块内区分接口、业务逻辑和数据读写；前端按登录、聊天和记忆页面组织。具体功能目录随开发创建，后续课程等模块继续放在这个项目中。

## 当前准备进度

已完成基础目录、Git 忽略规则、配置示例、后端独立 Python 环境，以及 React + TypeScript + Vite 网页应用。后端除 `/health` 外已接通 11 个业务接口。

后端已验证 Python 3.14.7、FastAPI、Uvicorn、Deep Agents 和配置库可正常使用；安装与检查命令见 [后端说明](backend/README.md)。

2026-10-12：表模型、迁移、API Schema、业务服务、正式 Deep Agents、记忆阶段、执行器及全部 11 个业务 HTTP 接口已接通，包含发送消息/失败重试 SSE。React 第一版业务页面也已完成。部署准备新增 Pages 同源代理、Turnstile、可信代理校验、MySQL 持久额度、数据库运行锁和 `/ready`；后端 455 项离线测试、154 项隔离 MySQL 测试及前端 5 项测试/类型检查/生产构建已通过。真实模型的完整公网聊天仍需上线后人工验收，会消耗模型额度。

手动体验真实聊天：MySQL 已启动且迁移、后端配置已准备好后，运行：

```bash
cd backend
uv run python -m app.cli.chat
```

程序先显示数据库、模型、tracing 和启动清理提示，确认后取得进程锁并核对残留状态，成功才进入注册/登录。真实提问会消耗 API 余额，聊天数据保存在项目库中；无需启动网页服务。终端和 FastAPI 共用业务与同项目进程锁，当前必须二选一运行。首次使用前停止旧版聊天进程，不允许其他目录副本或其他机器同时操作同一聊天库。

记忆流程现已改为 [Teaching Orchestrator 即时工具 + 空闲后台整理](docs/memory-lifecycle-zh.md)：普通问答不再前置一次 Memory Agent；主 Agent 按需读取完整主题，并在明确保存请求中直接调用受控写入工具。后台整理默认关闭，启用后等待用户空闲再处理成功对话。新增迁移 `20261012_0003`，内部持久保存真实推理/工具协议供回放；公开历史仍只有最终回答。迁移、启用开关、成本限制与隐私变化见该文档。敏感过滤不是完整隐私分类器，真实模型兼容性和提取质量仍需验收。

前后端已可分别启动，前端通过开发代理访问后端。已通过前端 SSE 分片测试、TypeScript 检查、生产打包、依赖审计，并在 Chrome 实际确认后端未登录响应会进入登录/注册页。用户已报告 Python 数据库检查成功；2026-09-24 已通过 Deep Agents → DeepSeek 的独立真实流式检查。为避免擅自消耗模型余额，本轮没有从网页发送真实模型问题；完整人工验收步骤见[前端说明](frontend/README.md)。

终端探针和正式聊天执行器均支持可选 LangSmith tracing（默认关闭）：配置方法和代码位置见[LangSmith 执行跟踪](backend/README.md#langsmith-执行跟踪可选)。未发送真实 trace 验证；网页会展示后端公开的进度与思考事件，但不会展示 LangSmith 后台页面。

## 启动网页

正式公网部署见 [Cloudflare Pages + Railway 部署手册](docs/cloudflare-pages-railway-deployment-zh.md)。

需要临时分享给老师时，见[临时公网演示](docs/temporary-demo-deployment-zh.md)：通过免费 HTTPS 隧道访问本机的正式构建页面，电脑需持续开机联网。

先启动 MySQL 并确认迁移已完成、后端 `DB_*` 已配置，停止本项目终端聊天。FastAPI 现在启动就会核对数据库和清理残留状态，失败则不接受请求；清理不删除聊天、不调用模型。只使用单进程，不关闭 lifespan 或配置多个 worker。

在两个终端分别运行（均从项目根目录开始）：

```bash
# 终端一：后端
cd backend
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
# 终端二：前端
cd frontend
npm ci
npm run dev
```

`npm ci` 在首次安装或重建依赖时运行即可。两个服务都启动后访问 <http://localhost:5173/>，未登录时应显示登录/注册页。按 Ctrl+C 可停止所在终端的服务。这是本机开发环境，不是已部署的公网网站。

## 本地配置

本机 MySQL 8.4.11 已通过 Docker 启动，项目数据库和账号已初始化；容器内和本机 3306 端口的客户端登录及 `SELECT 1` 测试均通过。根目录本地 `.env` 已填写密码，不提交 Git。启动、连接和停止步骤见 [本地 MySQL 说明](docs/local-mysql-setup-zh.md)。用户已报告迁移建表成功，业务数据仍待后续注册、聊天等功能写入。PyMySQL 已安装，用户已报告运行 Python 数据库检查成功；重新检查的方法见 [后端说明](backend/README.md)。

需要配置时，在各目录中将 `.env.example` 复制为 `.env`，再填写本机信息。`.env` 用于本地真实配置，已加入 Git 忽略规则；`.env.example` 保留在仓库中，供其他开发者了解需要哪些配置项。

根目录 `.env` 是给 Docker Compose 用的；`backend/.env` 是给后端用的，两者不是同一份配置。本机这两个文件都已创建，之后不要用模板覆盖已填写的密码。

后端已实现 DB_*、MODEL_* 配置读取和校验；模型提供方选定为 DeepSeek，已完成独立 Agent 流式探针。前端使用同站点相对路径 `/api/v1/...`，本地由 Vite 代理到后端，因此无需在前端 `.env` 保存后端地址或密钥。在 backend 目录运行 `uv run python -m app.agent.check` 可重新验证模型，但每次都会消耗 API 余额。

前端代码与构建后的配置可以被浏览器用户查看，因此模型 API Key、数据库密码和登录凭据只能留在后端，不能写入前端配置模板或代码。

## 第一版设计

- [功能范围与决策](docs/superpowers/specs/2026-09-20-ai-chat-demo-scope-and-decisions-zh.md)
- [API 设计](docs/superpowers/specs/2026-09-20-ai-chat-demo-api-design-zh.md)
- [内部实现设计与开发顺序](docs/superpowers/specs/2026-09-20-ai-chat-demo-implementation-design-zh.md)
- [第一版数据库表设计草案（待确认）](docs/superpowers/specs/2026-09-24-ai-chat-demo-database-design-zh.md)

原完整架构与数据库文档继续作为后续教学系统的设计参考。第一版按上面三份文档实施。
