"""变更单控制器：提交（含算法 B 影响面）/ 审批 / 列表。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.constants.enums import ChangeStatus
from src.services import audit_service, change_service


def list_changes(db: Session, **filters) -> dict:
    return change_service.list_changes(db, **filters)


def get_change(db: Session, change_id: int) -> dict:
    change = change_service.get_change(db, change_id)
    return change_service.serialize(change)


def create_change(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    change = change_service.create_change(db, payload, requester=principal["email"])
    replayed = getattr(change, "replayed", False)
    if replayed and request is not None:
        request.state.idempotent_replay = True
    if not replayed:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="change.create",
            entity="change_request",
            entity_id=change.id,
            detail={
                "cr_no": change.cr_no,
                "target": change.target_ref,
                "severity": change.severity,
                "score": change.impact_score,
                "base_version": change.base_version,
                "cycle_check": change.cycle_check_status,
                "conflict_draft": getattr(change, "conflict_draft_created", False),
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    result = change_service.serialize(change)
    result["replayed"] = replayed
    result["conflict_draft_created"] = getattr(change, "conflict_draft_created", False)
    return result


def decide(db: Session, change_id: int, payload: dict, principal: dict, request: Request) -> dict:
    change = change_service.get_change(db, change_id)
    before_status = change.status
    change = change_service.decide(
        db, change, status=payload["status"], note=payload.get("note"), approver=principal["email"]
    )
    replayed = getattr(change, "replayed", False)
    if replayed and request is not None:
        request.state.idempotent_replay = True
    # 只有真正发生状态流转才写审计；重放/拦截不产生新审计记录
    if not replayed and before_status == ChangeStatus.PENDING.value:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="change.approve" if payload["status"] == "approved" else "change.reject",
            entity="change_request",
            entity_id=change.id,
            detail={
                "cr_no": change.cr_no,
                "status": change.status,
                "applied_version": change.applied_version,
                "applied_revision": change.applied_revision,
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    result = change_service.serialize(change)
    result["replayed"] = replayed
    return result
