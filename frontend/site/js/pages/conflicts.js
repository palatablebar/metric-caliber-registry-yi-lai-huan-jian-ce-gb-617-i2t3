import { api } from '../api.js';
import { ENUMS, badgeTone, can } from '../config.js';
import { chips, drawer, emptyState, errorBox, errorMessage, esc, field, filterBar, fmtTime, impactRows, kv, table, toast } from '../ui.js';

const state = { severity: '', kind: '' };

function variantCard(metric, index) {
  return `<div class="diff-col">
    <h4>口径 ${String(index + 1).padStart(2, '0')} · ${esc(metric.code)} <span class="mono tiny muted">v${esc(metric.version ?? '—')}</span></h4>
    <p class="section-note">${esc(metric.expression || '—')}</p>
    <div class="dsl-box"><span class="label">字段</span>${chips(metric.fields, { withIndex: false })}</div>
    <div class="dsl-box"><span class="label">过滤</span>${chips(metric.filters, { withIndex: false })}</div>
    <div class="dsl-box"><span class="label">聚合</span>${chips(metric.aggs, { withIndex: false })}</div>
  </div>`;
}

function openConflict(view, ctx, conflict) {
  const writable = can(ctx.user.role, 'change:write');
  const first = conflict.metrics[0];
  const { root, close } = drawer({
    title: `同名冲突 · ${conflict.name}`,
    subtitle: `${conflict.metric_codes.join(' vs ')} · ${ENUMS.conflictKind[conflict.kind] || conflict.kind}`,
    body: `
      ${kv([
        ['差异类型', esc(ENUMS.conflictKind[conflict.kind] || conflict.kind)],
        ['严重度', `<span class="badge ${badgeTone(conflict.severity)}">${esc(ENUMS.severity[conflict.severity])}</span>`],
        ['影响分', `<span class="mono">${esc(conflict.score)}</span>`],
        ['下游报表', `${esc(conflict.impact.report_count)} 张（核心 ${esc(conflict.impact.core_count)} 张）`],
      ])}
      <div class="diff-cols">${conflict.metrics.map(variantCard).join('')}</div>
      <div class="dsl-box">
        <span class="label">差异明细</span>
        <div class="list-tight">
          <div class="impact-row"><span>仅 ${esc(conflict.metrics[0].code)} 有的字段</span><span class="mono">${esc((conflict.diff.fields_only_first || []).join(' / ') || '—')}</span></div>
          <div class="impact-row"><span>仅其他口径有的字段</span><span class="mono">${esc((conflict.diff.fields_only_others || []).join(' / ') || '—')}</span></div>
          <div class="impact-row"><span>仅 ${esc(conflict.metrics[0].code)} 的过滤条件</span><span class="mono">${esc((conflict.diff.filters_only_first || []).join(' ; ') || '—')}</span></div>
          <div class="impact-row"><span>仅其他口径的过滤条件</span><span class="mono">${esc((conflict.diff.filters_only_others || []).join(' ; ') || '—')}</span></div>
        </div>
      </div>
      <div class="dsl-box">
        <span class="label">影响面 · 下游报表（核心报表权重 ×3）</span>
        ${impactRows(conflict.impact)}
      </div>
      <div class="dsl-box">
        <span class="label">建议动作</span>
        <p class="section-note">${esc(conflict.suggestion)}</p>
      </div>
      ${
        writable
          ? `<div class="dsl-box">
              <span class="label">把这份冲突变成变更单</span>
              ${field({ label: '变更指标', name: 'metric_code', value: first.code, options: conflict.metrics.map((item) => ({ value: item.code, label: `${item.code} · ${item.name}` })) })}
              ${field({ label: '原因（月会结论 / 数仓变更）', name: 'reason', value: `同名冲突「${conflict.name}」：${conflict.suggestion}` })}
              <button class="btn sm accent" id="create-cr" type="button">提交变更单</button>
            </div>`
          : '<p class="muted tiny">只读角色可以查看冲突与影响面，提交变更单需要专员及以上角色。</p>'
      }`,
  });

  root.querySelector('#create-cr')?.addEventListener('click', async () => {
    const selectEl = root.querySelector('[name=metric_code]');
    const metricCode = selectEl?.value || first.code;
    const selected = conflict.metrics.find((item) => item.code === metricCode) || first;
    const reason = root.querySelector('[name=reason]')?.value || '';
    try {
      await api.post('/api/change-requests', {
        change_kind: 'caliber',
        target_ref: metricCode,
        metric_code: metricCode,
        reason,
        base_version: selected.version || 1,
      });
      toast('变更单已提交，等待管理员审批');
      close();
      await conflictsPage.render(view, ctx);
    } catch (err) {
      if (err.code === 'CONFLICT' && err.detail?.draft) {
        toast(`基线已过期，已转为冲突草案：${err.detail.draft.diff_summary || ''}`, 'err');
        close();
        await conflictsPage.render(view, ctx);
      } else {
        toast(err.code === 'CONFLICT' ? '该指标已有待审批的同类变更单' : errorMessage(err), 'err');
      }
    }
  });
}

