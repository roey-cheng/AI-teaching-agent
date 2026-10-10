# AI 聊天 Demo API 设计：第一版

**创建日期：** 2026-09-20

**更新日期：** 2026-10-11

**状态：** 全部 11 个业务 HTTP 接口已接入 `backend/app/api/`：账号四个、会话三个、历史/记忆各一个、发送/失败重试 SSE 两个。Cookie、来源检查、限流、统一错误响应、Deep Agents 执行、结果保存、断线取消、启动清理均已接入。自动测试使用隔离 MySQL 和假供应商流；真实模型 HTTP 联调、网页页面/端到端验收、终端重试命令和运行中长期故障自动恢复仍未完成。

**API 前缀：** /api/v1

## 1. 范围与版本说明

本文定义 Demo 第一版的 11 个业务接口。保留注册登录、用户隔离、会话侧栏、重命名、聊天历史、Deep Agents、流式 SSE、失败重试和只读 Profile Memory 页面。

本次修订替代此前的完整 Demo 方案。归档、cursor 分页、持久化任务队列、完整任务恢复、记忆版本合并和证据计数列为后续迭代。原设计可在 Git 提交 14c25d5 中查看，不再作为第一版必须完成的任务。

- 所有 AI 对话实际通过 Deep Agents；用户身份由后端登录状态确定。
- 会话和记忆直接属于 user_id，不要求课程、Lab、题目或 Enrollment。
- 实现按[内部设计](2026-09-20-ai-chat-demo-implementation-design-zh.md)执行，范围以[功能备忘录](2026-09-20-ai-chat-demo-scope-and-decisions-zh.md)为准。
- GET 用于读取；POST 用于提交或执行操作；PATCH 用于局部修改。
- v1 表示公开接口版本；11 个接口已按此契约接入，不要求保留旧草案字段的兼容实现。
- 阶段 0 的 GET /health 只是开发连通性检查，不计入这 11 个业务接口，不包含数据库凭据或其他内部信息。

## 2. 公共约定

### 2.1 登录和数据隔离

采用同站点部署：页面在 /，接口在 /api/v1。本地通过前端开发代理调用后端，生产环境使用 HTTPS。

- Cookie 名为 chat_session，保存不可预测的随机凭据，使用 HttpOnly、SameSite=Lax、Path=/；HTTPS 使用 Secure。
- MySQL 的 auth_sessions 保存凭据哈希、用户、到期和撤销信息。固定 7 天有效，不自动续期。
- 退出只撤销当前登录，后端重启保留仍有效的登录记录。
- 密码保存为专用密码哈希，模型密钥仅留在后端配置中。
- 写请求校验受信任 Origin；缺少或不匹配时拒绝。JSON 请求要求 application/json，退出接口无请求体。
- 账号 HTTP 已落实：POST 的 Origin 必须唯一且精确匹配 `HTTP_ALLOWED_ORIGINS`；本地默认允许 localhost/127.0.0.1 的 5173/8000。部署须设真实 HTTPS 来源及 `HTTP_COOKIE_SECURE=true`。账号接口不接受查询参数；退出/当前用户不接受正文。注册/登录媒体类型错误返回 `415 UNSUPPORTED_MEDIA_TYPE`，四个账号接口正文超过 16 KiB 返回 `413 REQUEST_TOO_LARGE`。配置模板为 `backend/http.env.example`。
- 会话/历史/记忆的五个 HTTP 接口已共用上述来源检查和 16 KiB 请求体上限。新建和重命名要求 application/json；GET 不接受正文；五个接口都不接受查询参数。路径 session_id 验证为 BIGINT UNSIGNED 范围内的正整数十进制字符串，不合法返回 422。未来发送聊天正文的上限另行落实，不能直接套用 16 KiB。
- 发送与重试两个 POST 同样要求可信 Origin、application/json、合法路径 ID，拒绝查询参数；原始请求体上限为 256 KiB，超限 413。正文仍按 Schema 最多 20,000 字符且不可纯空白，不接受前端模型配置。配置只在聊天请求中加载；模型/tracing/预算配置无效时，准入前返回 `503 MODEL_CONFIGURATION_UNAVAILABLE`，不保存问题或发出 SSE。
- 除注册登录外，业务操作要求登录；退出对已退出状态幂等成功。
- 查询和修改会话都检查 user_id；重试还要验证消息属于该会话。无权限与不存在统一返回 404。
- 不接受前端指定所属用户、完整模型上下文、模型密钥或工具列表。

### 2.2 数据格式与错误

普通响应使用 JSON；首次接受发送或重试后返回 SSE。所有 ID 用字符串，时间用 UTC ISO 8601。未知请求字段拒绝，不悄悄忽略归档等已暂缓功能的参数。

列表响应使用 items，本版不接受 limit、cursor、before，也不返回 next_cursor、next_before、has_more。演示数据较少时一次读取全部；上线扩大数据量前补分页。

错误格式：

