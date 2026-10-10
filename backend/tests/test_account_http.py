"""账号接口离线测试：真实 HTTP/Schema，替换业务，不读取本地密码或连接数据库。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.errors import APIError
from app.api.security import AccountRateLimiter
from app.core.http_config import HTTPSettings
from app.main import create_app
from app.schemas import LoginResponse, RegisterResponse, UserResponse
from app.services import errors
from app.services.login import LoginResult
from app.services.startup_cleanup import StartupCleanupResult

ORIGIN = "http://localhost:5173"
USER = UserResponse(user_id="123", email="student@example.com", display_name="小明")
PASSWORD = "only-a-test-password"


class AccountHTTPTest(unittest.TestCase):
    def make_client(self, *, secure=False):
        factory = object()

        @contextmanager
        def runtime():
            yield SimpleNamespace(cleanup=StartupCleanupResult(), session_factory=factory)

        app = create_app(runtime_factory=runtime, http_settings=HTTPSettings(
            _env_file=None, allowed_origins=[ORIGIN], cookie_secure=secure))
        client = self.enterContext(TestClient(app, base_url="https://testserver" if secure else "http://testserver"))
        return client, factory

    def login_result(self):
        return LoginResult(LoginResponse(user=USER), SecretStr("A" * 43), datetime.now(UTC) + timedelta(days=7))

    def post(self, client, path="login", **kwargs):
        kwargs.setdefault("json", {"email": USER.email, "password": PASSWORD})
        kwargs.setdefault("headers", {"Origin": ORIGIN})
        return client.post("/api/v1/auth/" + path, **kwargs)

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        self.assertTrue(error["message"])
        self.assertEqual(error["request_id"], response.headers["x-request-id"])
        self.assertNotIn(PASSWORD, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_register_normalizes_input_and_does_not_login(self):
        client, factory = self.make_client()
        result = RegisterResponse(**USER.model_dump(), created_at=datetime.now(UTC))
        with patch("app.api.accounts.register_user", return_value=result) as service:
            response = self.post(client, "register", json={
                "email": " STUDENT@EXAMPLE.COM ", "password": PASSWORD, "display_name": " 小明 "})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user_id"], "123")
        self.assertNotIn("set-cookie", response.headers)
        body, actual_factory = service.call_args.args
        self.assertIs(actual_factory, factory)
        self.assertEqual(body.email, "student@example.com")
        self.assertEqual(body.display_name, "小明")
        self.assertEqual(body.password.get_secret_value(), PASSWORD)
        self.assertNotIn(PASSWORD, response.text)

    def test_login_cookie_flags_expiry_and_public_json(self):
        for secure in (False, True):
            client, factory = self.make_client(secure=secure)
            client.cookies.set("chat_session", "B" * 43)
            with patch("app.api.accounts.login_user", return_value=self.login_result()) as service:
                response = self.post(client)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"user": USER.model_dump()})
            cookie = SimpleCookie(response.headers["set-cookie"])["chat_session"]
            self.assertEqual(cookie.value, "A" * 43)
            self.assertTrue(cookie["httponly"])
            self.assertEqual(bool(cookie["secure"]), secure)
            self.assertEqual(cookie["path"], "/")
            self.assertEqual(cookie["samesite"], "lax")
            self.assertTrue(cookie["expires"])
            self.assertTrue(604790 <= int(cookie["max-age"]) <= 604800)
            self.assertEqual(cookie["domain"], "")
            self.assertIs(service.call_args.args[1], factory)
            self.assertEqual(service.call_args.kwargs["current_token"].get_secret_value(), "B" * 43)
            self.assertNotIn("A" * 43, response.text)

    def test_current_user_reads_cookie_only(self):
        client, factory = self.make_client()
        client.cookies.set("chat_session", "A" * 43)
        with patch("app.api.accounts.get_current_user", return_value=USER) as service:
            response = client.get("/api/v1/users/me")
        self.assertEqual(response.json(), USER.model_dump())
        self.assertEqual(service.call_args.args[0].get_secret_value(), "A" * 43)
        self.assertIs(service.call_args.args[1], factory)
        self.assertNotIn("set-cookie", response.headers)

    def test_logout_empty_204_clears_cookie_even_when_missing(self):
        for secure in (False, True):
            client, factory = self.make_client(secure=secure)
            with patch("app.api.accounts.logout_user") as service:
                response = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
            service.assert_called_once_with(None, factory)
            cookie = SimpleCookie(response.headers["set-cookie"])["chat_session"]
            self.assertEqual(cookie["max-age"], "0")
            self.assertTrue(cookie["httponly"])
            self.assertEqual(bool(cookie["secure"]), secure)
            self.assertEqual(cookie["path"], "/")

    def test_origin_required_exact_and_single_before_service(self):
        client, _ = self.make_client()
        headers = [{}, {"Origin": "null"}, {"Origin": ORIGIN + ".evil.com"},
                   {"Origin": ORIGIN + "/"}, {"Origin": "http://localhost:5174"},
                   [("Origin", ORIGIN), ("Origin", ORIGIN)]]
        with patch("app.api.accounts.login_user") as service:
            for value in headers:
                self.assert_error(self.post(client, headers=value), 403, "ORIGIN_NOT_ALLOWED")
        service.assert_not_called()

    def test_logout_requires_origin_too(self):
        client, _ = self.make_client()
        self.assert_error(client.post("/api/v1/auth/logout"), 403, "ORIGIN_NOT_ALLOWED")

    def test_json_media_type_and_body_size(self):
        client, _ = self.make_client()
        self.assert_error(client.post("/api/v1/auth/login", content="{}", headers={"Origin": ORIGIN}),
                          415, "UNSUPPORTED_MEDIA_TYPE")
        self.assert_error(client.post("/api/v1/auth/login", content="x" * 16385,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"}), 413, "REQUEST_TOO_LARGE")

    def test_invalid_json_and_secret_fields_never_echo_input(self):
        client, _ = self.make_client()
        for body in ({"email": "invalid", "password": PASSWORD},
                     {"email": USER.email, "password": {"secret": PASSWORD}},
                     {"email": USER.email, "password": PASSWORD, "system_role": "ADMIN"}):
            self.assert_error(self.post(client, json=body), 422, "VALIDATION_ERROR")
        response = client.post("/api/v1/auth/login", content='{"password":"' + PASSWORD,
                               headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        self.assert_error(response, 422, "VALIDATION_ERROR")

    def test_unknown_query_and_unexpected_bodies_rejected(self):
        client, _ = self.make_client()
        for response in (
            client.get("/api/v1/users/me?user_id=2"),
            client.request("GET", "/api/v1/users/me", json={}),
            client.post("/api/v1/auth/logout", json={}, headers={"Origin": ORIGIN}),
            self.post(client, "login?user_id=2"),
        ):
            self.assert_error(response, 422, "VALIDATION_ERROR")

    def test_business_error_statuses_and_no_cookie_on_failure(self):
        client, _ = self.make_client()
        cases = [
            ("register_user", errors.EmailAlreadyRegisteredError, 409, "register"),
            ("register_user", errors.RegistrationUnavailableError, 503, "register"),
            ("login_user", errors.InvalidCredentialsError, 401, "login"),
            ("login_user", errors.LoginUnavailableError, 503, "login"),
            ("get_current_user", errors.AuthenticationRequiredError, 401, "me"),
            ("get_current_user", errors.AuthenticationUnavailableError, 503, "me"),
            ("logout_user", errors.LogoutUnavailableError, 503, "logout"),
        ]
        for name, error, status, path in cases:
            with self.subTest(name=error.__name__), patch("app.api.accounts." + name, side_effect=error()):
                if path == "me":
                    response = client.get("/api/v1/users/me")
                elif path == "logout":
                    response = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
                else:
                    body = {"email": USER.email, "password": PASSWORD}
                    if path == "register":
                        body["display_name"] = "小明"
                    response = self.post(client, path, json=body)
                self.assert_error(response, status, error.code)
                self.assertNotIn("set-cookie", response.headers)

    def test_unexpected_error_sanitized_and_request_ids_not_trusted(self):
        client, _ = self.make_client()
        with patch("app.api.accounts.get_current_user", side_effect=RuntimeError(PASSWORD)), \
                self.assertLogs("app.api.errors", level="ERROR") as logs:
            response = client.get("/api/v1/users/me", headers={"X-Request-ID": PASSWORD})
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assertNotEqual(response.headers["x-request-id"], PASSWORD)
        self.assertNotIn(PASSWORD, " ".join(logs.output))
        self.assertIn(response.headers["x-request-id"], " ".join(logs.output))

    def test_unknown_route_and_method_use_safe_envelope(self):
        client, _ = self.make_client()
        self.assert_error(client.get("/missing"), 404, "HTTP_ERROR")
        response = client.get("/api/v1/auth/login")
        self.assert_error(response, 405, "HTTP_ERROR")
        self.assertIn("POST", response.headers["allow"])

    def test_login_ip_limit_cannot_be_bypassed_with_forwarded_header(self):
        client, _ = self.make_client()
        with patch("app.api.accounts.login_user", side_effect=errors.InvalidCredentialsError()) as service:
            for index in range(10):
                response = self.post(client, json={"email": f"user{index}@example.com", "password": PASSWORD},
                    headers={"Origin": ORIGIN, "X-Forwarded-For": f"10.0.0.{index}"})
                self.assertEqual(response.status_code, 401)
            response = self.post(client)
        self.assert_error(response, 429, "RATE_LIMITED")
        self.assertTrue(1 <= int(response.headers["retry-after"]) <= 60)
        self.assertEqual(service.call_count, 10)

    def test_email_limit_is_normalized_and_shared_across_ips(self):
        client, _ = self.make_client()
        for _ in range(10):
            client.app.state.account_limiter.consume("login_email", "student@example.com", 60)
        with patch("app.api.accounts.login_user") as service:
            response = self.post(client, json={"email": " STUDENT@EXAMPLE.COM ", "password": PASSWORD})
        self.assert_error(response, 429, "RATE_LIMITED")
        service.assert_not_called()

    def test_register_ip_limit(self):
        client, _ = self.make_client()
        with patch("app.api.accounts.register_user", side_effect=errors.EmailAlreadyRegisteredError()) as service:
            for _ in range(10):
                self.post(client, "register", json={"email": USER.email, "password": PASSWORD, "display_name": "Demo"})
            response = self.post(client, "register")
        self.assert_error(response, 429, "RATE_LIMITED")
        self.assertTrue(3500 <= int(response.headers["retry-after"]) <= 3600)
        self.assertEqual(service.call_count, 10)

    def test_openapi_has_four_account_routes_and_schemas(self):
        client, _ = self.make_client()
        paths = client.get("/openapi.json").json()["paths"]
        self.assertTrue({"/health", "/api/v1/auth/register", "/api/v1/auth/login",
                         "/api/v1/auth/logout", "/api/v1/users/me"}.issubset(paths))
        self.assertIn("201", paths["/api/v1/auth/register"]["post"]["responses"])
        schema = paths["/api/v1/auth/login"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["$ref"], "#/components/schemas/ErrorResponse")


class HTTPPolicyTest(unittest.TestCase):
    def test_rate_window_expiration_and_memory_capacity(self):
        clock = [0]
        limiter = AccountRateLimiter(clock=lambda: clock[0], max_keys=1)
        for _ in range(10):
            limiter.consume("login_ip", "one", 60)
        with self.assertRaises(APIError):
            limiter.consume("login_ip", "one", 60)
        with self.assertRaises(APIError):
            limiter.consume("login_ip", "two", 60)
        clock[0] = 60
        limiter.consume("login_ip", "two", 60)
        self.assertEqual(len(limiter.entries), 1)

    def test_origin_configuration_rejects_unsafe_values(self):
        for value in ([], ["*"], ["null"], ["https://example.com/path"],
                      ["https://name:password@example.com"], ["https://example.com?x=1"],
                      ["https://example.com:bad"]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                HTTPSettings(_env_file=None, allowed_origins=value)
