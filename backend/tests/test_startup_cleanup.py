"""启动核对、进程锁和 HTTP 生命周期测试；无真实数据库或模型。"""

import asyncio
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core.runtime import open_backend_runtime
from app.core.runtime_lock import RuntimeAlreadyActiveError, RuntimeLock, RuntimeLockError
from app.main import create_app
from app.models import ChatSession, Message
from app.services.errors import StartupCleanupError
from app.services.generation_registry import GenerationRegistry
from app.services.startup_cleanup import StartupCleanupResult, reconcile_interrupted_generations


class RuntimeLockTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "chat.lock"

    def test_second_owner_rejected_then_lock_reusable_and_file_preserved(self):
        first = RuntimeLock(self.path)
        with self.assertRaises(RuntimeLockError):
            first.require_held()
        with first:
            first.require_held()
            with self.assertRaises(RuntimeAlreadyActiveError), RuntimeLock(self.path):
                self.fail("Must not acquire twice")
        with RuntimeLock(self.path) as second:
            second.require_held()
        self.assertEqual(self.path.read_bytes(), b"")

    def test_separate_process_is_blocked_while_parent_holds_lock(self):
        code = (
            "from pathlib import Path\nimport sys\n"
            "from app.core.runtime_lock import RuntimeLock, RuntimeAlreadyActiveError\n"
            "try:\n with RuntimeLock(Path(sys.argv[1])): print('acquired')\n"
            "except RuntimeAlreadyActiveError: print('busy')\n"
        )
        with RuntimeLock(self.path):
            result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "busy")
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "acquired")

    def test_process_crash_releases_lock_without_deleting_file(self):
        code = (
            "from pathlib import Path\nimport sys, os\n"
            "from app.core.runtime_lock import RuntimeLock\n"
            "lock=RuntimeLock(Path(sys.argv[1])); lock.__enter__(); os._exit(7)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7)
        with RuntimeLock(self.path) as lock:
            lock.require_held()

    def test_symlink_is_rejected_without_modifying_target(self):
        target = self.path.parent / "not-a-lock"
        target.touch()
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeLockError), RuntimeLock(self.path):
            self.fail("Symlink must not be opened")
        self.assertEqual(target.read_bytes(), b"")


class StartupCleanupTest(unittest.TestCase):
    def setUp(self):
        self.factory = MagicMock()
        self.session = self.factory.begin.return_value.__enter__.return_value
        self.registry, self.lock = GenerationRegistry(), MagicMock(spec=RuntimeLock)
        self.question = Message(
            message_id=2, chat_session_id=1, role="USER", content="Question", attempt_id=str(uuid4()),
            retry_of_attempt_id=str(uuid4()), client_message_key=str(uuid4()),
            generation_status="RUNNING", created_at=datetime(2026, 10, 11),
        )
        self.session.scalars.return_value.all.side_effect = [[1], [self.question], []]
        self.session.scalar.return_value = ChatSession(chat_session_id=1, user_id=1)

    def cleanup(self):
        return reconcile_interrupted_generations(self.factory, self.registry, runtime_lock=self.lock)

    def test_running_without_answer_is_interrupted_after_commit(self):
        original = self.question.attempt_id, self.question.retry_of_attempt_id, self.question.created_at
        result = self.cleanup()
        self.assertEqual(result, StartupCleanupResult(interrupted=1))
        self.assertEqual(self.question.generation_status, "FAILED")
        self.assertEqual(self.question.generation_error_code, "GENERATION_INTERRUPTED")
        self.assertEqual((self.question.attempt_id, self.question.retry_of_attempt_id, self.question.created_at), original)
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)
        self.session.add.assert_not_called()
        self.assertEqual(self.lock.require_held.call_count, 2)

    def test_existing_final_answer_is_preserved_and_status_restored(self):
        answer = Message(message_id=3, chat_session_id=1, role="ASSISTANT", content="Saved answer",
                         in_reply_to_message_id=2, created_at=datetime(2026, 10, 11))
        self.session.scalars.return_value.all.side_effect = [[1], [self.question], [answer]]
        self.assertEqual(self.cleanup(), StartupCleanupResult(restored_success=1))
        self.assertEqual(self.question.generation_status, "SUCCEEDED")
        self.assertIsNone(self.question.generation_error_code)
        self.assertEqual(answer.content, "Saved answer")

    def test_bad_answer_relationship_or_blank_content_stops_startup(self):
        for changes in ({"chat_session_id": 99}, {"content": "  "}, {"role": "USER"}):
            answer = Message(**(dict(message_id=3, chat_session_id=1, role="ASSISTANT", content="Answer",
                                     in_reply_to_message_id=2, created_at=datetime(2026, 10, 11)) | changes))
            self.session.scalars.return_value.all.side_effect = [[1], [self.question], [answer]]
            with self.assertRaises(StartupCleanupError):
                self.cleanup()
        self.assertEqual(self.question.generation_status, "RUNNING")

    def test_active_registry_is_not_cleared_or_modified(self):
        attempt = str(uuid4())
        with self.registry.locked(1) as slot:
            slot.claim(attempt)
        with self.assertRaises(StartupCleanupError):
            self.cleanup()
        self.factory.begin.assert_not_called()
        with self.registry.locked(1) as slot:
            self.assertEqual(slot.attempt_id, attempt)

    def test_cleanup_blocks_new_registry_access_and_reopens_after_failure(self):
        def reject():
            with self.assertRaises(RuntimeError), self.registry.locked(1):
                self.fail("Generation must not enter during startup cleanup")
            raise SQLAlchemyError("private database credentials")

        self.session.flush.side_effect = reject
        with self.assertRaises(StartupCleanupError) as caught:
            self.cleanup()
        self.assertNotIn("private", str(caught.exception))
        with self.registry.locked(1) as slot:
            self.assertIsNone(slot.attempt_id)

    def test_no_lock_means_no_database_access(self):
        self.lock.require_held.side_effect = RuntimeLockError()
        with self.assertRaises(RuntimeLockError):
            self.cleanup()
        self.factory.begin.assert_not_called()

    def test_malformed_attempt_is_rejected_not_converted_to_retryable_failure(self):
        self.question.attempt_id = "not-a-valid-attempt"
        with self.assertRaises(StartupCleanupError):
            self.cleanup()
        self.assertEqual(self.question.generation_status, "RUNNING")


