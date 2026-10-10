"""只使用假的 LangSmith Client；不调用模型、不上传 trace。"""

import asyncio
import io
import os
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langsmith.run_helpers import get_tracing_context
from pydantic import ValidationError

from app.agent.check import main
from app.agent.tracing import agent_tracing, async_agent_tracing
from app.core.config import TracingSettings


class TracingSettingsTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_disabled_by_default_and_empty_optional_fields(self):
        settings = TracingSettings(_env_file=None, api_key="", workspace_id="")
        self.assertFalse(settings.tracing)
        self.assertIsNone(settings.api_key)
        self.assertIsNone(settings.workspace_id)

    def test_read_dotenv_and_mask_secret(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=fake-trace-key\n"
                            "LANGSMITH_PROJECT=test-project\nDB_PASSWORD=unused\n")
            settings = TracingSettings(_env_file=path)
        self.assertTrue(settings.tracing)
        self.assertEqual(settings.project, "test-project")
        self.assertNotIn("fake-trace-key", repr(settings))
        self.assertNotIn("fake-trace-key", settings.model_dump_json())
        self.assertNotIn("LANGSMITH_API_KEY", os.environ)

    def test_reject_missing_key_bad_key_project_and_endpoint(self):
        for values in ({"tracing": True}, {"api_key": "fake secret"},
                       {"project": " "}, {"endpoint": "https://example.com"}):
            with self.subTest(values=values), self.assertRaises(ValidationError) as caught:
                TracingSettings(_env_file=None, **values)
            self.assertNotIn("fake secret", str(caught.exception))


class AgentTracingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def enabled_settings(self):
        return TracingSettings(_env_file=None, tracing=True, api_key="fake-trace-key",
                               project="test-project", endpoint="https://eu.api.smith.langchain.com")

    async def test_disabled_overrides_environment_and_creates_no_client(self):
        before = get_tracing_context()
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true"}), \
                patch("app.agent.tracing.Client") as client:
            with agent_tracing(TracingSettings(_env_file=None, tracing=False)):
                await asyncio.sleep(0)
                self.assertFalse(get_tracing_context()["enabled"])
            client.assert_not_called()
        self.assertEqual(get_tracing_context(), before)

    async def test_enabled_context_survives_await_and_restores_afterwards(self):
        before = get_tracing_context()
        with patch("app.agent.tracing.Client") as client:
            with agent_tracing(self.enabled_settings()):
                await asyncio.sleep(0)
                context = get_tracing_context()
                self.assertTrue(context["enabled"])
                self.assertEqual(context["project_name"], "test-project")
                self.assertIs(context["client"], client.return_value)
            client.assert_called_once_with(api_key="fake-trace-key",
                                           api_url="https://eu.api.smith.langchain.com",
                                           workspace_id=None, timeout_ms=5000)
            client.return_value.close.assert_called_once_with(timeout=5)
        self.assertEqual(get_tracing_context(), before)

    async def test_client_closes_and_context_restores_when_agent_fails(self):
        before = get_tracing_context()
        with patch("app.agent.tracing.Client") as client:
            with self.assertRaisesRegex(RuntimeError, "agent failed"):
                with agent_tracing(self.enabled_settings()):
                    raise RuntimeError("agent failed")
            client.return_value.close.assert_called_once_with(timeout=5)
        self.assertEqual(get_tracing_context(), before)

    async def test_cleanup_error_does_not_replace_agent_error_or_leak_secret(self):
        with patch("app.agent.tracing.Client") as client:
            client.return_value.close.side_effect = RuntimeError("fake-trace-key")
            with self.assertLogs("app.agent.tracing", level="WARNING") as logs:
                with self.assertRaisesRegex(ValueError, "agent failed"):
                    with agent_tracing(self.enabled_settings()):
                        raise ValueError("agent failed")
            self.assertNotIn("fake-trace-key", str(logs.output))

    async def test_probe_consumes_stream_inside_enabled_context(self):
        async def stream(_agent):
            await asyncio.sleep(0)
            self.assertTrue(get_tracing_context()["enabled"])
            self.assertEqual(get_tracing_context()["project_name"], "test-project")
            return 2

        with patch("app.agent.tracing.Client") as client, \
                patch("app.agent.check.load_model_settings"), \
                patch("app.agent.check.load_tracing_settings", return_value=self.enabled_settings()), \
                patch("app.agent.check.build_probe_agent"), \
                patch("app.agent.check.stream_answer", side_effect=stream), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(await main(), 0)
            client.return_value.close.assert_called_once_with(timeout=5)
        self.assertIn("upload success is not verified", output.getvalue())

    async def test_invalid_tracing_config_prevents_model_call(self):
        def bad_settings():
            return TracingSettings(_env_file=None, tracing=True)

        with patch("app.agent.check.load_model_settings"), \
                patch("app.agent.check.load_tracing_settings", side_effect=bad_settings), \
                patch("app.agent.check.build_probe_agent") as build, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(await main(), 1)
        build.assert_not_called()
        self.assertIn("LANGSMITH_*", output.getvalue())

    async def test_async_tracing_context_covers_await_but_close_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        closed_on = []
        before = get_tracing_context()
        with patch("app.agent.tracing.Client") as client:
            client.return_value.close.side_effect = lambda **kwargs: closed_on.append(threading.get_ident())
            async with async_agent_tracing(self.enabled_settings()):
                await asyncio.sleep(0)
                self.assertTrue(get_tracing_context()["enabled"])
                self.assertIs(get_tracing_context()["client"], client.return_value)
            client.return_value.close.assert_called_once_with(timeout=5)
        self.assertEqual(len(closed_on), 1)
        self.assertNotEqual(closed_on[0], loop_thread)
        self.assertEqual(get_tracing_context(), before)
