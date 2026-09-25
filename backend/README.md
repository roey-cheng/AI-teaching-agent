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
| argon2-cffi | 25.1.0 | 已为后续注册功能安装的密码哈希工具；尚未编写或接入注册业务逻辑 |

以上库还会自动安装它们所需的其他依赖，包括 LangGraph。安装了模型相关库不代表已经选定模型提供方，也不会自动调用模型。

MySQL 驱动、SQLAlchemy 和 Alembic 已安装，数据库配置读取和独立连接检查代码已编写。目前五张业务表的 ORM 模型、Alembic 配置和首份迁移脚本已实现，并通过独立临时 MySQL 8.4.11 的真实迁移验收；用户随后已报告项目数据库建表成功。已完成注册、当前用户、登录、会话、消息、SSE 数据、记忆响应及通用 HTTP 错误响应的 Pydantic Schema；业务服务、业务接口和统一异常处理器尚未实现。本次补 Schema 只运行离线测试，没有连接或修改项目数据库。

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

当前共 137 项测试通过（包括 /health、数据库与模型探针、五张 ORM 表模型、SQLAlchemy 连接检查的离线测试、账号/会话/消息/SSE/记忆/通用错误数据 Schema 校验，以及 11 项迁移离线测试）。这些测试使用虚构配置、模拟调用或离线 SQL 编译，不访问真实数据库或模型，也不产生 API 费用。应用模块导入不会自动读取配置或连接数据库；Alembic 的 env.py 则是命令执行入口，在线命令会连接数据库。/health 的行为保持不变。

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

当前有 137 项离线测试，真实验收的 9 组用例单独统计。以上不代表账号/聊天业务已实现；用户隔离、同会话回复校验、失败重试及迟到结果保护仍待业务层测试。隔离验收结束时项目库仍为空；用户随后已报告手动完成项目库建表。

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

到这里，第一版已讨论的 API 请求/响应数据格式已补齐；GET 查询和 204 退出响应不为凑数量创建空 Schema。接口路由、查询参数拒绝、认证、异常处理及业务逻辑仍未实现，不等于 11 个业务接口已经可用。

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
