"""Native LangGraph MySQL checkpoint storage; no implicit schema changes.

The application owns connection lifecycle and access control. The maintained
``langgraph-checkpoint-mysql`` package owns checkpoint SQL and its migrations.
Business messages remain separate from these graph-state tables.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import asyncmy
from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

CHECKPOINT_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


class CheckpointStorageNotReadyError(Exception):
    def __init__(self):
        super().__init__(
            "Checkpoint storage requires MySQL >= 8.4 and < 9.6 with current "
            "checkpoint migrations. Run the explicit database migration command."
        )


def checkpoint_serializer() -> JsonPlusSerializer:
    """Allow framework-safe message types, not arbitrary classes or pickle.

    An explicit empty extra allowlist enables strict deserialization regardless
    of a process-wide LANGGRAPH_STRICT_MSGPACK environment variable. Deep Agents
    files and summary events are plain dictionaries; messages are built-in safe
    types in the pinned LangGraph serializer.
    """
    return JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[])


def _require_supported_database(connection: Connection) -> None:
    dialect = connection.dialect
    version = dialect.server_version_info or ()
    # The pinned MySQL saver uses MD5 generated columns, removed in MySQL 9.6.
    if (
        dialect.name != "mysql"
        or getattr(dialect, "is_mariadb", False)
        or not (8, 4) <= version < (9, 6)
    ):
        raise CheckpointStorageNotReadyError()


def setup_checkpoint_schema(engine: Engine) -> None:
    """Explicit migration-only entry point; caller must hold the migration lock.

    SQLAlchemy restores the pooled connection's isolation level when it closes.
    The saver uses its own version table; do not copy its SQL into Alembic.
    """
    with engine.connect() as connection:
        _require_supported_database(connection)
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        driver = connection.connection.driver_connection
        if driver is None:
            raise CheckpointStorageNotReadyError()
        saver = PyMySQLSaver(driver, serde=checkpoint_serializer())
        saver.setup()


def check_checkpoint_schema(engine: Engine) -> None:
    """Read-only startup guard; never creates, updates, or repairs any table."""
    try:
        with engine.connect() as connection:
            _require_supported_database(connection)
            tables = set(inspect(connection).get_table_names())
            if not CHECKPOINT_TABLES.issubset(tables):
                raise CheckpointStorageNotReadyError()
            versions = list(
                connection.execute(text("SELECT v FROM checkpoint_migrations ORDER BY v")).scalars()
            )
            if versions != list(range(len(PyMySQLSaver.MIGRATIONS))):
                raise CheckpointStorageNotReadyError()
    except SQLAlchemyError:
        raise CheckpointStorageNotReadyError() from None


@asynccontextmanager
async def open_checkpoint_saver(engine: Engine) -> AsyncIterator[AsyncMySaver]:
    """Open a bounded async pool for the application lifespan, not for each turn.

    Reuses configured database credentials without constructing/logging a URL.
    Schema readiness is checked separately before opening this runtime resource.
    """
    url = engine.url
    if url.get_backend_name() != "mysql" or not url.database:
        raise CheckpointStorageNotReadyError()
    pool = await asyncmy.create_pool(
        host=url.host or "127.0.0.1",
        port=url.port or 3306,
        user=url.username,
        password=url.password or "",
        db=url.database,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
        read_timeout=10,
        minsize=1,
        maxsize=4,
        pool_recycle=300,
        local_infile=False,
        echo=False,
    )
    try:
        # Upstream supports pools but its AsyncPool.acquire annotation omits
        # the driver's async-context-manager return wrapper.
        yield AsyncMySaver(cast(Any, pool), serde=checkpoint_serializer())
    finally:
        pool.close()
        await pool.wait_closed()
