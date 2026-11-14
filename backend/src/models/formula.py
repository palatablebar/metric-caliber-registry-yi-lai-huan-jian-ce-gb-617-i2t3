"""口径公式：一个指标可以有多个版本；解析结果（算法 A）落在 parsed 里。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Formula(Base):
    __tablename__ = "formulas"
    __table_args__ = (UniqueConstraint("metric_id", "version", name="uq_formulas_metric_version"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dsl_text: Mapped[str] = mapped_column(Text)
    parse_status: Mapped[str] = mapped_column(String(12), default="ok", index=True)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    signature: Mapped[str] = mapped_column(String(64), default="", index=True)
    author_email: Mapped[str] = mapped_column(String(160), default="seed@metricreg.io")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 版本与依赖图同源证明：revision 同值表示当前口径与所展示依赖图来自同一次提交
    revision: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    # 提交来源（seed / formula_save / change_request）与来源变更单
    source: Mapped[str] = mapped_column(String(20), default="seed")
    change_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 该版本提交时刻锁定的依赖图快照（指标/字段引用，不随后续重建漂移）
    graph_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
