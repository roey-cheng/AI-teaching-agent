"""记忆响应的离线检查；只构造对象，不连接 MySQL 或调用 Agent。"""

from datetime import UTC, datetime, timedelta, timezone
from typing import get_args
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.models import AgentMemory
from app.models.agent_memory import MEMORY_TOPIC_TYPES
from app.schemas import MemoryListResponse, MemoryResponse, MemoryType


class MemorySchemaTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 25, 8, 30, 0, 123456)
        self.values = dict(memory_id=301, memory_type="LEARNING_PREFERENCE",
                           summary="喜欢使用中文解释。", updated_at=self.now)

    def test_response_has_only_four_documented_fields(self):
        response = MemoryResponse.model_validate(self.values | {
            "user_id": 123, "memory_key": "preference.language", "created_at": self.now,
            "profile_version": 42, "private_value": "not-for-output",
        })
        self.assertEqual(response.model_dump(mode="json"), {
            "memory_id": "301", "memory_type": "LEARNING_PREFERENCE",
            "summary": "喜欢使用中文解释。", "updated_at": "2026-09-25T08:30:00.123456Z",
        })

    def test_reads_orm_object_without_database_access(self):
        with patch("pymysql.connect") as connect:
            row = AgentMemory(**self.values, user_id=123,
                              memory_key="preference.language", created_at=self.now)
            response = MemoryResponse.model_validate(row)
            MemoryListResponse(items=[response])
            connect.assert_not_called()
        self.assertEqual(response.memory_id, "301")
        self.assertEqual(row.memory_id, 301)  # 格式化响应不修改 ORM 对象。
        self.assertNotIn("user_id", response.model_dump())

    def test_five_categories_match_all_database_topics(self):
        self.assertEqual(set(get_args(MemoryType)), set(MEMORY_TOPIC_TYPES.values()))
        self.assertEqual(len(get_args(MemoryType)), 5)
        for key, category in MEMORY_TOPIC_TYPES.items():
            with self.subTest(topic=key):
                response = MemoryResponse(**(self.values | {"memory_type": category}))
                self.assertEqual(response.memory_type, category)

    def test_unknown_category_and_topic_as_category_are_rejected(self):
        for category in ("UNKNOWN", "learning_goal", "preference.language", "", None, 1):
            with self.subTest(category=category), self.assertRaises(ValidationError):
                MemoryResponse(**(self.values | {"memory_type": category}))

    def test_summary_length_and_original_text_are_preserved(self):
        for summary in ("中", "中" * 500, "  喜欢分段解释\n并保留示例。  "):
            self.assertEqual(MemoryResponse(**(self.values | {"summary": summary})).summary, summary)
        for summary in ("", "中" * 501, 123, None, ["text"]):
            with self.subTest(summary_type=type(summary)), self.assertRaises(ValidationError):
                MemoryResponse(**(self.values | {"summary": summary}))

    def test_id_accepts_uint64_and_rejects_invalid_values(self):
        for value in (301, "301", 18446744073709551615):
            self.assertEqual(MemoryResponse(**(self.values | {"memory_id": value})).memory_id, str(value))
        for value in (0, -1, True, 1.5, "01", "1.0", " 1", 18446744073709551616, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MemoryResponse(**(self.values | {"memory_id": value}))

    def test_time_is_utc_and_json_round_trip_preserves_values(self):
        local = datetime(2026, 9, 25, 20, 30, tzinfo=timezone(timedelta(hours=12)))
        response = MemoryResponse(**(self.values | {"updated_at": local}))
        self.assertEqual(response.updated_at, datetime(2026, 9, 25, 8, 30, tzinfo=UTC))
        self.assertEqual(MemoryResponse.model_validate_json(response.model_dump_json()), response)
        for value in (None, 123, "not-a-date"):
            with self.assertRaises(ValidationError):
                MemoryResponse(**(self.values | {"updated_at": value}))

    def test_all_item_fields_are_required(self):
        for name in self.values:
            values = self.values.copy()
            del values[name]
            with self.subTest(field=name), self.assertRaises(ValidationError):
                MemoryResponse(**values)

    def test_list_requires_explicit_items_and_filters_pagination(self):
        self.assertEqual(MemoryListResponse(items=[]).model_dump(), {"items": []})
        response = MemoryListResponse.model_validate({
            "items": [self.values], "next_cursor": "unused", "has_more": True,
        })
        self.assertEqual(set(response.model_dump()), {"items"})
        self.assertIsInstance(response.items[0], MemoryResponse)
        for values in ({}, {"items": None}, {"items": {}}, {"items": ()}, {"items": [{}]}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                MemoryListResponse.model_validate(values)

    def test_list_does_not_sort_or_generate_memory(self):
        values = [self.values, self.values | {"memory_id": 302}]
        response = MemoryListResponse(items=values)
        self.assertEqual([item.memory_id for item in response.items], ["301", "302"])
        self.assertEqual(MemoryListResponse.model_validate_json(response.model_dump_json()), response)

    def test_json_schema_documents_fields_categories_and_bounds(self):
        schema = MemoryResponse.model_json_schema()
        self.assertEqual(set(schema["properties"]), set(self.values))
        self.assertEqual(set(schema["required"]), set(self.values))
        self.assertEqual(set(schema["properties"]["memory_type"]["enum"]), set(get_args(MemoryType)))
        self.assertEqual(schema["properties"]["summary"]["maxLength"], 500)
        self.assertEqual(MemoryListResponse.model_json_schema()["required"], ["items"])


if __name__ == "__main__":
    unittest.main()
