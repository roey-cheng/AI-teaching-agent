"""Owner-visible projection of saved provider reasoning, never the full protocol."""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas._types import DatabaseID


class ReasoningStep(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    step: int = Field(ge=1)
    text: str = Field(min_length=1, repr=False)


class MessageReasoningResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    message_id: DatabaseID
    available: bool
    steps: list[ReasoningStep] = Field(repr=False)
