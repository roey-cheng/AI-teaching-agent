# AI Teaching Assistant

用于教学场景的 AI Assistant 项目。第一版先实现网页聊天，使用 React + TypeScript、FastAPI、MySQL 和 Deep Agents；后续在同一个项目中扩展课程、作业等功能。

## 项目目录

```text
AI-teaching-agent/
├── compose.yaml         本地 MySQL 启动配置
├── .env.example         Docker 数据库密码模板，不含真实密码
├── backend/
│   ├── app/             后端入口、/health、数据库配置及独立连接检查代码
│   ├── tests/           后端接口测试
│   ├── .env.example     后端配置模板，不含真实密码或密钥
│   ├── .python-version  后端使用的 Python 版本
│   ├── pyproject.toml   后端依赖清单
│   ├── uv.lock          后端依赖版本锁定文件
│   └── README.md        后端目录与配置说明
├── frontend/
│   ├── src/             React 连接检查页面与样式
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

已完成基础目录、Git 忽略规则、配置示例、后端独立 Python 环境，以及 React + TypeScript + Vite 前端环境。已编写连接检查页面和后端 `/health` 接口。

后端已验证 Python 3.14.7、FastAPI、Uvicorn、Deep Agents 和配置库可正常使用；安装与检查命令见 [后端说明](backend/README.md)。

前后端已可分别启动，前端通过开发代理访问后端。已通过后端健康接口测试、前端类型检查与打包，以及实际 HTTP 直连和代理请求检查。浏览器自动验证因本机 UI 控制超时未完成，打开页面确认显示结果的步骤见 [前端说明](frontend/README.md)。用户已报告 Python 数据库检查成功；2026-09-24 已通过 Deep Agents → DeepSeek 的独立真实流式检查，收到 43 段文字。登录、网页聊天、SSE 和长期记忆仍未接入，不能把独立探针当作完整业务已完成。

## 启动连接检查页面

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

`npm ci` 在首次安装或重建依赖时运行即可。两个服务都启动后访问 <http://localhost:5173/>，页面应显示“后端连接成功”。按 Ctrl+C 可停止所在终端的服务。这是本机开发环境，不是已部署的公网网站。

## 本地配置

本机 MySQL 8.4.11 已通过 Docker 启动，项目数据库和账号已初始化；容器内和本机 3306 端口的客户端登录及 `SELECT 1` 测试均通过。根目录本地 `.env` 已填写密码，不提交 Git。启动、连接和停止步骤见 [本地 MySQL 说明](docs/local-mysql-setup-zh.md)。尚未创建业务表。PyMySQL 已安装，用户已报告运行 Python 数据库检查成功；重新检查的方法见 [后端说明](backend/README.md)。

需要配置时，在各目录中将 `.env.example` 复制为 `.env`，再填写本机信息。`.env` 用于本地真实配置，已加入 Git 忽略规则；`.env.example` 保留在仓库中，供其他开发者了解需要哪些配置项。

根目录 `.env` 是给 Docker Compose 用的；`backend/.env` 是给后端用的，两者不是同一份配置。本机这两个文件都已创建，之后不要用模板覆盖已填写的密码。

后端已实现 DB_*、MODEL_* 配置读取和校验；模型提供方选定为 DeepSeek，已完成独立 Agent 流式探针。前端当前健康检查使用固定相对路径 `/health`，业务 API 配置变量尚未接入，因此这张页面无需创建前端 `.env`。在 backend 目录运行 `uv run python -m app.agent.check` 可重新验证模型，但每次都会消耗 API 余额。其他配置加载仍待后续接入。

前端代码与构建后的配置可以被浏览器用户查看，因此模型 API Key、数据库密码和登录凭据只能留在后端，不能写入前端配置模板或代码。

## 第一版设计

- [功能范围与决策](docs/superpowers/specs/2026-09-20-ai-chat-demo-scope-and-decisions-zh.md)
- [API 设计](docs/superpowers/specs/2026-09-20-ai-chat-demo-api-design-zh.md)
- [内部实现设计与开发顺序](docs/superpowers/specs/2026-09-20-ai-chat-demo-implementation-design-zh.md)
- [第一版数据库表设计草案（待确认）](docs/superpowers/specs/2026-09-24-ai-chat-demo-database-design-zh.md)

原完整架构与数据库文档继续作为后续教学系统的设计参考。第一版按上面三份文档实施。
