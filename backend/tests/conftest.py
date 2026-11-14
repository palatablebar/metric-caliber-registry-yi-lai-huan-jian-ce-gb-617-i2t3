"""测试夹具：SQLite 内存库 + 最小指标/公式/报表装配（与 ORM 解耦的纯服务层测试）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.services import formula_service, metric_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_metric(db, code: str, name: str = "测试指标", dsl: str | None = None, author: str = "seed@x.io"):
    metric = metric_service.create_metric(
        db,
        {"code": code, "name": name, "domain": "trade", "tier": "normal", "unit": "元"},
        author=author,
    )
    if dsl:
        formula_service.save_version(db, metric, dsl_text=dsl, author_email=author, note="init", source="seed")
    db.flush()
    return metric


@pytest.fixture()
def make_metric_factory(db):
    def _make(code: str, name: str = "测试指标", dsl: str | None = None, author: str = "seed@x.io"):
        return make_metric(db, code, name, dsl, author)

    return _make
