"""消息、记忆等 Schema 共用的类型规则；不连接数据库，也不生成新编号。"""

import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, Field

from app.schemas._validation import database_datetime_as_utc, format_database_id


def _format_id(value: object) -> str:
    return format_database_id(value, "ID")


def _normalize_uuid(value: object) -> str:
    # 接收标准带连字符的 UUID 字符串，允许大写输入，统一保存/返回小写。
    # 不接受数字、空白、无连字符、urn:uuid: 或花括号等其他表示法。
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value
    ):
        raise ValueError("Expected a UUID string in the standard hyphenated format")
    return str(UUID(value))


# Annotated 的意思是“原有类型 + 校验规则”，不需要每个编号都重复写 validator。
DatabaseID = Annotated[str, BeforeValidator(_format_id)]
UUIDString = Annotated[str, BeforeValidator(_normalize_uuid), Field(json_schema_extra={"format": "uuid"})]
UTCDateTime = Annotated[datetime, AfterValidator(database_datetime_as_utc)]
