# 后端

语言约定：程序自身的运行提示和校验报错使用英文；代码注释与教学说明保留中文。用户消息、昵称、记忆及模型回答不在此规则下强制翻译。

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
| argon2-cffi | 25.1.0 | 注册业务使用的 Argon2id 密码哈希工具，自动生成随机盐 |

以上库还会自动安装它们所需的其他依赖，包括 LangGraph。安装了模型相关库不代表已经选定模型提供方，也不会自动调用模型。

MySQL 驱动、SQLAlchemy 和 Alembic 已安装，五张表模型、迁移、API Schema、账号/会话/历史业务已有测试，用户已报告项目库建表成功。已有正式 Deep Agents、聊天执行器、终端入口、思考/进度事件、结果保存和单进程生成控制。已接记忆存取服务、独立记忆 Agent 与 `/memory`，详见“Agent 自动记忆：两阶段执行”。失败重试业务及共享执行器已实现；终端和 FastAPI 已接启动残留清理及同项目进程锁。终端 `/retry`、业务 HTTP/SSE、Cookie 和聊天网页尚未接入。本次自动测试未调用真实模型或 LangSmith，也未连接或修改项目数据库。

## 终端真实聊天入口

在自己的交互终端中运行（不是把账号密码写进命令行）：

```bash
cd /Users/roey/projects/AI-teaching-agent/backend
uv run python -m app.cli.chat
```

前提：Docker 中的项目 MySQL 已运行，项目迁移已完成，`backend/.env` 的数据库和模型配置已填写。无需启动 Uvicorn、Vite 或浏览器；也不需要安装新依赖。只看帮助可以运行 `uv run python -m app.cli.chat --help`。

### 第一次怎样操作

1. 启动时先只读检查数据库版本、表及 Alembic 版本；不会自动建表，确认前不清理旧任务。
2. 程序显示目标数据库、模型、LangSmith 开关及启动清理提示。确认无误后输入 `y`，取得进程锁、再次检查数据库并核对残留状态；清理成功后才进入登录。直接回车则退出，不清理状态、不创建账号、会话或模型请求。
3. 输入 `r` 注册，按提示填写邮箱、密码、确认密码和昵称；已有账号则输入 `l` 登录。密码输入时不显示字符，是正常现象。注册后通过原有登录业务登录。
4. 登录成功会创建一个新会话。看到 `You>` 后输入自己的问题并回车，例如“请用中文解释 Python 的列表”。这才会调用真实模型，消耗 API 余额。
5. 程序分开显示 `[Progress]`、`[Thinking]` 和 `[Answer]`。思考文字仅在模型返回相应字段时出现；不是伪造的进度，也不承诺展示模型全部内部推理。看到 `[Saved]` 表示最终回答已保存。
6. 再发一条问题可验证多轮上下文；输入 `/history` 从 MySQL 读取当前会话，输入 `/quit` 退出。

| 命令 | 用途 |
|---|---|
| `/new` | 建立新会话，后续问题使用新的聊天上下文 |
| `/history` | 查询当前会话已保存的问答和生成状态，不调用模型 |
| `/memory` | 查询当前用户已保存的 Profile Memory，不调用模型 |
| `/help` | 显示命令说明 |
| `/quit` | 撤销本次终端登录并退出，不影响其他设备登录 |

账号、会话、问答和符合规则的个人记忆写入项目数据库，退出后仍保留。每个问题最多一次非思考的记忆判断模型调用，加一次正式思考/回答调用；重复消息回执不重新调用，命中本地敏感规则则跳过记忆模型。相较之前普通聊天通常增加一次模型请求和等待时间。思考和进度只实时显示，不保存到聊天记录。若 LangSmith 已开启，提示、记忆、推理及回答可能上传至所配置的项目；不希望上传时将 `LANGSMITH_TRACING=false`。本工具不打印或保存原始登录凭据到文件，不要把密钥或密码当聊天问题发送。

### 与未来网页版的关系

终端负责“读键盘、显示文字”，以后网页负责“接收 HTTP 请求、发送 SSE”。两者都调用同一个 `execute_chat_turn`，复用身份检查、消息保存、历史组装、Deep Agents 与最终回复保存，不另建一套聊天业务。此入口没有修改前端，也没有新增 HTTP 接口；终端打印不等于网页 SSE 已实现。

### 本次编写顺序和文件用途

1. `app/cli/__init__.py`、`app/cli/terminal.py`：建立终端模块；封装隐藏密码输入、安全文字显示和结构化事件的逐段打印。`message_done` 只显示保存结果，不重复打印整篇答案。
2. `app/services/generation_limit.py`：提供真实的准入检查，每用户在本进程滚动 60 秒最多接受 10 个新问题；`services/errors.py` 增加安全限流错误。准入时查重、忙碌或预算检查拒绝的请求不计数；计数后提交失败保守保留额度。
3. `app/cli/chat.py`：组合已有业务。启动检查 → 确认真实调用 → 注册/登录 → 新建会话 → 读取问题 → 调用执行器 → 显示事件 → 退出登录。使用同一个 `asyncio.Runner` 执行多轮异步聊天；等待键盘时没有模型在后台生成。
4. `tests/test_terminal_chat.py`、`tests/test_generation_limit.py`：验证命令、输入、事件显示、频率限制、错误和取消；`scripts/terminal_chat_mysql_cases.py` 加入现有隔离 MySQL 验收入口，验证实际登录、问答入库、多轮上下文和退出清理。

### 使用边界和验证

- 这是本机单进程调试工具。运行前停止使用同一数据库的其他聊天服务或 CLI；两种入口在代码中共存，但现在 FastAPI（即使仅提供 `/health`）和终端都会取得同一项目进程锁，不能同时运行。Vite 前端不持有此锁，可与 FastAPI 同时运行。
- 登录连续三次凭据错误会退出；生成频率限制重启后重置。尚未实现面向公网的注册/登录/IP 限流，不能把它当完整安全网关。
- Ctrl+C 会取消当前执行，等待在途数据库操作和资源清理，再退出登录。请等待，不要连续按。强杀或数据库不可用仍可能留下未确认状态；旧进程完全退出后的下一次启动会核对残留状态，但不会恢复或重跑模型。运行中数据库故障的自动修复仍未实现。
- 如果提交结果无法确认，会停止接受新问题并要求 `/history` 核对，不自动换 key 重发或重复调用模型。历史仍忙碌则继续禁止发送；历史查询失败则安全退出。
- 当前支持单行输入、新会话、当前会话历史和 `/memory`；记忆工具已接入，没有旧会话选择、失败回复重试或重新生成。失败可查看提示后有意发送一个新问题，它不等于重试原消息。
- 自动验收：324 项离线测试、95 项隔离 MySQL 测试通过，包括本次新增的 21 项离线测试和 5 项 MySQL 测试。使用真实 Deep Agents 图和临时 MySQL，但替换供应商网络流、关闭 tracing；不消耗你的 API 余额，不修改项目库。真实供应商的思考输出及 LangSmith 上传需由你按上述命令手动确认。

复查命令（在 `backend/` 中）：

```bash
uv run python -m unittest discover -s tests -q
# 要求 Docker 已启动，已有 mysql:8.4 镜像；只创建和清理专用临时测试容器。
uv run python scripts/check_migration_mysql.py
```

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