class RuntimeStartupTest(unittest.TestCase):
    def test_lock_check_cleanup_service_shutdown_order(self):
        calls = []

        @contextmanager
        def lock():
            calls.append("locked")
            try:
                yield MagicMock(spec=RuntimeLock)
            finally:
                calls.append("released")

        with patch("app.core.runtime.check_database_ready", side_effect=lambda _: calls.append("checked")), \
                patch("app.core.runtime.build_session_factory"), \
                patch("app.core.runtime.reconcile_interrupted_generations",
                      side_effect=lambda *a, **k: (calls.append("cleaned"), StartupCleanupResult())[1]):
            with open_backend_runtime(MagicMock(), lock=lock()) as runtime:
                calls.append("serving")
                self.assertIsInstance(runtime.registry, GenerationRegistry)
        self.assertEqual(calls, ["locked", "checked", "cleaned", "serving", "released"])

    def test_bad_database_never_starts_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chat.lock"
            with patch("app.core.runtime.check_database_ready", side_effect=SQLAlchemyError("private")), \
                    patch("app.core.runtime.reconcile_interrupted_generations") as cleanup:
                with self.assertRaises(StartupCleanupError), open_backend_runtime(MagicMock(), lock=RuntimeLock(path)):
                    self.fail("Must not serve")
            cleanup.assert_not_called()
            with RuntimeLock(path):
                pass


class WebStartupTest(unittest.TestCase):
    def test_http_only_after_startup_and_runtime_removed_at_shutdown(self):
        events, thread_ids = [], []
        runtime = SimpleNamespace(cleanup=StartupCleanupResult(2, 1))

        @contextmanager
        def scope():
            events.append("started")
            thread_ids.append(threading.get_ident())
            try:
                yield runtime
            finally:
                events.append("closed")

        app = create_app(runtime_factory=scope)
        with TestClient(app) as client:
            self.assertEqual(events, ["started"])
            self.assertIs(app.state.runtime, runtime)
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
        self.assertEqual(events, ["started", "closed"])
        self.assertFalse(hasattr(app.state, "runtime"))

    def test_startup_failure_does_not_enter_http_serving(self):
        @contextmanager
        def scope():
            raise StartupCleanupError()
            yield  # pragma: no cover -- contextmanager 的失败进入路径。

        app = create_app(runtime_factory=scope)
        with self.assertRaises(StartupCleanupError), TestClient(app):
            self.fail("Must not serve")
        self.assertFalse(hasattr(app.state, "runtime"))


class WebStartupCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_startup_thread_then_releases_scope(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()

        @contextmanager
        def scope():
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test release not received")
            try:
                yield SimpleNamespace(cleanup=StartupCleanupResult())
            finally:
                closed.set()

        app = create_app(runtime_factory=scope)

        async def start():
            async with app.router.lifespan_context(app):
                self.fail("Cancelled startup must not serve")

        task = asyncio.create_task(start())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 4))
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertFalse(hasattr(app.state, "runtime"))
