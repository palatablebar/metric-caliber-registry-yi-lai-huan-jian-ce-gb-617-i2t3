"""冲突草案：基于过期版本的口径提交不会覆盖任何人，先落草案并记录差异。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class FormulaDraft(Base):
    __tablename__ = "formula_drafts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_formula_drafts_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id", ondelete="CASCADE"), index=True)
    # 提交人声明所基于的版本；与提交时 current_version 不一致即为过期提交
    base_version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    # 提交时指标的实际最新版本
    head_version: Mapped[int] = mapped_column(Integer, default=1)
    proposed_dsl: Mapped[str] = mapped_column(Text)
    proposed_signature: Mapped[str] = mapped_column(String(64), default="", index=True)
    base_signature: Mapped[str] = mapped_column(String(64), default="")
    head_signature: Mapped[str] = mapped_column(String(64), default="")
    parse_status: Mapped[str] = mapped_column(String(12), default="ok", index=True)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    # 与 head 版本口径的结构化差异（fields / filters / aggs / dims / deps / expression）
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    diff_summary: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    source: Mapped[str] = mapped_column(String(20), default="formula_save")  # formula_save / change_request
    change_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_email: Mapped[str] = mapped_column(String(160), default="", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True, index=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