表模型阶段没有添加数据库会话、业务服务或迁移；随后已补充 Engine、只读连接检查及初始迁移脚本，见下方相应章节。模型不会自动执行密码哈希、邮箱规范化、登录鉴权、消息重试、记忆更新或 UTC 转换；这些仍是后续业务职责。CHECK、唯一约束及外键已通过迁移阶段的隔离 MySQL 测试，详见后面的真实验收记录。

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
Database connection successful: SELECT 1 returned 1; connection closed.
```

输出成功并以退出码 0 结束，才算验证通过；失败以退出码 1 结束。SELECT 1 是只读查询，不建表、不写入数据。同步驱动暂用于独立检查，尚未接入 FastAPI 请求处理或后台生成任务；后续业务接入时再安排连接生命周期和并发。

仅检查代码逻辑、不访问真实数据库：

```bash
uv run python -m unittest discover -s tests -v
```

当前共 267 项离线测试通过（2026-10-09 新增 23 项正式 Agent、只读记忆和调用前保护测试）。覆盖 /health、数据库与模型探针、五张 ORM 表模型、SQLAlchemy 连接检查、全部 API Schema、迁移及已实现业务。这些测试使用虚构配置、模拟调用、离线 SQL 编译或本地哈希计算，不访问真实数据库或模型，也不产生 API 费用。新增测试使用真实 Deep Agents 图搭配本地假模型，并将真实 DeepSeek 适配器的模型调用替换为测试返回值验证接线。应用模块导入不会自动读取配置或连接数据库；Alembic 在线命令会连接数据库。/health 的行为保持不变。

参考：[Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)、[PyMySQL 连接参数](https://pymysql.readthedocs.io/en/latest/modules/connections.html)。

### SQLAlchemy 连接检查

2026-09-24 已通过真实 MySQL 验证：SQLAlchemy → PyMySQL → MySQL，执行只读的 `SELECT 1` 返回 `1`，命令退出码为 0，结束时连接已释放。本次没有创建表、执行迁移或写入业务数据。最初沙箱内检查失败，同一命令在获准解除沙箱限制后成功，无需修改数据库配置。

| 文件 | 作用 |
|---|---|
| `app/db/engine.py` | 复用 DatabaseSettings，通过 `mysql+pymysql` 准备 SQLAlchemy Engine |
| `app/db/sqlalchemy_check.py` | 读取配置、实际连接、执行 SELECT 1、核对结果、释放连接 |
| `tests/test_sqlalchemy_check.py` | 验证配置传递、特殊字符密码、惰性连接、成功失败及清理，不访问真实 MySQL |

在 `backend/` 中执行：

```bash
uv run python -m app.db.sqlalchemy_check
```

预期输出：

```text
SQLAlchemy connection successful: SELECT 1 returned 1 via PyMySQL; connections released.
```

`build_database_engine()` 创建的是连接管理入口，直到 `engine.connect()` 才实际建立连接；它不导入模型或调用 `create_all()`。连接信息使用 `URL.create()` 分项传入，避免密码特殊字符导致手工拼接 URL 出错。日志不主动输出 SQL，检查失败不打印原始异常或配置值。

`with engine.connect()` 结束会归还连接；独立检查命令随后执行 `engine.dispose()`，释放池中连接。未来常驻后端应复用 Engine，在应用退出时释放，不照搬每个请求都 dispose 的探针用法。本次只提供同步连接验证，尚未接入 FastAPI 路由、ORM Session 或后台任务；未来异步业务必须另外安排同步数据库操作的执行位置和连接生命周期。

参考：[SQLAlchemy Engine 配置](https://docs.sqlalchemy.org/en/20/core/engines.html)、[连接管理](https://docs.sqlalchemy.org/en/20/core/connections.html)。

## Alembic 初始迁移

状态更新：隔离数据库的真实验收已完成，用户随后报告已手动执行项目库建表成功。以下保留初始化操作说明；补记忆/错误 Schema 时未重新执行迁移。

Migration 是数据库结构的版本变更记录，不是 API Schema，也不是把 Python 源文件存进 MySQL。Alembic 执行迁移中的建表指令，SQLAlchemy 将其转换成 SQL，再通过 PyMySQL 交给 MySQL。

| 文件 | 作用 |
|---|---|
| `alembic.ini` | 指定迁移目录；不保存数据库密码 |
| `alembic/env.py` | 登记模型 metadata，在线命令复用现有 DB_* 配置和 Engine；离线命令只输出 SQL |
| `alembic/script.py.mako` | 以后新增迁移文件时使用的模板 |
| `alembic/versions/20260925_0001_create_chat_tables.py` | 第一份固定结构快照：五张表及其主键、外键、唯一约束、CHECK、索引 |
| `tests/test_migrations.py` | 验证版本链、迁移与模型一致、建删表顺序、离线 SQL、配置与连接清理 |

迁移文件的 `upgrade()` 创建 users → auth_sessions → chat_sessions → agent_memory → messages；`downgrade()` 按相反顺序删除这些表，**会连同表内数据一起删除，不是无损撤销**。已在独立临时 MySQL 容器执行并验证这两个函数，没有在项目数据库上执行它们。

首份迁移不导入当前模型或记忆主题常量，而是固定保存当时的完整结构和 25 个主题配对，避免以后修改模型导致旧迁移改变。env.py 导入模型只是供后续差异比较；不会用 `Base.metadata.create_all()` 代替迁移。Alembic 自动比较不能保证发现所有 CHECK 等约束变化，新迁移必须人工审阅。

### 现在可以安全查看的内容（不连接数据库）

在 `backend/` 目录运行：

```bash
uv run alembic heads
uv run alembic history
uv run alembic upgrade head --sql
```

`heads` 预期包含 `20260925_0001 (head)`，表示代码里最新的迁移版本，不代表数据库已经执行到此版本。`history` 查看迁移链。**最后一条必须带 `--sql`**：只打印 SQL，不读取真实数据库配置，不执行 SQL。生成内容包括五张业务表，以及 Alembic 用于记账的 `alembic_version` 管理表。

从项目根目录也可使用 `backend/.venv/bin/alembic -c backend/alembic.ini heads`，路径不依赖终端当前工作目录。

### 真实 MySQL 验收与项目库建表

先核对 DB_* 指向正确的本地开发数据库；不要把本地密码发到聊天或提交 Git。在线配置拒绝 MariaDB 和低于 MySQL 8.4 的服务，项目实际验证目标仍是 MySQL 8.4。

1. 在明确可丢弃的隔离 MySQL 8.4 测试数据库验证 upgrade → downgrade → upgrade；只在该测试数据库验证删表，不在有用户数据的库里练习 downgrade。
2. 核对 SHOW CREATE TABLE、索引、外键、CHECK、字符集，以及合法插入、非法插入被拒绝、多行 NULL、事务回滚。详见[数据库设计第 11 节](../docs/superpowers/specs/2026-09-24-ai-chat-demo-database-design-zh.md#11-确认重点与实现验收)。
3. 确认项目库没有同名业务表、没有需保留的数据后，再执行 `uv run alembic upgrade head`。这条**不带 --sql**，会真正建表。
4. 运行 `uv run alembic current` 核对数据库版本为 `20260925_0001`，检查真实表结构；运行 `uv run alembic check` 辅助比较模型与库，不能替代 CHECK 等约束的实际验证。

首份在线迁移遇到已有同名业务表或视图会拒绝继续，不删除、不覆盖、不用 IF NOT EXISTS 掩盖结构差异；它不是接管旧库的脚本。MySQL DDL 可能隐式提交，整个 upgrade 不是一个可整体回滚的事务；失败时可能留下部分表（包括版本管理表）。不能盲目重跑、`stamp head` 或删除现存表，应先核对真实结构和版本，确定恢复方案。普通连接/SQL 错误只给英文安全提示，不直接输出凭据或原始异常。

**离线测试通过只表示脚本与当前模型一致且可生成 SQL，不表示 MySQL 已接受全部 DDL，也不表示真实约束已生效。** 因此另外提供显式运行的 `scripts/check_migration_mysql.py`，不纳入普通 unittest discovery。

2026-09-25 已完成真实验收：用现有 mysql:8.4 镜像创建临时容器，实际版本为 8.4.11，随机本机端口、独立 tmpfs 数据目录，不挂载项目数据库数据卷。脚本自行生成临时密码，将迁移子进程的全部 DB_* 指向临时目标，不修改任何 .env 文件，不接受外部数据库目标。

- `upgrade head`、`current`、`check` 通过；真实表结构、索引、字符集/排序规则与约束已核对。
- 9 组真实数据库测试通过：合法数据、中文/emoji、微秒时间、25 个记忆主题、多行 NULL、唯一约束、CHECK、外键与 RESTRICT、DML 事务回滚。
- 重复执行 `upgrade head` 不重建表且保留测试记录；`downgrade base` 后业务表及版本记录已移除，再次 `upgrade head` 和 `check` 通过。
- 测试结束后已删除本次临时容器和其中可丢弃的数据。没有删改项目数据库、既有容器或数据卷；随后只读核对项目库 `ai_teaching_assistant` 的表列表仍为空。

复测需要 Docker 正在运行、本机已有 mysql:8.4 镜像；会实际创建并清理临时测试资源，不产生模型 API 费用。在项目根目录执行：

```bash
backend/.venv/bin/python backend/scripts/check_migration_mysql.py
```

当前 324 项离线测试通过；真实验收共 95 组，包括 12 组执行器和 5 组终端聊天验证。只在新建临时容器测试，不操作项目数据库。已验证正式图执行、ASSISTANT 保存、两轮上下文、思考不入库、超时取消及迟到结果保护；不代表 HTTP 网页聊天可用。终端已接单进程生成限流，记忆写入、失败重试、启动/故障后自动核对仍待实现。项目库建表成功由用户之前报告。

参考：[Alembic 迁移环境](https://alembic.sqlalchemy.org/en/latest/tutorial.html)、[自动生成与审阅限制](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)。

## 第一批 Pydantic Schema：注册与用户信息

SQLAlchemy 模型描述“数据库怎么存”，Pydantic Schema 描述“接口接收和返回什么”。本次只写数据规则，没有接到 FastAPI 路由，没有执行注册、保存账号或建表。

| 文件 / 类 | 用途 |
|---|---|
| `app/schemas/auth.py` / `RegisterRequest` | 只接收 email、password、display_name；拒绝未知字段和错误类型 |
| `app/schemas/user.py` / `UserResponse` | 只返回 user_id、email、display_name，对应当前用户信息 |
| `app/schemas/auth.py` / `RegisterResponse` | 复用 UserResponse，并增加 UTC created_at，符合注册响应契约 |
| `app/schemas/__init__.py` | 统一导入入口，不是表模型登记或数据库连接 |
| `tests/test_account_schemas.py` | 17 项内存内测试：边界、输入类型、未知字段、输出过滤、ID 和 UTC |

输入规则：邮箱先去首尾空白、转小写，再校验合法格式和 320 字符上限；EmailStr 的邮箱合法性检查可能进一步拒绝不符合邮箱标准的长度或结构，不代表任意 320 字符字符串都合法。只接收纯邮箱，不接受“昵称 <邮箱>”。昵称去首尾空白后 1～100 字符。密码按 API 建议落实为 8～128 字符，不去空格、不改大小写。未增加其他密码复杂度规则。

使用 `pydantic[email]` 声明直接依赖，补充 `email-validator` 及其依赖；这不执行网络邮箱可投递性检查，也不发验证邮件。Schema 不能确认邮箱属于本人或是否已被注册，后者由后续业务和数据库唯一约束负责。

`SecretStr` 遮住常规显示及 JSON 序列化中的密码，不是哈希算法；后续业务需用 `request.password.get_secret_value()` 取出原密码执行专用密码哈希，不能把掩码保存为密码。`hide_input_in_errors` 隐藏异常文本中的输入，但原始 `ValidationError.errors()` / FastAPI 校验错误仍可能包含输入；未来接口异常处理必须移除输入内容并按 API 的统一错误格式响应，不能直接记录或返回完整请求/原始错误对象。

响应支持 `UserResponse.model_validate(user)` 读取 ORM 对象属性，再用 `model_dump(mode="json")` 生成可返回的字典。只声明三个公开字段，因此不会把密码哈希、系统角色或登录凭据带入响应；未来路由必须实际使用响应 Schema，不能绕过它直接返回原始记录。数字 ID 转字符串但不改变 ORM 对象，避免浏览器大整数精度损失。注册响应把项目约定的无时区 UTC 数据库时间补上 UTC 标记，有时区时间则转换到 UTC。

从 `backend/` 运行本阶段的离线测试：

```bash
uv run python -m unittest discover -s tests -p 'test_account_schemas.py' -v
```

参考：[Pydantic 模型](https://docs.pydantic.dev/latest/concepts/models/)、[邮箱类型](https://docs.pydantic.dev/latest/api/networks/#pydantic.networks.EmailStr)、[SecretStr](https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr)。

## 第二批 Schema：登录与聊天会话

本批沿用 API 设计第 4.2、5.1～5.3 节，新增六个 Schema，不创建新接口：

| 类 | 对应接口 / 用途 |
|---|---|
| `LoginRequest` | POST /api/v1/auth/login：邮箱、密码输入 |
| `LoginResponse` | 登录 JSON：`{"user": {...}}`，嵌套已有 UserResponse；不含 Cookie 或凭据 |
| `CreateSessionRequest` | POST /api/v1/chat/sessions：只接受 `{}`，不接受客户端指定用户或标题 |
| `RenameSessionRequest` | PATCH /api/v1/chat/sessions/{session_id}：只允许 title，首尾去空白后 1～100 字符 |
| `SessionResponse` | 新建、重命名和列表中的单个会话共用响应 |
| `SessionListResponse` | GET /api/v1/chat/sessions：`{"items": [...]}`，空列表为 `{"items": []}` |

登录类在 `app/schemas/auth.py`，会话类在 `app/schemas/chat.py`。`_validation.py` 提取已有的邮箱规范化、正整数 ID 格式化、数据库时间转 UTC 小函数，原注册与用户响应行为保持不变，各类共用同一套规则。下划线表示内部辅助模块的命名约定，不是 Python 的访问权限控制。

登录密码保持原样，只检查 1～128 字符；注册创建新密码仍要求 8～128 字符。Schema 通过不代表登录成功，账号存在性、密码哈希验证、ACTIVE 状态、限流及 Cookie 都属于后续业务和接口。请求拒绝未知字段，响应只保留公开字段；英文错误提示与密码遮罩规则保持不变。

`SessionResponse` 通过输入别名读取 ORM 的 `chat_session_id`，对外始终输出 `session_id`（包括按别名序列化时）；同时支持后端已整理好的 session_id 字典。ID 以字符串输出，三个时间按 UTC 输出，不返回 user_id、title_is_manual 等内部字段。响应只是读取并格式化，不修改 ORM 对象、不写数据库、不自动排序或筛选当前用户。

新建请求的空对象规则不代表用户可匿名创建会话；鉴权及归属必须由后续业务完成。查询列表没有请求体；拒绝 limit/cursor 等未知查询参数需在未来接口层落实，空请求体 Schema 不会自动检查 URL 查询参数。本批没有归档、分页、删除或实际重命名逻辑。

新增 `tests/test_login_session_schemas.py` 的 18 项离线测试，覆盖错误类型、额外字段、密码边界和遮罩、空请求、标题边界、嵌套输出、ORM 字段映射、UTC 与大整数精度。单独运行：

```bash
uv run python -m unittest discover -s tests -p 'test_login_session_schemas.py' -v
```

参考：[Pydantic 字段别名](https://docs.pydantic.dev/latest/concepts/alias/)。

## 第三批 Schema：消息、生成状态和 SSE 数据

沿用 API 设计第 6～8 节，覆盖接口 8（历史）、9（发送）、10（失败重试）及其响应数据；没有新增接口、订阅路由或真实流式发送实现。

| 文件 / Schema | 用途 |
|---|---|
| `app/schemas/message.py` / `SendMessageRequest` | client_message_key 与正文；正文最多 20,000 字符，拒绝纯空白，保留缩进与换行 |
| `RetryMessageRequest` | 只接收 failed_attempt_id；不接受旧 retry_key 或重复的问题正文 |
| `GenerationError`、`GenerationResponse` | 每条 USER 的错误摘要及最近生成状态；检查状态、回复编号、错误和 can_retry 是否矛盾 |
| `UserMessageResponse`、`AssistantMessageResponse` | 按 role 分别校验；USER 必有 generation，ASSISTANT 输出不含 generation |
| `MessageHistoryResponse` | session_id、is_generating、items；检查完整历史快照的排序、关联及重试标志 |
| `DuplicateMessageResponse` | 重复发送或重试的 JSON 回执；duplicate 必须是布尔 true，不携带完整回答 |
| `app/schemas/stream.py` / 四个 `Message*Data` | message_start、message_delta、message_done、message_error 的 data 结构 |
| `StreamError` | 流式错误中的 code、message、request_id；不是通用 HTTP 错误响应外壳 |
| `app/schemas/_types.py` | 复用数据库 ID、UUID 字符串及 UTC 时间的校验，供消息字段使用 |

UUID 输入采用标准带连字符的字符串形式，允许大小写，规范化成小写；不自动生成新键，也不限制为某个 UUID 版本。数据库数字 ID 输出字符串，拒绝布尔值、小数及超范围值。请求拒绝未知字段，因此前端不能提交 user_id、模型、工具、记忆或完整上下文。

生成状态规则：RUNNING 没有错误和回答编号且不可重试；SUCCEEDED 必须有回答编号、没有错误且不可重试；FAILED 必须有简短错误、没有最终回答编号。是否可重试仍由后端按当前有效状态计算。历史 Schema 额外拒绝重复/乱序消息、缺失或不匹配的答案关联、多个 RUNNING、空闲却仍 RUNNING、忙碌时或旧问题上标记可重试等矛盾；允许失败已落库但执行尚在清理的 busy=true 状态。它只检查给定快照，不查询数据库、修改排序、加锁、验证用户归属或保证查询时的一致性；服务层仍须负责这些事情。

USER 的 generation 是 API 嵌套对象，不是 messages 表的一列。后续查询层需将平铺生成字段、成功回答编号和计算后的 can_retry 组装成该对象，不能直接把未组装的 USER ORM 对象当完整历史响应返回。ASSISTANT 的公开字段与 ORM 对齐，可以按属性读取。

SSE 数据类上的 `event_name` 是类标签，不在 JSON 中重复输出；未来接口应按协议分别写 `event:` 和 `data:` 行。delta 不接受空字符串，但允许纯空格和换行，以保留模型输出格式；JSON 序列化会转义正文中的换行，真正的 SSE 编码/发送逻辑仍未实现。done 嵌套完整 ASSISTANT，error 必须包含 request_id。Schema 不能证明结果已提交，不能决定事件顺序、过滤正文内的敏感信息或阻止旧任务迟到写入；这些是后续 Agent、业务及流式接口的职责。

新增 `tests/test_message_schemas.py` 的 27 项测试，覆盖输入边界、UUID、状态组合、旧问题失败后新问题成功、生成清理、角色分流、历史关联、重复回执和四种 SSE 数据格式。测试不连接 MySQL 或发送模型请求：

```bash
uv run python -m unittest discover -s tests -p 'test_message_schemas.py' -v
```

参考：[Pydantic 按字段区分联合类型](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)、[模型校验器](https://docs.pydantic.dev/latest/concepts/validators/)。

## 第四批 Schema：个人记忆与通用 HTTP 错误

沿用 API 第 9 节及第 2.2 节，不添加业务接口、数据库列或迁移：

| 文件 / 类 | 用途 |
|---|---|
| `app/schemas/memory.py` / `MemoryResponse` | 记忆公开字段仅 memory_id、memory_type、summary、updated_at；支持读取已加载 ORM 对象 |
| `MemoryListResponse` | `GET /api/v1/me/memory` 返回 `{"items": [...]}`，无记忆时显式提供空列表 |
| `MemoryType` | 限定五种展示类别；不是 25 个内部主题 memory_key |
| `app/schemas/error.py` / `ErrorDetail` | 错误代号、可公开提示、请求编号：code、message、request_id |
| `ErrorResponse` | 通用 HTTP 错误外壳 `{"error": {...}}` |

记忆编号沿用正整数 ID 转字符串规则，更新时间沿用 UTC 规则；summary 限 1～500 字符并保留原文，不把用户内容强制翻译为英文。memory_key、user_id、created_at 等内部字段不输出。查询当前用户、按 updated_at DESC / memory_id DESC 排序、过滤敏感内容和写入记忆都仍由后续业务层负责；Schema 不查询、不排序、不鉴权。测试检查五种展示类别与现有 25 个模型主题的类别集合一致。

普通错误只有 error 外壳和三个必填字符串字段，code 最多 64 字符，message 最多 500 字符，request_id 非空但不假设它是 UUID。这些长度与现有 SSE 错误格式保持一致。不将 HTTP 状态码塞进 JSON；未来接口/异常处理器负责设置真实状态码，生成 request_id，并把 ValidationError、业务错误、未知异常转成安全的公开英文提示。输出字段过滤不是正文脱敏：不能直接传入原始异常文本，也不能原样返回可能含密码的 `.errors()` 数据。流开始后的错误继续使用 `MessageErrorData`，不更改已有 SSE 契约。

新增记忆 11 项、通用错误 8 项离线测试，覆盖 ORM 投影、五类记忆、字段过滤、长度/ID/时间边界、JSON Schema 与序列化、空列表、严格类型、错误外壳和与 SSE 错误字段的一致性。没有安装 FastAPI 异常处理器，运行时 `/health` 不受影响。

到这里，第一版已讨论的 API 请求/响应数据格式已补齐；GET 查询和 204 退出响应不为凑数量创建空 Schema。随后已实现下面的注册和登录业务逻辑；接口路由、Cookie 认证、查询参数拒绝、异常处理及其他业务仍未实现，不等于 11 个业务接口已经可用。

## 注册业务逻辑：先做服务，尚未接 HTTP 接口

目标：接收已由 `RegisterRequest` 校验的资料，创建一名普通用户，成功提交后返回 `RegisterResponse`。不自动登录，不写 auth_sessions、聊天或记忆，不创建 Cookie，不处理验证码或找回密码。

| 文件 | 负责什么 |
|---|---|
| `app/db/session.py` | `build_session_factory(engine)` 准备独立 ORM Session 的工厂；Session 用于本次查询、提交、回滚和关闭，不是登录状态 |
| `app/core/passwords.py` | `hash_password(SecretStr)` 使用 Argon2id 计算密码哈希；不打印或存储原密码 |
| `app/services/auth.py` | `register_user(request, session_factory)` 串起查重、哈希、插入、提交和公开响应 |
| `app/services/errors.py` | 邮箱已注册及注册结果无法确认的安全业务异常；没有 HTTP 依赖 |
| `tests/test_passwords.py`、`tests/test_registration_service.py` | 密码哈希与模拟连接测试，普通测试不访问数据库 |
| `scripts/registration_mysql_cases.py` | 真实 MySQL 注册用例，只由隔离测试脚本注入临时 Engine |

注册处理顺序：

1. 用短读取 Session 查询规范化后的邮箱。已有账号时抛出 `EmailAlreadyRegisteredError`，错误代号 `EMAIL_ALREADY_REGISTERED`，不计算哈希或修改原账号。
2. 关闭读取 Session 后再计算密码哈希，不在数据库事务中等待耗时的密码计算。使用 argon2-cffi 的 RFC_9106_LOW_MEMORY 参数：Argon2id、64 MiB、3 次迭代、并行度 4、随机盐。盐和参数已经包含在编码后的哈希里，不需要新增数据库列。密码不裁剪、不改大小写，不把 SecretStr 的星号掩码当作原密码。
3. 创建 `User`，角色固定 USER、状态固定 ACTIVE；创建和更新时间使用相同 UTC 值，last_login_at 为 NULL。编号由数据库生成。
4. 开启新的短写事务，`add()` 登记对象，`flush()` 执行 INSERT 并拿到编号，先校验可公开的响应，再退出事务上下文完成提交。只有提交成功才返回用户编号、邮箱、昵称和 UTC 注册时间，不返回密码、哈希或完整 ORM 对象。
5. 查重不是并发锁。两个请求都查到空时，由 MySQL 的 `uq_users_email` 唯一约束兜底。只有该约束的 1062 错误会映射成邮箱重复，不能把其他 SQL 错误都说成邮箱重复。
6. 上下文负责正常提交、异常回滚及关闭 Session。数据库或哈希失败返回安全的 `RegistrationUnavailableError`（代号 `REGISTRATION_UNAVAILABLE`），不暴露原始 SQL、异常、密码或连接信息，不自动重试。提交时断线可能无法确定是否落库，因此错误不保证“账号一定没创建”；需通过登录等方式核对，不能直接重复执行写入。

这是一段同步服务函数，不是 `/api/v1/auth/register` 路由。将来接口层需提供通过校验的请求、共享 Engine 创建的 Session 工厂，并负责 HTTP 201/409/503、Origin、限流、请求编号和安全错误转换。每次业务调用使用自己的 Session，不跨请求或线程共享；同步数据库/哈希计算不能直接塞进 async 路由阻塞事件循环。Engine 在应用生命周期复用，不在每次注册结束时 dispose。

真实验证：独立临时 MySQL 8.4.11 中的 5 组注册用例均通过：提交后新连接能读到账号、哈希验证原密码、响应不泄露哈希且不自动登录、规范化重复邮箱拒绝、两个并发请求只有一个成功、提交前故障回滚后可重新注册、哈希失败不插入账号。提交前注入故障的回滚验证不等于“网络断开时提交结果一定可知”。连同原迁移用例，共 14 组通过；临时容器和账号数据已删除，没有更改项目数据库。

从 backend 目录运行离线检查：

```bash
uv run python -m unittest discover -s tests
```

真实验收仍从项目根目录运行 `backend/.venv/bin/python backend/scripts/check_migration_mysql.py`，它自建、自清理隔离容器，不接受项目数据库作为目标。后续才实现注册路由和前端页面；目前打开网站仍只有 /health 检查。

参考：[argon2-cffi 密码哈希 API](https://argon2-cffi.readthedocs.io/en/stable/api.html)、[SQLAlchemy Session 与事务](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)。

## 登录业务逻辑：尚未接 HTTP / Cookie

`app/services/login.py` 的 `login_user(request, session_factory, current_token=None)` 接收已校验的 `LoginRequest`，只处理登录，不承担随后每次请求的登录状态检查或退出。`current_token` 将来由接口从 Cookie 读取并包装成 SecretStr，不能作为请求 JSON 新字段由前端指定。

| 文件 | 职责 |
|---|---|
| `app/core/passwords.py` / `verify_password` | 用 Argon2 工具验证原密码与已保存哈希；不通过重新生成随机哈希后比较字符串来验证 |
| `app/core/tokens.py` | 生成 32 字节随机登录凭据（URL-safe 编码为 43 字符），仅将 SHA-256 哈希写库 |
| `app/services/login.py` | 查询用户、验证密码、复核账号、轮换当前凭据、更新登录时间、保存登录状态 |
| `LoginResult` | 仅供后端内部使用的结果：公开 LoginResponse + SecretStr 凭据 + UTC 到期时间；不是接口 JSON |
| `tests/test_login_service.py`、`scripts/login_mysql_cases.py` | 离线规则验证与临时 MySQL 的真实事务验证 |

处理顺序：先短查询获取用户编号、密码哈希和状态，关闭读取 Session 后做密码验证。未知邮箱也用公开的占位哈希执行一次验证，不会因此创建账号或登录；这减少直接快速失败的差异，不承诺绝对恒定耗时。未知邮箱、错误密码、禁用账号均为 `INVALID_CREDENTIALS`。损坏的密码哈希也不能通过认证。后续接口必须加限流，并避免记录密码、凭据或原始请求。

密码验证成功后生成全新凭据。在一个短写事务内，锁定并重新检查用户仍 ACTIVE、密码哈希未改变，防止验证期间改密/禁用后仍签发凭据。记录登录时间和 updated_at；新 auth_sessions 的 expires_at 固定为 created_at + 7 天，revoked_at 初始为空。如果当前 Cookie 对应尚未过期/撤销的记录，同一事务只撤销这一条（切换账号也适用），不退出其他设备；不存在、畸形或过期旧凭据不阻止用正确密码重新登录。失败登录不撤销已有凭据。

只有提交成功才返回 `LoginResult`。未来接口仅将 `result.response` 序列化成约定的 `{"user": {...}}` JSON；使用 `result.token.get_secret_value()` 设置 HttpOnly/SameSite/Path/Secure 等 Cookie，不能把整个内部结果当 HTTP 正文返回或记录原凭据。随机凭据用 SHA-256，用户密码仍用 Argon2id，不混用两种哈希用途。

数据库失败统一为安全业务错误 `LOGIN_UNAVAILABLE`，不返回未确认提交的凭据、不自动重试；接口层将来映射为 503。提交断线可能产生未交付给浏览器的记录，不能承诺它一定没落库。Cookie 设置、Origin 校验、限流、HTTP 状态及请求编号仍待接口层处理；密码哈希参数升级/自动重哈希也没有在本次实现。

新增 12 项离线测试及 8 组真实 MySQL 用例；真实验证了凭据哈希持久化、固定 7 天、更新登录时间、统一凭据错误、轮换当前凭据保留其他设备、切换账号、过期/畸形 Cookie、错误密码不撤销旧登录、提交前故障整体回滚，以及验证期间禁用账号后拒绝登录。累计 163 项离线测试、22 组隔离 MySQL 用例通过，临时容器与测试数据均已清理，没有操作项目数据库。

参考：[Python secrets](https://docs.python.org/3/library/secrets.html)、[argon2-cffi verify](https://argon2-cffi.readthedocs.io/en/stable/api.html)。

## 登录状态检查业务：只读、不续期

入口是 `app/services/authentication.py` 的 `get_current_user(token, session_factory)`。以后接口从 Cookie 取出凭据，包装成 SecretStr 再调用；这个函数本身不接触 HTTP，不接收前端指定的 user_id，也不重新验证密码。

1. 缺少凭据或格式错误时直接拒绝，不连接数据库。
2. 复用 `hash_session_token` 算出哈希，一条 JOIN 查询把 auth_sessions 与所属 users 记录关联起来；只读取用户编号、邮箱、昵称、账号状态、到期及撤销时间，不读取密码哈希。
3. 记录不存在、已撤销、expires_at 小于或等于当前 UTC 时间、账号不是 ACTIVE，统一抛出 `AuthenticationRequiredError`（UNAUTHENTICATED）。
4. 通过检查后，用已有 `UserResponse` 返回公开的用户编号、邮箱和昵称。读取 Session 关闭；不提交写入，不修改登录时间或到期时间，不删除过期记录。
5. 数据库故障单独抛出 `AuthenticationUnavailableError`（AUTHENTICATION_UNAVAILABLE），不泄露底层异常，不自动重试。未来接口分别映射为 401 和 503，并补充统一错误格式；暂未实现这些 HTTP 映射。

这是查询时刻的身份检查，不会锁住账号直到后续业务完成；撤销或禁用后的新检查会拒绝，但不承诺中止已通过检查的在途操作。未来聊天/记忆业务仍必须按返回的用户编号限制数据访问，不能把身份检查当成完整的资源权限检查。

`tests/test_authentication_service.py` 新增 8 项离线测试；`scripts/authentication_mysql_cases.py` 新增 5 组真实 MySQL 测试，验证注册→登录→检查的完整链路、两个用户身份不混淆、未知凭据、微秒级到期边界、轮换后的旧凭据拒绝、其他设备仍有效、禁用后拒绝，以及多次检查不修改任何登录/用户字段。累计 171 项离线和 27 组隔离 MySQL 测试通过。没有改动数据库表结构，没有接入路由或退出业务。

## 退出登录业务：只撤销当前凭据

`app/services/logout.py` 的 `logout_user(token, session_factory)` 接收当前凭据（SecretStr 或 None），正常完成返回 None。它不创建 HTTP 响应、不清除浏览器 Cookie，不依赖 `get_current_user` 先通过：过期、撤销或禁用账号仍应能完成退出。

1. 没带凭据或格式不合法：直接成功，不查询数据库。
2. 合法格式的凭据：计算哈希，在短事务中执行一条条件 UPDATE，仅对哈希匹配、尚未撤销且尚未过期的 auth_sessions 设置 revoked_at 为当前 UTC 时间。
3. 影响 0 行也成功（未知、已过期、已撤销）；重复/并发退出不会覆盖原撤销时间。事务提交后才向调用方报告成功。
4. 不修改其他登录记录、用户、聊天或记忆；禁用账号无需重新通过登录检查，也可以撤销所持凭据。
5. 数据库执行或提交故障使用 `LogoutUnavailableError`（LOGOUT_UNAVAILABLE），未来接口映射 503，不泄露 SQL 或凭据，不自动重试。提交断线可能无法确认结果，不能声称一定退出成功或一定未提交；调用方后续重复退出是安全的。

未来接口成功时清除同名、同 Path 的 Cookie 并返回 204；无凭据也清 Cookie。Origin 校验仍必须按写请求约定执行，不能因为退出允许无效凭据就跳过。数据库故障不能伪装 204 成功。这些接口行为尚未实现。

新增 `tests/test_logout_service.py`（6 项离线）和 `scripts/logout_mysql_cases.py`（6 组真实 MySQL）：验证登录→检查→退出→旧凭据拒绝、其他设备/用户不受影响、业务数据原样保留、重复/并发退出、无效/过期凭据、禁用账号退出，以及提交前故障回滚。累计 177 项离线和 33 组隔离 MySQL 测试通过，测试容器及数据已清理。没有修改项目数据库、表结构或 HTTP 路由。

## 新建聊天会话业务：第一步

按“新建 → 列表 → 重命名”逐项实现和讲解，第一步是 `app/services/chat_sessions.py` 的 `create_chat_session(current_user, session_factory)`，返回已有的 SessionResponse。列表和重命名实现见后续两节。

- `current_user` 必须是后端调用 `get_current_user()` 完成身份验证后的 UserResponse，不得直接从前端请求正文构造。此函数不自行读取 Cookie 或重复检查登录；身份检查和实际操作间的禁用/撤销边界沿用前文约定。
- 在短事务中新增 ChatSession：user_id 取当前用户编号，title="new chat session"（保留用户的英文标题修改），title_is_manual=false，created_at、updated_at、last_activity_at 使用同一个 UTC 时间。ORM/初始迁移的数据库默认值仍是“新对话”，此业务显式写入英文值，不修改旧迁移。
- `add()` 登记待保存对象；`flush()` 真正执行 INSERT 并取得数据库编号，但尚未提交；SessionResponse 先校验/筛选返回字段，事务提交成功后才返回。
- 每次成功调用创建一个独立空会话，不写消息、记忆或登录记录，不调用 Agent。首条消息生成默认标题留到消息业务；不改 ORM、Schema 或迁移。
- SQLAlchemy 数据库故障转换成 SessionUnavailableError（SESSION_UNAVAILABLE），未来接口映射 503。提交结果不明时不自动重试、不声称一定没建成；未来前端先刷新列表再决定是否重新创建。
- 未来 POST /api/v1/chat/sessions 仍只接受 CreateSessionRequest `{}`，接口负责先验证身份与请求体。这里不新增 HTTP 路由，不改变 API 字段。

新建阶段新增 6 项离线测试（`tests/test_chat_session_service.py`）和 5 组真实 MySQL 测试（`scripts/chat_session_mysql_cases.py`）：验证默认值/UTC 时间、真实自增编号、多次创建与用户归属、其他表不变、提交前故障回滚及响应校验失败后 INSERT 回滚。当时累计 183 项离线和 38 组隔离 MySQL 测试通过；临时容器及测试数据已清理，不操作项目数据库。

## 查询会话列表业务：第二步

同一业务文件新增 `list_chat_sessions(current_user, session_factory) -> SessionListResponse`。调用方仍须先用 `get_current_user()` 验证身份，不从请求正文或查询参数构造所属用户。

1. 一条 SELECT 查询 chat_sessions，WHERE 强制限定当前用户的 user_id。
2. 数据库按 last_activity_at DESC、chat_session_id DESC 排序，最近活跃在前，同时间按数值编号倒序。不是按 updated_at 排序，所以未来单纯改名不会置顶。
3. `.all()` 读取此用户全部会话，不分页、不截断；逐条用 SessionResponse 整理为公开字段，再包装进 items。无数据返回 items=[]，不创建空会话。
4. 不读取 messages，不修改任何记录或时间，不调用模型。查询 Session 正常关闭，不进行写入提交。
5. 数据库查询/读取故障使用既有 SessionUnavailableError（SESSION_UNAVAILABLE），不伪装空列表、不自动重试。未来 HTTP 层映射为 503。

列表阶段新增 6 项离线和 4 组真实 MySQL 用例：验证只返回本人、无会话但别人有会话时仍为空、活动时间与编号排序、105 条全部返回、公开字段/UTC/大整数编号、单次查询只读会话表且全部表数据不变，以及查询故障。当时累计 189 项离线和 42 组真实 MySQL 验收通过，临时容器及数据已清理。不改项目数据库、表结构、Schema 或 HTTP 路由。

## 重命名会话业务：第三步

`rename_chat_session(current_user, session_id, request, session_factory)` 复用已认证的 UserResponse、现有 RenameSessionRequest 和 SessionResponse。标题由请求 Schema 去首尾空白后检查 1～100 字符；会话编号复用 format_database_id 校验，错误编号在查库前抛 ValueError，未来接口映射 422。

- 短事务中按 chat_session_id + 当前 user_id 查询并 SELECT FOR UPDATE 锁住记录；不存在或不属于本人统一 SessionNotFoundError（SESSION_NOT_FOUND，未来 404）。不返回其他用户数据。
- 只设置 title、title_is_manual=true、updated_at；即使标题没变也标记手动命名。created_at、last_activity_at、所属用户均不变，因此改名不置顶。
- 同会话并发改名由数据库行锁串行执行，后执行写入者的标题最终生效。不检查生成状态，允许 RUNNING 时改名；未来自动标题更新必须在同一会话行锁规则下尊重 title_is_manual，不能覆盖手动标题。
- flush 后验证响应，提交成功再返回。数据库故障沿用 SESSION_UNAVAILABLE，不自动重试、不泄露原始错误。响应校验错误也会使事务回滚，由未来统一异常处理器处理为安全的服务端错误。
- 无 HTTP 路由、Cookie、模型或消息业务改动，不新增字段或迁移。当前用户仍须来自调用方的身份验证，不能来自前端自报。

新增 8 项离线测试和 6 组真实 MySQL 验收：正常/同名/100 字符改名、非法标题/编号、他人或不存在会话、活动时间与其他数据不变、RUNNING 时允许改名、提交/响应故障回滚及并发改名。保留用户已将新建标题改为英文的代码，并同步该项测试和说明。累计 197 项离线、48 组隔离 MySQL 测试通过；临时容器和数据已清理，项目数据库未修改。会话管理三项业务至此完成，历史消息等消息业务另行实现。

## 读取历史消息业务：只读与运行状态协调

入口：`app/services/message_history.py` 的 `get_message_history(current_user, session_id, session_factory, registry)`。当前用户必须来自后端认证，registry 必须是应用生命周期共享的 GenerationRegistry，不提供默认空登记。尚未接 HTTP，也没有真实生成任务。

1. 校验编号，在当前会话短锁内新建数据库 Session；按会话编号和当前用户检查归属。不存在/不属于本人统一 SESSION_NOT_FOUND，拒绝后不查询消息。
2. 一次读取该会话全部 Message，按 created_at ASC、message_id ASC 排序；不跨会话加载，不分页、不截断、不写数据或修改活动时间。
3. 将 USER 的平铺生成字段组装为 generation；从当前会话的 ASSISTANT 引用建立成功回答编号。保留正文缩进；ASSISTANT 不输出 generation，内部模型标识、发送键和前驱编号不外露。
4. 只有会话无运行登记、问题是最后一条 USER、状态 FAILED 时 can_retry=true。旧问题的失败不会因新问题成功而消失。失败摘要只使用固定白名单（超时、中断、模型请求失败）；未知代码降级为 GENERATION_FAILED，数据库原始错误文字一律不直接返回。
5. 运行登记有当前 attempt_id 时 is_generating=true；即使已经保存 SUCCEEDED/FAILED、仍在清理，也保持忙碌。RUNNING 必须与最后问题和登记吻合；孤立 RUNNING、编号不匹配或问答配对矛盾，拒绝返回不一致的历史。
6. 数据库/快照校验/运行状态不一致统一 MessageHistoryUnavailableError（MESSAGE_HISTORY_UNAVAILABLE，未来 503）。不会返回假空列表、虚假空闲或擅自修复状态。读取本身不会清理重启残留 RUNNING；启动清理仍待生成服务阶段实现。

最小支撑位于 `app/services/generation_registry.py`：按会话提供同步短锁、claim(attempt_id)、release(attempt_id)；忙碌时拒绝第二次占用，旧编号不能释放新编号。无占用且无调用者/等待者的条目自动回收。它不是执行器，没有任务引用、取消或重启恢复实现。

未来发送、重试、最终保存和清理必须使用同一份 registry，并在对应短锁内完成状态切换与数据库提交；创建消息失败时撤销本次占用，提交结果不明时不得盲目释放。不能跨模型等待或 await 持锁；异步路由应将整个同步数据库业务函数放在线程池执行，不能在事件循环中直接等待 threading.Lock。仅支持单进程，实际运行接入前必须完成启动清理与应用生命周期装配。

新增 12 项历史/4 项登记离线测试、8 组真实 MySQL 验收，覆盖空历史、越权拒绝、完整问答关联、旧失败后新成功、清理期忙碌、残留状态拒绝、跨会话错误引用、错误脱敏、105 条完整读取、所有表只读及历史等待最终保存后取得一致快照。累计 213 项离线和 56 组隔离 MySQL 测试通过，临时容器/数据已清理；项目数据库及表结构未修改。

## 发送第一步：消息准入与保存

2026-09-26 新增 `app/services/message_submission.py`。`accept_user_message(current_user, session_id, request, session_factory, registry, *, check_new_message, input_policy)` 是必须用 with 的内部上下文管理器，不是公开 HTTP 接口或普通“发完即走”的函数。

### 进入时：接收、查重和保存

1. 调用方先认证；服务校验编号并复制校验后的 SendMessageRequest，保留原正文、缩进和换行，避免调用方之后修改请求影响清理。
2. 在共享 registry 的当前会话短锁内开事务，按会话编号+用户锁定 chat_sessions 行。不存在和越权统一 SESSION_NOT_FOUND。
3. 先按会话+client_message_key 查重：同键同正文返回现有 DuplicateMessageResponse，不新建记录、不调用准入检查、也不拥有原任务清理权；同键不同正文报 IDEMPOTENCY_CONFLICT。回执使用该 USER 当前 attempt_id/状态和真实关联回答编号，错误关联或未登记 RUNNING 不伪装正常。
4. 新消息遇到占用报 SESSION_BUSY；数据库存在无本地登记的 RUNNING 则报 MESSAGE_SEND_UNAVAILABLE，等待后续恢复，不覆盖旧记录。
5. 用后端 input_policy 组装输入并检查预算，再执行必填的 check_new_message 检查，然后生成 attempt_id、占用会话、保存一条 USER/RUNNING。预算检查使用下节的真实业务代码；check_new_message 由终端注入按用户生成限流，未来生产接口也不能注入空检查。这些步骤不允许网络或模型调用。
6. 首条 USER 且标题未手动修改时，用正文去首尾空白、换行替换为空格后的前 30 字符初始化标题；其他情况不覆盖标题。同一事务更新会话 updated_at 和 last_activity_at。提交成功后才交出 AcceptedMessage（会话/问题/执行编号，以及 prepared_input 输入快照），它是内部执行凭据，不新增 API 响应字段。

### 使用和退出：明确执行所有权

with 的主体位于数据库事务和短锁之外，正式执行器在这里执行并结算 Agent。不能把任务丢到后台后直接离开作用域，也不能在 async 事件循环中直接运行同步数据库/锁操作。执行器和终端取消已接入，HTTP/SSE 生命周期仍待实现。

正常离开或主体抛异常时，只处理本次拥有的 attempt_id：如果仍 RUNNING，保存 FAILED/GENERATION_INTERRUPTED；若已成功且回答存在，或已失败，则保留最终结果。确认提交后释放占用。重复回执退出不清理原任务；迟到的旧作用域不能释放新编号或改写新任务。

INSERT/提交异常时，不自动重发：在新事务重新锁父会话、查询本次键与编号。确认无记录才释放；已落库且未完成则标为中断，避免“提交成功但返回失败”留下无人执行的 RUNNING。核对/结算本身失败时保留占用并报 MESSAGE_SEND_UNAVAILABLE，不谎称空闲。当前没有数据库恢复后自动核对入口或启动清理；上线前必须补齐。即使异常后核对发现已提交，也不会自动执行模型。

### 测试与边界

新增 12 项离线测试、14 组隔离 MySQL 测试：保存/标题/活动时间、同键并发只存一次、忙碌时重复回执、正文冲突、拒绝不占新键、跨会话键隔离、越权拒绝、手动标题保留、作用域异常清理、已成功回复不覆盖、提交前回滚、提交后异常核对、清理失败保留占用及旧清理不能影响新执行。累计 225 项离线、70 组真实数据库测试通过；测试容器/数据已清理。

这一阶段没有 HTTP、SSE、真实模型调用、限流、失败重试或后台任务。输入组装与准入预算现已补上，详见下一节。测试中的成功回复直接写入临时库，不表示正式 ASSISTANT 保存业务已实现。不修改项目数据库或表结构，不提交/推送 Git。

## 发送第二步：组装 Agent 输入

2026-09-26 新增，简单说就是：**把 AI 回答前需要看的材料备齐，再判断放不放得下。这里还没有让 AI 开始回答。**

| 位置 | 职责 |
|---|---|
| `app/agent/input_policy.py` | 后端模型容量、应用预算、输出/工具等预留，以及明确标为估算的计数函数 |
| `app/services/agent_input.py` | 验证会话归属，读取当前用户记忆、挑选本会话成功问答，形成不可变输入快照 |
| `app/services/message_submission.py` | 保存新问题前调用上述组装；提交成功后把同一份快照交给未来执行器 |

### 输入是怎样整理的

1. 在消息准入的同一事务内确认会话属于当前用户。不接受前端指定别人的用户编号。
2. 从 `agent_memory` 按当前用户读取，整理成 `/memories/profile.md` 对应的文本快照；不创建磁盘文件，不保存第二份 Profile。正文是用户数据，不是系统指令，不能授权访问别人的数据。
3. 先计算系统提示、工具定义、完整当前问题和记忆的估算占用。必要内容已超预算则抛 `ContextTooLargeError`（未来 HTTP 层映射 `422 CONTEXT_TOO_LARGE`）；不保存 USER、不占用 key、不改标题或活动时间。
4. 从本会话最近的成功 USER 问题查找对应 ASSISTANT，逐轮加入预算。失败问题不入选；成功状态却缺少正确关联回答时报安全错误，不偷偷当作正常历史。
5. 下一整轮放不下就停止，不拆开问答、不跳过较近的大轮次。查询每批最多 50 轮，但可以继续读下一批，**50 不是总轮数限制，更没有 20 轮上限**。
6. 把选中的问答按旧到新排列，末尾完整加入当前问题一次。快照保留来源消息编号、选中轮数、是否因预算停止和估算方式，方便测试。未选中历史仍保留在数据库和页面。

`PreparedAgentInput.as_agent_messages()` 只转换为 LangChain 的 HumanMessage/AIMessage 列表，每次产生新对象；不会调用模型。系统提示在 `policy.system_prompt`，记忆在 `profile_memory`，不重复塞进聊天列表。新 USER 保存后不重新组装另一份上下文，因此不会再次把当前问题追加进去。重复请求只返回原回执，不重新读记忆或计算新预算。

### 当前预算及明确限制

本地配置型号是 `deepseek-v4-pro`。[DeepSeek 官方模型元数据文档](https://api-docs.deepseek.com/api/list-models/)于 2026-09-26 核对：上下文 1,048,576，最大输出 393,216 tokens。代码仅登记这个已核对型号，换型号必须新增经过确认的配置，不猜测容量；这些是静态配置，不会自动随供应商变化。

为了控制第一版成本，应用层默认总预算为 65,536，小于供应商上限；其中预留输出 4,096、Deep Agents 框架提示/包装 8,192、后续工具结果 4,096、安全余量 4,096，剩下 45,056 用于当前问题、记忆、已知系统提示/工具和成功历史。可通过后端 `AgentInputPolicy` 显式调整，不能由前端任意指定；不会修改你的 `.env` 或模型配置。

计数采用 **UTF-8 字节数 + 消息/外层开销的保守估算**，不是字符数，也不是 DeepSeek tokenizer 的精确结果或计费数字。中文、英文和 emoji 占用不同。它可能比实际 token 数大，从而更早舍弃旧历史；不是对供应商最终编码的数学保证。

本节输入组装完成时尚未创建正式 Deep Agent。2026-10-09 已补上下节的 Agent 工厂、只读记忆后端和最终请求预算保护；当前正式工具清单仍为空，未来启用工具时需要同步调整实际工具定义及预算计算。框架预留不是已经测量过的默认工具开销。**保守估算不等于供应商精确 tokenizer，也不能保证供应商最终编码永远不超窗。** 不向用户开放默认文件工具/子 Agent，不启用自动历史摘要。

当前输入组装已供新发送和失败重试共用。重试从原 USER 取问题，通过 retry_message_id 排除该问题自身，再完整加入一次；不把重试伪装成新消息。正式 Agent 与最终回复保存已接上；终端已接单进程生成限流和独立记忆阶段，HTTP/SSE 仍未实现。

### 验证

新增 19 项离线测试、8 组真实 MySQL 测试。验证 60 轮成功问答无固定轮数限制、相同时间戳跨批排序、逐整轮截取、当前问题原文只出现一次、失败问题排除、用户/会话隔离、记忆快照不被后续更新改变、超预算不落库且同 key 可再次提交、重复请求不重新预算及错误脱敏。

全量 244 项离线测试、78 组隔离 MySQL 8.4.11 测试及迁移回退再升级通过。只使用一次性测试库，测试容器和数据已清理；没有调用真实模型、消耗 API 余额或修改项目数据库。

## 创建正式聊天 Agent

2026-10-09 完成创建模块及离线执行验证。本次新增文件按实际编写顺序：

1. `app/agent/memory_backend.py`：先实现 `ProfileMemoryBackend`，继承 Deep Agents 的 `BackendProtocol`。把准备好的记忆文本作为只读虚拟 `/memories/profile.md` 提供给框架；没有实际磁盘文件、数据库连接或命令执行能力。其他文件路径拒绝读取，write/edit/delete/upload 全部拒绝；异步接口复用同样限制。每个实例只持有本次用户的不可变快照。
2. `app/agent/budget_middleware.py`：再实现 `ChatMemoryBudgetMiddleware`，继承框架的 `MemoryMiddleware`。使用同名替换机制，让框架先加载快照、加入 system message，再检查最终请求。每次运行都重新从绑定快照加载，不接受调用参数夹带的 `memory_contents` 覆盖用户记忆。另用无操作的 `NoAutomaticSummary` 同名替换主 Agent 的自动摘要中间件，不调用额外摘要模型。
3. `app/agent/factory.py`：最后用 `build_chat_agent(settings, prepared_input)` 组装前两项。校验配置型号与预算型号一致、容量未超已核对上限，然后创建 `ChatDeepSeek`，再调用真正的 `create_deep_agent`。不读取 `.env`，配置由后端调用方传入；不重复查询数据库、重新组装消息或创建新的用户问题。
4. `tests/test_agent_memory_backend.py`、`tests/test_agent_budget_middleware.py`、`tests/test_chat_agent_factory.py`：分别验证只读后端、中间件、工厂及真实框架接线，共新增 23 项测试。

### 正式工厂与旧探针的区别

`check.py` 仍是单独运行并产生费用的无思考连通性探针，支持下文的可选 LangSmith 跟踪。正式工厂不使用探针的固定问题、128 token 输出上限或终端打印。2026-10-11 正式工厂启用 thinking、reasoning_effort=low；`prepared_input.policy.output_tokens`（默认 4096）由思考与最终正文共同使用。SDK 超时 60 秒、自动重试为 0；执行器另有默认 120 秒总超时。思考占满额度且最终 finish_reason=length 时记录失败，不把半截文字保存成成功答案。

`build_chat_agent` 返回编译好的 LangGraph 图，不是回复。每次生成创建独立 Agent，不将带用户记忆的对象全局缓存。`checkpointer=None`、`store=None`，不持久化中途执行；长期记忆的权威来源仍是 MySQL，本次只消费输入组装阶段读取的快照。

### 工具和容量保护

本工厂负责“只读已有记忆并回答”，没有写入工具；写入已在回答前的独立记忆阶段实现。回答阶段的框架 memory 能力会读取后端并将记忆作为受限数据注入提示；不是仅由项目把 Profile 手工拼接到用户问题。[官方 Memory 说明](https://docs.langchain.com/oss/python/deepagents/memory)

`tools=[]`、`subagents=[]` 本身不足以关闭所有框架默认工具。实际保护由中间件实施：交给模型前清空工具清单；模型意外返回工具调用时报安全错误；工具执行入口也统一拒绝，包括 `task` 委派。框架内部仍可能构建默认子 Agent/工具节点，但没有允许执行它们的路径。另用 `ModelCallLimitMiddleware(run_limit=1, exit_behavior="error")` 限制本次回答的模型调用次数；将来加入保存记忆工具时必须显式调整这些限制并补测试，不能直接挂工具就宣称可用。

最终预算按实际 system message（含框架提示和记忆）、消息和空工具清单的序列化内容做 UTF-8 保守估算。框架内容已算入，不再重复扣初始的 framework_reserve；仍扣除输出、工具结果预留和安全余量。超限在调用模型前抛 `ContextTooLargeError`，不自动截断或摘要。生成已被接受之后遇到此错误，未来执行器应把它当作生成失败结算，不能伪装成从未接收过问题。未来启用工具时必须扩展为计入实际工具 schema 和执行结果。

### 已验证和未完成

267 项离线测试通过。新增测试验证：真实 `create_deep_agent` 编译成功；原生 MemoryMiddleware 读取快照；不同用户隔离；记忆正文只注入一次；超预算不调用模型/摘要；模型意外要求 task 时不执行；真实 DeepSeek 适配器接收正确配置（供应商调用已替换）；异步 astream 收到本地假模型的逐段文字。测试禁止网络连接并关闭 LangSmith 跟踪。

上述工厂开发没有调用真实 DeepSeek，没有消耗 API 余额，没有改项目数据库。工厂创建期间关闭 LangSmith，但该上下文不会自动覆盖执行阶段。2026-10-11 的 `chat_execution` 已把工厂接到消息准入作用域，并用 `async_agent_tracing` 包住整个流消费；此入口处理执行、取消与最终保存，仍未接 HTTP/SSE。单独进入准入作用域却不执行，退出时仍会按原规则将未完成问题标为中断。

## 正式聊天执行器：从问题到保存回答

2026-10-11 完成这一块后端能力。入口为 `app/services/chat_execution.py` 的 `execute_chat_turn`。它是内部异步业务函数，不是命令行程序，也不是新增公开 API。调用方必须先认证，传入应用共享的 `GenerationRegistry`、Session factory、模型与 tracing 配置、输入预算、真实限流检查和异步事件消费者。终端入口已注入 `GenerationRateLimiter`；未来 HTTP 接口还需账号/IP 防护等措施，不能把测试替身挂成生产接口。

### 编写顺序及文件作用

1. 新增 `app/core/async_work.py`：用 `asyncio.to_thread` 执行短数据库操作；取消时通过 shield 等待在途操作结束，避免释放会话后线程还在提交。
2. 修改 `app/agent/tracing.py`：补异步 tracing 上下文；上下文在事件循环设置，Client 收尾在线程完成。修改工厂开启正式聊天思考模式。
3. 扩充 `app/schemas/stream.py`：定义进度与思考事件；新增 `app/agent/output.py`，分离供应商推理字段与最终回答，过滤非主模型消息、未知内容块及工具调用，并管理流关闭。
4. 新增 `app/services/generation_result.py`：只负责短数据库事务，核对用户/会话/当前 attempt/RUNNING，保存 ASSISTANT 与 SUCCEEDED，或写安全 FAILED 摘要；最终状态不被覆盖。
5. 新增 `app/services/chat_execution.py`：把已有准入、输入快照、工厂、流处理及结果保存串在一起。选择完整运行的协程 + `on_event` 回调，而非可能被调用者遗弃的裸异步生成器。
6. 新增三份离线测试 `test_agent_output.py`、`test_generation_result.py`、`test_chat_execution.py`；扩充工厂和 tracing 测试；新增 `scripts/chat_execution_mysql_cases.py` 并接入现有隔离 MySQL 验收脚本。

### 一次执行的顺序

1. 在线程中进入已有 `accept_user_message`：验证归属、查重、检查预算/额度、准备历史和记忆快照、提交 USER/RUNNING。重复请求返回回执，不创建 Agent、不发送新流。
2. 发 `message_start`、`agent_progress: context_ready`。此时数据库事务和短锁已经结束，不会一直持锁等待模型。
3. 先执行独立记忆阶段，发记忆检查/保存/跳过等真实进度；内部模型文字和工具参数不显示。再创建正式回答 Deep Agent，发 `agent_running`，开始 `agent.astream`。通过 `metadata["langgraph_node"]` 只接收回答模型文字。模型可用 API 推理字段不是完整内部状态，不能把任意节点内容都展示给用户。
4. 收到推理就发 `reasoning_delta`；收到正文就发 `message_delta`。首次收到对应片段时分别发 `thinking`、`answering` 进度。没有推理就不伪造思考文字；当前没有搜索/工具执行，也不会谎称搜索中。
5. 等整个图正常退出，并验证 finish_reason=stop、正文非空。发 `saving`，在同一事务中保存完整 ASSISTANT 和 USER 的 SUCCEEDED。
6. 关闭执行资源、核对数据库并释放仍属于本次 attempt 的会话占用，最后发 `message_done`。模型失败则保存 FAILED 并发 `message_error`。断线消费者抛错或调用方取消时，完成清理再传播异常，不继续向断开的客户端发事件。

`on_event` 可以先让测试收集对象，未来也可以让 SSE 层序列化并发送到浏览器。当前只产生结构化事件，不等于已经创建 HTTP 流。

### 思考文字放在哪里

思考文字与工作进度都只用于本次事件流，不写进 `messages.content`，不进入下轮上下文，刷新后不恢复；数据库不增加列或迁移。正式回答仍持久化。启用 LangSmith 时，模型提示、记忆、推理和回答可能另外上传到 tracing 服务，不应把“不入聊天表”误解为“绝对不在任何地方留存”。

回答阶段仍无工具，不回传旧推理；记忆阶段单独关闭思考且不带历史助手消息，工具输出不进入回答图。这样不依赖保存历史 reasoning_content，详见“两阶段执行”；不能直接在原回答图上解除工具拦截。[DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)

### 安全边界与验证

- 只有当前占用与数据库 attempt 都匹配、且状态为 RUNNING，才能首次保存结果；FAILED 的迟到回答拒绝，已成功提交不再重复插入。
- 保存事务返回异常时，用新事务锁定父会话核对：已成功则返回成功；未提交则记录失败；数据库无法核对则保留占用并报安全业务异常，不谎称空闲。启动核对已接入；运行中长期数据库故障的自动修复仍未实现，应停止旧进程后再启动核对。
- 默认 120 秒从准入成功后开始，包含模型、事件消费及结果保存；同步事务取消不等于线程停止，因此必须等待实际事务与清理结束，总耗时可能超过 120 秒。
- 事件消费者应快速接收并遵守背压，不能无限缓存；未来 HTTP 层需在断线时取消执行协程。最终事件发送失败不回滚已保存回答，重连后查历史确认。
- 303 项离线测试通过；90 组真实 MySQL 8.4.11 验收通过，新增 12 组涵盖两轮上下文、思考不入库、重复/并发/隔离、超时取消、提交前后故障、取消发生在 USER/ASSISTANT 提交期间及迟到结果拒绝。MySQL 测试使用真实 Deep Agents 图，供应商网络部分由假流替换。
- 没有调用真实 DeepSeek 或 LangSmith，没有消耗真实 API 余额，没有触碰项目数据库。只创建并清理了一次性测试容器。未提交或推送 Git。

可复查测试（在 backend 目录）：

```bash
uv run python -m unittest discover -s tests -q
# 下面会创建并清理专用临时 MySQL 容器，要求 Docker 已启动和已有 mysql:8.4 镜像。
uv run python scripts/check_migration_mysql.py
```

后续已补记忆存取、独立记忆阶段、失败重试业务及启动清理，仍需补 HTTP 账号/IP 限流，以及 HTTP/SSE 和聊天网页。终端已接单进程生成限流，但尚无 `/retry` 命令。实际供应商的记忆提取、正式思考流与 LangSmith 上传仍需手动联调，不能把本地假流测试当成已验证真实云端效果。

## 个人记忆保存与查询业务

本节记录先前完成的记忆存取服务；后续已接入 Agent 记忆阶段与 `/memory`，见下一节。服务层本身不调用模型，不新增表、迁移或 HTTP 路由。

### 按编写顺序学习

1. `app/services/profile_memory.py` 的 `ProfileFact`：定义后端内部的存储参数，每条包含 `memory_key`（主题）和 `summary`（完整摘要）。复用表模型的 25 个主题，摘要去掉首尾空白后必须为 1～500 字符。拒绝额外传入 user_id、memory_type、attempt_id；类型由主题推导，用户和生成编号从后端准入结果获得。这不是新增公开 API Schema。
2. 同文件的 `list_profile_memory`：接收已通过认证的 `current_user`，只查询该用户的 `agent_memory`，按 updated_at、memory_id 倒序排列，再用已有 `MemoryListResponse` 返回公开字段。没有记忆返回 `items=[]`；数据库失败不能假装空列表。以后 `GET /api/v1/me/memory` 调用它，但目前还没有该路由。
3. 同文件的 `save_profile_facts`：接收后端的 `AcceptedMessage` 和已审核摘要，检查本地运行登记、会话归属、原问题及数据库 attempt/status；只有当前 RUNNING 尝试且尚无最终回答才能写。使用 MySQL 的 `INSERT ... ON DUPLICATE KEY UPDATE`：同用户同主题已有记录就更新，没有就插入。只更新本次涉及的主题，不拿一整份旧 Profile 覆盖所有内容。
4. `services/errors.py` 增加 `MemoryUnavailableError`：查询或保存不能确认时返回安全错误，不暴露原始 SQL、记忆或凭据。增加离线测试和 `scripts/profile_memory_mysql_cases.py`，后者加入现有隔离 MySQL 验收。

例如，调用方已经按规则整理出一条记忆时，内部参数长这样：

```python
ProfileFact(memory_key="preference.language", summary="喜欢用中文解释")
```

保存函数从可信执行上下文取得 user_id，自己推导 memory_type 为 LEARNING_PREFERENCE。如果这个用户已经有该主题，就更新同一条记录，保留 memory_id 和 created_at；不会因重复调用多出一条，不会改用户昵称或聊天消息。一次批次 1～25 条且不能重复主题，全批次在同一个短事务中提交，得到提交确认后才返回本次涉及的记忆。

### 存储完成不等于自动记忆完成

这里的摘要必须是调用方已经审核、合并后的完整主题值。例如原来是“使用 Git”，用户补充 Docker 时，调用方应该提交“使用 Git 和 Docker”，不能只提交“使用 Docker”导致旧事实丢失。存储层不调用 LLM，无法自行判断自然语言是补充、纠正、临时要求还是敏感信息，也不声称实现了语义过滤。

后续接入的记忆阶段通过提取提示、原话引用校验和保守敏感规则落实限制，仍不能把这些措施等同于完备的语义/隐私分类器。只开放受控的记忆写入工具，不能直接把此服务作为公开写接口。正式回答工厂仍禁止工具调用；记忆阶段的独立工具预算和调用限制见下一节。

### 事务、并发和失效保护

- 同用户不同会话写不同主题互不覆盖；同主题并发按数据库提交顺序后写生效，不做版本合并或证据累计。主题按固定顺序写入，减少多主题并发死锁风险；数据库错误不自动重试。
- 本地会话锁、父会话行锁和问题行锁一直覆盖到短事务结束，不在锁内等待模型。服务不释放生成占用，也不改变问题状态；释放和结算仍由执行器负责。
- 已失败、已完成、被新 attempt 替代或不再占用会话的旧调用不能写入。未来异步工具必须通过现有线程适配等待在途数据库操作收尾，取消后不能再启动新写入；本次不声称已完成工具取消集成。
- 提交前失败则整批回滚；提交确认丢失则可能已经落库，函数报“无法确认”，不谎报成功或保证原值未变。已提交记忆不会因之后的回答失败而回滚。
- 新会话继续使用现有输入组装逻辑读取同一份 MySQL 记忆；不新建磁盘 Profile 文件，也不把其他会话的全部聊天记录当长期记忆。

只运行新增离线测试：

```bash
cd /Users/roey/projects/AI-teaching-agent/backend
uv run python -m unittest discover -s tests -p 'test_profile_memory.py' -v
```

真实数据库验收仍用 `uv run python scripts/check_migration_mysql.py`，只操作脚本创建的临时容器。覆盖新增/更新/不重复、用户隔离、稳定排序、不同主题和同主题并发、跨会话读取、整批回滚、提交确认丢失、失效尝试拒绝。本次新增 14 项离线测试、9 项 MySQL 测试；全套共 338 项离线测试和 104 项 MySQL 测试通过，临时容器及其测试数据已清理。测试不调用模型、不修改项目库。

## Agent 自动记忆：两阶段执行

### 自己怎样试

仍用 `uv run python -m app.cli.chat`。登录后逐行输入：

```text
请记住：以后回答我时优先使用中文，并先讲用途，再解释代码。
/memory
/new
Explain a Python dictionary.
/memory
```

看 `[Progress] Profile memory save confirmed.` 和 `/memory` 的实际条目来确认保存，不只相信 AI 回答中的“记住了”。新会话应能读取同一用户的已有偏好；是否遵循偏好仍受模型输出影响。若没有条目，查看进度是跳过还是无法确认；可以用更明确的长期偏好再试，不要用密码或其他敏感资料测试。

本步骤没有自动访问你的项目数据库、真实模型或 LangSmith；下面描述的是接线与本地测试结果，真实模型提取质量仍需这次手动联调。

### 新文件及接入顺序

1. `app/agent/profile_memory_tool.py`：定义工具输入（主题、合并后的摘要、当前消息中的原话引用）、提取提示与敏感规则，创建只属于本次执行的 `BoundMemoryTool`。模型只见 `facts`，看不到也不能提供 user_id、Session factory、registry 或 attempt_id。工具通过已有 `save_profile_facts` 服务提交；确认成功才记录 saved。
2. `app/agent/memory_workflow.py`：用 `create_deep_agent` 建立一次短的记忆阶段。模型关闭思考，只收到当前用户原文和用户已有 Profile；不收到其他会话聊天或原来助手消息。中间件只允许 `save_profile_facts`，拒绝其他工具与多次工具调用。工具使用 `return_direct=True`：工具完成就结束这张图，不再调用模型生成一段确认话。
3. `app/services/chat_execution.py`：准入完成后，先运行记忆阶段，再运行已有正式回答 Agent；两个阶段使用同一个 attempt/总超时及 tracing 范围。确认保存后读回最新 Profile，传给回答阶段；只把最终回答写进 messages，不把工具参数、内部判断文字、思考或来源引用保存成聊天正文。
4. `app/schemas/stream.py` 与终端显示：增加 memory_checking / memory_saving / memory_saved / memory_skipped / memory_unavailable 实际进度；CLI 加 `/memory` 调用已有查询服务。这不是新增 HTTP API。

### 为什么拆开

[DeepSeek 官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)对携带工具的思考请求规定了推理内容回传要求。现有历史只保存正式回答，没有保存历史推理和工具轨迹，因此本版不在原聊天图里简单解除工具限制，也不伪造旧 reasoning_content。

记忆阶段关闭思考、没有旧 assistant 消息，最多一次模型调用、一次写入批次；之后正式回答阶段保持思考开启、工具清单为空，继续按历史上下文回答。两个阶段都是 Deep Agents 图，不是开放默认子 Agent 委派，也不新增模型供应商或依赖。记忆模型输出最多 2048 tokens、SDK 超时 20 秒、阶段超时 25 秒；回答保留 4096 输出额度和 SDK 60 秒超时。整体仍受 120 秒执行期限约束；在途数据库事务清理可能使实际退出稍晚。

### 记忆规则与边界

- 提取提示要求只记明确、持续的本人事实；不把临时指令、示例、引用、别人的信息或问过的知识当个人记忆。模型需读旧摘要，保留仍有效的事实，并合并补充/处理明确纠正。原话引用必须确实存在于当前问题，否则整批拒绝。引用匹配只证明文本出现过，不证明模型的语义理解正确。
- 25 个主题来自共同允许列表；每条摘要最多 500 字符、原话引用最多 1000 字符，每批最多 25 个不同主题。模型不能指定用户、分类或生成身份。敏感关键词/号码模式在原问题及候选摘要上做保守拦截；命中原问题时不调用记忆模型。可能误拒，也可能漏检，不承诺自动识别全部敏感语义；公开给真实用户前仍需隐私告知、控制/删除与更全面评估。
- 聊天原文仍按原有规则入 messages，回答模型仍读取原问题。跳过长期记忆不等于聊天原文被脱敏；LangSmith 开启时也可能另存请求和工具轨迹。
- 文件写入、执行命令、搜索和 `task` 等框架工具仍拒绝；不挂载宿主机目录，不开放其他用户数据。
- 无需记忆就跳过；记忆模型超时、输入超预算或保存无法确认时，不自动重试，给回答阶段一个后端生成的状态提示，尽量继续正常回答。事件消费者断开和执行取消不能被吞成普通记忆错误。
- 取消不启动后续写入；已开始的短事务等结果确定再退出生成作用域。记忆先提交而后续回答失败，记忆保留。确认丢失时不声称一定保存或一定回滚。若保存成功但读回失败，回答可用旧快照继续，`/memory` 或下一次提问再读真实记录。
- 记忆请求预算计算实际 system、消息和工具定义；答案仍检查最终实际请求。记忆增长时重新核算输入，从最旧的完整问答开始移除以腾出空间，不截断当前问题，也不删除数据库历史。

### 测试

`tests/test_memory_workflow.py` 使用真实框架和模拟模型；`scripts/memory_workflow_mysql_cases.py` 使用临时 MySQL，验证工具真正写入、跨会话读取、更新不重复、用户隔离、记忆失败后仍回答、回答失败保留已存记忆、重复请求不增加模型调用，以及取消等待在途记忆事务。终端测试验证 `/memory` 先检查身份且不调用模型。

本阶段新增 17 项离线测试、9 项 MySQL 测试；完整 355 项离线测试、113 项隔离 MySQL 测试通过，迁移重复执行及回退再升级验证通过。临时容器和一次性测试数据已清理，没有改动项目数据库、调用真实模型或上传 LangSmith。实际模型是否正确理解补充/纠正仍需手动检查，模拟模型测试不能替代语义质量评估。

```bash
uv run python -m unittest discover -s tests -p 'test_memory_workflow.py' -v
uv run python -m unittest discover -s tests -q
uv run python scripts/check_migration_mysql.py
```

## 失败重试业务

### 这一步做什么、不做什么

同一个用户问题没有成功得到完整回答时，允许重新执行 Agent。原问题不重复插入，成功回答不能“重新生成”，较早的失败问题也不能重试。本步骤不增加表、迁移、依赖或 API 字段，也没有接 HTTP 路由、网页按钮或终端 `/retry`。

用户问题编号始终相同，变化的是执行编号，例如：

```text
message_id=42，attempt_id=A：FAILED
请求重试：message_id=42，failed_attempt_id=A
接受后：message_id=42，attempt_id=B，retry_of_attempt_id=A，RUNNING
成功后：问题 42 为 SUCCEEDED，另保存一条指向问题 42 的 ASSISTANT 回答
```

此时相同的 `failed_attempt_id=A` 请求重复到达，只返回 B 的当前回执，不再调用模型。如果 B 也失败，刷新状态后使用 `failed_attempt_id=B` 才是下一次有意重试。只保留最近一跳的关系，不恢复所有旧请求的历史回执。

### 建议按这个顺序读文件

1. `app/services/message_retry.py`：新建的重试准入服务 `accept_failed_message_retry`。先检查会话归属、消息和最近一跳的重复请求；再检查编号、忙碌、残留 RUNNING、最后一个问题及 FAILED 状态。读取数据库里的原问题，组装输入并执行必填限流检查；在短事务内换新 attempt_id、清空错误、更新活动时间，不改变原文、发送键、创建时间或标题。事务提交、短锁释放后才 `yield` 执行凭据。
2. `app/services/agent_input.py`：修改已有输入组装函数，增加内部参数 `retry_message_id`，显式排除原问题的历史行；仍只选预算内的成功问答，最后加原问题一次，同时加载本人最新记忆。
3. `app/services/chat_execution.py`：新增业务入口 `execute_chat_retry`，将共用部分抽为 `_execute_with_scope`。新发送和重试只在“接收请求”时不同，后面共用记忆阶段、回答阶段、思考/进度事件、120 秒总超时、最终保存和取消清理。重复回执不进入两阶段、不发送另一套流。
4. `app/services/message_submission.py`：扩展已有收尾检查，允许核对重试 UPDATE 失败的两种结果：确实回滚则保留旧 FAILED；已经提交则将未完成的新尝试标记为中断。核对失败时保留忙碌，不能假装可重试。旧尝试结果/记忆写入仍须通过现有运行编号保护，不能影响新尝试。
5. `app/services/errors.py`：增加 `MESSAGE_NOT_FOUND`、`RETRY_NOT_ALLOWED`、`STALE_GENERATION` 三种安全业务错误；HTTP 状态与统一错误响应留给后续接口层。

`execute_chat_retry` 的调用方必须传已认证用户、应用共享的 registry、Session 工厂、模型/跟踪配置、事件消费者和 `check_retry`。新发送的 `check_new_message` 与 `check_retry` 必须调用同一份 `GenerationRateLimiter.check(user_id)`；真实新生成才计数，重复回执不消耗额度。不能传空限流函数充当生产实现。

所有同步数据库和短锁操作由共享执行器放在线程中处理。取消发生在提交期间时先等该短事务完成，再核对并清理；模型运行时不持有数据库事务或短锁。仍仅支持单进程；启动清理已接入，运行中故障的自动恢复仍待实现。

### 如何验证

- `tests/test_message_retry.py`：离线测试准入规则、安全错误和重复请求不创建模型。
- `scripts/message_retry_mysql_cases.py`：临时 MySQL 与真实 Deep Agents 图，供应商响应使用替身；验证成功/失败、连续重试、防重复和同时准入、用户/会话隔离、旧结果/旧记忆写入拒绝、原问题只进入上下文一次、记忆更新不重复、超时/取消、提交前回滚与提交后确认丢失。
- `scripts/check_migration_mysql.py`：接入上述真实数据库测试，创建并清理独立临时容器，不使用项目数据库；不调用真实模型、不上传 LangSmith。

本步骤新增 12 项离线测试和 15 项隔离 MySQL 测试；完整 367 项离线、128 项真实 MySQL 测试通过，重复迁移及回退再升级验收通过。测试容器与一次性数据已清理，项目数据库未改动。真实模型效果与 HTTP/网页端到端验收不包含在这些测试中。

从 `backend/` 运行：

```bash
uv run python -m unittest discover -s tests -p 'test_message_retry.py' -v
uv run python -m unittest discover -s tests -q
uv run python scripts/check_migration_mysql.py
```

最后一条需要 Docker 运行，包含真实建表、约束和业务测试；删除的仅是脚本本次创建的临时容器与一次性测试数据。

## 启动清理：避免永久显示正在生成

### 用人话理解

假设 Agent 回答到一半，后端被强行关闭：模型任务已经没了，但数据库里的问题仍可能是 `RUNNING`。启动清理不是删聊天，而是在下一次启动、开始处理请求之前核对这些旧状态。

- 没有保存最终 ASSISTANT 回答：原 USER 改为 `FAILED / GENERATION_INTERRUPTED`，可以按既有规则重试最后一个失败问题。
- 已存在同会话、正确关联且非空的最终 ASSISTANT 回答：保留回答，修正 USER 为 `SUCCEEDED`，清空错误。
- 原本是 `SUCCEEDED` 或 `FAILED`：不修改。
- 回答跨会话、空白、编号不合法等异常：停止启动，不猜测结果。

清理只更新残留 USER 的生成状态、错误和 `updated_at`。不删除记录，不调用模型，不改变问题正文、创建时间、执行编号、前驱编号、标题或会话活跃排序，也不改变登录和长期记忆。不恢复中途 checkpoint，不自动重试。

### 文件和方法

1. `app/core/runtime_lock.py`：`RuntimeLock` 使用 macOS/Linux 的操作系统文件锁，位于 `backend/.runtime/chat.lock`。它表示“这个项目已经有一个聊天进程在运行”。锁从启动前一直持有到正常退出，进程崩溃时由操作系统释放。锁文件内容为空，不是数据库、密码文件或聊天记录；目录已加入 `.gitignore`。文件保留是正常现象，不能通过删除运行中的锁文件强行启动第二个实例。
2. `app/db/readiness.py`：把原终端里的只读数据库检查移到共用位置，检查 MySQL 8.4+、迁移头和五张表存在，不运行迁移。终端原导入名仍可使用。
3. `app/services/startup_cleanup.py`：`reconcile_interrupted_generations` 执行实际核对。要求进程锁已持有、共享 GenerationRegistry 空闲；扫描残留 RUNNING，按会话顺序锁父行、再核对问题与回答。在同一事务中提交，避免遇到坏关联时留下半批修改。数据库提交返回成功后才报告修复数量。
4. `app/core/runtime.py`：`open_backend_runtime` 把进程锁、数据库检查、清理、Session 工厂、共享运行登记和限流器组装起来，再将这些交给服务。它不是新的 Agent 执行器。
5. `app/main.py`：通过 FastAPI 的 `lifespan`（应用启动和关闭时执行的代码）调用上述运行环境；完成清理后才接受请求。同步数据库检查放在线程中，取消时等待该线程结束后释放资源。`app.state.runtime` 留给后续接口复用共享依赖。仅 import 模块不会读取 `.env`、创建锁文件或访问数据库。
6. `app/cli/chat.py`：用户确认 `y` 之后进入同一个运行环境；清理成功才登录和聊天，退出聊天及注销登录后才释放锁。终端拒绝确认不会触发清理。

### 安全边界和启动方式变化

- 首次使用新启动逻辑前，必须先停止所有旧版本聊天进程。旧程序没有取得这把锁，不能靠新锁证明它已经退出。
- 文件锁只协调同一主机、同一项目目录/锁路径的入口。不同目录副本、其他机器、多个容器或绕过启动入口的程序不在保护范围内；禁止它们同时操作同一聊天库。不使用多个 Uvicorn worker，不关闭 lifespan。`--reload` 开发重启也必须等旧子进程退出。
- 不能只凭一份新建空 registry 判定旧任务不存在。进程锁负责同项目进程互斥；registry 的启动清理屏障另外拒绝本进程已有任务/等待者，并阻止清理期间的新准入。
- 锁文件是本地运行文件，不加入 Git；不存放在同步盘，不在运行中删除、替换或移动。
- **FastAPI 现在启动就需要数据库连接和当前迁移版本**。先准备好 MySQL、迁移和 `backend/.env` 的 `DB_*`，再运行 Uvicorn。这里不会调用模型，也不要求仅为 `/health` 加载模型密钥。
- 启动数据库故障、清理异常或提交结果不能确认时，进程不进入服务阶段，不自动重试或假称数据库未变。提交确认丢失时可能已完成修改；停止进程、恢复数据库后再次启动会重新核对，不会重复调用模型。
- 第一版数据量少，采用一次事务核对全部残留；数据规模扩大后另行设计分批启动修复。
- 这不是运行期间的扫描器；当前进程中发生长期数据库故障时先停止并等待执行结束，再重启核对，不能在有存活任务时手动运行清理。

命令不变：终端聊天用 `uv run python -m app.cli.chat`；网页后端用 `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`，两者选一个运行。成功时显示/记录 interrupted 与 restored successful 的数量，日志不包含问题、答案或凭据。

### 验证范围

`tests/test_startup_cleanup.py` 验证状态规则、进程互斥、子进程崩溃释放锁、启动失败/取消和 FastAPI 生命周期；`scripts/startup_cleanup_mysql_cases.py` 在临时 MySQL 验证真实事务、回滚、提交后确认丢失、状态恢复、重复启动无变化、活任务拒绝、清理后失败重试和 HTTP 启动顺序。终端测试还验证确认前不清理、失败不进入登录。

本次新增 18 项离线测试和 10 项真实 MySQL 测试；完整 385 项离线测试、138 项隔离 MySQL 测试通过，重复迁移及回退再升级通过，临时容器及测试数据已清理。自动验收只使用临时容器及测试模型替身，没有启动你的项目服务、修改项目数据库或发送真实模型/LangSmith 请求。

从 `backend/` 运行：

```bash
uv run python -m unittest discover -s tests -p 'test_startup_cleanup.py' -v
uv run python -m unittest discover -s tests -q
uv run python scripts/check_migration_mysql.py
```

## LangSmith 执行跟踪（可选）

LangSmith 之前已作为间接依赖安装，代码也曾导入 `tracing_context`，但当时明确关闭上传。现在把它列为直接依赖，并加入配置开关；默认仍关闭，填写配置后才启用。不需要给每个函数加装饰器，LangGraph 和 LangChain 自带的回调可以记录执行过程。[官方说明](https://docs.langchain.com/langsmith/trace-with-langgraph)

### 添加和修改的位置

| 文件 | 作用 |
|---|---|
| `app/core/config.py` 的 `TracingSettings` | 从后端 `.env` 读取开关、LangSmith 密钥、项目名和服务地址；不上传 |
| `app/agent/tracing.py` 的 `agent_tracing` | 导入 `Client`、`tracing_context`；明确把配置交给 SDK，并在执行期间开启跟踪，结束后给上传队列收尾 |
| `app/agent/check.py` 的 `main` | 将终端探针的整个异步回答过程放进跟踪范围 |
| `langsmith.env.example` | 无密钥的配置模板，供追加到已有 `.env` |
| `tests/test_agent_tracing.py` | 假 Client 离线测试，不上传真实 trace |

Pydantic 读取 `.env` 不等于把配置写进全局环境变量，所以这里显式创建 `Client` 并传入配置，而不是仅添加一行 import。不要给带密钥的 settings 对象加 `@traceable`。

### 如何启用并查看

1. 在自己的 LangSmith 账号中创建 API Key。它不是 DeepSeek API Key，不要发到聊天中。
2. 参考 `backend/langsmith.env.example`，把下列配置追加到 `backend/.env`，保留原有 `DB_*`、`MODEL_*`；已有同名配置时修改原行，不重复追加：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=填写自己的LangSmith密钥
LANGSMITH_PROJECT=ai-teaching-agent-dev
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_WORKSPACE_ID=
```

