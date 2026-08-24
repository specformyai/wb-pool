/* ============================================================
 * overview.js —— 概览页（首页）
 *
 * 聚合 pool / calls.health / keys / rates / proxy / uoomsg 六个源，
 * 给一眼看清「现在整个池子是什么状态」。
 * ============================================================ */

import {
  apiFetch, fmtMoney, fmtInt, fmtTime, escapeHtml,
  skeleton, errorState, refreshIcons, poll,
} from '@/shared.js';
// 模型健康区块（KPI 汇总卡 + 模型卡片网格）独立成模块，见 app/health.js
import { renderHealth, renderHealthStats, bindHealth } from '@/health.js';

let stopPoll = null;
const state = { data: null, err: '', loading: true };

/** 六个源并行拉，任何一个失败不影响其他卡片渲染 —— 概览页最怕一个接口挂了整页空白 */
async function loadAll() {
  const jobs = {
    pool: () => apiFetch('/api/pool'),
    health: () => apiFetch('/api/calls/health?window_h=24&buckets=24'),
    keys: () => apiFetch('/api/keys'),
    rates: () => apiFetch('/api/rates'),
    proxy: () => apiFetch('/api/proxy'),
    sms: () => apiFetch('/api/uoomsg/balance'),
  };
  const entries = await Promise.all(
    Object.entries(jobs).map(async ([k, fn]) => {
      try { return [k, { ok: true, d: await fn() }]; }
      catch (e) { return [k, { ok: false, err: e.message }]; }
    })
  );
  return Object.fromEntries(entries);
}

function statCard(icon, label, value, sub = '', cls = '') {
  return `<div class="stat ${cls}">
    <div class="stat-ic"><i data-lucide="${icon}"></i></div>
    <div><div class="stat-v">${value}</div>
      <div class="stat-l">${escapeHtml(label)}${sub ? ` · ${escapeHtml(sub)}` : ''}</div></div>
  </div>`;
}

function renderStats(d) {
  const cards = [];

  if (d.pool.ok) {
    const accs = d.pool.d.accounts || [];
    const st = d.pool.d.stats || {};
    // 池内余额只算「可用账号」的和：后端 stats.credits_total 已经这么算了。
    // 自己 reduce 全部账号会把已禁用/已死号的残额也算进去（实测 2039 → 31018），
    // 那个数字调度器一分也调不动，摆在概览上是误导。
    const sum = Number(st.credits_total) || 0;
    const usable = accs.filter((a) => a.usable).length;
    const risk = accs.filter((a) => a.expires_in_h != null && a.expires_in_h < 24).length;
    cards.push(statCard('layers', '账号', fmtInt(accs.length), `${usable} 可用`));
    cards.push(statCard('coins', '池内余额', '¥' + fmtMoney(sum),
      st.credits_checked_at ? '更新于 ' + fmtTime(st.credits_checked_at) : ''));
    if (risk) cards.push(statCard('shield-alert', 'Token 将过期', fmtInt(risk), '24 小时内', 'warn'));
  } else {
    cards.push(statCard('layers', '账号', '—', '读取失败', 'bad'));
  }

  if (d.keys.ok) {
    const ks = d.keys.d.keys || [];
    cards.push(statCard('key-round', 'API Key', fmtInt(ks.length),
      `${ks.filter((k) => k.enabled).length} 启用`));
  }

  return `<div class="stats">${cards.join('')}</div>`;
}

