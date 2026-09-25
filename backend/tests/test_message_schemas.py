"""消息 Schema 的内存内测试，不访问数据库，不调用 Agent 或发送 SSE。"""

import json
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.models import Message
from app.schemas import (
    AssistantMessageResponse,
    DuplicateMessageResponse,
    GenerationError,
    GenerationResponse,
    MessageDeltaData,
    MessageDoneData,
    MessageErrorData,
    MessageHistoryResponse,
    MessageResponse,
    MessageStartData,
    RetryMessageRequest,
    SendMessageRequest,
    UserMessageResponse,
)

ATTEMPT = "30dfe922-1183-4dfb-9ce7-ea940d619195"
CLIENT_KEY = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 9, 24, 8, 0)


def generation(status="FAILED", can_retry=False):
    return {
        "attempt_id": ATTEMPT, "status": status,
        "assistant_message_id": "102" if status == "SUCCEEDED" else None,
        "error": {"code": "GENERATION_TIMEOUT", "message": "Generation timed out."} if status == "FAILED" else None,
        "can_retry": can_retry,
    }


def question(status="FAILED", can_retry=False):
    return {
        "message_id": 101, "role": "USER", "content": "What is an API?",
        "in_reply_to_message_id": None, "created_at": NOW,
        "generation": generation(status, can_retry),
    }


def answer():
    return {
        "message_id": 102, "role": "ASSISTANT", "content": "An API is an interface.",
        "in_reply_to_message_id": 101, "created_at": NOW + timedelta(seconds=1),
    }


def history(items, busy=False):
    return {"session_id": 1, "is_generating": busy, "items": items}


