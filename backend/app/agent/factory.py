"""正式聊天 Agent 的创建入口；不读取 .env、不调用模型、不写数据库。"""

import json
from threading import Lock
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import StoreBackend
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_deepseek import ChatDeepSeek
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context

from app.agent.budget_middleware import ChatMemoryBudgetMiddleware, NoAutomaticSummary
from app.agent.input_policy import AgentInputPolicy, policy_for_model
from app.agent.orchestrator import ORCHESTRATOR_PROMPT, OrchestratorMiddleware, memory_tools
from app.agent.profile_store import PROFILE_MEMORY_PATH
from app.agent.run_context import ChatRunContext, RunScopedStore, require_run_context
from app.agent.teaching_skill import SKILLS_ROOT
from app.core.config import ModelSettings
from app.schemas._validation import format_database_id

type ChatGraph = CompiledStateGraph[Any, ChatRunContext | None, Any, Any]


def build_chat_agent(
    settings: ModelSettings,
    policy: AgentInputPolicy | None = None,
    *,
    with_memory_tools: bool = True,
) -> ChatGraph:
    """编译可复用的图，只绑定模型配置与固定策略，不接收用户/记忆/turn。

    返回的是 Deep Agents 编译好的图，而不是回答。chat_execution 执行器负责 astream、
    总超时、async_agent_tracing、取消、最终保存和运行登记收尾，不能直接作为公开接口。
    """
    policy = policy if policy is not None else policy_for_model(settings.name)
    if settings.name != policy.model_name:
        raise ValueError("Prepared input and chat model must use the same model")
    supported = policy_for_model(settings.name)
    if (
        policy.model_context_tokens > supported.model_context_tokens
        or policy.model_max_output_tokens > supported.model_max_output_tokens
    ):
        raise ValueError("Configured limits exceed the verified model limits")
    if json.loads(policy.tool_definitions_json) != []:
        raise ValueError("This chat agent requires server-owned tools")

    # 正式聊天启用 API 可见推理；输出额度由思考和正文共同使用，不自动重试。
    # 60 秒是 SDK 超时，不是整个业务总超时。终端 check.py 仍是原来的无思考探针。
    from app.agent.deepseek_protocol import ProtocolChatDeepSeek

    model_class = ProtocolChatDeepSeek if with_memory_tools else ChatDeepSeek
    model = model_class(
        model=settings.name,
        api_key=settings.api_key,
        base_url=settings.base_url,
        max_tokens=policy.output_tokens,
        timeout=60,
        max_retries=0,
        reasoning_effort="low",
        extra_body={"thinking": {"type": "enabled"}},
    )
    # A stateless router, not a cached user's Store. Each invocation supplies its
    # own snapshot through context=ChatRunContext(...).
    store = RunScopedStore()
    backend = StoreBackend(
        store=store, namespace=lambda runtime: require_run_context(runtime).store.namespace
    )

    # 各中间件扩展不同的状态字段，Deep Agents 会合并它们。
    # 只放宽这里的状态类型；列表元素仍必须是 AgentMiddleware。
    middleware: list[AgentMiddleware[Any, Any]] = [
        NoAutomaticSummary(),
        ModelCallLimitMiddleware(
            run_limit=4 if with_memory_tools else 1, exit_behavior="error"
        ),  # 限制 Agent 调用大语言模型的次数，防止无限循环和费用失控
        ChatMemoryBudgetMiddleware(backend, policy, run_scoped=True),
    ]
    if with_memory_tools:
        middleware[-1] = OrchestratorMiddleware(backend, policy)
    # 创建不是执行，不记录包含模型配置的工厂参数。真正执行时用 app.agent.tracing.agent_tracing。
    with tracing_context(enabled=False):
        agent = create_deep_agent(
            model=model,
            system_prompt=policy.system_prompt + (ORCHESTRATOR_PROMPT if with_memory_tools else ""),
            tools=memory_tools() if with_memory_tools else [],
            subagents=[],
            skills=[SKILLS_ROOT] if with_memory_tools else None,
            memory=[PROFILE_MEMORY_PATH],
            backend=backend,
            middleware=middleware,
            checkpointer=None,  # 流程不需要；每次运行的状态仍独立。
            context_schema=ChatRunContext,
            store=store,
            name="teaching_chat",
        )
    # 中间件只开放本轮记忆工具和受路径约束的技能读取，不开放默认子 Agent。
    # 工厂只创建；chat_execution 使用 TurnMemory 的完整协议历史真正运行。
    return agent


class ChatAgentProvider:
    """Cache a separate graph for each verified (user, conversation) pair.

    The application/CLI owns the cache, not a shared graph. Config changes
    require restarting that runtime. Entries contain no turn or history.
    """

    def __init__(self):
        self._lock = Lock()
        self._agents: dict[tuple[str, str], ChatGraph] = {}
        self._settings = None
        self._policy = None

    def get(
        self,
        settings: ModelSettings,
        policy: AgentInputPolicy,
        *,
        user_id: str | int,
        session_id: str | int,
    ) -> ChatGraph:
        # Callers must complete ownership checks before looking up the graph.
        key = (
            format_database_id(user_id, "User ID"),
            format_database_id(session_id, "Session ID"),
        )
        with self._lock:
            if self._settings is not None and (
                self._settings != settings or self._policy != policy
            ):
                raise ValueError("Chat configuration changed; restart the backend runtime")
            agent = self._agents.get(key)
            if agent is None:
                agent = build_chat_agent(settings, policy)
                self._settings = settings.model_copy(deep=True)
                self._policy = policy
                self._agents[key] = agent
            return agent
