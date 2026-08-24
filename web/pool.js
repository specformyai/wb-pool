/* ============================================================
 * pool.js —— 账号池页面（ES module，由 router.js 挂载）
 *
 * 由 k3 生成的原版改造而来，逻辑保持不变，改动仅限：
 *   - 工具函数改为 import shared.js（原版自带一套，会和其他页面重复定义）
 *   - DOM 模板搬进本文件，mount 时注入
 *   - 轮询改用 poll()，unmount 可停
 * ============================================================ */

import {
  $, $$, apiFetch, escapeHtml, fmtMoney, poll, refreshIcons,
} from '@/shared.js';
import { selectify } from '@/selectify.js';

// ---------- 状态 ----------
const state = {
  accounts: [],                 // 归一化后的账号列表
  stats: {},                    // 后端 /api/pool 的 stats 块（余额口径以它为准）
  loading: true,
  loadError: '',
  rotation: { mode: null, loading: true, saving: false, error: '' },
  q: '',                        // 搜索关键字
  status: 'all',                // 状态筛选
  balance: 'all',               // 余额筛选
  sort: { key: 'balance', dir: -1 },
  selected: new Set(),          // 已选手机号
  busyGlobal: false,            // 全局任务互斥锁
};

// ---------- 数据归一化（防御后端字段差异） ----------
/** 解析时间戳：兼容秒/毫秒 epoch 与 ISO 字符串，返回毫秒或 null */
function parseTs(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'number') { const ms = v > 1e12 ? v : v * 1000; return Number.isFinite(ms) ? ms : null; }
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? null : ms;
}
/** 套餐字段：可能是字符串 / 对象 / 数组 */
function fmtPackages(p) {
  if (!p) return '';
  if (Array.isArray(p)) return p.map(x => typeof x === 'string' ? x : (x?.name || x?.title || x?.label || '')).filter(Boolean).join('、');
  if (typeof p === 'object') return p.name || p.title || '';
  return String(p);
}
function normalizeAccount(raw, idx) {
  const phone = String(raw.phone ?? raw.account ?? '');
  const st = String(raw.status ?? 'active').toLowerCase();
  let statusKey = 'other', statusText = raw.status != null ? String(raw.status) : '未知';
  if (['active', 'ok', 'enabled', 'normal', 'on', 'online'].includes(st)) { statusKey = 'active'; statusText = '正常'; }
  else if (['disabled', 'disable', 'off', 'inactive', 'paused', 'stopped'].includes(st)) { statusKey = 'disabled'; statusText = '已禁用'; }
  else if (['error', 'banned', 'failed', 'invalid', 'expired', 'dead', 'cooldown'].includes(st)) { statusKey = 'error'; statusText = '异常'; }

  // 余额：多字段兜底。后端 GET /api/pool 实际返回 credits_total（不是 balance/credits），
  // k3 原版漏了这个字段名，导致余额列恒为 —、总余额恒为 0。
  const balRaw = raw.credits_total ?? raw.balance ?? raw.credits ?? raw.points;
  const balance = balRaw == null || balRaw === '' ? null : Number(balRaw);

  // Token 有效期：优先 expires_in_h（小时数），否则从绝对时间换算
  let hours = null;
  if (typeof raw.expires_in_h === 'number') hours = raw.expires_in_h;
  else {
    const ts = parseTs(raw.expires_at ?? raw.token_expires_at ?? raw.expire_at ?? raw.expires);
    if (ts != null) hours = (ts - Date.now()) / 36e5;
  }

  return {
    key: phone || String(raw.uid ?? `row${idx}`),
    phone,
    label: String(raw.label ?? raw.name ?? ''),
    balance,
    statusKey, statusText,
    hours,
    lastCheckin: parseTs(raw.last_checkin ?? raw.last_checkin_at ?? raw.checkin_at ?? raw.checked_at),
    packages: fmtPackages(raw.packages ?? raw.package),
    maxBal: 1, balTier: 'none', // 统一在 loadPool 中计算
  };
}

