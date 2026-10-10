"""真实 HTTP → 业务 → 隔离 MySQL，不使用开发者数据库或模型。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from app.core.http_config import HTTPSettings
from app.core.runtime import open_backend_runtime
from app.core.runtime_lock import RuntimeLock
from app.core.tokens import hash_session_token
from app.db.session import build_session_factory
from app.main import create_app
from app.models import AuthSession, User


class AccountHTTPMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.assertEqual(self.engine.url.database, "migration_test")
        self.assertNotEqual(self.engine.url.port, 3306)
        self.folder = self.enterContext(TemporaryDirectory())
        self.factory = build_session_factory(self.engine)
        self.created_user_ids = []
        self.email = f"http-{uuid4().hex}@example.com"
        self.data = {"email": self.email, "password": "http-test-password", "display_name": "网页测试"}
        self.headers = {"Origin": "http://localhost:5173"}
        self.client = self.enterContext(self.new_client())

    def tearDown(self):
        # 仅清理本用例在隔离库中创建的账号，不能影响其他用例的前置条件。
        self.assertEqual(self.engine.url.database, "migration_test")
        self.assertNotEqual(self.engine.url.port, 3306)
        if self.created_user_ids:
            with self.factory.begin() as session:
                session.execute(delete(AuthSession).where(AuthSession.user_id.in_(self.created_user_ids)))
                session.execute(delete(User).where(User.user_id.in_(self.created_user_ids)))

    def new_client(self):
        @contextmanager
        def runtime():
            with open_backend_runtime(self.engine, lock=RuntimeLock(Path(self.folder) / "http.lock")) as active:
                yield active
        return TestClient(create_app(runtime_factory=runtime, http_settings=HTTPSettings(
            _env_file=None, allowed_origins=[self.headers["Origin"]], cookie_secure=False)))

    def register(self):
        response = self.client.post("/api/v1/auth/register", json=self.data, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.created_user_ids.append(int(response.json()["user_id"]))
        return response.json()

    def login(self):
        response = self.client.post("/api/v1/auth/login", json={
            "email": self.email, "password": self.data["password"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return self.client.cookies.get("chat_session")

    def test_register_login_me_logout_real_database_roundtrip(self):
        registered = self.register()
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 401)
        token = self.login()
        me = self.client.get("/api/v1/users/me")
        self.assertEqual(me.json()["user_id"], registered["user_id"])
        self.assertNotIn("password_hash", me.text)
        with self.factory() as session:
            user = session.scalar(select(User).where(User.email == self.email))
            auth = session.scalar(select(AuthSession).where(AuthSession.user_id == user.user_id))
            self.assertNotEqual(user.password_hash, self.data["password"])
            self.assertEqual(auth.token_hash, hash_session_token(SecretStr(token)))
            self.assertNotEqual(auth.token_hash, token)
            self.assertEqual(auth.expires_at - auth.created_at, timedelta(days=7))
        response = self.client.post("/api/v1/auth/logout", headers=self.headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertIsNone(self.client.cookies.get("chat_session"))
        self.client.cookies.set("chat_session", token)
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/auth/logout", headers=self.headers).status_code, 204)

    def test_duplicate_register_wrong_password_rotation_and_disabled_user(self):
        user = self.register()
        duplicate = self.client.post("/api/v1/auth/register", json=self.data, headers=self.headers)
        self.assertEqual(duplicate.status_code, 409)
        wrong = self.client.post("/api/v1/auth/login", json={
            "email": self.email, "password": "wrong"}, headers=self.headers)
        self.assertEqual(wrong.status_code, 401)
        first = self.login()
        second = self.login()
        self.assertNotEqual(first, second)
        self.client.cookies.clear()
        self.client.cookies.set("chat_session", first)
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 401)
        self.client.cookies.set("chat_session", second)
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 200)
        with self.factory.begin() as session:
            session.execute(update(User).where(User.user_id == int(user["user_id"])).values(status="DISABLED"))
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 401)

    def test_persisted_cookie_survives_restart_and_expired_cookie_fails(self):
        self.register()
        token = self.login()
        self.client.__exit__(None, None, None)
        with self.new_client() as restarted:
            restarted.cookies.set("chat_session", token)
            self.assertEqual(restarted.get("/api/v1/users/me").status_code, 200)
            with self.factory.begin() as session:
                now = datetime.now(UTC).replace(tzinfo=None)
                session.execute(update(AuthSession).where(
                    AuthSession.token_hash == hash_session_token(SecretStr(token))).values(
                        created_at=now - timedelta(days=8), expires_at=now - timedelta(days=1)))
            self.assertEqual(restarted.get("/api/v1/users/me").status_code, 401)

    def test_two_accounts_cookie_identity_cannot_be_overridden_by_user_id(self):
        first = self.register()
        self.login()
        response = self.client.get("/api/v1/users/me?user_id=999999")
        self.assertEqual(response.status_code, 422)
        self.email = f"http-other-{uuid4().hex}@example.com"
        self.data["email"] = self.email
        second = self.register()
        self.assertNotEqual(first["user_id"], second["user_id"])
        self.assertEqual(self.client.get("/api/v1/users/me").json()["user_id"], first["user_id"])
        self.login()
        self.assertEqual(self.client.get("/api/v1/users/me").json()["user_id"], second["user_id"])
