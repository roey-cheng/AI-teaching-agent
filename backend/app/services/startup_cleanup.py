"""进程启动时核对残留 RUNNING；不重新运行 Agent，不删除消息，不修改最终状态。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.runtime_lock import RuntimeLock
from app.models import ChatSession, Message
from app.schemas import AssistantMessageResponse
from app.schemas._types import UUIDString
from app.services.errors import StartupCleanupError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import PUBLIC_GENERATION_ERRORS

_attempt_id = TypeAdapter(UUIDString)


@dataclass(frozen=True)
class StartupCleanupResult:
    interrupted: int = 0
    restored_success: int = 0


def reconcile_interrupted_generations(
    session_factory: sessionmaker[Session], registry: GenerationRegistry, *, runtime_lock: RuntimeLock,
) -> StartupCleanupResult:
    """仅在启动入口调用，取得进程锁且旧进程已退出后、接受业务请求前执行。

    本版数据量小，所有待修复行在同一事务内核对/提交；异常则停止启动。
    不改 attempt_id、前驱编号、正文、创建时间、标题、活动排序、登录或记忆。
    提交确认丢失时可能已经落库，不声称回滚，不自动重试；下次启动重新核对。
    """
    runtime_lock.require_held()
    try:
        with registry.startup_cleanup():
            interrupted = restored = 0
            with session_factory.begin() as session:
                chat_ids = session.scalars(select(Message.chat_session_id).where(
                    Message.generation_status == "RUNNING",
                ).distinct().order_by(Message.chat_session_id)).all()
                now = datetime.now(UTC).replace(tzinfo=None)
                for chat_id in chat_ids:
                    # 与正常写入相同：先锁父会话，再锁问题和回复；不凭旧 SELECT 快照更新。
                    chat = session.scalar(select(ChatSession).where(
                        ChatSession.chat_session_id == chat_id,
                    ).with_for_update())
                    if chat is None:
                        raise StartupCleanupError()
                    questions = session.scalars(select(Message).where(
                        Message.chat_session_id == chat_id, Message.generation_status == "RUNNING",
                    ).order_by(Message.message_id).with_for_update()).all()
                    for question in questions:
                        if question.role != "USER" or not question.attempt_id:
                            raise StartupCleanupError()
                        _attempt_id.validate_python(question.attempt_id)
                        if question.retry_of_attempt_id is not None:
                            _attempt_id.validate_python(question.retry_of_attempt_id)
                        answers = session.scalars(select(Message).where(
                            Message.in_reply_to_message_id == question.message_id,
                        ).with_for_update()).all()
                        if len(answers) > 1:
                            raise StartupCleanupError()
                        if answers:
                            answer = answers[0]
                            if (answer.role != "ASSISTANT" or answer.chat_session_id != chat_id
                                    or not answer.content.strip()):
                                raise StartupCleanupError()
                            AssistantMessageResponse.model_validate(answer)
                            question.generation_status = "SUCCEEDED"
                            question.generation_error_code = question.generation_error_message = None
                            restored += 1
                        else:
                            question.generation_status = "FAILED"
                            question.generation_error_code = "GENERATION_INTERRUPTED"
                            question.generation_error_message = PUBLIC_GENERATION_ERRORS["GENERATION_INTERRUPTED"]
                            interrupted += 1
                        question.updated_at = now
                session.flush()
                runtime_lock.require_held()
            return StartupCleanupResult(interrupted, restored)  # 提交返回成功后才报告。
    except (SQLAlchemyError, ValidationError, RuntimeError):
        raise StartupCleanupError() from None
