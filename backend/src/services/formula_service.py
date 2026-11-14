"""口径公式服务：版本读取、算法 A 预解析、保存新版本。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import ParseStatus
from src.models.formula import Formula
from src.models.metric import Metric
from src.services import dsl_parser, revision_service
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc


def serialize(formula: Formula) -> dict:
    return {
        "id": formula.id,
        "metric_id": formula.metric_id,
        "version": formula.version,
        "dsl_text": formula.dsl_text,
        "parse_status": formula.parse_status,
        "parse_error": formula.parse_error,
        "parsed": formula.parsed or {},
        "signature": formula.signature,
        "author_email": formula.author_email,
        "note": formula.note,
        "revision": formula.revision,
        "source": formula.source,
        "change_request_id": formula.change_request_id,
        "graph_snapshot": formula.graph_snapshot or {},
        "created_at": iso_utc(formula.created_at),
    }


def latest_map(db: Session) -> dict[int, Formula]:
    """每个指标取版本号最大的一条公式。"""
    rows = db.scalars(select(Formula).order_by(Formula.metric_id, Formula.version.desc())).all()
    latest: dict[int, Formula] = {}
    for row in rows:
        latest.setdefault(row.metric_id, row)
    return latest


def latest_for(db: Session, metric: Metric) -> Formula | None:
    return db.scalar(
        select(Formula)
        .where(Formula.metric_id == metric.id)
        .order_by(Formula.version.desc())
        .limit(1)
    )


def parse_preview(dsl_text: str, metric_code: str | None = None) -> dict:
    return dsl_parser.parse_dsl(dsl_text, metric_code)


def apply_parse(formula: Formula, parsed: dict) -> Formula:
    formula.parsed = {
        key: parsed.get(key)
        for key in ("name", "aggs", "fields", "filters", "dims", "deps", "expression", "where_text", "error")
    }
    if parsed["status"] == "ok":
        formula.parse_status = ParseStatus.OK.value
        formula.parse_error = None
        formula.signature = parsed["signature"]
    else:
        formula.parse_status = ParseStatus.ERROR.value
        error = parsed.get("error") or {}
        formula.parse_error = f"{error.get('code')} @{error.get('column')}: {error.get('message')}"
        formula.signature = ""
    return formula


def parse_or_raise(dsl_text: str, metric_code: str | None = None) -> dict:
    parsed = parse_preview(dsl_text, metric_code)
    if parsed["status"] != "ok":
        error = parsed["error"]
        raise AppError(
            "VALIDATION",
            422,
            {
                "code": error["code"],
                "message": error["message"],
                "position": error["position"],
                "column": error["column"],
                "snippet": error["snippet"],
            },
            message=f"DSL 解析失败：{error['message']}",
        )
    return parsed


def find_replay(
    db: Session,
    metric: Metric,
    *,
    signature: str,
    source: str,
    change_request_id: int | None,
) -> Formula | None:
    """同一提交（同来源 + 同 CR + 同签名）重放：返回已生成的正式版本，不再新增。"""
    stmt = select(Formula).where(
        Formula.metric_id == metric.id,
        Formula.signature == signature,
        Formula.source == source,
    )
    if change_request_id is None:
        stmt = stmt.where(Formula.change_request_id.is_(None))
    else:
        stmt = stmt.where(Formula.change_request_id == change_request_id)
    return db.scalar(stmt.order_by(Formula.version.desc()).limit(1))


def version_for(db: Session, metric: Metric, version: int) -> Formula | None:
    return db.scalar(
        select(Formula).where(Formula.metric_id == metric.id, Formula.version == version).limit(1)
    )


def save_version(
    db: Session,
    metric: Metric,
    *,
    dsl_text: str,
    author_email: str,
    note: str | None = None,
    seed_key: str | None = None,
    parsed: dict | None = None,
    source: str = "formula_save",
    change_request_id: int | None = None,
) -> Formula:
    parsed = parsed or parse_or_raise(dsl_text, metric.code)
    next_version = (
        db.scalar(select(func.max(Formula.version)).where(Formula.metric_id == metric.id)) or 0
    ) + 1
    revision = revision_service.revision_key(metric.id, next_version)
    formula = Formula(
        metric_id=metric.id,
        version=next_version,
        dsl_text=dsl_text.strip(),
        author_email=author_email,
        note=note,
        revision=revision,
        source=source if not seed_key else "seed",
        change_request_id=change_request_id,
        graph_snapshot=revision_service.graph_snapshot_from_parsed(
            parsed, version=next_version, signature=parsed["signature"]
        ),
        seed_key=seed_key,
    )
    apply_parse(formula, parsed)
    db.add(formula)
    metric.current_version = next_version
    metric.graph_revision = revision
    db.flush()
    return formula


def version_count(db: Session, metric_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Formula).where(Formula.metric_id == metric_id)) or 0