// ---------- 时间文案 ----------
function fmtAgo(ms) {
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return new Date(ms).toLocaleDateString('zh-CN');
}
function fmtHours(h) {
  if (h <= 0) return '已过期';
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} 分钟后`;
  if (h < 48) return `${Math.round(h)} 小时后`;
  return `${Math.floor(h / 24)} 天后`;
}

// ---------- 视图计算：筛选 + 排序 ----------
const STATUS_ORDER = { active: 0, error: 1, disabled: 2, other: 3 };
function viewAccounts() {
  const q = state.q.trim().toLowerCase();
  const list = state.accounts.filter(a => {
    if (state.status !== 'all' && a.statusKey !== state.status) return false;
    if (state.balance !== 'all' && a.balTier !== state.balance) return false;
    if (q && !(a.phone.toLowerCase().includes(q) || a.label.toLowerCase().includes(q))) return false;
    return true;
  });
  const { key, dir } = state.sort;
  const cmp = {
    phone:   (a, b) => a.phone.localeCompare(b.phone, 'zh-CN'),
    status:  (a, b) => STATUS_ORDER[a.statusKey] - STATUS_ORDER[b.statusKey],
    balance: (a, b) => (a.balance ?? -1) - (b.balance ?? -1),
    expiry:  (a, b) => (a.hours ?? Infinity) - (b.hours ?? Infinity),
    checkin: (a, b) => (a.lastCheckin ?? 0) - (b.lastCheckin ?? 0),
  }[key] || (() => 0);
  return list.sort((a, b) => cmp(a, b) * dir);
}

// ---------- 片段模板 ----------
function balHtml(a) {
  const pct = a.balance != null && a.maxBal > 0
    ? Math.max(0, Math.min(100, (a.balance / a.maxBal) * 100)) : 0;
  return `<div class="bal">
    <div class="bal-num ${a.balTier}">${fmtMoney(a.balance)}</div>
    <div class="bar ${a.balTier}"><i style="width:${pct}%"></i></div>
  </div>`;
}
function expHtml(a) {
  if (a.hours == null) return '<span class="muted">—</span>';
  const tier = a.hours <= 0 ? 'bad' : a.hours < 24 ? 'crit' : a.hours < 72 ? 'warn' : 'ok';
  const gauge = Math.max(0, Math.min(100, (a.hours / (30 * 24)) * 100)); // 以 30 天为满刻度
  return `<div class="exp ${tier}"><span class="exp-dot"></span><span>${fmtHours(a.hours)}</span></div>
          <div class="bar slim ${tier}"><i style="width:${gauge}%"></i></div>`;
}
function statusHtml(a) {
  const expiredBadge = (a.hours != null && a.hours <= 0) ? '<span class="pill st-bad mini">Token 过期</span>' : '';
  return `<span class="pill st-${a.statusKey}"><span class="pill-dot"></span>${escapeHtml(a.statusText)}</span>${expiredBadge}`;
}
function opsHtml(a, withText = false) {
  const dis = a.phone ? '' : 'disabled';
  const togg = a.statusKey === 'disabled' ? ['enable', '启用'] : ['disable', '禁用'];
  const b = (act, icon, label, cls = '') =>
    `<button class="icon-btn ${cls}" type="button" data-act="${act}" data-phone="${escapeHtml(a.phone)}"
       title="${label}" aria-label="${label} ${escapeHtml(a.phone)}" ${dis}>
       <i data-lucide="${icon}"></i>${withText ? `<span>${label}</span>` : ''}</button>`;
  return b('checkin', 'calendar-check', '签到')
       + b('token', 'rotate-ccw', '刷新 Token')
       + b(togg[0], 'power', togg[1])
       + b('remove', 'trash-2', '移除', 'danger');
}
function rowHtml(a) {
  return `<tr data-phone="${escapeHtml(a.phone)}">
    <td>${a.phone ? `<input type="checkbox" class="row-check" data-phone="${escapeHtml(a.phone)}"
        ${state.selected.has(a.phone) ? 'checked' : ''} aria-label="选择 ${escapeHtml(a.phone)}" />` : ''}</td>
    <td><div class="acc-phone">${escapeHtml(a.phone) || '—'}</div>${a.label ? `<div class="acc-label">${escapeHtml(a.label)}</div>` : ''}</td>
    <td>${statusHtml(a)}</td>
    <td>${balHtml(a)}</td>
    <td class="c-pkg" title="${escapeHtml(a.packages)}">${escapeHtml(a.packages) || '<span class="muted">—</span>'}</td>
    <td>${expHtml(a)}</td>
    <td class="muted">${a.lastCheckin ? fmtAgo(a.lastCheckin) : '—'}</td>
    <td class="c-ops">${opsHtml(a)}</td>
  </tr>`;
}
function cardHtml(a) {
  return `<article class="acc-card" data-phone="${escapeHtml(a.phone)}">
    <div class="ac-top">
      <label class="ac-check">${a.phone ? `<input type="checkbox" class="row-check" data-phone="${escapeHtml(a.phone)}"
          ${state.selected.has(a.phone) ? 'checked' : ''} aria-label="选择 ${escapeHtml(a.phone)}" />` : ''}
        <span class="acc-phone">${escapeHtml(a.phone) || '—'}</span></label>
      ${statusHtml(a)}
    </div>
    ${a.label ? `<div class="acc-label">${escapeHtml(a.label)}</div>` : ''}
    ${balHtml(a)}
    <div class="ac-meta">
      <div><span class="k">Token 有效期</span>${expHtml(a)}</div>
      <div><span class="k">最近签到</span><span class="muted">${a.lastCheckin ? fmtAgo(a.lastCheckin) : '—'}</span></div>
      <div class="span2"><span class="k">套餐</span>${escapeHtml(a.packages) || '—'}</div>
    </div>
    <div class="ac-ops">${opsHtml(a, true)}</div>
  </article>`;
}
function emptyHtml(colspan) {
  const inner = state.loadError
    ? `<i data-lucide="triangle-alert"></i><h4>加载失败</h4><p>${escapeHtml(state.loadError)}</p>
       <button class="btn" type="button" id="retryLoad"><i data-lucide="refresh-cw"></i><span>重试</span></button>`
    : `<i data-lucide="inbox"></i><h4>暂无匹配账号</h4><p>调整筛选条件，或点击右上角「导入账号」添加。</p>`;
  return colspan ? `<tr><td colspan="${colspan}"><div class="empty">${inner}</div></td></tr>` : `<div class="empty">${inner}</div>`;
}
function skeletonRows(n = 5) {
  return Array.from({ length: n }, () => `<tr>${Array.from({ length: 8 },
    () => '<td><div class="skl" style="width:70%"></div></td>').join('')}</tr>`).join('');
}
function skeletonCards(n = 3) {
  return Array.from({ length: n }, () =>
    `<article class="acc-card"><div class="skl" style="width:45%"></div><div class="skl" style="width:90%"></div><div class="skl" style="width:70%"></div></article>`).join('');
}

// ---------- 渲染 ----------
const COLS = [
  { key: 'phone', label: '账号', sort: true },
  { key: 'status', label: '状态', sort: true },
  { key: 'balance', label: '余额', sort: true },
  { key: 'packages', label: '套餐', sort: false },
  { key: 'expiry', label: 'Token 有效期', sort: true },
  { key: 'checkin', label: '最近签到', sort: true },
  { key: 'ops', label: '操作', sort: false },
];
function renderThead() {
  const { key, dir } = state.sort;
  $('#poolHead').innerHTML = '<tr><th style="width:36px"><input type="checkbox" id="checkAll" aria-label="全选" /></th>'
    + COLS.map(c => c.sort
      ? `<th class="sortable ${key === c.key ? 'on' : ''}" data-sort="${c.key}"><span>${c.label}</span><i data-lucide="${key === c.key ? (dir === 1 ? 'chevron-up' : 'chevron-down') : 'arrow-up-down'}"></i></th>`
      : `<th>${c.label}</th>`).join('')
    + '</tr>';
  // 全选框状态（仅针对当前可见且有手机号的行）
  const view = viewAccounts().filter(a => a.phone);
  const selInView = view.filter(a => state.selected.has(a.phone)).length;
  const all = $('#checkAll');
  all.checked = view.length > 0 && selInView === view.length;
  all.indeterminate = selInView > 0 && selInView < view.length;
  all.onchange = () => {
    if (all.checked) view.forEach(a => state.selected.add(a.phone));
    else view.forEach(a => state.selected.delete(a.phone));
    renderTable(); renderCards(); renderBatch();
  };
}
function renderTable() {
  const body = $('#poolBody');
  if (state.loading) { renderThead(); body.innerHTML = skeletonRows(); refreshIcons(); return; }
  const view = viewAccounts();
  renderThead();
  body.innerHTML = view.length ? view.map(rowHtml).join('') : emptyHtml(COLS.length + 1);
  refreshIcons();
}
function renderCards() {
  const box = $('#poolCards');
  if (state.loading) { box.innerHTML = skeletonCards(); refreshIcons(); return; }
  const view = viewAccounts();
  box.innerHTML = view.length ? view.map(cardHtml).join('') : emptyHtml(0);
  refreshIcons();
}
function renderStats() {
  const box = $('#poolStats');
  if (state.loading && !state.accounts.length) {
    box.innerHTML = Array.from({ length: 4 }, () => '<div class="stat"><div class="skl" style="width:60%"></div></div>').join('');
    return;
  }
  const list = state.accounts;
  const total = list.length;
  const active = list.filter(a => a.statusKey === 'active').length;
  // 总余额只统计可用账号：后端 stats.credits_total 就是这个口径，
  // 含禁用/死号的全量在 credits_total_all（实测 2039.9 vs 31018.97）。
  // 拿不到 stats 时退回按 usable 过滤自算，不要 reduce 全部账号。
  const sum = state.stats.credits_total != null
    ? Number(state.stats.credits_total)
    : list.filter(a => a.statusKey === 'active').reduce((s, a) => s + (a.balance || 0), 0);
  const risk = list.filter(a => a.hours != null && a.hours < 24).length; // 含已过期
  const card = (icon, label, value, cls = '') => `<div class="stat ${cls}">
      <div class="stat-ic"><i data-lucide="${icon}"></i></div>
      <div><div class="stat-v">${value}</div><div class="stat-l">${label}</div></div></div>`;
  box.innerHTML =
    card('layers', '账号总数', total) +
    card('zap', '活跃账号', active) +
    card('coins', '总余额', fmtYuan(sum)) +
    card('shield-alert', '需关注', risk, risk ? 'warn' : '');
  refreshIcons();
}
// 金额格式化：千分位 + 两位小数
function fmtYuan(n) {
  return '¥' + Number(n || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ---------- 批量操作条 ----------
function renderBatch() {
  const n = state.selected.size;
  $('#batchBar').classList.toggle('show', n > 0); // CSS 控制浮动条显隐
  $('#batchCount').textContent = n ? `已选 ${n} 项` : '';
}
// 列表筛选/排序变化后的统一重绘
function rerenderList() {
  renderTable();
  renderCards();
}

// ---------- 请求封装（统一走 HttpOnly Cookie 鉴权，前端不接触任何 token） ----------
/* 原本地 api() 已改用 shared.js 的 apiFetch —— 保留两份会重复声明，整个模块加载即炸 */

// ---------- 数据加载 ----------
// 字段归一：兼容后端不同的命名风格，同时补齐渲染所需的派生字段
let inFlight = false; // 请求去重，避免轮询与手动刷新叠加
async function loadAccounts(showSkeleton = true) {
  if (inFlight) return;
  inFlight = true;
  if (showSkeleton) { // 静默刷新不动 state.loading，避免列表闪骨架屏
    state.loading = true;
    state.loadError = '';
    renderStats(); renderTable(); renderCards();
  }
  try {
    const data = await apiFetch('/api/pool');
    const list = Array.isArray(data) ? data : (data?.accounts ?? data?.items ?? []);
    state.accounts = list.map(normalizeAccount);
    // 余额口径统一走后端 stats：credits_total 只累加可用账号，
    // credits_total_all 才是含禁用/死号的全量。前端自己 reduce 会得到后者。
    state.stats = (data && !Array.isArray(data) && data.stats) || {};
    // k3 原版把 maxBal/balTier 写成死值（maxBal:1、balTier:'none'），注释说"统一在 loadPool 中
    // 计算"但该函数并不存在，导致余额进度条恒满/恒空、"余额筛选"下拉永久失效。此处补上计算。
    const maxBal = Math.max(1, ...state.accounts.map(a => a.balance ?? 0));
    state.accounts.forEach(a => {
      a.maxBal = maxBal;
      if (a.balance == null) a.balTier = 'none';
      else if (a.balance <= 0) a.balTier = 'bad';            // 已耗尽（对应筛选 value=bad）
      else if (a.balance < maxBal * 0.25) a.balTier = 'warn'; // 余额偏低（value=warn）
      else a.balTier = 'ok';                                 // 余额充足（value=ok）
    });
    state.loadError = '';
    // 清理已不存在账号的选中态，防止批量条残留
    const phones = new Set(state.accounts.map(a => a.phone));
    [...state.selected].forEach(p => { if (!phones.has(p)) state.selected.delete(p); });
  } catch (e) {
    state.loadError = e.message || '网络异常，请稍后重试';
  } finally {
    state.loading = false;
    inFlight = false;
    renderStats(); renderTable(); renderCards(); renderBatch();
  }
}

// ---------- 调度策略 ----------
const ROT_LABEL = { round_robin: '轮询', random: '随机', balance: '余额优先', least_used: '最少使用', weight: '加权' };
let rotOptions = ['round_robin', 'random', 'balance'].map(k => ({ key: k, label: ROT_LABEL[k] }));
function renderRotation(current) {
  if (current) state.rotation = current;
  $('#rotSeg').innerHTML = rotOptions.map(o =>
    `<button class="seg-item ${o.key === state.rotation ? 'on' : ''}" type="button" data-rot="${escapeHtml(o.key)}">${escapeHtml(o.label)}</button>`).join('');
}
async function loadRotation() {
  try {
    const d = await apiFetch('/api/pool/rotation');
    const opts = Array.isArray(d?.strategies) ? d.strategies : (Array.isArray(d?.options) ? d.options : null);
    if (opts && opts.length) { // 后端若返回可选策略列表则以其为准
      rotOptions = opts.map(s => typeof s === 'string'
        ? { key: s, label: ROT_LABEL[s] || s }
        : { key: s.key ?? s.value, label: (s.label ?? s.name) ?? (ROT_LABEL[s.key] || s.key) });
    }
    renderRotation(typeof d === 'string' ? d : (d?.strategy ?? d?.current ?? d?.rotation ?? state.rotation));
  } catch { renderRotation(state.rotation); /* 拉取失败不阻塞页面，展示默认值 */ }
}
async function setRotation(key) {
  if (!key || key === state.rotation) return;
  const prev = state.rotation;
  renderRotation(key); // 乐观更新，失败时回滚
  try {
    await apiFetch('/api/pool/rotation', { method: 'POST', body: { strategy: key } });
    const label = (rotOptions.find(o => o.key === key) || {}).label || key;
    toast(`调度策略已切换为「${label}」`);
  } catch (e) {
    renderRotation(prev);
    toast(e.message || '策略更新失败', 'err');
  }
}

// ---------- 弹窗基础 ----------
function openModal(id) {
  const m = $('#' + id);
  if (!m) return;
  m.hidden = false;
  requestAnimationFrame(() => m.classList.add('open'));
}
function closeModal(id) {
  const m = $('#' + id);
  if (!m) return;
  m.classList.remove('open');
  m.hidden = true;
}

// ---------- 二次确认弹窗（危险操作专用，勾选后才可执行） ----------
let cfResolve = null;
function openConfirm({ title, desc, checkLabel, okText } = {}) {
  $('#cfTitle').textContent = title || '确认操作';
  $('#cfDesc').textContent = desc || '';
  $('#cfCheckLabel').textContent = checkLabel || '我已了解此操作的后果';
  $('#cfOk').querySelector('span').textContent = okText || '确认执行';
  $('#cfCheck').checked = false;
  $('#cfOk').disabled = true;
  openModal('confirmModal');
  return new Promise(res => { cfResolve = res; });
}
function closeConfirm(result) {
  if (!cfResolve) return;
  closeModal('confirmModal');
  const r = cfResolve;
  cfResolve = null;
  r(result);
}

// ---------- Toast 通知 ----------
function toast(msg, type = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<i data-lucide="${type === 'err' ? 'circle-x' : 'circle-check'}"></i><span>${escapeHtml(msg)}</span>`;
  $('#toasts').appendChild(el);
  refreshIcons();
  setTimeout(() => el.classList.add('out'), 3000);
  setTimeout(() => el.remove(), 3500);
}

