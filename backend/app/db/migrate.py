"""取得同一把数据库运行锁后执行 Alembic；供部署的 pre-deploy 命令使用。"""

from pathlib import Path

from alembic.config import Config

from alembic import command
from app.core.config import load_database_settings
from app.db.engine import build_database_engine
from app.db.runtime_guard import (
    DatabaseRuntimeAlreadyActiveError,
    DatabaseRuntimeLock,
    DatabaseRuntimeLockError,
)


def main() -> None:
    backend = Path(__file__).resolve().parents[2]
    engine = build_database_engine(load_database_settings())
    try:
        try:
            with DatabaseRuntimeLock(engine):
                command.upgrade(Config(str(backend / "alembic.ini")), "head")
        except DatabaseRuntimeAlreadyActiveError:
            raise SystemExit(
                "Migration refused: stop the backend using this database, then retry."
            ) from None
        except DatabaseRuntimeLockError:
            raise SystemExit("Migration refused: the database runtime lock is unavailable.") from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
