"""新消息准入与保存；不调用模型。使用 with 保证退出时结算本次未完成执行。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.input_policy import AgentInputPolicy
from app.models import ChatSession, Message
from app.schemas import DuplicateMessageResponse, SendMessageRequest, UserResponse
from app.schemas._validation import format_database_id
from app.services.errors import (
    IdempotencyConflictError,
    MessageSendUnavailableError,
    SessionBusyError,
    SessionNotFoundError,
)
from app.services.generation_registry import GenerationRegistry
from app.services.agent_input import PreparedAgentInput, prepare_agent_input


@dataclass(frozen=True)
class AcceptedMessage:
    """内部执行凭据，不是新的 API 响应 Schema，也不是已完成的 AI 回答。"""

    session_id: str
    user_message_id: str
    attempt_id: str
    prepared_input: PreparedAgentInput = field(repr=False)


def _owned_chat(session: Session, chat_id: int, user_id: int) -> ChatSession:
    chat = session.scalar(select(ChatSession).where(
        ChatSession.chat_session_id == chat_id, ChatSession.user_id == user_id,
    ).with_for_update())
    if chat is None:
        raise SessionNotFoundError()
    return chat


def _answer_id(session: Session, question: Message) -> int | None:
    answer = session.scalar(select(Message).where(Message.in_reply_to_message_id == question.message_id))
    if answer is not None and (answer.chat_session_id != question.chat_session_id or answer.role != "ASSISTANT"):
        raise MessageSendUnavailableError()
    if (question.generation_status == "SUCCEEDED") != (answer is not None):
        raise MessageSendUnavailableError()
    return answer.message_id if answer is not None else None


def _settle_interrupted(
    user_id: int, chat_id: int, client_key: str, attempt_id: str,
    session_factory: sessionmaker[Session], registry: GenerationRegistry,
) -> None:
    """只结算本次编号；也用于 INSERT 提交异常后的独立连接核对，不重发请求。"""
    try:
        with registry.locked(chat_id) as slot:
            if slot.attempt_id != attempt_id:
                return  # 迟到的清理绝不能动新执行。
            with session_factory.begin() as session:
                # 重新锁住父会话行，等之前不确定的事务完成，再读取当前提交结果。
                _owned_chat(session, chat_id, user_id)
                question = session.scalar(select(Message).where(
                    Message.chat_session_id == chat_id, Message.client_message_key == client_key,
                ).with_for_update())
                if question is not None:
                    if question.role != "USER" or question.attempt_id != attempt_id:
                        raise MessageSendUnavailableError()
                    _answer_id(session, question)  # 不覆盖已成功的回复，矛盾状态则保留占用待核对。
                    if question.generation_status == "RUNNING":
                        question.generation_status = "FAILED"
                        question.generation_error_code = "GENERATION_INTERRUPTED"
                        question.generation_error_message = "Response generation was interrupted. Please try again."
                        question.updated_at = datetime.now(UTC).replace(tzinfo=None)
                        session.flush()
            # 无记录=已确认未落库；有记录=最终状态已确认提交。此时才能释放。
            slot.release(attempt_id)
    except SQLAlchemyError:
        # 核对/结算失败时保留占用，不能误报空闲；后续恢复逻辑尚未接入。
        raise MessageSendUnavailableError() from None


@contextmanager
def accept_user_message(
    current_user: UserResponse, session_id: str, request: SendMessageRequest,
    session_factory: sessionmaker[Session], registry: GenerationRegistry, *,
    check_new_message: Callable[[SendMessageRequest], None],
    input_policy: AgentInputPolicy,
) -> Iterator[AcceptedMessage | DuplicateMessageResponse]:
    """仅供后端调用，使用 with；真实执行必须在作用域内完成，不能后台脱离它。

    input_policy 为后端配置，输入组装和预算检查在 USER 保存前完成。
    check_new_message 仍为必填的本地、快速准入检查（未来限流）；目前只注入测试替身。
    两者均不能调用模型/网络；生产接口不能把未实现的限流伪装成空检查。
    同步数据库与锁不能直接阻塞异步事件循环；chat_execution 在线程中进入/退出本作用域。
    HTTP/SSE 生命周期尚未接入。
    """
    chat_id = int(format_database_id(session_id, "Session ID"))
    user_id = int(current_user.user_id)
    # 保存一份校验后的输入快照，调用方后续修改原请求不会改变清理使用的消息键。
    request = SendMessageRequest.model_validate(request.model_dump())
    owned_attempt = None
    result = None
    try:
        with registry.locked(chat_id) as slot:
            with session_factory.begin() as session:
                chat = _owned_chat(session, chat_id, user_id)
                existing = session.scalar(select(Message).where(
                    Message.chat_session_id == chat_id,
                    Message.client_message_key == request.client_message_key,
                ))
                if existing is not None:
                    if existing.role != "USER":
                        raise MessageSendUnavailableError()
                    if existing.content != request.content:
                        raise IdempotencyConflictError()
                    if existing.generation_status == "RUNNING" and slot.attempt_id != existing.attempt_id:
                        raise MessageSendUnavailableError()
                    result = DuplicateMessageResponse(
                        duplicate=True, session_id=chat_id, user_message_id=existing.message_id,
                        attempt_id=existing.attempt_id, status=existing.generation_status,
                        assistant_message_id=_answer_id(session, existing),
                    )
                else:
                    if slot.attempt_id is not None:
                        raise SessionBusyError()
                    orphan = session.scalar(select(Message.message_id).where(
                        Message.chat_session_id == chat_id, Message.generation_status == "RUNNING",
                    ).limit(1))
                    if orphan is not None:
                        raise MessageSendUnavailableError()
                    prepared_input = prepare_agent_input(session, current_user, str(chat_id), request, input_policy)
                    check_new_message(request.model_copy(deep=True))  # 重复回执和忙碌拒绝不消耗新生成额度。
                    first_question = session.scalar(select(Message.message_id).where(
                        Message.chat_session_id == chat_id, Message.role == "USER",
                    ).limit(1)) is None
                    owned_attempt = str(uuid4())
                    if not slot.claim(owned_attempt):
                        owned_attempt = None
                        raise SessionBusyError()
                    now = datetime.now(UTC).replace(tzinfo=None)
                    question = Message(
                        chat_session_id=chat_id, role="USER", content=request.content,
                        client_message_key=request.client_message_key, attempt_id=owned_attempt,
                        generation_status="RUNNING", created_at=now, updated_at=now,
                    )
                    session.add(question)
                    if first_question and not chat.title_is_manual:
                        chat.title = request.content.strip().replace("\r\n", " ").replace("\r", " ").replace("\n", " ")[:30]
                    chat.updated_at = now
                    chat.last_activity_at = now
                    session.flush()
                    result = AcceptedMessage(str(chat_id), str(question.message_id), owned_attempt, prepared_input)
    except BaseException as error:
        # 不论是 flush 失败还是提交结果不明，先独立核对，不能直接释放本地占用。
        if owned_attempt is not None:
            _settle_interrupted(user_id, chat_id, request.client_message_key, owned_attempt, session_factory, registry)
        if isinstance(error, (SQLAlchemyError, ValidationError)):
            raise MessageSendUnavailableError() from None
        raise

    # 离开数据库事务和短锁之后才交给未来执行器；重复回执没有执行/清理所有权。
    try:
        yield result
    finally:
        if owned_attempt is not None:
            _settle_interrupted(user_id, chat_id, request.client_message_key, owned_attempt, session_factory, registry)
