"""真实终端业务/Deep Agents 图/临时 MySQL，只有用户输入和供应商流是测试替身。"""

import asyncio
import io
import os
import signal
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import select

from app.cli.chat import check_database_ready, run_chat
from app.cli.terminal import Terminal
from app.core.config import ModelSettings, TracingSettings
from app.db.session import build_session_factory
from app.models import AuthSession, ChatSession, Message, User
from app.schemas import LoginRequest, RegisterRequest
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.login import login_user
from app.services.logout import logout_user


class ScriptTerminal(Terminal):
    def __init__(self, lines, secrets):
        super().__init__(io.StringIO())
        self.lines, self.secrets = iter(lines), iter(secrets)

    def read(self, prompt):
        try:
            return next(self.lines)
        except StopIteration:
            raise EOFError from None

    def secret(self, prompt):
        return next(self.secrets)


class TerminalChatMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.email = f"cli-{uuid4().hex}@example.com"
        self.password = "cli-test-password"
        self.model = ModelSettings(_env_file=None, provider="deepseek", name="deepseek-v4-pro", api_key="fake-model-key")
        self.trace = TracingSettings(_env_file=None, tracing=False, api_key=None)
        self.inputs = []
        self.interrupt = False
        self.model_closed = False

        async def provider(_model, messages, **kwargs):
            if kwargs.get("tools"):
                yield ChatGenerationChunk(message=AIMessageChunk(content="No update", response_metadata={"finish_reason": "stop"}))
                return
            self.inputs.append(messages)
            try:
                yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "CLI visible thinking"}))
                if self.interrupt:
                    asyncio.get_running_loop().call_soon(os.kill, os.getpid(), signal.SIGINT)
                    await asyncio.sleep(10)
                yield ChatGenerationChunk(message=AIMessageChunk(content="CLI answer"))
                yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": "stop"}))
            finally:
                self.model_closed = True

        mock = patch("langchain_deepseek.ChatDeepSeek._astream", provider)
        mock.start()
        self.addCleanup(mock.stop)

    def run_lines(self, lines, secrets):
        terminal = ScriptTerminal(lines, secrets)
        code = run_chat(terminal, self.factory, self.model, self.trace)
        text = terminal.output.getvalue()
        self.assertNotIn(self.password, text)
        self.assertNotIn("fake-model-key", text)
        return code, text

    def existing_user(self):
        return register_user(RegisterRequest(email=self.email, password=self.password, display_name="CLI test"), self.factory)

    def rows(self):
        with self.factory() as session:
            user = session.scalar(select(User).where(User.email == self.email))
            chats = session.scalars(select(ChatSession).where(ChatSession.user_id == user.user_id)
                                    .order_by(ChatSession.chat_session_id)).all()
            messages = session.scalars(select(Message).join(ChatSession).where(ChatSession.user_id == user.user_id)
                                       .order_by(Message.message_id)).all()
            logins = session.scalars(select(AuthSession).where(AuthSession.user_id == user.user_id)).all()
            return user, chats, messages, logins

    def test_preflight_register_two_turns_history_new_and_logout(self):
        check_database_ready(self.engine)
        code, output = self.run_lines(["r", self.email, "CLI test", "First question", "Second question",
                                       "/history", "/new", "/history", "/quit"], [self.password, self.password])
        self.assertEqual(code, 0)
        user, chats, messages, logins = self.rows()
        self.assertEqual(len(chats), 2)
        self.assertEqual([m.role for m in messages], ["USER", "ASSISTANT", "USER", "ASSISTANT"])
        self.assertEqual([m.content for m in self.inputs[1][1:]], ["First question", "CLI answer", "Second question"])
        self.assertTrue(all("CLI visible thinking" not in m.content for m in messages))
        self.assertNotEqual(user.password_hash, self.password)
        self.assertEqual(len(logins), 1)
        self.assertIsNotNone(logins[0].revoked_at)
        self.assertIn("[Thinking]", output)
        self.assertIn("[Answer]", output)
        self.assertIn("[History]", output)
        self.assertIn("No saved messages yet", output)

    def test_existing_account_login_uses_new_chat_and_preserves_other_login(self):
        user = self.existing_user()
        other = login_user(LoginRequest(email=self.email, password=self.password), self.factory)
        try:
            code, _ = self.run_lines(["l", self.email, "Question", "/quit"], [self.password])
            self.assertEqual(code, 0)
            self.assertEqual(get_current_user(other.token, self.factory).user_id, user.user_id)
            _, chats, messages, logins = self.rows()
            self.assertEqual((len(chats), len(messages), len(logins)), (1, 2, 2))
            self.assertEqual(sum(login.revoked_at is not None for login in logins), 1)
        finally:
            logout_user(other.token, self.factory)

    def test_generation_limit_blocks_eleventh_question_before_user_insert(self):
        self.existing_user()
        code, output = self.run_lines(["l", self.email] + [f"Question {i}" for i in range(11)] + ["/quit"], [self.password])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.inputs), 10)
        self.assertEqual(len(self.rows()[2]), 20)
        self.assertIn("RATE_LIMITED", output)

    def test_failed_login_has_no_chat_or_model_call(self):
        self.existing_user()
        code, output = self.run_lines(["l", self.email] * 3, ["wrong-password"] * 3)
        self.assertEqual(code, 0)
        self.assertFalse(self.inputs)
        _, chats, messages, logins = self.rows()
        self.assertEqual((chats, messages, logins), ([], [], []))
        self.assertIn("Three failed logins", output)

    def test_ctrl_c_during_real_graph_cleans_database_before_logging_out(self):
        self.existing_user()
        self.interrupt = True
        code, output = self.run_lines(["l", self.email, "Interrupt this question"], [self.password])
        self.assertEqual(code, 0)
        self.assertTrue(self.model_closed)
        _, _, messages, logins = self.rows()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertIsNotNone(logins[0].revoked_at)
        self.assertNotIn("[Saved]", output)
