import { api } from '../api.js';
import { ENUMS, badgeTone, can, changeTone } from '../config.js';
import { drawer, emptyState, errorBox, errorMessage, esc, field, filterBar, fmtTime, impactRows, kv, table, toast } from '../ui.js';

const state = { status: '' };

const EXPORTS = [
  ['/api/exports/metrics.csv', 'metrics.csv', '指标清单', '48 条口径 + 解析结果 + 下游报表'],
  ['/api/exports/conflicts.csv', 'conflicts.csv', '冲突与影响面', '同名冲突、差异字段、严重度'],
  ['/api/exports/change-requests.csv', 'change-requests.csv', '变更单', '影响面与审批结论'],
  ['/api/exports/runs.csv', 'runs.csv', '校验运行记录', '解析失败、环、冲突统计'],
];

function openChange(view, ctx, change) {
  const approver = can(ctx.user.role, 'change:approve');
  const impact = change.impact || {};
  const locked = change.impact_graph_snapshot || {};
  const cycleTone = change.cycle_check_status === 'failed' ? 'danger' : change.cycle_check_status === 'passed' ? 'ok' : 'info';
  const { root, close } = drawer({
    title: `${change.cr_no} · ${change.change_kind_label}`,
    subtitle: `${change.target_ref} · 状态 ${change.status_label}`,
    body: `
      ${kv([
        ['目标', `<span class="mono">${esc(change.target_ref)}</span>`],
        ['类型', esc(change.change_kind_label)],
        ['基线版本', change.base_version ? `<span class="mono">v${esc(change.base_version)}</span>` : '—'],
        ['严重度', `<span class="badge ${badgeTone(change.severity)}">${esc(ENUMS.severity[change.severity])}</span>`],
        ['影响分', `<span class="mono">${esc(change.impact_score)}</span>（核心报表 ×3）`],
        ['环检测', `<span class="badge ${cycleTone}">${esc(change.cycle_check_label || change.cycle_check_status)}</span>`],
        ['影响锁定', locked.locked_at_version ? `提交时 v${esc(locked.locked_at_version)} 的影响面与依赖图已锁定` : '—'],
        ['影响指标', `${esc(impact.metric_count ?? (impact.metric_codes || []).length)} 个`],
        ['影响报表', `${esc(impact.report_count || 0)} 张`],
        ['生效版本', change.applied_version ? `<span class="mono">v${esc(change.applied_version)} · ${esc(change.applied_revision || '')}</span>` : '未生成（审批通过后才出版本）'],
        ['申请人', esc(change.requester_email)],
        ['申请时间', fmtTime(change.created_at)],
        ['审批人', esc(change.approver_email || '—')],
        ['审批结论', esc(change.decision_note || '—')],
        ['决定时间', fmtTime(change.decided_at)],
      ])}
      ${
        change.cycle_check_status === 'failed'
          ? `<div class="dsl-box"><span class="label">环检测拦截明细</span>
              <div class="list-tight">${(change.cycle_check_detail?.cycles || [])
                .map((c) => `<div class="impact-row"><span class="mono">${esc((c.path || []).join(' → '))}</span><span class="muted tiny">长度 ${esc(c.length)}</span></div>`)
                .join('')}</div>
              <p class="section-note tiny">该口径会引入依赖环，批准将被拦截，需先打断引用链。</p>
            </div>`
          : ''
      }
      <div class="dsl-box">
        <span class="label">变更原因</span>
        <p class="section-note">${esc(change.reason || '—')}</p>
      </div>
      ${
        change.proposed_dsl
          ? `<div class="dsl-box"><span class="label">拟定口径 DSL（算法 A 已校验）</span><pre class="code">${esc(change.proposed_dsl)}</pre></div>`
          : ''
      }
      <div class="dsl-box">
        <span class="label">影响面 · 提交时算法 B 计算并锁定</span>
        ${impactRows(impact)}
      </div>
      ${
        approver && change.status === 'pending'
          ? `<div class="dsl-box">
              <span class="label">管理员审批（环检测通过 + 审批完成才生成正式版本）</span>
              ${field({ label: '审批备注', name: 'note', value: '', placeholder: '批准理由 / 驳回原因' })}
              <div style="display:flex;gap:8px">
                <button class="btn sm accent" id="approve" type="button">批准</button>
                <button class="btn sm ghost" id="reject" type="button">驳回</button>
              </div>
            </div>`
          : change.status === 'pending'
            ? '<p class="muted tiny">待管理员审批。专员可以提交变更单与查看影响面，不能批准。</p>'
            : '<p class="muted tiny">该变更单已完成审批。</p>'
      }`,
  });

  const decide = async (status) => {
    try {
      const res = await api.patch(`/api/change-requests/${change.id}`, {
        status,
        note: root.querySelector('[name=note]')?.value?.trim() || null,
      });
      if (res.replayed) toast('该变更单已审批过，未重复生成版本或审计');
      else toast(status === 'approved' ? '已批准变更单并生成正式版本' : '已驳回变更单，原口径与依赖图保留');
      close();
      await changesPage.render(view, ctx);
    } catch (err) {
      if (err.code === 'CONFLICT') {
        if (err.detail?.reason === 'cycle') toast('批准被拦截：新口径会引入依赖环', 'err');
        else toast(errorMessage(err), 'err');
      } else {
        toast(errorMessage(err), 'err');
      }
    }
  };
  root.querySelector('#approve')?.addEventListener('click', () => decide('approved'));
  root.querySelector('#reject')?.addEventListener('click', () => decide('rejected'));
}

