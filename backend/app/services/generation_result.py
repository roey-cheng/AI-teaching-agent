"""生成结果的短事务：匹配当前尝试，原子保存最终回答与状态；不调用模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Message
from app.schemas import AssistantMessageResponse, GenerationError
from app.services.errors import MessageSendUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.message_submission import AcceptedMessage, _owned_chat

PUBLIC_GENERATION_ERRORS = {
    "GENERATION_TIMEOUT": "Response generation timed out. Please try again.",
    "GENERATION_INTERRUPTED": "Response generation was interrupted. Please try again.",
    "MODEL_REQUEST_FAILED": "Failed to generate a response. Please try again later.",
}


class StaleGenerationError(Exception):
    """失效尝试不能保存结果或改变新尝试状态。"""

    def __init__(self):
        super().__init__("This generation is no longer active.")


@dataclass(frozen=True)
class GenerationOutcome:
    assistant: AssistantMessageResponse | None = None
    error: GenerationError | None = None


def settle_generation(
    accepted: AcceptedMessage, session_factory: sessionmaker[Session], registry: GenerationRegistry,
    *, answer: str | None = None, error_code: str | None = None,
) -> GenerationOutcome:
    """answer 与 error_code 二选一。已成功的提交可核对返回，最终状态不会被覆盖。

    本函数不释放占用；调用方必须等模型和线程结束，再退出消息准入作用域。
    提交异常不在这里重试；执行器用新的失败结算事务锁父行，核对旧事务实际结果。
    """
    if (answer is None) == (error_code is None):
        raise ValueError("Provide either an answer or an error code")
    if answer is not None and not answer.strip():
        raise ValueError("Final answer must not be blank")
    if error_code is not None and error_code not in PUBLIC_GENERATION_ERRORS:
        raise ValueError("Unknown public generation error")
    chat_id = int(accepted.session_id)
    if accepted.prepared_input.session_id != accepted.session_id:
        raise StaleGenerationError()
    try:
        with registry.locked(chat_id) as slot:
            if slot.attempt_id != accepted.attempt_id:
                raise StaleGenerationError()
            with session_factory.begin() as session:
                chat = _owned_chat(session, chat_id, int(accepted.prepared_input.user_id))
                question = session.scalar(select(Message).where(
                    Message.message_id == int(accepted.user_message_id),
                    Message.chat_session_id == chat_id,
                ).with_for_update())
                if question is None or question.role != "USER" or question.attempt_id != accepted.attempt_id:
                    raise StaleGenerationError()
                saved = session.scalar(select(Message).where(Message.in_reply_to_message_id == question.message_id))
                if saved is not None and (saved.role != "ASSISTANT" or saved.chat_session_id != chat_id):
                    raise MessageSendUnavailableError()
                if question.generation_status == "SUCCEEDED":
                    if saved is None:
                        raise MessageSendUnavailableError()
                    return GenerationOutcome(assistant=AssistantMessageResponse.model_validate(saved))
                if saved is not None:
                    raise MessageSendUnavailableError()
                if question.generation_status == "FAILED":
                    if answer is not None:
                        raise StaleGenerationError()
                    code = question.generation_error_code
                    return GenerationOutcome(error=GenerationError(
                        code=code if code in PUBLIC_GENERATION_ERRORS else "MODEL_REQUEST_FAILED",
                        message=PUBLIC_GENERATION_ERRORS.get(code, PUBLIC_GENERATION_ERRORS["MODEL_REQUEST_FAILED"]),
                    ))
                if question.generation_status != "RUNNING":
                    raise StaleGenerationError()
                now = datetime.now(UTC).replace(tzinfo=None)
                question.updated_at = now
                if answer is not None:
                    saved = Message(
                        chat_session_id=chat_id, role="ASSISTANT", content=answer,
                        in_reply_to_message_id=question.message_id,
                        model_key=accepted.prepared_input.policy.model_name,
                        created_at=now, updated_at=now,
                    )
                    session.add(saved)
                    question.generation_status = "SUCCEEDED"
                    question.generation_error_code = question.generation_error_message = None
                    chat.updated_at = chat.last_activity_at = now
                    session.flush()
                    outcome = GenerationOutcome(assistant=AssistantMessageResponse.model_validate(saved))
                else:
                    question.generation_status = "FAILED"
                    question.generation_error_code = error_code
                    question.generation_error_message = PUBLIC_GENERATION_ERRORS[error_code]
                    session.flush()
                    outcome = GenerationOutcome(error=GenerationError(
                        code=error_code, message=PUBLIC_GENERATION_ERRORS[error_code],
                    ))
            # with begin 退出且提交成功后，才把结果交回执行器。
            return outcome
    except (SQLAlchemyError, ValidationError):
        raise MessageSendUnavailableError() from None
