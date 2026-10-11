"""离线/临时 MySQL 测试的显式断言与运行环境，不读取 .env 或连接数据库。"""

from sqlalchemy.orm import Session, sessionmaker

from app.core.runtime import BackendRuntime
from app.services.generation_limit import GenerationRateLimiter
from app.services.generation_registry import GenerationRegistry
from app.services.startup_cleanup import StartupCleanupResult


def require_value[T](value: T | None) -> T:
    """断言查询/可选结果存在，再供测试检查其字段。"""
    if value is None:
        raise AssertionError("Expected a non-None test result")
    return value


def require_type[T](value: object, expected: type[T]) -> T:
    """运行时检查真实分支，不用 cast 假装它属于预期类型。"""
    if not isinstance(value, expected):
        raise AssertionError(f"Expected {expected.__name__}, got {type(value).__name__}")  # noqa: TRY004 -- 这是测试断言失败，不是业务参数错误。
    return value


def make_runtime(
    *,
    session_factory: sessionmaker[Session] | None = None,
    registry: GenerationRegistry | None = None,
    limiter: GenerationRateLimiter | None = None,
    cleanup: StartupCleanupResult | None = None,
) -> BackendRuntime:
    # 未绑定引擎的 sessionmaker 不会连接开发者的数据库。
    return BackendRuntime(
        session_factory if session_factory is not None else sessionmaker(),
        registry if registry is not None else GenerationRegistry(),
        limiter if limiter is not None else GenerationRateLimiter(),
        cleanup if cleanup is not None else StartupCleanupResult(),
    )
