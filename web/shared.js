/* ============================================================
 * shared.js —— 全站公共工具层
 *
 * 所有页面模块 import 这里的函数，不自己重复定义。
 * （上一轮 k3 分次生成时重复声明 normalizeAccount，导致整个模块
 *  加载即抛 SyntaxError、页面全空 —— 公共层集中定义就是为了避免这个。）
 * ============================================================ */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* 图片等静态资源的基路径。
 *
 * dev 下静态根就是 app/ 本身（'./'），生产下后端把 web/ 挂在 /static，
 * index.html 的路径由 build_prod.py 重写、但 JS 里的字符串它不碰 ——
 * 侧栏 logo 写死 './icon-192.png' 在生产会解析成 /icon-192.png 而 404。
 * 从 <link rel="icon"> 的 href 反推基路径，两种部署形态都拿得到正确前缀。 */
export const ASSET = (() => {
  const href = document.querySelector('link[rel="icon"]')?.getAttribute('href') || './';
  return href.replace(/[^/]*$/, '');   // 去掉文件名，留目录前缀
})();

/** 创建元素：el('div', {class:'x', onclick:fn}, ['文本' | node]) */
export function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    // text 走 textContent —— 落到下面的 setAttribute 会变成 <option text="x">，
    // 元素看着存在但没有文字（下拉框全是空白项就是这么来的）。
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else if (v != null && v !== false) n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return n;
}

/* ---------------------------------------------------------------- 请求层 */

/**
 * 统一请求封装。失败一律 throw Error(可读消息)，调用方只管 try/catch。
 * 后端错误体形如 {"detail":"..."} 或 {"error":"..."}，都要能提出来 ——
 * 只显示 "HTTP 500" 对排查毫无帮助。
 */
export async function apiFetch(path, opts = {}) {
  const init = { credentials: 'same-origin', ...opts };
  if (init.body && typeof init.body !== 'string') {
    init.body = JSON.stringify(init.body);
    init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    throw new Error(`网络不可达：${e.message}`);
  }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON，保留 text */ }
  if (!r.ok) {
    const msg = (data && (data.detail ?? data.error ?? data.message)) || text.slice(0, 200);
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return data;
}

/* ---------------------------------------------------------------- 格式化 */

export const fmtMoney = (n) => (n == null || Number.isNaN(Number(n))) ? '—'
  : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const fmtInt = (n) => (n == null || Number.isNaN(Number(n))) ? '—'
  : Math.round(Number(n)).toLocaleString('zh-CN');