地址必须匹配账号区域。本项目当前配置校验支持 US 默认地址及 EU 地址 `https://eu.api.smith.langchain.com`；其他区域需先扩展校验，不能直接填 US 地址替代。仅当 Key 需要指定工作区时填写 `LANGSMITH_WORKSPACE_ID`。

3. 在 `backend` 目录执行：

```bash
uv sync --locked
uv run python -m app.agent.check
```

这会调用一次真实模型并可能产生费用。进入 LangSmith 对应工作区的 `ai-teaching-agent-dev` 项目查看新 trace，可检查图节点、模型输入输出、耗时及错误；实际显示内容取决于执行步骤和供应商返回的数据。终端显示 tracing 开启只代表代码已启用，不证明上传成功；没有记录时检查 Key、项目、工作区、区域和网络。

**隐私提醒：开启后，对话、系统提示及提示中的个人记忆可能上传到 LangSmith。** 当前探针只发送固定问题，不读取数据库记忆；正式聊天未来接入时也需要考虑这点。要关闭，设置 `LANGSMITH_TRACING=false`。

终端探针使用 `agent_tracing`；新消息执行器使用 `async_agent_tracing`，后者把 Client 收尾放到线程，避免阻塞事件循环。正式工厂的创建不等于执行，跟踪范围覆盖整个流消费。按次管理 Client、收尾最多等待 5 秒；未来大并发可另行采用应用生命周期 Client。HTTP 网页尚未接入，不能因此认为网页聊天已有 trace。

