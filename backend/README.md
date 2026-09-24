# 后端

使用 Python + FastAPI，在 `app/` 中逐步实现账号、聊天和 Deep Agents 等功能。目前已准备独立 Python 环境和基础依赖，并创建应用入口 `app/main.py` 及 `GET /health` 接口；账号和聊天等业务接口尚未实现。

## Python 环境

使用 Python 3.14 和 `uv` 管理环境。当前本机验证版本为 Python 3.14.7、uv 0.12.15。

| 文件或目录 | 作用 | 是否提交 Git |
|---|---|---|
| `.python-version` | 告诉 uv 本项目使用 Python 3.14 | 是 |
| `pyproject.toml` | 声明 Python 版本范围及项目直接依赖 | 是 |
| `uv.lock` | 锁定直接依赖及其依赖的具体版本，便于其他机器复现安装 | 是 |
| `.venv/` | 本项目实际使用的 Python 虚拟环境和已安装依赖 | 否，可重新安装生成 |

虚拟环境相当于后端自己的工具箱：项目依赖安装在这里，不修改系统 Python 的依赖环境。

### 已安装的基础依赖

| 依赖 | 本次安装版本 | 用途 |
|---|---|---|
| FastAPI | 0.141.1 | 后续编写 HTTP API |
| Uvicorn | 0.53.0 | 后续启动 FastAPI 服务 |
| Deep Agents | 0.7.15 | 后续组织模型调用、工具和记忆使用 |
| langchain-deepseek | 1.1.1 | 将 DeepSeek 模型接到 Deep Agents，已验证真实流式调用 |
| pydantic-settings | 2.15.0 | 读取和校验数据库配置 |
| PyMySQL（含 rsa 依赖） | 1.2.3 | Python 连接 MySQL 的驱动 |

以上库还会自动安装它们所需的其他依赖，包括 LangGraph。安装了模型相关库不代表已经选定模型提供方，也不会自动调用模型。

MySQL 驱动、SQLAlchemy 和 Alembic 已安装，数据库配置读取和独立连接检查代码已编写。目前五张业务表的 ORM 模型已实现，尚未通过迁移创建业务表，没有迁移文件；接口 Pydantic Schema 尚未编写。

### 第一张表模型：users

- `app/db/base.py`：共同的 ORM 基类，收集表结构；不连接数据库。
- `app/models/user.py`：描述 users 的九个字段、唯一约束、CHECK 和 MySQL 类型，附中文注释。
- `app/models/__init__.py`：集中导入已经实现的模型。
- `tests/test_user_model.py`：检查模型结构，并离线生成 MySQL 建表 SQL 文本；不会执行 SQL。

在 `backend/` 中运行 `uv run python -m unittest discover -s tests -p 'test_user_model.py' -v` 可检查模型。测试通过只表示 Python 映射和 SQL 编译符合预期，不代表真实 MySQL 已建表或约束已生效；这些留到迁移阶段验证。此阶段不调用 `create_all()`，也不执行 Alembic 迁移。

### 其他四张表模型

| 文件 | 描述的业务表 | 重点 |
|---|---|---|
| `app/models/auth_session.py` | auth_sessions：登录状态 | 凭据哈希唯一，登录到期和撤销时间 |
| `app/models/chat_session.py` | chat_sessions：聊天会话 | 用户归属、标题、手动命名标记、侧栏活动时间 |
| `app/models/message.py` | messages：问题和回复 | 自引用外键、发送键防重复、USER/ASSISTANT 字段规则 |
| `app/models/agent_memory.py` | agent_memory：个人记忆 | 每用户每主题唯一，25 个固定主题与分类配对 |

`app/models/__init__.py` 统一导入五个模型，结构登记到同一个 `Base.metadata`。记忆主题允许列表 `MEMORY_TOPIC_TYPES` 与 CHECK 来源一致，供后续 Schema、工具和服务复用；未来迁移应冻结当时的约束，不直接引用不断变化的模型常量。

`tests/test_chat_models.py` 离线检查字段、外键目标、索引、唯一约束、CHECK 声明及全部表和索引的 MySQL SQL 编译。运行：

```bash
uv run python -m unittest discover -s tests -v
```

