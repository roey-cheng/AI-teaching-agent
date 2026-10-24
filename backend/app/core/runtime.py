"""终端与 FastAPI 共用的启动顺序：进程锁→数据库检查→清理→开始服务。"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.agent.factory import ChatAgentProvider
from app.core.config import load_generation_quota_settings
from app.core.runtime_lock import RuntimeLock
from app.db.readiness import DatabaseNotReadyError, check_database_ready
from app.db.runtime_guard import DatabaseRuntimeGuard, DatabaseRuntimeLock
from app.db.session import build_session_factory
from app.services.errors import StartupCleanupError
from app.services.generation_limit import GenerationRateLimiter
from app.services.generation_quota import GenerationQuota
from app.services.generation_registry import GenerationRegistry
from app.services.startup_cleanup import StartupCleanupResult, reconcile_interrupted_generations


@dataclass(frozen=True)
class BackendRuntime:
    session_factory: sessionmaker[Session]
    registry: GenerationRegistry
    limiter: GenerationRateLimiter
    quota: GenerationQuota
    database_guard: DatabaseRuntimeGuard
    cleanup: StartupCleanupResult
    chat_agents: ChatAgentProvider = field(default_factory=ChatAgentProvider, repr=False)


@contextmanager
def open_backend_runtime(
    engine: Engine,
    *,
    lock: AbstractContextManager[RuntimeLock] | None = None,
    database_lock: AbstractContextManager[DatabaseRuntimeGuard] | None = None,
) -> Iterator[BackendRuntime]:
    """引擎由调用方创建/释放；退出前调用方必须已等待所有生成和清理完成。"""
    local_guard = lock if lock is not None else RuntimeLock()
    database_guard = database_lock if database_lock is not None else DatabaseRuntimeLock(engine)
    with local_guard as held_lock, database_guard as held_database_lock:
        try:
            held_database_lock.require_held()
            check_database_ready(engine)
            factory = build_session_factory(engine)
            registry = GenerationRegistry()
            quota = GenerationQuota(load_generation_quota_settings())
            cleanup = reconcile_interrupted_generations(
                factory, registry, runtime_lock=held_lock, quota=quota
            )
            from app.services.memory_tasks import recover_memory_jobs

            recover_memory_jobs(factory)
            held_database_lock.require_held()
            runtime = BackendRuntime(
                factory,
                registry,
                GenerationRateLimiter(),
                quota,
                held_database_lock,
                cleanup,
            )
        except DatabaseNotReadyError:
            raise
        except Exception:  # noqa: BLE001 -- 启动边界统一脱敏；异常仍通过 StartupCleanupError 传播。
            raise StartupCleanupError() from None
        # 不把正常运行期错误包成启动错误；进程锁覆盖整个服务生命周期。
        yield runtime
