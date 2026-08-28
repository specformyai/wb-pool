/* ============================================================
 * pages.js —— 由 k3 生成的页面模块合并产物
 *
 * 每个页面导出 mountXxx / unmountXxx，由 router.js 按需挂载。
 * 合并脚本已自动去除跨文件重复的顶层符号定义。
 * 请勿手工编辑此文件 —— 改动会在下次合并时被覆盖，
 * 要改请改 k3_pages/<part>.js 或合并脚本 merge_pages.py。
 * ============================================================ */

import {
  $, $$, apiFetch, confirmDialog, copyText, el, errorState, escapeHtml, fmtDur, fmtInt, fmtMoney, fmtTime, openModal, poll, refreshIcons, skeleton, toast,
} from '@/shared.js';
import { selectify } from '@/selectify.js';

/* ==================== keys-and-rates ==================== */
// 统一从 shared.js 复用工具，严禁在本文件重复实现

// —— 通用小工具 ——
// 名称/备注/prefix_note 都是可输入文本，进 innerHTML 前必须转义防 XSS
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

// POST 统一 JSON 封装，避免每个调用点重复 stringify + header
const post = (path, obj) => apiFetch(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(obj ?? {}),
});

// clipboard API 在非安全上下文会抛错，降级到 textarea + execCommand 兜底
/* copyText: 改用 shared.js 的实现 */
// 弹窗挂在页面 root 内而不是 body：CSS 选择器带页面根前缀，挂出去样式会失效
/* openModal: 改用 shared.js 的实现 */
/* ==================== 页面一：API Key 管理 ==================== */
let keysRoot = null;
let keysTimer = null;  // 轮询句柄：切页后必须清，否则请求泄漏
let keysModal = 0;     // 开着的弹窗数：弹窗期间暂停轮询，避免重绘打断输入

export function mountKeys(root) {
  keysRoot = root;
  root.classList.add('page-keys');
  root.innerHTML = `
    <div class="pk-top">
      <h2><i data-lucide="key-round"></i>API Key 管理</h2>
      <button class="x-btn pri" data-act="create"><i data-lucide="plus"></i>新建 Key</button>
    </div>
    <div class="pk-note" hidden></div>
    <div class="pk-stats"></div>
    <div class="pk-list"></div>`;
  $('.page-keys [data-act="create"]').addEventListener('click', openCreateKey);
  loadKeys();
  // 用量数字随调用实时变化，30s 轮询保持管理台数据新鲜
  keysTimer = setInterval(() => { if (!keysModal) loadKeys(true); }, 30000);
  refreshIcons();
}

export function unmountKeys() {
  if (keysTimer) { clearInterval(keysTimer); keysTimer = null; }
  if (keysRoot) keysRoot.classList.remove('page-keys'); // root 会被复用，残留 class 会让本页样式泄漏到别的页
  keysRoot = null;
}

async function loadKeys(silent) {
  const listEl = $('.page-keys .pk-list');
  if (!listEl) return; // 已切页，丢弃本次刷新
  if (!silent) { listEl.innerHTML = skeleton(3); refreshIcons(); }
  try {
    const d = await apiFetch('/api/keys');
    renderKeyStats(d.keys || []);
    renderKeyList(d.keys || []);
    const noteEl = $('.page-keys .pk-note');
    if (d.prefix_note) { // 调用方式提示，有内容才占版面
      noteEl.hidden = false;
      noteEl.innerHTML = `<i data-lucide="info"></i><span>${esc(d.prefix_note)}</span>`;
    }
    refreshIcons();
  } catch (e) {
    // 失败必须给页面内可重试的错误态，不能只 toast 完留白
    $('.page-keys .pk-stats').innerHTML = '';
    listEl.innerHTML = `<div class="x-err"><i data-lucide="cloud-off"></i>
      <p>加载失败：${esc(e.message)}</p>
      <button class="x-btn" data-act="retry"><i data-lucide="refresh-cw"></i>重试</button></div>`;
    listEl.querySelector('[data-act="retry"]').addEventListener('click', () => loadKeys());
    refreshIcons();
  }
}

function renderKeyStats(keys) {
  const sum = (f) => keys.reduce((s, k) => s + (k[f] || 0), 0);
  $('.page-keys .pk-stats').innerHTML = [
    ['key-round', 'Key 总数', fmtInt(keys.length)],
    ['toggle-right', '启用中', fmtInt(keys.filter((k) => k.enabled).length)],
    ['activity', '总请求数', fmtInt(sum('request_count'))],
    ['coins', '总消耗积分', fmtMoney(sum('credits'))],
  ].map(([ic, t, v]) => `<div class="pk-stat"><div class="k"><i data-lucide="${ic}"></i>${t}</div><div class="v">${v}</div></div>`).join('');
}

function renderKeyList(keys) {
  const listEl = $('.page-keys .pk-list');
  if (!keys.length) {
    listEl.innerHTML = `<div class="x-err"><i data-lucide="inbox"></i><p>还没有任何 Key，点击右上角「新建 Key」开始</p></div>`;
    return;
  }
  listEl.innerHTML = `<div class="pk-grid">${keys.map(keyCardHtml).join('')}</div>`;
  keys.forEach((k) => bindKeyCard(listEl, k));
}

function keyCardHtml(k) {
  const isEnv = k.source === 'env';
  // env Key 的禁用按钮保留 title 说明原因，所以 CSS 里不能给 disabled 加 pointer-events:none
  return `<div class="pk-card ${k.enabled ? '' : 'off'}" data-id="${esc(k.id)}">
    <div class="pk-c1">
      <span class="nm" title="${esc(k.name)}">${esc(k.name)}</span>
      ${isEnv ? '<span class="pk-env" title="由环境变量注入，请修改部署环境后重启生效">来自环境变量</span>' : ''}
    </div>
    <div class="pk-mask">${esc(k.masked)}</div>
    <div class="pk-nums">
      <div class="pk-num"><div class="n">${fmtInt(k.request_count)}</div><div class="t">请求数</div></div>
      <div class="pk-num"><div class="n">${fmtInt(k.tokens)}</div><div class="t">Tokens</div></div>
      <div class="pk-num"><div class="n">${fmtMoney(k.credits)}</div><div class="t">消耗积分</div></div>
    </div>
    <div class="pk-meta">
      <span>创建于 ${fmtTime(k.created_at)}</span>
      <span>最近使用 ${k.last_used ? fmtTime(k.last_used) : '从未使用'}</span>
    </div>
    ${k.note ? `<div class="pk-note2">${esc(k.note)}</div>` : ''}
    <div class="pk-acts">
      <button class="pk-sw ${k.enabled ? 'on' : ''}" data-act="toggle" title="${k.enabled ? '点击停用' : '点击启用'}"></button>
      <span class="sp"></span>
      <button class="x-btn sm" data-act="edit"><i data-lucide="pencil"></i>编辑</button>
      <button class="x-btn sm" data-act="rotate" ${isEnv ? 'disabled title="环境变量 Key 请在部署环境中轮换"' : ''}><i data-lucide="rotate-cw"></i>轮换</button>
      <button class="x-btn sm dan" data-act="del" ${isEnv ? 'disabled title="环境变量 Key 不可删除"' : ''}><i data-lucide="trash-2"></i></button>
    </div>
  </div>`;
}

function bindKeyCard(listEl, k) {
  const isEnv = k.source === 'env';
  const card = listEl.querySelector(`.pk-card[data-id="${CSS.escape(k.id)}"]`);
  if (!card) return;
  card.querySelector('[data-act="toggle"]').addEventListener('click', async () => {
    try {
      await post(`/api/keys/${k.id}/update`, { enabled: !k.enabled });
      toast(k.enabled ? '已停用' : '已启用', 'ok');
      loadKeys(true); // 不做乐观更新：直接重拉，失败时状态天然不会错位
    } catch (e) { toast(e.message, 'err'); }
  });
  card.querySelector('[data-act="edit"]').addEventListener('click', () => openEditKey(k));
  if (!isEnv) { // env 卡按钮已 disabled，无需再绑（disabled 按钮本身不触发 click）
    card.querySelector('[data-act="rotate"]').addEventListener('click', () => rotateKey(k));
    card.querySelector('[data-act="del"]').addEventListener('click', () => deleteKey(k));
  }
}

function openCreateKey() {
  keysModal++;
  const { box: back, close } = openModal(`
    <h3><i data-lucide="key-round"></i>新建 API Key</h3>
    <div class="x-sub">创建后完整 Key 只显示一次</div>
    <div class="x-field"><label>名称</label><input class="x-in" name="name" placeholder="例如：生产环境-后端服务" maxlength="64"></div>
    <div class="x-field"><label>备注（可选）</label><input class="x-in" name="note" placeholder="用途说明" maxlength="200"></div>
    <div class="x-acts">
      <button class="x-btn" data-act="cancel">取消</button>
      <button class="x-btn pri" data-act="ok"><i data-lucide="plus"></i>创建</button>
    </div>`, { scope: 'page-keys', onClose: () => keysModal-- });
  back.querySelector('[data-act="cancel"]').addEventListener('click', close);
  back.querySelector('[data-act="ok"]').addEventListener('click', async (e) => {
    const name = back.querySelector('[name="name"]').value.trim();
    if (!name) { toast('请填写名称', 'warn'); return; }
    const note = back.querySelector('[name="note"]').value.trim();
    e.currentTarget.disabled = true; // 防连点创建出多个 Key
    try {
      const r = await post('/api/keys', { name, note });
      close(); loadKeys(true);
      showKeyReveal('Key 创建成功', r.key); // 明文仅此一次返回，必须立刻醒目展示
    } catch (err) { toast(err.message, 'err'); e.currentTarget.disabled = false; }
  });
}