本次未添加 Engine、数据库会话、业务服务或迁移。模型也不会自动执行密码哈希、邮箱规范化、登录鉴权、消息重试、记忆更新或 UTC 转换；这些仍是后续业务职责。CHECK、唯一约束及外键是否在真实 MySQL 中生效，仍须迁移阶段的隔离数据库测试验证。

### 如何使用

以下命令从项目根目录开始执行，需要先安装 uv：

```bash
cd backend
uv sync --locked
uv run python --version
uv run python -c "from deepagents import create_deep_agent; print('Deep Agents 导入成功')"
```

- `uv sync --locked`：按锁定文件安装或同步依赖；首次使用可能需要联网下载 Python 和依赖。若依赖清单与锁定文件不一致，会报错而不是悄悄改锁定版本。
- `uv run`：使用本项目 `.venv` 运行后面的命令，不需要手动激活环境。

换电脑或重新下载项目后，可用同样的命令重新创建环境，不需要复制 `.venv`。

本次已验证：虚拟环境隔离、四项依赖导入、配置类实例化、临时 FastAPI 应用的内存内 HTTP 请求、依赖兼容性，以及锁定文件重复同步。临时测试没有留下接口代码，也没有启动监听端口或调用真实模型；这些检查不等于数据库、模型或完整聊天功能已验证。

## 配置模板

配置写在本地 `backend/.env`。目前实现了 DB_* 和 MODEL_* 的读取，其他字段仍为后续功能预留：

| 字段 | 含义 |
|---|---|
| APP_ENV | 当前运行环境，开发时为 development |
| FRONTEND_ORIGIN | 允许浏览器发起写请求的前端来源，开发示例为 http://localhost:5173 |
| DB_HOST / DB_PORT | MySQL 服务的地址和端口 |
| DB_NAME | 项目数据库名称，本机已由 Docker 初始化为 ai_teaching_assistant |
| DB_USER / DB_PASSWORD | ai_teaching_app 账号及其密码；用户已报告连接检查成功 |
| MODEL_PROVIDER | 当前填写 deepseek |
| MODEL_NAME | 模型名称，例如 deepseek-flash |
| MODEL_API_KEY | 模型 API Key，真实值只填写到本地 .env |
| MODEL_BASE_URL | 当前填写 https://api.deepseek.com；探针只允许 DeepSeek 官方地址 |

本机 `backend/.env` 已创建并填写；不要再用模板覆盖它。DB_PASSWORD 使用根目录 Compose 配置的 MYSQL_PASSWORD，不使用 MYSQL_ROOT_PASSWORD。两份配置目前是一次性复制，不会自动同步；以后改数据库密码需同步对应配置。

其他电脑首次配置时，按上表在本目录创建 `.env` 并填写；不要覆盖已有配置。不要把真实信息放进公开模板；本地 `.env` 已被 Git 忽略。

## 数据库配置与连接检查

最初编写代码时只做了模拟测试；随后用户已报告运行下方命令并看到“数据库连接成功”。2026-09-24 的模型检查没有重新连接 MySQL。模拟测试和真实连接是两类验证，不能相互代替。

| 文件 | 负责什么 |
|---|---|
| app/core/config.py | 从 backend/.env 或环境变量读取 DB_*，校验端口、必填项及非空密码 |
| app/db/connection.py | 将配置交给 PyMySQL 建立连接，设置连接、读、写各 5 秒超时 |
| app/db/check.py | 独立命令：读取配置 → 建立连接 → 执行 SELECT 1 → 检查结果 → 关闭连接 |
| tests/test_db_config.py | 用虚构配置验证读取、格式校验、密码掩码，不使用真实密码 |
| tests/test_db_check.py | 用模拟数据库连接验证成功、失败、参数与关闭逻辑，不访问 MySQL |

`SecretStr` 让常规显示密码对象时呈现掩码，不是把 .env 加密；不要主动输出明文密码。检查命令失败时只输出安全提示，不打印原始异常或连接参数。

配置文件按代码位置定位，不依赖终端当前目录。同名 DB_* 环境变量优先于 .env，注意不要让旧环境变量覆盖本地配置。只加载数据库配置，不会读取根目录管理员密码，也不会把配置发给前端。

需要重新检查时，确认 MySQL 已启动，然后在 backend 目录运行：

```bash
uv run python -m app.db.check
```

这条命令会实际连接数据库，而不是仅导入驱动。预期输出：

```text
数据库连接成功：SELECT 1 返回 1，连接已关闭。
```

