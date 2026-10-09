"""只读会话历史；不发送消息、不运行 Agent、不修复数据库状态。"""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models import ChatSession, Message
from app.schemas import (
    AssistantMessageResponse,
    GenerationError,
    GenerationResponse,
    MessageHistoryResponse,
    UserMessageResponse,
    UserResponse,
)
from app.schemas._validation import format_database_id
from app.services.errors import MessageHistoryUnavailableError, SessionNotFoundError
from app.services.generation_registry import GenerationRegistry

# 只发布白名单错误摘要，绝不把数据库中的原始异常、URL 或凭据直接发给前端。
_PUBLIC_ERRORS = {
    "GENERATION_TIMEOUT": "Response generation timed out. Please try again.",
    "GENERATION_INTERRUPTED": "Response generation was interrupted. Please try again.",
    "MODEL_REQUEST_FAILED": "Failed to generate a response. Please try again later.",
}


def _assemble_history(chat_id: int, messages: list[Message], active_attempt: str | None) -> MessageHistoryResponse:
    users = [message for message in messages if message.role == "USER"]
    latest = users[-1] if users else None
    running = [message for message in users if message.generation_status == "RUNNING"]
    # RUNNING 必须是最后一个问题且与当前登记吻合；不能把重启残留当成仍在执行。
    if running and (len(running) != 1 or running[0] is not latest
                    or running[0].attempt_id != active_attempt):
        raise MessageHistoryUnavailableError()
    # 已保存最终状态但清理未结束时，登记仍保留，同样算忙碌。
    if active_attempt is not None and (latest is None or latest.attempt_id != active_attempt):
        raise MessageHistoryUnavailableError()
    busy = active_attempt is not None
    answers = {}
    for message in messages:
        if message.role == "ASSISTANT":
            if message.in_reply_to_message_id in answers:
                raise MessageHistoryUnavailableError()
            answers[message.in_reply_to_message_id] = message.message_id
    items = []
    for message in messages:
        if message.role == "ASSISTANT":
            items.append(AssistantMessageResponse.model_validate(message))
        elif message.role == "USER":
            error = None
            if message.generation_status == "FAILED":
                code = message.generation_error_code
                if code not in _PUBLIC_ERRORS:
                    code = "GENERATION_FAILED"
                error = GenerationError(code=code, message=_PUBLIC_ERRORS.get(
                    code, "Response generation failed. Please try again later."))
            items.append(UserMessageResponse(
                message_id=message.message_id, role="USER", content=message.content,
                in_reply_to_message_id=None, created_at=message.created_at,
                generation=GenerationResponse(
                    attempt_id=message.attempt_id, status=message.generation_status,
                    assistant_message_id=answers.get(message.message_id), error=error,
                    can_retry=not busy and message is latest and message.generation_status == "FAILED",
                ),
            ))
        else:
            raise MessageHistoryUnavailableError()
    # 校验成功问答配对，防止跨会话引用、缺失回答或矛盾状态被静默输出。
    return MessageHistoryResponse(session_id=chat_id, is_generating=busy, items=items)


def get_message_history(
    current_user: UserResponse, session_id: str, session_factory: sessionmaker[Session],
    registry: GenerationRegistry,
) -> MessageHistoryResponse:
    """身份由调用方验证。registry 必须与未来发送/重试/结算共享，不能默认造空登记。"""
    chat_id = int(format_database_id(session_id, "Session ID"))
    try:
        # 先拿短锁，再新开数据库 Session，避免等待后沿用旧事务快照。
        # 未来所有消息写入必须在同一把锁中提交；长时间的模型调用不持此锁。
        with registry.locked(chat_id) as slot:
            with session_factory() as session:
                owned_id = session.scalar(select(ChatSession.chat_session_id).where(
                    ChatSession.chat_session_id == chat_id,
                    ChatSession.user_id == int(current_user.user_id),
                ))
                if owned_id is None:
                    raise SessionNotFoundError()
                messages = session.scalars(select(Message).where(Message.chat_session_id == chat_id)
                                           .order_by(Message.created_at.asc(), Message.message_id.asc())).all()
                response = _assemble_history(chat_id, messages, slot.attempt_id)
        return response
    except (SQLAlchemyError, ValidationError):
        # 数据库故障或损坏的历史快照都不能返回假空列表/不一致的成功响应。
        raise MessageHistoryUnavailableError() from None