function openEditKey(k) {
  const isEnv = k.source === 'env';
  keysModal++;
  const { box: back, close } = openModal(`
    <h3><i data-lucide="pencil"></i>编辑 Key</h3>
    <div class="x-sub">${esc(k.masked)}</div>
    <div class="x-field"><label>名称</label>
      <input class="x-in" name="name" value="${esc(k.name)}" maxlength="64"
        ${isEnv ? 'disabled title="环境变量 Key 不可改名"' : ''}></div>
    <div class="x-field"><label>备注</label><input class="x-in" name="note" value="${esc(k.note || '')}" maxlength="200"></div>
    <div class="x-acts">
      <button class="x-btn" data-act="cancel">取消</button>
      <button class="x-btn pri" data-act="ok">保存</button>
    </div>`, { scope: 'page-keys', onClose: () => keysModal-- });
  back.querySelector('[data-act="cancel"]').addEventListener('click', close);
  back.querySelector('[data-act="ok"]').addEventListener('click', async (e) => {
    const body = { note: back.querySelector('[name="note"]').value.trim() };
    if (!isEnv) { // env Key 连 name 字段都不要提交，避免后端歧义
      const name = back.querySelector('[name="name"]').value.trim();
      if (!name) { toast('名称不能为空', 'warn'); return; }
      body.name = name;
    }
    e.currentTarget.disabled = true;
    try {
      await post(`/api/keys/${k.id}/update`, body);
      toast('已保存', 'ok'); close(); loadKeys(true);
    } catch (err) { toast(err.message, 'err'); e.currentTarget.disabled = false; }
  });
}

// 新建/轮换共用的一次性明文弹窗
function showKeyReveal(title, key) {
  keysModal++;
  const { box: back, close } = openModal(`
    <h3><i data-lucide="shield-check"></i>${esc(title)}</h3>
    <div class="x-sub">请立即复制并妥善保存</div>
    <div class="x-keyreveal">${esc(key)}</div>
    <div class="x-acts" style="margin-top:0;justify-content:flex-start">
      <button class="x-btn pri" data-act="copy"><i data-lucide="copy"></i>复制</button>
    </div>
    <div class="x-once"><i data-lucide="shield-alert"></i><span>完整 Key 只显示这一次，关闭后无法再次查看。</span></div>
    <div class="x-acts"><button class="x-btn" data-act="done">我已保存，关闭</button></div>`, { scope: 'page-keys', onClose: () => keysModal-- });
  back.querySelector('[data-act="copy"]').addEventListener('click', async () => {
    const ok = await copyText(key);
    toast(ok ? '已复制到剪贴板' : '复制失败，请手动全选复制', ok ? 'ok' : 'err');
  });
  back.querySelector('[data-act="done"]').addEventListener('click', close);
}

async function rotateKey(k) {
  // 轮换会让旧 Key 立即失效，属破坏性操作，先确认
  if (!(await confirmDialog('轮换 Key', `确定轮换「${k.name}」吗？旧 Key 将立即失效。`))) return;
  try {
    const r = await post(`/api/keys/${k.id}/rotate`);
    loadKeys(true);
    showKeyReveal('轮换成功', r.key);
  } catch (e) { toast(e.message, 'err'); }
}

async function deleteKey(k) {
  if (!(await confirmDialog('删除 Key', `确定删除「${k.name}」吗？使用该 Key 的调用将立即失败，且不可恢复。`))) return;
  try {
    await apiFetch(`/api/keys/${k.id}`, { method: 'DELETE' });
    toast('已删除', 'ok'); loadKeys(true);
  } catch (e) { toast(e.message, 'err'); }
}

/* ==================== 页面二：倍率表 ==================== */
let ratesRoot = null;
let ratesTimer = null;    // 轮询句柄
let measureTimer = null;  // 实测「已用时」计时句柄
let ratesModal = 0;
// 排序状态放模块级：重新拉数据后沿用用户选择
const ratesState = { rows: [], base: '', sort: 'multiplier', dir: -1 };

export function mountRates(root) {
  ratesRoot = root;
  root.classList.add('page-rates');
  root.innerHTML = `
    <div class="pr-top">
      <h2><i data-lucide="scale"></i>倍率表</h2>
      <div class="pr-acts">
        <button class="x-btn" data-act="measure"><i data-lucide="flask-conical"></i>实测倍率</button>
        <button class="x-btn dan" data-act="reset"><i data-lucide="trash-2"></i>重置统计</button>
      </div>
    </div>
    <div class="pr-note" hidden></div>
    <div class="pr-body"></div>`;
  $('.page-rates [data-act="measure"]').addEventListener('click', openMeasure);
  $('.page-rates [data-act="reset"]').addEventListener('click', resetRates);
  loadRates();
  ratesTimer = setInterval(() => { if (!ratesModal) loadRates(true); }, 30000);
  refreshIcons();
}

export function unmountRates() {
  // 两个定时器都必须清：轮询 + 实测计时，否则切页后持续泄漏
  if (ratesTimer) { clearInterval(ratesTimer); ratesTimer = null; }
  stopMeasureTimer();
  if (ratesRoot) ratesRoot.classList.remove('page-rates');
  ratesRoot = null;
}

function stopMeasureTimer() {
  if (measureTimer) { clearInterval(measureTimer); measureTimer = null; }
}

async function loadRates(silent) {
  const body = $('.page-rates .pr-body');
  if (!body) return;
  if (!silent) { body.innerHTML = skeleton(5); refreshIcons(); }
  try {
    const d = await apiFetch('/api/rates');
    ratesState.rows = d.rows || [];
    ratesState.base = d.base_model || '';
    // note 是「数值来自 usage 累计、非官方倍率」的准确性声明，必须常驻顶部
    const noteEl = $('.page-rates .pr-note');
    if (d.note) {
      noteEl.hidden = false;
      noteEl.innerHTML = `<i data-lucide="info"></i><span>${esc(d.note)}</span>`;
    }
    renderRates();
  } catch (e) {
    body.innerHTML = `<div class="x-err"><i data-lucide="cloud-off"></i>
      <p>加载失败：${esc(e.message)}</p>
      <button class="x-btn" data-act="retry"><i data-lucide="refresh-cw"></i>重试</button></div>`;
    body.querySelector('[data-act="retry"]').addEventListener('click', () => loadRates());
    refreshIcons();
  }
}

function renderRates() {
  const body = $('.page-rates .pr-body');
  const { rows, base, sort, dir } = ratesState;
  if (!rows.length) {
    body.innerHTML = `<div class="x-err"><i data-lucide="inbox"></i><p>暂无统计数据，跑些请求或点「实测倍率」后此处会有数据</p></div>`;
    refreshIcons(); return;
  }
  // credits_per_1k 为 null 即样本不足：永远沉底，不参与排序
  const valid = rows.filter((r) => r.credits_per_1k != null);
  const lack = rows.filter((r) => r.credits_per_1k == null);
  valid.sort((a, b) => ((a[sort] ?? 0) - (b[sort] ?? 0)) * dir);
  const maxMult = Math.max(...valid.map((r) => r.multiplier || 0), 1e-9); // 条形图相对最大值归一
  const head = [ // [字段, 表头文案, 是否可排序] —— 列严格对应后端返回字段
    ['model', '模型', false], ['multiplier', '倍率', true],
    ['credits_per_1k', '每 1k 积分', true], ['credits_per_request', '每请求积分', false],
    ['requests', '请求数', true], ['total_tokens', '总 Tokens', false],
    ['credits', '累计积分', false], ['last_seen', '最近使用', true],
  ];
  body.innerHTML = `<div class="pr-wrap"><table><thead><tr>${head.map(([k, t, s]) =>
    `<th class="${s ? 'sortable' : ''}" data-k="${k}">${t}${sort === k ? `<span class="arw">${dir === -1 ? '↓' : '↑'}</span>` : ''}</th>`).join('')}</tr></thead>
    <tbody>${[...valid, ...lack].map((r) => rateRowHtml(r, base, maxMult)).join('')}</tbody></table></div>`;
  $$('.page-rates th.sortable').forEach((th) => th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (ratesState.sort === k) ratesState.dir *= -1; // 同列再点翻转方向
    else { ratesState.sort = k; ratesState.dir = -1; } // 数值列默认降序更符合查大头的直觉
    renderRates();
  }));
  refreshIcons();
}

function rateRowHtml(r, base, maxMult) {
  const lack = r.credits_per_1k == null;
  const pct = Math.min(100, ((r.multiplier || 0) / maxMult) * 100);
  return `<tr class="${lack ? 'lack' : ''}">
    <td><div class="pr-model">${esc(r.model)}
      ${r.model === base ? '<span class="pr-badge base">基准 1×</span>' : ''}
      ${r.thinking_tokens > 0 ? '<span class="pr-badge thk" title="含思考 tokens：实际成本远高于 completion_tokens 体现的水平">含思考</span>' : ''}
    </div></td>
    <td><div class="pr-mult"><div class="pr-bar"><i style="width:${pct.toFixed(1)}%"></i></div><b>${r.multiplier != null ? `${Number(r.multiplier).toFixed(2)}×` : '—'}</b></div></td>
    <td>${lack ? '<span class="pr-lack">样本不足</span>' : fmtMoney(r.credits_per_1k)}</td>
    <td>${fmtMoney(r.credits_per_request)}</td>
    <td>${fmtInt(r.requests)}</td>
    <td>${fmtInt(r.total_tokens)}</td>
    <td>${fmtMoney(r.credits)}</td>
    <td>${r.last_seen ? fmtTime(r.last_seen) : '—'}</td>
  </tr>`;
}

function openMeasure() {
  ratesModal++;
  const models = [...new Set(ratesState.rows.map((r) => r.model))];
  // datalist：已有模型直接选，也允许输入表里还没出现的新模型 id
  const { box: back, close } = openModal(`
    <h3><i data-lucide="flask-conical"></i>实测倍率</h3>
    <div class="x-sub">向目标模型发起数轮真实请求，按 usage.credit 反推倍率</div>
    <div class="x-field"><label>模型</label>
      <input class="x-in" name="model" list="pr-models" placeholder="模型 ID" value="${esc(ratesState.base)}">
      <datalist id="pr-models">${models.map((m) => `<option value="${esc(m)}">`).join('')}</datalist></div>
    <div class="x-field"><label>轮数（越多越准，耗时也越长）</label>
      <input class="x-in" name="rounds" type="number" min="1" max="20" value="3"></div>
    <div class="pr-prog" hidden><span class="pr-spin"></span><span>实测中…已用时 <b class="t">0ms</b>，请勿关闭</span></div>
    <div class="x-acts">
      <button class="x-btn" data-act="cancel">取消</button>
      <button class="x-btn pri" data-act="go"><i data-lucide="play"></i>开始实测</button>
    </div>`, { scope: 'page-rates',
                onClose: () => { ratesModal--; stopMeasureTimer(); } });
  back.querySelector('[data-act="cancel"]').addEventListener('click', close);
  back.querySelector('[data-act="go"]').addEventListener('click', () => runMeasure(back, close));
}