~~~json
{
  "error": {
    "code": "SESSION_NOT_FOUND",
    "message": "Session not found or inaccessible.",
    "request_id": "req_example"
  }
}
~~~

错误的 code、message、request_id 均为必填非空字符串；code 最多 64 字符、message 最多 500 字符，与现有 SSE 错误约束一致。程序生成的公开错误提示使用英文，不强制翻译用户内容。Schema 只检查输出形状，不自动生成请求编号、设置 HTTP 状态码或清理原始异常中的敏感信息；这些由后续异常处理器负责。

| HTTP 状态 | 含义 |
|---|---|
| 401 | 未登录、登录失效、凭据错误 |
| 403 | 请求来源校验失败 |
| 404 | 会话或消息不存在或无权访问 |
| 409 | 邮箱重复、会话忙、幂等冲突、重试条件不成立或状态已变化 |
| 413 | 账号/会话/查询请求超过 16 KiB，或发送/重试请求超过 256 KiB |
| 415 | 要求 JSON 的写接口未使用 application/json |
| 422 | 参数错误、空消息、未知字段或上下文超限 |
| 429 | 基础请求频率限制 |
| 503 | 流开始前确认模型配置或数据库等服务不可用 |
| 500 | 未预期错误，仅返回过滤后的描述 |

流开始后通过 message_error 报错，不能再更改 HTTP 状态。连接已经断开时不保证终止事件送达，重新查询历史确认结果。

### 2.3 并发和基础限流

仅部署一个 FastAPI 服务进程。前端在生成时禁用当前会话发送与重试按钮；后端用会话级运行登记和短临界区，原子完成“检查是否忙 → 占用”，同一会话最多一个运行。第二个新请求返回 409 SESSION_BUSY，不保存消息。

基础频率计数放在进程内存，重启重置：登录按 IP 和规范化邮箱分别 10 次/分钟，注册按 IP 10 次/小时，新生成按用户 10 次/分钟。重复请求回执不消耗新的生成额度。不建设持久化队列、排队状态或多进程容量协调。

## 3. 11 个业务接口总览

| 编号 | 方法 | 路径 | 功能 | 成功响应 |
|---|---|---|---|---|
| 1 | POST | /api/v1/auth/register | 注册 | 201 JSON |
| 2 | POST | /api/v1/auth/login | 登录 | 200 JSON + Cookie |
| 3 | POST | /api/v1/auth/logout | 退出当前登录 | 204 |
| 4 | GET | /api/v1/users/me | 当前用户 | 200 JSON |
| 5 | POST | /api/v1/chat/sessions | 新建会话 | 201 JSON |
| 6 | GET | /api/v1/chat/sessions | 本人全部会话 | 200 JSON |
| 7 | PATCH | /api/v1/chat/sessions/{session_id} | 仅重命名 | 200 JSON |
| 8 | GET | /api/v1/chat/sessions/{session_id}/messages | 会话全部消息及生成状态 | 200 JSON |
| 9 | POST | /api/v1/chat/sessions/{session_id}/messages | 发送消息、流式回复 | 200 SSE；重复请求为 JSON |
| 10 | POST | /api/v1/chat/sessions/{session_id}/messages/{message_id}/retry | 重试最后一个失败问题 | 200 SSE；最近一次重复请求为 JSON |
| 11 | GET | /api/v1/me/memory | 本人记忆摘要 | 200 JSON |

归档与重命名原先共用接口 7；暂缓归档后接口 7 仍保留。接口 10 的失败重试也保留，因此仍为 11 个。

## 4. 账号接口

### 4.1 注册账号

**`POST /api/v1/auth/register`**，不要求登录。

请求字段：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `email` | string | 是 | 合法邮箱，最长 320 字符；后端去除首尾空格并统一大小写 |
| `password` | string | 是 | 建议 8～128 字符；不自动裁剪或改变大小写 |
| `display_name` | string | 是 | 去除首尾空格后 1～100 字符 |

```json
{
  "email": "student@example.com",
  "password": "example-password-only",
  "display_name": "小明"
}
```

成功返回 `201 Created`：

```json
{
  "user_id": "123",
  "email": "student@example.com",
  "display_name": "小明",
  "created_at": "2026-09-20T08:00:00Z"
}
```

注册成功不自动登录，前端引导用户登录。密码在后端进行专用密码哈希处理；不返回或记录密码及其哈希。公开注册不能设置管理员角色。

主要错误：`409 EMAIL_ALREADY_REGISTERED`、`422 VALIDATION_ERROR`；数据库或密码哈希服务异常使用 `503 REGISTRATION_UNAVAILABLE` 的安全提示。提交期间断线可能无法确认账号是否创建，不自动重试写入，需先核对账号状态。

注册服务已实现于 `backend/app/services/auth.py`，HTTP 路由已接 `backend/app/api/accounts.py`，错误映射已接 `api/errors.py`。密码采用 argon2-cffi 的 Argon2id 与随机盐；读取查重结束后再哈希，短写事务内保存用户。并发重复注册依靠邮箱唯一约束兜底，注册成功不创建登录记录。

