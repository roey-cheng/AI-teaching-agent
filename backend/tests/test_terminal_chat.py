"""终端入口离线测试；通过依赖替身避免访问用户配置、数据库和真实模型。"""

import asyncio
from datetime import datetime
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.cli.chat import DatabaseNotReadyError, authenticate, check_database_ready, launch, run_chat
from app.cli.terminal import EventPrinter, Terminal, terminal_text
from app.core.config import DatabaseSettings, ModelSettings, TracingSettings
from app.schemas import (
    AgentProgressData, AssistantMessageResponse, MessageDeltaData, MessageDoneData, MessageHistoryResponse,
    ReasoningDeltaData, UserResponse,
)
from app.services.errors import AuthenticationRequiredError, InvalidCredentialsError


class ScriptTerminal(Terminal):
    def __init__(self, lines=(), secrets=()):
        super().__init__(io.StringIO())
        self.lines, self.secrets = iter(lines), iter(secrets)

    def read(self, prompt):
        try:
            value = next(self.lines)
        except StopIteration:
            raise EOFError from None
        if isinstance(value, BaseException):
            raise value
        return value

    def secret(self, prompt):
        return next(self.secrets)


def model():
    return ModelSettings(_env_file=None, provider="deepseek", name="deepseek-v4-pro", api_key="fake-model-key")


def tracing():
    return TracingSettings(_env_file=None, tracing=False, api_key=None)


class TerminalOutputTest(unittest.IsolatedAsyncioTestCase):
    def test_control_characters_cannot_execute_even_when_split_across_chunks(self):
        value = "\x1b[2J\rback\b\x9b31m\x07🙂\n\t"
        self.assertEqual(terminal_text(value), "[2Jback31m🙂\n\t")
        terminal = ScriptTerminal()
        terminal.write("\x1b", end="")
        terminal.write("]52;clipboard\x07", end="")
        self.assertNotIn("\x1b", terminal.output.getvalue())
        self.assertNotIn("\x07", terminal.output.getvalue())

    async def test_event_sections_stream_and_done_does_not_repeat_answer(self):
        terminal, attempt = ScriptTerminal(), str(uuid4())
        printer = EventPrinter(terminal)
        await printer(AgentProgressData(attempt_id=attempt, stage="thinking"))
        await printer(ReasoningDeltaData(attempt_id=attempt, text="reason"))
        await printer(MessageDeltaData(attempt_id=attempt, text="answer-unique"))
        await printer(MessageDoneData(attempt_id=attempt, assistant_message=AssistantMessageResponse(
            message_id="2", role="ASSISTANT", content="answer-unique", in_reply_to_message_id="1",
            created_at=datetime(2026, 10, 11),
        )))
        text = terminal.output.getvalue()
        self.assertIn("[Thinking]\nreason", text)
        self.assertIn("[Answer]\nanswer-unique", text)
        self.assertEqual(text.count("answer-unique"), 1)
        self.assertIn("[Saved]", text)


class TerminalAuthenticationTest(unittest.TestCase):
    def test_register_then_login_hides_password(self):
        terminal = ScriptTerminal(["r", "a@example.com", "Test"], ["fake-password", "fake-password"])
        with patch("app.cli.chat.register_user") as register, patch("app.cli.chat.login_user") as login:
            self.assertIs(authenticate(terminal, "factory"), login.return_value)
        self.assertEqual(register.call_args.args[0].display_name, "Test")
        self.assertEqual(login.call_args.args[0].password.get_secret_value(), "fake-password")
        self.assertNotIn("fake-password", terminal.output.getvalue())

    def test_three_failed_logins_stop_without_fourth_call(self):
        terminal = ScriptTerminal(["l", "a@example.com"] * 3, ["fake-password"] * 3)
        with patch("app.cli.chat.login_user", side_effect=InvalidCredentialsError()) as login:
            self.assertIsNone(authenticate(terminal, "factory"))
        self.assertEqual(login.call_count, 3)
        self.assertIn("Three failed logins", terminal.output.getvalue())

    def test_password_mismatch_and_bad_input_do_not_register(self):
        terminal = ScriptTerminal(["r", "a@example.com", "r", "invalid-email", "Test", "q"],
                                  ["password-a", "password-b", "password-a", "password-a"])
        with patch("app.cli.chat.register_user") as register, patch("app.cli.chat.login_user") as login:
            self.assertIsNone(authenticate(terminal, "factory"))
        register.assert_not_called()
        login.assert_not_called()


class ChatLoopTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="1", email="a@example.com", display_name="Test")
        self.token = SecretStr("fake-login-token")
        self.mocks = {}
        for name in ("authenticate", "get_current_user", "create_chat_session", "get_message_history", "logout_user"):
            p = patch(f"app.cli.chat.{name}")
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        self.mocks["authenticate"].return_value = SimpleNamespace(token=self.token)
        self.mocks["get_current_user"].return_value = self.user
        self.mocks["create_chat_session"].return_value = SimpleNamespace(session_id="10")
        self.mocks["get_message_history"].return_value = MessageHistoryResponse(session_id="10", is_generating=False, items=[])
        p = patch("app.cli.chat.execute_chat_turn", new_callable=AsyncMock)
        self.execute = p.start()
        self.addCleanup(p.stop)

    def run_lines(self, lines):
        terminal = ScriptTerminal(lines)
        code = run_chat(terminal, "factory", model(), tracing())
        return code, terminal.output.getvalue()

    def test_blank_commands_history_new_and_quit_do_not_call_model(self):
        code, text = self.run_lines([" ", "/nope", "/help", "/history", "/new", "/quit"])
        self.assertEqual(code, 0)
        self.execute.assert_not_called()
        self.assertEqual(self.mocks["create_chat_session"].call_count, 2)
        self.mocks["get_message_history"].assert_called_once()
        self.mocks["logout_user"].assert_called_once_with(self.token, "factory")
        self.assertIn("Unknown command", text)

    def test_input_preserved_unique_keys_shared_registry_and_real_rate_limit(self):
        async def accepted(*args, **kwargs):
            kwargs["check_new_message"](args[2])
        self.execute.side_effect = accepted
        code, text = self.run_lines(["  question  "] * 11 + ["/quit"])
        self.assertEqual(code, 0)
        calls = self.execute.call_args_list
        self.assertEqual(calls[0].args[2].content, "  question  ")
        self.assertEqual(len({c.args[2].client_message_key for c in calls}), 11)
        self.assertTrue(all(c.args[4] is calls[0].args[4] for c in calls))
        self.assertIn("RATE_LIMITED", text)
        self.assertEqual(self.mocks["get_current_user"].call_count, 12)

    def test_uncertain_result_does_not_resend_or_start_new_session_until_history_checked(self):
        self.execute.side_effect = RuntimeError("private-db-password")
        code, text = self.run_lines(["question", "do not send", "/new", "/history", "/quit"])
        self.assertEqual(code, 0)
        self.execute.assert_awaited_once()
        self.mocks["create_chat_session"].assert_called_once()
        self.assertIn("Sending is paused", text)
        self.assertNotIn("private-db-password", text)

    def test_history_failure_after_uncertainty_exits_without_new_generation(self):
        self.execute.side_effect = RuntimeError("private-db-password")
        self.mocks["get_message_history"].side_effect = RuntimeError("private-query")
        code, text = self.run_lines(["question", "/history", "never send"])
        self.assertEqual(code, 1)
        self.execute.assert_awaited_once()
        self.assertNotIn("private", text)

    def test_authentication_is_rechecked_and_expired_login_stops_model_call(self):
        self.mocks["get_current_user"].side_effect = [self.user, AuthenticationRequiredError()]
        code, _ = self.run_lines(["question"])
        self.assertEqual(code, 1)
        self.execute.assert_not_called()
        self.mocks["logout_user"].assert_called_once()

    def test_eof_or_keyboard_interrupt_logs_out_without_deleting_history(self):
        for ending in (EOFError(), KeyboardInterrupt()):
            self.mocks["logout_user"].reset_mock()
            code, text = self.run_lines([ending])
            self.assertEqual(code, 0)
            self.mocks["logout_user"].assert_called_once()
            self.assertIn("Saved chat history is kept", text)

    def test_runner_sigint_waits_for_executor_cleanup_before_logout(self):
        import os
        import signal
        steps = []
        async def running(*args, **kwargs):
            asyncio.get_running_loop().call_soon(os.kill, os.getpid(), signal.SIGINT)
            try:
                await asyncio.sleep(10)
            finally:
                await asyncio.sleep(0)
                steps.append("execution-cleaned")
        self.execute.side_effect = running
        self.mocks["logout_user"].side_effect = lambda *args: steps.append("logged-out")
        code, _ = self.run_lines(["question"])
        self.assertEqual(code, 0)
        self.assertEqual(steps, ["execution-cleaned", "logged-out"])

    def test_logout_failure_is_not_reported_as_success_and_keeps_secret_hidden(self):
        self.mocks["logout_user"].side_effect = RuntimeError("fake-login-token")
        code, text = self.run_lines(["/quit"])
        self.assertEqual(code, 1)
        self.assertIn("Logout could not be confirmed", text)
        self.assertNotIn("fake-login-token", text)


