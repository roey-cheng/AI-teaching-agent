"""Native StoreBackend + SQLAlchemy Store boundary, without external services."""

import unittest
from datetime import datetime
from unittest.mock import MagicMock

from deepagents.backends import StoreBackend
from langgraph.store.base import PutOp
from sqlalchemy.dialects import mysql

from app.agent.profile_store import PROFILE_MEMORY_PATH, ProfileStore, topic_path
from app.models import AgentMemory


class ProfileStoreTest(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.row = AgentMemory(
            memory_id=7,
            user_id=1,
            memory_key="preference.language",
            memory_type="LEARNING_PREFERENCE",
            summary="中文",
            created_at=datetime(2026, 10, 1),
            updated_at=datetime(2026, 10, 2),
        )
        self.session.scalars.return_value.all.return_value = [self.row]
        self.store = ProfileStore(1, self.session)
        self.path = topic_path("preference.language")

    def test_native_backend_reads_existing_rows_and_derived_profile(self):
        backend = self.store.backend()
        self.assertIs(type(backend), StoreBackend)
        self.assertEqual(backend.download_files([self.path])[0].content, "中文".encode())
        content = backend.download_files([PROFILE_MEMORY_PATH])[0].content
        assert content is not None
        self.assertIn("中文", content.decode())
        item = self.store.get(self.store.namespace, self.path)
        assert item is not None
        self.assertEqual(item.created_at, self.row.created_at)
        self.assertEqual(item.updated_at, self.row.updated_at)

    def test_facts_use_one_bounded_read_and_scope_search(self):
        self.assertEqual(self.store.facts(), {"preference.language": "中文"})
        self.session.scalars.assert_called_once()
        self.assertIsNone(self.store.get(("users", "2", "profile"), self.path))
        self.assertEqual(self.store.search(("users", "2")), [])
        self.assertEqual(self.store.list_namespaces(), [self.store.namespace])
        self.assertEqual(len(self.store.search(self.store.namespace, limit=1, offset=1)), 1)
        self.assertEqual(self.store.search(self.store.namespace, filter={"content": "absent"}), [])

    def test_new_store_reloads_changes_instead_of_caching_rows(self):
        self.assertEqual(self.store.facts()["preference.language"], "中文")
        self.row.summary = "English"
        fresh = ProfileStore(1, self.session)
        self.assertEqual(fresh.facts()["preference.language"], "English")

    def test_read_only_denies_direct_store_and_native_file_writes(self):
        for call in (
            lambda: self.store.put(self.store.namespace, self.path, {"content": "new"}),
            lambda: self.store.backend().write(self.path, "new"),
            lambda: self.store.backend().delete(self.path),
        ):
            with self.assertRaises(PermissionError):
                call()
        self.session.execute.assert_not_called()

    def test_native_write_uses_existing_table_and_never_commits(self):
        store = ProfileStore(1, self.session, writable=True)
        result = store.backend().upload_files([(self.path, b"English")])
        self.assertIsNone(result[0].error)
        compiled = self.session.execute.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertEqual(compiled.params["user_id_m0"], 1)
        self.assertEqual(compiled.params["summary_m0"], "English")
        self.assertIn("ON DUPLICATE KEY UPDATE", str(compiled))
        self.assertNotIn("memory_id", str(compiled).split("ON DUPLICATE KEY UPDATE")[1])
        self.session.commit.assert_not_called()
        self.session.rollback.assert_not_called()

    def test_bad_trailing_operation_validated_before_any_sql_write(self):
        store = ProfileStore(1, self.session, writable=True)
        valid = PutOp(store.namespace, self.path, {"content": "valid"})
        for invalid in (
            PutOp(("users", "2", "profile"), self.path, {"content": "other user"}),
            PutOp(store.namespace, PROFILE_MEMORY_PATH, {"content": "index overwrite"}),
            PutOp(store.namespace, "/etc/passwd", {"content": "host file"}),
            PutOp(store.namespace, self.path, None),
            PutOp(store.namespace, self.path, {"content": "x" * 501}),
            PutOp(store.namespace, self.path, {"content": "   "}),
            PutOp(store.namespace, self.path, {"content": "YQ==", "encoding": "base64"}),
            PutOp(store.namespace, self.path, {"content": "valid"}, ttl=1),
        ):
            with self.subTest(op=invalid.key), self.assertRaises((ValueError, PermissionError)):
                store.batch([valid, invalid])
        self.session.execute.assert_not_called()

    def test_invalid_persisted_owner_rejected(self):
        self.row.user_id = 2
        with self.assertRaises(ValueError):
            self.store.facts()

    def test_turn_snapshot_never_follows_later_mutations(self):
        facts = {"preference.language": "中文"}
        store = ProfileStore.snapshot(1, "index", facts=facts)
        facts["preference.language"] = "English"
        self.assertEqual(store.backend().download_files([self.path])[0].content, "中文".encode())
        item = store.get(store.namespace, self.path)
        assert item is not None
        item.value["content"] = "forged"
        self.assertEqual(store.backend().download_files([self.path])[0].content, "中文".encode())


class AsyncProfileStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_session_cannot_leak_into_graph_threads(self):
        session = MagicMock()
        store = ProfileStore(1, session)
        with self.assertRaises(RuntimeError):
            await store.aget(store.namespace, PROFILE_MEMORY_PATH)
        session.scalars.assert_not_called()