// ---------- 按钮忙态 ----------
async function withBusy(btn, fn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add('busy');
  try { await fn(); } finally { btn.disabled = false; btn.classList.remove('busy'); }
}

// ---------- 单账号操作（与 opsHtml 约定的 data-act / data-phone 属性对接） ----------
async function handleRowAction(act, phone, btn) {
  const acc = state.accounts.find(a => a.phone === phone);
  if (btn) btn.disabled = true;
  try {
    if (act === 'checkin') { // 单个签到
      const r = await apiFetch('/api/pool/checkin_one', { method: 'POST', body: { phone } });
      toast((r && r.message) || `${phone} 签到成功`);
    } else if (act === 'token') { // 刷新 Token
      await apiFetch('/api/pool/refresh_token', { method: 'POST', body: { phone } });
      toast(`${phone} Token 已刷新`);
    } else if (act === 'toggle') { // 启用 / 禁用
      const next = acc && acc.statusKey === 'active' ? 'disabled' : 'active';
      await apiFetch('/api/pool/status', { method: 'POST', body: { phone, status: next } });
      toast(`${phone} 已${next === 'active' ? '启用' : '禁用'}`);
    } else if (act === 'remove') { // 移除：危险操作，先二次确认
      const okDel = await openConfirm({
        title: '移除账号',
        desc: `确定将 ${phone} 从账号池中移除吗？移除后网关将不再调度该账号，此操作不可恢复。`,
        okText: '确认移除',
      });
      if (!okDel) return;
      await apiFetch('/api/pool/remove', { method: 'POST', body: { phone } });
      state.selected.delete(phone);
      toast(`${phone} 已移除`);
    }
    await loadAccounts(false); // 操作后静默刷新列表
  } catch (e) {
    toast(e.message || '操作失败', 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------- 批量任务（逐个执行 + 进度弹窗反馈） ----------
const BATCH_DEFS = {
  checkin: { label: '批量签到', run: p => apiFetch('/api/pool/checkin_one', { method: 'POST', body: { phone: p } }) },
  token:   { label: '批量刷新 Token', run: p => apiFetch('/api/pool/refresh_token', { method: 'POST', body: { phone: p } }) },
  enable:  { label: '批量启用', run: p => apiFetch('/api/pool/status', { method: 'POST', body: { phone: p, status: 'active' } }) },
  disable: { label: '批量禁用', run: p => apiFetch('/api/pool/status', { method: 'POST', body: { phone: p, status: 'disabled' } }) },
  remove:  { label: '批量移除', run: p => apiFetch('/api/pool/remove', { method: 'POST', body: { phone: p } }) },
};
function pgItemHtml(p) {
  return `<li data-phone="${escapeHtml(p)}"><span class="pg-ic"><i data-lucide="clock"></i></span><span class="pg-phone">${escapeHtml(p)}</span><em class="pg-st">等待</em></li>`;
}
function pgSetItem(li, phase, text) {
  if (!li) return;
  li.className = phase;
  const icon = phase === 'run' ? 'loader' : phase === 'ok' ? 'circle-check' : 'circle-x';
  li.querySelector('.pg-ic').innerHTML = `<i data-lucide="${icon}"></i>`;
  li.querySelector('.pg-st').textContent = text;
  refreshIcons();
}
async function runBatch(action) {
  const def = BATCH_DEFS[action];
  const phones = [...state.selected];
  if (!def || !phones.length) return;
  $('#pgTitle').textContent = def.label;
  $('#pgList').innerHTML = phones.map(pgItemHtml).join('');
  const closeBtn = $('#pgClose');
  closeBtn.disabled = true;
  closeBtn.textContent = '执行中…';
  const total = phones.length;
  let done = 0, ok = 0;
  const updateBar = () => {
    $('#pgFill').style.width = `${(done / total) * 100}%`;
    $('#pgMeta').textContent = `共 ${total} 项 · 已完成 ${done} · 成功 ${ok} · 失败 ${done - ok}`;
  };
  updateBar();
  openModal('progressModal');
  refreshIcons();
  for (const p of phones) { // 串行执行，避免对后端造成突发压力
    const li = $('#pgList').querySelector(`li[data-phone="${CSS.escape(p)}"]`);
    pgSetItem(li, 'run', '执行中');
    try {
      await def.run(p);
      ok++;
      pgSetItem(li, 'ok', '成功');
    } catch (e) {
      pgSetItem(li, 'err', e.message || '失败');
    }
    done++;
    updateBar();
  }
  closeBtn.disabled = false;
  closeBtn.textContent = '完成';
  state.selected.clear();
  renderBatch();
  toast(`${def.label}完成：成功 ${ok} / ${total}`, ok === total ? 'ok' : 'err');
  await loadAccounts(false);
}

// ---------- 排序 ----------
function applySort(key, dir, syncSelect = true) {
  state.sort = { key, dir };
  if (syncSelect) { // 表头点击排序时同步右侧排序下拉框
    const v = `${key}:${dir}`;
    const sel = $('#sortSel');
    if ([...sel.options].some(o => o.value === v)) sel.value = v;
  }
  rerenderList();
}

// ---------- 事件绑定 ----------
function bindEvents() {
  // 表头排序（事件委托，innerHTML 重绘后依然生效）
  $('#poolHead').addEventListener('click', e => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const key = th.dataset.sort;
    applySort(key, state.sort.key === key ? -state.sort.dir : 1);
  });
  // 行内操作 + 空状态重试（表格与卡片流共用处理函数）
  const onOpsClick = e => {
    if (e.target.closest('#retryLoad')) { loadAccounts(true); return; }
    const btn = e.target.closest('[data-act],[data-op]');
    if (!btn || !btn.dataset.phone) return;
    handleRowAction(btn.dataset.act || btn.dataset.op, btn.dataset.phone, btn);
  };
  // 行勾选：更新全选半选态与批量条，不做整体重绘
  const onCheckChange = e => {
    const cb = e.target.closest('input[type="checkbox"][data-phone]');
    if (!cb) return;
    if (cb.checked) state.selected.add(cb.dataset.phone);
    else state.selected.delete(cb.dataset.phone);
    renderThead();
    renderBatch();
  };
  $('#poolBody').addEventListener('click', onOpsClick);
  $('#poolBody').addEventListener('change', onCheckChange);
  $('#poolCards').addEventListener('click', onOpsClick);
  $('#poolCards').addEventListener('change', onCheckChange);

  // 搜索（防抖 200ms）
  let qTimer = null;
  $('#q').addEventListener('input', e => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.q = e.target.value.trim(); rerenderList(); }, 200);
  });
  // 状态筛选 chips
  $('#statusChips').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    [...$('#statusChips').children].forEach(c => c.classList.toggle('on', c === chip));
    state.status = chip.dataset.v;
    rerenderList();
  });
  // 余额筛选 / 排序下拉：先把原生 select 换成自绘组件（原生的展开层是系统
  // 绘制的，深色页面里点开是白底），再绑事件 —— 组件的 change 从根节点派发，
  // e.target.value 与原生语义一致。
  selectify($('#balFilter'), { width: '132px' });
  selectify($('#sortSel'), { width: '158px' });
  $('#balFilter').addEventListener('change', e => { state.balance = e.target.value; rerenderList(); });
  $('#sortSel').addEventListener('change', e => {
    const [key, dir] = e.target.value.split(':');
    applySort(key, Number(dir), false);
  });

  // 调度策略切换
  $('#rotSeg').addEventListener('click', e => {
    const b = e.target.closest('[data-rot]');
    if (b) setRotation(b.dataset.rot);
  });

  // 批量操作条
  $('#batchBar').addEventListener('click', async e => {
    if (e.target.closest('#batchClear')) {
      state.selected.clear();
      renderTable(); renderCards(); renderBatch();
      return;
    }
    const b = e.target.closest('[data-batch]');
    if (!b) return;
    const act = b.dataset.batch;
    if (act === 'remove') { // 批量移除属危险操作，先二次确认
      const okDel = await openConfirm({
        title: '批量移除',
        desc: `确定移除选中的 ${state.selected.size} 个账号吗？移除后不可恢复。`,
        okText: '确认移除',
      });
      if (!okDel) return;
    }
    runBatch(act);
  });

  // 页头全局操作
  $('#btnReload').addEventListener('click', () => loadAccounts(true));
  $('#btnRefreshBal').addEventListener('click', () => withBusy($('#btnRefreshBal'), async () => {
    try {
      await apiFetch('/api/pool/refresh_balance', { method: 'POST', body: {} });
      toast('余额已刷新');
      await loadAccounts(false);
    } catch (err) { toast(err.message || '刷新余额失败', 'err'); }
  }));
  $('#btnCheckinAll').addEventListener('click', () => withBusy($('#btnCheckinAll'), async () => {
    try {
      const r = await apiFetch('/api/pool/checkin', { method: 'POST', body: {} });
      toast((r && r.message) || '一键签到完成');
      await loadAccounts(false);
    } catch (err) { toast(err.message || '一键签到失败', 'err'); }
  }));
  $('#btnImport').addEventListener('click', () => {
    $('#importForm').reset();
    $('#importError').hidden = true;
    openModal('importModal');
    $('#impPhone').focus();
  });

  // 导入表单提交
  $('#importForm').addEventListener('submit', e => {
    e.preventDefault();
    const errEl = $('#importError');
    const showErr = msg => { errEl.textContent = msg || ''; errEl.hidden = !msg; };
    const phone = $('#impPhone').value.trim();
    const label = $('#impLabel').value.trim();
    const accessToken = $('#impAccess').value.trim();
    const refreshToken = $('#impRefresh').value.trim();
    if (!/^1[3-9]\d{9}$/.test(phone)) return showErr('请输入有效的 11 位手机号');
    if (!accessToken || !refreshToken) return showErr('Access Token 与 Refresh Token 均不能为空');
    showErr('');
    withBusy($('#importSubmit'), async () => {
      try {
        await apiFetch('/api/pool/import', {
          method: 'POST',
          body: { phone, label, access_token: accessToken, refresh_token: refreshToken },
        });
        closeModal('importModal');
        $('#importForm').reset();
        toast(`账号 ${phone} 导入成功`);
        await loadAccounts(false);
      } catch (err) {
        showErr(err.message || '导入失败，请稍后重试');
      }
    });
  });

  // 弹窗关闭（遮罩 / 关闭按钮统一走 data-close）
  $('#poolPage').addEventListener('click', e => {
    const c = e.target.closest('[data-close]');
    if (c) closeModal(c.dataset.close);
  });
  // 二次确认弹窗交互
  $('#cfCheck').addEventListener('change', e => { $('#cfOk').disabled = !e.target.checked; });
  $('#cfCancel').addEventListener('click', () => closeConfirm(false));
  $('#cfOk').addEventListener('click', () => closeConfirm(true));
  // 进度弹窗关闭（执行中按钮禁用，天然阻止误关）
  $('#pgClose').addEventListener('click', () => closeModal('progressModal'));
  // Esc 快捷关闭
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!$('#importModal').hidden) closeModal('importModal');
    else if (!$('#confirmModal').hidden) closeConfirm(false);
    else if (!$('#progressModal').hidden && !$('#pgClose').disabled) closeModal('progressModal');
  });
}