async function runMeasure(back, close) {
  const model = back.querySelector('[name="model"]').value.trim();
  const rounds = Math.max(1, Math.min(20, parseInt(back.querySelector('[name="rounds"]').value, 10) || 3));
  if (!model) { toast('请填写模型 ID', 'warn'); return; }
  // 实测期间锁按钮：重复提交会产生并发请求污染统计样本
  back.querySelector('[data-act="go"]').disabled = true;
  back.querySelector('[data-act="cancel"]').disabled = true;
  const prog = back.querySelector('.pr-prog');
  prog.hidden = false;
  const t0 = Date.now();
  const tEl = prog.querySelector('.t');
  stopMeasureTimer();
  measureTimer = setInterval(() => { tEl.textContent = fmtDur(Date.now() - t0); }, 200);
  try {
    await post('/api/rates/measure', { model, rounds });
    toast('实测完成，数据已更新', 'ok');
    close(); loadRates(true);
  } catch (e) {
    toast(`实测失败：${e.message}`, 'err');
    back.querySelector('[data-act="go"]').disabled = false;
    back.querySelector('[data-act="cancel"]').disabled = false;
  } finally {
    stopMeasureTimer(); // 无论成败都停表，句柄也留给 unmount 兜底
  }
}

async function resetRates() {
  // 清空统计不可逆，必须二次确认
  if (!(await confirmDialog('重置统计', '确定清空全部倍率统计吗？已积累的样本将丢失。'))) return;
  try {
    await post('/api/rates/reset');
    toast('统计已清空', 'ok'); loadRates(true);
  } catch (e) { toast(e.message, 'err'); }
}

/* ==================== calls-and-proxy ==================== */
/* ---------- 本文件内部小工具（非 shared 已有能力，不重复定义） ---------- */
// 后端字段（模型名/错误文本）会拼进 innerHTML，统一转义防 XSS
/* esc: 与其他页面重复，改用首次定义 */
// fmtDur/fmtTime 未约定 null 行为，这里兜底成占位符
const dur = v => (v == null ? '—' : fmtDur(v));
const tm = v => (v ? fmtTime(v) : '—');
const pct = v => (v == null ? '—' : (v * 100).toFixed(1) + '%');
// 页内可重试错误态：硬性规则要求失败不能只 toast
const errBox = (msg, act) => `<div class="errbox"><i data-lucide="alert-triangle"></i><span>${esc(msg)}</span><button class="btn" data-act="${act}">重试</button></div>`;

/* ==================== 页面一：调用监控 ==================== */
// state 权重：bad 最前、idle 最后，让运维第一眼落在出问题的模型上
const ST_ORDER = { bad: 0, degraded: 1, ok: 2, idle: 3 };
const ST_LABEL = { ok: '正常', degraded: '波动', bad: '异常', idle: '空闲' };
// 窗口预设；buckets 即热条格数，7d 用 28 格（6h/格），168 格会密到看不清
const WINDOWS = [{ h: 1, b: 24, t: '1h' }, { h: 6, b: 24, t: '6h' }, { h: 24, b: 24, t: '24h' }, { h: 168, b: 28, t: '7d' }];

// win 放模块级：切页再回来保留用户上次选的窗口
const calls = { root: null, win: 2, timer: null, onVis: null };

export function mountCalls(root) {
  calls.root = root;
  root.classList.add('page-calls');
  root.innerHTML = `
    <div class="ph">
      <h2><i data-lucide="activity"></i>调用监控</h2>
      <div class="seg">${WINDOWS.map((w, i) => `<button data-act="win" data-i="${i}" class="${i === calls.win ? 'on' : ''}">${w.t}</button>`).join('')}</div>
      <span style="flex:1"></span>
      <button class="btn danger" data-act="reset"><i data-lucide="trash-2"></i>清空日志</button>
    </div>
    <div class="grid" id="cGrid">${skeleton(3)}</div>
    <div class="ph"><h2><i data-lucide="list"></i>最近调用</h2><span class="muted" id="cHint"></span></div>
    <div class="tblwrap" id="cRecent">${skeleton(5)}</div>`;
  refreshIcons();
  root.addEventListener('click', onCallsClick);
  // 页面不可见时轮询纯属浪费后端配额：隐藏即暂停，回来时立即补一次再恢复
  calls.onVis = () => { if (document.hidden) stopCallsPoll(); else { refreshCalls(true); startCallsPoll(); } };
  document.addEventListener('visibilitychange', calls.onVis);
  refreshCalls(false);
  startCallsPoll();
}

export function unmountCalls() {
  stopCallsPoll(); // 必须清，否则切页后 15s 轮询泄漏
  if (calls.onVis) document.removeEventListener('visibilitychange', calls.onVis);
  if (calls.root) { calls.root.removeEventListener('click', onCallsClick); calls.root.classList.remove('page-calls'); calls.root.innerHTML = ''; }
  calls.root = null;
}

function startCallsPoll() { stopCallsPoll(); calls.timer = setInterval(() => refreshCalls(true), 15000); }
function stopCallsPoll() { if (calls.timer) { clearInterval(calls.timer); calls.timer = null; } }
function refreshCalls(silent) { loadHealth(silent); loadRecent(silent); }

function onCallsClick(e) {
  const t = e.target.closest('[data-act]');
  if (!t) return;
  const act = t.dataset.act;
  if (act === 'win') { // 切窗口只影响健康卡，「最近调用」与窗口无关不重拉
    calls.win = +t.dataset.i;
    $$('.page-calls .seg button').forEach(b => b.classList.toggle('on', b === t));
    loadHealth(false);
  } else if (act === 'reset') resetCalls();
  else if (act === 'retry-h') loadHealth(false);
  else if (act === 'retry-r') loadRecent(false);
  else if (act === 'err') t.classList.toggle('open'); // 错误文本点击展开/收起
}

async function loadHealth(silent) {
  const grid = $('#cGrid');
  if (!grid) return;
  if (!silent) grid.innerHTML = skeleton(3); // 轮询走静默刷新，否则骨架屏每 15s 闪一次
  try {
    const w = WINDOWS[calls.win];
    const d = await apiFetch(`/api/calls/health?window_h=${w.h}&buckets=${w.b}`);
    renderHealth(grid, d.models || []);
    const hint = $('#cHint');
    if (hint) hint.textContent = '每 15s 自动刷新 · 更新于 ' + tm(d.generated_at);
  } catch (err) {
    grid.innerHTML = errBox('健康数据加载失败：' + err.message, 'retry-h');
    refreshIcons();
  }
}

function renderHealth(grid, models) {
  if (!models.length) { grid.innerHTML = '<div class="empty">窗口内暂无模型调用数据</div>'; return; }
  const sorted = [...models].sort((a, b) => ((ST_ORDER[a.state] ?? 9) - (ST_ORDER[b.state] ?? 9)) || (b.total - a.total));
  grid.innerHTML = sorted.map(m => `
    <div class="mcard s-${esc(m.state)}">
      <div class="mhead"><span class="mname">${esc(m.model)}</span><span class="badge s-${esc(m.state)}">${ST_LABEL[m.state] || esc(m.state)}</span></div>
      <div class="mmain"><span class="rate">${pct(m.rate)}</span><span class="msub">成功率 · 窗口内 ${fmtInt(m.total)} 次调用</span></div>
      <div class="mmeta">
        <span>p50 ${dur(m.p50_ms)}</span><span>p95 ${dur(m.p95_ms)}</span>
        <span>${fmtInt(m.tokens)} tok</span><span>${fmtMoney(m.credits)} 积分</span>
        <span>${fmtInt(m.accounts)} 账号</span><span>最近 ${tm(m.last_ts)}</span>
      </div>
      ${m.last_error && m.state !== 'ok' ? `<div class="merr" title="${esc(m.last_error)}">${esc(m.last_error)}</div>` : ''}
      <div class="hstrip">${(m.buckets || []).map(bk => `<i class="s-${esc(bk.state || 'idle')}" data-tip="成功 ${bk.ok} / 失败 ${bk.fail}"></i>`).join('')}</div>
    </div>`).join('');
}

async function loadRecent(silent) {
  const box = $('#cRecent');
  if (!box) return;
  if (!silent) box.innerHTML = skeleton(5);
  try {
    const d = await apiFetch('/api/calls/recent?limit=80');
    renderRecent(box, d.calls || []);
  } catch (err) {
    box.innerHTML = errBox('调用日志加载失败：' + err.message, 'retry-r');
    refreshIcons();
  }
}

function renderRecent(box, list) {
  if (!list.length) { box.innerHTML = '<div class="empty">暂无调用日志</div>'; return; }
  box.innerHTML = `<table>
    <thead><tr><th>时间</th><th>模型</th><th>账号</th><th>状态码</th><th>耗时</th><th>Tokens</th><th>积分</th><th>错误</th></tr></thead>
    <tbody>${list.map(c => `
      <tr class="${c.ok ? '' : 'fail'}">
        <td>${tm(c.ts)}</td><td>${esc(c.model)}</td><td>${esc(c.account ?? c.phone ?? '')}</td>
        <td class="${c.ok ? 'tok' : 'tbad'}">${c.code ?? c.status ?? '—'}</td>
        <td>${dur(c.ms)}</td><td>${fmtInt(c.tokens)}</td><td>${fmtMoney(c.credits)}</td>
        <td>${c.error ? `<span class="errtxt" data-act="err" title="点击展开">${esc(c.error)}</span>` : ''}</td>
      </tr>`).join('')}</tbody></table>`;
}

