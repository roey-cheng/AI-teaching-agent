"""登录与会话 Schema 的离线测试：不登录、不查库、不创建会话。"""

import json
import unittest
from datetime import UTC, datetime, timedelta, timezone

from pydantic import ValidationError

from app.models import ChatSession, User
from app.schemas import (
    CreateSessionRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RenameSessionRequest,
    SessionListResponse,
    SessionResponse,
)


class LoginSchemaTest(unittest.TestCase):
    def test_login_normalizes_email_exactly_like_registration(self):
        data = {"email": "  Student@Example.COM  ", "password": "  PassWord123  "}
        login = LoginRequest.model_validate(data)
        registration = RegisterRequest.model_validate(data | {"display_name": "小雨"})
        self.assertEqual(login.email, registration.email)
        self.assertEqual(login.email, "student@example.com")
        self.assertEqual(login.password.get_secret_value(), data["password"])

    def test_login_password_checks_format_not_new_password_strength(self):
        for length, valid in ((0, False), (1, True), (7, True), (8, True), (128, True), (129, False)):
            with self.subTest(length=length):
                data = {"email": "student@example.com", "password": "A" * length}
                if valid:
                    self.assertEqual(LoginRequest.model_validate(data).password.get_secret_value(), "A" * length)
                else:
                    with self.assertRaises(ValidationError):
                        LoginRequest.model_validate(data)

    def test_login_requires_fields_and_rejects_wrong_types(self):
        data = {"email": "student@example.com", "password": "Fake-password"}
        for field in data:
            with self.subTest(field=field, missing=True), self.assertRaises(ValidationError):
                LoginRequest.model_validate({key: value for key, value in data.items() if key != field})
            for bad in (None, True, 12345678, [], {}, b"abcdefgh"):
                with self.subTest(field=field, bad_type=type(bad).__name__), self.assertRaises(ValidationError):
                    LoginRequest.model_validate(data | {field: bad})

    def test_login_rejects_invalid_email_and_unknown_fields(self):
        data = {"email": "student@example.com", "password": "Fake-password"}
        for email in ("", "not-an-email", "Name <student@example.com>", "a" * 321 + "@example.com"):
            with self.subTest(email=email), self.assertRaises(ValidationError):
                LoginRequest.model_validate(data | {"email": email})
        for field in ("user_id", "display_name", "system_role", "token_hash", "remember_me"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                LoginRequest.model_validate(data | {field: "unexpected"})

    def test_login_password_is_masked_and_json_input_is_supported(self):
        secret = "Fake-password"
        request = LoginRequest.model_validate_json(json.dumps({
            "email": "student@example.com", "password": secret,
        }))
        self.assertNotIn(secret, repr(request))
        self.assertNotIn(secret, request.model_dump_json())
        self.assertEqual(json.loads(request.model_dump_json())["password"], "**********")
        with self.assertRaises(ValidationError) as captured:
            LoginRequest.model_validate({"email": "invalid", "password": secret})
        self.assertNotIn(secret, str(captured.exception))

    def test_login_response_wraps_public_user_and_excludes_credentials(self):
        user = User(user_id=123, email="student@example.com", display_name="小雨",
                    password_hash="fake-hash", system_role="USER", status="ACTIVE")
        payload = LoginResponse.model_validate({
            "user": user, "token": "fake-token", "cookie": "fake-cookie",
        }).model_dump(mode="json")
        self.assertEqual(payload, {
            "user": {"user_id": "123", "email": "student@example.com", "display_name": "小雨"},
        })
        with self.assertRaises(ValidationError):
            LoginResponse.model_validate(payload["user"])  # 不能忘记外层 user。

    def test_login_json_schema_matches_api_fields(self):
        request = LoginRequest.model_json_schema()
        self.assertEqual(set(request["required"]), {"email", "password"})
        self.assertFalse(request["additionalProperties"])
        self.assertEqual(request["properties"]["password"]["minLength"], 1)
        self.assertTrue(request["properties"]["password"]["writeOnly"])
        response = LoginResponse.model_json_schema()
        self.assertEqual(set(response["properties"]), {"user"})


class SessionRequestTest(unittest.TestCase):
    def test_create_session_accepts_only_empty_object(self):
        self.assertEqual(CreateSessionRequest.model_validate({}).model_dump(), {})
        self.assertEqual(CreateSessionRequest.model_validate_json("{}").model_dump(), {})
        for value in (None, [], "", 1, {"user_id": "123"}, {"title": "hello"}, {"status": "ARCHIVED"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CreateSessionRequest.model_validate(value)

    def test_rename_trims_edges_and_preserves_internal_whitespace(self):
        request = RenameSessionRequest.model_validate({"title": " \tPython  basics\n "})
        self.assertEqual(request.title, "Python  basics")

    def test_rename_checks_length_after_trimming(self):
        for title, valid in (("", False), (" \n\t", False), ("雨", True),
                             (" " + "雨" * 100 + " ", True), ("雨" * 101, False)):
            with self.subTest(length=len(title)):
                if valid:
                    self.assertEqual(RenameSessionRequest.model_validate({"title": title}).title, title.strip())
                else:
                    with self.assertRaises(ValidationError):
                        RenameSessionRequest.model_validate({"title": title})

    def test_rename_requires_string_title_and_rejects_extra_fields(self):
        for value in ({}, {"title": None}, {"title": 123}, {"title": True}, {"title": []},
                      {"title": b"hello"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                RenameSessionRequest.model_validate(value)
        for field in ("status", "user_id", "session_id", "title_is_manual", "last_activity_at"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RenameSessionRequest.model_validate({"title": "hello", field: "unexpected"})

    def test_request_json_schemas_forbid_unknown_fields(self):
        for model in (CreateSessionRequest, RenameSessionRequest):
            self.assertFalse(model.model_json_schema()["additionalProperties"])
        self.assertEqual(CreateSessionRequest.model_json_schema()["properties"], {})
        self.assertEqual(RenameSessionRequest.model_json_schema()["required"], ["title"])


class SessionResponseTest(unittest.TestCase):
    def session(self, **overrides):
        values = {
            "chat_session_id": 1001, "user_id": 123, "title": "Python basics",
            "title_is_manual": True, "created_at": datetime(2026, 9, 24, 8),
            "updated_at": datetime(2026, 9, 24, 9), "last_activity_at": datetime(2026, 9, 24, 8, 30),
        }
        return ChatSession(**(values | overrides))

    def test_orm_column_is_mapped_to_api_id_and_internal_fields_are_filtered(self):
        session = self.session()
        response = SessionResponse.model_validate(session)
        expected = {
            "session_id": "1001", "title": "Python basics",
            "created_at": "2026-09-24T08:00:00Z", "updated_at": "2026-09-24T09:00:00Z",
            "last_activity_at": "2026-09-24T08:30:00Z",
        }
        self.assertEqual(response.model_dump(mode="json"), expected)
        self.assertEqual(response.model_dump(mode="json", by_alias=True), expected)
        self.assertEqual(session.chat_session_id, 1001)
        self.assertIsNone(session.created_at.tzinfo)  # 格式化响应不修改 ORM 对象。

    def test_response_accepts_api_field_name_and_roundtrips_json(self):
        response = SessionResponse.model_validate(self.session())
        data = response.model_dump()
        self.assertEqual(SessionResponse.model_validate(data), response)
        self.assertEqual(SessionResponse.model_validate_json(response.model_dump_json()), response)

    def test_large_ids_preserve_precision_and_bad_ids_are_rejected(self):
        for value in (9007199254740993, 18446744073709551615, "18446744073709551615"):
            with self.subTest(value=value):
                self.assertEqual(SessionResponse.model_validate(self.session(chat_session_id=value)).session_id, str(value))
        for value in (None, 0, -1, True, 1.5, "bad", "001", 18446744073709551616):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SessionResponse.model_validate(self.session(chat_session_id=value))

    def test_all_three_times_are_utc_and_required(self):
        aware = datetime(2026, 9, 24, 20, tzinfo=timezone(timedelta(hours=12)))
        for field in ("created_at", "updated_at", "last_activity_at"):
            with self.subTest(field=field):
                response = SessionResponse.model_validate(self.session(**{field: aware}))
                self.assertEqual(getattr(response, field), datetime(2026, 9, 24, 8, tzinfo=UTC))
                self.assertEqual(response.model_dump(mode="json")[field], "2026-09-24T08:00:00Z")
                with self.assertRaises(ValidationError):
                    SessionResponse.model_validate(self.session(**{field: None}))

    def test_list_supports_empty_and_nested_orm_objects_without_sorting(self):
        self.assertEqual(SessionListResponse(items=[]).model_dump(mode="json"), {"items": []})
        response = SessionListResponse.model_validate({"items": [
            self.session(chat_session_id=2), self.session(chat_session_id=1),
        ]})
        self.assertEqual([item.session_id for item in response.items], ["2", "1"])
        self.assertEqual(set(response.model_dump(mode="json")), {"items"})
        for data in ({}, {"items": None}, {"items": {}}, {"items": [None]}, {"items": [{}]}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                SessionListResponse.model_validate(data)

    def test_response_json_schemas_expose_api_names_and_no_pagination(self):
        expected = {"session_id", "title", "created_at", "updated_at", "last_activity_at"}
        for mode in ("validation", "serialization"):
            schema = SessionResponse.model_json_schema(mode=mode)
            self.assertEqual(set(schema["properties"]), expected)
            self.assertEqual(set(schema["required"]), expected)
        self.assertEqual(set(SessionListResponse.model_json_schema()["properties"]), {"items"})
