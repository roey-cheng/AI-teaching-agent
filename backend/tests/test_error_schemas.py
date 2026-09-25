"""只验证错误数据格式，不安装 FastAPI 异常处理器。"""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.schemas import ErrorDetail, ErrorResponse, StreamError


class ErrorSchemaTest(unittest.TestCase):
    def setUp(self):
        self.values = dict(code="SESSION_NOT_FOUND", message="Session not found or inaccessible.",
                           request_id="req_example")

    def test_response_matches_documented_error_envelope(self):
        response = ErrorResponse(error=self.values)
        self.assertIsInstance(response.error, ErrorDetail)
        self.assertEqual(response.model_dump(), {"error": self.values})
        self.assertEqual(ErrorResponse.model_validate_json(response.model_dump_json()), response)

    def test_all_fields_are_required_and_strict_strings(self):
        for name in self.values:
            values = self.values.copy()
            del values[name]
            with self.subTest(missing=name), self.assertRaises(ValidationError):
                ErrorDetail(**values)
            for bad in (None, 1, True, [], {}):
                with self.subTest(field=name, value=bad), self.assertRaises(ValidationError):
                    ErrorDetail(**(self.values | {name: bad}))
        for values in ({}, self.values, {"error": None}, {"error": "failed"}, {"error": {}}):
            with self.assertRaises(ValidationError):
                ErrorResponse.model_validate(values)

    def test_string_bounds(self):
        ErrorDetail(code="C" * 64, message="M" * 500, request_id="r")
        for changes in ({"code": ""}, {"code": "C" * 65}, {"message": ""},
                        {"message": "M" * 501}, {"request_id": ""}):
            with self.subTest(fields=list(changes)), self.assertRaises(ValidationError):
                ErrorDetail(**(self.values | changes))

    def test_undeclared_details_are_not_serialized(self):
        response = ErrorResponse.model_validate({
            "error": self.values | {"input": "private-password", "traceback": "internal-stack"},
            "status_code": 404, "detail": [{"input": "private-password"}], "sql": "internal-sql",
        })
        self.assertEqual(response.model_dump(), {"error": self.values})
        self.assertNotIn("private-password", response.model_dump_json())

    def test_error_schema_does_not_sanitize_strings_or_map_status_codes(self):
        # 不伪装成脱敏器：调用方必须先选取安全公开提示，不能塞原始异常。
        response = ErrorResponse(error=self.values | {"message": "fixture-secret-in-message"})
        self.assertEqual(response.error.message, "fixture-secret-in-message")
        self.assertNotIn("status_code", ErrorResponse.model_fields)
        with patch("pymysql.connect") as connect:
            ErrorResponse(error=self.values)
            connect.assert_not_called()

    def test_validation_error_text_hides_input(self):
        values = self.values | {"code": {"password": "fixture-sensitive-input"}}
        with self.assertRaises(ValidationError) as caught:
            ErrorResponse(error=values)
        self.assertNotIn("fixture-sensitive-input", str(caught.exception))
        # .errors() 默认仍可能带 input；后续 HTTP 异常处理不能直接返回它。
        self.assertNotIn("fixture-sensitive-input", str(caught.exception.errors(include_input=False)))

    def test_public_fields_align_with_sse_error_without_coupling_handlers(self):
        self.assertEqual(ErrorDetail(**self.values).model_dump(), StreamError(**self.values).model_dump())
        http = ErrorDetail.model_json_schema()
        sse = StreamError.model_json_schema()
        self.assertEqual(http["properties"], sse["properties"])

    def test_json_schema_has_only_required_public_fields(self):
        detail = ErrorDetail.model_json_schema()
        self.assertEqual(set(detail["properties"]), set(self.values))
        self.assertEqual(set(detail["required"]), set(self.values))
        response = ErrorResponse.model_json_schema()
        self.assertEqual(set(response["properties"]), {"error"})
        self.assertEqual(response["required"], ["error"])


if __name__ == "__main__":
    unittest.main()
