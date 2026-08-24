/* dropdown.js — 深色主题通用下拉选择组件
 * API 对齐原生 <select>：root.value 读写、'change' 事件（CustomEvent）、
 * root.setOptions() 动态换选项、root.destroy() 清理全局监听。
 */
import { el, refreshIcons } from './shared.js';

const GAP = 6;    // 面板与按钮的间距
const MAX_H = 300; // 面板最大高度
const EDGE = 8;   // 距视口边缘的最小留白

// 模块级：当前处于打开状态的实例的完整关闭函数。
// 任意时刻最多一个面板打开；新实例打开前先调用它，
// 走的是上一个实例自己的完整关闭路径（移除面板节点、解绑 scroll/resize
// 监听、aria-expanded=false、chevron 复位、isOpen 复位），不留脏状态。
let activeClose = null;

export function dropdown(options = [], opts = {}) {
  const placeholder = opts.placeholder ?? '请选择';
  const ariaLabel = opts.ariaLabel ?? opts.placeholder ?? '下拉选择';
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : null;

  let items = Array.isArray(options) ? options.slice() : [];
  let cur = opts.value;

  let isOpen = false;
  let destroyed = false;
  let panel = null;      // 挂在 body 上的面板节点
  let listEl = null;     // 面板内滚动容器
  let searchInp = null;  // 选项多于 8 个时存在的搜索框
  let view = [];         // 过滤后的可见选项
  let rows = [];         // 与 view 一一对应的行元素
  let hi = -1;           // 键盘高亮下标（指向 view）
  let filter = '';
  let hostWatch = null;  // 监视宿主按钮是否被路由切换摘除的 MutationObserver

  /* ---------------- 按钮（combobox） ---------------- */
  const labelEl = el('span', { class: 'dd-label' });
  const btn = el('button', {
    class: 'dd-btn',
    type: 'button',
    role: 'combobox',
    'aria-haspopup': 'listbox',
    'aria-expanded': 'false',
    'aria-label': ariaLabel,
    onclick: () => { if (isOpen) close(); else open(); },
    onkeydown: onKey,
  });
  btn.appendChild(labelEl);
  btn.appendChild(el('i', { class: 'dd-chev', 'data-lucide': 'chevron-down' }));

  const root = el('div', { class: 'dd' });
  if (opts.width != null) {
    root.style.width = typeof opts.width === 'number' ? opts.width + 'px' : String(opts.width);
  }
  root.appendChild(btn);
  refreshIcons(root);

  const findItem = (v) => items.find((o) => o.value === v);

  function syncBtn() {
    const it = findItem(cur);
    labelEl.textContent = it ? String(it.label) : placeholder;
    labelEl.classList.toggle('dd-ph', !it);
  }
  syncBtn();

  /* ---------------- 开合 ---------------- */
  function open() {
    if (isOpen || destroyed) return;
    // 互斥：先让已打开的实例走它自己的完整关闭路径，再接管 activeClose
    if (activeClose) activeClose();
    activeClose = close;
    isOpen = true;
    root.classList.add('dd-open');
    btn.setAttribute('aria-expanded', 'true');
    filter = '';
    buildPanel();

    document.addEventListener('mousedown', onDocDown, true);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', place);

    // 面板挂在 body 上，宿主按钮却在页面容器里 —— SPA 路由切换时容器被
    // 整块换掉，按钮随之消失，面板却会永久留在 body 上挡住点击。
    // 这里盯着按钮是否还在文档里，一旦脱离就自我关闭。
    hostWatch = new MutationObserver(() => {
      if (!btn.isConnected) close();
    });
    hostWatch.observe(document.body, { childList: true, subtree: true });

    // 高亮落在当前选中项（禁用时跳过），否则落到首个可选项
    const si = view.findIndex((o) => !o.disabled && o.value === cur);
    hi = si >= 0 ? si : view.findIndex((o) => !o.disabled);
    applyHi(false);

    requestAnimationFrame(() => {
      if (!panel) return;
      if (rows[hi]) rows[hi].scrollIntoView({ block: 'nearest' }); // 选中项滚进可视区
      panel.classList.add('dd-show');
      if (searchInp) searchInp.focus();
      else btn.focus();
    });
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    if (activeClose === close) activeClose = null; // 释放模块级持有者
    root.classList.remove('dd-open');
    btn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', onDocDown, true);
    window.removeEventListener('scroll', onScroll, true);
    window.removeEventListener('resize', place);
    if (hostWatch) { hostWatch.disconnect(); hostWatch = null; }
    if (panel) {
      panel.remove();   // 关闭即移除 body 上的面板节点
      panel = null;
      listEl = null;
      searchInp = null;
      view = [];
      rows = [];
      hi = -1;
    }
  }

  /* ---------------- 面板 ---------------- */
  function buildPanel() {
    panel = el('div', { class: 'dd-panel', role: 'listbox', 'aria-label': ariaLabel });

    if (items.length > 8) {
      searchInp = el('input', {
        class: 'dd-search-inp',
        type: 'text',
        placeholder: '搜索…',
        'aria-label': '搜索选项',
        autocomplete: 'off',
        spellcheck: 'false',
        oninput: () => { filter = searchInp.value; renderList(); },
        onkeydown: onKey,
      });
      const wrap = el('div', { class: 'dd-search' });
      wrap.appendChild(searchInp);
      panel.appendChild(wrap);
    }

    listEl = el('div', { class: 'dd-list' });
    panel.appendChild(listEl);
    renderList();
    document.body.appendChild(panel);
    place();
  }

  function renderList() {
    const q = filter.trim().toLowerCase();
    view = items.filter((o) => {
      if (!q) return true;
      return (
        String(o.label).toLowerCase().includes(q) ||
        (o.hint != null && String(o.hint).toLowerCase().includes(q))
      );
    });

    rows = [];
    listEl.innerHTML = '';
    if (!view.length) {
      listEl.appendChild(el('div', {
        class: 'dd-none',
        text: items.length ? '无匹配项' : '暂无选项',
      }));
      hi = -1;
      return;
    }

    view.forEach((o, i) => {
      const attrs = {
        class: 'dd-opt' + (o.disabled ? ' dd-dis' : ''),
        role: 'option',
        'aria-selected': o.value === cur ? 'true' : 'false',
      };
      if (o.disabled) attrs['aria-disabled'] = 'true';
      const row = el('div', attrs);
      row.appendChild(el('i', { class: 'dd-check', 'data-lucide': 'check' }));
      row.appendChild(el('span', { class: 'dd-opt-label', text: String(o.label) }));
      if (o.hint != null && o.hint !== '') {
        row.appendChild(el('span', { class: 'dd-hint', text: String(o.hint) }));
      }
      row.addEventListener('click', () => { if (!o.disabled) pick(o); });
      row.addEventListener('mousemove', () => {
        if (o.disabled || hi === i) return;
        hi = i;
        applyHi(false);
      });
      listEl.appendChild(row);
      rows.push(row);
    });
    refreshIcons(listEl);

    if (hi < 0 || hi >= view.length || view[hi].disabled) {
      hi = view.findIndex((o) => !o.disabled);
    }
    applyHi(false);
  }

  // 用按钮的 getBoundingClientRect 定位 fixed 面板；空间不足时向上弹出
  function place() {
    if (!panel) return;
    const r = btn.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // 面板宽度不小于按钮宽度，且不超出视口
    const pw = Math.min(Math.max(r.width, 140), vw - EDGE * 2);
    panel.style.width = pw + 'px';

    panel.style.maxHeight = '';
    const natural = panel.offsetHeight; // 受 CSS 300px 上限约束的自然高度

    const below = vh - r.bottom - GAP - EDGE;
    const above = r.top - GAP - EDGE;
    const up = below < natural && above > below;

    const mh = Math.max(72, Math.min(MAX_H, up ? above : below));
    panel.style.maxHeight = mh + 'px';
    const ph = Math.min(natural, mh);

    const top = Math.max(EDGE, up ? r.top - GAP - ph : r.bottom + GAP);
    const left = Math.max(EDGE, Math.min(r.left, vw - pw - EDGE));

    panel.style.top = top + 'px';
    panel.style.left = left + 'px';
    panel.classList.toggle('dd-up', up);
  }

  /* ---------------- 选中 / 高亮 ---------------- */
  function pick(o) {
    if (o.disabled) return;
    const changed = o.value !== cur;
    cur = o.value;
    syncBtn();
    close();
    btn.focus();
    if (changed) {
      root.dispatchEvent(new CustomEvent('change', { detail: { value: cur }, bubbles: true }));
      if (onChange) onChange(cur);
    }
  }

  function applyHi(scroll = true) {
    for (let i = 0; i < rows.length; i++) {
      rows[i].classList.toggle('dd-hl', i === hi);
    }
    if (scroll && rows[hi]) rows[hi].scrollIntoView({ block: 'nearest' });
  }

  // ↑↓ 循环移动，跳过 disabled
  function step(d) {
    if (!view.length) return;
    let i = hi;
    for (let n = 0; n < view.length; n++) {
      i = (i + d + view.length) % view.length;
      if (!view[i].disabled) { hi = i; applyHi(); return; }
    }
  }

  // Home / End：跳首 / 尾的可选项
  function jump(first) {
    if (first) {
      for (let i = 0; i < view.length; i++) if (!view[i].disabled) { hi = i; applyHi(); return; }
    } else {
      for (let i = view.length - 1; i >= 0; i--) if (!view[i].disabled) { hi = i; applyHi(); return; }
    }
  }

  /* ---------------- 事件 ---------------- */
  function onKey(e) {
    const k = e.key;
    if (!isOpen) {
      if (k === 'ArrowDown' || k === 'ArrowUp' || k === 'Enter' || k === ' ') {
        e.preventDefault();
        open();
      }
      return;
    }
    switch (k) {
      case 'ArrowDown': e.preventDefault(); step(1); break;
      case 'ArrowUp':   e.preventDefault(); step(-1); break;
      case 'Home':      e.preventDefault(); jump(true); break;
      case 'End':       e.preventDefault(); jump(false); break;
      case 'Enter':
        e.preventDefault();
        if (view[hi] && !view[hi].disabled) pick(view[hi]);
        break;
      case 'Escape':
        e.preventDefault();
        e.stopPropagation();
        close();
        btn.focus();
        break;
      case 'Tab':
        close();
        break;
      default:
        break; // 搜索框内正常输入字符
    }
  }

  function onDocDown(e) {
    if (!panel) return;
    if (panel.contains(e.target) || root.contains(e.target)) return;
    close();
  }

  function onScroll(e) {
    if (panel && e.target && panel.contains(e.target)) return; // 列表内部滚动不重排
    place();
  }

  /* ---------------- 对外 API ---------------- */
  Object.defineProperty(root, 'value', {
    get: () => cur,
    set: (v) => {
      cur = v;
      syncBtn();
      if (isOpen && listEl) renderList(); // 同步刷新选中勾与 aria-selected
    },
  });

  root.setOptions = (next) => {
    if (destroyed) return;
    items = Array.isArray(next) ? next.slice() : [];
    // 对齐原生 select：换完选项后如果当前值已不存在（或从来没设过），
    // 自动落到第一个非 disabled 的选项上。老代码里普遍是
    // innerHTML='' 再逐个 append(option)，中间不会显式赋 value，
    // 原生 select 这时会自己选中第一项 —— 不跟着做的话按钮会一直停在
    // placeholder，页面看起来像没加载出数据。
    if (cur === undefined || !findItem(cur)) {
      const first = items.find((o) => !o.disabled);
      cur = first ? first.value : undefined;
    }
    syncBtn();
    if (isOpen) { close(); open(); } // 选项数变化可能影响搜索框显隐，重建面板
  };

  root.destroy = () => {
    destroyed = true;
    close(); // 移除 document / window 监听与面板节点
    if (activeClose === close) activeClose = null; // 自己持有时清空，避免野指针
  };

  return root;
}