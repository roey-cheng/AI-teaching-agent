"""Bundled skill discovery and the virtual filesystem boundary; no model API."""

import unittest
from contextlib import chdir
from tempfile import TemporaryDirectory

from deepagents.middleware.skills import SkillsMiddleware
from langgraph.runtime import Runtime

from app.agent.memory_backend import PROFILE_MEMORY_PATH, snapshot_backend
from app.agent.teaching_skill import SKILLS_ROOT, TEACHING_SKILL_PATH, load_teaching_skill


class TeachingSkillTest(unittest.TestCase):
    def test_resource_load_does_not_depend_on_working_directory(self):
        with TemporaryDirectory() as directory, chdir(directory):
            content = load_teaching_skill()
        self.assertIn("name: adaptive-teaching", content)

    def test_native_discovery_and_paged_read_share_bundled_content(self):
        content = load_teaching_skill()
        backend = snapshot_backend("private profile", skill_text=content)
        middleware = SkillsMiddleware(backend=backend, sources=[SKILLS_ROOT])
        update = middleware.before_agent({"messages": []}, Runtime(), {})
        self.assertIsNotNone(update)
        assert update is not None
        self.assertEqual(len(update["skills_metadata"]), 1)
        self.assertEqual(update["skills_metadata"][0]["path"], TEACHING_SKILL_PATH)
        result = backend.read(TEACHING_SKILL_PATH, offset=1, limit=2)
        assert result.file_data is not None
        self.assertEqual(
            result.file_data["content"], "".join(content.splitlines(keepends=True)[1:3])
        )
        self.assertEqual(backend.download_files([TEACHING_SKILL_PATH])[0].content, content.encode())
        self.assertEqual(
            backend.download_files([PROFILE_MEMORY_PATH])[0].content, b"private profile"
        )

    def test_skills_are_read_only_and_paths_are_exact(self):
        backend = snapshot_backend("private profile", skill_text=load_teaching_skill())
        for path in ("/etc/passwd", "/skills/adaptive-teaching/../SKILL.md", "SKILL.md"):
            self.assertIsNotNone(backend.read(path).error)
            self.assertIsNotNone(backend.download_files([path])[0].error)
        with self.assertRaises(PermissionError):
            backend.write(TEACHING_SKILL_PATH, "overwrite")
        with self.assertRaises(PermissionError):
            backend.edit(TEACHING_SKILL_PATH, "name: adaptive-teaching", "name: changed")
        with self.assertRaises(PermissionError):
            backend.delete(TEACHING_SKILL_PATH)
        with self.assertRaises(PermissionError):
            backend.upload_files([(TEACHING_SKILL_PATH, b"overwrite")])
        self.assertIsNotNone(snapshot_backend("profile").read(TEACHING_SKILL_PATH).error)
