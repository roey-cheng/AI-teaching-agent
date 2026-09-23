import io
import os
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import ValidationError

from app.agent.check import build_probe_agent, main, probe_without_tools, stream_answer, visible_text
from app.core.config import ModelSettings


def fake_settings(**overrides):
    values = dict(provider="deepseek", name="deepseek-flash", api_key="fake-secret")
    values.update(overrides)
    return ModelSettings(_env_file=None, **values)


class ModelSettingsTest(unittest.TestCase):
    def test_read_model_fields_and_mask_key(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("MODEL_PROVIDER=deepseek\nMODEL_NAME=deepseek-flash\nMODEL_API_KEY=fake-secret\nDB_PASSWORD=unused\n")
            settings = ModelSettings(_env_file=path)
            self.assertEqual(settings.name, "deepseek-flash")
            self.assertEqual(settings.api_key.get_secret_value(), "fake-secret")
            self.assertNotIn("fake-secret", repr(settings))
            with patch.dict(os.environ, {"MODEL_NAME": "override-model"}):
                self.assertEqual(ModelSettings(_env_file=path).name, "override-model")

    def test_reject_invalid_config_without_exposing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            for fields in ({"api_key": ""}, {"api_key": "fake secret"}, {"name": " "},
                           {"provider": "other"}, {"base_url": "https://example.com"}):
                with self.subTest(fields=fields), self.assertRaises(ValidationError) as caught:
                    fake_settings(**fields)
                self.assertNotIn("fake-secret", str(caught.exception))

    def test_build_uses_deep_agents_and_bounded_model(self):
        with patch.dict(os.environ, {}, clear=True), patch("app.agent.check.ChatDeepSeek") as model, \
                patch("app.agent.check.create_deep_agent") as create:
            build_probe_agent(fake_settings())
            self.assertEqual(model.call_args.kwargs["max_retries"], 0)
            self.assertEqual(model.call_args.kwargs["max_tokens"], 128)
            self.assertEqual(model.call_args.kwargs["api_base"], "https://api.deepseek.com")
            self.assertIs(create.call_args.kwargs["model"], model.return_value)
            self.assertIsNone(create.call_args.kwargs["checkpointer"])

    def test_visible_text_filters_internal_content(self):
        metadata = {"langgraph_node": "model"}
        chunk = AIMessageChunk(content=[{"type": "reasoning", "reasoning": "hidden"},
                                        {"type": "text", "text": "你好"}])
        self.assertEqual(visible_text(chunk, metadata), "你好")
        self.assertEqual(visible_text(chunk, {"langgraph_node": "tools"}), "")
        self.assertEqual(visible_text(HumanMessage(content="question"), metadata), "")
        self.assertEqual(visible_text(AIMessageChunk(content="", additional_kwargs={"reasoning_content": "hidden"}), metadata), "")


class FakeAgent:
    def __init__(self, reason="stop", empty=False):
        self.reason, self.empty, self.closed = reason, empty, False

    async def astream(self, *args, **kwargs):
        try:
            if not self.empty:
                yield AIMessageChunk(content="你好"), {"langgraph_node": "model"}
                yield AIMessageChunk(content="世界"), {"langgraph_node": "model"}
            yield AIMessageChunk(content="", response_metadata={"finish_reason": self.reason}), {"langgraph_node": "model"}
        finally:
            self.closed = True


class AgentCheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_counts_chunks_and_closes(self):
        agent = FakeAgent()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(await stream_answer(agent), 2)
        self.assertEqual(output.getvalue(), "你好世界")
        self.assertTrue(agent.closed)

    async def test_incomplete_and_empty_are_not_success(self):
        for agent in (FakeAgent(reason="length"), FakeAgent(empty=True)):
            with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                await stream_answer(agent)
            self.assertTrue(agent.closed)

    async def test_probe_hides_builtin_tools(self):
        from unittest.mock import Mock
        request, handler = Mock(), AsyncMock()
        await probe_without_tools.awrap_model_call(request, handler)
        request.override.assert_called_once_with(tools=[])
        handler.assert_awaited_once_with(request.override.return_value)

    async def test_invalid_settings_do_not_call_agent(self):
        with patch("app.agent.check.load_model_settings", side_effect=OSError("fake-secret")), \
                patch("app.agent.check.build_probe_agent") as build, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(await main(), 1)
        build.assert_not_called()
        self.assertNotIn("fake-secret", output.getvalue())

    async def test_success_and_failure_exit_codes(self):
        for failure in (None, TimeoutError("fake-secret"), RuntimeError("fake-secret")):
            with patch("app.agent.check.load_model_settings"), patch("app.agent.check.build_probe_agent"), \
                    patch("app.agent.check.stream_answer", new_callable=AsyncMock) as stream, \
                    redirect_stdout(io.StringIO()) as output:
                stream.return_value = 2
                stream.side_effect = failure
                self.assertEqual(await main(), 0 if failure is None else 1)
                self.assertNotIn("fake-secret", output.getvalue())
