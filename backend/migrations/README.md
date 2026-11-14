# migrations

参考建表语句。运行时由 SQLAlchemy 的 `Base.metadata.create_all` 建表（启动时执行），
本目录的 SQL 只用于评审时快速看清表结构与自然键/唯一约束，**不会被执行**。

- 5 个业务实体：`metrics` / `formulas` / `dependencies` / `reports` / `change_requests`
- 并发治理实体：`formula_drafts`（过期提交的冲突草案，见 `002_optimistic_concurrency.sql`）
- 横切：`users`（三角色）、`audit_logs`（审计）、`runs`（校验运行）
- 自然键：`metrics.code`、`formulas(metric_id, version)`、`dependencies(from_metric_id, kind, to_ref)`、
  `reports.code`、`change_requests.cr_no`、`runs.run_no`
- 幂等键：`formula_drafts.idempotency_key`、`change_requests.idempotency_key`（重跑同一变更不新增版本/草案/审计）

## 002 乐观并发（评审要点）

- 提交必须带 `base_version`；与 `metrics.current_version` 不一致即为过期提交，落 `formula_drafts` 并附结构化差异，不覆盖当前口径。
- `change_requests` 增加 `base_version` / `impact_graph_snapshot`（锁定影响面与依赖图）/
  `cycle_check_status`（环检测结论）/ `applied_version` / `applied_revision` / `idempotency_key`。
- `formulas` 增加 `revision` / `graph_snapshot` / `source` / `change_request_id`；
  `metrics` 增加 `graph_revision`，二者同值即「当前版本与依赖图来自同一次提交」。