### 4.2 登录

**`POST /api/v1/auth/login`**，不要求已有登录。

```json
{
  "email": "student@example.com",
  "password": "example-password-only"
}
```

邮箱采用与注册一致的规范化方式。密码必填，接收 1～128 字符，不自动裁剪或改变大小写；登录不套用注册时设置新密码的 8 字符下限。格式合规不代表密码正确，验证失败仍按下述规则返回 `401 INVALID_CREDENTIALS`；缺失、空字符串、错误类型、超长或未知请求字段返回 `422 VALIDATION_ERROR`。

成功返回 `200 OK`，设置登录 Cookie，并返回：

```json
{
  "user": {
    "user_id": "123",
    "email": "student@example.com",
    "display_name": "小明"
  }
}
```

后端验证密码与账号状态，成功后创建或轮换当前登录凭据。不存在的账号、错误密码或不可登录账号统一返回 `401 INVALID_CREDENTIALS`，不提供密码或内部原因。

登录服务已实现于 `backend/app/services/login.py`：密码验证在数据库事务外，写入前复核账号状态/哈希；同一事务更新登录时间、撤销当前尚未过期/撤销的旧凭据并创建固定 7 天的新记录，不撤销其他设备。数据库只存随机凭据的 SHA-256 哈希。服务内部结果另带 SecretStr 原凭据，现由 `api/accounts.py` 在业务成功后设置 Cookie，不放入上述 JSON。HTTP 路由、Cookie 和 IP/邮箱限流已接入。数据库失败返回 `503 LOGIN_UNAVAILABLE`，不暴露内部错误或返回未确认提交的凭据，不自动重试。

### 4.3 退出登录

**`POST /api/v1/auth/logout`**，无请求体。

服务端撤销当前登录凭据并清除 Cookie，返回 `204 No Content`。凭据不存在或已失效时也返回 `204`。不删除会话、消息和记忆，不自动退出其他设备。

业务函数已实现于 `backend/app/services/logout.py` 的 `logout_user`：无需先通过登录状态检查；缺少或畸形凭据直接成功，否则按凭据哈希执行条件更新，仅撤销当前尚未过期/撤销的记录。未知、过期、已撤销凭据成功无操作，重复退出不覆盖原撤销时间，禁用账号也能退出。数据库失败返回 `503 LOGOUT_UNAVAILABLE`，不伪装成功，不自动重试；提交断线不保证可确认撤销结果。HTTP 路由、204/503 映射、Origin 检查和 Cookie 清除已接入；服务失败时不清除 Cookie 冒充已退出。

### 4.4 当前用户

**`GET /api/v1/users/me`**，要求登录，无请求体。

成功返回 `200 OK`：

```json
{
  "user_id": "123",
  "email": "student@example.com",
  "display_name": "小明"
}
```

前端打开或刷新网站时调用。未登录返回 `401 UNAUTHENTICATED`，不返回密码哈希或内部凭据。

业务函数已实现于 `app/services/authentication.py` 的 `get_current_user`：用登录凭据哈希关联 auth_sessions 和 users，校验未撤销、未到期且账号 ACTIVE，再返回 UserResponse。缺少、畸形、未知、过期或撤销凭据以及禁用账号均为 UNAUTHENTICATED；数据库故障使用 AUTHENTICATION_UNAVAILABLE，HTTP 映射为 503，不冒充未登录。检查不自动续期、不更新最后登录时间，也不接受前端指定用户。HTTP 路由、Cookie 读取、401/503 映射已实现；后续资源操作仍需独立检查数据归属。

## 5. 会话接口

### 5.1 新建会话

POST /api/v1/chat/sessions，要求登录，请求体 {}，成功返回 201：

~~~json
{
  "session_id": "1001",
  "title": "new chat session",
  "created_at": "2026-09-21T08:10:00Z",
  "updated_at": "2026-09-21T08:10:00Z",
  "last_activity_at": "2026-09-21T08:10:00Z"
}
~~~

后端绑定当前用户。首条消息去掉首尾空白、换行转空格，截取前 30 个字符作为标题，不调用模型；手动命名后不再覆盖。每次成功调用创建一个会话；按钮防连击，网络结果不明时先刷新列表，不自动重新创建。

新建业务已实现于 `backend/app/services/chat_sessions.py` 的 `create_chat_session`：调用方传入已验证的当前用户，事务内显式保存标题“new chat session”（用户已修改为英文）、非手动标记和同一 UTC 创建/修改/活动时间，提交后返回 SessionResponse。ORM 和旧迁移的数据库默认值仍为“新对话”，不修改已创建的表。只创建空会话，不生成首条消息标题或调用模型。HTTP 路由已接 `api/chat_sessions.py`，必须提交空对象 `{}`，由 `api/dependencies.py` 验证 Cookie 确定主人。数据库故障返回 `503 SESSION_UNAVAILABLE`，不自动重试；前端按钮尚未实现。

