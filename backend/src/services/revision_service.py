"""乐观并发与冲突草案：revision 键、口径结构差异、过期提交落草案。

设计要点：
- 任何口径提交都必须带 base_version（所基于的版本），与当前版本不一致即为过期提交；
- 过期提交不覆盖任何人，落 FormulaDraft（status=open）并附与 head 的结构化差异；
- 草案带 idempotency_key，同一提交重放只返回原草案，不新增记录。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CYCLE_CHECK_LABELS,
    DRAFT_STATUS_LABELS,
    DraftStatus,
)
from src.models.change_request import ChangeRequest
from src.models.formula import Formula
from src.models.formula_draft import FormulaDraft
from src.models.metric import Metric
from src.utils.timeutil import iso_utc, utcnow

_DIFF_KEYS = ("aggs", "fields", "filters", "dims", "deps")


def revision_key(metric_id: int, version: int) -> str:
    """口径版本与依赖图同源的绑定键：同一 revision 即同一次提交。"""
    return f"rev-{int(metric_id):06d}-v{int(version)}"


def graph_snapshot_from_parsed(parsed: dict, *, version: int, signature: str) -> dict:
    return {
        "version": version,
        "signature": signature,
        "aggs": list(parsed.get("aggs") or []),
        "fields": list(parsed.get("fields") or []),
        "filters": list(parsed.get("filters") or []),
        "dims": list(parsed.get("dims") or []),
        "deps": list(parsed.get("deps") or []),
        "expression": parsed.get("expression") or "",
    }


def _set_diff(left: list[str], right: list[str]) -> dict:
    left_set, right_set = set(left), set(right)
    return {
        "base": sorted(left_set),
        "head": sorted(right_set),
        "only_in_base": sorted(left_set - right_set),
        "only_in_head": sorted(right_set - left_set),
    }


def diff_parsed(base_parsed: dict, head_parsed: dict) -> dict:
    """两份解析结果的结构化差异；空 dict 表示口径完全一致。"""
    diff = {key: _set_diff(base_parsed.get(key) or [], head_parsed.get(key) or []) for key in _DIFF_KEYS}
    diff["expression_changed"] = (base_parsed.get("expression") or "") != (head_parsed.get("expression") or "")
    diff["signature_equal"] = (base_parsed.get("signature") or "") == (head_parsed.get("signature") or "")
    return diff


def summarize_diff(diff: dict) -> str:
    labels = {
        "aggs": "聚合方式",
        "fields": "字段集",
        "filters": "过滤条件",
        "dims": "维度",
        "deps": "引用指标",
    }
    changed = [labels[key] for key in _DIFF_KEYS if diff.get(key, {}).get("only_in_base") or diff.get(key, {}).get("only_in_head")]
    if not changed and diff.get("expression_changed"):
        changed.append("表达式写法")
    if not changed:
        return "口径内容一致，仅提交基线过期"
    return "差异：" + " / ".join(changed)


def find_replay(db: Session, idempotency_key: str | None) -> FormulaDraft | None:
    if not idempotency_key:
        return None
    return db.scalar(select(FormulaDraft).where(FormulaDraft.idempotency_key == idempotency_key))


def create_conflict_draft(
    db: Session,
    *,
    metric: Metric,
    base_version: int,
    proposed_dsl: str,
    proposed_parsed: dict,
    base_formula: Formula | None,
    head_formula: Formula | None,
    source: str,
    author_email: str,
    note: str | None,
    change_request: ChangeRequest | None = None,
    idempotency_key: str | None = None,
    seed_key: str | None = None,
) -> FormulaDraft:
    """把过期提交保存为冲突草案，并计算与 head 口径的差异。"""
    replay = find_replay(db, idempotency_key)
    if replay is not None:
        return replay
    base_parsed = (base_formula.parsed or {}) if base_formula else {}
    head_parsed = (head_formula.parsed or {}) if head_formula else {}
    diff = diff_parsed(proposed_parsed, head_parsed)
    draft = FormulaDraft(
        metric_id=metric.id,
        base_version=base_version,
        head_version=metric.current_version,
        proposed_dsl=proposed_dsl.strip(),
        proposed_signature=proposed_parsed.get("signature") or "",
        base_signature=(base_formula.signature if base_formula else "") or "",
        head_signature=(head_formula.signature if head_formula else "") or "",
        parse_status=proposed_parsed.get("status") or "ok",
        parse_error=None,
        parsed=proposed_parsed,
        diff=diff,
        diff_summary=summarize_diff(diff),
        status=DraftStatus.OPEN.value,
        source=source,
        change_request_id=change_request.id if change_request else None,
        author_email=author_email,
        note=note,
        idempotency_key=idempotency_key,
        seed_key=seed_key,
    )
    db.add(draft)
    db.flush()
    return draft


def serialize(draft: FormulaDraft, metric_code: str | None = None, cr_no: str | None = None) -> dict:
    return {
        "id": draft.id,
        "metric_id": draft.metric_id,
        "metric_code": metric_code,
        "base_version": draft.base_version,
        "head_version": draft.head_version,
        "stale": draft.base_version != draft.head_version,
        "proposed_dsl": draft.proposed_dsl,
        "proposed_signature": draft.proposed_signature,
        "base_signature": draft.base_signature,
        "head_signature": draft.head_signature,
        "parse_status": draft.parse_status,
        "parse_error": draft.parse_error,
        "diff": draft.diff or {},
        "diff_summary": draft.diff_summary,
        "status": draft.status,
        "status_label": DRAFT_STATUS_LABELS.get(draft.status, draft.status),
        "source": draft.source,
        "change_request_id": draft.change_request_id,
        "cr_no": cr_no,
        "author_email": draft.author_email,
        "note": draft.note,
        "resolution_note": draft.resolution_note,
        "created_at": iso_utc(draft.created_at),
        "resolved_at": iso_utc(draft.resolved_at),
    }


def list_drafts(
    db: Session,
    *,
    status: str | None = None,
    metric_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    stmt = select(FormulaDraft)
    count_stmt = select(func.count()).select_from(FormulaDraft)
    if status:
        stmt = stmt.where(FormulaDraft.status == status)
        count_stmt = count_stmt.where(FormulaDraft.status == status)
    if metric_id is not None:
        stmt = stmt.where(FormulaDraft.metric_id == metric_id)
        count_stmt = count_stmt.where(FormulaDraft.metric_id == metric_id)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(FormulaDraft.id.desc()).limit(limit).offset(offset)).all()
    metric_codes = {metric.id: metric.code for metric in db.scalars(select(Metric)).all()}
    cr_nos = {cr.id: cr.cr_no for cr in db.scalars(select(ChangeRequest)).all()}
    return {
        "items": [
            serialize(
                row,
                metric_codes.get(row.metric_id),
                cr_nos.get(row.change_request_id) if row.change_request_id else None,
            )
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_draft(db: Session, draft_id: int) -> FormulaDraft:
    draft = db.get(FormulaDraft, draft_id)
    if draft is None:
        from src.utils.errors import not_found

        raise not_found("formula_draft", draft_id)
    return draft


def resolve_draft(db: Session, draft: FormulaDraft, *, resolution_note: str | None) -> FormulaDraft:
    draft.status = DraftStatus.RESOLVED.value
    draft.resolution_note = resolution_note
    draft.resolved_at = utcnow()
    db.flush()
    return draft


def discard_draft(db: Session, draft: FormulaDraft, *, resolution_note: str | None) -> FormulaDraft:
    draft.status = DraftStatus.DISCARDED.value
    draft.resolution_note = resolution_note
    draft.resolved_at = utcnow()
    db.flush()
    return draft


def cycle_check_label(status: str) -> str:
    return CYCLE_CHECK_LABELS.get(status, status)
