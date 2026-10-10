"""FastAPI 入口；先完成启动核对，再接受请求。业务路由尚未接入。"""

from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
import logging

from fastapi import FastAPI

from app.core.async_work import complete_in_thread
from app.core.config import load_database_settings
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
) -> FastAPI:
    """测试可注入独立运行环境，避免访问开发者 .env 和项目数据库。"""
    @asynccontextmanager
    async def lifespan(application: FastAPI):
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
            if hasattr(application.state, "runtime"):
                del application.state.runtime
            if entered:
                await complete_in_thread(lambda: scope.__exit__(None, None, None))

    application = FastAPI(title="AI Teaching Assistant", lifespan=lifespan)

    @application.get("/health")
    def health() -> dict[str, str]:
        """进程存活检查；启动已核对数据库，但本请求不检查数据库/模型持续可用性。"""
        return {"status": "ok"}

    return application


app = create_app()
