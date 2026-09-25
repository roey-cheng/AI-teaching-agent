"""只在内存中校验输入和输出，不连接 MySQL、不发邮件、不调用模型。"""

import json
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from app.models import User
from app.schemas import RegisterRequest, RegisterResponse, UserResponse


class RegisterRequestTest(unittest.TestCase):
    def valid_data(self):
        return {"email": "student@example.com", "password": "Example-password", "display_name": "小雨"}

    def test_normalizes_email_and_name_but_preserves_password(self):
        request = RegisterRequest.model_validate({
            "email": "  Student@Example.COM  ",
            "password": "  AbCd1234  ",
            "display_name": "  小 雨  ",
        })
        self.assertEqual(request.email, "student@example.com")
        self.assertEqual(request.display_name, "小 雨")
        self.assertEqual(request.password.get_secret_value(), "  AbCd1234  ")

    def test_password_boundaries(self):
        for length, valid in ((7, False), (8, True), (128, True), (129, False)):
            with self.subTest(length=length):
                data = self.valid_data() | {"password": "A" * length}
                if valid:
                    self.assertEqual(len(RegisterRequest.model_validate(data).password.get_secret_value()), length)
                else:
                    with self.assertRaises(ValidationError):
                        RegisterRequest.model_validate(data)

    def test_name_boundaries_apply_after_trimming(self):
        for name, valid in (("", False), (" \t\n", False), (" 雨 ", True),
                            (" " + "雨" * 100 + " ", True), ("雨" * 101, False)):
            with self.subTest(name_length=len(name)):
                data = self.valid_data() | {"display_name": name}
                if valid:
                    self.assertEqual(RegisterRequest.model_validate(data).display_name, name.strip())
                else:
                    with self.assertRaises(ValidationError):
                        RegisterRequest.model_validate(data)

    def test_invalid_email_formats_and_oversize_are_rejected(self):
        for email in ("abc", "", "a b@example.com", "@example.com", "a@",
                      "小雨 <a@example.com>", "a" * 321 + "@example.com"):
            with self.subTest(email_length=len(email)), self.assertRaises(ValidationError):
                RegisterRequest.model_validate(self.valid_data() | {"email": email})

    def test_email_validation_does_not_query_dns(self):
        # 格式合法不等于邮箱存在；Pydantic EmailStr 不做网络可投递性检查。
        with patch("dns.resolver.resolve", side_effect=AssertionError("不应查 DNS")):
            self.assertEqual(RegisterRequest.model_validate(self.valid_data()).email, "student@example.com")

    def test_missing_fields_and_wrong_types_are_rejected(self):
        for field in ("email", "password", "display_name"):
            data = self.valid_data()
            del data[field]
            with self.subTest(field=field, missing=True), self.assertRaises(ValidationError):
                RegisterRequest.model_validate(data)
            for value in (None, 12345678, True, [], {}, b"abcdefgh"):
                with self.subTest(field=field, value_type=type(value).__name__), self.assertRaises(ValidationError):
                    RegisterRequest.model_validate(self.valid_data() | {field: value})

    def test_unknown_or_privileged_fields_are_rejected(self):
        for field in ("user_id", "system_role", "status", "password_hash", "unexpected"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RegisterRequest.model_validate(self.valid_data() | {field: "ADMIN"})

    def test_password_is_not_exposed_in_repr_json_or_error_text(self):
        data = self.valid_data()
        request = RegisterRequest.model_validate(data)
        self.assertNotIn(data["password"], repr(request))
        self.assertNotIn(data["password"], request.model_dump_json())
        self.assertEqual(json.loads(request.model_dump_json())["password"], "**********")
        # 原始 errors() 仍可能含 input；后续 HTTP 错误处理不能直接返回/记录它。
        with self.assertRaises(ValidationError) as captured:
            RegisterRequest.model_validate(data | {"display_name": ""})
        self.assertNotIn(data["password"], str(captured.exception))

    def test_json_schema_marks_required_fields_and_forbids_extras(self):
        schema = RegisterRequest.model_json_schema()
        self.assertEqual(set(schema["required"]), {"email", "password", "display_name"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["email"]["format"], "email")
        self.assertEqual(schema["properties"]["password"]["minLength"], 8)
        self.assertEqual(schema["properties"]["password"]["maxLength"], 128)
        self.assertTrue(schema["properties"]["password"]["writeOnly"])


class UserResponseTest(unittest.TestCase):
    def make_user(self, **overrides):
        values = {
            "user_id": 123, "email": "student@example.com", "display_name": "小雨",
            "password_hash": "fake-sensitive-hash", "system_role": "USER", "status": "ACTIVE",
            "created_at": datetime(2026, 9, 24, 8, 0), "updated_at": datetime(2026, 9, 24, 8, 0),
        }
        return User(**(values | overrides))

    def test_user_response_reads_orm_object_and_only_exposes_public_fields(self):
        user = self.make_user()
        payload = UserResponse.model_validate(user).model_dump(mode="json")
        self.assertEqual(payload, {"user_id": "123", "email": "student@example.com", "display_name": "小雨"})
        self.assertNotIn("fake-sensitive-hash", json.dumps(payload))
        self.assertEqual(user.user_id, 123)  # 输出转换不修改原 ORM 属性。

    def test_extra_backend_dictionary_fields_are_filtered(self):
        payload = UserResponse.model_validate({
            "user_id": 123, "email": "student@example.com", "display_name": "小雨",
            "password": "fake-secret", "password_hash": "fake-hash", "token_hash": "fake-token",
            "system_role": "ADMIN", "created_at": datetime(2026, 9, 24),
        }).model_dump(mode="json")
        self.assertEqual(set(payload), {"user_id", "email", "display_name"})

    def test_registration_includes_created_at_in_utc_and_no_sensitive_fields(self):
        response = RegisterResponse.model_validate(self.make_user())
        payload = json.loads(response.model_dump_json())
        self.assertEqual(payload, {
            "user_id": "123", "email": "student@example.com", "display_name": "小雨",
            "created_at": "2026-09-24T08:00:00Z",
        })
        self.assertEqual(response.created_at.tzinfo, UTC)

    def test_aware_datetime_is_converted_to_utc(self):
        value = datetime(2026, 9, 24, 20, 0, tzinfo=timezone(timedelta(hours=12)))
        response = RegisterResponse.model_validate(self.make_user(created_at=value))
        self.assertEqual(response.model_dump(mode="json")["created_at"], "2026-09-24T08:00:00Z")

    def test_user_id_accepts_uint64_as_string_without_precision_loss(self):
        for value in (1, "123", 9007199254740993, 18446744073709551615, "18446744073709551615"):
            with self.subTest(value=value):
                response = UserResponse.model_validate(self.make_user(user_id=value))
                self.assertEqual(response.user_id, str(value))

    def test_invalid_user_ids_are_rejected(self):
        for value in (None, True, False, 0, -1, 1.5, "abc", "001", " 1", "١", 18446744073709551616):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                UserResponse.model_validate(self.make_user(user_id=value))

    def test_missing_registration_time_is_rejected(self):
        with self.assertRaises(ValidationError):
            RegisterResponse.model_validate(self.make_user(created_at=None))

    def test_response_json_schema_contains_only_public_fields(self):
        self.assertEqual(set(UserResponse.model_json_schema()["properties"]),
                         {"user_id", "email", "display_name"})
        self.assertEqual(set(RegisterResponse.model_json_schema()["properties"]),
                         {"user_id", "email", "display_name", "created_at"})
