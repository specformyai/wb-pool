/* ============================================================
 * history.js —— 「历史对话」页
 *
 * 数据来源是上游计费用量流水（app/history.py），不是真正的会话接口：
 *   - 只有用户侧输入（input 字段），上游**不返回助手回复**，所以只渲染单侧气泡
 *   - 没有 conversationId，会话边界靠时间间隔推断（后端 build_sessions）
 *   - 部分行 input 为空（纯 API 调用），这类只作为「调用」计数
 *
 * 抓取要逐月分页打几十到几百个上游请求，所以走异步任务 + 轮询进度。
 * ============================================================ */
import {
  $, $$, apiFetch, escapeHtml, fmtInt, fmtMoney, fmtTime,
  refreshIcons, toast, copyText, confirmDialog,
} from '@/shared.js';
import { selectify } from '@/selectify.js';

const esc = (v) => escapeHtml(v == null ? '' : String(v));

const GAP_KEY = 'wbpool.history.gap';
const SEL_KEY = 'wbpool.history.phone';

let accounts = [];        // /api/history/accounts 的结果
let curPhone = '';        // 当前选中账号（完整 phone，不是 masked）
let curData = null;       // /api/history/data 的结果
let curSession = -1;      // 当前选中会话下标，-1 = 未选
let pollTimer = null;     // 抓取进度轮询
let gapMin = 30;

/* ---------------------------------------------------------------- 工具 */

function loadGap() {
  let v = 30;
  try {
    const s = localStorage.getItem(GAP_KEY);
    if (s) v = parseInt(s, 10);
  } catch { /* 隐私模式 */ }
  return Number.isFinite(v) && v >= 1 ? v : 30;
}

function saveGap(v) {
  try { localStorage.setItem(GAP_KEY, String(v)); } catch { /* 隐私模式 */ }
}

function loadSel() {
  try { return localStorage.getItem(SEL_KEY) || ''; } catch { return ''; }
}

function saveSel(v) {
  try { localStorage.setItem(SEL_KEY, v || ''); } catch { /* 隐私模式 */ }
}