async function resetCalls() {
  if (!(await confirmDialog('清空调用日志', '将删除全部调用记录且不可恢复，确认继续？'))) return;
  try {
    await apiFetch('/api/calls/reset', { method: 'POST' });
    toast('日志已清空', 'ok');
    refreshCalls(true);
  } catch (err) { toast('清空失败：' + err.message, 'err'); }
}

/* ==================== 页面二：代理池 ==================== */
const MODE_LABEL = { off: '关闭', fixed: '固定', rotate: '轮换' };
const proxy = { root: null, data: null, probing: false, tick: null, t0: 0, onClick: null };

export function mountProxy(root) {
  proxy.root = root;
  root.classList.add('page-proxy');
  root.innerHTML = `
    <div class="ph">
      <h2><i data-lucide="globe"></i>代理池</h2>
      <div class="seg" id="pSeg">
        <button data-act="mode" data-m="off">关闭</button>
        <button data-act="mode" data-m="fixed">固定</button>
        <button data-act="mode" data-m="rotate">轮换</button>
      </div>
      <span style="flex:1"></span>
      <button class="btn" data-act="discover" id="pDiscBtn"><i data-lucide="search"></i>自动探测</button>
      <button class="btn" data-act="addExit"><i data-lucide="plus"></i>添加出口</button>
      <button class="btn" data-act="probe" id="pProbeBtn"><i data-lucide="radar"></i>探测全部</button>
    </div>
    <div id="pBody">${skeleton(4)}</div>`;
  refreshIcons();
  proxy.onClick = onProxyClick;
  root.addEventListener('click', proxy.onClick);
  loadProxy();
}

export function unmountProxy() {
  if (proxy.tick) { clearInterval(proxy.tick); proxy.tick = null; } // 探测计时器必须清
  if (proxy.root) { proxy.root.removeEventListener('click', proxy.onClick); proxy.root.classList.remove('page-proxy'); proxy.root.innerHTML = ''; }
  proxy.root = null; proxy.data = null; proxy.probing = false;
}

function onProxyClick(e) {
  const t = e.target.closest('[data-act]');
  if (!t) return;
  const act = t.dataset.act;
  if (act === 'mode') setMode(t.dataset.m);
  else if (act === 'probe') doProbe();
  else if (act === 'retry') loadProxy();
  else if (act === 'addExit') openAddExit();
  else if (act === 'discover') openDiscover();
  else if (act === 'delExit') delExit(t.dataset.port);
}

async function loadProxy() {
  const body = $('#pBody');
  try {
    proxy.data = await apiFetch('/api/proxy');
    renderProxy();
  } catch (err) {
    body.innerHTML = errBox('代理状态加载失败：' + err.message, 'retry');
    refreshIcons();
  }
}

function renderProxy() {
  const d = proxy.data, body = $('#pBody');
  if (!d || !body) return;
  $$('#pSeg button').forEach(b => b.classList.toggle('on', b.dataset.m === d.mode));
  // 后端 results 是「数组」：[{port, cc, ok, detail, ip, ms, checked_at}, ...]。
  // 原来当成按端口号索引的对象读，Object.keys 拿到的是 "0".."19" 这种数组下标，
  // 再和 usable 里的真端口号取并集 → 20 个出口渲染成 35 张卡，且 IP/地区全空。
  const list = Array.isArray(d.results) ? d.results
    : Object.values(d.results || {});          // 兼容后端将来改回对象形态
  const res = Object.fromEntries(list.map(r => [String(r.port), r]));
  const usable = new Set((d.usable || []).map(String));
  // 出口表本身（后端 status() 的 exits 字段）：port -> label。
  // 这一项必须并进端口全集，否则「刚添加、还没探测」的出口在页面上根本不出现
  // —— 用户点了添加、后端 200、settings.json 也写了，界面却一片空白。
  const exitLabels = Object.fromEntries((d.exits || []).map(x => [String(x.port), x.label || '']));
  // 端口全集 = 出口表 ∪ 探测记录 ∪ usable。
  // 三个来源都要：出口表给「配了但没探过的」，results 给「探过但已从表里删掉的」历史记录，
  // usable 兜底后端将来只给可用列表的情况。
  const ports = [...new Set([...Object.keys(exitLabels), ...Object.keys(res), ...usable])]
    .sort((a, b) => a - b);
  const oks = list.filter(r => r.ok && r.ms > 0);
  const avg = oks.length ? Math.round(oks.reduce((s, r) => s + r.ms, 0) / oks.length) : null;
  body.innerHTML = `
    <div class="statbar">
      <div><b>${fmtInt(d.exits_configured ?? (d.exits || []).length)}</b>配置出口</div>
      <div><b class="tok">${usable.size}</b>可用出口</div>
      <div><b>${avg == null ? '—' : fmtDur(avg)}</b>平均延迟</div>
      <div><b>${tm(d.probed_at)}</b>最近探测</div>
      <div><b>${esc(d.host || '—')}</b>出口主机</div>
    </div>
    ${d.mode === 'off' ? '<div class="note"><i data-lucide="info"></i>代理已关闭，所有请求走本机出口</div>' : ''}
    ${d.mode === 'fixed' && d.fixed_url ? `<div class="note"><i data-lucide="link"></i>固定代理：${esc(d.fixed_url)}</div>` : ''}
    ${proxy.probing ? '<div class="note" id="pProg">正在逐个探测出口…</div>' : ''}
    ${ports.length ? `<div class="pgrid ${d.mode === 'off' ? 'dim' : ''}">${ports.map(p => portCard(p, res[p], usable.has(p), exitLabels[p])).join('')}</div>`
      : `<div class="empty">
           <p>还没有配置任何出口。</p>
           <p class="dim">如果本机跑着代理（gost / squid / v2ray 之类），点「自动探测」扫一遍；
              也可以用「添加出口」手动填端口。不需要代理就把模式切到「关闭」，请求直接走本机出口。</p>
         </div>`}`;
  refreshIcons();
}

function portCard(port, r, inUse, label) {
  // 未探测过的端口没有 results 记录：用灰点表示「未知」，不能误判成不可用
  const st = !r ? '' : (r.ok ? 'ok' : 'bad');
  // 后端字段名是 detail（不是 error）、cc（地区标签）；ms 由 probe_all 计时给出。
  // ip 为 "?" 表示出口通但取 IP 的那一跳失败，照实显示成未知而不是当成有值。
  const ip = r && r.ip && r.ip !== '?' ? esc(r.ip) : (r && r.ok ? '<span class="dim">IP 未知</span>' : '—');
  const err = r ? esc(r.detail || r.error || '未知错误') : '';
  return `<div class="pcard">
    <div class="prow">
      <span class="dot ${st}"></span><b>:${esc(port)}</b>
      ${(r && r.cc) || label ? `<span class="pcc">${esc((r && r.cc) || label)}</span>` : ''}
      ${inUse ? '<span class="tag">在用</span>' : ''}
      ${proxy.probing ? '<i data-lucide="loader-2" class="spin"></i>' : ''}
      <span style="flex:1"></span>
      <button class="pdel" data-act="delExit" data-port="${esc(port)}"
              title="移出出口表" aria-label="移出出口表 ${esc(port)}">
        <i data-lucide="x"></i></button>
    </div>
    <div class="pip">${ip}</div>
    <div class="pms">${r
      ? (r.ok ? (r.ms > 0 ? fmtDur(r.ms) : '<span class="dim">延迟未知</span>')
        : `<span class="perr" title="${err}">${err.slice(0, 42)}</span>`)
      : '未探测'}</div>
  </div>`;
}

