"""HTTP 来源与输入边界检查、账号单进程限流；不承担密码验证。"""

from collections import deque
from hashlib import sha256
import math
from threading import Lock
import time

from fastapi import Request
from fastapi.routing import APIRoute
from pydantic import SecretStr

from app.api.errors import APIError

COOKIE_NAME = "chat_session"


class AccountRateLimiter:
    def __init__(self, *, clock=time.monotonic, max_keys=10000):
        self.clock = clock
        self.max_keys = max_keys
        self.entries = {}
        self.lock = Lock()

    def consume(self, kind: str, identity: str, window: int):
        # 不在限流表里保留邮箱/地址明文；重启重置，符合单进程第一版约定。
        key = (kind, sha256(identity.encode()).digest())
        with self.lock:
            now = self.clock()
            for old_key, (old_window, times) in list(self.entries.items()):
                while times and times[0] <= now - old_window:
                    times.popleft()
                if not times:
                    del self.entries[old_key]
            if key not in self.entries:
                if len(self.entries) >= self.max_keys:
                    self.reject(60)  # 不通过淘汰仍有效的计数绕过限制。
                self.entries[key] = (window, deque())
            times = self.entries[key][1]
            if len(times) >= 10:
                self.reject(max(1, math.ceil(times[0] + window - now)))
            times.append(now)

    @staticmethod
    def reject(seconds):
        raise APIError(429, "RATE_LIMITED", "Too many requests. Please try again later.",
                       headers={"Retry-After": str(seconds)})


def cookie_token(request: Request) -> SecretStr | None:
    raw = request.cookies.get(COOKIE_NAME)
    return SecretStr(raw) if raw else None


def check_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in request.app.state.http_settings.allowed_origins:
        raise APIError(403, "ORIGIN_NOT_ALLOWED", "Request origin is not allowed.")


def reject_query_parameters(request: Request) -> None:
    if request.query_params:
        raise APIError(422, "VALIDATION_ERROR", "Query parameters are not accepted here.")


def require_json(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise APIError(415, "UNSUPPORTED_MEDIA_TYPE", "Use application/json.")


async def read_small_body(request: Request, *, allow_body: bool, max_bytes: int = 16384) -> None:
    # 默认用于账号/会话元信息；聊天路由显式传入更大的正文上限。
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise APIError(413, "REQUEST_TOO_LARGE", "Request body exceeds the endpoint size limit.")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not allow_body and body:
        raise APIError(422, "VALIDATION_ERROR", "This endpoint does not accept a request body.")
    # 缓存受限的正文，让 FastAPI 后续仍能读取同一份请求。
    request._body = body


class AccountRoute(APIRoute):
    """先检查来源/体积/限流，再交给 FastAPI 解析 JSON 和 Pydantic Schema。"""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request: Request):
            if request.method == "POST":
                check_origin(request)
            reject_query_parameters(request)
            name = request.url.path.rsplit("/", 1)[-1]
            if request.method == "POST" and name in {"login", "register"}:
                # 不直接信任 X-Forwarded-For；反向代理信任范围需由部署配置限制。
                ip = request.client.host if request.client else "unknown"
                request.app.state.account_limiter.consume(name + "_ip", ip, 60 if name == "login" else 3600)
                require_json(request)
            await read_small_body(request, allow_body=name not in {"logout", "me"})
            return await original(request)

        return guarded


class ResourceRoute(APIRoute):
    """会话、历史与记忆接口边界；认证另由共享依赖执行。"""

    max_body_bytes = 16384

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request: Request):
            write = request.method in {"POST", "PATCH"}
            if write:
                check_origin(request)
                require_json(request)
            reject_query_parameters(request)
            await read_small_body(request, allow_body=write, max_bytes=self.max_body_bytes)
            return await original(request)

        return guarded


class ChatStreamRoute(ResourceRoute):
    # 20,000 字符即使用 JSON 的代理对转义也能装下；Schema 仍检查字符数。
    max_body_bytes = 262144