async function renderAudit(view, ctx, user) {
  const panel = view.querySelector('#audit-panel');
  if (!panel) return;
  try {
    const data = await api.get('/api/audit', { limit: 10 });
    panel.innerHTML = `<div class="panel-head"><h2>审计尾迹</h2><span class="meta">${data.total} 条 · 最近 10</span></div>
      <div class="panel-body flush">${table({
        rows: data.items,
        columns: [
          { key: 'created_at', label: '时间', render: (row) => `<span class="tiny">${fmtTime(row.created_at)}</span>` },
          { key: 'actor_email', label: '操作人', className: 'mono' },
          { key: 'actor_role', label: '角色' },
          { key: 'action_label', label: '动作' },
          { key: 'entity', label: '实体', className: 'mono' },
          { key: 'layer', label: '层' },
        ],
        empty: { title: '没有审计记录', hint: '登录或提交变更单后会写入。' },
      })}</div>`;
    panel.querySelectorAll('table.data tbody tr').forEach((row) => row.replaceWith(row));
  } catch (err) {
    panel.innerHTML = `<div class="panel-body"><div class="error-box"><span>${esc(errorMessage(err))}</span></div></div>`;
  }
}

export const changesPage = {
  async render(view, ctx) {
    let data;
    let runs;
    try {
      data = await api.get('/api/change-requests', { status: state.status, limit: 50 });
      runs = await api.get('/api/runs', { limit: 3 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => changesPage.render(view, ctx));
      return;
    }
    const latest = runs.items[0];
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>校验运行</h2>
          <div style="display:flex;gap:10px;align-items:center">
            <span class="meta">${runs.total} 次</span>
            ${can(ctx.user.role, 'run:create') ? '<button class="btn sm accent" id="run-now">跑一次校验</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${
            latest
              ? `<dl class="kv">
                  <dt>运行号</dt><dd class="mono">${esc(latest.run_no)}</dd>
                  <dt>状态</dt><dd>${esc(ENUMS.runStatus[latest.status] || latest.status)}</dd>
                  <dt>输入 / 问题</dt><dd>${esc(latest.input_count)} 条口径 → ${esc(latest.output_count)} 条问题</dd>
                  <dt>冲突 / 环</dt><dd>${esc(latest.metrics?.conflicts_total ?? 0)} 处冲突 / ${esc(latest.metrics?.cycles_total ?? 0)} 个环</dd>
                  <dt>依赖边</dt><dd class="mono">指标 ${esc(latest.metrics?.dep_edges?.metric ?? 0)} · 字段 ${esc(latest.metrics?.dep_edges?.field ?? 0)} · 报表 ${esc(latest.metrics?.dep_edges?.report ?? 0)}</dd>
                  <dt>日志</dt><dd class="tiny">${esc(latest.log_text || '—')}</dd>
                </dl>`
              : emptyState({ title: '还没有校验运行', hint: '点右上角跑一次。' })
          }
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>变更单</h2><span class="meta">${data.total} 张 · 待审批 ${data.items.filter((item) => item.status === 'pending').length}</span></div>
        ${filterBar('change-filters', [
          { label: '状态', name: 'status', value: state.status, options: [{ value: '', label: '全部状态' }, ...Object.entries(ENUMS.changeStatus).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: data.items,
          columns: [
            { key: 'cr_no', label: '变更单号', className: 'mono' },
            { key: 'target_ref', label: '目标', className: 'mono' },
            { key: 'change_kind_label', label: '类型' },
            {
              key: 'base_version',
              label: '基线',
              className: 'num',
              render: (row) => (row.base_version ? `v${esc(row.base_version)}` : '—'),
            },
            {
              key: 'cycle_check_status',
              label: '环检测',
              render: (row) => {
                const tone = row.cycle_check_status === 'failed' ? 'danger' : row.cycle_check_status === 'passed' ? 'ok' : 'info';
                return `<span class="badge ${tone}">${esc(ENUMS.cycleCheck[row.cycle_check_status] || row.cycle_check_status)}</span>`;
              },
            },
            {
              key: 'severity',
              label: '严重度',
              render: (row) => `<span class="badge ${badgeTone(row.severity)}">${esc(ENUMS.severity[row.severity])}</span>`,
            },
            { key: 'impact_score', label: '影响分', className: 'num' },
            {
              key: 'applied_version',
              label: '生效版本',
              className: 'num',
              render: (row) => (row.applied_version ? `v${esc(row.applied_version)}` : '<span class="muted tiny">未生成</span>'),
            },
            {
              key: 'impact',
              label: '影响面',
              render: (row) => `<span class="tiny">指标 ${esc((row.impact.metric_codes || []).length)} / 报表 ${esc(row.impact.report_count || 0)}</span>`,
            },
            {
              key: 'status',
              label: '状态',
              render: (row) => `<span class="badge ${changeTone(row.status)}">${esc(row.status_label)}</span>`,
            },
            { key: 'requester_email', label: '申请人', className: 'mono' },
            { key: 'created_at', label: '申请时间', render: (row) => `<span class="tiny">${fmtTime(row.created_at)}</span>` },
          ],
          empty: { title: '没有变更单', hint: '在「冲突与影响面」里把一处冲突变成变更单。' },
        })}
      </section>
      ${
        can(ctx.user.role, 'export:read')
          ? `<section class="panel">
              <div class="panel-head"><h2>导出</h2><span class="meta">UTF-8 BOM · CSV</span></div>
              <div class="panel-body">
                <div class="export-grid">
                  ${EXPORTS.map(
                    ([path, filename, title, hint]) => `<div class="export-card">
                      <b>${esc(title)}</b>
                      <span class="hint">${esc(hint)}</span>
                      <button class="btn sm ghost" data-export="${esc(path)}" data-filename="${esc(filename)}">下载 ${esc(filename)}</button>
                    </div>`,
                  ).join('')}
                </div>
              </div>
            </section>`
          : `<section class="panel"><div class="panel-head"><h2>导出</h2><span class="meta">403</span></div>
              <div class="panel-body"><p class="section-note">只读角色不提供导出（后端会返回真实 403）。如需导出请使用专员或管理员账号。</p></div></section>`
      }
      ${can(ctx.user.role, 'audit:read') ? '<section class="panel" id="audit-panel"></section>' : ''}`;

    view.querySelector('#run-now')?.addEventListener('click', async () => {
      try {
        const run = await api.post('/api/runs', { trigger: 'manual' });
        toast(`校验完成：${run.output_count} 条问题`);
        await changesPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelector('#change-filters')?.addEventListener('change', (event) => {
      state.status = event.target.value;
      changesPage.render(view, ctx);
    });
    view.querySelectorAll('[data-export]').forEach((button) => {
      button.addEventListener('click', async () => {
        try {
          await api.download(button.dataset.export, button.dataset.filename);
          toast(`${button.dataset.filename} 已下载`);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const crNo = row.querySelector('.mono')?.textContent;
        const item = data.items.find((entry) => entry.cr_no === crNo);
        if (item) openChange(view, ctx, item);
      });
    });
    if (can(ctx.user.role, 'audit:read')) await renderAudit(view, ctx, ctx.user);
  },
};

export default changesPage;