async function setMode(m) {
  if (!proxy.data || proxy.data.mode === m || proxy.probing) return;
  const prev = proxy.data.mode;
  proxy.data.mode = m; renderProxy(); // 乐观更新：先给选中态反馈，失败再回滚，避免慢请求下界面"没反应"
  try {
    await apiFetch('/api/proxy/mode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: m }) });
    toast('代理模式已切换为「' + MODE_LABEL[m] + '」', 'ok');
  } catch (err) {
    proxy.data.mode = prev; renderProxy();
    toast('切换失败：' + err.message, 'err');
  }
}

async function doProbe() {
  if (proxy.probing) return; // 防连点导致并发探测
  proxy.probing = true; proxy.t0 = Date.now();
  renderProxy(); // 重绘让所有卡片进入 loading 态
  const btn = $('#pProbeBtn');
  if (btn) btn.disabled = true;
  // 后端串行探测、最后一次性返回，前端拿不到逐端口进度；
  // 用「已用时」计时做整体进度提示，比伪造百分比进度条诚实
  proxy.tick = setInterval(() => {
    const p = $('#pProg');
    if (p) p.textContent = `正在逐个探测出口，可能较慢… 已用时 ${Math.round((Date.now() - proxy.t0) / 1000)}s`;
  }, 500);
  try {
    proxy.data = await apiFetch('/api/proxy/probe', { method: 'POST' });
    toast('探测完成', 'ok');
  } catch (err) { toast('探测失败：' + err.message, 'err'); }
  finally {
    proxy.probing = false;
    clearInterval(proxy.tick); proxy.tick = null;
    if (btn) btn.disabled = false;
    renderProxy();
  }
}

/* ---- 出口表增删：出口拓扑是每台机器自己的事，不能写死在代码里 ---- */

/** 端口合法性：后端 add_exit 对非法值抛 ValueError（→400），前端先挡一道给即时反馈 */
function badPort(v) {
  const n = Number(v);
  if (!Number.isInteger(n) || n < 1 || n > 65535) return '端口必须是 1-65535 的整数';
  return '';
}

function openAddExit() {
  const m = openModal(`
    <h3><i data-lucide="plus"></i>添加出口</h3>
    <p class="modal-msg dim">填本机代理监听的端口。标签随便写，只是方便你认出它是哪条线路。</p>
    <label class="field"><span>端口</span>
      <input class="inp" id="axPort" type="number" min="1" max="65535" placeholder="例如 3128" /></label>
    <label class="field"><span>标签（可选）</span>
      <input class="inp" id="axLabel" type="text" maxlength="32" placeholder="例如 squid / HK / 家宽" /></label>
    <div class="modal-err" id="axErr" hidden></div>
    <div class="modal-foot">
      <button class="btn ghost" data-close>取消</button>
      <button class="btn primary" id="axOk">添加</button>
    </div>`, { size: 'sm' });

  const err = m.box.querySelector('#axErr');
  const portInp = m.box.querySelector('#axPort');
  const okBtn = m.box.querySelector('#axOk');
  const showErr = (t) => { err.textContent = t; err.hidden = !t; };

  okBtn.addEventListener('click', async () => {
    const port = portInp.value.trim();
    const bad = badPort(port);
    if (bad) { showErr(bad); portInp.focus(); return; }
    showErr('');
    okBtn.disabled = true; okBtn.textContent = '添加中…';
    try {
      const r = await apiFetch('/api/proxy/exits', {
        method: 'POST',
        body: { port: Number(port), label: m.box.querySelector('#axLabel').value.trim() },
      });
      m.close();
      toast(r.action === 'updated' ? `出口 :${port} 标签已更新` : `出口 :${port} 已添加`, 'ok');
      await loadProxy();
    } catch (e2) {
      showErr(e2.message || '添加失败');
      okBtn.disabled = false; okBtn.textContent = '添加';
    }
  });
  portInp.focus();
}

async function delExit(port) {
  if (!port) return;
  const ok = await confirmDialog('移出出口表', `确定把出口 :${port} 从出口表里删掉？`
    + '\n这只改本项目的配置，不会动那个端口上真正跑着的代理进程。',
    { okText: '删除' });
  if (!ok) return;
  try {
    await apiFetch('/api/proxy/exits/' + encodeURIComponent(port), { method: 'DELETE' });
    toast(`出口 :${port} 已移除`, 'ok');
    await loadProxy();
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
  }
}

function openDiscover() {
  const m = openModal(`
    <h3><i data-lucide="search"></i>自动探测出口</h3>
    <p class="modal-msg dim">扫一段端口，看哪些能当 HTTP 代理用。只扫出口主机（当前
      <code>${esc(proxy.data?.host || '127.0.0.1')}</code>），不会碰外网。</p>
    <label class="field"><span>起始端口</span>
      <input class="inp" id="dcFrom" type="number" min="1" max="65535" value="60001" /></label>
    <label class="field"><span>结束端口</span>
      <input class="inp" id="dcTo" type="number" min="1" max="65535" value="60030" /></label>
    <label class="chk"><input type="checkbox" id="dcAdd" checked />
      <span>把扫到的可用出口直接加入出口表</span></label>
    <div class="modal-err" id="dcErr" hidden></div>
    <div class="modal-foot">
      <button class="btn ghost" data-close>取消</button>
      <button class="btn primary" id="dcOk">开始探测</button>
    </div>`, { size: 'sm' });

  const err = m.box.querySelector('#dcErr');
  const okBtn = m.box.querySelector('#dcOk');
  const showErr = (t) => { err.textContent = t; err.hidden = !t; };

  okBtn.addEventListener('click', async () => {
    const from = m.box.querySelector('#dcFrom').value.trim();
    const to = m.box.querySelector('#dcTo').value.trim();
    const bad = badPort(from) || badPort(to);
    if (bad) { showErr(bad); return; }
    if (Number(from) > Number(to)) { showErr('起始端口不能大于结束端口'); return; }
    // 后端对单次扫描量有上限（MAX_SCAN_PORTS），这里先给出可读提示，
    // 否则用户填个 1-65535 只会收到一句干巴巴的 400。
    if (Number(to) - Number(from) + 1 > 4096) {
      showErr('一次最多扫 4096 个端口，范围太大请分几次扫');
      return;
    }
    showErr('');
    okBtn.disabled = true; okBtn.textContent = '探测中…';
    try {
      const r = await apiFetch('/api/proxy/discover', {
        method: 'POST',
        body: {
          ranges: [[Number(from), Number(to)]],
          add: m.box.querySelector('#dcAdd').checked,
        },
      });
      m.close();
      const n = (r.usable_ports || []).length;
      toast(n ? `扫到 ${n} 个可用出口：${(r.usable_ports || []).join(', ')}`
        : `扫了 ${fmtInt(r.scanned)} 个端口，没有可用出口`, n ? 'ok' : 'err');
      await loadProxy();
    } catch (e2) {
      showErr(e2.message || '探测失败');
      okBtn.disabled = false; okBtn.textContent = '开始探测';
    }
  });
}

/* ==================== reg ==================== */
/* 后端只会返回这四种状态，映射到徽章样式与文案 */
const STATE_BADGE = {
  pending: ['idle', '排队中'],
  running: ['acc', '运行中'],
  done: ['ok', '已完成'],
  failed: ['bad', '失败'],
  stopped: ['idle', '已停止'],
};

const POLL_MS = 3000;      // running 任务的轮询间隔
const MAX_LOG_LINES = 200; // 日志只保留末尾 200 行，长任务不至于把 DOM 撑爆
const PHONE_RE = /^1\d{10}$/;

/* 模块级运行态：unmount 时只能靠它找回并停掉全部轮询 */
let root = null;
let alive = false;
let taskStops = new Map();   // task_id -> stop()
let expandedIds = new Set(); // 记住展开的任务，重渲染后保持展开
let invitesCache = [];

export function mountReg(rootEl) {
  stopAllPolls(); // 防止异常重复 mount 残留上一轮轮询
  root = rootEl;
  alive = true;
  taskStops = new Map();
  expandedIds = new Set();
  invitesCache = [];
  root.innerHTML = tpl();
  refreshIcons();
  // 邀请码下拉换成自绘组件：原生 select 展开层是系统绘制的，深色页面里白底刺眼。
  // 垫片保留了 .innerHTML / .value 用法，所以 fillInviteSelects() 不用改。
  for (const id of ['#autoInvite', '#mInvite']) {
    const sel = $(id, root);
    if (sel) selectify(sel, { width: '100%' });
  }
  bindTabs();
  bindAutoForm();
  bindManualForm();
  bindMisc();
  // 四块数据互不依赖，并行拉首屏
  loadTasks();
  loadBalance();
  loadInvites();
  loadSessions();
}

export function unmountReg() {
  alive = false;
  stopAllPolls(); // 关键：不停掉的话切页后 poll 仍在后台打请求
  if (root) root.innerHTML = '';
  root = null;
}

/* ---------------- 模板 ---------------- */

function tpl() {
  return `
  <div class="page-head">
    <h1>注册中心</h1>
    <p>批量自动注册与单号手动注册</p>
  </div>
  <div id="regInviteErr"></div>
  <div class="seg reg-tabs">
    <button class="on" data-tab="auto"><i data-lucide="bot"></i> 自动注册</button>
    <button data-tab="manual"><i data-lucide="keyboard"></i> 手动注册</button>
  </div>

  <div class="reg-pane" data-pane="auto">
    <div class="reg-auto-top">
      <div class="card reg-balance">
        <div class="card-h"><h3><i data-lucide="wallet"></i> 接码平台余额</h3></div>
        <div id="regBalanceBody">${skeleton(1)}</div>
      </div>
      <div class="card">
        <div class="card-h"><h3>开始批量注册</h3><p>通过接码平台自动取号、收码并完成注册</p></div>
        <form id="autoStartForm" class="reg-form">
          <div class="field count"><label>注册数量</label>
            <input id="autoCount" type="number" min="1" max="100" value="5"></div>
          <div class="field"><label>邀请码</label>
            <select id="autoInvite"><option value="">加载中…</option></select></div>
          <button class="btn primary" type="submit"><i data-lucide="play"></i> 启动任务</button>
        </form>
      </div>
    </div>
    <div class="reg-tasks-head">
      <h3>任务列表</h3>
      <div class="acts">
        <button class="btn ghost sm" id="tasksRefresh"><i data-lucide="refresh-cw"></i> 刷新</button>
        <button class="btn danger sm" id="tasksClear"><i data-lucide="trash-2"></i> 清理已完成</button>
      </div>
    </div>
    <div class="reg-tasks" id="regTasks">${skeleton(3)}</div>
  </div>

  <div class="reg-pane" data-pane="manual" hidden>
    <div class="card">
      <div class="card-h"><h3>发起手动注册</h3><p>填手机号发送验证码，再在下方会话里填码完成注册</p></div>
      <form id="mStartForm" class="reg-form">
        <div class="field"><label>手机号</label>
          <input id="mPhone" inputmode="numeric" maxlength="11" placeholder="1 开头 11 位手机号" autocomplete="off">
          <div class="field-err" id="mPhoneErr" hidden>手机号格式不正确，应为 1 开头 11 位数字</div>
        </div>
        <div class="field"><label>邀请码</label>
          <select id="mInvite"><option value="">加载中…</option></select></div>
        <button class="btn primary" type="submit"><i data-lucide="send"></i> 发送验证码</button>
      </form>
    </div>
    <div class="card">
      <div class="card-h">
        <h3>等待验证码的会话</h3>
        <p>验证码会发送到对应手机号，填入后完成注册</p>
        <div class="acts"><button class="btn ghost sm" id="sessRefresh"><i data-lucide="refresh-cw"></i> 刷新</button></div>
      </div>
      <div class="reg-sessions" id="regSessions">${skeleton(2)}</div>
    </div>
  </div>`;
}

/* ---------------- 绑定 ---------------- */

function bindTabs() {
  const seg = $('.reg-tabs', root);
  seg.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-tab]');
    if (!btn) return;
    seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
    root.querySelectorAll('.reg-pane').forEach(p => { p.hidden = p.dataset.pane !== btn.dataset.tab; });
    // 切到手动页时补一次会话刷新，保证看到的是最新等待态
    if (btn.dataset.tab === 'manual') loadSessions();
  });
}

function bindMisc() {
  $('#tasksRefresh', root).addEventListener('click', loadTasks);
  $('#tasksClear', root).addEventListener('click', clearTasks);
  $('#sessRefresh', root).addEventListener('click', loadSessions);
}