### 5.2 查询左侧会话列表

GET /api/v1/chat/sessions，无查询参数，成功返回 200：

~~~json
{
  "items": [
    {
      "session_id": "1001",
      "title": "Python 的列表是什么",
      "created_at": "2026-09-21T08:10:00Z",
      "updated_at": "2026-09-21T08:30:00Z",
      "last_activity_at": "2026-09-21T08:30:00Z"
    }
  ]
}
~~~

只返回本人全部会话，按 last_activity_at DESC, session_id DESC 排序，即最近活跃的在前。接受新消息、接受重试及成功保存回复时更新活动时间；浏览、改名和失败结算不提升排序。无数据返回 items=[]。

列表业务已实现于 `backend/app/services/chat_sessions.py` 的 `list_chat_sessions`：调用方传入已验证用户，查询时限制所属用户，按活动时间和数据库会话编号倒序取全部记录，返回 SessionListResponse。不读取消息、不修改时间。GET 路由、Cookie 认证及拒绝查询参数已接入，数据库故障返回 `503 SESSION_UNAVAILABLE`，不能返回虚假的空列表。

### 5.3 重命名

PATCH /api/v1/chat/sessions/{session_id}，要求登录并拥有会话：

~~~json
{"title": "Python 基础学习"}
~~~

title 必填，去首尾空格后 1～100 字符。成功返回 200 和更新后的会话对象，字段同新建响应；记录 title_is_manual=true。生成中允许改名。

重命名业务已实现于 `backend/app/services/chat_sessions.py` 的 `rename_chat_session`：校验编号后，在短事务中按会话编号和当前用户锁定记录，只更新标题、手动标记和 updated_at，不改 last_activity_at。相同标题也设置手动标记。PATCH 路由、Cookie 认证、Origin、参数校验和错误映射已接入；不存在或不属于本人统一返回 `404 SESSION_NOT_FOUND`，数据库故障为 `503 SESSION_UNAVAILABLE`，不自动重试；事务提交后才返回 SessionResponse。

本版不接受 status=ARCHIVED，不实现归档、删除或恢复接口。

## 6. 聊天消息、发送与失败重试

### 6.1 消息与当前生成状态

一次新发送保存一条 USER 消息；生成成功后保存一条关联它的 ASSISTANT 消息。失败重试复用原 USER 消息，不新增问题，不覆盖已保存的成功回答。

为支持重试和刷新后的错误提示，在 USER 消息上保存“最近一次生成”的少量字段，而不建立独立生成尝试表：

| 字段 | 含义 |
|---|---|
| attempt_id | 当前生成编号，后端生成 UUID，每次重试换新编号 |
| retry_of_attempt_id | 此次重试针对哪个失败编号；首次发送为空，供防重复判断 |
| generation_status | RUNNING / SUCCEEDED / FAILED |
| generation_error_code / generation_error_message | 最近一次失败原因，其余状态为空 |

attempt_id 只是区分这一次执行的编号，不表示实现了完整任务系统。本版不保留每次尝试的历史记录。

- RUNNING：正在执行 Agent 或保存结果。
- SUCCEEDED：完整回答与成功状态已在同一事务中提交。
- FAILED：已确认本次未完成，包括模型错误、超时或执行中断。
- 这里的成功不代表答案一定正确；记忆更新失败也不一定导致聊天失败。
- 半截回复只在页面临时显示，失败时标注不完整，不保存为最终 ASSISTANT。
- 每条 USER 的状态独立保存，新问题成功不能抹掉旧问题的失败信息。

### 6.2 查询全部历史

GET /api/v1/chat/sessions/{session_id}/messages，无分页参数，要求登录且拥有会话。

~~~json
{
  "session_id": "1001",
  "is_generating": false,
  "items": [
    {
      "message_id": "2001",
      "role": "USER",
      "content": "请解释 Python 列表。",
      "in_reply_to_message_id": null,
      "created_at": "2026-09-21T08:30:00Z",
      "generation": {
        "attempt_id": "30dfe922-1183-4dfb-9ce7-ea940d619195",
        "status": "FAILED",
        "assistant_message_id": null,
        "error": {
          "code": "GENERATION_TIMEOUT",
          "message": "Response generation timed out. Please try again."
        },
        "can_retry": true
      }
    }
  ]
}
~~~

全部消息按 created_at ASC, message_id ASC 返回，即从旧到新。同一时间用 ID 保证稳定顺序。ASSISTANT 不包含 generation，使用 in_reply_to_message_id 指向问题。

