# 会话状态与上下文：整改后的运行方式

## 谁负责什么

```text
HTTP / CLI：身份、消息幂等、额度、同会话串行
  → 查本会话已发布 checkpoint_id
  → Deep Agents 图（每个会话独立缓存，可回收）
      LangGraph State：messages / files / 原生摘要事件
      原生 checkpointer：逐步持久化到 MySQL
      CompositeBackend
        默认：StateBackend，会话文件与历史归档
        /memories/：StoreBackend，只读画像快照
        /skills/：只读教学技能
  → 回答成功：一个业务事务提交回答 + 新的已发布 checkpoint_id
```

`ChatRunDependencies` 仅携带本轮依赖、只读画像和工具执行边界，不再负责保存会话历史。图缓存没有用户历史副本，回收缓存不会删除 checkpoint。

职责划分依据：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 区分会话 checkpoint 与跨会话 Store；[Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends) 提供 StateBackend、StoreBackend 及 CompositeBackend 路由。

正常调用只提交当前 `HumanMessage`，其 ID 由数据库问题 ID 生成。`thread_id` 由后端已鉴权的用户和会话生成，前端不能自由选择。第一次使用旧会话时，从成功的历史记录初始化 checkpoint 一次；没有供应商协议的早期回答以不可信历史引用保留，不编造思考字段。

## 失败不是“自动恢复所有副作用”

产品仍然是失败后由用户明确点击重试，不自动继续产生费用。失败尝试的 checkpoint 会保留，但不发布。重试从上一次成功的 checkpoint 开始，而不是从 `latest` 读取失败半轮。

数据库 `chat_thread_heads` 是发布指针，不是自制 checkpoint 引擎。它与最终回答在同一业务事务提交，解决框架存储和业务消息表不能天然跨事务原子提交的问题。

`memory_write_receipts` 按用户问题 ID 保存已确认的显式画像写入，与画像、版本更新同事务提交。同一个问题重试不能再次写入；如果重试产生不同提案，返回之前的回执和当前事实，不谎称新提案已经保存。

启动仍将未结算生成标记为中断。不要调用 `ainvoke(None)` 直接恢复这些旧业务尝试；完整中断点恢复需要另一套经过验收的业务恢复入口。

## 长上下文

- 正式聊天使用 Deep Agents 原生 `SummarizationMiddleware`；不再用 `DisableSummarizationMiddleware` 关闭它。
- 当前应用窗口为 65,536，摘要触发计数为 `policy.admission_input_budget_tokens`（默认 45,056），保留近期预算最多 8,192。计数是保守 UTF-8 估算，不是计费 token 数。
- 摘要另用一个关闭 thinking、无工具、无自动重试、输出至多 2,048 tokens 的模型请求。因此四次回答模型调用限制不包含这次内部摘要；它仍受整轮总超时限制。
- 摘要内容、摘要请求的 token 不能混入对外回复或“模型思考”流。
- 历史归档在会话 StateBackend 中，长期画像不会被历史文件覆盖。最终模型请求仍执行包含工具定义、框架提示和记忆的预算检查。
- 首次导入的旧历史可能大于普通聊天预算，摘要请求单独受已配置的供应商容量上限保护；超过该限制会失败，不会悄悄丢掉旧记录。超大型旧会话需要另行分批迁移。

不是全仓库所有 `checkpointer=None` 都机械替换：`check.py` 是一次性连通性探针，`consolidation.py` 是独立、输入有界的后台画像提取任务；它们不承担多轮会话。旧 `memory_workflow.py` 目前没有被正式 HTTP/CLI 聊天执行链路调用。本次必须持久化的是 `factory.py` 创建的正式聊天图，该入口已经强制要求 checkpointer。

## 存储依赖与部署

使用 `langgraph-checkpoint-mysql[asyncmy,pymysql]==3.0.0`：这是社区维护的 LangGraph MySQL 集成，不是 LangChain 官方的 MySQL saver。应用不自行实现其 SQL、序列化或 checkpoint 执行协议。运行使用 AsyncMySaver；建表使用包提供的同步 setup。

沿用现有 DB_* 配置和同一个 MySQL，不需要增加数据库凭据。当前适配器检查 MySQL >=8.4 且 <9.6。序列化禁用 pickle，使用框架安全类型白名单。

部署前需要备份，并在维护窗口停止旧后端后执行：

```sh
cd backend
uv sync --locked
uv run --no-sync python -m app.db.migrate
```

这条命令在已有运行锁内执行 Alembic，再执行存储库自己的 checkpoint migrations。启动只检查，不自动建表。新增两张业务表及存储库自己的四张 checkpoint 表；不要只运行 Alembic 后跳过原生 saver setup。

本次开发没有执行上述命令连接现有本地或线上应用数据库，也没有部署。

## 尚未声称解决的事项

- 单实例运行锁仍保留；多 worker、多副本和零停机滚动部署尚不支持。
- checkpoint 与归档会增长，尚未启用自动清理。未来清理必须保留已发布指针、活动分支和必要恢复点。
- 图缓存默认上限 128 个会话，闲置超过 30 分钟可在后续查找时回收；已运行的图引用不会被主动关闭。
- 旧代码回滚不能自动删除新存储。业务迁移的 downgrade 明确拒绝，需要备份与数据一致性方案。

## 验证方式

离线测试使用显式注入的 InMemorySaver 和假模型，不访问模型 API。真实持久化验收在一次性 MySQL 测试容器及随机测试数据库完成，使用固定测试凭据，不读取项目 .env。

相关测试：`test_conversation_context.py`、`test_chat_checkpoints.py`、`test_checkpoint_storage.py`、`test_checkpoint_lifecycle.py`、`test_checkpoint_mysql_integration.py`、`test_checkpoint_execution_mysql.py`、`test_memory_write_receipt.py`、`test_memory_receipt_mysql.py`，以及执行器、准入、迁移、取消和 SSE 流式协议的原有测试。

2026-10-25 验收：全量发现 573 项，556 项通过、17 项数据库用例在默认运行中跳过；这 17 项已在隔离数据库单独补跑且全部通过。另 20 项准入/执行兼容脚本用例通过。Ruff、格式、应用类型和 diff 空白检查通过。没有进行线上迁移、浏览器端上线联调或真实模型效果评测。
