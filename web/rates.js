/* ==========================================================================
   rates.js —— 「模型与倍率」页（路由 id: rates）
   整页只有一张「可用模型」卡片网格：数据来自上游官方 console 模型接口
   （GET /api/models），倍率以 tag 形式展示，不做手动实测。
   导出 mountRates(root) / unmountRates()，模块内只用 root.querySelector。
   ========================================================================== */

import {
  el,
  apiFetch,
  toast,
  escapeHtml,
  skeleton,
  errorState,
  refreshIcons,
  cmpBy,
} from './shared.js';
import { dropdown } from './dropdown.js';

/* 数据来源药丸：未知 source 回落显示原字符串 */
const SRC_LABEL = {
  upstream_sync: '官方接口',
  console_api: '官方接口',
  cache: '本地缓存',
  static: '静态兜底',
};
const SRC_ICON = {
  upstream_sync: 'cloud-download',
  console_api: 'cloud-download',
  cache: 'database',
  static: 'hard-drive',
};
const pillClass = src =>
  src === 'upstream_sync' || src === 'console_api' ? 'console_api'
    : src === 'static' ? 'static'
      : '';

/* 能力筛选 chip：key -> 图标 / 文案 / details 字段 */
const CAPS = [
  ['images', 'image', '图像', 'supports_images'],
  ['tool', 'wrench', '工具', 'supports_tool_call'],
  ['reasoning', 'brain', '推理', 'supports_reasoning'],
];
const CAP_FIELD = Object.fromEntries(CAPS.map(c => [c[0], c[3]]));

/* 1048576 直接 /1e6 会显示 "1.048576M"（旧版就是这么写的，旧数据里没有
 * 百万级 ctx 所以没暴露）。保留一位小数，整数不拖 ".0"。 */
const fmtCtx = (n) => {
  if (!n) return '';
  if (n < 1e6) return Math.round(n / 1000) + 'K';
  const m = n / 1048576 >= 1 && n % 1048576 === 0 ? n / 1048576 : n / 1e6;
  return (Math.round(m * 10) / 10).toString().replace(/\.0$/, '') + 'M';
};

/* credits 形如 "x0.79" / "x0.00 credits"：照旧版去掉尾巴 */
const creditText = s => String(s || '').replace(/\s*credits?$/i, '').trim();
const creditVal = x => {
  const n = parseFloat(creditText(x.credits).replace(/^x/i, ''));
  return Number.isFinite(n) ? n : -1;
};

const byId = (a, b) => String(a.id).localeCompare(String(b.id));
const cmpRate = desc => (a, b) => {
  const ra = creditVal(a);
  const rb = creditVal(b);
  if (ra < 0 && rb < 0) return byId(a, b); /* 都缺倍率 → 按名称 */
  if (ra < 0) return 1;                    /* 缺倍率的永远排最后 */
  if (rb < 0) return -1;
  return (desc ? rb - ra : ra - rb) || byId(a, b);
};
const SORTS = {
  'rate-desc': cmpRate(true),
  'rate-asc': cmpRate(false),
  'ctx-desc': (a, b) => ((b.ctx || 0) - (a.ctx || 0)) || byId(a, b),
  name: cmpBy('id'),
};

/* ---------- 模块级状态（每次 mount 重置） ---------- */
let root = null;
let refs = {};
let rows = [];          // details[]
let modelIds = [];      // models[]
let q = '';
let sortKey = 'rate-desc';
let capSel = new Set();
let mountSeq = 0;       // 卸载后使在途请求回调作废
let stopFns = [];       // 定时器 / 轮询的 stop 函数（本页暂无，保留清理入口）

