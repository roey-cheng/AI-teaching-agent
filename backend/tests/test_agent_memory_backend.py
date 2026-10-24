"""Exercise the real StoreBackend, with scoped, read-only turn Stores."""

import unittest

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import SandboxBackendProtocol

from app.agent.memory_backend import PROFILE_MEMORY_PATH, snapshot_backend
from app.agent.profile_store import ProfileStore
from testing_support import require_value


class StoreMemoryBackendTest(unittest.TestCase):
    def setUp(self):
        self.text = "中文 preference\nsecond line\n"
        self.backend = snapshot_backend(self.text)

    def test_native_backend_and_exact_paths(self):
        self.assertIs(type(self.backend), StoreBackend)
        paths = [
            "/etc/passwd",
            PROFILE_MEMORY_PATH,
            "/memories/../profile.md",
            "memories/profile.md",
        ]
        results = self.backend.download_files(paths)
        self.assertEqual([r.path for r in results], paths)
        self.assertEqual(require_value(results[1].content).decode(), self.text)
        for index in (0, 2, 3):
            self.assertEqual(results[index].error, "file_not_found")

    def test_native_paged_read_and_search(self):
        result = self.backend.read(PROFILE_MEMORY_PATH, offset=1, limit=1)
        self.assertEqual(require_value(result.file_data)["content"], "second line\n")
        self.assertIsNotNone(self.backend.read("/private/data").error)
        self.assertEqual(
            require_value(self.backend.ls("/memories/").entries)[0]["path"], PROFILE_MEMORY_PATH
        )
        matches = require_value(self.backend.glob("**/*.md").matches)
        self.assertEqual([m["path"] for m in matches], [PROFILE_MEMORY_PATH])
        self.assertFalse(self.backend.ls("/Users").entries)

    def test_all_mutation_routes_denied(self):
        for operation in (
            lambda: self.backend.write(PROFILE_MEMORY_PATH, "overwrite"),
            lambda: self.backend.write("/new.md", "new"),
            lambda: self.backend.edit(PROFILE_MEMORY_PATH, "中文", "English"),
            lambda: self.backend.delete(PROFILE_MEMORY_PATH),
            lambda: self.backend.upload_files([(PROFILE_MEMORY_PATH, b"changed")]),
        ):
            with self.assertRaises(PermissionError):
                operation()
        self.assertEqual(
            self.backend.download_files([PROFILE_MEMORY_PATH])[0].content, self.text.encode()
        )

    def test_scoped_store_has_no_host_or_sandbox_access(self):
        store = ProfileStore.snapshot(2, "other user")
        self.assertIsNone(store.get(("users", "1", "profile"), PROFILE_MEMORY_PATH))
        self.assertEqual(store.search(("users", "1")), [])
        self.assertNotIn("other user", repr(store))
        self.assertNotIsInstance(self.backend, SandboxBackendProtocol)
        self.assertFalse(hasattr(self.backend, "execute"))


class AsyncMemoryBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_native_operations_keep_same_boundary(self):
        backend = snapshot_backend("中文")
        result = await backend.adownload_files([PROFILE_MEMORY_PATH, "/other"])
        self.assertEqual(result[0].content, "中文".encode())
        self.assertIsNotNone(result[1].error)
        self.assertEqual(
            require_value((await backend.aread(PROFILE_MEMORY_PATH)).file_data)["content"], "中文"
        )
        with self.assertRaises(PermissionError):
            await backend.aedit(PROFILE_MEMORY_PATH, "中文", "new")
        with self.assertRaises(PermissionError):
            await backend.adelete(PROFILE_MEMORY_PATH)
