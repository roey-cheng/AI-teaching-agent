"""五个资源接口的隔离 MySQL 验收；仅用测试库，不调用模型。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.core.http_config import HTTPSettings
from app.core.runtime import open_backend_runtime
from app.core.runtime_lock import RuntimeLock
from app.db.session import build_session_factory
from app.main import create_app
from app.models import AgentMemory, AuthSession, ChatSession, Message, User

BASE = "/api/v1/chat/sessions"
MEMORY = "/api/v1/me/memory"


class ResourceHTTPFixture(unittest.TestCase):
    engine = None

    def setUp(self):
        self.assertEqual(self.engine.url.database, "migration_test")
        self.assertNotEqual(self.engine.url.port, 3306)
        folder = self.enterContext(TemporaryDirectory())
        self.factory = build_session_factory(self.engine)
        self.user_ids = []
        self.headers = {"Origin": "http://localhost:5173"}

        @contextmanager
        def runtime():
            with open_backend_runtime(self.engine, lock=RuntimeLock(Path(folder) / "resources.lock")) as active:
                yield active

        app = create_app(runtime_factory=runtime, http_settings=HTTPSettings(
            _env_file=None, allowed_origins=[self.headers["Origin"]]))
        self.client = self.enterContext(TestClient(app))
        self.user_id, self.token = self.account()

    def tearDown(self):
        # 只清理本用例的临时账号及其子记录，避免干扰旧用例；不接受项目库目标。
        self.assertEqual(self.engine.url.database, "migration_test")
        self.assertNotEqual(self.engine.url.port, 3306)
        if self.user_ids:
            with self.factory.begin() as session:
                chats = select(ChatSession.chat_session_id).where(ChatSession.user_id.in_(self.user_ids))
                session.execute(delete(Message).where(Message.chat_session_id.in_(chats), Message.role == "ASSISTANT"))
                session.execute(delete(Message).where(Message.chat_session_id.in_(chats)))
                session.execute(delete(ChatSession).where(ChatSession.user_id.in_(self.user_ids)))
                session.execute(delete(AgentMemory).where(AgentMemory.user_id.in_(self.user_ids)))
                session.execute(delete(AuthSession).where(AuthSession.user_id.in_(self.user_ids)))
                session.execute(delete(User).where(User.user_id.in_(self.user_ids)))

    def account(self):
        # 不让新账号登录轮换掉旧 Cookie，模拟两位独立浏览器用户。
        self.client.cookies.clear()
        data = {"email": f"resources-{uuid4().hex}@example.com", "password": "http-test-password", "display_name": "Test"}
        response = self.client.post("/api/v1/auth/register", json=data, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        user_id = int(response.json()["user_id"])
        self.user_ids.append(user_id)
        response = self.client.post("/api/v1/auth/login", json={
            "email": data["email"], "password": data["password"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return user_id, self.client.cookies.get("chat_session")

    def use_token(self, token):
        self.client.cookies.clear()
        self.client.cookies.set("chat_session", token)

    def create(self):
        response = self.client.post(BASE, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def question(self, chat_id, *, status="FAILED", content="Question", now=None):
        now = now or datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            row = Message(chat_session_id=int(chat_id), role="USER", content=content,
                          client_message_key=str(uuid4()), attempt_id=str(uuid4()), generation_status=status,
                          created_at=now, updated_at=now,
                          generation_error_code="PRIVATE_ERROR" if status == "FAILED" else None,
                          generation_error_message="private-sql-and-credentials" if status == "FAILED" else None)
            session.add(row)
            session.flush()
            return row.message_id, row.attempt_id


class ResourceHTTPMySQLTest(ResourceHTTPFixture):
    def test_create_list_rename_persist_and_activity_order_unchanged(self):
        self.assertEqual(self.client.get(BASE).json(), {"items": []})
        first, second = self.create(), self.create()
        ids = [item["session_id"] for item in self.client.get(BASE).json()["items"]]
        self.assertEqual(ids, [second["session_id"], first["session_id"]])
        response = self.client.patch(BASE + "/" + first["session_id"], json={"title": " 中文标题 "}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "中文标题")
        self.assertEqual(response.json()["last_activity_at"], first["last_activity_at"])
        self.assertEqual([item["session_id"] for item in self.client.get(BASE).json()["items"]], ids)
        with self.factory() as session:
            row = session.get(ChatSession, int(first["session_id"]))
            self.assertEqual(row.user_id, self.user_id)
            self.assertTrue(row.title_is_manual)
            self.assertEqual(row.title, "中文标题")
        history = self.client.get(BASE + "/" + first["session_id"] + "/messages")
        self.assertEqual(history.json(), {"session_id": first["session_id"], "is_generating": False, "items": []})

    def test_other_user_sessions_history_and_rename_are_indistinguishable_from_missing(self):
        chat = self.create()
        self.question(chat["session_id"], content="owner-only-text")
        other_id, other_token = self.account()
        other_chat = self.create()
        self.assertEqual([i["session_id"] for i in self.client.get(BASE).json()["items"]], [other_chat["session_id"]])
        for identifier in (chat["session_id"], "18446744073709551615"):
            for response in (
                self.client.get(BASE + "/" + identifier + "/messages"),
                self.client.patch(BASE + "/" + identifier, json={"title": "Stolen"}, headers=self.headers),
            ):
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "SESSION_NOT_FOUND")
                self.assertNotIn("owner-only-text", response.text)
        self.use_token(self.token)
        self.assertEqual([i["session_id"] for i in self.client.get(BASE).json()["items"]], [chat["session_id"]])
        self.assertEqual(self.client.get(BASE + "/" + chat["session_id"] + "/messages").status_code, 200)

    def test_history_order_answers_failed_summary_and_retry_rules(self):
        chat = self.create()["session_id"]
        now = datetime.now(UTC).replace(tzinfo=None)
        old, _ = self.question(chat, now=now, content="Old failed question")
        succeeded, _ = self.question(chat, now=now, status="SUCCEEDED", content="Success question")
        with self.factory.begin() as session:
            answer = Message(chat_session_id=int(chat), role="ASSISTANT", content="Saved answer",
                             in_reply_to_message_id=succeeded, created_at=now, updated_at=now)
            session.add(answer)
            session.flush()
            answer_id = answer.message_id
        latest, _ = self.question(chat, now=now, content="Latest failed question")
        response = self.client.get(BASE + "/" + chat + "/messages")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual([i["message_id"] for i in items], list(map(str, [old, succeeded, answer_id, latest])))
        self.assertFalse(items[0]["generation"]["can_retry"])
        self.assertTrue(items[-1]["generation"]["can_retry"])
        self.assertEqual(items[1]["generation"]["assistant_message_id"], str(answer_id))
        self.assertEqual(items[2]["in_reply_to_message_id"], str(succeeded))
        self.assertNotIn("generation", items[2])
        self.assertEqual(items[0]["generation"]["error"]["code"], "GENERATION_FAILED")
        self.assertNotIn("private-sql-and-credentials", response.text)
        self.assertNotIn("client_message_key", response.text)

    def test_shared_running_registry_and_cleanup_busy_state(self):
        chat = self.create()["session_id"]
        message_id, attempt_id = self.question(chat, status="RUNNING")
        registry = self.client.app.state.runtime.registry
        with registry.locked(int(chat)) as slot:
            self.assertTrue(slot.claim(attempt_id))
        try:
            response = self.client.get(BASE + "/" + chat + "/messages")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["is_generating"])
            self.assertEqual(response.json()["items"][0]["generation"]["status"], "RUNNING")
            # 生成中仍可改标题；不改变任务编号或登记。
            self.assertEqual(self.client.patch(BASE + "/" + chat, json={"title": "Running"}, headers=self.headers).status_code, 200)
            with registry.locked(int(chat)):
                with self.factory.begin() as session:
                    session.execute(update(Message).where(Message.message_id == message_id).values(
                        generation_status="FAILED", generation_error_code="GENERATION_TIMEOUT",
                        generation_error_message="Response generation timed out."))
            during_cleanup = self.client.get(BASE + "/" + chat + "/messages").json()
            self.assertTrue(during_cleanup["is_generating"])
            self.assertFalse(during_cleanup["items"][0]["generation"]["can_retry"])
        finally:
            with registry.locked(int(chat)) as slot:
                slot.release(attempt_id)
        after_cleanup = self.client.get(BASE + "/" + chat + "/messages").json()
        self.assertFalse(after_cleanup["is_generating"])
        self.assertTrue(after_cleanup["items"][0]["generation"]["can_retry"])

    def test_orphan_running_snapshot_returns_503_without_repairing(self):
        chat = self.create()["session_id"]
        identifier, _ = self.question(chat, status="RUNNING")
        response = self.client.get(BASE + "/" + chat + "/messages")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "MESSAGE_HISTORY_UNAVAILABLE")
        with self.factory() as session:
            self.assertEqual(session.get(Message, identifier).generation_status, "RUNNING")

    def test_memory_is_read_only_sorted_and_owned_by_logged_in_user(self):
        self.assertEqual(self.client.get(MEMORY).json(), {"items": []})
        other_id, other_token = self.account()
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            for owner, key, kind, summary, updated in (
                (self.user_id, "preference.language", "LEARNING_PREFERENCE", "喜欢中文", now),
                (self.user_id, "learning.goal", "LEARNING_GOAL", "Learn Python", now + timedelta(seconds=1)),
                (other_id, "preference.language", "LEARNING_PREFERENCE", "Other user only", now),
            ):
                session.add(AgentMemory(user_id=owner, memory_key=key, memory_type=kind, summary=summary,
                                        created_at=now, updated_at=updated))
        self.assertEqual([i["summary"] for i in self.client.get(MEMORY).json()["items"]], ["Other user only"])
        self.use_token(self.token)
        before = self.client.get(MEMORY)
        self.assertEqual(before.status_code, 200)
        items = before.json()["items"]
        self.assertEqual([i["summary"] for i in items], ["Learn Python", "喜欢中文"])
        self.assertEqual(set(items[0]), {"memory_id", "memory_type", "summary", "updated_at"})
        self.assertEqual(self.client.get(MEMORY).json(), before.json())
        self.assertEqual(self.client.get(MEMORY + "?user_id=" + str(other_id)).status_code, 422)
        self.assertEqual(self.client.post(MEMORY, json={"summary": "overwrite"}, headers=self.headers).status_code, 405)
        self.assertEqual(self.client.get(MEMORY).json(), before.json())