export function mountRates(rootEl) {
  unmountRates(); /* 防重复挂载 */
  root = rootEl;
  root.classList.add('page', 'page-rates');
  const token = ++mountSeq;
  q = '';
  sortKey = 'rate-desc';
  capSel = new Set();
  rows = [];
  modelIds = [];

  /* 页面标题区 + 两个动作按钮 */
  const btnReload = el('button', {
    class: 'btn ghost', type: 'button',
    html: '<i data-lucide="refresh-cw"></i><span>读取缓存</span>',
    onclick: () => load(false, token),
  });
  const btnSync = el('button', {
    class: 'btn primary', type: 'button',
    html: '<i data-lucide="cloud-download"></i><span>同步上游</span>',
    onclick: () => sync(token),
  });
  const head = el('div', { class: 'page-h' });
  head.appendChild(el('div', {
    html: '<h1>模型与倍率</h1><p>官方 console 接口直读的账号可用模型与积分倍率，无需手动实测</p>',
  }));
  const acts = el('div', { class: 'acts' });
  acts.append(btnReload, btnSync);
  head.appendChild(acts);

  /* 卡片：状态条 */
  const stateBox = el('div', { class: 'model-state' });

  /* 工具条：搜索框 */
  const search = el('input', {
    class: 'inp', type: 'search',
    placeholder: '搜索模型 id / 名称 / 描述…', 'aria-label': '搜索模型',
  });
  search.addEventListener('input', () => { q = search.value; renderGrid(); });
  const searchWrap = el('div', { class: 'rates-search', html: '<i data-lucide="search"></i>' });
  searchWrap.appendChild(search);

  /* 工具条：排序下拉 */
  // 原生 select 的展开列表由系统绘制、CSS 管不到，深色页面上必然白底黑字，
  // 所以这里用自绘的 dropdown 组件（API 对齐：value / change）。
  const sortSel = dropdown([
    { value: 'rate-desc', label: '倍率降序' },
    { value: 'rate-asc', label: '倍率升序' },
    { value: 'ctx-desc', label: '上下文降序' },
    { value: 'name', label: '名称' },
  ], { value: sortKey, ariaLabel: '排序方式', width: '150px' });
  sortSel.classList.add('rates-sort');
  sortSel.addEventListener('change', () => { sortKey = sortSel.value; renderGrid(); });

  /* 工具条：能力筛选 chip（多选切换） */
  const chips = el('div', { class: 'rates-chips' });
  for (const [key, icon, label] of CAPS) {
    const chip = el('button', {
      class: 'chip', type: 'button',
      html: `<i data-lucide="${icon}"></i>${label}`,
      onclick: () => {
        if (capSel.has(key)) capSel.delete(key); else capSel.add(key);
        chip.classList.toggle('on', capSel.has(key));
        renderGrid();
      },
    });
    chips.appendChild(chip);
  }

  const count = el('span', { class: 'rates-count dim' });
  const bar = el('div', { class: 'rates-bar' });
  bar.append(searchWrap, sortSel, chips, count);

  /* 卡片：网格 */
  const grid = el('div', { class: 'model-grid' });
  const card = el('div', { class: 'card' });
  card.appendChild(el('div', {
    class: 'card-h',
    html: '<div><h3><i data-lucide="boxes"></i>可用模型</h3>' +
      '<p>直接读官方 console 模型接口（<code class="mono">/console/enterprises/personal/models</code> 的 CLI agent 白名单），拿到的就是账号真实可用的模型与积分倍率。缓存 1 小时，失败 5 分钟内不重试并回落静态表，每 6 小时自动刷新。</p></div>',
  }));
  card.append(stateBox, bar, grid);
  root.append(head, card);

  refs = { stateBox, grid, count, btnReload, btnSync };
  refreshIcons(root);
  load(true, token);
}

export function unmountRates() {
  mountSeq += 1; /* 在途请求回调直接作废 */
  stopFns.forEach(stop => { try { stop(); } catch { /* ignore */ } });
  stopFns = [];
  if (root) {
    root.classList.remove('page-rates', 'page');
    root.innerHTML = '';
  }
  root = null;
  refs = {};
  rows = [];
  modelIds = [];
  capSel = new Set();
}

/* ---------- 数据流 ---------- */

async function load(initial, token) {
  if (initial) refs.grid.innerHTML = skeleton(6);
  setBusy(refs.btnReload, true, '读取中…');
  try {
    const d = await apiFetch('/api/models');
    if (token !== mountSeq) return;
    apply(d);
    if (d.error) toast(String(d.error), 'bad');
  } catch (e) {
    if (token !== mountSeq) return;
    if (rows.length) toast(e.message || '模型列表读取失败', 'bad'); /* 有旧数据则保留视图 */
    else showError(e);
  } finally {
    if (token === mountSeq) setBusy(refs.btnReload, false);
  }
}

async function sync(token) {
  setBusy(refs.btnSync, true, '同步中…');
  try {
    await apiFetch('/api/admin/sync-models', { method: 'POST' });
    const d = await apiFetch('/api/models'); /* 成功后重新加载列表 */
    if (token !== mountSeq) return;
    apply(d);
    const src = d.source || 'cache';
    toast(`已从${SRC_LABEL[src] || src}同步 ${modelIds.length} 个模型`, 'ok');
    if (d.error) toast(String(d.error), 'bad');
  } catch (e) {
    if (token !== mountSeq) return;
    toast(e.message || '同步失败', 'bad');
    if (!rows.length) showError(e);
  } finally {
    if (token === mountSeq) setBusy(refs.btnSync, false);
  }
}

