"""连接 Alembic、表模型和 MySQL；只有在线命令才读取数据库配置。"""

from alembic import context
from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app import models  # noqa: F401  导入五个模型，登记到共同的 metadata。
from app.core.config import load_database_settings
from app.db.base import Base
from app.db.engine import build_database_engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """--sql：只输出 MySQL SQL 文本；不读密码、不连接、不执行建表。"""
    context.configure(
        dialect_name="mysql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线命令实际连接 MySQL；upgrade/downgrade 会修改数据库。"""
    engine = None
    try:
        engine = build_database_engine(load_database_settings())
        with engine.connect() as connection:
            dialect = connection.dialect
            if (
                dialect.name != "mysql"
                or getattr(dialect, "is_mariadb", False)
                or (dialect.server_version_info or ()) < (8, 4)
            ):
                raise CommandError("Migrations require MySQL 8.4 or newer, not MariaDB.")
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )
            # MySQL DDL 不能靠这里的事务整体回滚；失败后必须检查实际表结构。
            with context.begin_transaction():
                context.run_migrations()
    except (ValidationError, SQLAlchemyError, OSError):
        # 不把连接凭据或原始数据库异常输出到终端。
        raise CommandError(
            "Migration command failed. Check DB_* settings, MySQL availability, "
            "permissions and schema state. DDL may have partially applied; "
            "do not blindly retry or stamp the revision."
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
