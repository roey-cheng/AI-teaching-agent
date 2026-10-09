"""会话管理的隔离 MySQL 验收；仅使用临时容器注入的 Engine。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import build_session_factory
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.schemas import LoginRequest, RegisterRequest, RenameSessionRequest
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.chat_sessions import create_chat_session, list_chat_sessions, rename_chat_session
from app.services.errors import SessionNotFoundError, SessionUnavailableError
from app.services.login import login_user


class ChatSessionMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.user = self.authenticated_user()

    def authenticated_user(self):
        email = f"chat-create-{uuid4().hex}@example.com"
        register_user(RegisterRequest(email=email, password="test-password", display_name="Chat tester"), self.factory)
        login = login_user(LoginRequest(email=email, password="test-password"), self.factory)
        return get_current_user(login.token, self.factory)

    def rows(self):
        with self.factory() as session:
            return session.execute(select(ChatSession.__table__).where(
                ChatSession.user_id == int(self.user.user_id))).all()

    def test_creation_persists_defaults_and_returns_database_id_and_utc(self):
        result = create_chat_session(self.user, self.factory)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(result.session_id, str(row.chat_session_id))
        self.assertEqual(row.user_id, int(self.user.user_id))
        self.assertEqual(row.title, "new chat session")
        self.assertFalse(row.title_is_manual)
        self.assertEqual(row.created_at, row.updated_at)
        self.assertEqual(row.created_at, row.last_activity_at)
        self.assertEqual(result.created_at, row.created_at.replace(tzinfo=UTC))
        self.assertEqual(set(result.model_dump()), {
            "session_id", "title", "created_at", "updated_at", "last_activity_at",
        })

    def test_multiple_calls_and_users_create_separate_owned_sessions(self):
        first = create_chat_session(self.user, self.factory)
        second = create_chat_session(self.user, self.factory)
        other_user = self.authenticated_user()
        third = create_chat_session(other_user, self.factory)
        self.assertEqual(len({first.session_id, second.session_id, third.session_id}), 3)
        self.assertEqual(len(self.rows()), 2)
        with self.factory() as session:
            self.assertEqual(session.get(ChatSession, int(third.session_id)).user_id, int(other_user.user_id))

    def test_creation_does_not_change_users_logins_messages_or_memories(self):
        def snapshot():
            with self.factory() as session:
                return [session.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns)).all()
                        for model in (User, AuthSession, Message, AgentMemory)]
        before = snapshot()
        create_chat_session(self.user, self.factory)
        self.assertEqual(snapshot(), before)

    def test_failure_before_commit_rolls_back_insert(self):
        def fail_before_commit(session):
            raise SQLAlchemyError("fixture-private-error")
        event.listen(self.factory, "before_commit", fail_before_commit)
        try:
            with self.assertRaises(SessionUnavailableError):
                create_chat_session(self.user, self.factory)
        finally:
            event.remove(self.factory, "before_commit", fail_before_commit)
        self.assertEqual(self.rows(), [])
        create_chat_session(self.user, self.factory)
        self.assertEqual(len(self.rows()), 1)

    def test_response_validation_failure_rolls_back_flushed_insert(self):
        with patch("app.services.chat_sessions.SessionResponse.model_validate", side_effect=ValueError("fixture")):
            with self.assertRaises(ValueError):
                create_chat_session(self.user, self.factory)
        self.assertEqual(self.rows(), [])

    def test_list_empty_even_when_other_user_has_sessions(self):
        other = self.authenticated_user()
        create_chat_session(other, self.factory)
        self.assertEqual(list_chat_sessions(self.user, self.factory).model_dump(), {"items": []})
        self.assertEqual(self.rows(), [])

    def test_list_filters_owner_and_sorts_activity_then_numeric_id(self):
        sessions = [create_chat_session(self.user, self.factory) for _ in range(3)]
        other = self.authenticated_user()
        hidden = create_chat_session(other, self.factory)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            for index, chat in enumerate(sessions):
                # 前两条活动时间相同，第三条虽然编号更大但活动更早，应排最后。
                session.execute(update(ChatSession).where(ChatSession.chat_session_id == int(chat.session_id))
                                .values(last_activity_at=now if index < 2 else now - timedelta(days=1),
                                        updated_at=now + timedelta(days=index)))
        result = list_chat_sessions(self.user, self.factory)
        self.assertEqual([item.session_id for item in result.items],
                         [sessions[1].session_id, sessions[0].session_id, sessions[2].session_id])
        self.assertNotIn(hidden.session_id, [item.session_id for item in result.items])
        self.assertEqual([item.session_id for item in list_chat_sessions(other, self.factory).items],
                         [hidden.session_id])

    def test_list_returns_all_sessions_without_silent_page_limit(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            session.add_all([ChatSession(user_id=int(self.user.user_id), title=f"Chat {i}",
                                         created_at=now, updated_at=now, last_activity_at=now)
                             for i in range(105)])
        result = list_chat_sessions(self.user, self.factory)
        self.assertEqual(len(result.items), 105)
        ids = [int(item.session_id) for item in result.items]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_list_only_reads_chat_table_and_leaves_all_data_unchanged(self):
        create_chat_session(self.user, self.factory)
        def snapshot():
            with self.factory() as session:
                return [session.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns)).all()
                        for model in (User, AuthSession, ChatSession, Message, AgentMemory)]
        before = snapshot()
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            list_chat_sessions(self.user, self.factory)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].lstrip().upper().startswith("SELECT"))
        self.assertIn("FROM chat_sessions", statements[0])
        self.assertNotIn("messages", statements[0])
        self.assertEqual(snapshot(), before)

    def test_rename_persists_trimmed_title_without_reordering_or_changing_other_rows(self):
        first = create_chat_session(self.user, self.factory)
        create_chat_session(self.user, self.factory)
        def snapshot():
            with self.factory() as session:
                return [session.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns)).all()
                        for model in (User, AuthSession, Message, AgentMemory)]
        before_other = snapshot()
        before_rows = {row.chat_session_id: row for row in self.rows()}
        before_order = [item.session_id for item in list_chat_sessions(self.user, self.factory).items]
        updated_time = first.updated_at + timedelta(seconds=1)
        with patch("app.services.chat_sessions.datetime") as clock:
            clock.now.return_value = updated_time
            result = rename_chat_session(self.user, first.session_id,
                                         RenameSessionRequest(title="  Python 学习🙂  "), self.factory)
        after_rows = {row.chat_session_id: row for row in self.rows()}
        row = after_rows[int(first.session_id)]
        self.assertEqual(row.title, "Python 学习🙂")
        self.assertTrue(row.title_is_manual)
        self.assertEqual(result.updated_at, updated_time)
        self.assertEqual(result.created_at, first.created_at)
        self.assertEqual(result.last_activity_at, first.last_activity_at)
        self.assertEqual(row.updated_at, updated_time.replace(tzinfo=None))
        for key in before_rows:
            if key != int(first.session_id):
                self.assertEqual(after_rows[key], before_rows[key])
        self.assertEqual([item.session_id for item in list_chat_sessions(self.user, self.factory).items], before_order)
        self.assertEqual(snapshot(), before_other)

    def test_rename_other_user_and_missing_session_have_same_error(self):
        other = self.authenticated_user()
        hidden = create_chat_session(other, self.factory)
        failures = []
        for session_id in (hidden.session_id, "18446744073709551615"):
            with self.assertRaises(SessionNotFoundError) as caught:
                rename_chat_session(self.user, session_id, RenameSessionRequest(title="Forbidden"), self.factory)
            failures.append((caught.exception.code, str(caught.exception)))
        self.assertEqual(failures[0], failures[1])
        self.assertEqual(list_chat_sessions(other, self.factory).items[0].title, hidden.title)
        self.assertEqual(self.rows(), [])

    def test_rename_same_title_marks_manual_and_accepts_100_characters(self):
        chat = create_chat_session(self.user, self.factory)
        rename_chat_session(self.user, chat.session_id, RenameSessionRequest(title=chat.title), self.factory)
        self.assertTrue(self.rows()[0].title_is_manual)
        title = "文" * 100
        response = rename_chat_session(self.user, chat.session_id, RenameSessionRequest(title=title), self.factory)
        self.assertEqual(response.title, title)
        self.assertEqual(self.rows()[0].title, title)

    def test_rename_is_allowed_while_message_is_running(self):
        chat = create_chat_session(self.user, self.factory)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            session.add(Message(chat_session_id=int(chat.session_id), role="USER", content="Question",
                                client_message_key=str(uuid4()), attempt_id=str(uuid4()), generation_status="RUNNING",
                                created_at=now, updated_at=now))
        result = rename_chat_session(self.user, chat.session_id, RenameSessionRequest(title="Still running"), self.factory)
        self.assertEqual(result.title, "Still running")
        with self.factory() as session:
            self.assertEqual(session.scalar(select(Message.generation_status).where(
                Message.chat_session_id == int(chat.session_id))), "RUNNING")

    def test_rename_commit_and_response_failures_roll_back_all_changes(self):
        chat = create_chat_session(self.user, self.factory)
        before = self.rows()
        def fail(session):
            raise SQLAlchemyError("fixture-private-error")
        event.listen(self.factory, "before_commit", fail)
        try:
            with self.assertRaises(SessionUnavailableError):
                rename_chat_session(self.user, chat.session_id, RenameSessionRequest(title="Rollback"), self.factory)
        finally:
            event.remove(self.factory, "before_commit", fail)
        self.assertEqual(self.rows(), before)
        with patch("app.services.chat_sessions.SessionResponse.model_validate", side_effect=ValueError("fixture")):
            with self.assertRaises(ValueError):
                rename_chat_session(self.user, chat.session_id, RenameSessionRequest(title="Rollback again"), self.factory)
        self.assertEqual(self.rows(), before)

    def test_concurrent_renames_complete_and_preserve_activity(self):
        chat = create_chat_session(self.user, self.factory)
        barrier = Barrier(2)
        def rename(title):
            barrier.wait(timeout=10)
            return rename_chat_session(self.user, chat.session_id, RenameSessionRequest(title=title), self.factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(rename, title) for title in ("First", "Second")]
            self.assertEqual([job.result(timeout=15).title for job in jobs], ["First", "Second"])
        row = self.rows()[0]
        self.assertIn(row.title, ("First", "Second"))
        self.assertTrue(row.title_is_manual)
        self.assertEqual(row.created_at, chat.created_at.replace(tzinfo=None))
        self.assertEqual(row.last_activity_at, chat.last_activity_at.replace(tzinfo=None))