function apply(d) {
  modelIds = d.models || [];
  rows = d.details || [];
  renderState(d);
  renderGrid();
}

function showError(e) {
  refs.stateBox.innerHTML = '';
  refs.grid.innerHTML = '';
  const cell = el('div', { class: 'rates-cell' });
  cell.appendChild(errorState(e.message || '读取失败', () => load(false, mountSeq)));
  refs.grid.appendChild(cell);
}

/* ---------- 渲染 ---------- */

function renderState(d) {
  const src = d.source || 'cache';
  const when = d.probed_at
    ? new Date(d.probed_at * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';
  refs.stateBox.innerHTML =
    `<span class="ms-pill ${pillClass(src)}"><i data-lucide="${SRC_ICON[src] || 'database'}"></i>${escapeHtml(SRC_LABEL[src] || src)}</span>` +
    `<span class="ms-item"><i data-lucide="boxes"></i>${(d.models || rows).length} 个模型</span>` +
    `<span class="ms-item"><i data-lucide="clock"></i>${escapeHtml(when)}</span>` +
    (d.error ? `<span class="ms-item err"><i data-lucide="triangle-alert"></i>${escapeHtml(String(d.error))}</span>` : '');
  refreshIcons(refs.stateBox);
}

function filtered() {
  const kw = q.trim().toLowerCase();
  const list = rows.filter(x => {
    if (kw && !`${x.id} ${x.name || ''} ${x.desc || ''}`.toLowerCase().includes(kw)) return false;
    for (const key of capSel) {
      if (!x[CAP_FIELD[key]]) return false;
    }
    return true;
  });
  return list.sort(SORTS[sortKey] || SORTS['rate-desc']);
}

function renderGrid() {
  const list = filtered();
  refs.count.textContent = list.length === rows.length
    ? `共 ${rows.length} 个`
    : `筛出 ${list.length} / ${rows.length}`;
  if (!rows.length) {
    refs.grid.innerHTML =
      '<div class="rates-cell empty"><i data-lucide="package-open"></i><p>没有模型数据，点「同步上游」</p></div>';
  } else if (!list.length) {
    refs.grid.innerHTML =
      '<div class="rates-cell empty"><i data-lucide="search-x"></i><p>没有匹配的模型，调整搜索词或能力筛选</p></div>';
  } else {
    refs.grid.innerHTML = list.map(cardHtml).join('');
  }
  refreshIcons(refs.grid);
}

function cardHtml(x) {
  const caps = [
    x.supports_reasoning ? ['brain', '推理'] : null,
    x.supports_images ? ['image', '图像'] : null,
    x.supports_tool_call ? ['wrench', '工具'] : null,
  ].filter(Boolean);
  const mult = creditText(x.credits);
  const desc = x.desc ? String(x.desc) : '';
  return `<div class="model-card ok">
    ${x.is_default ? '<span class="model-def">默认</span>' : ''}
    <div class="model-name">${escapeHtml(String(x.id))}</div>
    <div class="model-alias">${escapeHtml(String(x.name || x.id))}</div>
    <div class="model-tags">
      <span class="tag tier">${escapeHtml(String(x.vendor_label || 'codebuddy'))}</span>
      ${x.ctx ? `<span class="tag">${fmtCtx(x.ctx)} ctx</span>` : ''}
      ${x.max_output_tokens ? `<span class="tag">${fmtCtx(x.max_output_tokens)} out</span>` : ''}
      ${mult ? `<span class="tag rate">${escapeHtml(mult)}</span>` : ''}
      ${x.unlisted ? '<span class="tag unlisted" title="官方 console 模型表未列出，实测可正常调用">官方未列</span>' : ''}
    </div>
    ${caps.length ? `<div class="model-caps">${caps.map(([ic, t]) => `<span class="cap"><i data-lucide="${ic}"></i>${t}</span>`).join('')}</div>` : ''}
    ${desc ? `<div class="model-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</div>` : ''}
  </div>`;
}

/* 按钮加载态：禁用两个按钮防并发，图标旋转，结束后恢复文案 */
function setBusy(btn, busy, label) {
  const span = btn.querySelector('span');
  if (busy) {
    if (span && label) { btn.dataset.label = span.textContent; span.textContent = label; }
    btn.classList.add('busy');
    refs.btnReload.disabled = true;
    refs.btnSync.disabled = true;
  } else {
    if (span && btn.dataset.label) span.textContent = btn.dataset.label;
    btn.classList.remove('busy');
    refs.btnReload.disabled = false;
    refs.btnSync.disabled = false;
  }
}