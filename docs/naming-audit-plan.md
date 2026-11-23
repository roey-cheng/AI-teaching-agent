# 命名语义核查与修改计划

日期：2026-11-23。范围：代码命名、解释性注释、当前文档和对应测试。不改变数据库结构、公开 HTTP/SSE 协议、模型工具名或虚拟路径，不部署。

状态：已完成并通过下述检查。改名对应的职责说明见 [代码命名说明](code-naming-guide.md)。

## 命名原则

- 名称应区分：会话、一次生成尝试、模型请求、跨会话画像、数据库存储和内存快照。
- `context` 只在框架规定的接口或确实表示上下文时使用；应用注入的数据采用明确的依赖/画像/准入名称。
- 编译图不叫“执行聊天”；图缓存不叫“Agent 列表”；转发器不叫“存储本体”。
- 不把“未命中检测规则”写成“无异常”，不把助手中间文字写成“工具执行步骤”。
- 保留 LangGraph/Deep Agents 的参数、钩子、状态键、同名中间件替换标识和已持久化的工具名称。

## 实施顺序

- [x] 统一内部类/函数名称、更新全部调用点与测试。
- [x] 明确 factory 局部变量和运行依赖字段，解释只读快照与持久化存储的区别。
- [x] 修正后台画像整理状态、已发布 checkpoint 指针等名称。
- [x] 修正前端与课件审核页的误导名称和文案。
- [x] 更新当前架构文档、增加术语表，保留历史设计文档的历史语境。
- [x] 完成后端测试、前端测试/构建、导入器测试、格式和类型检查。

## 主要映射

| 原名 | 新名 | 实际职责 |
| --- | --- | --- |
| `build_chat_agent` | `compile_chat_graph` | 编译图，不执行聊天 |
| `ChatAgentProvider` | `SessionChatGraphCache` | 每个用户/会话独立缓存图 |
| `ChatRunContext` | `ChatRunDependencies` | 本次图运行注入的依赖，不是模型上下文窗口 |
| `RunScopedStore` | `ProfileSnapshotStoreRouter` | 转发到当前运行的只读画像 Store |
| `TurnMemory` | `ChatAttemptState` | 一次生成尝试的调用计数、工具权限、协议消息 |
| `PreparedAgentInput` | `ChatAdmissionSnapshot` | 准入时的问题、画像和预算，不是最终模型请求 |
| `OrchestratorMiddleware` | `TeachingChatMiddleware` | 教学聊天的工具限制、协议收集和预算检查 |
| `ChatMemoryBudgetMiddleware` | `ProfilePromptBudgetMiddleware` | 画像注入后检查提示预算 |
| `ProtocolChatDeepSeek` | `ReasoningReplayChatDeepSeek` | 回传已记录的供应商 reasoning 字段 |
| `ChatThreadHead` | `PublishedChatCheckpoint` | 下一次允许使用的 checkpoint 指针，不是最新任意分支 |

更多局部字段与后台任务映射在完成后的术语表中说明。

## 兼容边界

`store`、`backend`、`checkpointer`、`context_schema`、`context` 等左侧框架参数保持不变；`messages`、`files`、`thread_id`、`checkpoint_id`、数据库表/列、HTTP 字段、SSE 事件及已存在的工具名保持不变。

`ProfileStore` 既支持事务内数据库访问，也支持只读快照，不将整个类误改名为 `MySQLProfileStore`；用明确的构造方法和变量名区分两种模式。

## 验收结果

- 后端全量发现 574 项：545 项离线通过，29 项默认跳过的数据库测试已在独立 MySQL 容器完整补跑并全部通过。
- 数据库补验组成：聊天执行 6 项、画像回执/事务 5 项、原生存档 1 项、画像业务/迁移 17 项。只使用本地已有测试镜像和随机测试库，未读取应用 `.env` 或访问应用数据库；测试库及一次性容器已清理。
- 新增测试确认 Python 模型类改名后数据库表名/列名不变。
- 前端 `npm run build`：21 项测试、TypeScript 检查、Vite 构建全部通过。
- 课件导入器：24 项测试通过；审核页 JavaScript 语法、Ruff 及格式检查通过。未执行课件 OCR、视觉模型或重新解析。
- 后端 Ruff、格式、`basedpyright app`、`git diff --check` 通过。独立复查确认框架参数、替换标识、工具名、checkpoint 键和虚拟路径保持不变。
- 没有调用真实模型、迁移应用数据库、commit/push 或部署。保留原有未提交改动。

这次没有把所有旧通用变量强行换成长名字，也没有改写历史设计中的旧架构；修改的是已确认会误导职责/生命周期/数据含义的名称。框架规定名和公开协议保留，并补充解释。