这里的“全部历史”仅指路径中这个会话的全部消息，供页面展示，不包含其他会话的消息。发送或重试时，后端另外组装模型上下文：先为系统提示、当前问题、本人 Profile Memory、工具及输出等预留 token 预算，再从最近一轮完整成功问答向前选取，下一轮放不下就停止，最终按从旧到新顺序交给模型。不设固定轮数上限，当前问题只完整加入一次。模型窗口、可配置应用 token 预算和实际输入/输出限制共同约束加载量，计数适配规则见内部设计第 3.1 节。未选入的历史仍保留在数据库和页面；当前问题移除历史后仍放不下时，接受前返回 422 CONTEXT_TOO_LARGE，不保存新问题或截断正文。新发送和失败重试已共用输入组装与预算估算；重试预算拒绝保持原失败记录不变，HTTP 映射尚未接入。

is_generating 表示本会话是否仍有实际运行。generation 取 USER 自身的最新状态，成功时返回关联的 assistant_message_id；失败时返回简短错误。can_retry 只有在“会话无运行、该问题为整个会话最后一条 USER、最近生成已确认失败”时为 true。查询时保证同一响应的状态一致；写接口仍重新验证。

本接口不调用模型。旧版 latest_generation 和独立尝试历史均不使用；前端用 is_generating 判断忙碌，用每条消息的 generation 显示结果。

只读业务已实现于 `backend/app/services/message_history.py` 的 `get_message_history`。按本人会话检查归属，再读取有序完整消息、匹配回答并计算状态；显式接收共享 GenerationRegistry，在会话短锁内读取数据库和运行登记，不能每次请求创建新的空登记。清理尚未结束即使数据库已是最终状态仍算忙碌。未登记的 RUNNING、登记编号不匹配或问答关系损坏时，返回 `503 MESSAGE_HISTORY_UNAVAILABLE`，不静默返回空闲或修改数据库。错误摘要使用白名单英文提示，未知代码返回 GENERATION_FAILED，不暴露数据库原始错误文字。GET 已接 `api/chat_sessions.py`，使用应用生命周期中的同一份 registry，将整个同步业务在线程中执行；Cookie 认证及 404/503 错误映射已接入。

### 6.3 发送并流式回复

POST /api/v1/chat/sessions/{session_id}/messages：

~~~json
{
  "client_message_key": "550e8400-e29b-41d4-a716-446655440000",
  "content": "我是 Python 初学者，请解释一下列表。"
}
~~~

client_message_key 是前端生成的 UUID，同一次发送网络重发复用；content 最长 20,000 字符，不允许纯空白，保留代码缩进和换行。

处理顺序：

1. 验证身份、会话归属和字段，先查询消息键是否已经存在。
2. 对新请求检查输入预算、频率和会话占用，在单进程内原子占用会话。
3. 在短事务中保存 USER、初始 attempt_id 和 RUNNING 状态，必要时初始化标题。
4. 事务外运行 Deep Agents，逐步转发可见回答；模型执行使用默认 120 秒的请求级超时。
5. 成功时在同一事务中保存一条完整 ASSISTANT，并将对应 USER 标记 SUCCEEDED；提交后才发送 message_done。
6. 失败时记录 FAILED 和简短原因，连接仍可用则发送 message_error，完成清理后释放本次占用。

被拒绝的新请求不留下 USER 或占用新消息键。模型生成期间不持有数据库事务。保存时仍核对 attempt_id 与 RUNNING 状态，旧执行不能覆盖新一次重试。

消息准入已实现于 `backend/app/services/message_submission.py` 的 `accept_user_message`：以 with 管理执行所有权，检查归属、重复键、忙碌、输入预算及必填限流检查，保存 USER/RUNNING 并更新标题/活动时间。新接受返回 AcceptedMessage（含输入快照），重复返回 DuplicateMessageResponse。`chat_execution.execute_chat_turn` 在该作用域内串起正式 Agent、异步流、思考/进度事件、超时取消与最终保存。HTTP/SSE 现已由 `api/chat_messages.py` 与 `api/sse.py` 接入，注入共享登记与真实用户生成限流；账号/IP 限流和启动清理也已接入。退出时核对状态并匹配释放占用，不覆盖最终状态，不盲目重发 INSERT 或调用模型；核对失败保留占用。运行中长期数据库故障的自动核对仍待实现。

### 6.4 重试最后一个失败问题

POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/retry。message_id 是 USER 消息 ID。

~~~json
{
  "failed_attempt_id": "30dfe922-1183-4dfb-9ce7-ea940d619195"
}
~~~

failed_attempt_id 必填 UUID，取自历史接口中该问题的 generation.attempt_id，表示“我要重试页面上这一次失败”。这替代旧草案的 retry_key，简化为依据失败编号防重复，不需要保存全部重试请求历史。

接受条件：

- 当前用户拥有会话及消息。
- 该消息是整个会话最后一条 USER，最新状态是已确认的 FAILED。
- failed_attempt_id 等于该问题当前 attempt_id，会话没有其他运行。
- 已成功的回答、较早问题和结果尚未确定的断线请求都不能直接重试。

接受后为原问题创建新的 attempt_id，记录 retry_of_attempt_id=failed_attempt_id，状态改为 RUNNING，并清空旧错误；不插入新 USER。复用接口 9 的上下文组装、Agent、SSE 和保存逻辑。

