"""Read saved reasoning for an owned successful answer; no writes or model requests."""

from langchain_core.messages import AIMessage, messages_from_dict
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.orchestrator import validate_protocol
from app.models import AgentTurnRecord, ChatSession, Message
from app.schemas import UserResponse
from app.schemas._validation import format_database_id
from app.schemas.reasoning import MessageReasoningResponse, ReasoningStep
from app.services.errors import MessageNotFoundError, MessageReasoningUnavailableError


def extract_reasoning(records: list) -> list[ReasoningStep]:
    # Only parse the two persisted message types; never return raw serialized messages.
    if not isinstance(records, list) or any(
        not isinstance(item, dict) or item.get("type") not in {"ai", "tool"} for item in records
    ):
        raise ValueError("Invalid saved protocol")
    messages = messages_from_dict(records)
    validate_protocol(messages)
    steps = []
    for message in messages:
        if isinstance(message, AIMessage):
            text = message.additional_kwargs["reasoning_content"]
            if text.strip():
                steps.append(ReasoningStep(step=len(steps) + 1, text=text))
    return steps


def get_message_reasoning(
    current_user: UserResponse,
    session_id: str,
    message_id: str,
    session_factory: sessionmaker[Session],
) -> MessageReasoningResponse:
    chat_id = int(format_database_id(session_id, "Session ID"))
    answer_id = int(format_database_id(message_id, "Message ID"))
    user_id = int(current_user.user_id)
    try:
        with session_factory() as session:
            owned = session.scalar(
                select(ChatSession.chat_session_id).where(
                    ChatSession.chat_session_id == chat_id, ChatSession.user_id == user_id
                )
            )
            if owned is None:
                raise MessageNotFoundError()
            answer = session.get(Message, answer_id)
            if answer is None or answer.chat_session_id != chat_id or answer.role != "ASSISTANT":
                raise MessageNotFoundError()
            question = session.get(Message, answer.in_reply_to_message_id)
            if (
                question is None
                or question.chat_session_id != chat_id
                or question.role != "USER"
                or question.generation_status != "SUCCEEDED"
            ):
                raise MessageNotFoundError()
            record = session.get(AgentTurnRecord, question.message_id)
            if record is not None and record.user_id != user_id:
                raise MessageNotFoundError()
            steps = [] if record is None else extract_reasoning(record.messages)
            return MessageReasoningResponse(
                message_id=message_id, available=bool(steps), steps=steps
            )
    except SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError:
        raise MessageReasoningUnavailableError() from None
