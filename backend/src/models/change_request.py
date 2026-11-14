"""变更单：口径/公式/字段类型变更的影响面与审批记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cr_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    metric_id: Mapped[int | None] = mapped_column(
        ForeignKey("metrics.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_ref: Mapped[str] = mapped_column(String(120), index=True)
    change_kind: Mapped[str] = mapped_column(String(20), default="caliber", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    proposed_dsl: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 乐观并发：提交时声明所基于的口径版本
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    # 提交时锁定的下游影响与依赖图快照（审批拿的是当时的影响面，不随后续提交漂移）
    impact_graph_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # 提交时对「假设新口径已生效」的图做环检测的结论
    cycle_check_status: Mapped[str] = mapped_column(String(12), default="not_run", index=True)
    cycle_check_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    # 批准后生成的正式口径版本；未批准为空
    applied_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 幂等键：同一变更单的重复提交/重放不新增版本与审计
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(12), default="medium", index=True)
    impact_score: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requester_email: Mapped[str] = mapped_column(String(160), default="")
    approver_email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
