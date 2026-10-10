"""正式聊天 Agent 的创建入口；不读取 .env、不调用模型、不写数据库。"""

import json

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_deepseek import ChatDeepSeek
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context

from app.agent.budget_middleware import ChatMemoryBudgetMiddleware, NoAutomaticSummary
from app.agent.input_policy import policy_for_model
from app.agent.memory_backend import PROFILE_MEMORY_PATH, ProfileMemoryBackend
from app.core.config import ModelSettings
from app.services.agent_input import PreparedAgentInput


def build_chat_agent(settings: ModelSettings, prepared_input: PreparedAgentInput) -> CompiledStateGraph:
    """每次生成单独创建，不缓存用户状态；仅供已经完成身份/会话校验的后端调用。

    返回的是 Deep Agents 编译好的图，而不是回答。chat_execution 执行器负责 astream、
    总超时、async_agent_tracing、取消、最终保存和运行登记收尾，不能直接作为公开接口。
    """
    policy = prepared_input.policy
    if settings.name != policy.model_name:
        raise ValueError("Prepared input and chat model must use the same model")
    supported = policy_for_model(settings.name)
    if (policy.model_context_tokens > supported.model_context_tokens
            or policy.model_max_output_tokens > supported.model_max_output_tokens):
        raise ValueError("Configured limits exceed the verified model limits")
    if prepared_input.memory_path != PROFILE_MEMORY_PATH or json.loads(policy.tool_definitions_json) != []:
        raise ValueError("This chat agent requires the standard memory path and no model tools")

    # 正式聊天启用 API 可见推理；输出额度由思考和正文共同使用，不自动重试。
    # 60 秒是 SDK 超时，不是整个业务总超时。终端 check.py 仍是原来的无思考探针。
    model = ChatDeepSeek(
        model=settings.name, api_key=settings.api_key, api_base=settings.base_url,
        max_tokens=policy.output_tokens, timeout=60, max_retries=0,
        reasoning_effort="low", extra_body={"thinking": {"type": "enabled"}},
    )
    backend = ProfileMemoryBackend(prepared_input.profile_memory)
    # 创建不是执行，不记录包含模型配置的工厂参数。真正执行时用 app.agent.tracing.agent_tracing。
    with tracing_context(enabled=False):
        agent = create_deep_agent(
            model=model, system_prompt=policy.system_prompt,
            tools=[], subagents=[], skills=None,
            memory=[PROFILE_MEMORY_PATH], backend=backend,
            middleware=[
                NoAutomaticSummary(),
                ModelCallLimitMiddleware(run_limit=1, exit_behavior="error"),
                ChatMemoryBudgetMiddleware(backend, policy),
            ],
            checkpointer=None, store=None, name="teaching_chat",
        )
    # subagents=[] 不保证框架不创建默认子 Agent；中间件双重阻止 task 等一切工具委派。
    # 工厂只创建；chat_execution 使用 prepared_input.as_agent_messages() 真正运行。
    return agent
