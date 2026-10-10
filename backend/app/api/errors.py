"""统一安全错误；不回传原始请求、SQL、密码或异常文本。"""

from http import HTTPStatus
import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.schemas.error import ErrorDetail, ErrorResponse
from app.services import errors

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers=None):
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def error_response(request: Request, status: int, code: str, message: str, headers=None):
    request_id = getattr(request.state, "request_id", "req_" + uuid4().hex)
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, request_id=request_id))
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)


def install_error_handlers(app: FastAPI):
    @app.exception_handler(APIError)
    async def api_error(request, error):
        return error_response(request, error.status, error.code, error.message, error.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI 默认的 errors() 包含 input；密码校验失败时也不能回显。
        return error_response(request, 422, "VALIDATION_ERROR", "Invalid request fields or JSON body.")

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(request, error.status_code, "HTTP_ERROR",
                              HTTPStatus(error.status_code).phrase, error.headers)

    mapping = {
        errors.EmailAlreadyRegisteredError: 409,
        errors.InvalidCredentialsError: 401,
        errors.AuthenticationRequiredError: 401,
        errors.RegistrationUnavailableError: 503,
        errors.LoginUnavailableError: 503,
        errors.AuthenticationUnavailableError: 503,
        errors.LogoutUnavailableError: 503,
        errors.SessionNotFoundError: 404,
        errors.SessionUnavailableError: 503,
        errors.MessageHistoryUnavailableError: 503,
        errors.MemoryUnavailableError: 503,
        errors.MessageNotFoundError: 404,
        errors.SessionBusyError: 409,
        errors.IdempotencyConflictError: 409,
        errors.RetryNotAllowedError: 409,
        errors.StaleAttemptError: 409,
        errors.ContextTooLargeError: 422,
        errors.MessageSendUnavailableError: 503,
        errors.AgentInputUnavailableError: 503,
    }

    async def business_error(request, error):
        # 使用我们定义的安全提示，不信任异常实例可能附带的文本。
        safe = type(error)()
        return error_response(request, mapping[type(error)], safe.code, str(safe))

    for error_type in mapping:
        app.add_exception_handler(error_type, business_error)

    @app.exception_handler(errors.GenerationRateLimitError)
    async def generation_limit(request, error):
        return error_response(request, 429, "RATE_LIMITED", "Too many new generations. Please wait before sending another message.",
                              {"Retry-After": str(error.retry_after_seconds)})


class RequestContextMiddleware:
    """纯 ASGI 中间件；不缓存响应，后续 SSE 也可逐段发送。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = "req_" + uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception:
            # 不向 Uvicorn 传播带 SQL/凭据的原始异常；取消不是 Exception，仍正常传播。
            logger.error("Unhandled HTTP error; request_id=%s", request_id)
            if started:
                raise RuntimeError("Response interrupted; internal details suppressed.") from None
            response = error_response(Request(scope), 500, "INTERNAL_ERROR", "An unexpected error occurred.")
            await response(scope, receive, safe_send)
