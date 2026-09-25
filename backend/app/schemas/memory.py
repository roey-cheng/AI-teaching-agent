"""Profile Memory 只读接口的数据格式；不读取、提取或保存记忆。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas._types import DatabaseID, UTCDateTime


MemoryType = Literal[
    "LEARNING_PREFERENCE",
    "LEARNING_GOAL",
    "PROGRAMMING_BACKGROUND",
    "PERSONAL_BACKGROUND",
    "DAILY_PREFERENCE",
]


class MemoryResponse(BaseModel):
    """一条已保存记忆的公开摘要；不暴露 user_id、memory_key 或创建时间。"""

    model_config = ConfigDict(
        from_attributes=True, extra="ignore", strict=True, hide_input_in_errors=True
    )

    memory_id: DatabaseID
    memory_type: MemoryType
    summary: str = Field(min_length=1, max_length=500)
    updated_at: UTCDateTime
    # 25 个 memory_key 是内部主题，以上五种 memory_type 是页面展示类别。
    # 字段过滤不是正文脱敏；记忆的权限和敏感信息过滤仍由业务层负责。


class MemoryListResponse(BaseModel):
    """GET /api/v1/me/memory 的输出；无记忆时明确传入 items=[]。"""

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    items: list[MemoryResponse]
    # 查询层按当前用户过滤，再按 updated_at DESC、memory_id DESC 排序。
    # Schema 不查询数据库、不验证归属、不自动排序，不提供记忆增删改接口。