/** '2026-08-26 17:07:00' -> '08-26 17:07' */
function shortTime(s) {
  const t = String(s || '');
  if (t.length >= 16) return t.slice(5, 16);
  return t;
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

/* ---------------------------------------------------------------- 骨架 */

function shell() {
  return `
  <div class="page-head">
    <div>
      <h1>历史对话</h1>
      <p>从上游用量流水还原账号上的历史输入。被封号（11140）同样可以拉取。</p>
    </div>
  </div>

  <div class="card hist-ctl">
    <div class="hist-ctl-row">
      <div class="field hist-f-acc">
        <label for="histAcc">账号</label>
        <select id="histAcc"></select>
      </div>
      <div class="field hist-f-gap">
        <label for="histGap">会话间隔</label>
        <select id="histGap">
          <option value="10">10 分钟</option>
          <option value="30">30 分钟</option>
          <option value="60">1 小时</option>
          <option value="180">3 小时</option>
          <option value="1440">按天</option>
        </select>
      </div>
      <div class="hist-acts">
        <button class="btn" id="histFetch"><i data-lucide="cloud-download"></i><span>拉取</span></button>
        <button class="btn ghost" id="histExport"><i data-lucide="download"></i><span>导出</span></button>
        <button class="btn ghost" id="histDrop"><i data-lucide="trash-2"></i><span>清缓存</span></button>
      </div>
    </div>
    <div class="hist-prog" id="histProg" hidden>
      <div class="hist-prog-bar"><span id="histProgFill"></span></div>
      <div class="hist-prog-t" id="histProgT">准备中…</div>
    </div>
    <div class="hist-note" id="histNote"></div>
  </div>

  <div id="histSum"></div>

  <div class="hist-main" id="histMain">
    <div class="card hist-side">
      <div class="card-h"><div><h3>会话</h3><p id="histSideSub">—</p></div></div>
      <div class="hist-slist" id="histSlist"></div>
    </div>
    <div class="card hist-conv">
      <div class="card-h">
        <div><h3 id="histConvT">对话</h3><p id="histConvSub">选择左侧的会话</p></div>
        <div class="acts">
          <button class="btn ghost sm" id="histCopy"><i data-lucide="copy"></i><span>复制</span></button>
        </div>
      </div>
      <div class="hist-msgs" id="histMsgs"></div>
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 账号下拉 */

function accLabel(a) {
  const bits = [a.masked || a.phone];
  if (a.status && a.status !== 'active') bits.push(a.status);
  if (a.cached_rows) bits.push(`${fmtInt(a.cached_rows)} 条`);
  else bits.push('未拉取');
  return bits.join(' · ');
}

function fillAccounts() {
  const sel = $('#histAcc');
  if (!sel) return;
  sel.innerHTML = accounts.map((a) =>
    `<option value="${esc(a.phone)}">${esc(accLabel(a))}</option>`).join('');
  if (curPhone && accounts.some((a) => a.phone === curPhone)) sel.value = curPhone;
  else if (accounts.length) { curPhone = accounts[0].phone; sel.value = curPhone; }
  selectify(sel, { width: '260px' });
}

function curAcc() {
  return accounts.find((a) => a.phone === curPhone) || null;
}

function renderNote() {
  const n = $('#histNote');
  if (!n) return;
  const a = curAcc();
  if (!a) { n.innerHTML = ''; return; }
  const bits = [];
  if (a.cached && a.cached_at) bits.push(`缓存于 ${fmtTime(a.cached_at * 1000)}`);
  if (a.registered_at) bits.push(`注册 ${esc(String(a.registered_at).slice(0, 10))}`);
  const errs = curData && curData.errors && curData.errors.length
    ? `<span class="hist-note-bad">${esc(curData.errors.length)} 个月份抓取有误</span>` : '';
  n.innerHTML = bits.length || errs
    ? `<span>${esc(bits.join(' · '))}</span>${errs}` : '';
}

/* ---------------------------------------------------------------- 汇总卡 */

function renderSummary() {
  const box = $('#histSum');
  if (!box) return;
  if (!curData) { box.innerHTML = ''; return; }
  const s = curData.summary || {};
  const models = (s.models || []).slice(0, 6);

  const kpi = (icon, label, value, sub = '') => `
    <div class="mh-kpi">
      <div class="mh-kpi-ic"><i data-lucide="${icon}" class="mh-ic"></i></div>
      <div class="mh-kpi-v">${value}</div>
      <div class="mh-kpi-l">${label}</div>
      ${sub ? `<div class="mh-kpi-sub">${sub}</div>` : ''}
    </div>`;

  const span = s.first && s.last
    ? `${esc(String(s.first).slice(0, 10))} ~ ${esc(String(s.last).slice(0, 10))}` : '—';

  const chips = models.map((m) =>
    `<span class="hist-chip"><span class="mono">${esc(m.model)}</span>
      <b>${fmtInt(m.n)}</b>${m.credits > 0 ? `<i>${fmtMoney(m.credits)}</i>` : ''}</span>`).join('');

  box.innerHTML = `
    <div class="card">
      <div class="card-h"><div><h3>汇总</h3><p>${span}</p></div></div>
      <div class="mh-kpis">
        ${kpi('activity', '总调用', fmtInt(s.count))}
        ${kpi('message-square-text', '带正文', fmtInt(s.with_text),
    s.count ? `占 ${((s.with_text / s.count) * 100).toFixed(1)}%` : '')}
        ${kpi('coins', '消耗积分', fmtMoney(s.credits))}
        ${kpi('messages-square', '会话数', fmtInt((curData.sessions || []).length))}
      </div>
      ${chips ? `<div class="hist-chips">${chips}</div>` : ''}
    </div>`;
}

/* ---------------------------------------------------------------- 会话列表 */

function renderSessionList() {
  const box = $('#histSlist');
  const sub = $('#histSideSub');
  if (!box) return;

  if (!curData) {
    box.innerHTML = `<div class="empty-state sm"><i data-lucide="inbox"></i>
      <div class="t">还没有数据</div><div class="s">点上面的「拉取」</div></div>`;
    if (sub) sub.textContent = '—';
    refreshIcons();
    return;
  }

  const list = curData.sessions || [];
  if (sub) sub.textContent = `${fmtInt(list.length)} 个`;

  if (!list.length) {
    box.innerHTML = `<div class="empty-state sm"><i data-lucide="inbox"></i>
      <div class="t">没有记录</div><div class="s">这个账号在上游没有流水</div></div>`;
    refreshIcons();
    return;
  }

  box.innerHTML = list.map((s, i) => {
    const topModel = Object.entries(s.models || {}).sort((a, b) => b[1] - a[1])[0];
    return `
    <button class="hist-sitem${i === curSession ? ' on' : ''}" data-si="${i}">
      <div class="hist-sitem-t">${esc(s.title)}</div>
      <div class="hist-sitem-m">
        <span>${esc(shortTime(s.start))}</span>
        <span class="mono">${fmtInt(s.count)} 次</span>
        ${s.credits > 0 ? `<span class="mono">${fmtMoney(s.credits)}</span>` : ''}
        ${topModel ? `<span class="mono hist-sitem-mo">${esc(topModel[0])}</span>` : ''}
      </div>
    </button>`;
  }).join('');
  refreshIcons();
}

/* ---------------------------------------------------------------- 对话区 */

function renderConv() {
  const box = $('#histMsgs');
  const title = $('#histConvT');
  const sub = $('#histConvSub');
  if (!box) return;

  const list = (curData && curData.sessions) || [];
  const s = curSession >= 0 ? list[curSession] : null;

  if (!s) {
    box.innerHTML = `<div class="chat-empty">
      <i data-lucide="messages-square"></i>
      <div class="chat-empty-t">选择一个会话</div>
      <div class="chat-empty-s">上游只保存用户侧输入，助手回复无法还原</div>
    </div>`;
    if (title) title.textContent = '对话';
    if (sub) sub.textContent = '选择左侧的会话';
    refreshIcons();
    return;
  }

  if (title) title.textContent = s.title;
  if (sub) {
    sub.textContent = `${shortTime(s.start)} ~ ${shortTime(s.end)} · `
      + `${s.count} 次调用 · ${s.texts} 条正文 · ${fmtMoney(s.credits)} 积分`;
  }

  const rows = s.rows || [];
  const html = rows.map((r) => {
    const text = String(r.input || r.inputTrunc || '').trim();
    const meta = `<div class="hist-mmeta">
        <span class="mono">${esc(r.model || '?')}</span>
        <span>${esc(shortTime(r.requestTime))}</span>
        ${Number(r.credit) > 0 ? `<span class="mono">${fmtMoney(r.credit)}</span>` : ''}
      </div>`;
    if (!text) {
      return `<div class="hist-msg empty">${meta}
        <div class="hist-nocontent">（无正文 · 纯 API 调用）</div></div>`;
    }
    return `<div class="hist-msg">${meta}
      <div class="chat-bubble hist-bubble">${esc(text)}</div></div>`;
  }).join('');

  box.innerHTML = html || `<div class="chat-empty"><i data-lucide="inbox"></i>
    <div class="chat-empty-t">这个会话没有内容</div></div>`;
  refreshIcons();
  box.scrollTop = 0;
}

/* ---------------------------------------------------------------- 数据加载 */

async function loadData(quiet = false) {
  if (!curPhone) return;
  try {
    curData = await apiFetch(`/api/history/data?phone=${encodeURIComponent(curPhone)}&gap=${gapMin}`);
    curSession = (curData.sessions || []).length ? 0 : -1;
  } catch (e) {
    curData = null;
    curSession = -1;
    if (!quiet) {
      const msg = String(e.message || e);
      // 404 = 还没抓过，这是正常初始状态，不当错误提示
      if (!/还没有抓取过/.test(msg)) toast(msg, 'bad');
    }
  }
  renderSummary();
  renderSessionList();
  renderConv();
  renderNote();
  refreshIcons();
}

async function loadAccounts() {
  const d = await apiFetch('/api/history/accounts');
  accounts = d.accounts || [];
  return d.tasks || [];
}

/* ---------------------------------------------------------------- 抓取 */

function showProg(on) {
  const p = $('#histProg');
  if (p) p.hidden = !on;
}

function setProg(pct, text) {
  const fill = $('#histProgFill');
  const t = $('#histProgT');
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (t) t.textContent = text;
}

async function startFetch() {
  if (!curPhone) { toast('先选一个账号', 'bad'); return; }
  const btn = $('#histFetch');
  if (btn) btn.disabled = true;
  showProg(true);
  setProg(2, '启动中…');

  let taskId = '';
  try {
    const r = await apiFetch('/api/history/fetch', { method: 'POST', body: { phone: curPhone } });
    taskId = r.task_id;
    if (r.already) toast('该账号已有抓取任务在跑，接着看进度', 'ok');
  } catch (e) {
    showProg(false);
    if (btn) btn.disabled = false;
    toast(String(e.message || e), 'bad');
    return;
  }

  stopPoll();
  pollTimer = setInterval(async () => {
    let t;
    try {
      t = await apiFetch(`/api/history/status/${taskId}`);
    } catch (e) {
      stopPoll();
      showProg(false);
      if (btn) btn.disabled = false;
      toast(`进度查询失败：${String(e.message || e)}`, 'bad');
      return;
    }
    const tot = t.months_total || 0;
    const done = t.months_done || 0;
    const pct = tot ? (done / tot) * 100 : 5;
    setProg(pct, `逐月抓取 ${done}/${tot || '?'} · 已得 ${fmtInt(t.rows || 0)} 条`);

    if (t.done) {
      stopPoll();
      if (btn) btn.disabled = false;
      setProg(100, `完成 · ${fmtInt(t.rows || 0)} 条`);
      setTimeout(() => showProg(false), 1500);
      if (t.ok) {
        toast(`抓到 ${fmtInt(t.rows || 0)} 条记录`, 'ok');
        await loadAccounts();
        fillAccounts();
        await loadData();
      } else {
        toast(t.error || '抓取失败', 'bad');
      }
    }
  }, 1200);
}

/* ---------------------------------------------------------------- 导出 / 复制 */

function convText(s) {
  const lines = [`# ${s.title}`, `${s.start} ~ ${s.end} · ${s.count} 次调用 · ${s.credits} 积分`, ''];
  for (const r of s.rows || []) {
    const text = String(r.input || r.inputTrunc || '').trim();
    lines.push(`[${r.requestTime}] ${r.model || '?'}`
      + (Number(r.credit) > 0 ? ` · ${r.credit}` : ''));
    lines.push(text || '（无正文）');
    lines.push('');
  }
  return lines.join('\n');
}

function exportAll() {
  if (!curData) { toast('还没有数据', 'bad'); return; }
  const s = curData.summary || {};
  const parts = [
    `账号 ${curData.masked || curPhone}`,
    `${s.count} 次调用 · ${s.with_text} 条正文 · ${s.credits} 积分`,
    `区间 ${s.first} ~ ${s.last}`,
    '='.repeat(70), '',
  ];
  for (const sess of curData.sessions || []) parts.push(convText(sess), '-'.repeat(70), '');
  const blob = new Blob([parts.join('\n')], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `history_${(curData.masked || curPhone).replace(/\D/g, '')}.txt`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------------------------------------------------------- 挂载 */

export async function mount(root) {
  gapMin = loadGap();
  curPhone = loadSel();
  root.classList.add('page', 'page-history');
  root.innerHTML = shell();

  const gapSel = $('#histGap');
  if (gapSel) {
    gapSel.value = String(gapMin);
    selectify(gapSel, { width: '128px' });
  }

  try {
    await loadAccounts();
  } catch (e) {
    root.innerHTML = `<div class="err-state"><i data-lucide="cloud-off"></i>
      <div class="err-msg">账号列表加载失败：${esc(e.message || e)}</div></div>`;
    refreshIcons();
    return;
  }
  fillAccounts();
  renderNote();

  // 账号切换
  $('#histAcc')?.addEventListener('change', async (e) => {
    curPhone = e.target.value;
    saveSel(curPhone);
    curData = null;
    curSession = -1;
    await loadData(true);
  });

  // 间隔切换：纯前端重切分，重新请求后端（切分在后端做）
  gapSel?.addEventListener('change', async (e) => {
    gapMin = parseInt(e.target.value, 10) || 30;
    saveGap(gapMin);
    if (curData) await loadData(true);
  });

  $('#histFetch')?.addEventListener('click', startFetch);
  $('#histExport')?.addEventListener('click', exportAll);

  $('#histDrop')?.addEventListener('click', async () => {
    if (!curPhone) return;
    const ok = await confirmDialog('清除缓存',
      '删除本地缓存的抓取结果？上游数据不受影响，可以重新拉取。');
    if (!ok) return;
    try {
      await apiFetch(`/api/history/data?phone=${encodeURIComponent(curPhone)}`, { method: 'DELETE' });
      curData = null;
      curSession = -1;
      await loadAccounts();
      fillAccounts();
      renderSummary();
      renderSessionList();
      renderConv();
      renderNote();
      toast('缓存已清除', 'ok');
    } catch (e) {
      toast(String(e.message || e), 'bad');
    }
  });

  $('#histCopy')?.addEventListener('click', async () => {
    const list = (curData && curData.sessions) || [];
    const s = curSession >= 0 ? list[curSession] : null;
    if (!s) { toast('先选一个会话', 'bad'); return; }
    await copyText(convText(s));
    toast('已复制', 'ok');
  });

  // 会话列表点击（事件委托，列表会整体重渲染）
  $('#histSlist')?.addEventListener('click', (e) => {
    const btn = e.target.closest?.('[data-si]');
    if (!btn) return;
    curSession = parseInt(btn.dataset.si, 10);
    $$('.hist-sitem', $('#histSlist')).forEach((b) =>
      b.classList.toggle('on', b === btn));
    renderConv();
  });

  refreshIcons();
  await loadData(true);
}

export function unmount() {
  stopPoll();
  accounts = [];
  curData = null;
  curSession = -1;
}
