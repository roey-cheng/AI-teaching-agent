"""发送与失败重试两个 POST：复用同一个执行器生命周期和 SSE 传输层。"""

from dataclasses import dataclass

from fastapi import APIRouter, Request, Response

from app.agent.input_policy import AgentInputPolicy, policy_for_model
from app.api.dependencies import CurrentUser
from app.api.errors import APIError
from app.api.security import ChatStreamRoute
from app.api.sse import ChatStreamResponse
from app.core.async_work import complete_in_thread
from app.core.config import ModelSettings, TracingSettings, load_model_settings, load_tracing_settings
from app.schemas import DuplicateMessageResponse, ErrorResponse, RetryMessageRequest, SendMessageRequest
from app.schemas._types import DatabaseID
from app.services.chat_execution import execute_chat_retry, execute_chat_turn


@dataclass(frozen=True)
class ChatConfiguration:
    model: ModelSettings
    tracing: TracingSettings
    policy: AgentInputPolicy


def load_chat_configuration() -> ChatConfiguration:
    # 发消息才读取；账号、历史等接口不依赖模型密钥。配置异常不回显密钥。
    try:
        model = load_model_settings()
        return ChatConfiguration(model, load_tracing_settings(), policy_for_model(model.name))
    except Exception:
        raise APIError(503, "MODEL_CONFIGURATION_UNAVAILABLE", "Chat model configuration is unavailable.") from None


router = APIRouter(
    prefix="/api/v1/chat/sessions", tags=["Chat streaming"], route_class=ChatStreamRoute,
    responses={
        **{status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 413, 415, 422, 429, 500, 503)},
        200: {"model": DuplicateMessageResponse, "description": "SSE for an accepted generation; JSON for a duplicate.",
              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
    },
)


def response_for(request: Request, user, session_id, body, *, message_id=None):
    if request.app.state.stopping:
        raise APIError(503, "SERVER_SHUTTING_DOWN", "The server is shutting down. Please try again later.")
    runtime = request.app.state.runtime

    async def execute(on_event):
        configuration = await complete_in_thread(load_chat_configuration)
        options = dict(
            model_settings=configuration.model, tracing_settings=configuration.tracing,
            input_policy=configuration.policy, on_event=on_event,
        )
        # 新发送/重试共用每用户限流器；只在业务确认是新执行后扣额度，重复回执不扣。
        check = lambda _body: runtime.limiter.check(user.user_id)
        if message_id is None:
            return await execute_chat_turn(user, session_id, body, runtime.session_factory, runtime.registry,
                                           check_new_message=check, **options)
        return await execute_chat_retry(user, session_id, message_id, body, runtime.session_factory, runtime.registry,
                                        check_retry=check, **options)

    return ChatStreamResponse(execute, request_id=request.state.request_id,
                              active_requests=request.app.state.active_stream_requests)


@router.post("/{session_id}/messages", response_class=Response)
async def send_message(session_id: DatabaseID, body: SendMessageRequest, request: Request, user: CurrentUser):
    return response_for(request, user, session_id, body)


@router.post("/{session_id}/messages/{message_id}/retry", response_class=Response)
async def retry_message(session_id: DatabaseID, message_id: DatabaseID, body: RetryMessageRequest,
                        request: Request, user: CurrentUser):
    return response_for(request, user, session_id, body, message_id=message_id)
