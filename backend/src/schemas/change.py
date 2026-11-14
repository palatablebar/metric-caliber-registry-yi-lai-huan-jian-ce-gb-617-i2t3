from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChangeIn(BaseModel):
    target_ref: str = Field(min_length=2, max_length=120)
    change_kind: Literal["field_type", "caliber", "formula"] = "caliber"
    metric_code: str | None = Field(default=None, max_length=48)
    reason: str = Field(default="", max_length=1000)
    proposed_dsl: str | None = Field(default=None, max_length=1200)
    # 乐观并发：提交所基于的口径版本；口径/公式类变更必填
    base_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, max_length=160)


class DecisionIn(BaseModel):
    status: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


class DraftResolveIn(BaseModel):
    action: Literal["resolve", "discard"] = "resolve"
    note: str | None = Field(default=None, max_length=1000)
