"""回答前的独立、非思考记忆阶段；不把工具轨迹混入历史回答或可见思考。"""

import asyncio
from dataclasses import replace
import json

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_deepseek import ChatDeepSeek

from app.agent.budget_middleware import AgentToolUseNotAllowedError, NoAutomaticSummary
from app.agent.input_policy import estimate_message_tokens, estimate_text_tokens
from app.agent.memory_backend import ProfileMemoryBackend
from app.agent.output import managed_agent_stream
from app.agent.profile_memory_tool import (
    BoundMemoryTool, MEMORY_EXTRACTION_PROMPT, TOOL_NAME, contains_sensitive_memory,
)
from app.core.async_work import complete_in_thread
from app.services.agent_input import _profile_memory
from app.services.errors import ContextTooLargeError

MEMORY_STAGE_TIMEOUT_SECONDS = 25


class MemoryToolOnlyMiddleware(AgentMiddleware):
    """隐藏并拒绝所有框架自带工具，每次请求复核实际提示、工具定义及消息预算。"""

    def __init__(self, tool, policy):
        self.tool, self.policy = tool, policy

    def request(self, request):
        request = request.override(tools=[self.tool], tool_choice=None)
        messages = ([request.system_message] if request.system_message else []) + request.messages
        size = estimate_text_tokens(json.dumps({
            "messages": [message.model_dump(mode="json", exclude_none=True) for message in messages],
            "tools": [convert_to_openai_tool(self.tool)],
        }, ensure_ascii=False)) + 32 * len(messages) + 256
        if size + 2048 + self.policy.safety_tokens > self.policy.app_context_tokens:
            raise ContextTooLargeError()
        return request

    @staticmethod
    def check(response):
        calls = []
        for message in response.result:
            if isinstance(message, AIMessage):
                if message.response_metadata.get("finish_reason") not in (None, "stop", "tool_calls"):
                    raise AgentToolUseNotAllowedError()
                if message.invalid_tool_calls:
                    raise AgentToolUseNotAllowedError()
                calls.extend(message.tool_calls)
                if message.additional_kwargs.get("tool_calls") and not message.tool_calls:
                    raise AgentToolUseNotAllowedError()
        if len(calls) > 1 or any(call["name"] != TOOL_NAME for call in calls):
            raise AgentToolUseNotAllowedError()
        return response

    def wrap_model_call(self, request, handler):
        return self.check(handler(self.request(request)))

    async def awrap_model_call(self, request, handler):
        return self.check(await handler(self.request(request)))

    def wrap_tool_call(self, request, handler):
        # 工具只提供 async 实现，禁止同步绕过线程/取消管理。
        raise AgentToolUseNotAllowedError()

    async def awrap_tool_call(self, request, handler):
        if request.tool_call["name"] != TOOL_NAME:
            raise AgentToolUseNotAllowedError()
        return await handler(request)


def build_memory_agent(settings, prepared, bound):
    # 不带旧 assistant 消息/推理；关闭思考的工具阶段，无需伪造或持久化 reasoning_content。
    model = ChatDeepSeek(
        model=settings.name, api_key=settings.api_key, api_base=settings.base_url,
        max_tokens=2048, timeout=20, max_retries=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return create_deep_agent(
        model=model, system_prompt=MEMORY_EXTRACTION_PROMPT,
        tools=[bound.tool], subagents=[], skills=None,
        backend=ProfileMemoryBackend(prepared.profile_memory),
        middleware=[NoAutomaticSummary(), ModelCallLimitMiddleware(run_limit=1, exit_behavior="error"),
                    MemoryToolOnlyMiddleware(bound.tool, prepared.policy)],
        checkpointer=None, store=None, name="profile_memory_update",
    )


def with_memory_result(prepared, profile, status, topics=()):
    """新记忆占据更多上下文时，只移除最早的完整问答；当前问题始终保留。"""
    if status == "saved":
        note = "Backend confirmed saving these memory topics this turn: " + ", ".join(topics) + "."
    elif status == "unconfirmed":
        note = "Memory update could not be confirmed this turn. Do not claim success or guaranteed rollback."
    else:
        note = "No new memory save was confirmed this turn. Do not claim to have saved a new memory."
    policy = replace(prepared.policy, system_prompt=prepared.policy.system_prompt + "\n" + note)
    messages = list(prepared.messages)

    def size():
        return (estimate_text_tokens(policy.system_prompt) + estimate_text_tokens(profile)
                + estimate_text_tokens(policy.tool_definitions_json) + 256
                + sum(estimate_message_tokens(message.content) for message in messages))

    trimmed = False
    while size() > policy.input_limit and len(messages) > 1:
        del messages[:2]
        trimmed = True
    if size() > policy.input_limit:
        raise ContextTooLargeError()
    return replace(prepared, policy=policy, profile_memory=profile, messages=tuple(messages),
                   estimated_input_tokens=size(), history_rounds=(len(messages) - 1) // 2,
                   history_truncated=prepared.history_truncated or trimmed)


async def prepare_memory_for_turn(accepted, factory, registry, settings, progress):
    """最多一次记忆模型请求/一次批量写入；失败不盲目重试，不阻止正常聊天。"""
    prepared = accepted.prepared_input
    bound = BoundMemoryTool(accepted, factory, registry, progress)
    profile = prepared.profile_memory
    await progress("memory_checking")
    try:
        if contains_sensitive_memory(bound.question):
            bound.status = "rejected"
        else:
            # progress 的消费者异常不可当成可忽略的记忆错误，延后到外层统一传播。
            async with asyncio.timeout(MEMORY_STAGE_TIMEOUT_SECONDS) as deadline:
                agent = build_memory_agent(settings, prepared, bound)
                stream = agent.astream(
                    {"messages": [HumanMessage(content=json.dumps({
                        "existing_profile": prepared.profile_memory, "current_user_message": bound.question,
                    }, ensure_ascii=False))]},
                    config={"run_name": "profile_memory_update", "recursion_limit": 20,
                            "metadata": {"attempt_id": accepted.attempt_id}},
                    stream_mode="messages", subgraphs=False,
                )
                async with managed_agent_stream(stream):
                    async for _chunk, _metadata in stream:
                        # 记忆阶段的正文/参数不展示为答案或思考，但依旧遵守取消。
                        if deadline.expired():
                            raise TimeoutError
                        if asyncio.current_task().cancelling():
                            raise asyncio.CancelledError
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # 事件发送故障必须传播，否则会对已经断开的客户端继续调用模型。
        from app.services.chat_execution import EventDeliveryError
        if isinstance(error, EventDeliveryError):
            raise
        if bound.status != "saved":
            bound.status = "unconfirmed"
    finally:
        bound.close()
    if bound.status == "saved":
        try:
            def reload_profile():
                with factory() as session:
                    return _profile_memory(session, int(prepared.user_id))
            profile = await complete_in_thread(reload_profile)
        except Exception:
            # 已确认保存，但读回失败。仍可回答，不伪造新的 Profile 正文。
            pass
    await progress("memory_saved" if bound.status == "saved" else "memory_unavailable"
                   if bound.status == "unconfirmed" else "memory_skipped")
    return with_memory_result(prepared, profile, bound.status, bound.saved_topics)
