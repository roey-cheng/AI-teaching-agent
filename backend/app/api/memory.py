"""只读 Profile Memory；查询已保存的记忆，不调用 Agent 重新提取。"""

from fastapi import APIRouter, Request

from app.api.dependencies import CurrentUser
from app.api.security import ResourceRoute
from app.core.async_work import complete_in_thread
from app.schemas import ErrorResponse, MemoryListResponse
from app.services.profile_memory import list_profile_memory

router = APIRouter(
    prefix="/api/v1/me", tags=["Profile memory"], route_class=ResourceRoute,
    responses={status: {"model": ErrorResponse} for status in (401, 413, 422, 500, 503)},
)


@router.get("/memory", response_model=MemoryListResponse)
async def memory(request: Request, user: CurrentUser):
    factory = request.app.state.runtime.session_factory
    return await complete_in_thread(lambda: list_profile_memory(user, factory))
