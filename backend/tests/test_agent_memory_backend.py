"""只测试内存中的虚拟文件，不读写宿主机或 MySQL。"""

from dataclasses import FrozenInstanceError
import unittest

from deepagents.backends.protocol import SandboxBackendProtocol

from app.agent.memory_backend import PROFILE_MEMORY_PATH, ProfileMemoryBackend


class ProfileMemoryBackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = ProfileMemoryBackend("中文 preference\nsecond line\n")

    def test_download_exact_path_only_preserves_order_and_content(self):
        paths = ["/etc/passwd", PROFILE_MEMORY_PATH, "/memories/../profile.md", "memories/profile.md"]
        results = self.backend.download_files(paths)
        self.assertEqual([r.path for r in results], paths)
        self.assertEqual(results[1].content.decode(), self.backend.profile_text)
        for index in (0, 2, 3):
            self.assertEqual(results[index].error, "file_not_found")
            self.assertIsNone(results[index].content)

    def test_read_windows_and_invalid_path(self):
        result = self.backend.read(PROFILE_MEMORY_PATH, offset=1, limit=1)
        self.assertEqual(result.file_data["content"], "second line")
        self.assertEqual((result.total_lines, result.start_line, result.end_line), (2, 2, 2))
        self.assertEqual(self.backend.read(PROFILE_MEMORY_PATH, -1, 1).start_line, 1)
        self.assertIsNone(self.backend.read(PROFILE_MEMORY_PATH, 100, 1).start_line)
        self.assertEqual(self.backend.read(PROFILE_MEMORY_PATH, limit=0).file_data["content"], "")
        self.assertIsNotNone(self.backend.read("/private/data").error)

    def test_every_write_method_denied_and_snapshot_unchanged(self):
        before = self.backend.profile_text
        self.assertIsNotNone(self.backend.write(PROFILE_MEMORY_PATH, "overwrite").error)
        self.assertIsNotNone(self.backend.edit(PROFILE_MEMORY_PATH, "中文", "English").error)
        self.assertIsNotNone(self.backend.delete(PROFILE_MEMORY_PATH).error)
        self.assertEqual(self.backend.upload_files([(PROFILE_MEMORY_PATH, b"changed")])[0].error, "permission_denied")
        self.assertEqual(self.backend.profile_text, before)

    def test_instances_are_isolated_immutable_and_not_sandbox(self):
        other = ProfileMemoryBackend("Other user's memory")
        self.assertNotEqual(other.download_files([PROFILE_MEMORY_PATH])[0].content,
                            self.backend.download_files([PROFILE_MEMORY_PATH])[0].content)
        with self.assertRaises(FrozenInstanceError):
            self.backend.profile_text = "changed"
        self.assertNotIn("中文", repr(self.backend))
        self.assertNotIsInstance(self.backend, SandboxBackendProtocol)
        self.assertFalse(hasattr(self.backend, "execute"))

    def test_listing_never_lists_host_files_and_search_is_disabled(self):
        self.assertEqual(self.backend.ls("/memories/").entries[0]["path"], PROFILE_MEMORY_PATH)
        self.assertIsNotNone(self.backend.ls("/Users").error)
        self.assertIsNotNone(self.backend.grep("password", "/").error)
        self.assertIsNotNone(self.backend.glob("**/*").error)


class AsyncMemoryBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_reads_and_writes_use_same_restrictions(self):
        backend = ProfileMemoryBackend("中文")
        result = await backend.adownload_files([PROFILE_MEMORY_PATH, "/other"])
        self.assertEqual(result[0].content.decode(), "中文")
        self.assertIsNotNone(result[1].error)
        self.assertEqual((await backend.aread(PROFILE_MEMORY_PATH)).file_data["content"], "中文")
        self.assertIsNotNone((await backend.awrite(PROFILE_MEMORY_PATH, "new")).error)
        self.assertIsNotNone((await backend.aedit(PROFILE_MEMORY_PATH, "中文", "new")).error)
        self.assertIsNotNone((await backend.adelete(PROFILE_MEMORY_PATH)).error)
        self.assertIsNotNone((await backend.aupload_files([(PROFILE_MEMORY_PATH, b"new")]))[0].error)
