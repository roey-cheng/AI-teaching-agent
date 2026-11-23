"""Application checkpoint resources must outlive streams and close before DB locks."""

import asyncio
import unittest
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from unittest.mock import MagicMock, patch

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import MemorySettings
from app.core.http_config import HTTPSettings
from app.main import create_app
from app.services.errors import StartupCleanupError
from testing_support import make_runtime


class ApplicationCheckpointLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, events):
        engine = create_engine("mysql+pymysql://example:example@127.0.0.1/example")
        self.addCleanup(engine.dispose)
        runtime = replace(
            make_runtime(session_factory=sessionmaker(bind=engine)), session_graph_cache=None
        )

        @contextmanager
        def scope():
            events.append("database-enter")
            try:
                yield runtime
            finally:
                events.append("database-exit")

        return create_app(
            runtime_factory=scope,
            http_settings=HTTPSettings.from_sources(_env_file=None),
            memory_settings=MemorySettings.from_sources(_env_file=None, background_enabled=False),
            turnstile_verifier=MagicMock(),
        )

    async def test_shutdown_waits_for_stream_before_pool_and_database_close(self):
        events = []
        app = self.make_app(events)

        @asynccontextmanager
        async def saver_scope(engine):
            events.append("checkpoint-enter")
            try:
                yield InMemorySaver()
            finally:
                events.append("checkpoint-exit")

        async def stream():
            try:
                await asyncio.Event().wait()
            finally:
                events.append("stream-exit")

        with patch("app.main.open_checkpoint_saver", saver_scope):
            async with app.router.lifespan_context(app):
                self.assertIsNotNone(app.state.runtime.session_graph_cache)
                task = asyncio.create_task(stream())
                app.state.active_stream_requests.add(task)
                await asyncio.sleep(0)
        self.assertEqual(
            events,
            [
                "database-enter",
                "checkpoint-enter",
                "stream-exit",
                "checkpoint-exit",
                "database-exit",
            ],
        )

    async def test_pool_startup_failure_is_sanitized_and_releases_database_scope(self):
        events = []
        app = self.make_app(events)

        @asynccontextmanager
        async def failed_saver(engine):
            raise RuntimeError("fake-sensitive-connection-detail")
            yield  # pragma: no cover -- async context-manager shape only.

        with (
            patch("app.main.open_checkpoint_saver", failed_saver),
            self.assertRaises(StartupCleanupError) as caught,
        ):
            async with app.router.lifespan_context(app):
                self.fail("Startup must not complete")
        self.assertNotIn("fake-sensitive", str(caught.exception))
        self.assertEqual(events, ["database-enter", "database-exit"])
