"""指标控制器：注册 / 修改 / 算法 A 预解析 / 保存口径版本。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.formula import Formula
from src.services import (
    audit_service,
    formula_service,
    metric_service,
    report_service,
    revision_service,
)
from src.utils.errors import AppError


def list_metrics(db: Session, **filters) -> dict:
    return metric_service.list_metrics(
        db,
        formulas=formula_service.latest_map(db),
        reports_index=report_service.reports_by_metric(db),
        **filters,
    )


def detail(db: Session, metric_id: int) -> dict:
    metric = metric_service.get_metric(db, metric_id)
    formula = formula_service.latest_for(db, metric)
    reports = report_service.reports_by_metric(db).get(metric.code, [])
    metric_payload = metric_service.serialize(
        metric, formula,
        report_names=[report["name"] for report in reports],
        dep_codes=[dep for dep in ((formula.parsed or {}).get("deps") or [])] if formula else [],
    )
    # 版本与依赖图同源证明：当前公式 revision 与指标图 revision 对齐即来自同一次提交
    graph_revision = metric.graph_revision
    formula_revision = formula.revision if formula else None
    graph_snapshot = (formula.graph_snapshot or {}) if formula else {}
    metric_payload.update(
        {
            "formula_revision": formula_revision,
            "graph_revision": graph_revision,
            "revision_aligned": bool(formula_revision) and formula_revision == graph_revision,
            "graph_snapshot": graph_snapshot,
            "graph_source": "same_commit" if formula_revision == graph_revision and formula_revision else "stale",
        }
    )
    drafts_page = revision_service.list_drafts(db, metric_id=metric.id, limit=20)
    return {
        "metric": metric_payload,
        "history": [
            formula_service.serialize(row)
            for row in db.scalars(
                select(Formula).where(Formula.metric_id == metric.id).order_by(Formula.version.desc())
            ).all()
        ],
        "drafts": drafts_page["items"],
    }


def create_metric(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    metric = metric_service.create_metric(db, payload, author=principal["email"])
    if payload.get("dsl_text"):
        formula_service.save_version(
            db, metric, dsl_text=payload["dsl_text"], author_email=principal["email"], note="注册时初始口径"
        )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="metric.create",
        entity="metric",
        entity_id=metric.id,
        detail={"code": metric.code, "name": metric.name, "tier": metric.tier},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    formula = formula_service.latest_for(db, metric)
    return metric_service.serialize(metric, formula)


def update_metric(db: Session, metric_id: int, payload: dict, principal: dict, request: Request) -> dict:
    metric = metric_service.get_metric(db, metric_id)
    metric_service.update_metric(db, metric, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="metric.update",
        entity="metric",
        entity_id=metric.id,
        detail={"fields": sorted(payload.keys())},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return metric_service.serialize(metric, formula_service.latest_for(db, metric))


def parse_dsl(db: Session, metric_id: int, dsl_text: str, principal: dict) -> dict:
    metric = metric_service.get_metric(db, metric_id)
    return formula_service.parse_preview(dsl_text, metric.code)


def save_formula(db: Session, metric_id: int, payload: dict, principal: dict, request: Request) -> dict:
    metric = metric_service.get_metric(db, metric_id)
    dsl_text = payload["dsl_text"]
    parsed = formula_service.parse_or_raise(dsl_text, metric.code)
    base_version = payload.get("base_version")
    if base_version is None:
        raise AppError(
            "VALIDATION",
            422,
            {"field": "base_version", "message": "保存口径必须附带所基于的版本号 base_version"},
        )
    base_version = int(base_version)
    base_formula = formula_service.version_for(db, metric, base_version)
    if base_formula is None:
        raise AppError(
            "VALIDATION",
            422,
            {"field": "base_version", "current_version": metric.current_version, "message": "所基于的版本不存在"},
        )
    head_version = metric.current_version

    # 幂等重放：同来源、无 CR、同签名的正式版本已存在则直接返回，不新增版本/审计
    replay = formula_service.find_replay(
        db, metric, signature=parsed["signature"], source="formula_save", change_request_id=None
    )
    if replay is not None:
        if request is not None:
            request.state.idempotent_replay = True
        return {"replayed": True, "formula": formula_service.serialize(replay)}

    # 过期提交：保留为冲突草案，不覆盖当前口径
    if base_version != head_version:
        idem = payload.get("idempotency_key")
        draft_key = f"draft-m{metric.id}-{idem}" if idem else None
        draft = revision_service.create_conflict_draft(
            db,
            metric=metric,
            base_version=base_version,
            proposed_dsl=dsl_text,
            proposed_parsed=parsed,
            base_formula=base_formula,
            head_formula=formula_service.latest_for(db, metric),
            source="formula_save",
            author_email=principal["email"],
            note=payload.get("note"),
            idempotency_key=draft_key,
        )
        db.flush()
        db.commit()
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "stale_base_version",
                "base_version": base_version,
                "current_version": head_version,
                "draft": revision_service.serialize(draft, metric.code),
                "message": "提交基于过期版本，已保留为冲突草案，未覆盖当前口径",
            },
        )

    formula = formula_service.save_version(
        db,
        metric,
        dsl_text=dsl_text,
        author_email=principal["email"],
        note=payload.get("note"),
        parsed=parsed,
        source="formula_save",
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="formula.save",
        entity="metric",
        entity_id=metric.id,
        detail={
            "code": metric.code,
            "version": formula.version,
            "revision": formula.revision,
            "base_version": base_version,
            "signature": formula.signature,
        },
        ip=request.client.host if request is not None and request.client else None,
    )
    db.commit()
    return {"replayed": False, "formula": formula_service.serialize(formula)}
