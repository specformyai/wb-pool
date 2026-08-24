/* selectify.js — 把页面里已有的原生 <select> 就地换成 dropdown 组件。
 *
 * 存在的理由：原生 select 的展开列表由操作系统绘制，CSS 管不到，
 * 深色后台里点开必然是一片白底黑字。dropdown.js 是纯 DOM 实现，
 * 但它的 API 只有 value / change / setOptions，而页面里的老代码
 * 到处在用 innerHTML、append(option)、querySelectorAll('option')
 * 来填充选项。与其把 9 处调用点全改写，不如在这里补一层兼容垫片：
 * 让替换后的节点继续"长得像"一个 select。
 */
import { dropdown } from './dropdown.js';

/* 从一段 <option> HTML 或真实 option 节点里抽出 {value,label,disabled} */
function readOptions(host) {
  return [...host.querySelectorAll('option')].map((o) => ({
    value: o.value,
    label: o.textContent.trim(),
    disabled: o.disabled,
  }));
}

/* 给 dropdown 根节点补上 select 的常用接口，老代码不用改就能继续跑。
 * 注意 innerHTML 是访问器属性：这里用 defineProperty 覆盖，
 * setter 里把 HTML 解析成选项数组再交给 setOptions。 */
function shim(root, initial) {
  // dropdown 没有暴露 getOptions()，所以这里自己留一份副本，
  // append() 追加时要基于它来拼，不能反过来问组件要。
  let cache = initial.slice();
  const apply = (next) => { cache = next.slice(); root.setOptions(cache); };

  const parse = (html) => {
    const box = document.createElement('select');
    box.innerHTML = html;
    return readOptions(box);
  };

  Object.defineProperty(root, 'innerHTML', {
    configurable: true,
    get() { return ''; },
    set(html) { apply(parse(html)); },
  });

  // append(el('option', ...)) 是老代码的另一种填充方式：
  // 累加到当前选项后面，而不是替换。
  root.append = (...nodes) => {
    const add = [];
    for (const n of nodes) {
      if (n && n.tagName === 'OPTION') {
        add.push({ value: n.value, label: n.textContent.trim(), disabled: n.disabled });
      }
    }
    if (add.length) apply([...cache, ...add]);
  };

  root.appendChild = (n) => { root.append(n); return n; };
  root.getOptions = () => cache.slice();

  // pool.js 的表头排序会用 [...sel.options].some(o => o.value === v)
  // 来判断某个值是否存在，所以这里也要提供 .options（只读、像 option 的对象数组）。
  Object.defineProperty(root, 'options', {
    configurable: true,
    get() { return cache.map((o) => ({ value: o.value, text: o.label, disabled: !!o.disabled })); },
  });
}

/**
 * 就地替换一个原生 select。
 * @param {HTMLSelectElement} sel  页面上已存在的 select
 * @param {object} opts            透传给 dropdown（width / ariaLabel / placeholder）
 * @returns {HTMLElement} dropdown 根节点（已插入到原位置）
 */
export function selectify(sel, opts = {}) {
  if (!sel || sel.tagName !== 'SELECT') return sel;

  const items = readOptions(sel);
  const dd = dropdown(items, {
    value: sel.value || (items[0] && items[0].value),
    ariaLabel: opts.ariaLabel || sel.getAttribute('aria-label') || '',
    placeholder: opts.placeholder,
    width: opts.width,
    ...opts,
  });

  // 保留 id / class，老代码的 $('#sortSel') 之类才能继续选到它
  if (sel.id) dd.id = sel.id;
  for (const c of sel.classList) {
    if (c !== 'inp') dd.classList.add(c); // .inp 是输入框样式，会和 .dd-btn 打架
  }
  shim(dd, items);
  sel.replaceWith(dd);
  return dd;
}

/** 批量替换：在容器里按选择器找 select 全部换掉 */
export function selectifyAll(root, selector = 'select', opts = {}) {
  const out = [];
  for (const sel of root.querySelectorAll(selector)) out.push(selectify(sel, opts));
  return out;
}