class MessageRequestTest(unittest.TestCase):
    def test_send_preserves_code_and_normalizes_uuid_case(self):
        content = "\n    if ready:\n        print('你好')\n"
        request = SendMessageRequest.model_validate({"client_message_key": CLIENT_KEY.upper(), "content": content})
        self.assertEqual(request.client_message_key, CLIENT_KEY)
        self.assertEqual(request.content, content)
        self.assertEqual(SendMessageRequest.model_validate_json(request.model_dump_json()), request)

    def test_send_rejects_blank_text_but_preserves_valid_edge_spaces(self):
        for text in ("", " ", "\t\r\n", "\u3000\u00a0"):
            with self.subTest(text=repr(text)), self.assertRaises(ValidationError):
                SendMessageRequest.model_validate({"client_message_key": CLIENT_KEY, "content": text})
        self.assertEqual(SendMessageRequest(client_message_key=CLIENT_KEY, content=" a ").content, " a ")

    def test_send_character_boundaries_not_byte_boundaries(self):
        self.assertEqual(len(SendMessageRequest(client_message_key=CLIENT_KEY, content="雨" * 20000).content), 20000)
        with self.assertRaises(ValidationError):
            SendMessageRequest(client_message_key=CLIENT_KEY, content="雨" * 20001)

    def test_requests_require_uuid_strings_in_standard_format(self):
        for schema, name, extra in (
            (SendMessageRequest, "client_message_key", {"content": "hello"}),
            (RetryMessageRequest, "failed_attempt_id", {}),
        ):
            valid = schema.model_validate({name: ATTEMPT.upper()} | extra)
            self.assertEqual(getattr(valid, name), ATTEMPT)
            for bad in (None, True, 123, [], UUID(ATTEMPT), "bad", " " + ATTEMPT,
                        ATTEMPT.replace("-", ""), "{" + ATTEMPT + "}", "urn:uuid:" + ATTEMPT):
                with self.subTest(schema=schema.__name__, bad=repr(bad)), self.assertRaises(ValidationError):
                    schema.model_validate({name: bad} | extra)

    def test_send_missing_fields_and_wrong_content_types(self):
        for data in ({}, {"content": "hello"}, {"client_message_key": CLIENT_KEY}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                SendMessageRequest.model_validate(data)
        for bad in (None, 123, True, [], {}, b"hello"):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValidationError):
                SendMessageRequest.model_validate({"client_message_key": CLIENT_KEY, "content": bad})

    def test_requests_reject_context_and_legacy_retry_fields(self):
        for field in ("user_id", "session_id", "model", "tools", "api_key", "messages", "memory"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                SendMessageRequest.model_validate({"client_message_key": CLIENT_KEY, "content": "hello", field: "bad"})
        for field in ("retry_key", "content", "attempt_id", "message_id", "user_id"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RetryMessageRequest.model_validate({"failed_attempt_id": ATTEMPT, field: "bad"})
        with self.assertRaises(ValidationError):
            RetryMessageRequest.model_validate({})

    def test_request_json_schema_documents_uuid_and_limits(self):
        schema = SendMessageRequest.model_json_schema()
        self.assertEqual(set(schema["required"]), {"client_message_key", "content"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["client_message_key"]["format"], "uuid")
        self.assertEqual(schema["properties"]["content"]["maxLength"], 20000)
        self.assertEqual(RetryMessageRequest.model_json_schema()["required"], ["failed_attempt_id"])


class GenerationSchemaTest(unittest.TestCase):
    def test_valid_states_and_nullable_fields_are_present(self):
        for status in ("RUNNING", "SUCCEEDED", "FAILED"):
            with self.subTest(status=status):
                payload = GenerationResponse.model_validate(generation(status)).model_dump(mode="json")
                self.assertEqual(set(payload), {"attempt_id", "status", "assistant_message_id", "error", "can_retry"})
        self.assertTrue(GenerationResponse.model_validate(generation("FAILED", True)).can_retry)

    def test_state_field_contradictions_are_rejected(self):
        invalid = [
            generation("RUNNING") | {"assistant_message_id": "102"},
            generation("RUNNING") | {"can_retry": True},
            generation("RUNNING") | {"error": generation()["error"]},
            generation("SUCCEEDED") | {"assistant_message_id": None},
            generation("SUCCEEDED") | {"can_retry": True},
            generation("SUCCEEDED") | {"error": generation()["error"]},
            generation("FAILED") | {"error": None},
            generation("FAILED") | {"assistant_message_id": "102"},
            generation() | {"status": "QUEUED"},
            generation() | {"can_retry": 1},
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                GenerationResponse.model_validate(data)

    def test_generation_requires_explicit_fields(self):
        for field in generation():
            data = generation()
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                GenerationResponse.model_validate(data)

    def test_error_summary_length_and_internal_field_filtering(self):
        for field, max_length in (("code", 64), ("message", 500)):
            for value in ("", "a" * (max_length + 1), None, 1):
                data = {"code": "FAILED", "message": "Failed."} | {field: value}
                with self.subTest(field=field, length=len(value) if isinstance(value, str) else None), self.assertRaises(ValidationError):
                    GenerationError.model_validate(data)
        self.assertEqual(GenerationError.model_validate({
            "code": "FAILED", "message": "Failed.", "traceback": "private", "request_id": "not-in-history",
        }).model_dump(), {"code": "FAILED", "message": "Failed."})


class MessageHistoryTest(unittest.TestCase):
    def test_history_supports_empty_failed_running_and_successful_snapshots(self):
        self.assertEqual(MessageHistoryResponse.model_validate(history([])).model_dump(mode="json"),
                         {"session_id": "1", "is_generating": False, "items": []})
        MessageHistoryResponse.model_validate(history([question("FAILED", True)]))
        MessageHistoryResponse.model_validate(history([question("RUNNING")], busy=True))
        payload = MessageHistoryResponse.model_validate(history([question("SUCCEEDED"), answer()])).model_dump(mode="json")
        self.assertIn("generation", payload["items"][0])
        self.assertNotIn("generation", payload["items"][1])
        self.assertEqual(payload["items"][1]["in_reply_to_message_id"], "101")
        self.assertEqual(payload["items"][0]["created_at"], "2026-09-24T08:00:00Z")

    def test_role_discriminator_and_required_fields(self):
        adapter = TypeAdapter(MessageResponse)
        for role in ("SYSTEM", "TOOL", "user", None):
            with self.subTest(role=role), self.assertRaises(ValidationError):
                adapter.validate_python(question() | {"role": role})
        for field in ("role", "generation", "in_reply_to_message_id", "created_at"):
            data = question()
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                adapter.validate_python(data)
        with self.assertRaises(ValidationError):
            AssistantMessageResponse.model_validate(answer() | {"in_reply_to_message_id": None})
        with self.assertRaises(ValidationError):
            UserMessageResponse.model_validate(question() | {"in_reply_to_message_id": "99"})

    def test_assistant_output_reads_orm_without_leaking_internal_fields(self):
        row = Message(**answer(), chat_session_id=1, model_key="private-model", updated_at=NOW)
        payload = AssistantMessageResponse.model_validate(row).model_dump(mode="json")
        self.assertEqual(set(payload), {"message_id", "role", "content", "in_reply_to_message_id", "created_at"})
        extra = answer() | {"generation": generation(), "tool_calls": ["private"], "reasoning": "private"}
        self.assertEqual(AssistantMessageResponse.model_validate(extra).model_dump(mode="json"), payload)

    def test_history_json_roundtrip_and_timezone_conversion(self):
        q = question()
        q["created_at"] = datetime(2026, 9, 24, 20, tzinfo=timezone(timedelta(hours=12)))
        response = MessageHistoryResponse.model_validate(history([q]))
        self.assertEqual(response.items[0].created_at, NOW.replace(tzinfo=UTC))
        self.assertEqual(MessageHistoryResponse.model_validate_json(response.model_dump_json()), response)

    def test_large_ids_are_strings_and_invalid_ids_are_rejected(self):
        q = question() | {"message_id": 18446744073709551615}
        self.assertEqual(UserMessageResponse.model_validate(q).message_id, "18446744073709551615")
        for bad in (None, True, 0, -1, 1.5, "01", "bad", 18446744073709551616):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                UserMessageResponse.model_validate(question() | {"message_id": bad})

    def test_old_failure_remains_visible_after_new_question_succeeds(self):
        old = question() | {"message_id": "99", "created_at": NOW - timedelta(minutes=1)}
        response = MessageHistoryResponse.model_validate(history([old, question("SUCCEEDED"), answer()]))
        self.assertEqual(response.items[0].generation.status, "FAILED")
        self.assertFalse(response.items[0].generation.can_retry)
        invalid = deepcopy(old)
        invalid["generation"]["can_retry"] = True
        with self.assertRaises(ValidationError):
            MessageHistoryResponse.model_validate(history([invalid, question("SUCCEEDED"), answer()]))

    def test_busy_and_retry_flags_are_consistent_including_cleanup(self):
        # 清理尚未结束时允许 FAILED + busy，但不能重试。
        MessageHistoryResponse.model_validate(history([question("FAILED")], busy=True))
        for data in (
            history([question("FAILED", True)], busy=True),
            history([question("RUNNING")], busy=False),
            history([question("RUNNING"), question("RUNNING") | {"message_id": 103}], busy=True),
            history([]) | {"is_generating": "false"},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                MessageHistoryResponse.model_validate(data)

    def test_duplicate_ids_out_of_order_and_dangling_replies_are_rejected(self):
        for items in (
            [question(), question()],
            [answer(), question("SUCCEEDED")],
            [question("SUCCEEDED")],
            [answer()],
            [question("FAILED"), answer()],
            [question("SUCCEEDED"), answer() | {"in_reply_to_message_id": 999}],
            [question("SUCCEEDED"), answer(), answer() | {"message_id": 103}],
        ):
            with self.subTest(items=items), self.assertRaises(ValidationError):
                MessageHistoryResponse.model_validate(history(items))
        with self.assertRaises(ValidationError):
            AssistantMessageResponse.model_validate(answer() | {"in_reply_to_message_id": 102})

    def test_same_time_history_uses_numeric_id_order(self):
        # 字符串排序会把 10 放在 9 前面，但数据库数字 ID 的顺序应该是 9、10。
        q1 = question() | {"message_id": "9"}
        q2 = question() | {"message_id": "10"}
        MessageHistoryResponse.model_validate(history([q1, q2]))
        with self.assertRaises(ValidationError):
            MessageHistoryResponse.model_validate(history([q2, q1]))

    def test_history_requires_list_and_has_no_legacy_summary_or_pagination(self):
        for data in ({"session_id": 1, "is_generating": False}, history(None), history([None])):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                MessageHistoryResponse.model_validate(data)
        schema = MessageHistoryResponse.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"session_id", "is_generating", "items"})
        self.assertEqual(schema["properties"]["items"]["items"]["discriminator"]["propertyName"], "role")


class DuplicateAndStreamTest(unittest.TestCase):
    def receipt(self, status="RUNNING"):
        return {"duplicate": True, "session_id": 1, "user_message_id": 101, "attempt_id": ATTEMPT,
                "status": status, "assistant_message_id": 102 if status == "SUCCEEDED" else None}

    def test_duplicate_receipts_for_all_three_states(self):
        for status in ("RUNNING", "SUCCEEDED", "FAILED"):
            with self.subTest(status=status):
                payload = DuplicateMessageResponse.model_validate(self.receipt(status)).model_dump(mode="json")
                self.assertEqual(payload["user_message_id"], "101")
                self.assertEqual(set(payload), set(self.receipt(status)))

    def test_invalid_duplicate_receipts_are_rejected(self):
        for bad in (False, 1, "true", None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                DuplicateMessageResponse.model_validate(self.receipt() | {"duplicate": bad})
        for data in (self.receipt() | {"assistant_message_id": 102},
                     self.receipt("SUCCEEDED") | {"assistant_message_id": None},
                     self.receipt("SUCCEEDED") | {"assistant_message_id": 101},
                     self.receipt() | {"status": "QUEUED"}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                DuplicateMessageResponse.model_validate(data)

    def test_sse_event_names_are_metadata_not_data_fields(self):
        examples = (
            (MessageStartData, "message_start", {"session_id": 1, "user_message_id": 101, "attempt_id": ATTEMPT}),
            (MessageDeltaData, "message_delta", {"attempt_id": ATTEMPT, "text": "hello"}),
            (MessageDoneData, "message_done", {"attempt_id": ATTEMPT, "assistant_message": answer()}),
            (MessageErrorData, "message_error", {"attempt_id": ATTEMPT, "error": {
                "code": "MODEL_REQUEST_FAILED", "message": "Generation failed.", "request_id": "req_example",
            }}),
        )
        for model, name, data in examples:
            with self.subTest(model=model.__name__):
                response = model.model_validate(data)
                self.assertEqual(model.event_name, name)
                self.assertEqual(set(response.model_dump(mode="json")), set(data))
                self.assertNotIn("event_name", model.model_json_schema()["properties"])
                self.assertEqual(model.model_validate_json(response.model_dump_json()), response)

    def test_sse_delta_preserves_whitespace_and_escapes_newlines_in_json(self):
        for text in (" ", "\n", "\t", "\n    print('hello')\n"):
            with self.subTest(text=repr(text)):
                response = MessageDeltaData(attempt_id=ATTEMPT, text=text)
                self.assertEqual(json.loads(response.model_dump_json())["text"], text)
                self.assertNotIn("\n", response.model_dump_json())
        for bad in ("", None, 1):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                MessageDeltaData.model_validate({"attempt_id": ATTEMPT, "text": bad})

    def test_sse_done_requires_assistant_and_error_requires_request_id(self):
        with self.assertRaises(ValidationError):
            MessageDoneData.model_validate({"attempt_id": ATTEMPT, "assistant_message": question()})
        for error in ({"code": "FAILED", "message": "Failed."},
                      {"code": "FAILED", "message": "Failed.", "request_id": ""}):
            with self.subTest(error=error), self.assertRaises(ValidationError):
                MessageErrorData.model_validate({"attempt_id": ATTEMPT, "error": error})

    def test_schemas_do_not_open_database_connections(self):
        with patch("pymysql.connect", side_effect=AssertionError("Database access is not allowed")) as connect:
            SendMessageRequest(client_message_key=CLIENT_KEY, content="Hello")
            RetryMessageRequest(failed_attempt_id=ATTEMPT)
            MessageHistoryResponse.model_validate(history([question("SUCCEEDED"), answer()]))
            DuplicateMessageResponse.model_validate(self.receipt())
            MessageDoneData.model_validate({"attempt_id": ATTEMPT, "assistant_message": answer()})
            connect.assert_not_called()
