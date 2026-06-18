from __future__ import annotations

from uuid import UUID

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID | None = None
    message: str = Field(min_length=1, max_length=2_000)
    user_id: int = Field(gt=0)
    site_id: int = Field(gt=0)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message cannot be blank")
        return value

    def conversation_id_text(self) -> str | None:
        if self.conversation_id is None:
            return None
        return str(self.conversation_id)


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str


class RagIndexEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_id: int = Field(gt=0)
    source_type: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=100)
    operation: Literal["created", "updated", "deleted"]


class RagIndexResponse(BaseModel):
    ok: bool
    documents: int
    chunks: int
    deleted_chunks: int