// ---------- 轮询刷新（60s，页面隐藏时暂停；回到前台立即补一次） ----------
let stopPoll = null;
function startPolling() {
  // 用 shared 的 poll：页面隐藏自动暂停，unmount 时能真正停掉。
  // 原来的裸 setInterval 在切页后会继续跑，多切几次就是每秒好几个请求。
  stopPoll = poll(() => loadAccounts(false), 60 * 1000);
}

// ---------- 初始化 ----------
async function init() {
  // 兜底默认值，防止 state 上字段缺失
  state.q = state.q ?? '';
  state.status = state.status ?? 'all';
  state.balance = state.balance ?? 'all';
  state.rotation = state.rotation || 'round_robin';
  bindEvents();
  renderRotation(state.rotation);
  renderStats(); renderTable(); renderCards(); renderBatch();
  refreshIcons(); // 渲染页头等静态区域图标
  startPolling();
  await Promise.all([loadAccounts(true), loadRotation()]);
}


// ---------------------------------------------------------------- 页面外壳
const TEMPLATE = `<section class="pool-page" id="poolPage">

  <!-- 页头：标题 + 全局操作 -->
  <header class="pool-head">
    <div class="ph-text">
      <h1>账号池</h1>
      <p>管理接入账号的余额、签到、Token 与调度策略</p>
    </div>
    <div class="ph-actions">
      <button class="btn" id="btnReload" type="button"><i data-lucide="refresh-cw"></i><span>刷新</span></button>
      <button class="btn" id="btnRefreshBal" type="button"><i data-lucide="wallet"></i><span>刷新余额</span></button>
      <button class="btn" id="btnCheckinAll" type="button"><i data-lucide="calendar-check"></i><span>一键签到</span></button>
      <button class="btn btn-primary" id="btnImport" type="button"><i data-lucide="plus"></i><span>导入账号</span></button>
    </div>
  </header>

  <!-- 概览统计（JS 渲染） -->
  <div class="pool-stats" id="poolStats"></div>

  <!-- 调度策略（GET/POST /api/pool/rotation） -->
  <div class="rot-card">
    <div class="rot-info">
      <span class="rot-ic"><i data-lucide="shuffle"></i></span>
      <div>
        <h3>调度策略</h3>
        <p>决定网关调用时账号的选取顺序</p>
      </div>
    </div>
    <div class="seg" id="rotSeg"></div>
  </div>

  <!-- 工具栏：搜索 / 状态筛选 / 余额筛选 / 排序 -->
  <div class="pool-toolbar">
    <label class="search">
      <i data-lucide="search"></i>
      <input id="q" type="search" placeholder="搜索手机号或备注…" autocomplete="off" />
    </label>
    <div class="chips" id="statusChips">
      <button class="chip on" type="button" data-v="all">全部</button>
      <button class="chip" type="button" data-v="active">正常</button>
      <button class="chip" type="button" data-v="disabled">已禁用</button>
      <button class="chip" type="button" data-v="error">异常</button>
    </div>
    <div class="toolbar-selects">
      <select id="balFilter" aria-label="余额筛选">
        <option value="all">全部余额</option>
        <option value="ok">余额充足</option>
        <option value="warn">余额偏低</option>
        <option value="bad">已耗尽</option>
      </select>
      <select id="sortSel" aria-label="排序方式">
        <option value="balance:-1">余额从高到低</option>
        <option value="balance:1">余额从低到高</option>
        <option value="expiry:1">有效期临近优先</option>
        <option value="expiry:-1">有效期充足优先</option>
        <option value="checkin:-1">最近签到优先</option>
        <option value="phone:1">按手机号</option>
      </select>
    </div>
  </div>

  <!-- 桌面端表格（JS 渲染 thead/tbody） -->
  <div class="pool-table-wrap" id="tableWrap">
    <table class="pool-table">
      <thead id="poolHead"></thead>
      <tbody id="poolBody"></tbody>
    </table>
  </div>

  <!-- 移动端卡片流（JS 渲染） -->
  <div class="pool-cards" id="poolCards"></div>

  <!-- 批量操作浮动条 -->
  <div class="batch-bar" id="batchBar">
    <span class="batch-count" id="batchCount"></span>
    <div class="batch-ops">
      <button class="btn sm" type="button" data-batch="checkin"><i data-lucide="calendar-check"></i><span>签到</span></button>
      <button class="btn sm" type="button" data-batch="token"><i data-lucide="rotate-ccw"></i><span>Token</span></button>
      <button class="btn sm" type="button" data-batch="enable"><i data-lucide="power"></i><span>启用</span></button>
      <button class="btn sm" type="button" data-batch="disable"><i data-lucide="power"></i><span>禁用</span></button>
      <button class="btn sm danger" type="button" data-batch="remove"><i data-lucide="trash-2"></i><span>移除</span></button>
      <button class="icon-btn" id="batchClear" type="button" aria-label="取消选择"><i data-lucide="x"></i></button>
    </div>
  </div>

  <!-- 导入账号弹窗（POST /api/pool/import） -->
  <div class="modal" id="importModal" hidden>
    <div class="modal-mask" data-close="importModal"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="importTitle">
      <header class="m-head">
        <h3 id="importTitle">导入账号</h3>
        <button class="icon-btn" type="button" data-close="importModal" aria-label="关闭"><i data-lucide="x"></i></button>
      </header>
      <form id="importForm" novalidate>
        <div class="field">
          <label for="impPhone">手机号 <b>*</b></label>
          <input id="impPhone" name="phone" type="text" inputmode="tel" placeholder="例如 13800001234" required />
        </div>
        <div class="field">
          <label for="impLabel">备注</label>
          <input id="impLabel" name="label" type="text" placeholder="可选，便于识别" />
        </div>
        <div class="field">
          <label for="impAccess">Access Token <b>*</b></label>
          <textarea id="impAccess" name="access_token" rows="2" spellcheck="false" placeholder="粘贴 access_token" required></textarea>
        </div>
        <div class="field">
          <label for="impRefresh">Refresh Token <b>*</b></label>
          <textarea id="impRefresh" name="refresh_token" rows="2" spellcheck="false" placeholder="粘贴 refresh_token" required></textarea>
        </div>
        <p class="form-error" id="importError" hidden></p>
        <footer class="m-foot">
          <button class="btn" type="button" data-close="importModal">取消</button>
          <button class="btn btn-primary" id="importSubmit" type="submit"><i data-lucide="plus"></i><span>导入</span></button>
        </footer>
      </form>
    </div>
  </div>

  <!-- 二次确认弹窗（危险操作专用） -->
  <div class="modal" id="confirmModal" hidden>
    <div class="modal-mask"></div>
    <div class="modal-card narrow" role="alertdialog" aria-modal="true" aria-labelledby="cfTitle">
      <header class="m-head">
        <span class="danger-badge"><i data-lucide="shield-alert"></i></span>
        <h3 id="cfTitle">确认操作</h3>
      </header>
      <p class="cf-desc" id="cfDesc"></p>
      <label class="cf-check">
        <input type="checkbox" id="cfCheck" />
        <span id="cfCheckLabel">我已了解此操作的后果</span>
      </label>
      <footer class="m-foot">
        <button class="btn" type="button" id="cfCancel">取消</button>
        <button class="btn btn-danger" type="button" id="cfOk" disabled><i data-lucide="trash-2"></i><span>确认执行</span></button>
      </footer>
    </div>
  </div>

  <!-- 批量任务进度弹窗 -->
  <div class="modal" id="progressModal" hidden>
    <div class="modal-mask"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="pgTitle">
      <header class="m-head">
        <h3 id="pgTitle">批量任务</h3>
      </header>
      <div class="pg-bar" id="pgBar"><span id="pgFill"></span></div>
      <div class="pg-meta" id="pgMeta"></div>
      <ul class="pg-list" id="pgList"></ul>
      <footer class="m-foot">
        <button class="btn btn-primary" type="button" id="pgClose" disabled>执行中…</button>
      </footer>
    </div>
  </div>

  <!-- 全局通知（成功/失败反馈） -->
  <div class="toasts" id="toasts" aria-live="polite"></div>

</section>`;

export async function mount(root) {
  root.innerHTML = TEMPLATE;

  // 兜底默认值，防止 state 上字段缺失
  state.q = state.q ?? '';
  state.status = state.status ?? 'all';
  state.balance = state.balance ?? 'all';
  state.rotation = state.rotation || 'round_robin';

  bindEvents();
  renderRotation(state.rotation);
  renderStats(); renderTable(); renderCards(); renderBatch();
  refreshIcons();
  startPolling();
  await Promise.all([loadAccounts(true), loadRotation()]);
}

export function unmount() {
  if (stopPoll) { stopPoll(); stopPoll = null; }
  // 选中状态是页面局部的，切走后清掉，避免回来时批量条显示上次的残留数字
  state.sel = new Set();
}