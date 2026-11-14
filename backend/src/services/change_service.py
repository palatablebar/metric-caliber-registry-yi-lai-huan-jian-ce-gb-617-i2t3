"""变更单服务：提交带基线版本、锁定影响面与依赖图、环检测通过+审批后才生成正式版本。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANGE_KIND_LABELS,
    CHANGE_STATUS_LABELS,
    ChangeKind,
    ChangeStatus,
)
from src.models.change_request import ChangeRequest
from src.models.formula import Formula
from src.models.metric import Metric
from src.services import (
    formula_service,
    graph_service,
    revision_service,
)
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, local_now, utcnow


def serialize(change: ChangeRequest, metric_code: str | None = None) -> dict:
    return {
        "id": change.id,
        "cr_no": change.cr_no,
        "metric_id": change.metric_id,
        "metric_code": metric_code,
        "target_ref": change.target_ref,
        "change_kind": change.change_kind,
        "change_kind_label": CHANGE_KIND_LABELS.get(change.change_kind, change.change_kind),
        "reason": change.reason,
        "proposed_dsl": change.proposed_dsl,
        "base_version": change.base_version,
        "impact": change.impact or {},
        "impact_graph_snapshot": change.impact_graph_snapshot or {},
        "cycle_check_status": change.cycle_check_status,
        "cycle_check_label": revision_service.cycle_check_label(change.cycle_check_status),
        "cycle_check_detail": change.cycle_check_detail or {},
        "applied_version": change.applied_version,
        "applied_revision": change.applied_revision,
        "idempotency_key": change.idempotency_key,
        "severity": change.severity,
        "impact_score": change.impact_score,
        "status": change.status,
        "status_label": CHANGE_STATUS_LABELS.get(change.status, change.status),
        "requester_email": change.requester_email,
        "approver_email": change.approver_email,
        "decision_note": change.decision_note,
        "decided_at": iso_utc(change.decided_at),
        "created_at": iso_utc(change.created_at),
    }


def next_cr_no(db: Session) -> str:
    prefix = f"CR{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(ChangeRequest).where(ChangeRequest.cr_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def compute_impact(db: Session, *, change_kind: str, target_ref: str, metric_code: str | None) -> dict:
    if change_kind == ChangeKind.FIELD_TYPE.value:
        return graph_service.impact_for_field(db, target_ref)
    if not metric_code:
        raise AppError("VALIDATION", 422, {"field": "metric_code", "message": "口径/公式类变更需要指定指标"})
    return graph_service.impact_for_metric(db, metric_code)


def _idempotency_key(payload: dict, requester: str) -> str | None:
    raw = payload.get("idempotency_key")
    if raw:
        return str(raw)[:160]
    # 未显式给键时，用「人 + 目标 + DSL + 基线」派生稳定键，保证重复提交可重放
    dsl = (payload.get("proposed_dsl") or "").strip()
    if not dsl:
        return None
    import hashlib

    digest = hashlib.sha256(
        f"{requester}|{payload.get('target_ref')}|{payload.get('base_version')}|{dsl}".encode("utf-8")
    ).hexdigest()
    return f"cr-{digest[:32]}"


def _replay_change(db: Session, key: str | None) -> ChangeRequest | None:
    if not key:
        return None
    return db.scalar(select(ChangeRequest).where(ChangeRequest.idempotency_key == key))


def create_change(db: Session, payload: dict, *, requester: str) -> ChangeRequest:
    change_kind = payload.get("change_kind") or ChangeKind.CALIBER.value
    target_ref = (payload.get("target_ref") or "").strip()
    metric_code = (payload.get("metric_code") or "").strip() or None
    if not target_ref:
        raise AppError("VALIDATION", 422, {"field": "target_ref"})
    if metric_code is None and change_kind != ChangeKind.FIELD_TYPE.value:
        raise AppError("VALIDATION", 422, {"field": "metric_code"})

    idem_key = _idempotency_key(payload, requester)
    replay = _replay_change(db, idem_key)
    if replay is not None:
        # 同一变更重放：原样返回，不新增版本、不新增草案、不新增审计
        replay.replayed = True  # type: ignore[attr-defined]
        return replay

    # 同一目标 + 同类变更已有待审批单：保留原有 409 守卫，避免两条待审批单竞争出版本
    pending = db.scalar(
        select(ChangeRequest).where(
            ChangeRequest.target_ref == target_ref,
            ChangeRequest.change_kind == change_kind,
            ChangeRequest.status == ChangeStatus.PENDING.value,
        )
    )
    if pending is not None:
        raise AppError("CONFLICT", 409, {"cr_no": pending.cr_no, "status": pending.status})

    metric = None
    if metric_code:
        metric = db.scalar(select(Metric).where(Metric.code == metric_code))
        if metric is None:
            raise AppError("NOT_FOUND", 404, {"entity": "metric", "code": metric_code})
        target_ref = metric_code

    proposed_dsl = (payload.get("proposed_dsl") or "").strip() or None
    base_version = payload.get("base_version")
    if metric is not None and proposed_dsl and base_version is None:
        # 口径类提交必须声明所基于的版本
        raise AppError(
            "VALIDATION",
            422,
            {"field": "base_version", "message": "口径提交必须附带所基于的版本号 base_version"},
        )

    # 字段类型变更：无 DSL、无指标基线，走原影响面逻辑
    if metric is None or not proposed_dsl:
        impact = compute_impact(db, change_kind=change_kind, target_ref=target_ref, metric_code=metric_code)
        change = ChangeRequest(
            cr_no=next_cr_no(db),
            metric_id=metric.id if metric else None,
            target_ref=target_ref,
            change_kind=change_kind,
            reason=payload.get("reason") or "",
            proposed_dsl=proposed_dsl,
            base_version=base_version,
            impact=impact,
            impact_graph_snapshot={},
            cycle_check_status="not_run",
            severity=impact.get("severity") or "low",
            impact_score=int(impact.get("score") or 0),
            status=ChangeStatus.PENDING.value,
            requester_email=requester,
            idempotency_key=idem_key,
        )
        db.add(change)
        db.flush()
        return change

    # 口径/公式类：先做算法 A 解析
    parsed = formula_service.parse_or_raise(proposed_dsl, metric.code)
    head_version = metric.current_version
    declared_base = int(base_version)
    base_formula = formula_service.version_for(db, metric, declared_base)
    if base_formula is None:
        raise AppError(
            "VALIDATION",
            422,
            {"field": "base_version", "current_version": head_version, "message": "所基于的版本不存在"},
        )
    head_formula = formula_service.latest_for(db, metric)

    # 提交时刻锁定影响面与依赖图快照
    locked = graph_service.locked_impact_snapshot(db, metric, parsed)
    impact = locked["impact"]

    # 对假设图做环检测（提交时先检一次，结果随单锁定）
    cycle_check = graph_service.hypothetical_cycle_check(db, metric, parsed)

    change = ChangeRequest(
        cr_no=next_cr_no(db),
        metric_id=metric.id,
        target_ref=target_ref,
        change_kind=change_kind,
        reason=payload.get("reason") or "",
        proposed_dsl=proposed_dsl,
        base_version=declared_base,
        impact=impact,
        impact_graph_snapshot=locked,
        cycle_check_status=cycle_check["status"],
        cycle_check_detail=cycle_check,
        severity=impact.get("severity") or "low",
        impact_score=int(impact.get("score") or 0),
        status=ChangeStatus.PENDING.value,
        requester_email=requester,
        idempotency_key=idem_key,
    )
    db.add(change)
    db.flush()

    # 过期提交：不覆盖、不阻塞，保留为冲突草案并说明与 head 的差异
    if declared_base != head_version:
        draft_key = f"draft-m{metric.id}-{idem_key}" if idem_key else None
        revision_service.create_conflict_draft(
            db,
            metric=metric,
            base_version=declared_base,
            proposed_dsl=proposed_dsl,
            proposed_parsed=parsed,
            base_formula=base_formula,
            head_formula=head_formula,
            source="change_request",
            author_email=requester,
            note=payload.get("reason") or "",
            change_request=change,
            idempotency_key=draft_key,
        )
        change.conflict_draft_created = True  # type: ignore[attr-defined]
    return change


def decide(
    db: Session,
    change: ChangeRequest,
    *,
    status: str,
    note: str | None,
    approver: str,
) -> ChangeRequest:
    if change.status != ChangeStatus.PENDING.value:
        # 已审批单的重复决策：直接返回现状（重放不新增版本与审计）
        change.replayed = True  # type: ignore[attr-defined]
        return change
    if status not in (ChangeStatus.APPROVED.value, ChangeStatus.REJECTED.value):
        raise AppError("VALIDATION", 422, {"field": "status"})

    if status == ChangeStatus.REJECTED.value:
        # 驳回：保留原口径与原依赖图，不动任何公式/边
        change.status = status
        change.decision_note = note
        change.approver_email = approver
        change.decided_at = utcnow()
        db.flush()
        return change

    # ---- 批准路径：环检测通过 + 审批完成才生成正式版本 ----
    metric = db.get(Metric, change.metric_id) if change.metric_id else None
    if metric is None or not change.proposed_dsl:
        # 字段类型等无 DSL 变更：批准只改状态
        change.status = status
        change.decision_note = note
        change.approver_email = approver
        change.decided_at = utcnow()
        db.flush()
        return change

    parsed = formula_service.parse_or_raise(change.proposed_dsl, metric.code)

    # 1) 提交后指标又被别人推进：基线已过期，禁止把旧口径直接落成新版本
    if change.base_version is not None and change.base_version != metric.current_version:
        raise AppError(
            "CONFLICT",
            409,
            {
                "cr_no": change.cr_no,
                "base_version": change.base_version,
                "current_version": metric.current_version,
                "message": "该指标在本变更单提交后已有新版本，请基于最新版本重新提交（原变更单保留为冲突草案）",
            },
        )

    # 2) 环检测复算（假设图）：不通过则整笔回滚，正式版本不生成、原口径与原依赖图保留
    cycle_check = graph_service.hypothetical_cycle_check(db, metric, parsed)
    change.cycle_check_status = cycle_check["status"]
    change.cycle_check_detail = cycle_check
    if cycle_check["status"] != "passed":
        db.flush()
        raise AppError(
            "CONFLICT",
            409,
            {
                "cr_no": change.cr_no,
                "reason": "cycle",
                "cycles": cycle_check["cycles"],
                "message": "批准被拦截：新口径会引入依赖环，请先打断引用链",
            },
        )

    # 3) 幂等：同 CR 已生成过正式版本则直接复用，不新增版本/审计
    existing = db.scalar(
        select(Formula).where(
            Formula.metric_id == metric.id,
            Formula.change_request_id == change.id,
        )
    )
    if existing is not None:
        change.applied_version = existing.version
        change.applied_revision = existing.revision
        change.status = status
        change.decision_note = note
        change.approver_email = approver
        change.decided_at = utcnow()
        db.flush()
        return change

    # 4) 生成正式版本（revision 与依赖图快照在同一次提交里绑定）
    formula = formula_service.save_version(
        db,
        metric,
        dsl_text=change.proposed_dsl,
        author_email=approver,
        note=f"批准 {change.cr_no}：{change.reason or ''}".strip(),
        parsed=parsed,
        source="change_request",
        change_request_id=change.id,
    )
    change.applied_version = formula.version
    change.applied_revision = formula.revision
    change.status = status
    change.decision_note = note
    change.approver_email = approver
    change.decided_at = utcnow()
    db.flush()
    return change


def get_change(db: Session, change_id: int) -> ChangeRequest:
    change = db.get(ChangeRequest, change_id)
    if change is None:
        raise not_found("change_request", change_id)
    return change


def list_changes(
    db: Session,
    *,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(ChangeRequest)
    count_stmt = select(func.count()).select_from(ChangeRequest)
    if status:
        stmt = stmt.where(ChangeRequest.status == status)
        count_stmt = count_stmt.where(ChangeRequest.status == status)
    if severity:
        stmt = stmt.where(ChangeRequest.severity == severity)
        count_stmt = count_stmt.where(ChangeRequest.severity == severity)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(ChangeRequest.id.desc()).limit(limit).offset(offset)).all()
    codes = {metric.id: metric.code for metric in db.scalars(select(Metric)).all()}
    return {
        "items": [serialize(row, codes.get(row.metric_id or -1)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
