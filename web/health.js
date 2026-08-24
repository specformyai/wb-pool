/* health.js — 概览页「模型健康」：KPI 汇总卡 + 模型卡片网格 */
import { $$, escapeHtml, fmtInt, fmtMoney, fmtDur } from '@/shared.js';

/* 状态字典：rank 用于排序（bad → degraded → ok → idle） */
const STATE = {
  bad:      { rank: 0, icon: 'x-circle',       label: '异常' },
  degraded: { rank: 1, icon: 'alert-triangle', label: '波动' },
  ok:       { rank: 2, icon: 'check-circle-2', label: '正常' },
  idle:     { rank: 3, icon: 'moon',           label: '空闲' },
};
const stKey = (s) => (STATE[s] ? s : 'idle');
const esc = (v) => escapeHtml(v == null ? '' : String(v));

const VISIBLE = 6; /* 默认展示的卡片数 */

/* 卡片区头部（保留 data-go="calls" 路由约定） */
function cardHead(windowH) {
  const h = Number.isFinite(+windowH) ? +windowH : 24;
  return `
    <div class="card-h">
      <div>
        <h3>模型健康</h3>
        <p>近 ${esc(h)} 小时</p>
      </div>
      <div class="acts">
        <button class="btn ghost sm" data-go="calls"><span>全部</span><i data-lucide="arrow-right"></i></button>
      </div>
    </div>`;
}

/* ---------------------------------------------------------------- */
export function renderHealthStats(d) {
  if (!d || !d.ok || !d.d) return '';
  const h = d.d;
  const avg = typeof h.avg_rate === 'number' ? (h.avg_rate * 100).toFixed(1) + '%' : '—';
  const warn = (h.abnormal ?? 0) > 0;

  const item = (icon, label, value, sub = '', warnIc = false) => `
    <div class="mh-kpi">
      <div class="mh-kpi-ic${warnIc ? ' warn' : ''}"><i data-lucide="${icon}" class="mh-ic"></i></div>
      <div class="mh-kpi-v">${value}</div>
      <div class="mh-kpi-l">${label}</div>${sub}
    </div>`;

  return `<div class="mh-kpis">${
    item('line-chart', '模型数量', fmtInt(h.model_count)) +
    item('activity', '总请求', fmtInt(h.total)) +
    item('check-circle-2', '平均成功率', avg) +
    item('alert-triangle', '异常模型', fmtInt(h.abnormal),
      `<div class="mh-kpi-sub">正常 ${fmtInt(h.normal)}</div>`, warn)
  }</div>`;
}

/* ---------------------------------------------------------------- */
export function renderHealth(d) {
  if (!d || !d.ok || !d.d) {
    return `<div class="card">${cardHead(24)}
      <div class="err-state"><i data-lucide="cloud-off"></i><div class="err-msg">${esc(d && d.err ? d.err : '数据加载失败')}</div></div>
    </div>`;
  }

  const h = d.d;
  const models = Array.isArray(h.models) ? h.models : [];

  if (!models.length) {
    return `<div class="card">${cardHead(h.window_h)}
      <div class="empty-state"><i data-lucide="inbox"></i>
        <div class="t">窗口内没有调用记录</div>
        <div class="s">发起一次请求后这里会显示各模型的成功率与延迟</div>
      </div>
    </div>`;
  }

  const sorted = [...models].sort((a, b) =>
    STATE[stKey(a.state)].rank - STATE[stKey(b.state)].rank || (b.total || 0) - (a.total || 0));

  const nBuckets = h.buckets > 0 ? h.buckets : 24;
  const cards = sorted.map((m, i) => modelCard(m, nBuckets, i >= VISIBLE)).join('');

  const more = sorted.length > VISIBLE
    ? `<div class="mh-more"><button class="btn ghost sm" data-mh-more><span>显示全部 ${fmtInt(sorted.length)} 个模型</span></button></div>`
    : '';

  return `<div class="card">${cardHead(h.window_h)}<div class="mh-grid">${cards}</div>${more}</div>`;
}

/* ---------------------------------------------------------------- */
function modelCard(m, nBuckets, hidden) {
  const key = stKey(m.state);
  const s = STATE[key];
  const total = m.total || 0;
  const fail = m.fail || 0;
  const rate = total && typeof m.rate === 'number' ? (m.rate * 100).toFixed(1) + '%' : '—';

  const meta = [`${fmtInt(m.accounts ?? 0)} 个账号`, `${fmtInt(total)} 总请求`];
  if ((m.credits ?? 0) > 0) meta.push(`${fmtMoney(m.credits)} 积分`);

  const bk = Array.isArray(m.buckets) ? m.buckets : [];
  let bars = '';
  for (let i = 0; i < nBuckets; i++) {
    const b = bk[i] || {};
    bars += `<span class="mh-bar ${stKey(b.state)}" title="第 ${i + 1} 小时 · 成功 ${b.ok || 0} / 失败 ${b.fail || 0}"></span>`;
  }

  const ttft = m.ttft_ms ? fmtDur(m.ttft_ms) : '—';
  const tps = m.tps ? `${esc(m.tps)} t/s` : '—';

  const errRaw = typeof m.last_error === 'string' ? m.last_error.trim() : '';
  const errHtml = errRaw ? `
    <details class="mh-err">
      <summary><i data-lucide="chevron-right" class="mh-chev"></i><span class="mh-err-line mono" title="${esc(errRaw)}">${esc(errRaw.replace(/\s+/g, ' '))}</span></summary>
      <pre class="mh-err-full mono">${esc(errRaw)}</pre>
    </details>` : '';

  return `
  <div class="mh-card${hidden ? ' mh-hide' : ''}">
    <div class="mh-card-body">
      <div class="mh-card-h">
        <div class="mh-name mono">${esc(m.model || '未知模型')}</div>
        <span class="mh-pill ${key}"><i data-lucide="${s.icon}" class="mh-pill-ic"></i><span>${s.label}</span></span>
      </div>
      <div class="mh-meta">${esc(meta.join(' · '))}</div>
      <div class="mh-metrics">
        <div class="mh-metric"><div class="mh-metric-l">成功率</div><div class="mh-metric-v mono">${rate}</div></div>
        <div class="mh-metric"><div class="mh-metric-l">成功</div><div class="mh-metric-v mono">${fmtInt(m.ok)}</div></div>
        <div class="mh-metric"><div class="mh-metric-l">错误</div><div class="mh-metric-v mono${fail > 0 ? ' bad' : ''}">${fmtInt(fail)}</div></div>
      </div>
      <div class="mh-bars">
        <div class="mh-bars-row">${bars}</div>
        <div class="mh-bars-cap"><span>过去</span><span>现在</span></div>
      </div>
    </div>
    <div class="mh-foot">
      <span>近期平均首字延迟：<span class="mh-foot-v mono">${ttft}</span></span>
      <span>近期平均输出速度：<span class="mh-foot-v mono">${tps}</span></span>
    </div>
    ${errHtml}
  </div>`;
}

/* ---------------------------------------------------------------- */
const boundRoots = new WeakSet();

export function bindHealth(root) {
  if (!root || boundRoots.has(root)) return;
  boundRoots.add(root);
  root.addEventListener('click', (e) => {
    const t = e.target;
    const btn = t && t.closest ? t.closest('[data-mh-more]') : null;
    if (!btn || !root.contains(btn)) return;
    $$('.mh-card.mh-hide', root).forEach((c) => c.classList.remove('mh-hide'));
    (btn.closest('.mh-more') || btn).remove();
  });
}