输出成功并以退出码 0 结束，才算验证通过；失败以退出码 1 结束。SELECT 1 是只读查询，不建表、不写入数据。同步驱动暂用于独立检查，尚未接入 FastAPI 请求处理或后台生成任务；后续业务接入时再安排连接生命周期和并发。

仅检查代码逻辑、不访问真实数据库：

```bash
uv run python -m unittest discover -s tests -v
```

当前共 20 项测试通过（包括 /health、数据库与模型探针测试）。这些测试使用虚构配置和模拟调用，不访问真实数据库或模型，也不产生 API 费用。导入这些模块不会自动读取配置或连接数据库；/health 的行为保持不变。

参考：[Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)、[PyMySQL 连接参数](https://pymysql.readthedocs.io/en/latest/modules/connections.html)。

## Deep Agents 最小真实调用（阶段 0）

2026-09-24 已实际运行成功：通过 Deep Agents 调用本地配置的 DeepSeek 模型，询问“什么是 API？”，收到 **43 段非空回答文字**，最终正常结束。这不是模拟测试；它使用了 API 余额。片段数不是 token 数，后续运行也不保证仍为 43 段。

在 backend 目录运行：

```bash
uv run python -m app.agent.check
```

每运行一次都会重新发送问题并可能产生费用，不要当作免费的健康检查反复运行。

| 位置 | 用人话说 |
|---|---|
| app/core/config.py 中的 ModelSettings | 从 .env 取出模型名称、地址和密钥，先检查格式；不发送请求 |
| app/agent/check.py 中的 ChatDeepSeek | 配好“怎样联系 DeepSeek”的适配器 |
| create_deep_agent(model=...) | 把这个模型交给 Deep Agents，创建 Agent 执行流程 |
| agent.astream(...) | 开始真正提问，一边接收回答、一边打印到终端 |
| tests/test_agent_check.py | 使用假配置和模拟回答检查代码，不花 API 余额 |

探针限制为一次模型调用、最多 128 个输出 token、单次网络超时 30 秒、整体等待 60 秒，不自动重试。模型输出被截断、为空或未正常结束都不算成功。只打印安全错误提示，不打印原始异常或密钥；遇到错误可提供安全提示排查，不要发送 .env 内容。

这次只验证回答，所以在发给模型前隐藏所有工具（仅设置 tools=[] 不会移除框架内置工具）。没有挂载宿主机文件、连接数据库或设置持久化 checkpoint，也关闭了本次 LangSmith tracing。只发送固定测试问题和 Agent 提示，不发送项目文件或数据库内容。工具调用和长期记忆需要后续另外验证。

**终端逐段打印不等于网页 SSE 已完成。** 后面还要让 FastAPI 把这些文字转成 SSE，再让 React 接收并显示。当前网页仍是 /health 检查页面。

参考：[DeepSeek 官方调用说明](https://api-docs.deepseek.com/)、[ChatDeepSeek 适配器](https://docs.langchain.com/oss/python/integrations/chat/deepseek)、[Deep Agents 流式输出](https://docs.langchain.com/oss/python/deepagents/streaming)。模型名称以 DeepSeek 官方当前说明为准，适配器文档里的示例名称可能较旧。

## 启动后端与健康检查

在 backend 目录执行：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- `app.main:app` 指 `app/main.py` 中的 FastAPI 对象 `app`。
- `127.0.0.1` 只监听本机；`8000` 是后端使用的端口。
- `--reload` 用于开发时修改代码后自动重启，不是生产部署配置。
- 保持终端运行，用 Ctrl+C 停止。

访问 <http://127.0.0.1:8000/health>，应收到 `{"status":"ok"}`。这个接口只证明 HTTP 应用能响应，不会连接 MySQL 或调用大模型，不代表完整服务依赖已就绪；它不计入 11 个业务接口。

前端通过 Vite 开发代理访问同一个接口，启动步骤见 [前端说明](../frontend/README.md)。目前未配置宽泛的 CORS 许可，生产同站点部署时仍需单独配置转发。

已有可重复运行的健康接口测试：

```bash
uv run python -m unittest discover -s tests -v
```

这个测试不需要先启动服务，也不依赖数据库或模型。此前只有临时内存内测试；现在已新增实际应用入口和测试文件，但业务功能仍未实现。
