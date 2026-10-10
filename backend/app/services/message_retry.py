"""失败重试的准入：复用原 USER 行，领取新执行编号；不在这里调用模型。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.input_policy import AgentInputPolicy
from app.models import Message
from app.schemas import DuplicateMessageResponse, RetryMessageRequest, SendMessageRequest, UserResponse
from app.schemas._validation import format_database_id
from app.services.agent_input import prepare_agent_input
from app.services.errors import (
    MessageNotFoundError, MessageSendUnavailableError, RetryNotAllowedError,
    SessionBusyError, StaleAttemptError,
)
from app.services.generation_registry import GenerationRegistry
from app.services.message_submission import AcceptedMessage, _answer_id, _owned_chat, _settle_interrupted


@contextmanager
def accept_failed_message_retry(
    current_user: UserResponse, session_id: str, message_id: str, request: RetryMessageRequest,
    session_factory: sessionmaker[Session], registry: GenerationRegistry, *,
    check_retry: Callable[[RetryMessageRequest], None], input_policy: AgentInputPolicy,
) -> Iterator[AcceptedMessage | DuplicateMessageResponse]:
    """调用方先认证；必须与新发送共享 registry、每用户生成限流器及执行作用域。

    重复请求先查回执，真正的新重试才消耗额度。同步锁/数据库放在线程中运行。
    失败提交独立核对：可能仍是旧 FAILED，也可能本次 RUNNING 已经提交。
    """
    chat_id = int(format_database_id(session_id, "Session ID"))
    question_id = int(format_database_id(message_id, "Message ID"))
    user_id = int(current_user.user_id)
    request = RetryMessageRequest.model_validate(request.model_dump())
    owned_attempt = None
    client_key = None

    def settle():
        _settle_interrupted(
            user_id, chat_id, client_key, owned_attempt, session_factory, registry,
            previous_attempt_id=request.failed_attempt_id,
        )

    try:
        with registry.locked(chat_id) as slot:
            with session_factory.begin() as session:
                chat = _owned_chat(session, chat_id, user_id)
                question = session.scalar(select(Message).where(
                    Message.chat_session_id == chat_id, Message.message_id == question_id,
                ).with_for_update())
                if question is None:
                    raise MessageNotFoundError()
                if question.role != "USER":
                    raise RetryNotAllowedError()
                answer_id = _answer_id(session, question)
                # 即使新重试仍在运行、已失败/成功或之后发了新问题，也只返回这一跳的回执。
                if question.retry_of_attempt_id == request.failed_attempt_id:
                    if question.generation_status == "RUNNING" and slot.attempt_id != question.attempt_id:
                        raise MessageSendUnavailableError()  # 不冒充已经恢复了重启残留任务。
                    result = DuplicateMessageResponse(
                        duplicate=True, session_id=chat_id, user_message_id=question_id,
                        attempt_id=question.attempt_id, status=question.generation_status,
                        assistant_message_id=answer_id,
                    )
                else:
                    if question.attempt_id != request.failed_attempt_id:
                        raise StaleAttemptError()
                    if slot.attempt_id is not None:
                        raise SessionBusyError()
                    orphan = session.scalar(select(Message.message_id).where(
                        Message.chat_session_id == chat_id, Message.generation_status == "RUNNING",
                    ).limit(1))
                    if orphan is not None:
                        raise MessageSendUnavailableError()
                    latest_id = session.scalar(select(Message.message_id).where(
                        Message.chat_session_id == chat_id, Message.role == "USER",
                    ).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1))
                    if question.generation_status != "FAILED" or latest_id != question_id:
                        raise RetryNotAllowedError()
                    # 内容、原发送键均来自已验证的数据库行，前端不能替换问题或指定用户。
                    original = SendMessageRequest(client_message_key=question.client_message_key, content=question.content)
                    prepared = prepare_agent_input(
                        session, current_user, str(chat_id), original, input_policy,
                        retry_message_id=str(question_id),
                    )
                    check_retry(request.model_copy(deep=True))
                    client_key = question.client_message_key
                    owned_attempt = str(uuid4())
                    if not slot.claim(owned_attempt):
                        owned_attempt = None
                        raise SessionBusyError()
                    now = datetime.now(UTC).replace(tzinfo=None)
                    question.attempt_id = owned_attempt
                    question.retry_of_attempt_id = request.failed_attempt_id
                    question.generation_status = "RUNNING"
                    question.generation_error_code = question.generation_error_message = None
                    question.updated_at = now
                    chat.updated_at = chat.last_activity_at = now
                    session.flush()
                    result = AcceptedMessage(str(chat_id), str(question_id), owned_attempt, prepared)
    except BaseException as error:
        if owned_attempt is not None:
            settle()
        if isinstance(error, (SQLAlchemyError, ValidationError)):
            raise MessageSendUnavailableError() from None
        raise

    # 事务和短锁已结束，再把执行凭据交给共享的 Agent 执行器。
    try:
        yield result
    finally:
        if owned_attempt is not None:
            settle()
