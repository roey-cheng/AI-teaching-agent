# 代码命名与职责说明

更新：2026-11-23。本页描述当前实现；旧设计文档中的名称只用于历史追溯。

## 先分清五件事

| 概念 | 本项目的准确含义 | 不是什么 |
| --- | --- | --- |
| 会话 session / thread | 用户打开的一段聊天；框架 thread_id 由用户与会话 ID 生成 | 不是一次模型 API 请求 |
| 生成尝试 attempt | 从接受一个问题或重试请求，到成功/失败/取消的一次执行 | 不是长期记忆；同一个问题重试会产生新的 attempt |
| 运行依赖 dependencies | 本次执行需要的准入快照、只读画像和尝试对象 | 不是发给模型的聊天历史，也不是 context window |
| 会话存档 checkpoint | 框架保存的会话图状态 | 不是跨会话的长期画像，也不保证任意副作用自动恢复 |
| 长期画像 profile | 学习目标、解释偏好等跨会话事实，权威数据在 MySQL | 不是整段聊天全文，也不是模型推理记录 |

`context window` 只表示模型能够接收/输出的 token 容量。框架参数 `context=` 则承载应用运行依赖；两者不是同一种 context。

## 工厂与运行链路

| 当前名称 | 职责 | 旧名 |
| --- | --- | --- |
| `compile_chat_graph` | 编译并返回图，不执行聊天 | `build_chat_agent` |
| `SessionChatGraphCache` | 按用户/会话缓存图；不把历史存在缓存里 | `ChatAgentProvider` |
| `ChatAdmissionSnapshot` | 准入时的问题、画像文本和本地预算快照；尚未恢复会话历史 | `PreparedAgentInput` |
| `prepare_chat_admission` | 读取准入资料并检查本地预算 | `prepare_agent_input` |
| `ChatRunDependencies` | 本次图运行注入的依赖容器 | `ChatRunContext` |
| `ChatAttemptState` | 本次生成尝试的模型调用计数、画像工具权限、协议消息 | `TurnMemory` |
| `TeachingChatMiddleware` | 教学聊天的工具限制、请求预算和协议记录 | `OrchestratorMiddleware` |
| `ProfilePromptBudgetMiddleware` | 注入只读画像后检查完整请求预算；基础模式拒绝工具 | `ChatMemoryBudgetMiddleware` |
| `ReasoningReplayChatDeepSeek` | 在带工具的请求中回传已经记录的供应商 reasoning_content | `ProtocolChatDeepSeek` |

`ChatRunDependencies` 的字段明确区分三件事：

- `admission_snapshot`：准入快照，不是最终 LLM 请求。
- `profile_snapshot_store`：本次运行读取的画像副本，不是数据库连接。
- `attempt_state`：这次生成尝试的临时状态，不是已经保存的会话历史。

`ChatAttemptState.model_call_count` 表示当前尝试的正式回答模型调用次数；`profile_read_count` 表示画像读取工具调用次数；`protocol_messages` 是本次尝试已收集的 AI/Tool 协议消息。失败重试会重新创建这些计数和收集器。

## Store、StoreBackend 和存档器不是同一个东西

| 当前名称 | 作用 |
| --- | --- |
| `ProfileSnapshotStoreRouter` | 只转发操作到本次运行的 `profile_snapshot_store`；本身不存数据 |
| `ProfileStore.read_only_snapshot(...)` | 创建供图读取的临时内存副本，不访问 MySQL、不提交写入 |
| `ProfileStore(user_id, session, ...)` | 事务绑定的画像数据库适配器；提交由调用它的业务服务负责 |
| `StoreBackend` | 框架提供的文件路径到 Store 的转换层 |
| `StateBackend` | 把会话文件放在图状态中，由 checkpointer 保存 |
| `PublishedChatCheckpoint` | 业务确认可以作为下一次运行起点的存档指针；不是最新失败分支 |

`ProfileStore` 有两种显式构造方式，因此不能把整个类叫成 `MySQLProfileStore`。读取路径 `/memories/profile.md` 保持不变；路径不是数据库表名或电脑上真实文件。

工厂中的局部变量分别叫 `model_client`、`profile_store_router`、`file_backend` 和 `chat_graph`，不再用几个含义过宽的 `model/store/backend/agent` 来解释本项目角色。

## 后台画像整理

- `MemoryConsolidationState`：后台整理的排队、活动时间和重试状态，不是框架会话 State。
- `MemoryConsolidationJob` / `ClaimedConsolidationJob`：持久化任务记录 / 已领取任务资料。
- `record_consolidation_activity`：登记活跃时间和待整理来源，不是“已经保存画像”。

## 预算与本地快照字段

- `AgentInputPolicy.admission_input_budget_tokens`：扣除输出、框架、工具结果和安全预留后的准入预算，不是模型厂家声明的 context window，也不是最终请求可占用的全部额度。
- `estimated_local_input_tokens`：准入阶段（或兼容流程最近一次计算）的本地保守估算；刷新画像后不自动重算。不是完整恢复后的模型请求 token 数或计费数，最终请求仍独立检查预算。
- `included_history_turns` / `input_history_trimmed`：只描述快照自带的消息列表；正式准入通常为 `0/False`，不表示会话没有历史。旧的独立记忆流程仍使用这些字段。
- `to_langchain_messages()`：仅转换本地消息格式，不恢复 checkpoint，也不调用模型。

## 必须保留的名字

框架规定的 `model=`、`backend=`、`store=`、`memory=`、`checkpointer=`、`context_schema=`、`context=`、`batch/abatch` 和中间件钩子不能按本项目偏好随意改名。左边保留框架参数，右边写具体对象，例如：

```python
model=model_client,
backend=file_backend,
store=profile_store_router,
context_schema=ChatRunDependencies,
```

同名替换标识 `MemoryMiddleware` / `SummarizationMiddleware`、状态键 `messages/files/memory_contents`、`thread_id/checkpoint_id`、虚拟路径和模型工具名也保留。数据库表/列、HTTP/SSE 字段没有因这次命名修改而变化。

例如 SSE 的 `context_ready` 是历史协议名：目前表示准入问题与画像已准备好，不代表完整历史已恢复。当前执行器的注释已明确这个边界。网页在收到 `message_done` 后使用本地“已保存/同步历史”状态，不再错误显示仍在保存。

## 前端和审核页

- `intermediateAssistantMessages` 是工具调用前的助手文字，不是工具名、参数或执行结果。
- “推理片段”是保存的 reasoning 文本片段编号，不宣称它等于模型真实思维步骤；公开响应的 `steps/step` 字段保留。
- 审核页 `no_rule_flags` 表示未命中当前检查规则，不保证页面没有问题。
- `page_decision_count` 只统计有整页决定的页数，不代表所有元素均审核完或 OCR/Vision 已完成。

本次修改不新增模型调用、迁移或部署。具体检查结果见 [命名核查计划](naming-audit-plan.md)。
