"""多个受保护接口共用的登录检查；身份不能由前端正文或查询参数指定。"""

from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie
from pydantic import SecretStr

from app.api.security import COOKIE_NAME
from app.core.async_work import complete_in_thread
from app.schemas import UserResponse
from app.services.authentication import get_current_user

session_cookie = APIKeyCookie(name=COOKIE_NAME, auto_error=False)


async def require_current_user(
    request: Request, token: Annotated[str | None, Security(session_cookie)],
) -> UserResponse:
    # auto_error=False：统一由现有业务返回 UNAUTHENTICATED，不用框架默认报错。
    secret = SecretStr(token) if token else None
    factory = request.app.state.runtime.session_factory
    return await complete_in_thread(lambda: get_current_user(secret, factory))


CurrentUser = Annotated[UserResponse, Depends(require_current_user)]