防重复规则在同一会话短临界区内判断：

1. 若当前 retry_of_attempt_id 已等于请求的 failed_attempt_id，说明它刚被重试过，返回当前生成的 JSON 回执，不再调用模型，即使这次重试后来也失败了。
2. 否则按接受条件判断；编号已过时返回 409 STALE_GENERATION，其他条件不成立返回 409 RETRY_NOT_ALLOWED 或 SESSION_BUSY。
3. 如果新的尝试再次失败，用户刷新状态后用新的失败编号发起下一次有意重试。更旧请求重放只返回冲突，不恢复完整历史回执。

数据库保留同一 USER 最多一条成功回复的唯一约束。重试改变的是该问题的最新生成元数据，不修改问题正文。返回事件格式与接口 9 一致。

**业务实现状态（2026-10-11）：** `backend/app/services/message_retry.py` 的 `accept_failed_message_retry` 已实现上述准入和一跳重复回执；`chat_execution.execute_chat_retry` 与新发送共用两阶段 Agent、思考/进度事件、超时、保存和清理。原问题只进入模型输入一次；重试 UPDATE 提交异常时独立核对，确认回滚则保留旧失败，确认新尝试已提交且未完成则标为中断，核对失败保留占用。两个 HTTP/SSE 路由已接入，并使用同一份每用户生成限流器；不新增表或迁移。终端重试命令及网页按钮尚未接入。

访问不到会话仍使用 `404 SESSION_NOT_FOUND`；在本人会话中找不到目标消息使用 `404 MESSAGE_NOT_FOUND`；传入 ASSISTANT 消息使用 `409 RETRY_NOT_ALLOWED`。流开始前数据库核对失败返回 `503 MESSAGE_SEND_UNAVAILABLE`；流开始后无法确认结果时结束流并要求查询历史。这些 HTTP 映射已接统一错误处理器；RUNNING 与本地登记不一致不能伪装正常回执，需先完成启动/故障清理。

### 6.5 简单超时、断线与重启

**启动实现状态（2026-10-11）：** `startup_cleanup.reconcile_interrupted_generations` 已由终端确认后的入口和 FastAPI lifespan 调用，不增加公开接口。启动先取得 macOS/Linux 的同项目进程锁，再检查 MySQL/迁移，并在空闲登记屏障下以同一事务核对残留 RUNNING：有效最终回复已存在则恢复 SUCCEEDED，否则标记 FAILED / GENERATION_INTERRUPTED；异常关联、数据库失败或提交未确认则停止启动。清理不调用模型，不删除历史或改动原执行编号、创建时间、会话排序、登录和记忆。进程锁覆盖整个运行期间，只保护同主机同目录入口；首次使用必须停止未接锁的旧程序，禁止其他目录或其他机器同时操作同一聊天库。FastAPI 现在启动就依赖数据库，`/health` 本身仍只检查 HTTP 存活，不代表数据库持续可用。

- 默认 120 秒是一次 Agent 执行的请求级超时，配置在后端；不建立 180 秒持久化 deadline、扫描器、队列或任务领取流程。
- 超时取消本次异步调用，已确认未完成时记录 FAILED / GENERATION_TIMEOUT。停止接收本次后续片段；数据库保存仍验证当前编号与状态。
- 第一版生成随请求运行。服务检测到浏览器断线时取消本次执行；不承诺断线后继续完成，不实现 SSE 断点续传。
- 断线可能发生在结果提交之后。前端先显示“连接中断，正在确认结果”，查询历史；如果结果已经保存，展示成功回答。仍在清理时保持忙碌，不能立即重试。
- 重启不恢复模型执行；已保存消息、登录和记忆保留。单进程启动时做一次残留 RUNNING 状态清理，核对已保存结果后将未完成的生成标为 FAILED / GENERATION_INTERRUPTED，然后接受请求。这只是避免永久显示生成中，不重新运行任务。
- 事务提交结果不明或数据库不可用时先查询确认，不能擅自报失败并启动第二次生成。失败状态无法写入时，服务恢复后只清理已确认没有存活执行者的残留状态，不能误伤仍在运行的请求。
- 只允许单进程运行，旧进程完全退出再启动新进程；不能把上述清理用于仍有其他工作进程运行的部署。

## 7. SSE 流式协议

发送与重试的 POST 响应直接承载 SSE，不另建订阅接口。前端使用 fetch 读取响应流；原生 EventSource 只发 GET，不用于这两个 POST。

响应为 Content-Type: text/event-stream; charset=utf-8、Cache-Control: no-cache, no-store、X-Accel-Buffering: no；部署时还需关闭代理缓冲/压缩缓冲并配置读取超时。按空行解析事件，不能把一个网络数据块当成一个完整事件。