async function renderDrafts(view, ctx) {
  const panel = view.querySelector('#drafts-panel');
  if (!panel) return;
  try {
    const data = await api.get('/api/conflicts/drafts', { status: 'open', limit: 50 });
    if (!data.items.length) {
      panel.innerHTML = `<div class="panel-head"><h2>过期提交 · 冲突草案</h2><span class="meta">0 条</span></div>
        <div class="panel-body"><p class="muted tiny">没有基于过期版本的提交。所有口径提交都基于当前版本时，这里保持为空。</p></div>`;
      return;
    }
    const writable = can(ctx.user.role, 'change:write');
    panel.innerHTML = `<div class="panel-head">
        <h2>过期提交 · 冲突草案</h2><span class="meta">${data.total} 条待处理</span>
      </div>
      ${table({
        rows: data.items,
        columns: [
          { key: 'metric_code', label: '指标', className: 'mono' },
          { key: 'base_version', label: '基线 / 当前', className: 'mono', render: (row) => `v${esc(row.base_version)} / v${esc(row.head_version)}` },
          { key: 'diff_summary', label: '与当前口径差异', render: (row) => `<span class="tiny">${esc(row.diff_summary || '—')}</span>` },
          { key: 'source', label: '来源' },
          { key: 'author_email', label: '提交人', className: 'mono' },
          { key: 'created_at', label: '时间', render: (row) => `<span class="tiny">${fmtTime(row.created_at)}</span>` },
          {
            key: 'actions',
            label: '操作',
            render: (row) => (writable
              ? `<button class="btn sm ghost" data-resolve="${esc(row.id)}">重新基线</button>
                 <button class="btn sm ghost" data-discard="${esc(row.id)}">废弃</button>`
              : '—'),
          },
        ],
        empty: { title: '没有待处理草案' },
      })}`;
    const act = async (id, action) => {
      try {
        await api.patch(`/api/conflicts/drafts/${id}`, { action, note: action === 'discard' ? '页面废弃' : '已基于最新版本重新提交' });
        toast(action === 'discard' ? '草案已废弃' : '草案已标记重新基线');
        await conflictsPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    };
    panel.querySelectorAll('[data-resolve]').forEach((btn) => btn.addEventListener('click', () => act(btn.dataset.resolve, 'resolve')));
    panel.querySelectorAll('[data-discard]').forEach((btn) => btn.addEventListener('click', () => act(btn.dataset.discard, 'discard')));
  } catch (err) {
    panel.innerHTML = `<div class="panel-body"><div class="error-box"><span>${esc(errorMessage(err))}</span></div></div>`;
  }
}

export const conflictsPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/conflicts', { severity: state.severity, kind: state.kind, limit: 50 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => conflictsPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>同名不同口径 · 冲突与影响面</h2>
          <div style="display:flex;gap:10px;align-items:center">
            <span class="meta">${data.total} 处</span>
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost" id="export-conflicts">导出 CSV</button>' : ''}
          </div>
        </div>
        ${filterBar('conflict-filters', [
          { label: '严重度', name: 'severity', value: state.severity, options: [{ value: '', label: '全部等级' }, ...Object.entries(ENUMS.severity).map(([value, label]) => ({ value, label }))] },
          { label: '差异类型', name: 'kind', value: state.kind, options: [{ value: '', label: '全部类型' }, ...Object.entries(ENUMS.conflictKind).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: data.items,
          columns: [
            { key: 'name', label: '同名指标', render: (row) => `<b>${esc(row.name)}</b>` },
            {
              key: 'kind',
              label: '差异类型',
              render: (row) => `<span class="badge warn">${esc(ENUMS.conflictKind[row.kind] || row.kind)}</span>`,
            },
            {
              key: 'severity',
              label: '严重度',
              render: (row) => `<span class="badge ${badgeTone(row.severity)}">${esc(ENUMS.severity[row.severity])}</span>`,
            },
            { key: 'metric_codes', label: '口径成员', className: 'mono', render: (row) => esc(row.metric_codes.join(' / ')) },
            { key: 'score', label: '影响分', className: 'num' },
            { key: 'impact', label: '下游报表', className: 'num', render: (row) => esc(row.impact.report_count) },
            { key: 'suggestion', label: '建议动作', render: (row) => `<span class="tiny">${esc(row.suggestion.slice(0, 42))}…</span>` },
          ],
          empty: { title: '没有匹配的冲突', hint: '调整筛选条件，或改一条口径后再跑校验。' },
        })}
      </section>
      <section class="panel" id="drafts-panel"></section>`;

    const filters = view.querySelector('#conflict-filters');
    filters?.addEventListener('change', () => {
      state.severity = filters.querySelector('[name=severity]').value;
      state.kind = filters.querySelector('[name=kind]').value;
      conflictsPage.render(view, ctx);
    });
    view.querySelector('#export-conflicts')?.addEventListener('click', async () => {
      try {
        await api.download('/api/exports/conflicts.csv', 'conflicts.csv');
        toast('冲突与影响面 CSV 已下载');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelector('section.panel table.data tbody')?.querySelectorAll('tr').forEach((row) => {
      row.addEventListener('click', () => {
        const name = row.querySelector('b')?.textContent;
        const item = data.items.find((entry) => entry.name === name);
        if (item) openConflict(view, ctx, item);
      });
    });
    await renderDrafts(view, ctx);
  },
};

export default conflictsPage;
