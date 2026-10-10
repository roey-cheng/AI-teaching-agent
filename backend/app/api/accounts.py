"""四个账号接口：读取 HTTP 输入 → 已有业务函数 → HTTP 响应。"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response

from app.api.security import AccountRoute, COOKIE_NAME, cookie_token
from app.core.async_work import complete_in_thread
from app.schemas import LoginRequest, LoginResponse, RegisterRequest, RegisterResponse, UserResponse
from app.schemas.error import ErrorResponse
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.login import login_user
from app.services.logout import logout_user

router = APIRouter(
    prefix="/api/v1", tags=["Accounts"], route_class=AccountRoute,
    # 覆盖 FastAPI 默认的 422 文档，实际返回的是项目统一错误外壳。
    responses={status: {"model": ErrorResponse} for status in (401, 403, 409, 413, 415, 422, 429, 500, 503)},
)


@router.post("/auth/register", response_model=RegisterResponse, status_code=201)
async def register(body: RegisterRequest, request: Request):
    factory = request.app.state.runtime.session_factory
    # 整段同步业务放到线程：密码哈希和 MySQL 不阻塞异步事件循环。
    return await complete_in_thread(lambda: register_user(body, factory))


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    request.app.state.account_limiter.consume("login_email", str(body.email), 60)
    factory = request.app.state.runtime.session_factory
    token = cookie_token(request)
    result = await complete_in_thread(lambda: login_user(body, factory, current_token=token))
    # 业务已确认提交才设置 Cookie；原凭据只在响应头中，不进 JSON。
    response.set_cookie(
        key=COOKIE_NAME, value=result.token.get_secret_value(),
        max_age=max(0, int((result.expires_at - datetime.now(UTC)).total_seconds())),
        expires=result.expires_at, path="/", httponly=True, samesite="lax",
        secure=request.app.state.http_settings.cookie_secure,
    )
    return result.response


@router.post("/auth/logout", status_code=204, response_class=Response)
async def logout(request: Request):
    factory = request.app.state.runtime.session_factory
    token = cookie_token(request)
    await complete_in_thread(lambda: logout_user(token, factory))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax",
                           secure=request.app.state.http_settings.cookie_secure)
    return response


@router.get("/users/me", response_model=UserResponse)
async def me(request: Request):
    factory = request.app.state.runtime.session_factory
    token = cookie_token(request)
    return await complete_in_thread(lambda: get_current_user(token, factory))
