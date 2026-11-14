from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CHANGE_WRITE_ROLES
from src.controllers import conflict_controller, draft_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.change import DraftResolveIn

router = APIRouter(prefix="/conflicts", tags=["conflicts"])


@router.get("")
def list_conflicts(
    severity: str | None = None,
    kind: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return conflict_controller.list_conflicts(db, severity=severity, kind=kind, limit=limit, offset=offset)


@router.get("/drafts")
def list_drafts(
    status: str | None = None,
    metric_code: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return draft_controller.list_drafts(db, status=status, metric_code=metric_code, limit=limit, offset=offset)


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return draft_controller.get_draft(db, draft_id)


@router.patch("/drafts/{draft_id}")
def resolve_draft(
    draft_id: int,
    payload: DraftResolveIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHANGE_WRITE_ROLES)),
) -> dict:
    return draft_controller.resolve_draft(db, draft_id, payload.model_dump(), principal, request)


@router.get("/{conflict_id}")
def conflict_detail(
    conflict_id: str, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return conflict_controller.detail(db, conflict_id)
