"""乐观并发 / 冲突草案 / 变更单锁定影响面 / 环检测门禁 / 幂等 的服务层测试。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.constants.enums import ChangeStatus, DraftStatus
from src.models.audit import AuditLog
from src.models.change_request import ChangeRequest
from src.models.formula import Formula
from src.models.formula_draft import FormulaDraft
from src.models.report import Report
from src.services import change_service, formula_service, revision_service
from src.utils.errors import AppError

from tests.conftest import make_metric

DSL_V1 = "m = sum(dwd.order.amount) where order_status = 'paid' group by dt"
DSL_V2 = "m = sum(dwd.order.amount) where order_status = 'paid' and refund_status = 'done' group by dt"
DSL_V3 = "m = count(dwd.order.id) where order_status = 'paid' group by dt"


def _audit_count(db, action: str | None = None) -> int:
    stmt = select(func.count()).select_from(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return db.scalar(stmt) or 0


# ---------- 1. 提交附带基线版本：过期提交不覆盖，落冲突草案并说明差异 ----------

def test_stale_formula_save_becomes_conflict_draft_without_overwrite(db):
    metric = make_metric(db, "m1", dsl=DSL_V1)
    v1 = metric.current_version
    # 分析师 B 在 v1 基础上推进到 v2
    formula_service.save_version(db, metric, dsl_text=DSL_V2, author_email="b@x.io", source="formula_save")
    assert metric.current_version == v1 + 1

    # 分析师 A 仍基于 v1 提交
    parsed = formula_service.parse_or_raise(DSL_V3, metric.code)
    base = formula_service.version_for(db, metric, v1)
    head = formula_service.latest_for(db, metric)
    draft = revision_service.create_conflict_draft(
        db,
        metric=metric,
        base_version=v1,
        proposed_dsl=DSL_V3,
        proposed_parsed=parsed,
        base_formula=base,
        head_formula=head,
        source="formula_save",
        author_email="a@x.io",
        note="stale",
    )
    db.flush()

    # 当前口径仍是 v2，没有被覆盖、没有新增正式版本
    assert metric.current_version == v1 + 1
    assert formula_service.latest_for(db, metric).dsl_text == DSL_V2
    # 草案保留且写明差异
    assert draft.status == DraftStatus.OPEN.value
    assert draft.base_version == v1 and draft.head_version == v1 + 1
    assert draft.diff["aggs"]["only_in_base"] or draft.diff["fields"]["only_in_base"]
    assert draft.diff_summary
    stored = db.scalar(select(FormulaDraft).where(FormulaDraft.id == draft.id))
    assert stored.proposed_dsl == DSL_V3


def test_conflict_draft_idempotent_replay_returns_same_draft(db):
    metric = make_metric(db, "m2", dsl=DSL_V1)
    formula_service.save_version(db, metric, dsl_text=DSL_V2, author_email="b@x.io", source="formula_save")
    parsed = formula_service.parse_or_raise(DSL_V3, metric.code)
    kwargs = dict(
        metric=metric,
        base_version=1,
        proposed_dsl=DSL_V3,
        proposed_parsed=parsed,
        base_formula=formula_service.version_for(db, metric, 1),
        head_formula=formula_service.latest_for(db, metric),
        source="formula_save",
        author_email="a@x.io",
        note="x",
        idempotency_key="k-1",
    )
    first = revision_service.create_conflict_draft(db, **kwargs)
    second = revision_service.create_conflict_draft(db, **kwargs)
    assert first.id == second.id
    assert db.scalar(select(func.count()).select_from(FormulaDraft)) == 1


def test_formula_save_requires_base_version_via_controller(db):
    from src.controllers import metric_controller

    metric = make_metric(db, "m3", dsl=DSL_V1)
    with pytest.raises(AppError) as exc:
        metric_controller.save_formula(
            db, metric.id, {"dsl_text": DSL_V2, "base_version": None},
            {"email": "a@x.io", "role": "analyst"}, request=None,
        )
    assert exc.value.code == "VALIDATION"


def test_formula_save_stale_returns_conflict_and_keeps_head(db):
    from src.controllers import metric_controller

    metric = make_metric(db, "m4", dsl=DSL_V1)
    formula_service.save_version(db, metric, dsl_text=DSL_V2, author_email="b@x.io", source="formula_save")
    head_before = formula_service.latest_for(db, metric).id
    with pytest.raises(AppError) as exc:
        metric_controller.save_formula(
            db, metric.id, {"dsl_text": DSL_V3, "base_version": 1, "idempotency_key": "f-1"},
            {"email": "a@x.io", "role": "analyst"}, request=None,
        )
    assert exc.value.code == "CONFLICT"
    assert exc.value.detail["draft"]["status"] == "open"
    assert formula_service.latest_for(db, metric).id == head_before
    # 重放同一提交不新增草案
    with pytest.raises(AppError) as again_exc:
        metric_controller.save_formula(
            db, metric.id, {"dsl_text": DSL_V3, "base_version": 1, "idempotency_key": "f-1"},
            {"email": "a@x.io", "role": "analyst"}, request=None,
        )
    assert again_exc.value.detail["draft"]["id"] == exc.value.detail["draft"]["id"]


# ---------- 2. 变更单锁定提交时影响面；环检测通过+审批才生成正式版本 ----------

def _add_report(db, code: str, metric_code: str, core: bool = False):
    db.add(
        Report(
            code=code, name=f"报表{code}", owner="o", channel="BI",
            is_core=core, weight=3 if core else 1, metric_codes=[metric_code],
        )
    )
    db.flush()


def test_change_request_locks_impact_and_cycle_result(db):
    metric = make_metric(db, "gmv_x", name="GMV X", dsl=DSL_V1)
    _add_report(db, "r01", "gmv_x", core=True)
    change = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_x", "metric_code": "gmv_x",
         "proposed_dsl": DSL_V2, "base_version": 1, "reason": "月会"},
        requester="analyst@x.io",
    )
    assert change.base_version == 1
    # 影响面被锁定为快照
    assert change.impact_graph_snapshot["locked_at_version"] == 1
    assert change.impact["report_count"] == 1 and change.impact_score == 3
    # 提交时已跑环检测
    assert change.cycle_check_status == "passed"
    # 待审批阶段没有正式版本
    assert change.applied_version is None
    assert formula_service.latest_for(db, metric).version == 1


def test_approval_generates_version_with_revision_and_graph_snapshot(db):
    metric = make_metric(db, "gmv_y", name="GMV Y", dsl=DSL_V1)
    change = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_y", "metric_code": "gmv_y",
         "proposed_dsl": DSL_V2, "base_version": 1},
        requester="analyst@x.io",
    )
    change_service.decide(db, change, status=ChangeStatus.APPROVED.value, note="ok", approver="admin@x.io")
    formula = formula_service.latest_for(db, metric)
    assert formula.version == 2
    assert formula.source == "change_request" and formula.change_request_id == change.id
    # revision 与依赖图快照同源绑定
    assert formula.revision == revision_service.revision_key(metric.id, 2)
    assert formula.graph_snapshot["version"] == 2
    assert metric.graph_revision == formula.revision
    assert change.applied_version == 2 and change.applied_revision == formula.revision


def test_approval_blocked_by_cycle_keeps_original_formula_and_graph(db):
    # 构造 a -> b 无环；让 a 的新口径引用 b，b 再引用 a 形成环
    a = make_metric(db, "cyc_a", name="A", dsl="cyc_a = sum(dwd.t.x) group by dt")
    b = make_metric(db, "cyc_b", name="B", dsl="cyc_b = sum(@cyc_a) group by dt")
    change = change_service.create_change(
        db,
        {"change_kind": "formula", "target_ref": "cyc_a", "metric_code": "cyc_a",
         "proposed_dsl": "cyc_a = sum(@cyc_b) group by dt", "base_version": 1},
        requester="analyst@x.io",
    )
    # 提交时环检测已失败并锁定
    assert change.cycle_check_status == "failed"
    assert change.cycle_check_detail["cycles"]
    before_version = a.current_version
    before_dsl = formula_service.latest_for(db, a).dsl_text
    with pytest.raises(AppError) as exc:
        change_service.decide(db, change, status="approved", note="try", approver="admin@x.io")
    assert exc.value.code == "CONFLICT"
    # 原口径与版本号保留，没有生成正式版本（拦截发生在 save_version 之前，无脏数据）
    assert a.current_version == before_version
    assert formula_service.latest_for(db, a).dsl_text == before_dsl
    assert db.scalar(select(func.count()).select_from(Formula).where(Formula.metric_id == a.id)) == 1
    # 变更单仍是待审批
    assert db.get(ChangeRequest, change.id).status == ChangeStatus.PENDING.value


def test_approval_rejected_keeps_original_caliber(db):
    metric = make_metric(db, "gmv_z", name="GMV Z", dsl=DSL_V1)
    change = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_z", "metric_code": "gmv_z",
         "proposed_dsl": DSL_V2, "base_version": 1},
        requester="analyst@x.io",
    )
    change_service.decide(db, change, status="rejected", note="不同意", approver="admin@x.io")
    assert metric.current_version == 1
    assert formula_service.latest_for(db, metric).dsl_text == DSL_V1
    assert change.applied_version is None


def test_approving_stale_change_is_rejected_and_keeps_draft(db):
    metric = make_metric(db, "gmv_w", name="GMV W", dsl=DSL_V1)
    change = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_w", "metric_code": "gmv_w",
         "proposed_dsl": DSL_V2, "base_version": 1},
        requester="analyst@x.io",
    )
    # 别人在审批前把指标推进到 v2
    formula_service.save_version(db, metric, dsl_text=DSL_V3, author_email="other@x.io", source="formula_save")
    with pytest.raises(AppError) as exc:
        change_service.decide(db, change, status="approved", note="late", approver="admin@x.io")
    assert exc.value.code == "CONFLICT"
    assert change.status == ChangeStatus.PENDING.value
    assert metric.current_version == 2


# ---------- 3. 重跑同一变更不新增版本或审计 ----------

def test_change_create_replay_returns_same_request_no_duplicate(db):
    make_metric(db, "gmv_r", name="GMV R", dsl=DSL_V1)
    payload = {"change_kind": "caliber", "target_ref": "gmv_r", "metric_code": "gmv_r",
               "proposed_dsl": DSL_V2, "base_version": 1}
    first = change_service.create_change(db, payload, requester="analyst@x.io")
    second = change_service.create_change(db, payload, requester="analyst@x.io")
    assert first.id == second.id
    assert db.scalar(select(func.count()).select_from(ChangeRequest)) == 1


def test_second_pending_change_on_same_target_is_conflict_409(db):
    make_metric(db, "gmv_t", name="GMV T", dsl=DSL_V1)
    change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_t", "metric_code": "gmv_t",
         "proposed_dsl": DSL_V2, "base_version": 1},
        requester="a@x.io",
    )
    with pytest.raises(AppError) as exc:
        change_service.create_change(
            db,
            {"change_kind": "caliber", "target_ref": "gmv_t", "metric_code": "gmv_t",
             "proposed_dsl": DSL_V3, "base_version": 1},
            requester="b@x.io",
        )
    assert exc.value.code == "CONFLICT"


def test_stale_change_after_approval_becomes_draft(db):
    metric = make_metric(db, "gmv_u", name="GMV U", dsl=DSL_V1)
    first = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_u", "metric_code": "gmv_u",
         "proposed_dsl": DSL_V2, "base_version": 1},
        requester="a@x.io",
    )
    change_service.decide(db, first, status="approved", note="ok", approver="admin@x.io")
    assert metric.current_version == 2
    # 另一人基于旧 v1 再提变更单 -> 过期，落草案，不新增待审批竞争单
    stale = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_u", "metric_code": "gmv_u",
         "proposed_dsl": DSL_V3, "base_version": 1},
        requester="b@x.io",
    )
    assert getattr(stale, "conflict_draft_created", False) is True
    draft = db.scalar(select(FormulaDraft).where(FormulaDraft.change_request_id == stale.id))
    assert draft is not None and draft.status == DraftStatus.OPEN.value
    assert draft.base_version == 1 and draft.head_version == 2
    # 当前口径仍是已批准的 v2
    assert formula_service.latest_for(db, metric).version == 2


def test_repeated_decision_does_not_add_version_or_audit(db):
    metric = make_metric(db, "gmv_p", name="GMV P", dsl=DSL_V1)
    change = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv_p", "metric_code": "gmv_p",
         "proposed_dsl": DSL_V2, "base_version": 1},
        requester="analyst@x.io",
    )
    change_service.decide(db, change, status="approved", note="ok", approver="admin@x.io")
    db.add(AuditLog(actor_email="admin@x.io", actor_role="admin", action="change.approve",
                    entity="change_request", entity_id=str(change.id), detail={}))
    db.flush()
    audits_before = _audit_count(db, "change.approve")
    # 再次决策同一张已审批单：重放，不抛错、不新增版本
    again = change_service.decide(db, change, status="approved", note="again", approver="admin@x.io")
    assert getattr(again, "replayed", False) is True
    assert metric.current_version == 2
    assert db.scalar(select(func.count()).select_from(Formula).where(Formula.metric_id == metric.id)) == 2
    assert _audit_count(db, "change.approve") == audits_before


def test_formula_save_replay_does_not_add_version(db):
    from src.controllers import metric_controller

    metric = make_metric(db, "gmv_q", name="GMV Q", dsl=DSL_V1)
    payload = {"dsl_text": DSL_V2, "base_version": 1, "idempotency_key": "save-1"}
    principal = {"email": "a@x.io", "role": "analyst"}
    first = metric_controller.save_formula(db, metric.id, payload, principal, request=None)
    second = metric_controller.save_formula(db, metric.id, payload, principal, request=None)
    assert first["formula"]["version"] == second["formula"]["version"] == 2
    assert second["replayed"] is True
    assert db.scalar(select(func.count()).select_from(Formula).where(Formula.metric_id == metric.id)) == 2
    # 重放不写新审计
    assert _audit_count(db, "formula.save") == 1

# ---------- 4. 指标详情可证明当前版本与依赖图来自同一次提交 ----------

def test_revision_alignment_reflects_same_commit(db):
    metric = make_metric(db, "gmv_s", name="GMV S", dsl=DSL_V1)
    formula = formula_service.latest_for(db, metric)
    # seed 保存即盖章，二者同源
    assert metric.graph_revision == formula.revision
    aligned = metric.graph_revision == formula.revision and bool(formula.revision)
    assert aligned is True
    # 模拟依赖图尚未重建到最新提交 -> 不再同源
    metric.graph_revision = "rev-old"
    assert (formula.revision == metric.graph_revision) is False
