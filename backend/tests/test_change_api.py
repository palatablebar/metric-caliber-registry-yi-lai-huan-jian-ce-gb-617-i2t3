"""端到端 API 测试：基线版本、过期冲突草案、环检测门禁、审批出版本（走真实路由 + 中间件）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.database import get_db
from src.config.app import create_app
from src.models import Base
from src.models.formula import Formula
from src.models.formula_draft import FormulaDraft
from src.services import formula_service, metric_service
from src.utils.security import create_access_token


@pytest.fixture()
def client_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    app = create_app()

    def override_get_db():
        db = session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # 不进入 with 上下文，避免触发 lifespan 里的 wait_for_db / seed（测试用内存库）
    client = TestClient(app)
    yield client, session


def _headers(email: str, role: str) -> dict:
    token, _ = create_access_token(email, role)
    return {"Authorization": f"Bearer {token}"}


ANALYST = _headers("analyst@x.io", "analyst")
ADMIN = _headers("admin@x.io", "admin")

DSL1 = "e2e = sum(dwd.t.amount) where status = 'paid' group by dt"
DSL2 = "e2e = sum(dwd.t.amount) where status = 'paid' and refund = 0 group by dt"
DSL3 = "e2e = count(dwd.t.id) where status = 'paid' group by dt"


def _seed_metric(session_factory):
    with session_factory() as db:
        metric = metric_service.create_metric(db, {"code": "e2e", "name": "E2E指标"}, author="seed@x.io")
        formula_service.save_version(db, metric, dsl_text=DSL1, author_email="seed@x.io", source="seed")
        db.commit()
        return metric.id


def test_formula_save_requires_base_version(client_db):
    client, session_factory = client_db
    metric_id = _seed_metric(session_factory)
    resp = client.post(f"/api/metrics/{metric_id}/formulas", headers=ANALYST, json={"dsl_text": DSL2})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION"


def test_stale_submission_returns_conflict_draft_and_keeps_head(client_db):
    client, session_factory = client_db
    metric_id = _seed_metric(session_factory)
    # 先把 head 推进到 v2
    ok = client.post(
        f"/api/metrics/{metric_id}/formulas",
        headers=ANALYST, json={"dsl_text": DSL2, "base_version": 1, "idempotency_key": "c1"},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["formula"]["version"] == 2
    # 基于过期 v1 再提交 -> 409 冲突草案
    stale = client.post(
        f"/api/metrics/{metric_id}/formulas",
        headers=ANALYST, json={"dsl_text": DSL3, "base_version": 1, "idempotency_key": "c2"},
    )
    assert stale.status_code == 409
    payload = stale.json()
    assert payload["code"] == "CONFLICT"
    assert payload["detail"]["draft"]["status"] == "open"
    assert payload["detail"]["draft"]["stale"] is True
    # head 仍是 v2
    with session_factory() as db:
        metric = metric_service.get_metric(db, metric_id)
        assert metric.current_version == 2
        assert formula_service.latest_for(db, metric).dsl_text == DSL2
        drafts = db.scalars(select(FormulaDraft)).all()
        assert len(drafts) == 1 and drafts[0].diff_summary


def test_change_request_approval_gate_and_version(client_db):
    client, session_factory = client_db
    metric_id = _seed_metric(session_factory)
    # 提交变更单（带基线）
    cr = client.post(
        "/api/change-requests", headers=ANALYST,
        json={"change_kind": "caliber", "target_ref": "e2e", "metric_code": "e2e",
              "proposed_dsl": DSL2, "base_version": 1},
    )
    assert cr.status_code == 201, cr.text
    cr_body = cr.json()
    assert cr_body["cycle_check_status"] == "passed"
    assert cr_body["impact_graph_snapshot"]["locked_at_version"] == 1
    cr_id = cr_body["id"]
    # analyst 无权批准 -> 403
    forbidden = client.patch(f"/api/change-requests/{cr_id}", headers=ANALYST, json={"status": "approved"})
    assert forbidden.status_code == 403
    # admin 批准 -> 生成 v2
    approved = client.patch(f"/api/change-requests/{cr_id}", headers=ADMIN, json={"status": "approved", "note": "同意"})
    assert approved.status_code == 200, approved.text
    ab = approved.json()
    assert ab["applied_version"] == 2 and ab["applied_revision"]
    # 详情里版本与依赖图同源
    detail = client.get(f"/api/metrics/{metric_id}", headers=ADMIN).json()
    assert detail["metric"]["current_version"] == 2
    assert detail["metric"]["revision_aligned"] is True
    assert detail["metric"]["formula_revision"] == detail["metric"]["graph_revision"]
    # 重放批准：不新增版本，且 controller 标记幂等重放（HTTP 审计也会跳过）
    replay = client.patch(f"/api/change-requests/{cr_id}", headers=ADMIN, json={"status": "approved"})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    with session_factory() as db:
        assert len(db.scalars(select(Formula).where(Formula.metric_id == metric_id)).all()) == 2
        # 领域审计只有一次批准记录（重放未新增）
        from src.models.audit import AuditLog

        approve_audits = db.scalars(
            select(AuditLog).where(AuditLog.action == "change.approve")
        ).all()
        assert len(approve_audits) == 1


def test_cycle_change_cannot_be_approved(client_db):
    client, session_factory = client_db
    with session_factory() as db:
        a = metric_service.create_metric(db, {"code": "ca", "name": "CA"}, author="seed@x.io")
        b = metric_service.create_metric(db, {"code": "cb", "name": "CB"}, author="seed@x.io")
        formula_service.save_version(db, a, dsl_text="ca = sum(dwd.t.x) group by dt", author_email="s@x.io", source="seed")
        formula_service.save_version(db, b, dsl_text="cb = sum(@ca) group by dt", author_email="s@x.io", source="seed")
        db.commit()
        a_id = a.id
    cr = client.post(
        "/api/change-requests", headers=ANALYST,
        json={"change_kind": "formula", "target_ref": "ca", "metric_code": "ca",
              "proposed_dsl": "ca = sum(@cb) group by dt", "base_version": 1},
    )
    assert cr.json()["cycle_check_status"] == "failed"
    cr_id = cr.json()["id"]
    resp = client.patch(f"/api/change-requests/{cr_id}", headers=ADMIN, json={"status": "approved"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "cycle"
    with session_factory() as db:
        metric = metric_service.get_metric(db, a_id)
        assert metric.current_version == 1
        assert len(db.scalars(select(Formula).where(Formula.metric_id == a_id)).all()) == 1