| 事件 | 含义 | 数据 |
|---|---|---|
| message_start | 问题已登记，本次生成开始 | session_id、user_message_id、attempt_id |
| agent_progress | 实际执行阶段，不是模型编造的步骤 | attempt_id、stage |
| reasoning_delta | 模型 API 提供的新增推理文字，与回答分开 | attempt_id、text |
| message_delta | 新增可见回答片段 | attempt_id、text |
| message_done | 完整回复已保存 | attempt_id、assistant_message |
| message_error | 本次生成已确认失败 | attempt_id、error |

2026-10-11：`api/chat_messages.py` 已把两个 POST 接入共享执行器，`api/sse.py` 负责白名单事件编码与传输。收到 message_start 后才发送 SSE 响应头；准入错误仍使用普通 HTTP JSON，重复请求直接返回 200 JSON 回执，不产生另一套事件流。默认队列最多 8 个公开事件、空闲心跳 15 秒、单次网络发送超时 15 秒。检测断线/写失败/服务器关闭时取消并等待执行器清理，不遗留后台生成任务。错误事件沿用本次 HTTP request_id。思考与进度不增加公开接口数量，仍是 11 个业务接口。

`stage` 允许：`context_ready`（历史与记忆快照准备完毕）、`memory_checking`（判断是否需更新记忆）、`memory_saving`（准备提交记忆）、`memory_saved`（记忆提交已确认）、`memory_skipped`（无新记忆保存）、`memory_unavailable`（无法确认记忆更新，继续回答）、`agent_running`（即将消费回答 Agent 流）、`thinking`（开始收到 API 推理文字）、`answering`（开始收到回答正文）、`saving`（完整生成结束，开始保存回答）。记忆阶段内部输出不作为 reasoning_delta/message_delta 展示。`thinking`、`answering` 各在首个对应片段时发送一次；没有推理片段就不虚构思考内容。记忆已保存不代表回答已完成；回答完成/失败仍由终止事件表示。

思考文字仅当次展示，不进入 `messages.content`、不进入下轮历史，也不新增数据库列；刷新后不恢复。未来前端用单独的可折叠区域展示，按 `attempt_id` 隔离。所有这些文字都按不可信文本处理，不能直接注入 HTML。当前只允许主模型白名单文字字段，工具参数、通用图状态及记忆快照不直接转发。模型生成的推理可能复述输入中的个人信息，因此不是额外的隐私隔离机制；只应交给通过认证的会话所有者。

~~~text
event: message_start
data: {"session_id":"1001","user_message_id":"2001","attempt_id":"30dfe922-1183-4dfb-9ce7-ea940d619195"}

event: message_delta
data: {"attempt_id":"30dfe922-1183-4dfb-9ce7-ea940d619195","text":"Python 列表可以保存多个元素。"}

event: message_done
data: {"attempt_id":"30dfe922-1183-4dfb-9ce7-ea940d619195","assistant_message":{"message_id":"2002","role":"ASSISTANT","content":"Python 列表可以保存多个元素。","in_reply_to_message_id":"2001","created_at":"2026-09-21T08:30:03Z"}}

~~~

失败示例：

~~~text
event: message_error
data: {"attempt_id":"30dfe922-1183-4dfb-9ce7-ea940d619195","error":{"code":"MODEL_REQUEST_FAILED","message":"Failed to generate a response. Please try again later.","request_id":"req_example"}}

~~~

每个连接正常情况下收到 start、零或多个 delta、一个 done 或 error。断线时可能收不到终止事件。前端按 attempt_id 拼接，忽略已终止或旧尝试的迟到片段，done 时以完整回答替换临时文字。

流开始后若最终结算无法确认或出现未预期内部异常，后端结束流但不编造 message_error/FAILED 或 message_done。EOF 前没有终止事件时，前端按“结果待确认”读取历史，不直接重试；HTTP 200 本身不代表回答成功。服务只记录安全请求编号，不向流写入异常或 SQL 原文。

片段不单独入库。内部工具参数、系统提示、模型推理和记忆日志不作为聊天回答发送。可使用 : ping 注释心跳，前端不显示它。Markdown 渲染禁用或净化不受信任的 HTML。

## 8. 消息防重复与回执

新消息使用 messages(session_id, client_message_key) 唯一约束。同键同正文返回已保存问题的当前生成回执，不新建问题或调用模型；同键不同正文返回 409 IDEMPOTENCY_CONFLICT。身份和归属始终先验证，查重优先于忙碌拒绝。

~~~json
{
  "duplicate": true,
  "session_id": "1001",
  "user_message_id": "2001",
  "attempt_id": "30dfe922-1183-4dfb-9ce7-ea940d619195",
  "status": "RUNNING",
  "assistant_message_id": null
}
~~~

发送请求的重复回执指向该问题当前生成，不承诺返回最初那次执行的历史；重试重复回执按第 6.4 节。前端按 Content-Type 区分 SSE 与 JSON，收到回执后读取历史。

网络自动重发不等于用户点击失败重试。失败后点击重试走接口 10，不换 client_message_key 重新提交一条相同问题。用户主动再次发送同样文字则是新消息，使用新消息键。

## 9. Profile Memory