function bindAutoForm() {
  const form = $('#autoStartForm', root);
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const count = parseInt($('#autoCount', root).value, 10);
    const invite = $('#autoInvite', root).value;
    if (!Number.isInteger(count) || count < 1) { toast('注册数量至少为 1', 'warn'); return; }
    // 邀请码可选：invite 为空时后端跳过绑定，注册流程不受影响
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      const r = await apiFetch('/api/auto_register/start', { method: 'POST', body: { count, invite_code: invite } });
      // 后端一号一任务，返回的是 task_ids 数组；旧代码读 r.task_id 恒为 undefined
      const ids = r.task_ids || [];
      toast(ids.length > 1 ? `已启动 ${ids.length} 个注册任务` : `任务 ${ids[0] || ''} 已启动`, 'ok');
      await loadTasks(); // 全量刷一次，syncPolls 会为新的 running 任务挂上轮询
    } catch (e) {
      toast(e.message || '启动失败', 'err');
    } finally {
      btn.disabled = false;
    }
  });
}

function bindManualForm() {
  const form = $('#mStartForm', root);
  const phone = $('#mPhone', root);
  phone.addEventListener('input', () => setPhoneErr(false));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const p = phone.value.trim();
    // 格式错误就地拦截，不把无效请求打到后端
    if (!PHONE_RE.test(p)) { setPhoneErr(true); phone.focus(); return; }
    const invite = $('#mInvite', root).value; // 空 = 不使用邀请码
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      await apiFetch('/api/register/start', { method: 'POST', body: { phone: p, invite_code: invite } });
      toast('验证码已发送', 'ok');
      phone.value = '';
      await loadSessions();
    } catch (e) {
      toast(e.message || '发送失败', 'err');
    } finally {
      btn.disabled = false;
    }
  });
}

function setPhoneErr(on) {
  const err = $('#mPhoneErr', root);
  const input = $('#mPhone', root);
  if (err) err.hidden = !on;
  if (input) input.classList.toggle('err', on);
}

/* ---------------- 余额 / 邀请码 ---------------- */

async function loadBalance() {
  const box = $('#regBalanceBody', root);
  if (!box) return;
  try {
    const r = await apiFetch('/api/uoomsg/balance');
    if (!alive) return;
    // ok=false 多为 token 未配置：只影响自动注册，降级为警告条而非错误态
    if (!r.ok) {
      box.innerHTML = `<div class="note warn-note"><i data-lucide="alert-triangle"></i> ${escapeHtml(r.error || '接码平台不可用')}</div>`;
    } else {
      box.innerHTML = `<div class="reg-balance-v">¥ ${fmtMoney(r.balance)}</div>
        <div class="reg-balance-sub">uoomsg 接码平台 · 余额不足会导致取号失败</div>`;
    }
  } catch (e) {
    if (!alive) return;
    box.innerHTML = '';
    box.appendChild(errorState(e.message || '余额加载失败', loadBalance));
  }
  refreshIcons();
}

async function loadInvites() {
  const errBox = $('#regInviteErr', root);
  try {
    const r = await apiFetch('/api/invite/codes');
    if (!alive) return;
    // code 为空的账号不可用（拿不到邀请码），直接排除出选项
    invitesCache = (r.codes || []).filter(c => c.code);
    fillInviteSelects();
    if (errBox) errBox.innerHTML = '';
  } catch (e) {
    if (!alive) return;
    // 两个 tab 的表单都依赖邀请码，错误态放在公共顶部，一处重试全局生效
    if (errBox) {
      errBox.innerHTML = '';
      errBox.appendChild(errorState('邀请码加载失败：' + (e.message || '未知错误'), loadInvites));
      refreshIcons();
    }
  }
}

function fillInviteSelects() {
  // 必须有一个 value="" 的真实选项：下拉是自绘组件（selectify + dropdown），
  // setOptions() 会把当前值落到第一个非 disabled 选项上，选项里没有空值时
  // 用户根本没法把邀请码选成「不填」。加上它，「不带邀请码注册」才是可达状态。
  // 放在末尾，默认仍然选中第一个真实邀请码。
  const opts = invitesCache.map(c =>
    `<option value="${escapeHtml(c.code)}">${escapeHtml(c.masked)} · ${escapeHtml(c.code)} · 已邀 ${c.valid_invited ?? 0}/${c.invited ?? 0}</option>`
  ).join('') + '<option value="">不使用邀请码</option>';
  for (const id of ['#autoInvite', '#mInvite']) {
    const sel = $(id, root);
    if (!sel) continue;   // 原来是 return，第一个选择器缺失会连带跳过第二个
    const prev = sel.value;
    sel.innerHTML = opts;
    // 重载后尽量保留用户已选中的邀请码
    if (prev && invitesCache.some(c => c.code === prev)) sel.value = prev;
  }
}

/* ---------------- 任务列表 ---------------- */

async function loadTasks() {
  const box = $('#regTasks', root);
  if (!box) return;
  try {
    const r = await apiFetch('/api/auto_register/tasks');
    if (!alive) return;
    const tasks = r.tasks || [];
    renderTasks(tasks);
    syncPolls(tasks);
  } catch (e) {
    if (!alive) return;
    box.innerHTML = '';
    box.appendChild(errorState(e.message || '任务加载失败', loadTasks));
    refreshIcons();
  }
}

function renderTasks(tasks) {
  const box = $('#regTasks', root);
  if (!box) return;
  if (!tasks.length) {
    box.innerHTML = `<div class="empty-state"><i data-lucide="inbox"></i><p>还没有注册任务，从上方发起一个吧</p></div>`;
    refreshIcons();
    return;
  }
  // 新任务排前面：started_at 越大越近
  const sorted = [...tasks].sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  box.innerHTML = sorted.map(taskHtml).join('');
  bindTaskCards(box);
  refreshIcons();
  for (const t of sorted) if (expandedIds.has(t.id)) scrollLogs(t.id);
}

function metaHtml(t) {
  return `
    <span>进度 <b>${t.done ?? 0}/${t.target ?? 0}</b></span>
    <span class="t-ok">成功 <b>${t.ok ?? 0}</b></span>
    <span class="t-bad">失败 <b>${t.fail ?? 0}</b></span>
    <span>耗时 <b>${fmtRun(t.started_at, t.finished_at)}</b></span>
    ${t.current ? `<span class="reg-task-cur">${escapeHtml(t.current)}</span>` : ''}`;
}

function logsText(t) {
  const lines = (t.logs || []).slice(-MAX_LOG_LINES);
  return lines.map(escapeHtml).join('\n') || '暂无日志';
}

function taskHtml(t) {
  const [cls, label] = STATE_BADGE[t.state] || ['idle', t.state];
  const pct = t.target ? Math.min(100, Math.round((t.done / t.target) * 100)) : 0;
  const barCls = t.state === 'done' ? ' ok' : t.state === 'failed' ? ' bad' : '';
  const expanded = expandedIds.has(t.id);
  return `
  <div class="card reg-task" data-id="${escapeHtml(t.id)}">
    <div class="reg-task-h">
      <span class="badge ${cls}">${label}</span>
      <span class="mono reg-task-id">${escapeHtml(t.id)}</span>
      <span class="spacer"></span>
      ${t.state === 'running' ? `<button class="btn danger sm" data-act="stop"><i data-lucide="square"></i> 停止</button>` : ''}
      <button class="btn ghost sm icon" data-act="toggle" title="查看日志"><i data-lucide="${expanded ? 'chevron-up' : 'chevron-down'}"></i></button>
    </div>
    <div class="bar${barCls}"><i style="width:${pct}%"></i></div>
    <div class="reg-task-meta">${metaHtml(t)}</div>
    <div class="reg-logs" ${expanded ? '' : 'hidden'}><pre>${logsText(t)}</pre></div>
  </div>`;
}

function bindTaskCards(box) {
  box.querySelectorAll('.reg-task').forEach(card => {
    card.addEventListener('click', async (ev) => {
      const id = card.dataset.id;
      // 日志区内的点击是用户在选中文本，不应触发折叠
      if (ev.target.closest('.reg-logs')) return;
      const actBtn = ev.target.closest('[data-act]');
      if (actBtn && actBtn.dataset.act === 'stop') {
        ev.stopPropagation();
        await stopTask(id, actBtn);
        return;
      }
      // 卡片其余区域（含展开按钮）点击都切换日志区
      toggleLogs(card, id);
    });
  });
}

function toggleLogs(card, id) {
  const logs = card.querySelector('.reg-logs');
  const btn = card.querySelector('[data-act="toggle"]');
  if (!logs || !btn) return;
  const willOpen = logs.hidden;
  logs.hidden = !willOpen;
  if (willOpen) { expandedIds.add(id); scrollLogs(id); } else { expandedIds.delete(id); }
  // refreshIcons 会把 <i> 替换成 <svg>，所以直接重写按钮内容再重渲染
  btn.innerHTML = `<i data-lucide="${willOpen ? 'chevron-up' : 'chevron-down'}"></i>`;
  refreshIcons();
}

function scrollLogs(id) {
  const pre = $(`.reg-task[data-id="${id}"] .reg-logs pre`, root);
  if (pre) pre.scrollTop = pre.scrollHeight;
}

/* ---------------- 轮询：只挂 running 任务 ---------------- */

function syncPolls(tasks) {
  // pending 是线程启动前的短暂窗口，也要挂轮询，否则那一段不会自动刷新
  const runningIds = new Set(tasks.filter(t => t.state === 'running' || t.state === 'pending').map(t => t.id));
  // 已结束/被清掉的任务停掉轮询；没有 running 时这里会把 Map 清空，完全停止请求
  for (const [id, stop] of taskStops) {
    if (!runningIds.has(id)) { stop(); taskStops.delete(id); }
  }
  for (const id of runningIds) {
    if (!taskStops.has(id)) taskStops.set(id, poll(() => pollTask(id), POLL_MS));
  }
}

