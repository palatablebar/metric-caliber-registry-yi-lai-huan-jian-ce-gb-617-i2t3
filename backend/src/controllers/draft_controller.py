"""冲突草案控制器：过期提交形成的草案列表 / 详情 / 解决（重新基线或废弃）。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.metric import Metric
from src.services import audit_service, revision_service
from src.utils.errors import AppError


def list_drafts(db: Session, *, status: str | None = None, metric_code: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    metric_id = None
    if metric_code:
        metric = db.scalar(select(Metric).where(Metric.code == metric_code))
        if metric is None:
            raise AppError("NOT_FOUND", 404, {"entity": "metric", "code": metric_code})
        metric_id = metric.id
    return revision_service.list_drafts(db, status=status, metric_id=metric_id, limit=limit, offset=offset)


def get_draft(db: Session, draft_id: int) -> dict:
    draft = revision_service.get_draft(db, draft_id)
    metric = db.get(Metric, draft.metric_id)
    return revision_service.serialize(draft, metric.code if metric else None)


def resolve_draft(db: Session, draft_id: int, payload: dict, principal: dict, request: Request) -> dict:
    draft = revision_service.get_draft(db, draft_id)
    metric = db.get(Metric, draft.metric_id)
    if payload.get("action") == "discard":
        revision_service.discard_draft(db, draft, resolution_note=payload.get("note"))
        action = "draft.discard"
    else:
        revision_service.resolve_draft(db, draft, resolution_note=payload.get("note"))
        action = "draft.resolve"
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action=action,
        entity="formula_draft",
        entity_id=draft.id,
        detail={"metric": metric.code if metric else draft.metric_id, "status": draft.status},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return revision_service.serialize(draft, metric.code if metric else None)
