-- 002 乐观并发 / 冲突草案 / 版本-依赖图同源（PostgreSQL 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）
-- 背景：两位分析师同时提交同一指标口径时，后一次会悄悄覆盖前一次。
-- 本迁移让提交附带基线版本，过期提交落冲突草案；变更单锁定影响面与依赖图，
-- 环检测通过且审批完成才生成正式版本；正式版本与依赖图用 revision 绑定同源。

-- 1) 冲突草案：过期提交不覆盖任何人，先落草案并记录与 head 的差异
CREATE TABLE formula_drafts (
    id                  SERIAL PRIMARY KEY,
    metric_id           INTEGER      NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    base_version        INTEGER      NOT NULL,          -- 提交人声明所基于的版本
    head_version        INTEGER      NOT NULL,          -- 提交时指标实际最新版本
    proposed_dsl        TEXT         NOT NULL,
    proposed_signature  VARCHAR(64)  NOT NULL DEFAULT '',
    base_signature      VARCHAR(64)  NOT NULL DEFAULT '',
    head_signature      VARCHAR(64)  NOT NULL DEFAULT '',
    parse_status        VARCHAR(12)  NOT NULL DEFAULT 'ok',
    parse_error         TEXT,
    parsed              JSON,
    diff                JSON,                            -- 与 head 的结构化差异
    diff_summary        VARCHAR(300) NOT NULL DEFAULT '',
    status              VARCHAR(16)  NOT NULL DEFAULT 'open',  -- open/resolved/discarded
    source              VARCHAR(20)  NOT NULL DEFAULT 'formula_save',
    change_request_id   INTEGER      REFERENCES change_requests(id) ON DELETE SET NULL,
    author_email        VARCHAR(160) NOT NULL DEFAULT '',
    note                TEXT,
    resolution_note     TEXT,
    idempotency_key     VARCHAR(160) UNIQUE,
    seed_key            VARCHAR(120) UNIQUE,
    created_at          TIMESTAMP    NOT NULL,
    resolved_at         TIMESTAMP
);
CREATE INDEX ix_formula_drafts_metric ON formula_drafts (metric_id);
CREATE INDEX ix_formula_drafts_status ON formula_drafts (status);
CREATE UNIQUE INDEX uq_formula_drafts_idempotency ON formula_drafts (idempotency_key);

-- 2) 变更单：基线版本、锁定的影响面/依赖图快照、环检测结论、生效版本、幂等键
ALTER TABLE change_requests ADD COLUMN base_version INTEGER;
ALTER TABLE change_requests ADD COLUMN impact_graph_snapshot JSON NOT NULL DEFAULT '{}';
ALTER TABLE change_requests ADD COLUMN cycle_check_status VARCHAR(12) NOT NULL DEFAULT 'not_run';
ALTER TABLE change_requests ADD COLUMN cycle_check_detail JSON NOT NULL DEFAULT '{}';
ALTER TABLE change_requests ADD COLUMN applied_version INTEGER;
ALTER TABLE change_requests ADD COLUMN applied_revision VARCHAR(80);
ALTER TABLE change_requests ADD COLUMN idempotency_key VARCHAR(160) UNIQUE;
CREATE INDEX ix_change_requests_base_version ON change_requests (base_version);
CREATE UNIQUE INDEX uq_change_requests_idempotency ON change_requests (idempotency_key);

-- 3) 公式版本：revision 绑定依赖图快照，记录来源变更单
ALTER TABLE formulas ADD COLUMN revision VARCHAR(80);
ALTER TABLE formulas ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'seed';
ALTER TABLE formulas ADD COLUMN change_request_id INTEGER;
ALTER TABLE formulas ADD COLUMN graph_snapshot JSON NOT NULL DEFAULT '{}';
CREATE INDEX ix_formulas_revision ON formulas (revision);
CREATE INDEX ix_formulas_change_request ON formulas (change_request_id);

-- 4) 指标：当前生效依赖图对应的 revision（与当前公式 revision 对齐即同源）
ALTER TABLE metrics ADD COLUMN graph_revision VARCHAR(80);

-- 历史数据回填：把已有公式版本按 (metric_id, version) 生成确定性 revision
UPDATE formulas SET revision = 'rev-' || lpad(metric_id::text, 6, '0') || '-v' || version::text
 WHERE revision IS NULL;
UPDATE metrics SET graph_revision = (
    SELECT f.revision FROM formulas f
    WHERE f.metric_id = metrics.id AND f.version = metrics.current_version
) WHERE graph_revision IS NULL;
