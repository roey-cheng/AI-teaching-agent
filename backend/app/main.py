"""FastAPI 入口；启动核对后接受全部业务请求，关闭时等待流式生成清理。"""

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
import logging

from fastapi import FastAPI

from app.api.accounts import router as accounts_router
from app.api.chat_sessions import router as sessions_router
from app.api.chat_messages import router as messages_router
from app.api.memory import router as memory_router
from app.api.errors import RequestContextMiddleware, install_error_handlers
from app.api.security import AccountRateLimiter
from app.core.async_work import complete_before_cancelling, complete_in_thread
from app.core.config import load_database_settings
from app.core.http_config import HTTPSettings
from app.core.runtime import BackendRuntime, open_backend_runtime
from app.db.engine import build_database_engine
from app.services.errors import StartupCleanupError

logger = logging.getLogger(__name__)


@contextmanager
def configured_runtime():
    # import app 不读取 .env、不连接数据库；只有真正启动应用时才做这些事。
    try:
        engine = build_database_engine(load_database_settings())
    except Exception:
        raise StartupCleanupError() from None
    try:
        with open_backend_runtime(engine) as runtime:
            yield runtime
    finally:
        engine.dispose()


def create_app(
    *, runtime_factory: Callable[[], AbstractContextManager[BackendRuntime]] | None = None,
    http_settings: HTTPSettings | None = None,
) -> FastAPI:
    """测试可注入独立运行环境，避免访问开发者 .env 和项目数据库。"""
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.http_settings = http_settings if http_settings is not None else HTTPSettings()
        application.state.account_limiter = AccountRateLimiter()
        application.state.active_stream_requests = set()
        application.state.stopping = False
        scope = (runtime_factory or configured_runtime)()
        entered = False

        def enter():
            nonlocal entered
            runtime = scope.__enter__()
            entered = True
            return runtime

        try:
            # 取消也必须等数据库线程返回，再退出作用域释放进程锁。
            runtime = await complete_in_thread(enter)
            application.state.runtime = runtime
            logger.info("Startup reconciliation complete: %s interrupted, %s restored successful.",
                        runtime.cleanup.interrupted, runtime.cleanup.restored_success)
            yield  # 执行到这里后，服务器才开始正常接受 HTTP 请求。
        finally:
            # 即使服务器先触发 shutdown，也须等待 SSE 执行器完成取消/提交核对，
            # 之后才能释放数据库资源和同项目进程锁。
            application.state.stopping = True
            try:
                streams = tuple(application.state.active_stream_requests)
                for task in streams:
                    task.cancel()
                if streams:
                    await complete_before_cancelling(asyncio.gather(*streams, return_exceptions=True))
            finally:
                if hasattr(application.state, "runtime"):
                    del application.state.runtime
                if entered:
                    await complete_in_thread(lambda: scope.__exit__(None, None, None))

    application = FastAPI(title="AI Teaching Assistant", lifespan=lifespan)
    application.add_middleware(RequestContextMiddleware)
    install_error_handlers(application)
    application.include_router(accounts_router)
    application.include_router(sessions_router)
    application.include_router(messages_router)
    application.include_router(memory_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        """进程存活检查；启动已核对数据库，但本请求不检查数据库/模型持续可用性。"""
        return {"status": "ok"}

    return application


app = create_app()
