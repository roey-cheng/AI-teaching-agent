"""启动前的只读数据库检查；不执行迁移或修复表结构。"""

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

from app.db.base import Base
from app import models  # noqa: F401 -- 导入五张表，将它们登记到 Base.metadata。


class DatabaseNotReadyError(Exception):
    def __init__(self):
        super().__init__("MySQL 8.4+ and the current project migrations are required.")


def check_database_ready(engine: Engine) -> None:
    backend = Path(__file__).resolve().parents[2]
    heads = set(ScriptDirectory.from_config(Config(str(backend / "alembic.ini"))).get_heads())
    with engine.connect() as connection:
        dialect = connection.dialect
        if (dialect.name != "mysql" or getattr(dialect, "is_mariadb", False)
                or (dialect.server_version_info or ()) < (8, 4)):
            raise DatabaseNotReadyError()
        actual = set(MigrationContext.configure(connection).get_current_heads())
        tables = set(inspect(connection).get_table_names())
        if actual != heads or not set(Base.metadata.tables).issubset(tables):
            raise DatabaseNotReadyError()
