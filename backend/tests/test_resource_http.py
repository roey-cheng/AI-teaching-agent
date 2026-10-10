"""会话/历史/记忆接口离线测试，业务替身不连接数据库或模型。"""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.http_config import HTTPSettings
from app.main import create_app
from app.schemas import MemoryListResponse, MessageHistoryResponse, SessionListResponse, SessionResponse, UserResponse
from app.services import errors
from app.services.generation_registry import GenerationRegistry
from app.services.startup_cleanup import StartupCleanupResult

ORIGIN = "http://localhost:5173"
BASE = "/api/v1/chat/sessions"
MEMORY = "/api/v1/me/memory"
USER = UserResponse(user_id="123", email="student@example.com", display_name="Student")


class ResourceHTTPTest(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(session_factory=object(), registry=GenerationRegistry(),
                                       cleanup=StartupCleanupResult())

        @contextmanager
        def runtime():
            yield self.runtime

        self.client = self.enterContext(TestClient(create_app(
            runtime_factory=runtime, http_settings=HTTPSettings(_env_file=None, allowed_origins=[ORIGIN]))))
        self.auth = self.enterContext(patch("app.api.dependencies.get_current_user", return_value=USER))
        self.client.cookies.set("chat_session", "A" * 43)
        now = datetime.now(UTC)
        self.session = SessionResponse(session_id="42", title="Example", created_at=now,
                                       updated_at=now, last_activity_at=now)

    def call(self, method, path, **kwargs):
        kwargs.setdefault("headers", {"Origin": ORIGIN})
        return self.client.request(method, path, **kwargs)

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        self.assertEqual(error["request_id"], response.headers["x-request-id"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_create_uses_authenticated_user_and_returns_201(self):
        with patch("app.api.chat_sessions.create_chat_session", return_value=self.session) as service:
            response = self.call("POST", BASE, json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["session_id"], "42")
        service.assert_called_once_with(USER, self.runtime.session_factory)
        self.assertEqual(self.auth.call_args.args[0].get_secret_value(), "A" * 43)
        self.assertNotIn("user_id", response.json())
        self.assertNotIn("set-cookie", response.headers)

    def test_list_returns_items_and_calls_existing_service(self):
        with patch("app.api.chat_sessions.list_chat_sessions", return_value=SessionListResponse(items=[self.session])) as service:
            response = self.call("GET", BASE)
        self.assertEqual(response.json()["items"][0]["title"], "Example")
        service.assert_called_once_with(USER, self.runtime.session_factory)

    def test_rename_trims_title_and_passes_string_id(self):
        with patch("app.api.chat_sessions.rename_chat_session", return_value=self.session) as service:
            response = self.call("PATCH", BASE + "/42", json={"title": " New title "})
        self.assertEqual(response.status_code, 200)
        user, identifier, body, factory = service.call_args.args
        self.assertEqual((user, identifier, body.title, factory), (USER, "42", "New title", self.runtime.session_factory))

    def test_history_passes_shared_registry_and_returns_empty_snapshot(self):
        result = MessageHistoryResponse(session_id="42", is_generating=False, items=[])
        with patch("app.api.chat_sessions.get_message_history", return_value=result) as service:
            response = self.call("GET", BASE + "/42/messages")
        self.assertEqual(response.json(), {"session_id": "42", "is_generating": False, "items": []})
        service.assert_called_once_with(USER, "42", self.runtime.session_factory, self.runtime.registry)

    def test_memory_uses_current_user_and_empty_items_are_valid(self):
        with patch("app.api.memory.list_profile_memory", return_value=MemoryListResponse(items=[])) as service:
            response = self.call("GET", MEMORY)
        self.assertEqual(response.json(), {"items": []})
        service.assert_called_once_with(USER, self.runtime.session_factory)

    def test_all_five_endpoints_require_login_before_business(self):
        self.auth.side_effect = errors.AuthenticationRequiredError()
        cases = [("POST", BASE, {}), ("GET", BASE, None), ("PATCH", BASE + "/42", {"title": "New"}),
                 ("GET", BASE + "/42/messages", None), ("GET", MEMORY, None)]
        for method, path, body in cases:
            with self.subTest(path=path, method=method):
                self.assert_error(self.call(method, path, **({"json": body} if body is not None else {})),
                                  401, "UNAUTHENTICATED")

    def test_missing_cookie_is_not_replaced_by_frontend_identity(self):
        self.client.cookies.clear()
        self.auth.side_effect = errors.AuthenticationRequiredError()
        self.assert_error(self.call("GET", MEMORY), 401, "UNAUTHENTICATED")
        self.assertIsNone(self.auth.call_args.args[0])

    def test_both_writes_require_origin_and_json(self):
        for method, path, body in (("POST", BASE, {}), ("PATCH", BASE + "/42", {"title": "New"})):
            self.assert_error(self.call(method, path, json=body, headers={}), 403, "ORIGIN_NOT_ALLOWED")
            self.assert_error(self.call(method, path, json=body, headers={"Origin": "https://evil.example"}),
                              403, "ORIGIN_NOT_ALLOWED")
            self.assert_error(self.call(method, path, content="{}"), 415, "UNSUPPORTED_MEDIA_TYPE")

    def test_empty_create_object_required_and_unknown_fields_rejected(self):
        with patch("app.api.chat_sessions.create_chat_session") as service:
            for body in ({"user_id": "999"}, {"title": "Wrong"}, [], None):
                self.assert_error(self.call("POST", BASE, json=body,
                    headers={"Origin": ORIGIN, "Content-Type": "application/json"}), 422, "VALIDATION_ERROR")
            self.assert_error(self.call("POST", BASE, headers={"Origin": ORIGIN, "Content-Type": "application/json"}),
                              422, "VALIDATION_ERROR")
        service.assert_not_called()

    def test_rename_constraints_and_no_archive_field(self):
        with patch("app.api.chat_sessions.rename_chat_session") as service:
            for body in ({}, {"title": "   "}, {"title": "x" * 101}, {"title": 12},
                         {"title": "New", "status": "ARCHIVED"}, {"title": "New", "user_id": "99"}):
                self.assert_error(self.call("PATCH", BASE + "/42", json=body), 422, "VALIDATION_ERROR")
        service.assert_not_called()

    def test_invalid_path_ids_rejected_before_resource_service(self):
        with patch("app.api.chat_sessions.get_message_history") as service:
            for identifier in ("0", "-1", "01", "1.5", "abc", "18446744073709551616"):
                self.assert_error(self.call("GET", BASE + "/" + identifier + "/messages"), 422, "VALIDATION_ERROR")
        service.assert_not_called()

    def test_uint64_id_preserved_as_string(self):
        identifier = "18446744073709551615"
        with patch("app.api.chat_sessions.get_message_history", side_effect=errors.SessionNotFoundError()) as service:
            self.assert_error(self.call("GET", BASE + "/" + identifier + "/messages"), 404, "SESSION_NOT_FOUND")
        self.assertEqual(service.call_args.args[1], identifier)

    def test_no_queries_or_get_bodies(self):
        for path in (BASE, BASE + "/42/messages", MEMORY):
            for query in ("?user_id=999", "?cursor=abc", "?limit=20"):
                self.assert_error(self.call("GET", path + query), 422, "VALIDATION_ERROR")
            self.assert_error(self.call("GET", path, json={}), 422, "VALIDATION_ERROR")
        self.assert_error(self.call("PATCH", BASE + "/42?user_id=999", json={"title": "New"}), 422, "VALIDATION_ERROR")

    def test_body_size_limit(self):
        response = self.call("PATCH", BASE + "/42", content="x" * 16385,
                             headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        self.assert_error(response, 413, "REQUEST_TOO_LARGE")

    def test_safe_business_errors_not_empty_success(self):
        cases = [("chat_sessions.list_chat_sessions", "GET", BASE, errors.SessionUnavailableError),
                 ("chat_sessions.get_message_history", "GET", BASE + "/42/messages", errors.MessageHistoryUnavailableError),
                 ("memory.list_profile_memory", "GET", MEMORY, errors.MemoryUnavailableError)]
        for service, method, path, error in cases:
            with patch("app.api." + service, side_effect=error()):
                self.assert_error(self.call(method, path), 503, error.code)
        self.auth.side_effect = errors.AuthenticationUnavailableError()
        self.assert_error(self.call("GET", MEMORY), 503, "AUTHENTICATION_UNAVAILABLE")

    def test_no_public_memory_writes(self):
        self.assertEqual(self.call("POST", MEMORY, json={}).status_code, 405)

    def test_openapi_documents_eleven_business_operations_and_cookie_auth(self):
        spec = self.client.get("/openapi.json").json()
        self.assertEqual(sum(len(methods) for path, methods in spec["paths"].items() if path.startswith("/api/v1/")), 11)
        for path, method in ((BASE, "post"), (BASE, "get"), (BASE + "/{session_id}", "patch"),
                             (BASE + "/{session_id}/messages", "get"), (MEMORY, "get")):
            self.assertTrue(spec["paths"][path][method]["security"])