async function pollTask(id) {
  try {
    const t = await apiFetch(`/api/auto_register/status/${encodeURIComponent(id)}`);
    if (!alive) return;
    updateTaskCard(t);
    if (t.state !== 'running' && t.state !== 'pending') {
      const stop = taskStops.get(id);
      if (stop) { stop(); taskStops.delete(id); }
      if (t.state === 'done') toast(`任务 ${id} 已完成`, 'ok');
      else if (t.state === 'failed') toast(`任务 ${id} 失败`, 'err');
    }
  } catch (e) {
    if (!alive) return;
    // 状态接口失败（如任务刚被清理），停掉该轮询并回全量视图自愈
    const stop = taskStops.get(id);
    if (stop) { stop(); taskStops.delete(id); }
    loadTasks();
  }
}

/* 局部补丁式更新，避免重渲染打断日志滚动与展开态 */
function updateTaskCard(t) {
  const card = $(`.reg-task[data-id="${t.id}"]`, root);
  if (!card) { loadTasks(); return; }
  const [cls, label] = STATE_BADGE[t.state] || ['idle', t.state];
  const badge = card.querySelector('.badge');
  if (badge) { badge.className = `badge ${cls}`; badge.textContent = label; }
  const pct = t.target ? Math.min(100, Math.round((t.done / t.target) * 100)) : 0;
  const bar = card.querySelector('.bar');
  if (bar) {
    bar.className = 'bar' + (t.state === 'done' ? ' ok' : t.state === 'failed' ? ' bad' : '');
    const fill = bar.querySelector('i');
    if (fill) fill.style.width = pct + '%';
  }
  const meta = card.querySelector('.reg-task-meta');
  if (meta) meta.innerHTML = metaHtml(t);
  // 结束后不再允许停止
  const stopBtn = card.querySelector('[data-act="stop"]');
  if (stopBtn && t.state !== 'running') stopBtn.remove();
  const pre = card.querySelector('.reg-logs pre');
  if (pre) {
    pre.innerHTML = logsText(t);
    // 展开中的日志始终吸底，用户看到的是最新一行
    if (expandedIds.has(t.id)) pre.scrollTop = pre.scrollHeight;
  }
}

/* ---------------- 任务操作 ---------------- */

async function stopTask(id, btn) {
  const ok = await confirmDialog('停止任务', `确定停止任务 ${id} 吗？进行中的号码流程会被中断。`, { danger: true });
  if (!ok) return;
  btn.disabled = true;
  try {
    await apiFetch(`/api/auto_register/stop/${encodeURIComponent(id)}`, { method: 'POST' });
    toast('已发送停止指令', 'ok');
    await loadTasks();
  } catch (e) {
    toast(e.message || '停止失败', 'err');
    btn.disabled = false;
  }
}

async function clearTasks() {
  const ok = await confirmDialog('清理已完成', '将删除所有已结束（完成 / 失败 / 停止）的任务记录，确定吗？', { danger: true });
  if (!ok) return;
  try {
    await apiFetch('/api/auto_register/clear', { method: 'POST' });
    toast('已清理', 'ok');
    loadTasks();
  } catch (e) {
    toast(e.message || '清理失败', 'err');
  }
}

/* ---------------- 手动注册会话 ---------------- */

async function loadSessions() {
  const box = $('#regSessions', root);
  if (!box) return;
  try {
    const r = await apiFetch('/api/register/sessions');
    if (!alive) return;
    renderSessions(r.sessions || []);
  } catch (e) {
    if (!alive) return;
    box.innerHTML = '';
    box.appendChild(errorState(e.message || '会话加载失败', loadSessions));
    refreshIcons();
  }
}

function renderSessions(list) {
  const box = $('#regSessions', root);
  if (!box) return;
  // 该列表只承载「等码 → 完成注册」这一步，其他状态不展示
  const waiting = list.filter(s => s.state === 'waiting_code');
  if (!waiting.length) {
    box.innerHTML = `<div class="empty-state"><i data-lucide="mail-check"></i><p>暂无等待验证码的会话</p></div>`;
    refreshIcons();
    return;
  }
  box.innerHTML = waiting.map(s => `
    <div class="reg-sess" data-id="${escapeHtml(s.id)}">
      <div class="reg-sess-info">
        <span class="badge acc">等待验证码</span>
        <span class="mono reg-sess-phone">${escapeHtml(s.phone)}</span>
        <span class="reg-sess-time">${fmtTime(s.created_at)}</span>
        <span class="reg-sess-inv">${s.invite_code ? '邀请码 ' + escapeHtml(s.invite_code) : '无邀请码'}</span>
      </div>
      <div class="reg-sess-ops">
        <div class="field"><input inputmode="numeric" maxlength="8" placeholder="验证码" autocomplete="off"></div>
        <button class="btn primary sm" data-act="finish"><i data-lucide="check"></i> 完成注册</button>
        <button class="btn ghost sm" data-act="drop"><i data-lucide="x"></i> 放弃</button>
      </div>
      ${s.error ? `<span class="reg-sess-err">${escapeHtml(s.error)}</span>` : ''}
    </div>`).join('');
  box.querySelectorAll('.reg-sess').forEach(row => {
    row.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      if (btn.dataset.act === 'finish') await finishSess(row, row.dataset.id, btn);
      else if (btn.dataset.act === 'drop') await dropSess(row);
    });
  });
  refreshIcons();
}

async function finishSess(row, id, btn) {
  const input = row.querySelector('input');
  const code = (input.value || '').trim();
  // 只拦空值，格式校验交给后端（会返回 400 + detail）
  if (!code) { input.classList.add('err'); input.focus(); return; }
  input.classList.remove('err');
  btn.disabled = true;
  try {
    const r = await apiFetch('/api/register/finish', { method: 'POST', body: { session_id: id, code } });
    toast(`注册成功：${r.phone || ''}`, 'ok');
    row.remove();
    if (!$('#regSessions .reg-sess', root)) renderSessions([]);
  } catch (e) {
    toast(e.message || '注册失败', 'err');
    btn.disabled = false;
  }
}

async function dropSess(row) {
  // 后端没有放弃会话的接口，这里仅做前端移除，会话会随后端超时自然失效
  const ok = await confirmDialog('放弃会话', '仅从列表移除该会话，手机端的验证码流程会自行超时。确定吗？');
  if (!ok) return;
  row.remove();
  if (!$('#regSessions .reg-sess', root)) renderSessions([]);
}

/* ---------------- 工具 ---------------- */

function stopAllPolls() {
  for (const stop of taskStops.values()) stop();
  taskStops.clear();
}

