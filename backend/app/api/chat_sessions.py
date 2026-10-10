"""会话与历史 HTTP 接口；没有发送消息或调用模型的逻辑。"""

from fastapi import APIRouter, Request

from app.api.dependencies import CurrentUser
from app.api.security import ResourceRoute
from app.core.async_work import complete_in_thread
from app.schemas import (
    CreateSessionRequest, ErrorResponse, MessageHistoryResponse, RenameSessionRequest,
    SessionListResponse, SessionResponse,
)
from app.schemas._types import DatabaseID
from app.services.chat_sessions import create_chat_session, list_chat_sessions, rename_chat_session
from app.services.message_history import get_message_history

router = APIRouter(
    prefix="/api/v1/chat/sessions", tags=["Chat sessions"], route_class=ResourceRoute,
    responses={status: {"model": ErrorResponse} for status in (401, 403, 404, 413, 415, 422, 500, 503)},
)


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: CreateSessionRequest, request: Request, user: CurrentUser):
    # body 必须是 {}，只用于校验；会话主人来自已经验证的 Cookie。
    factory = request.app.state.runtime.session_factory
    return await complete_in_thread(lambda: create_chat_session(user, factory))


@router.get("", response_model=SessionListResponse)
async def list_sessions(request: Request, user: CurrentUser):
    factory = request.app.state.runtime.session_factory
    return await complete_in_thread(lambda: list_chat_sessions(user, factory))


@router.patch("/{session_id}", response_model=SessionResponse)
async def rename_session(
    session_id: DatabaseID, body: RenameSessionRequest, request: Request, user: CurrentUser,
):
    factory = request.app.state.runtime.session_factory
    return await complete_in_thread(lambda: rename_chat_session(user, session_id, body, factory))


@router.get("/{session_id}/messages", response_model=MessageHistoryResponse)
async def history(session_id: DatabaseID, request: Request, user: CurrentUser):
    runtime = request.app.state.runtime
    # 必须使用应用共享登记，否则会把正在执行的对话错误地判成空闲。
    # 锁和数据库读取整体在线程中执行，不阻塞异步服务器。
    return await complete_in_thread(lambda: get_message_history(
        user, session_id, runtime.session_factory, runtime.registry,
    ))