实现进度：查询 Schema、内部查询/保存业务、独立 Agent 记忆阶段与终端 `/memory` 已接入。只读 HTTP 路由已接 `api/memory.py`，由 Cookie 确定用户并调用 `list_profile_memory`，不调用模型；数据库故障返回 `503 MEMORY_UNAVAILABLE`，不是空列表。不提供公开记忆增删改接口，网页尚未完成。记忆阶段最多一次非思考模型判断和一次写入，然后使用原思考模型回答；真实模型的提取质量与云端 tracing 仍需手动验证。来源引用校验和敏感模式拦截不是完备的语义隐私保证。

GET /api/v1/me/memory，要求登录，返回本人实际保存的摘要：

~~~json
{
  "items": [
    {
      "memory_id": "301",
      "memory_type": "LEARNING_PREFERENCE",
      "summary": "喜欢使用中文解释。",
      "updated_at": "2026-09-21T08:30:00Z"
    }
  ]
}
~~~

无记忆返回 items=[]；按 updated_at DESC, memory_id DESC 排序。本版覆盖明确陈述且适合长期保留的回答偏好、学习目标、编程背景、个人背景和日常偏好，类型使用 LEARNING_PREFERENCE、LEARNING_GOAL、PROGRAMMING_BACKGROUND、PERSONAL_BACKGROUND、DAILY_PREFERENCE。前端需支持这五种展示分类；与完整教学系统的分类差异在 Demo 内部设计中记录。

候选主题及敏感信息排除规则以[表设计草案第 8 节](2026-09-24-ai-chat-demo-database-design-zh.md#8-agent_memory个人长期记忆)的 25 个主题为准。本次仅扩充记忆内容和分类，不改变响应字段或增加公开接口；不自动修改账号昵称，也不增加提醒或日历功能。长期记忆过滤不等于聊天原文已脱敏。面向真实用户开放前，需另行确认记忆告知、控制与删除方案。

Agent 从 MySQL 投影出的虚拟 /memories/profile.md 读取记忆，并调用本项目自定义的 save_profile_facts 工具保存具体条目。页面和 Agent 使用同一份数据，不另存实际 Markdown 文件。该工具不是公开接口，也不是声称框架自带此函数。

保存按 user_id + memory_key 做简单更新或插入，只改本次涉及的主题，不能覆盖整份旧 Profile。不同主题互不覆盖；同主题并发冲突按实际提交顺序后写生效，本版不承诺按用户陈述先后自动合并。保留数据库唯一约束和短事务，不实现用户级版本表、证据表或证据计数。

用户、问题和运行身份由可信后端绑定，不由模型任意指定。只保存有用户陈述依据的内容，不自行猜测。实际写入成功才可确认已记住；记忆写失败可以继续正常回答。此前已提交记忆不会因后续回答失败被撤销，重试相同主题执行更新而不追加重复条目。

只提供查看页面，不提供前端记忆增删改接口。

## 10. 功能与接口对应

| 网站功能 | 方法与路径 / 内部职责 |
|---|---|
| 注册、登录、退出 | POST /api/v1/auth/register；POST /api/v1/auth/login；POST /api/v1/auth/logout |
| 刷新检查登录 | GET /api/v1/users/me |
| 新建对话 | POST /api/v1/chat/sessions |
| 左侧会话栏 | GET /api/v1/chat/sessions，一次读取全部 |
| 手动改名 | PATCH /api/v1/chat/sessions/{session_id}，仅 title |
| 切换会话、刷新历史及失败提示 | GET /api/v1/chat/sessions/{session_id}/messages |
| 发消息、Deep Agents、流式回答 | POST /api/v1/chat/sessions/{session_id}/messages |
| 原问题失败重试 | POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/retry |
| 查看记忆 | GET /api/v1/me/memory |
| 默认标题、多轮上下文、记忆提取 | 已有发送/重试服务内部完成 |
| 生成状态和禁用按钮 | SSE、历史的 generation / is_generating；后端仍验证并发 |
| 持久化和用户隔离 | 后端与 MySQL，不增加“保存回答”接口 |

## 11. 实现前的工作与暂缓范围

当前按 5 张核心表实现：users、auth_sessions、chat_sessions、messages、agent_memory。完整表字段与老师要求的实施顺序见[内部设计](2026-09-20-ai-chat-demo-implementation-design-zh.md)。

保留简单运行编号和消息状态，以支持 11 个接口中的原消息重试；暂缓 generation_attempts、memory_profiles、memory_evidence 三张扩展表，以及 QUEUED、active_attempt_id、持久化 deadline、完整尝试历史和版本冲突合并。

不做归档、cursor 分页、成功回答重新生成、多进程协调、任务队列、SSE 断点续传或中途任务重启恢复。每个功能的验收以第一版为准，不再要求旧方案的完整并发测试矩阵。

模型提供方、型号、依赖版本和输入预算在阶段 0 及接入时确认。设计写入文档不代表代码、迁移、联调和测试已完成。
