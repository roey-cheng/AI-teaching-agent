"""正式聊天 Agent 的创建入口；不读取 .env、不调用模型、不写数据库。"""

import json
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context

from app.agent.budget_middleware import ProfilePromptBudgetMiddleware
from app.agent.conversation_context import build_summary_middleware
from app.agent.input_policy import AgentInputPolicy, policy_for_model
from app.agent.orchestrator import ORCHESTRATOR_PROMPT, TeachingChatMiddleware, memory_tools
from app.agent.profile_store import PROFILE_MEMORY_PATH
from app.agent.run_context import (
    ChatRunDependencies,
    ProfileSnapshotStoreRouter,
    require_chat_run_dependencies,
)
from app.agent.teaching_skill import SKILLS_ROOT
from app.core.config import ModelSettings
from app.schemas._validation import format_database_id

type ChatGraph = CompiledStateGraph[Any, ChatRunDependencies | None, Any, Any]


def compile_chat_graph(
    settings: ModelSettings,
    policy: AgentInputPolicy | None = None,
    *,
    checkpointer: BaseCheckpointSaver,
    enable_chat_tools: bool = True,
) -> ChatGraph:
    """编译可复用的聊天图，不执行模型，也不绑定某次生成尝试的数据。

    返回的是 Deep Agents 编译好的图，而不是回答。chat_execution 执行器负责 astream、
    总超时、async_agent_tracing、取消、最终保存和运行登记收尾，不能直接作为公开接口。
    """
    policy = policy if policy is not None else policy_for_model(settings.name)
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TypeError("A runtime-owned persistent checkpointer is required")
    if settings.name != policy.model_name:
        raise ValueError("Chat policy and model client must use the same model")
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
    from app.agent.deepseek_protocol import ReasoningReplayChatDeepSeek

    model_client_class = ReasoningReplayChatDeepSeek if enable_chat_tools else ChatDeepSeek
    model_client = model_client_class(
        model=settings.name,
        api_key=settings.api_key,
        base_url=settings.base_url,
        max_tokens=policy.output_tokens,
        timeout=60,  # 限制一次模型api请求的等待时间
        max_retries=0,
        reasoning_effort="low",
        extra_body={"thinking": {"type": "enabled"}},  # 启用thinking设置
    )
    # 这里只创建转发器；用户画像快照由每次运行的 ChatRunDependencies 提供。
    profile_store_router = ProfileSnapshotStoreRouter()
    file_backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(
                store=profile_store_router,
                namespace=lambda runtime: (
                    require_chat_run_dependencies(runtime).profile_snapshot_store.namespace
                ),
            ),
            SKILLS_ROOT: FilesystemBackend(
                root_dir=Path(__file__).parent / "skills", virtual_mode=True
            ),
        },
    )

    # 各中间件扩展不同的状态字段，Deep Agents 会合并它们。
    # 只放宽这里的状态类型；列表元素仍必须是 AgentMiddleware。
    middleware: list[AgentMiddleware[Any, Any]] = [
        build_summary_middleware(model_client, file_backend, policy),
        ModelCallLimitMiddleware(
            run_limit=4 if enable_chat_tools else 1, exit_behavior="error"
        ),  # 限制 Agent 调用大语言模型的次数，防止无限循环和费用失控
        ProfilePromptBudgetMiddleware(file_backend, policy, run_scoped=True),
    ]
    if enable_chat_tools:
        middleware[-1] = TeachingChatMiddleware(file_backend, policy)
    # 创建不是执行，不记录包含模型配置的工厂参数。真正执行时用 app.agent.tracing.agent_tracing。
    with tracing_context(enabled=False):
        chat_graph = create_deep_agent(
            model=model_client,
            system_prompt=policy.system_prompt + (ORCHESTRATOR_PROMPT if enable_chat_tools else ""),
            tools=memory_tools() if enable_chat_tools else [],
            subagents=[],
            skills=[SKILLS_ROOT] if enable_chat_tools else None,
            memory=[PROFILE_MEMORY_PATH],
            backend=file_backend,
            permissions=[
                FilesystemPermission(
                    operations=["write"], paths=["/memories/**", "/skills/**"], mode="deny"
                ),
            ],
            middleware=middleware,
            checkpointer=checkpointer,  # 会话状态由 LangGraph 按 thread_id 持久化。
            context_schema=ChatRunDependencies,  # 框架 context 参数承载运行依赖，不是模型历史。
            store=profile_store_router,  # 左侧为框架参数；右侧为本项目的画像快照转发器。
            name="teaching_chat",
        )
    # 中间件只开放画像工具和受路径约束的技能/会话归档读取，不开放默认子 Agent。
    # 工厂只创建；执行器按会话 thread_id 提交新消息，历史由 checkpointer 恢复。
    return chat_graph


class SessionChatGraphCache:
    """按已鉴权的用户/会话缓存编译后的图，不缓存聊天历史或生成尝试。

    The application/CLI owns the cache, not a shared graph. Config changes
    require restarting that runtime. Entries contain no turn or history.
    """

    def __init__(
        self,
        checkpointer: BaseCheckpointSaver,
        *,
        max_cached_sessions: int = 128,
        idle_ttl_seconds: float = 1800,
        clock=monotonic,
    ):
        if not isinstance(checkpointer, BaseCheckpointSaver):
            raise TypeError("A runtime-owned persistent checkpointer is required")
        if max_cached_sessions <= 0 or idle_ttl_seconds <= 0:
            raise ValueError("Graph cache limits must be positive")
        self._checkpointer = checkpointer
        self._max_cached_sessions = max_cached_sessions
        self._idle_ttl_seconds = idle_ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._session_graphs: OrderedDict[tuple[str, str], tuple[ChatGraph, float]] = OrderedDict()
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
            now = self._clock()
            expired = [
                entry_key
                for entry_key, (_, touched) in self._session_graphs.items()
                if now - touched >= self._idle_ttl_seconds
            ]
            for entry_key in expired:
                del self._session_graphs[entry_key]
            cached = self._session_graphs.pop(key, None)
            if cached is None:
                chat_graph = compile_chat_graph(settings, policy, checkpointer=self._checkpointer)
                self._settings = settings.model_copy(deep=True)
                self._policy = policy
            else:
                chat_graph = cached[0]
            self._session_graphs[key] = (chat_graph, now)
            while len(self._session_graphs) > self._max_cached_sessions:
                # Evict only the cache reference, never close/cancel an active graph.
                # Execution retains its own reference; state lives in the checkpointer.
                self._session_graphs.popitem(last=False)
            return chat_graph
