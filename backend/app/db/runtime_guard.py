"""用 MySQL advisory lock 保证一套聊天数据库同时只有一个后端运行。"""

from hashlib import sha256
from typing import Protocol

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError


class DatabaseRuntimeAlreadyActiveError(Exception):
    def __init__(self):
        super().__init__("Another backend is already using this chat database.")


class DatabaseRuntimeLockError(Exception):
    def __init__(self):
        super().__init__("The database runtime lock could not be acquired or verified.")


class DatabaseRuntimeGuard(Protocol):
    def require_held(self) -> None: ...


def runtime_lock_name(engine: Engine) -> str:
    """只用非敏感的目标位置生成稳定名称，不把账号或密码放进 MySQL 锁名。"""
    url = engine.url
    identity = f"{url.host or ''}:{url.port or 3306}/{url.database or ''}"
    return f"ai-teaching-runtime:{sha256(identity.encode()).hexdigest()[:32]}"


class DatabaseRuntimeLock:
    """锁绑定一条专用连接；连接关闭或进程消失时 MySQL 会自动释放锁。"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.name = runtime_lock_name(engine)
        self._connection: Connection | None = None
        self._connection_id: int | None = None

    def __enter__(self):
        if self._connection is not None:
            raise DatabaseRuntimeLockError()
        connection = None
        try:
            connection = self.engine.connect()
            acquired = connection.scalar(
                text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": self.name}
            )
            if acquired != 1:
                raise DatabaseRuntimeAlreadyActiveError()
            connection_id = connection.scalar(text("SELECT CONNECTION_ID()"))
            if not isinstance(connection_id, int):
                raise DatabaseRuntimeLockError()
            self._connection = connection
            self._connection_id = connection_id
            return self
        except DatabaseRuntimeAlreadyActiveError:
            if connection is not None:
                connection.close()
            raise
        except SQLAlchemyError, OSError, TypeError, ValueError:
            if connection is not None:
                connection.close()
            raise DatabaseRuntimeLockError() from None

    def require_held(self) -> None:
        if self._connection is None or self._connection_id is None:
            raise DatabaseRuntimeLockError()
        try:
            owner = self._connection.scalar(
                text("SELECT IS_USED_LOCK(:lock_name)"), {"lock_name": self.name}
            )
            if owner != self._connection_id:
                raise DatabaseRuntimeLockError()
        except SQLAlchemyError:
            raise DatabaseRuntimeLockError() from None

    def __exit__(self, *args):
        connection, self._connection = self._connection, None
        self._connection_id = None
        if connection is None:
            return
        try:
            connection.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": self.name})
        finally:
            connection.close()