// fmtDur 面向毫秒级短耗时；任务耗时常跨分钟，这里按秒自行格式化
function fmtRun(started, finished) {
  if (!started) return '—';
  const end = finished || Math.floor(Date.now() / 1000);
  const s = Math.max(0, end - started);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`;
}

/* ==================== invite ==================== */
// 模块级状态：rootEl 为空即页面已卸载，所有异步回调据此放弃写 DOM
let rootEl = null;
let stopPoll = null;
let detailSeq = 0;          // 明细请求的“代数”，快速切换展开卡时丢弃过期响应
let bgFailNotified = false; // 后台轮询失败只提醒一次，避免每 30s 重复弹 toast
const state = { codes: [], expanded: null, detail: {} }; // detail: phone → 明细缓存

export function mountInvite(root){
  rootEl = root;
  root.innerHTML = `
  <div class="page-invite">
    <header class="page-head">
      <h1>邀请返利</h1>
      <p>各账号拉新进度、返利积分与邀请码绑定</p>
      <div class="acts">
        <button class="btn ghost" id="iv-refresh"><i data-lucide="refresh-cw"></i>刷新</button>
        <button class="btn primary" id="iv-bind"><i data-lucide="link"></i>绑定邀请码</button>
      </div>
    </header>
    <div class="stats" id="iv-stats"></div>
    <div class="invite-grid" id="iv-grid"></div>
  </div>`;
  refreshIcons();

  // 手动刷新时清掉明细缓存，展开中的卡会重新拉最新好友数据
  $('#iv-refresh', root).addEventListener('click', () => { state.detail = {}; loadCodes(true); });
  $('#iv-bind', root).addEventListener('click', openBindModal);
  // 点击委托挂在 grid 上：卡片每次重渲染后依然生效，无需重复绑定
  $('#iv-grid', root).addEventListener('click', onGridClick);

  loadCodes(true);
  stopPoll = poll(() => loadCodes(false), 30000);
}

export function unmountInvite(){
  if (stopPoll) { stopPoll(); stopPoll = null; } // 不停止的话切页后轮询泄漏
  detailSeq++;          // 让在途的明细请求回调失效
  rootEl = null;
  state.codes = [];
  state.expanded = null;
  state.detail = {};
}

/* ---------- 数据加载 ---------- */

async function loadCodes(initial){
  if (!rootEl) return;
  const grid = $('#iv-grid', rootEl);
  // 只有首次（还没有任何数据）才上骨架屏；已有数据时刷新不闪空
  if (initial && !state.codes.length) grid.innerHTML = `<div class="card iv-skel">${skeleton(4)}</div>`;
  try {
    const d = await apiFetch('/api/invite/codes');
    if (!rootEl) return;
    state.codes = d.codes || [];
    bgFailNotified = false;
    // 账号集合可能变化，清掉已消失账号的明细缓存，防止读到旧数据
    const phones = new Set(state.codes.map(c => c.phone));
    Object.keys(state.detail).forEach(p => { if (!phones.has(p)) delete state.detail[p]; });
    renderStats();
    renderGrid();
  } catch (err) {
    if (!rootEl) return;
    if (state.codes.length) {
      // 后台刷新失败保留旧列表，首帧失败才整页错误态
      if (!bgFailNotified) { toast(err.message || '刷新失败', 'err'); bgFailNotified = true; }
    } else {
      grid.innerHTML = '';
      grid.appendChild(errorState(err.message || '加载失败', () => loadCodes(true)));
    }
  }
}

/* ---------- 渲染 ---------- */

function renderStats(){
  const ok = state.codes.filter(c => !c.error); // 不可用账号没有 invited 等字段，统计时必须剔除
  const sum = k => ok.reduce((s, c) => s + (c[k] || 0), 0);
  const capped = ok.filter(c => c.cap_reached).length;
  $('#iv-stats', rootEl).innerHTML = [
    ['users',      '账号数',       fmtInt(state.codes.length)],
    ['user-plus',  '总邀请人数',   fmtInt(sum('invited'))],
    ['user-check', '总有效邀请',   fmtInt(sum('valid_invited'))],
    ['coins',      '累计返利积分', fmtInt(sum('earned'))],
    ['flag',       '已达上限账号', fmtInt(capped)]
  ].map(([ic, label, v]) => `<div class="stat">
    <div class="stat-ic"><i data-lucide="${ic}"></i></div>
    <div class="stat-v">${v}</div>
    <div class="stat-l">${label}</div>
  </div>`).join('');
  refreshIcons();
}

function renderGrid(){
  const grid = $('#iv-grid', rootEl);
  // 展开中的账号若变成不可用或消失，直接收起，避免指向不存在的卡
  if (state.expanded && !state.codes.some(c => !c.error && c.phone === state.expanded)) state.expanded = null;
  if (!state.codes.length) {
    grid.innerHTML = `<div class="empty-state"><i data-lucide="inbox"></i><p>暂无账号数据</p></div>`;
    refreshIcons();
    return;
  }
  grid.innerHTML = state.codes.map(cardHtml).join('');
  refreshIcons();
  // 重渲染会重建明细容器，展开中的卡需要重新填充：有缓存先秒开，再后台静默更新
  if (state.expanded) {
    if (state.detail[state.expanded]) {
      paintDetail(state.expanded);
      loadDetail(state.expanded, true);
    } else {
      loadDetail(state.expanded, false);
    }
  }
}

function cardHtml(c){
  // 不可用账号只有 phone/masked/code(空)/error 四个字段，invited 等是 undefined，绝不能直接读
  if (c.error) {
    return `<div class="invite-cell">
      <div class="card invite-card disabled">
        <div class="ic-top">
          <span class="ic-phone mono">${escapeHtml(c.masked)}</span>
          <span class="badge bad nodot">不可用</span>
        </div>
        <div class="ic-err"><i data-lucide="ban"></i><span>${escapeHtml(c.error)}</span></div>
      </div>
    </div>`;
  }
  const pct = c.cap > 0 ? Math.min(100, (c.earned || 0) / c.cap * 100) : 0;
  const open = state.expanded === c.phone;
  return `<div class="invite-cell">
    <div class="card invite-card ${c.cap_reached ? 'capped' : ''} ${open ? 'open' : ''}" data-phone="${escapeHtml(c.phone)}">
      <div class="ic-top">
        <span class="ic-phone mono">${escapeHtml(c.masked)}</span>
        ${c.label ? `<span class="badge acc nodot">${escapeHtml(c.label)}</span>` : ''}
        ${c.cap_reached ? `<span class="badge warn">已达上限</span>` : ''}
        <i data-lucide="chevron-down" class="ic-chev"></i>
      </div>
      <div class="ic-code">
        <b class="mono">${escapeHtml(c.code)}</b>
        <button class="btn icon sm ghost js-copy" data-copy="${escapeHtml(c.code)}" title="复制邀请码"><i data-lucide="copy"></i></button>
      </div>
      <div class="ic-link">
        <i data-lucide="link-2"></i>
        <span class="mono">${escapeHtml(c.link)}</span>
        <button class="btn icon sm ghost js-copy" data-copy="${escapeHtml(c.link)}" title="复制邀请链接"><i data-lucide="copy"></i></button>
      </div>
      <div class="ic-meta">
        <span>有效 <b>${fmtInt(c.valid_invited)}</b> 人 / 共邀 <b>${fmtInt(c.invited)}</b> 人</span>
      </div>
      <div class="ic-bar">
        <div class="bar ${c.cap_reached ? 'warn' : ''}"><i style="width:${c.cap_reached ? 100 : pct.toFixed(1)}%"></i></div>
        <span class="mono">${fmtInt(c.earned)} / ${fmtInt(c.cap)}</span>
      </div>
    </div>
    <div class="invite-detail" data-phone="${escapeHtml(c.phone)}"${open ? '' : ' hidden'}></div>
  </div>`;
}

/* ---------- 卡片交互 ---------- */

function onGridClick(e){
  const copyBtn = e.target.closest('.js-copy');
  if (copyBtn) {
    e.stopPropagation(); // 不阻断的话点复制会同时触发卡片展开
    copyText(copyBtn.dataset.copy).then(ok => toast(ok ? '已复制到剪贴板' : '复制失败', ok ? 'ok' : 'err'));
    return;
  }
  const card = e.target.closest('.invite-card');
  if (!card || card.classList.contains('disabled')) return; // 不可用账号整卡禁交互
  // 再次点击同一张即收起，展开另一张会自动收起当前这张 —— 同时只展开一张
  state.expanded = state.expanded === card.dataset.phone ? null : card.dataset.phone;
  renderGrid();
}

function detailBox(phone){
  return rootEl ? $(`.invite-detail[data-phone="${phone}"]`, rootEl) : null;
}

async function loadDetail(phone, silent){
  const my = ++detailSeq;
  if (!silent) {
    const box = detailBox(phone);
    if (box) box.innerHTML = `<div class="card iv-detail-body">${skeleton(3)}</div>`;
  }
  try {
    const d = await apiFetch(`/api/invite?phone=${encodeURIComponent(phone)}`);
    if (!rootEl || my !== detailSeq) return; // 切页或已切换展开卡，丢弃过期响应
    state.detail[phone] = d;
    paintDetail(phone);
  } catch (err) {
    if (!rootEl || my !== detailSeq) return;
    if (silent && state.detail[phone]) return; // 静默刷新失败保留旧内容，不打扰阅读
    const b = detailBox(phone);
    if (!b) return;
    b.innerHTML = '';
    b.appendChild(errorState(err.message || '好友明细加载失败', () => loadDetail(phone, false)));
  }
}

function paintDetail(phone){
  const box = detailBox(phone);
  const d = state.detail[phone];
  if (!box || !d) return;
  const friends = d.friends || [];
  // 注意这个接口的字段名与 codes 列表不同：invite_count / total_credits / cap_value
  box.innerHTML = `<div class="card iv-detail-body">
    <div class="iv-detail-head">
      <span>好友明细</span>
      <span class="iv-detail-meta">有效 ${fmtInt(d.valid_invite_count)} / 共邀 ${fmtInt(d.invite_count)} · 返利 ${fmtInt(d.total_credits)} / ${fmtInt(d.cap_value)}</span>
    </div>
    ${friends.length ? `<div class="tbl-wrap"><div class="tbl-scroll">
      <table class="tbl">
        <thead><tr><th>好友</th><th>注册时间</th><th>状态</th><th class="num">贡献积分</th></tr></thead>
        <tbody>${friends.map(f => `<tr>
          <td class="mono">${escapeHtml(f.masked)}</td>
          <td>${fmtTime(f.registered_at)}</td>
          <td>${f.valid ? '<span class="badge ok">有效</span>' : '<span class="badge idle">未生效</span>'}</td>
          <td class="num mono">${fmtInt(f.credits)}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div></div>`
    : `<div class="empty-state"><i data-lucide="users"></i><p>还没有邀请到好友</p></div>`}
  </div>`;
  refreshIcons();
}

/* ---------- 绑定邀请码弹窗 ---------- */

function openBindModal(){
  const okAccounts = state.codes.filter(c => !c.error); // 不可用账号不能作为目标
  if (!okAccounts.length) { toast('没有可绑定的账号', 'warn'); return; }

  const m = openModal(`
  <div class="modal-body">
    <h3 class="iv-modal-title">绑定邀请码</h3>
    <div class="field">
      <label>目标账号</label>
      <select class="iv-select" id="bd-target">${okAccounts.map(c =>
        `<option value="${escapeHtml(c.phone)}">${escapeHtml(c.masked)}${c.label ? `（${escapeHtml(c.label)}）` : ''}</option>`).join('')}
      </select>
    </div>
    <div class="field">
      <label>邀请码</label>
      <select class="iv-select" id="bd-code"></select>
    </div>
    <div class="note warn-note"><i data-lucide="info"></i>不能绑定自己的邀请码，下方列表已自动排除目标账号本人的码。</div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" id="bd-cancel">取消</button>
    <button class="btn primary" id="bd-submit"><i data-lucide="check"></i>确认绑定</button>
  </div>`, { size: '' });
  refreshIcons();

  // 弹窗里的两个下拉同样换成自绘组件（原生展开层不受 CSS 控制）
  const targetSel = selectify($('#bd-target', m.box), { width: '100%' });
  const codeSel = selectify($('#bd-code', m.box), { width: '100%' });
  const submitBtn = $('#bd-submit', m.box);

  function refreshCodeOptions(){
    const t = targetSel.value;
    // 服务端对绑自己的码会报 12313，前端直接把目标账号自己的码从选项里排掉
    const opts = okAccounts.filter(c => c.phone !== t && c.code);
    if (!opts.length) {
      codeSel.innerHTML = `<option value="">（没有其他可用邀请码）</option>`;
      codeSel.disabled = true;
      submitBtn.disabled = true;
    } else {
      codeSel.disabled = false;
      submitBtn.disabled = false;
      codeSel.innerHTML = opts.map(c =>
        `<option value="${escapeHtml(c.code)}">${escapeHtml(c.code)} · ${escapeHtml(c.masked)}${c.label ? `（${escapeHtml(c.label)}）` : ''}</option>`).join('');
    }
  }
  targetSel.addEventListener('change', refreshCodeOptions);
  refreshCodeOptions();

  $('#bd-cancel', m.box).addEventListener('click', m.close);
  submitBtn.addEventListener('click', async () => {
    const body = { phone: targetSel.value, code: codeSel.value };
    if (!body.code) { toast('请选择邀请码', 'warn'); return;
    }
    submitBtn.disabled = true; // 防连点重复提交
    try {
      const r = await apiFetch('/api/invite/bind', { method: 'POST', body });
      toast(r.detail || '绑定成功', 'ok');
      m.close();
      state.detail = {}; // 绑定会改统计口径，缓存明细一并作废
      loadCodes(false);
    } catch (err) {
      toast(err.message || '绑定失败', 'err');
      submitBtn.disabled = false;
    }
  });
}