class TerminalStartupTest(unittest.TestCase):
    def database(self):
        return DatabaseSettings(_env_file=None, name="fake_db", user="fake_user", password="fake-db-secret")

    def test_declining_does_not_start_chat_or_expose_credentials(self):
        terminal = ScriptTerminal(["n"])
        with patch("app.cli.chat.build_database_engine") as engine, patch("app.cli.chat.check_database_ready"), \
                patch("app.cli.chat.run_chat") as chat:
            self.assertEqual(launch(terminal, self.database(), model(), tracing()), 0)
            chat.assert_not_called()
            engine.return_value.dispose.assert_called_once()
        self.assertNotIn("fake-db-secret", terminal.output.getvalue())
        self.assertNotIn("fake-model-key", terminal.output.getvalue())

    def test_unavailable_db_stops_before_confirmation_or_model(self):
        terminal = ScriptTerminal()
        with patch("app.cli.chat.build_database_engine") as engine, \
                patch("app.cli.chat.check_database_ready", side_effect=SQLAlchemyError("fake-db-secret")), \
                patch("app.cli.chat.run_chat") as chat:
            self.assertEqual(launch(terminal, self.database(), model(), tracing()), 1)
            chat.assert_not_called()
            engine.return_value.dispose.assert_called_once()
        self.assertNotIn("fake-db-secret", terminal.output.getvalue())

    def test_tracing_warning_precedes_confirmation(self):
        terminal = ScriptTerminal(["n"])
        config = TracingSettings(_env_file=None, tracing=True, api_key="fake-trace-secret")
        with patch("app.cli.chat.build_database_engine"), patch("app.cli.chat.check_database_ready"):
            launch(terminal, self.database(), model(), config)
        self.assertIn("LangSmith is ON", terminal.output.getvalue())
        self.assertNotIn("fake-trace-secret", terminal.output.getvalue())

    def test_preflight_checks_heads_and_tables_without_writing(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.dialect.name = "mysql"
        connection.dialect.is_mariadb = False
        connection.dialect.server_version_info = (8, 4, 11)
        with patch("app.cli.chat.MigrationContext") as context, patch("app.cli.chat.inspect") as inspector:
            context.configure.return_value.get_current_heads.return_value = ("20260925_0001",)
            inspector.return_value.get_table_names.return_value = ["users", "auth_sessions", "chat_sessions", "messages", "agent_memory"]
            check_database_ready(engine)
            context.configure.return_value.get_current_heads.return_value = ()
            with self.assertRaises(DatabaseNotReadyError):
                check_database_ready(engine)
            connection.execute.assert_not_called()

    def test_non_interactive_main_never_reads_credentials_or_database(self):
        from app.cli.chat import main
        with patch("sys.argv", ["chat"]), patch("sys.stdin.isatty", return_value=False), \
                patch("app.cli.chat.Terminal"), patch("app.cli.chat.load_database_settings") as load:
            self.assertEqual(main(), 1)
        load.assert_not_called()