本次 276 项离线测试通过，包括新增 9 项 tracing 测试；没有使用真实 LangSmith Key，没有调用真实模型或确认云端上传。

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

这次只验证回答，所以在发给模型前隐藏所有工具（仅设置 tools=[] 不会移除框架内置工具）。没有挂载宿主机文件、连接数据库或设置持久化 checkpoint；LangSmith tracing 由上文配置控制，默认关闭。只发送固定测试问题和 Agent 提示，不发送项目文件或数据库内容。工具调用和长期记忆需要后续另外验证。

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

启动前先准备 MySQL、当前迁移和 `DB_*`，并停止本项目终端聊天。FastAPI 启动会连接数据库核对残留生成状态，成功后才接受请求。访问 <http://127.0.0.1:8000/health>，应收到 `{"status":"ok"}`；这个请求本身不查数据库或调用大模型，只证明 HTTP 应用此时能响应，不代表数据库或模型持续可用。它不计入 11 个业务接口。

前端通过 Vite 开发代理访问同一个接口，启动步骤见 [前端说明](../frontend/README.md)。目前未配置宽泛的 CORS 许可，生产同站点部署时仍需单独配置转发。

已有可重复运行的健康接口测试：

```bash
uv run python -m unittest discover -s tests -v
```

离线健康接口测试注入假的启动环境，不访问数据库或模型；真实启动清理另有隔离 MySQL 验收。现已有注册和登录服务函数，但业务 HTTP 接口仍未接入，不能把 /health 可用等同于网站可注册登录。
