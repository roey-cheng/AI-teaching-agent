"""一次新消息的异步执行器；内部后端入口，不是 HTTP 接口，也不实现失败重试。"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from uuid import uuid4

from openai import APITimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.factory import build_chat_agent
from app.agent.input_policy import AgentInputPolicy
from app.agent.output import extract_visible_chunk, managed_agent_stream
from app.agent.tracing import async_agent_tracing
from app.core.async_work import complete_in_thread
from app.core.config import ModelSettings, TracingSettings
from app.schemas import (
    AgentProgressData, DuplicateMessageResponse, MessageDeltaData, MessageDoneData,
    MessageErrorData, MessageStartData, ReasoningDeltaData, SendMessageRequest, StreamError, UserResponse,
)
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import settle_generation
from app.services.message_submission import accept_user_message

ChatEvent = (MessageStartData | AgentProgressData | ReasoningDeltaData | MessageDeltaData
             | MessageDoneData | MessageErrorData)


class EventDeliveryError(Exception):
    """消费者断开/处理失败；不能继续假装完成或向坏掉的消费者再次发送。"""

    def __init__(self):
        super().__init__("Chat event delivery was interrupted.")


async def execute_chat_turn(
    current_user: UserResponse, session_id: str, request: SendMessageRequest,
    session_factory: sessionmaker[Session], registry: GenerationRegistry, *,
    model_settings: ModelSettings, tracing_settings: TracingSettings,
    input_policy: AgentInputPolicy, check_new_message: Callable[[SendMessageRequest], None],
    on_event: Callable[[ChatEvent], Awaitable[None]], timeout_seconds: float = 120,
) -> MessageDoneData | MessageErrorData | DuplicateMessageResponse:
    """调用方先认证，传应用共享 registry 和真实限流检查。严禁作为匿名公开模型代理。

    on_event 接收结构化事件；未来 SSE 层负责序列化，并在断线时取消本协程。
    不采用调用方可能遗弃的裸 async generator，不脱离请求偷偷继续生成。
    准入/结算错误向外抛安全业务异常；已接收的模型失败返回 message_error。
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Generation timeout must be positive and finite")
    if model_settings.name != input_policy.model_name:
        raise ValueError("Input policy and model must match")

    async def emit(event: ChatEvent):
        try:
            await on_event(event)
        except Exception:
            raise EventDeliveryError() from None

    # __enter__ 在线程中执行。即使取消发生在提交期间，也保留清理所需的进入结果。
    scope = accept_user_message(current_user, session_id, request, session_factory, registry,
                                check_new_message=check_new_message, input_policy=input_policy)
    entered = False

    def enter():
        nonlocal entered
        result = scope.__enter__()
        entered = True
        return result

    try:
        accepted = await complete_in_thread(enter)
        if isinstance(accepted, DuplicateMessageResponse):
            return accepted  # 不创建模型，不发送另一套流，不再消耗生成额度。

        async def progress(stage):
            await emit(AgentProgressData(attempt_id=accepted.attempt_id, stage=stage))

        try:
            async with asyncio.timeout(timeout_seconds) as deadline:
                await emit(MessageStartData(session_id=accepted.session_id,
                                            user_message_id=accepted.user_message_id, attempt_id=accepted.attempt_id))
                await progress("context_ready")  # 准入已完成历史与用户记忆快照的读取。
                answer_parts = []
                finish_reason = None
                seen_reasoning = seen_answer = False
                output_bytes = 0
                async with async_agent_tracing(tracing_settings):
                    agent = build_chat_agent(model_settings, accepted.prepared_input)
                    await progress("agent_running")
                    stream = agent.astream(
                        {"messages": accepted.prepared_input.as_agent_messages()},
                        config={"run_name": "teaching_chat", "metadata": {"attempt_id": accepted.attempt_id}},
                        stream_mode="messages", subgraphs=False,
                    )
                    async with managed_agent_stream(stream):
                        async for chunk, metadata in stream:
                            # 即便下游吞掉取消，也禁止继续输出/保存本次结果。
                            if deadline.expired():
                                raise TimeoutError
                            if asyncio.current_task().cancelling():
                                raise asyncio.CancelledError
                            visible = extract_visible_chunk(chunk, metadata)
                            output_bytes += len(visible.reasoning.encode("utf-8")) + len(visible.answer.encode("utf-8"))
                            # 额外内存保护，不冒充精确 token 计数；模型侧额度包含思考和正文。
                            if output_bytes > input_policy.output_tokens * 32:
                                raise ValueError("Model output exceeded the execution size limit")
                            if visible.reasoning:
                                if not seen_reasoning:
                                    await progress("thinking")
                                    seen_reasoning = True
                                await emit(ReasoningDeltaData(attempt_id=accepted.attempt_id, text=visible.reasoning))
                            if visible.answer:
                                if not seen_answer:
                                    await progress("answering")
                                    seen_answer = True
                                answer_parts.append(visible.answer)
                                await emit(MessageDeltaData(attempt_id=accepted.attempt_id, text=visible.answer))
                            if visible.finish_reason is not None:
                                finish_reason = visible.finish_reason
                # stop 片段还不算成功：必须等图完整结束，中间件可能在最后拒绝工具调用。
                answer = "".join(answer_parts)
                if finish_reason != "stop" or not answer.strip():
                    raise ValueError("The model did not produce a complete answer")
                if deadline.expired():
                    raise TimeoutError
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                await progress("saving")
                outcome = await complete_in_thread(lambda: settle_generation(
                    accepted, session_factory, registry, answer=answer,
                ))
        except (asyncio.CancelledError, EventDeliveryError):
            # finally 退出准入作用域：核对已提交成功的回复，否则标中断，不再发送事件。
            raise
        except Exception as error:
            code = "GENERATION_TIMEOUT" if isinstance(error, (TimeoutError, APITimeoutError)) else "MODEL_REQUEST_FAILED"
            # 新事务核对提交结果：如果成功提交但连接返回失败，保留成功，不写相反状态。
            outcome = await complete_in_thread(lambda: settle_generation(
                accepted, session_factory, registry, error_code=code,
            ))
    finally:
        if entered:
            # 先关闭模型流/跟踪并等在途线程，再核对数据库最终状态并匹配释放占用。
            await complete_in_thread(lambda: scope.__exit__(None, None, None))

    # 清理完成后才给调用方 terminal；若此时连接断开，数据库结果仍可由历史接口确认。
    if outcome.assistant is not None:
        terminal = MessageDoneData(attempt_id=accepted.attempt_id, assistant_message=outcome.assistant)
    else:
        terminal = MessageErrorData(attempt_id=accepted.attempt_id, error=StreamError(
            code=outcome.error.code, message=outcome.error.message, request_id=f"req_{uuid4().hex}",
        ))
    await emit(terminal)
    return terminal