/** 基础设施：代理 / 接码余额 / 倍率基准，三个小卡横排 */
function renderInfra(d) {
  const items = [];

  if (d.proxy.ok) {
    const p = d.proxy.d;
    const modeTxt = { off: '已关闭', fixed: '固定出口', rotate: '轮换出口' }[p.mode] || p.mode;
    const usable = (p.usable || []).length;
    const cls = p.mode === 'off' ? 'idle' : (usable ? 'ok' : 'bad');
    items.push(`<div class="infra">
      <div class="infra-h"><i data-lucide="route"></i><span>代理池</span>
        <span class="badge ${cls}">${modeTxt}</span></div>
      <div class="infra-v">${p.mode === 'off' ? '走本机出口'
        : `${usable} / ${p.exits_configured || 0} 出口可用`}</div>
      <div class="infra-s">${p.probed_at ? fmtTime(p.probed_at) + '探测' : '尚未探测'}</div>
    </div>`);
  }

  if (d.sms.ok) {
    const s = d.sms.d;
    items.push(`<div class="infra">
      <div class="infra-h"><i data-lucide="message-square-code"></i><span>接码平台</span>
        <span class="badge ${s.ok ? 'ok' : 'warn'}">${s.ok ? '已配置' : '未配置'}</span></div>
      <div class="infra-v">${s.ok ? '¥' + fmtMoney(s.balance) : '—'}</div>
      <div class="infra-s">${s.ok ? '可用余额' : escapeHtml(s.error || '')}</div>
    </div>`);
  }

  if (d.rates.ok) {
    const r = d.rates.d;
    const rows = r.rows || [];
    const sampled = rows.filter((x) => x.credits_per_1k != null);
    items.push(`<div class="infra">
      <div class="infra-h"><i data-lucide="scale"></i><span>倍率基准</span>
        <span class="badge ${sampled.length ? 'acc' : 'idle'}">${sampled.length} 有样本</span></div>
      <div class="infra-v mono">${escapeHtml(r.base_model || '—')}</div>
      <div class="infra-s">${sampled.length ? '最便宜模型，作 1× 基准' : '样本不足，跑几次请求后可算'}</div>
    </div>`);
  }

  if (!items.length) return '';
  return `<div class="infra-grid">${items.join('')}</div>`;
}

function render(root) {
  if (state.loading && !state.data) {
    root.innerHTML = `
      <div class="page-head"><div><h1>概览</h1><p>账号池、模型可用性与基础设施状态</p></div></div>
      <div class="stats">${Array.from({ length: 4 }, () =>
        '<div class="stat"><div class="skl" style="width:70%"></div></div>').join('')}</div>
      <div class="card">${skeleton(6)}</div>`;
    refreshIcons();
    return;
  }
  if (state.err) {
    root.innerHTML = `<div class="page-head"><div><h1>概览</h1></div></div>`;
    root.appendChild(errorState(state.err, () => boot(root)));
    refreshIcons();
    return;
  }
  const d = state.data;
  root.innerHTML = `
    <div class="page-head">
      <div><h1>概览</h1><p>账号池、模型可用性与基础设施状态</p></div>
      <div class="acts">
        <button class="btn ghost" data-act="reload"><i data-lucide="rotate-cw"></i><span>刷新</span></button>
      </div>
    </div>
    ${renderStats(d)}
    ${renderInfra(d)}
    ${renderHealthStats(d.health)}
    ${renderHealth(d.health)}`;
  refreshIcons();
  bindHealth(root);   // 「显示全部模型」的事件委托，重复调用会自动忽略

  root.querySelector('[data-act="reload"]')?.addEventListener('click', () => boot(root));
  root.querySelectorAll('[data-go]').forEach((b) =>
    b.addEventListener('click', () => location.hash = '#/' + b.dataset.go));
}

async function boot(root) {
  state.loading = true;
  state.err = '';
  render(root);
  try {
    state.data = await loadAll();
    state.err = '';
  } catch (e) {
    state.err = e.message;
  } finally {
    state.loading = false;
    render(root);
  }
}

export function mount(root) {
  boot(root);
  // 概览是常驻页，30 秒刷一次够了；poll 内部会在页面隐藏时自动暂停
  stopPoll = poll(async () => {
    state.data = await loadAll();
    render(root);
  }, 30000);
}

export function unmount() {
  if (stopPoll) { stopPoll(); stopPoll = null; }
}