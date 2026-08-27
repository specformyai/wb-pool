/* ============================================================
 * router.js —— hash 路由 + 页面生命周期管理
 *
 * 每个页面模块导出 mount(root) / unmount()。切页时必须调上一个页面的
 * unmount 清理定时器 —— 否则轮询会叠加，切几次后每秒好几个请求。
 * ============================================================ */

import { $, ASSET, refreshIcons, toast } from '@/shared.js';

const ROUTES = [
  { id: 'overview', name: '概览',      icon: 'layout-dashboard', mod: () => import('@/overview.js') },
  { id: 'chat',     name: '对话调试',  icon: 'messages-square',  mod: () => import('@/chat.js'), fn: 'Chat' },
  { id: 'pool',     name: '账号池',    icon: 'layers',           mod: () => import('@/pool.js') },
  { id: 'calls',    name: '调用监控',  icon: 'activity',         mod: () => import('@/pages.js'), fn: 'Calls' },
  { id: 'history',  name: '历史对话',  icon: 'history',          mod: () => import('@/history.js') },
  { id: 'keys',     name: 'API Key',   icon: 'key-round',        mod: () => import('@/pages.js'), fn: 'Keys' },
  { id: 'rates',    name: '模型与倍率', icon: 'boxes',            mod: () => import('@/rates.js'), fn: 'Rates' },
  { id: 'reg',      name: '注册中心',  icon: 'user-plus',        mod: () => import('@/pages.js'), fn: 'Reg' },
  { id: 'invite',   name: '邀请返利',  icon: 'gift',             mod: () => import('@/pages.js'), fn: 'Invite' },
  { id: 'proxy',    name: '代理池',    icon: 'route',            mod: () => import('@/pages.js'), fn: 'Proxy' },
  { id: 'settings', name: '设置',      icon: 'settings',         mod: () => import('@/settings.js') },
];

let current = null;       // { id, unmount }
const modCache = new Map();

/* -------------------------------------------------------------- 侧栏收起 */

const COLLAPSE_KEY = 'wbpool.side.collapsed';

function applyCollapsed(on) {
  document.body.classList.toggle('side-collapsed', on);
  try { localStorage.setItem(COLLAPSE_KEY, on ? '1' : '0'); } catch { /* 隐私模式 */ }
}

function initCollapsed() {
  let stored = null;
  try { stored = localStorage.getItem(COLLAPSE_KEY); } catch { /* 隐私模式 */ }
  // 没存过时按视口宽度决定默认值；存过就完全尊重用户的选择
  const on = stored === null ? window.innerWidth < 1100 : stored === '1';
  document.body.classList.toggle('side-collapsed', on);
}

function renderNav() {
  const side = $('#side');
  side.innerHTML = `
    <div class="brand">
      <div class="brand-mark"><img src="${ASSET}icon-192.png" alt="" width="34" height="34" /></div>
      <div><div class="brand-t">wb-pool</div><div class="brand-s">账号池网关</div></div>
      <button class="side-toggle" id="sideToggle" title="收起侧栏" aria-label="收起侧栏">
        <i data-lucide="panel-left-close"></i>
      </button>
    </div>
    <button class="side-expand" id="sideExpand" title="展开侧栏" aria-label="展开侧栏">
      <i data-lucide="panel-left-open"></i>
    </button>
    ${ROUTES.map((r) => `
      <button class="nav-item${r.id === 'settings' ? ' nav-last' : ''}" data-route="${r.id}" data-label="${r.name}">
        <i data-lucide="${r.icon}"></i><span>${r.name}</span>
      </button>`).join('')}`;
  side.querySelectorAll('[data-route]').forEach((b) =>
    b.addEventListener('click', () => { location.hash = '#/' + b.dataset.route; }));
  side.querySelector('#sideToggle').addEventListener('click', () => applyCollapsed(true));
  side.querySelector('#sideExpand').addEventListener('click', () => applyCollapsed(false));
}

function markActive(id) {
  $('#side').querySelectorAll('[data-route]').forEach((b) =>
    b.classList.toggle('on', b.dataset.route === id));
}

async function go(id) {
  const route = ROUTES.find((r) => r.id === id) || ROUTES[0];

  // 先清理上一个页面，再挂新的。顺序反了会让旧轮询在新页面上继续跑。
  if (current?.unmount) {
    try { current.unmount(); } catch (e) { console.warn('unmount 失败', e); }
  }
  current = null;

  const root = $('#view');
  // class 也要重置，不只是 innerHTML —— 页面模块自己 add 的作用域类
  // （chat-page / page-rates 之类）如果它的 unmount 忘了 remove，就会一直叠加，
  // 下一页会同时套上两套页面样式。这里统一收口，不依赖每个页面自觉。
  root.className = 'view';
  root.innerHTML = '<div class="card" style="margin-top:24px"><div class="skl" style="width:40%"></div>'
    + '<div class="skl" style="width:70%"></div><div class="skl" style="width:55%"></div></div>';
  markActive(route.id);

  try {
    let mod = modCache.get(route.id);
    if (!mod) { mod = await route.mod(); modCache.set(route.id, mod); }

    // pages.js 里多个页面共存，用 mountXxx/unmountXxx 命名区分
    const mountFn = route.fn ? mod['mount' + route.fn] : mod.mount;
    const unmountFn = route.fn ? mod['unmount' + route.fn] : mod.unmount;
    if (typeof mountFn !== 'function') {
      throw new Error(`页面模块缺少 ${route.fn ? 'mount' + route.fn : 'mount'} 导出`);
    }
    root.innerHTML = '';
    await mountFn(root);
    current = { id: route.id, unmount: unmountFn };
  } catch (e) {
    console.error('[router] 加载页面失败', route.id, e);
    root.innerHTML = `<div class="err-state">
      <i data-lucide="triangle-alert"></i>
      <div class="err-msg">页面「${route.name}」加载失败：${String(e.message || e)}</div>
      <button class="btn ghost sm" onclick="location.reload()">
        <i data-lucide="rotate-cw"></i><span>重新加载</span></button></div>`;
    refreshIcons();
  }
}

function currentId() {
  const h = location.hash.replace(/^#\/?/, '').split('?')[0];
  return ROUTES.some((r) => r.id === h) ? h : ROUTES[0].id;
}

export function start() {
  initCollapsed();
  renderNav();
  refreshIcons();
  window.addEventListener('hashchange', () => go(currentId()));
  go(currentId());
}