/** 秒级时间戳 → 相对时间；超过 7 天显示绝对日期（相对时间在长跨度下无意义） */
export function fmtTime(ts) {
  if (!ts) return '—';
  const s = Number(ts) > 1e12 ? Number(ts) / 1000 : Number(ts); // 容错毫秒
  const diff = Date.now() / 1000 - s;
  if (diff < 0) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`;
  const d = new Date(s * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtDur(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return '—';
  const v = Number(ms);
  if (v < 1000) return `${Math.round(v)}ms`;
  // 先按输出精度定量再判区间：v=59999 若直接比 60000 会走秒分支，
  // toFixed(1) 把 59.999 进位成 "60.0s"（本该是 1m0s）。
  if (Math.round(v / 100) / 10 < 60) return `${(v / 1000).toFixed(1)}s`;
  // 总秒数先定下来再拆分钟，否则分钟与秒各自取整，
  // 秒进位到 60 时分钟不跟着涨 -> "11m60s"。
  const total = Math.round(v / 1000);
  return `${Math.floor(total / 60)}m${total % 60}s`;
}

/* ---------------------------------------------------------------- 反馈 UI */

function toastHost() {
  let h = $('#toasts');
  if (!h) { h = el('div', { id: 'toasts', class: 'toasts' }); document.body.appendChild(h); }
  return h;
}

export function toast(msg, type = 'ok') {
  const icon = { ok: 'check-circle', err: 'alert-circle', warn: 'alert-triangle' }[type] || 'info';
  const n = el('div', { class: `toast ${type}` , html:
    `<i data-lucide="${icon}"></i><span>${escapeHtml(String(msg))}</span>` });
  toastHost().appendChild(n);
  refreshIcons();
  setTimeout(() => { n.classList.add('out'); setTimeout(() => n.remove(), 300); }, 3200);
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** 二次确认弹窗，返回 Promise<boolean> */
export function confirmDialog(title, msg, { danger = true, okText = '确认' } = {}) {
  return new Promise((resolve) => {
    const wrap = el('div', { class: 'modal' });
    wrap.innerHTML = `
      <div class="modal-mask"></div>
      <div class="modal-box sm">
        <h3>${escapeHtml(title)}</h3>
        <p class="modal-msg">${escapeHtml(msg)}</p>
        <div class="modal-foot">
          <button class="btn ghost" data-act="no">取消</button>
          <button class="btn ${danger ? 'danger' : 'primary'}" data-act="yes">${escapeHtml(okText)}</button>
        </div>
      </div>`;
    const done = (v) => { wrap.remove(); resolve(v); };
    wrap.addEventListener('click', (e) => {
      const a = e.target.closest('[data-act]')?.dataset.act;
      if (a === 'yes') done(true);
      else if (a === 'no' || e.target.classList.contains('modal-mask')) done(false);
    });
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { document.removeEventListener('keydown', esc); done(false); }
    });
    document.body.appendChild(wrap);
    refreshIcons();
    wrap.querySelector('[data-act="yes"]').focus();
  });
}

/** 通用弹窗：传入 innerHTML，返回 { box, close }，调用方自己绑事件 */
export function openModal(html, { size = '' } = {}) {
  const wrap = el('div', { class: 'modal' });
  wrap.innerHTML = `<div class="modal-mask"></div><div class="modal-box ${size}">${html}</div>`;
  const close = () => wrap.remove();
  wrap.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-mask') || e.target.closest('[data-close]')) close();
  });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { document.removeEventListener('keydown', esc); close(); }
  });
  document.body.appendChild(wrap);
  refreshIcons();
  return { box: wrap.querySelector('.modal-box'), wrap, close };
}

export const skeleton = (n = 3, w = '100%') =>
  Array.from({ length: n }, (_, i) =>
    `<div class="skl" style="width:${typeof w === 'function' ? w(i) : w}"></div>`).join('');

export function errorState(msg, onRetry) {
  const n = el('div', { class: 'err-state', html:
    `<i data-lucide="cloud-off"></i>
     <div class="err-msg">${escapeHtml(msg)}</div>
     <button class="btn ghost sm" data-retry><i data-lucide="rotate-cw"></i><span>重试</span></button>` });
  n.querySelector('[data-retry]').addEventListener('click', onRetry);
  return n;
}

export const refreshIcons = () => { if (window.lucide) window.lucide.createIcons(); };

/** 复制到剪贴板。navigator.clipboard 在非 HTTPS 下不可用，需要 textarea 兜底 */
export async function copyText(s) {
  try {
    await navigator.clipboard.writeText(s);
    return true;
  } catch {
    const ta = el('textarea', { style: 'position:fixed;left:-9999px' });
    ta.value = s;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

/**
 * 轮询助手：自动在页面隐藏时暂停（省掉后台无谓请求），返回 stop()。
 * 每个页面 unmount 必须调 stop，否则切页后旧轮询继续跑、状态串台。
 */
export function poll(fn, ms) {
  let timer = null, stopped = false;
  const tick = async () => {
    if (stopped) return;
    if (!document.hidden) { try { await fn(); } catch { /* 单次失败不中断轮询 */ } }
    timer = setTimeout(tick, ms);
  };
  timer = setTimeout(tick, ms);
  const onVis = () => { if (!document.hidden && !stopped) { clearTimeout(timer); tick(); } };
  document.addEventListener('visibilitychange', onVis);
  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVis);
  };
}

/** 排序助手：返回比较函数，null/undefined 恒排最后（不管升降序） */
export function cmpBy(key, dir = 'desc') {
  const sign = dir === 'asc' ? 1 : -1;
  return (a, b) => {
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === 'string') return sign * x.localeCompare(y, 'zh-CN');
    return sign * (x - y);
  };
}
