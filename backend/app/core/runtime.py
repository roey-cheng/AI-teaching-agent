"""终端与 FastAPI 共用的启动顺序：进程锁→数据库检查→清理→开始服务。"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.runtime_lock import RuntimeLock
from app.db.readiness import DatabaseNotReadyError, check_database_ready
from app.db.session import build_session_factory
from app.services.errors import StartupCleanupError
from app.services.generation_limit import GenerationRateLimiter
from app.services.generation_registry import GenerationRegistry
from app.services.startup_cleanup import StartupCleanupResult, reconcile_interrupted_generations


@dataclass(frozen=True)
class BackendRuntime:
    session_factory: sessionmaker[Session]
    registry: GenerationRegistry
    limiter: GenerationRateLimiter
    cleanup: StartupCleanupResult


@contextmanager
def open_backend_runtime(engine: Engine, *, lock: RuntimeLock | None = None) -> Iterator[BackendRuntime]:
    """引擎由调用方创建/释放；退出前调用方必须已等待所有生成和清理完成。"""
    with (lock if lock is not None else RuntimeLock()) as held_lock:
        try:
            check_database_ready(engine)
            factory = build_session_factory(engine)
            registry = GenerationRegistry()
            cleanup = reconcile_interrupted_generations(factory, registry, runtime_lock=held_lock)
            runtime = BackendRuntime(factory, registry, GenerationRateLimiter(), cleanup)
        except DatabaseNotReadyError:
            raise
        except Exception:
            raise StartupCleanupError() from None
        # 不把正常运行期错误包成启动错误；进程锁覆盖整个服务生命周期。
        yield runtime
