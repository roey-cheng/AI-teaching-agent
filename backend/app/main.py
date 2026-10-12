"""FastAPI 入口；启动核对后接受全部业务请求，关闭时等待流式生成清理。"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.accounts import router as accounts_router
from app.api.chat_messages import router as messages_router
from app.api.chat_sessions import router as sessions_router
from app.api.errors import RequestContextMiddleware, install_error_handlers
from app.api.memory import router as memory_router
from app.api.security import AccountRateLimiter
from app.core.async_work import complete_before_cancelling, complete_in_thread
from app.core.config import MemorySettings, load_database_settings, load_turnstile_settings
from app.core.http_config import HTTPSettings
from app.core.runtime import BackendRuntime, open_backend_runtime
from app.db.engine import build_database_engine
from app.db.runtime_guard import DatabaseRuntimeLockError
from app.services.errors import StartupCleanupError
from app.services.turnstile import TurnstileVerifier, TurnstileVerifierProtocol

logger = logging.getLogger(__name__)


@contextmanager
def configured_runtime():
    # import app 不读取 .env、不连接数据库；只有真正启动应用时才做这些事。
    try:
        engine = build_database_engine(load_database_settings())
    except Exception:  # noqa: BLE001 -- 启动配置和建引擎失败统一脱敏，避免驱动异常暴露凭据。
        raise StartupCleanupError() from None
    try:
        with open_backend_runtime(engine) as runtime:
            yield runtime
    finally:
        engine.dispose()


def create_app(
    *,
    runtime_factory: Callable[[], AbstractContextManager[BackendRuntime]] | None = None,
    http_settings: HTTPSettings | None = None,
    turnstile_verifier: TurnstileVerifierProtocol | None = None,
    memory_settings: MemorySettings | None = None,
) -> FastAPI:
    """测试可注入独立运行环境，避免访问开发者 .env 和项目数据库。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.http_settings = (
            http_settings if http_settings is not None else HTTPSettings.from_sources()
        )
        application.state.account_limiter = AccountRateLimiter()
        application.state.turnstile_verifier = turnstile_verifier or TurnstileVerifier(
            load_turnstile_settings()
        )
        application.state.active_stream_requests = set()
        application.state.stopping = False
        scope = (runtime_factory or configured_runtime)()
        entered = False
        memory_task = None

        def enter():
            nonlocal entered
            runtime = scope.__enter__()
            entered = True
            return runtime

        try:
            # 取消也必须等数据库线程返回，再退出作用域释放进程锁。
            runtime = await complete_in_thread(enter)
            application.state.runtime = runtime
            memory_config = (
                memory_settings if memory_settings is not None else MemorySettings.from_sources()
            )
            if memory_config.background_enabled:
                from app.agent.consolidation import memory_worker

                memory_task = asyncio.create_task(memory_worker(runtime, memory_config))
            logger.info(
                "Startup reconciliation complete: %s interrupted, %s restored successful.",
                runtime.cleanup.interrupted,
                runtime.cleanup.restored_success,
            )
            yield  # 执行到这里后，服务器才开始正常接受 HTTP 请求。
        finally:
            # 即使服务器先触发 shutdown，也须等待 SSE 执行器完成取消/提交核对，
            # 之后才能释放数据库资源和同项目进程锁。
            application.state.stopping = True
            try:
                if memory_task is not None:
                    memory_task.cancel()
                    await complete_before_cancelling(
                        asyncio.gather(memory_task, return_exceptions=True)
                    )
                streams = tuple(application.state.active_stream_requests)
                for task in streams:
                    task.cancel()
                if streams:
                    await complete_before_cancelling(
                        asyncio.gather(*streams, return_exceptions=True)
                    )
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

    @application.get("/ready")
    async def ready():
        """平台就绪检查：数据库可查询，而且本进程仍持有生产运行锁。"""

        if application.state.stopping or not hasattr(application.state, "runtime"):
            return JSONResponse({"status": "not_ready"}, status_code=503)

        def check():
            runtime = application.state.runtime
            runtime.database_guard.require_held()
            with runtime.session_factory() as session:
                if session.scalar(text("SELECT 1")) != 1:
                    raise RuntimeError("Database readiness query failed")

        try:
            await complete_in_thread(check)
        except DatabaseRuntimeLockError, SQLAlchemyError, RuntimeError:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    return application


app = create_app()
