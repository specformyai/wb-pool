/* wb-pool WebUI */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let MODELS = [];
let regSession = null, regTimer = null;
let ME = null;                                     // 当前登录用户
let HIDE_DEAD = localStorage.getItem('wbpool_hide_dead') === '1';
let HEALTH_WIN = Number(localStorage.getItem('wbpool_health_win') || 24);

const icons = () => window.lucide && lucide.createIcons();

/* ---------- toast ---------- */
function toast(msg, kind = 'info') {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  const ic = kind === 'ok' ? 'check-circle-2' : kind === 'err' ? 'alert-circle' : 'info';
  t.innerHTML = `<i data-lucide="${ic}"></i><span></span>`;
  t.querySelector('span').textContent = String(msg).slice(0, 400);
  $('#toastHost').appendChild(t);
  icons();
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 320); }, kind === 'err' ? 6500 : 3600);
}

/* ---------- ask（替代原生 prompt / confirm） ----------
   headless 浏览器和部分内嵌 WebView 里 window.prompt/confirm 会直接吊死整个页面，
   所以统一走自绘 modal，返回 Promise：输入框取值 or null（取消）；确认框 true/false。 */
let ASK_RESOLVE = null;
function askClose(val) {
  $('#askModal').hidden = true;
  const f = ASK_RESOLVE; ASK_RESOLVE = null;
  if (f) f(val);
}
function askOpen({ title, desc = '', label = '', value = '', ok = '确定', input = true, icon = 'message-square-text', danger = false }) {
  return new Promise(resolve => {
    ASK_RESOLVE = resolve;
    $('#askTitle').textContent = title;
    const d = $('#askDesc');
    d.textContent = desc; d.hidden = !desc;
    $('#askInputWrap').hidden = !input;
    $('#askLabel').textContent = label;
    $('#askInput').value = value;
    $('#askInput').required = input;
    $('#askOkTxt').textContent = ok;
    $('#askOk').classList.toggle('danger', danger);
    $('#askOk').classList.toggle('primary', !danger);
    setIcon($('#askHead'), icon);
    $('#askModal').hidden = false;
    icons();
    if (input) { const i = $('#askInput'); i.focus(); i.select(); }
    else $('#askOk').focus();
  });
}
const askText = (title, label, value = '', desc = '') =>
  askOpen({ title, label, value, desc, input: true, icon: 'pencil-line' });
const askConfirm = (title, desc, ok = '确认', danger = true) =>
  askOpen({ title, desc, input: false, ok, danger, icon: 'alert-triangle' })
    .then(v => v !== null);

$('#askForm').addEventListener('submit', e => {
  e.preventDefault();
  askClose($('#askInputWrap').hidden ? true : $('#askInput').value);
});
$('#askClose').onclick = () => askClose(null);
$('#askCancel').onclick = () => askClose(null);
$('#askModal').addEventListener('click', e => { if (e.target === $('#askModal')) askClose(null); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('#askModal').hidden) askClose(null);
});

/* ---------- fetch ----------
   鉴权靠 HttpOnly 的 wb_session cookie，前端不再持有任何密钥。
   任何接口返回 401 都直接把界面切回登录页。                        */
async function api(path, opts = {}) {
  const h = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const r = await fetch(path, { ...opts, headers: h, credentials: 'same-origin' });
  const txt = await r.text();
  let data; try { data = JSON.parse(txt); } catch { data = { raw: txt }; }
  if (!r.ok) {
    if (r.status === 401) { showGate('登录已过期，请重新登录'); }
    const m = data?.detail?.error?.message || data?.error || data?.detail || txt.slice(0, 300);
    const e = new Error(typeof m === 'string' ? m : JSON.stringify(m));
    e.status = r.status;
    e.data = data;          // 调用方需要看具体字段（如 can_retry）
    throw e;
  }
  return data;
}
function busy(btn, on) { btn && btn.classList.toggle('loading', on); if (btn) btn.disabled = on; }
async function run(btn, fn) {
  busy(btn, true);
  try { return await fn(); }
  catch (e) { toast(e.message, 'err'); throw e; }
  finally { busy(btn, false); }
}
function showOut(el, data, isErr = false) {
  const n = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  el.textContent = n;
  el.className = `out show ${isErr ? 'err' : 'ok'}`;
}

/* ---------- tabs ---------- */
$$('#tabs .tab').forEach(t => t.onclick = () => {
  $$('#tabs .tab').forEach(x => x.classList.remove('active'));
  $$('.view').forEach(v => v.classList.remove('active'));
  t.classList.add('active');
  $('#view-' + t.dataset.view).classList.add('active');
  const v = t.dataset.view;
  if (v === 'accounts') { loadPool(); loadRotation().catch(() => {}); }
  if (v === 'register') loadInviteCodes().catch(() => {});
  if (v === 'models') loadModels(false);
  if (v === 'keys') loadKeys();
  if (v === 'proxy') loadProxy();
  if (v === 'overview') { loadOverview(); loadHealthBars(); }
  if (v === 'chat') loadChatAccounts().catch(() => {});
  if (v === 'api') fillApi();
});

/* ---------- 登录 / 会话 ---------- */
function showGate(msg) {
  ME = null;
  $('#app').hidden = true;
  $('#gate').hidden = false;
  $('#gateMsg').textContent = msg || '';
  $('#gateMsg').classList.toggle('show', !!msg);
  icons();
  setTimeout(() => $('#loginUser').focus(), 60);
}
function showApp(state) {
  ME = state.user;
  $('#gate').hidden = true;
  $('#app').hidden = false;
  $('#userName').textContent = state.user || '已登录';
  $('#pwdBanner').hidden = !state.default_password;
  icons();
}
async function checkAuth() {
  try {
    const st = await fetch('/api/auth/state', { credentials: 'same-origin' }).then(r => r.json());
    if (st.logged_in) { showApp(st); return true; }
    showGate('');
    $('#gateFoot').textContent = st.default_password
      ? '首次使用：默认账号 admin / admin，登录后请立刻改密码。' : '';
    return false;
  } catch {
    showGate('连不上服务端');
    return false;
  }
}
$('#gateForm').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = $('#btnLogin');
  busy(btn, true);
  try {
    const d = await fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: $('#loginUser').value.trim(), password: $('#loginPass').value }),
    }).then(async r => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || '登录失败');
      return j;
    });
    $('#loginPass').value = '';
    showApp({ user: d.user, default_password: d.default_password });
    toast('登录成功', 'ok');
    boot();
  } catch (err) {
    $('#gateMsg').textContent = err.message;
    $('#gateMsg').classList.add('show');
    $('#loginPass').select();
  } finally { busy(btn, false); }
});
$('#loginEye').onclick = () => {
  const i = $('#loginPass');
  i.type = i.type === 'password' ? 'text' : 'password';
  $('#loginEye').innerHTML = `<i data-lucide="${i.type === 'password' ? 'eye' : 'eye-off'}"></i>`;
  icons();
};
$('#userBtn').onclick = e => { e.stopPropagation(); $('#userPop').hidden = !$('#userPop').hidden; };
document.addEventListener('click', () => { if ($('#userPop')) $('#userPop').hidden = true; });
$('#btnLogout').onclick = async () => {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
  showGate('已退出登录');
};

/* 修改密码 */
function openPwd() { $('#pwdModal').hidden = false; $('#pwdMsg').classList.remove('show'); icons(); $('#pwdOld').focus(); }
function closePwd() { $('#pwdModal').hidden = true; ['#pwdOld', '#pwdNew', '#pwdNew2'].forEach(s => $(s).value = ''); }
$('#btnOpenPwd').onclick = openPwd;
$('#btnBannerPwd').onclick = openPwd;
$('#pwdClose').onclick = closePwd;
$('#pwdCancel').onclick = closePwd;
$('#pwdModal').addEventListener('click', e => { if (e.target === $('#pwdModal')) closePwd(); });
$('#pwdForm').addEventListener('submit', async e => {
  e.preventDefault();
  const msg = $('#pwdMsg');
  if ($('#pwdNew').value !== $('#pwdNew2').value) {
    msg.textContent = '两次输入的新密码不一样'; msg.classList.add('show'); return;
  }
  try {
    await api('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ old: $('#pwdOld').value, new: $('#pwdNew').value }),
    });
    closePwd();
    toast('密码已修改，请重新登录', 'ok');
    showGate('密码已修改，请用新密码登录');
  } catch (err) {
    msg.textContent = err.message; msg.classList.add('show');
  }
});

/* ---------- health ---------- */
async function health() {
  const pill = $('#healthPill'), txt = $('#healthTxt');
  try {
    const d = await api('/api/health');
    pill.className = 'pill ok';
    txt.textContent = `${d.stats.usable}/${d.stats.total} 可用 · ${d.proxy_mode}`;
    return d;
  } catch (e) {
    pill.className = 'pill bad'; txt.textContent = '离线';
  }
}

/* ---------- overview ---------- */
function fmt(n, d = 0) {
  if (n === null || n === undefined || n === -1) return '—';
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: d });
}
function ago(ts) {
  if (!ts) return '从未';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  return Math.floor(s / 86400) + ' 天前';
}
const ST = { active: '可用', exhausted: '配额耗尽', dead: '失效', disabled: '已停用' };
/* created_at 后端给的是 unix 秒（不是 ISO 串），统一格式化成 YYYY-MM-DD HH:MM */
function when(ts) {
  if (!ts) return '—';
  const n = typeof ts === 'number' ? ts : Number(ts);
  const d = Number.isFinite(n) ? new Date(n * 1000) : new Date(String(ts));
  if (isNaN(d.getTime())) return '—';
  const p = x => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadOverview() {
  let d;
  try { d = await api('/api/pool'); } catch (e) { return; }
  const s = d.stats;
  $('#heroCredits').textContent = fmt(s.credits_total, 2);
  $('#heroUsable').textContent = s.usable;
  $('#heroTotal').textContent = s.total;
  $('#heroReq').textContent = fmt(s.requests);
  $('#heroSpent').textContent = fmt(s.credits_spent, 2);

  // 总额已排除不可用/停用账号，把被排除的部分单独说清楚，免得对不上账
  const ex = Number(s.credits_unusable || 0);
  const note = $('#heroExcluded');
  if (ex > 0.005) {
    const dead = (s.total || 0) - (s.usable || 0);
    note.innerHTML = `<i data-lucide="info"></i>已排除 ${dead} 个不可用 / 停用账号上的 ${fmt(ex, 2)} 积分（连池内合计 ${fmt(s.credits_total_all, 2)}）`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }

  const by = s.by_status || {};
  $('#statCards').innerHTML = [
    ['可用账号', s.usable, `共 ${s.total} 个`, 'circle-check'],
    ['配额耗尽', by.exhausted || 0, '12 小时后自动重试', 'battery-low'],
    ['已失效', (by.dead || 0) + (by.disabled || 0), '需要重新登录', 'unplug'],
    ['累计 Token', fmt(s.tokens), `${fmt(s.requests)} 次请求`, 'hash'],
  ].map(([k, v, sub, ic]) => `<div class="stat">
      <div class="stat-top"><i data-lucide="${ic}"></i>${k}</div>
      <div class="stat-val">${v}</div><div class="stat-sub">${sub}</div></div>`).join('');

  const max = Math.max(...d.accounts.map(a => a.credits_total > 0 ? a.credits_total : 0), 2100);
  $('#overviewList').innerHTML = d.accounts.length ? d.accounts.map(a => `
    <div class="acc-item">
      <div class="acc-top">
        <span class="acc-phone">${a.masked}</span>
        <span class="badge ${a.status}">${ST[a.status] || a.status}</span>
      </div>
      <div class="acc-credits">${fmt(a.credits_total, 2)}</div>
      <div class="acc-bar"><i style="width:${Math.min(100, (a.credits_total > 0 ? a.credits_total : 0) / max * 100)}%"></i></div>
      <div class="acc-foot"><span>${a.request_count} 次请求</span><span>${ago(a.last_used)}</span></div>
    </div>`).join('') : '<div class="chat-empty">池子是空的，去「添加账号」加一个</div>';
  icons();

  const sel = $('#invPhone');
  if (sel) sel.innerHTML = d.accounts.map(a => `<option value="${a.phone}">${a.masked}</option>`).join('');
}

/* ---------- 模型可用性观察条 ---------- */
const HEALTH_LABEL = { ok: '正常', degraded: '不稳', bad: '异常', idle: '无调用' };

function paintHealthWindow() {
  $$('#healthWindow button').forEach(b => b.classList.toggle('on', Number(b.dataset.h) === HEALTH_WIN));
}
$$('#healthWindow button').forEach(b => b.onclick = () => {
  HEALTH_WIN = Number(b.dataset.h);
  localStorage.setItem('wbpool_health_win', String(HEALTH_WIN));
  paintHealthWindow(); loadHealthBars();
});
$('#healthOnlyUsed').onchange = () => loadHealthBars();
$('#btnHealthRefresh').onclick = e => run(e.currentTarget, loadHealthBars);

async function loadHealthBars() {
  paintHealthWindow();
  const d = await api(`/api/calls/health?window_h=${HEALTH_WIN}&buckets=50`).catch(() => null);
  if (!d) return;

  const pct1 = v => v === null || v === undefined ? '—' : (v * 100).toFixed(1) + '%';
  $('#healthKpi').innerHTML = [
    ['模型数量', fmt(d.model_count), '', 'line-chart'],
    ['总请求', fmt(d.total), '', 'activity'],
    ['平均成功率', pct1(d.avg_rate), '', 'check-circle-2'],
    ['异常模型', fmt(d.abnormal), `正常 ${fmt(d.normal)}${d.idle ? ` · 无调用 ${fmt(d.idle)}` : ''}`,
      'alert-triangle'],
  ].map(([k, v, sub, ic]) => `<div class="hk">
      <div class="hk-ico"><i data-lucide="${ic}"></i></div>
      <div class="hk-label">${k}</div>
      <div class="hk-val">${v}</div>
      ${sub ? `<div class="hk-sub">${sub}</div>` : ''}
    </div>`).join('');

  let list = d.models || [];
  if ($('#healthOnlyUsed').checked) list = list.filter(m => m.total > 0);
  if (!list.length) {
    $('#healthList').innerHTML = `<div class="chat-empty">这个窗口内还没有调用记录。去「对话调试」发一条，或让客户端跑起来，这里就会有数据。</div>`;
    icons(); return;
  }

  const secs = d.bucket_seconds || 0;
  const span = secs >= 3600 ? `${(secs / 3600).toFixed(1)} 小时` : `${Math.round(secs / 60)} 分钟`;

  $('#healthList').innerHTML = list.map(m => {
    const bars = m.buckets.map(b => {
      const tip = b.ok + b.fail ? `成功 ${b.ok} / 失败 ${b.fail}（每格 ${span}）` : `无调用（每格 ${span}）`;
      return `<i class="hb ${b.state}" title="${tip}"></i>`;
    }).join('');
    // 徽章取 state 与 tail_state 里更差的那个：累计 99% 但现在正连挂，不能还写「正常」
    const RANK = { bad: 0, degraded: 1, ok: 2, idle: 3 };
    const shown = RANK[m.tail_state] < RANK[m.state] ? m.tail_state : m.state;
    const BIC = { ok: 'check-circle-2', degraded: 'alert-triangle', bad: 'x-circle', idle: 'minus-circle' };
    const tail = m.tail_state === 'bad' && m.state !== 'bad'
      ? `<span class="hr-tail"><i data-lucide="trending-down"></i>最近连续失败</span>` : '';
    const rate = m.rate === null ? '—' : (m.rate * 100).toFixed(m.rate === 1 ? 0 : 1) + '%';
    const err = m.last_error
      ? `<div class="hr-err" title="${esc(m.last_error)}"><i data-lucide="alert-circle"></i>${esc(m.last_error)}</div>` : '';
    const ttft = m.ttft_ms ? (m.ttft_ms / 1000).toFixed(1) + ' s' : '—';
    const tps = m.tps ? m.tps.toFixed(m.tps < 10 ? 1 : 0) + ' t/s' : '—';
    return `<div class="hcard">
      <div class="hc-head">
        <span class="hc-name">${esc(m.model)}</span>
        <span class="badge h-${shown}"><i data-lucide="${BIC[shown]}"></i>${HEALTH_LABEL[shown]}</span>
      </div>
      <div class="hc-meta">
        <span>账号 <b>${fmt(m.accounts || 0)}</b></span>
        <span>模型 <b class="mono">${esc(m.model)}</b></span>
        ${tail}
      </div>
      <div class="hc-meta"><span><b>${fmt(m.total)}</b> 总请求</span>
        <span>p50 / p95 <b class="mono">${m.p50_ms ? fmt(m.p50_ms) + ' / ' + fmt(m.p95_ms) + ' ms' : '—'}</b></span></div>
      <div class="hc-metrics">
        <div><span>成功率</span><b>${rate}</b></div>
        <div><span>成功</span><b>${fmt(m.ok)}</b></div>
        <div><span>错误</span><b class="${m.fail ? 'e' : ''}">${fmt(m.fail)}</b></div>
      </div>
      <div class="hbar">${bars}</div>
      <div class="hc-axis"><span>过去</span><span>现在</span></div>
      ${err}
      <div class="hc-foot">
        <span>近期平均首字延迟<b>${ttft}</b></span>
        <span>近期平均输出速度<b>${tps}</b></span>
      </div>
    </div>`;
  }).join('');
  icons();
}

$('#btnRefreshBal').onclick = e => run(e.currentTarget, async () => {
  const r = await api('/api/pool/refresh_balance', { method: 'POST' });
  toast(`已刷新 ${r.results.length} 个账号余额`, 'ok'); loadOverview(); health();
});
$('#btnCheckin').onclick = e => run(e.currentTarget, async () => {
  const r = await api('/api/pool/checkin', { method: 'POST' });
  const got = r.results.filter(x => x.ok).reduce((s, x) => s + (x.credit || 0), 0);
  toast(`签到完成，新增 ${got} 积分`, 'ok'); loadOverview();
});

/* ---------- pool ---------- */
function setIcon(el, name) {
  // lucide.createIcons() 会把 <i data-lucide> 整个替换成 <svg>，
  // 所以第二次切换时 querySelector('i') 是 null —— 必须连 svg 一起找，换成新的 <i> 再重绘
  const old = el.querySelector('i[data-lucide], svg');
  if (!old) return;
  const i = document.createElement('i');
  i.setAttribute('data-lucide', name);
  old.replaceWith(i);
}
function paintHideDead() {
  const b = $('#btnHideDead');
  b.classList.toggle('on', HIDE_DEAD);
  b.setAttribute('aria-pressed', HIDE_DEAD ? 'true' : 'false');
  $('#hideDeadTxt').textContent = HIDE_DEAD ? '已隐藏不可用' : '隐藏不可用';
  setIcon(b, HIDE_DEAD ? 'eye-off' : 'eye');
  icons();
}
$('#btnHideDead').onclick = () => {
  HIDE_DEAD = !HIDE_DEAD;
  localStorage.setItem('wbpool_hide_dead', HIDE_DEAD ? '1' : '0');
  paintHideDead(); loadPool();
};

async function loadPool() {
  const d = await api('/api/pool').catch(e => { toast(e.message, 'err'); return null; });
  if (!d) return;
  paintHideDead();

  const all = d.accounts || [];
  // usable 由后端算（active 且未过冷却期），前端只负责按它过滤
  const shown = HIDE_DEAD ? all.filter(a => a.usable) : all;
  const hidden = all.length - shown.length;
  const note = $('#poolFilterNote');
  if (HIDE_DEAD && hidden > 0) {
    note.innerHTML = `<i data-lucide="eye-off"></i>已隐藏 ${hidden} 个不可用账号（失效 / 停用 / 配额耗尽冷却中）`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }

  const tb = $('#poolTable tbody');
  tb.innerHTML = shown.length ? shown.map(a => `
    <tr>
      <td><div style="font-family:'JetBrains Mono',monospace">${a.masked}</div>
          <div style="font-size:11px;color:var(--txt3)">${a.label || a.uid.slice(0, 10) || '—'}</div></td>
      <td><span class="badge ${a.status}">${ST[a.status] || a.status}</span></td>
      <td class="num">${fmt(a.credits_total, 2)}</td>
      <td class="num">${fmt(a.credits_spent, 3)}</td>
      <td class="num">${a.request_count}</td>
      <td class="num">${a.expires_in_h > 0 ? a.expires_in_h + ' h' : '已过期'}</td>
      <td style="font-size:11px;color:var(--txt3)">${when(a.registered_at).slice(0, 10)}</td>
      <td style="font-size:11.5px;color:var(--txt3)">${a.last_checkin || '—'}</td>
      <td class="err-cell" title="${(a.last_error || '').replace(/"/g, '&quot;')}">${a.last_error || '—'}</td>
      <td><div class="row-actions">
        <button class="btn tiny ghost" data-act="ci" data-p="${a.phone}" title="单独签到"><i data-lucide="calendar-check-2"></i></button>
        <button class="btn tiny ghost" data-act="rt" data-p="${a.phone}" title="刷新 token"><i data-lucide="refresh-cw"></i></button>
        <button class="btn tiny ghost" data-act="tg" data-p="${a.phone}" data-s="${a.status === 'disabled' ? 'active' : 'disabled'}" title="启用/停用"><i data-lucide="power"></i></button>
        <button class="btn tiny danger" data-act="rm" data-p="${a.phone}" data-m="${esc(a.masked)}" title="移除"><i data-lucide="trash-2"></i></button>
      </div></td>
    </tr>`).join('')
    : `<tr><td colspan="10" style="text-align:center;color:var(--txt3);padding:30px">${
        all.length ? '当前筛选下没有账号，点「已隐藏不可用」可看全部' : '池子是空的'}</td></tr>`;
  icons();

  tb.querySelectorAll('button[data-act]').forEach(b => b.onclick = () => run(b, async () => {
    const p = b.dataset.p, act = b.dataset.act;
    if (act === 'ci') {
      const r = await api('/api/pool/checkin_one', { method: 'POST', body: JSON.stringify({ phone: p }) });
      if (r.skipped || r.already) toast(`${r.masked || p} 今天已签到`, 'info');
      else toast(r.ok ? `签到成功 +${r.credit} 积分，余额 ${r.credits_total}` : `签到失败: ${r.error}`, r.ok ? 'ok' : 'err');
    } else if (act === 'rm') {
      if (!await askConfirm('移除账号', `确认把 ${b.dataset.m || p} 从池中移除？移除后不再参与轮询，本地登录态一并清除。`, '移除')) return;
      await api('/api/pool/remove', { method: 'POST', body: JSON.stringify({ phone: p }) });
      toast('已移除', 'ok');
    } else if (act === 'tg') {
      await api('/api/pool/status', { method: 'POST', body: JSON.stringify({ phone: p, status: b.dataset.s }) });
      toast('状态已更新', 'ok');
    } else {
      const r = await api('/api/pool/refresh_token', { method: 'POST', body: JSON.stringify({ phone: p }) });
      toast(r.ok ? `token 已刷新，有效 ${r.expires_in_h} 小时` : `刷新失败: ${r.error}`, r.ok ? 'ok' : 'err');
    }
    loadPool(); health();
  }));
}
$('#btnPoolReload').onclick = e => run(e.currentTarget, loadPool);
$('#btnPoolBal').onclick = e => run(e.currentTarget, async () => {
  await api('/api/pool/refresh_balance', { method: 'POST' }); toast('余额已刷新', 'ok'); loadPool();
});

/* ---------- rotation strategy ---------- */
const ROT_HINT = {
  lru: '请求轮流分给每个账号，积分均匀下降',
  drain: '一直用同一个账号直到积分打光，再换下一个'
};
function paintRotation(mode) {
  $$('#rotationMode button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  $('#rotationHint').textContent = ROT_HINT[mode] || '';
}
async function loadRotation() {
  const d = await api('/api/pool/rotation').catch(() => null);
  if (d) paintRotation(d.mode);
}
$$('#rotationMode button').forEach(b => b.onclick = () => paintRotation(b.dataset.mode));
$('#btnRotationSave').onclick = e => run(e.currentTarget, async () => {
  const mode = $('#rotationMode button.on')?.dataset.mode || 'lru';
  const r = await api('/api/pool/rotation', { method: 'POST', body: JSON.stringify({ mode }) });
  if (r.ok) toast(`策略已设为${mode === 'drain' ? '优先耗尽' : '轮询'}`, 'ok');
  else toast(r.error || '设置失败', 'err');
  paintRotation(r.mode);
});
$('#btnPoolCheckin').onclick = e => run(e.currentTarget, async () => {
  const r = await api('/api/pool/checkin', { method: 'POST' });
  toast(`签到 ${r.results.filter(x => x.ok).length} 个成功`, 'ok'); loadPool();
});
$('#btnImport').onclick = e => run(e.currentTarget, async () => {
  const at = $('#impAT').value.trim();
  if (!at) return toast('access_token 不能为空', 'err');
  const r = await api('/api/pool/import', {
    method: 'POST', body: JSON.stringify({
      access_token: at, refresh_token: $('#impRT').value.trim(),
      label: $('#impLabel').value.trim(), phone: $('#impPhone').value.trim()
    })
  });
  showOut($('#impOut'), r);
  toast(`导入成功（${r.action}），积分 ${r.credits}`, 'ok');
  $('#impAT').value = $('#impRT').value = '';
  loadPool(); health();
});

/* ---------- register ---------- */
$('#btnRegStart').onclick = e => run(e.currentTarget, async () => {
  const phone = $('#regPhone').value.trim();
  if (!phone) return toast('请填写手机号', 'err');
  const body = { phone };
  if ($('#regUseProxy').checked === false) body.proxy = null;
  const r = await api('/api/register/start', { method: 'POST', body: JSON.stringify(body) });
  regSession = r.session_id;
  showOut($('#regOut'), r);
  $('#step1').classList.add('done');
  $('#step2').classList.remove('disabled');
  $('#btnRegFinish').disabled = false;
  $('#regCode').focus();
  toast(`验证码已发往 ${r.phone}（出口 ${r.proxy}）`, 'ok');
  let left = r.expires_in || 300;
  clearInterval(regTimer);
  regTimer = setInterval(() => {
    left--;
    $('#regCountdown').textContent = left > 0
      ? `会话剩余 ${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`
      : '会话已过期，请重新发码';
    if (left <= 0) { clearInterval(regTimer); $('#btnRegFinish').disabled = true; }
  }, 1000);
});

$('#btnRegFinish').onclick = e => run(e.currentTarget, async () => {
  const code = $('#regCode').value.trim();
  if (!code) return toast('请填写验证码', 'err');
  try {
    const inviteCode = ($('#regInviteManual').value.trim() || $('#regInviteSel').value || '');
    const r = await api('/api/register/finish', {
      method: 'POST',
      body: JSON.stringify({ session_id: regSession, code,
                             label: $('#regLabel').value.trim(),
                             invite_code: inviteCode })
    });
    showOut($('#regOut'), r);
    let extra = '';
    if (r.invite) extra = r.invite.ok ? '，邀请码已绑定' : `，邀请码未绑定(${r.invite.error})`;
    toast(`${r.masked} 已入池，积分 ${r.credits}${extra}`, r.invite && !r.invite.ok ? 'info' : 'ok');
    loadInviteCodes().catch(() => {});
    $('#step2').classList.add('done');
    clearInterval(regTimer); $('#regCountdown').textContent = '';
    $('#regCode').value = '';
    loadOverview(); health();
  } catch (err) {
    // 验证码填错时后端保留会话（can_retry），前端也要让用户能直接重填，
    // 不能把 step2 关掉逼他重新发码——上游发码有频率限制。
    const retry = !!(err.data && err.data.can_retry);
    showOut($('#regOut'), err.message, true);
    if (retry) {
      $('#regCode').value = '';
      $('#regCode').focus();
      $('#btnRegFinish').disabled = false;
      toast('验证码不对，直接重填即可（无需重新发码）', 'err');
      return;
    }
    throw err;
  }
});
$('#regCode').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btnRegFinish').click(); });

$('#btnInvite').onclick = e => run(e.currentTarget, async () => {
  const p = $('#invPhone').value;
  const r = await api('/api/invite?phone=' + encodeURIComponent(p));
  showOut($('#invOut'), r);
});

/* ---------- 邀请码 ---------- */
let INV_CODES = [];
async function loadInviteCodes() {
  const d = await api('/api/invite/codes');
  INV_CODES = d.codes || [];
  const ok = INV_CODES.filter(c => c.code);

  $('#inviteGrid').innerHTML = INV_CODES.length ? INV_CODES.map(c => c.code ? `
    <div class="inv-card">
      <div class="inv-top">
        <span class="inv-phone">${c.masked}</span>
        ${c.cap_reached ? '<span class="badge exhausted">已达上限</span>' : ''}
      </div>
      <div class="inv-code" data-copy="${c.code}" title="点击复制">${c.code}<i data-lucide="copy"></i></div>
      <div class="inv-stats">
        <span>已邀 <b>${c.invited}</b></span>
        <span>有效 <b>${c.valid_invited}</b></span>
        <span>得分 <b>${c.earned}</b>/${c.cap}</span>
      </div>
      <div class="inv-link" data-copy="${c.link}" title="点击复制邀请链接">复制邀请链接<i data-lucide="link"></i></div>
    </div>` : `
    <div class="inv-card bad">
      <div class="inv-top"><span class="inv-phone">${c.masked}</span></div>
      <div class="inv-err">${c.error || '取不到邀请码'}</div>
      <div class="inv-note">该账号不可用，取不到邀请码</div>
    </div>`).join('') : '<div class="chat-empty">池子是空的</div>';
  icons();

  $$('#inviteGrid [data-copy]').forEach(el => el.onclick = async () => {
    try { await navigator.clipboard.writeText(el.dataset.copy); toast('已复制', 'ok'); }
    catch { toast('复制失败，手动选中吧', 'err'); }
  });

  // 注册向导的下拉
  const sel = $('#regInviteSel');
  if (sel) {
    sel.innerHTML = '<option value="">不填</option>' + ok.map(c =>
      `<option value="${c.code}">${c.masked} · ${c.code} · 已邀 ${c.invited}</option>`).join('');
  }
  const bs = $('#bindPhone');
  if (bs) bs.innerHTML = INV_CODES.map(c => `<option value="${c.phone}">${c.masked}</option>`).join('');
  const hint = $('#regInviteHint');
  if (hint) hint.textContent = ok.length
    ? `池内有 ${ok.length} 个可用邀请码。注意：不能填被注册号自己的码。`
    : '池内暂无可用邀请码，先加一个账号。';
  return INV_CODES;
}
$('#btnInviteCodes').onclick = e => run(e.currentTarget, loadInviteCodes);

$('#btnBind').onclick = e => run(e.currentTarget, async () => {
  const phone = $('#bindPhone').value, code = $('#bindCode').value.trim();
  if (!code) return toast('邀请码不能为空', 'err');
  const r = await api('/api/invite/bind', {
    method: 'POST', body: JSON.stringify({ phone, invite_code: code })
  });
  showOut($('#bindOut'), r, !r.ok);
  toast(r.ok ? '绑定成功' : r.error, r.ok ? 'ok' : 'err');
  if (r.ok) { loadInviteCodes(); loadOverview(); }
});

/* ---------- models ---------- */
const SRC_LABEL = { console_api: '官方接口', cache: '本地缓存', static: '静态兜底' };
const fmtCtx = n => !n ? '' : (n >= 1e6 ? (n / 1e6) + 'M' : Math.round(n / 1000) + 'K');

async function loadModels(force) {
  const d = await api('/api/models' + (force ? '?force=true' : '')).catch(e => { toast(e.message, 'err'); return null; });
  if (!d) return;
  MODELS = d.models || [];
  const rows = d.details || [];

  const src = d.source || 'cache';
  const when = d.probed_at ? new Date(d.probed_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
  $('#modelState').innerHTML =
    `<span class="ms-pill ${src}"><i data-lucide="${src === 'console_api' ? 'cloud-download' : src === 'static' ? 'hard-drive' : 'database'}"></i>${SRC_LABEL[src] || src}</span>` +
    `<span class="ms-item"><i data-lucide="boxes"></i>${MODELS.length} 个模型</span>` +
    `<span class="ms-item"><i data-lucide="clock"></i>${when}</span>` +
    (d.error ? `<span class="ms-item err"><i data-lucide="triangle-alert"></i>${d.error}</span>` : '');

  $('#modelGrid').innerHTML = rows.length ? rows.map(x => {
    const caps = [
      x.supports_reasoning ? ['brain', '推理'] : null,
      x.supports_images ? ['image', '图像'] : null,
      x.supports_tool_call ? ['wrench', '工具'] : null,
    ].filter(Boolean);
    const mult = (x.credits || '').replace(/\s*credits?$/i, '');
    return `<div class="model-card ok">
      ${x.is_default ? '<span class="model-def">默认</span>' : ''}
      <div class="model-name">${x.id}</div>
      <div class="model-alias">${x.name || x.id}</div>
      <div class="model-tags">
        <span class="tag tier">${x.vendor_label || 'codebuddy'}</span>
        ${x.ctx ? `<span class="tag">${fmtCtx(x.ctx)} ctx</span>` : ''}
        ${x.max_output_tokens ? `<span class="tag">${fmtCtx(x.max_output_tokens)} out</span>` : ''}
        ${mult ? `<span class="tag rate">${mult}</span>` : ''}
        ${x.unlisted ? `<span class="tag unlisted" title="官方 console 模型表未列出，实测可正常调用">官方未列</span>` : ''}
      </div>
      ${caps.length ? `<div class="model-caps">${caps.map(([ic, t]) =>
        `<span class="cap"><i data-lucide="${ic}"></i>${t}</span>`).join('')}</div>` : ''}
      ${x.desc ? `<div class="model-desc" title="${x.desc.replace(/"/g, '&quot;')}">${x.desc}</div>` : ''}
    </div>`;
  }).join('') : '<div class="chat-empty">没有模型数据，点「同步上游」</div>';
  icons();

  const sel = $('#chatModel');
  sel.innerHTML = rows.length
    ? rows.map(x => `<option value="${x.id}"${x.is_default ? ' selected' : ''}>${x.id}</option>`).join('')
    : MODELS.map(m => `<option>${m}</option>`).join('');

  if (d.error) toast(d.error, 'err');
  else if (force) toast(`已从${SRC_LABEL[src] || src}同步 ${MODELS.length} 个模型`, 'ok');
}
$('#btnModels').onclick = e => run(e.currentTarget, () => loadModels(false));
$('#btnModelsProbe').onclick = e => run(e.currentTarget, () => loadModels(true));

$('#btnRates').onclick = e => run(e.currentTarget, async () => {
  const d = await api('/api/rates');
  renderRates(d);
});
$('#btnMeasure').onclick = e => run(e.currentTarget, async () => {
  toast('实测中，每个模型要等上游异步结算，请耐心等待', 'info');
  const d = await api('/api/rates/measure', { method: 'POST', body: JSON.stringify({}) });
  showOut($('#rateOut'), d.results);
  toast('实测完成', 'ok');
  renderRates(await api('/api/rates'));
});
function renderRates(d) {
  const rows = d.rows || [], meta = d.meta || {};
  const tb = $('#rateTable tbody');
  tb.innerHTML = rows.length ? rows.map(x => {
    const m = meta[x.model] || {};
    const hi = x.model === d.base_model ? ' style="color:var(--ok)"' : '';
    return `<tr><td style="font-family:'JetBrains Mono',monospace">${x.model}</td>
      <td>${m.vendor_label || m.vendor || '—'}</td>
      <td class="num">${x.credits ?? '—'}</td>
      <td class="num">${(x.total_tokens ?? 0).toLocaleString()}</td>
      <td class="num">${x.credits_per_1k ?? '—'}</td>
      <td class="num"${hi}>${x.multiplier ? x.multiplier.toFixed(2) + '×' : '—'}</td></tr>`;
  }).join('') : '<tr><td colspan="6" style="text-align:center;color:var(--txt3);padding:26px">还没有记账数据。跑几次对话，或点「实测一轮」</td></tr>';
  if (d.base_model) $('#rateOut').className = 'out';
}

/* ---------- proxy ---------- */
async function loadProxy() {
  const d = await api('/api/proxy').catch(e => { toast(e.message, 'err'); return null; });
  if (!d) return;
  $$('#proxyMode button').forEach(b => b.classList.toggle('on', b.dataset.mode === d.mode));
  $('#proxyFixedUrl').value = d.fixed_url || '';
  const res = d.results || [];
  $('#proxyGrid').innerHTML = res.length ? res.map(x => `
    <div class="px ${x.ok ? 'ok' : 'bad'}">
      <div class="px-head"><span class="px-cc">${x.cc}</span><span class="px-port">:${x.port}</span></div>
      <div class="px-ip">${x.ip || '—'}</div>
      <div class="px-state">${x.ok ? '目标可达' : (x.detail || '不可用').slice(0, 40)}</div>
    </div>`).join('') : '<div class="chat-empty">还没探活，点「全量探活」</div>';
  icons();
}
$$('#proxyMode button').forEach(b => b.onclick = () => {
  $$('#proxyMode button').forEach(x => x.classList.remove('on')); b.classList.add('on');
});
$('#btnProxyGet').onclick = e => run(e.currentTarget, loadProxy);
$('#btnProxyProbe').onclick = e => run(e.currentTarget, async () => {
  const r = await api('/api/proxy/probe', { method: 'POST' });
  toast(`探活完成，${r.usable.length}/${r.results.length} 个出口可达`, 'ok');
  loadProxy();
});
$('#btnProxyMode').onclick = e => run(e.currentTarget, async () => {
  const mode = $('#proxyMode button.on')?.dataset.mode || 'off';
  const r = await api('/api/proxy/mode', {
    method: 'POST', body: JSON.stringify({ mode, url: $('#proxyFixedUrl').value.trim() })
  });
  toast(`模式已设为 ${r.mode}`, 'ok'); health();
});

/* ---------- chat ---------- */
function addMsg(role, text = '') {
  const d = document.createElement('div');
  d.className = `msg ${role}`;
  d.innerHTML = `<div class="msg-av"><i data-lucide="${role === 'user' ? 'user' : 'sparkles'}"></i></div>
                 <div class="msg-body"></div>`;
  $('#chatLog').appendChild(d);
  d.querySelector('.msg-body').textContent = text;
  icons();
  $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
  return d.querySelector('.msg-body');
}
$('#btnChatClear').onclick = () => { $('#chatLog').innerHTML = ''; $('#chatMeta').textContent = ''; };

/* 账号下拉：切到对话调试时自动拉取，让用户指定要用哪个号 */
async function loadChatAccounts() {
  const d = await api('/api/pool').catch(() => null);
  const sel = $('#chatAccount');
  if (!d) return;
  sel.innerHTML = '<option value="">自动选号</option>' +
    d.accounts.map(a =>
      `<option value="${a.phone}">${a.masked}${a.status !== 'active' ? ' [' + (a.status || '?') + ']' : ''} · ${(a.credits_total ?? '?').toString().slice(0,7)} 积分</option>`
    ).join('');
}


async function send() {
  const inp = $('#chatInput'), text = inp.value.trim();
  if (!text) return;
  const model = $('#chatModel').value || 'default';
  const stream = $('#chatStream').checked;
  addMsg('user', text); inp.value = '';
  const body = addMsg('bot', '');
  body.innerHTML = '<span class="cursor"></span>';
  const t0 = performance.now();
  const btn = $('#btnChatSend'); busy(btn, true);

  const h = { 'Content-Type': 'application/json' };
  if (KEY) h['Authorization'] = `Bearer ${KEY}`;
  const forceAcc = ($('#chatAccount')?.value || '').trim();
  if (forceAcc) h['X-WB-Force-Account'] = forceAcc;
  try {
    const r = await fetch('/v1/chat/completions', {
      method: 'POST', headers: h,
      body: JSON.stringify({ model, messages: [{ role: 'user', content: text }], stream })
    });
    const acct = r.headers.get('X-WB-Account') || '';
    if (!r.ok) {
      const t = await r.text();
      body.textContent = `[错误] ${t.slice(0, 500)}`;
      $('#chatMeta').textContent = `HTTP ${r.status}`;
      return;
    }
    let out = '', think = '', usage = null, firstAt = null;
    if (stream) {
      const rd = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n'); buf = lines.pop();
        for (const ln of lines) {
          if (!ln.startsWith('data:')) continue;
          const p = ln.slice(5).trim();
          if (p === '[DONE]') continue;
          let j; try { j = JSON.parse(p); } catch { continue; }
          if (j.error) { out += `\n[错误] ${j.error.message}`; continue; }
          usage = j.usage || usage;
          for (const c of j.choices || []) {
            const d = c.delta || {};
            if (d.reasoning_content) think += d.reasoning_content;
            if (d.content) { out += d.content; firstAt = firstAt ?? performance.now(); }
          }
          body.innerHTML = (think ? `<div class="msg-think">${esc(think)}</div>` : '') +
                           esc(out) + '<span class="cursor"></span>';
          $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
        }
      }
    } else {
      const j = await r.json();
      const m = j.choices?.[0]?.message || {};
      out = m.content || ''; think = m.reasoning_content || ''; usage = j.usage;
    }
    body.innerHTML = (think ? `<div class="msg-think">${esc(think)}</div>` : '') + esc(out);
    const dt = ((performance.now() - t0) / 1000).toFixed(2);
    const ttfb = firstAt ? ((firstAt - t0) / 1000).toFixed(2) : '—';
    $('#chatMeta').textContent =
      `模型 ${model} · 账号 ${acct} · 总耗时 ${dt}s · 首字 ${ttfb}s` +
      (usage ? ` · tokens ${usage.prompt_tokens}+${usage.completion_tokens}=${usage.total_tokens}` : '');
  } catch (e) {
    body.textContent = `[异常] ${e.message}`;
  } finally { busy(btn, false); }
}
const esc = s => String(s).replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
$('#btnChatSend').onclick = send;
$('#chatInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
});

/* ---------- API 密钥 ---------- */
async function loadKeys() {
  const d = await api('/api/keys').catch(e => { toast(e.message, 'err'); return null; });
  if (!d) return;
  const keys = d.keys || [];
  $('#keyEmpty').hidden = keys.length > 0;
  const tb = $('#keyTable tbody');
  tb.innerHTML = keys.map(k => {
    const isEnv = k.id === 'env';
    return `<tr class="${k.enabled ? '' : 'row-off'}">
      <td>
        <div class="k-name">${esc(k.name || '未命名')}</div>
        ${isEnv ? '<div class="k-sub">来自 .env 的 WB_API_KEY</div>'
                : (k.note ? `<div class="k-sub">${esc(k.note)}</div>` : '')}
      </td>
      <td><code class="mono k-mask">${esc(k.masked)}</code>
        ${isEnv ? '' : `<button class="btn tiny ghost" data-act="reveal" data-k="${k.id}" title="查看完整密钥"><i data-lucide="eye"></i></button>`}
      </td>
      <td><span class="badge ${k.enabled ? 'active' : 'disabled'}">${k.enabled ? '启用' : '已停用'}</span></td>
      <td class="num">${fmt(k.request_count)}</td>
      <td class="num">${fmt(k.tokens)}</td>
      <td class="num">${fmt(k.credits, 3)}</td>
      <td style="font-size:11.5px;color:var(--txt3)">${ago(k.last_used)}</td>
      <td style="font-size:11px;color:var(--txt3)">${when(k.created_at)}</td>
      <td><div class="row-actions">
        ${isEnv ? '<span class="k-sub">—</span>' : `
        <button class="btn tiny ghost" data-act="toggle" data-k="${k.id}" data-en="${k.enabled ? 0 : 1}" title="${k.enabled ? '停用' : '启用'}"><i data-lucide="power"></i></button>
        <button class="btn tiny ghost" data-act="rename" data-k="${k.id}" data-n="${esc(k.name || '')}" title="改名"><i data-lucide="pencil"></i></button>
        <button class="btn tiny ghost" data-act="rotate" data-k="${k.id}" title="重新生成"><i data-lucide="refresh-cw"></i></button>
        <button class="btn tiny danger" data-act="del" data-k="${k.id}" title="删除"><i data-lucide="trash-2"></i></button>`}
      </div></td>
    </tr>`;
  }).join('');
  icons();

  tb.querySelectorAll('button[data-act]').forEach(b => b.onclick = () => run(b, async () => {
    const id = b.dataset.k, act = b.dataset.act;
    if (act === 'reveal') {
      const r = await api(`/api/keys/${id}/reveal`);
      showNewKey(r.key, '完整密钥');
    } else if (act === 'toggle') {
      await api(`/api/keys/${id}/update`, {
        method: 'POST', body: JSON.stringify({ enabled: b.dataset.en === '1' }),
      });
      toast('已更新', 'ok');
    } else if (act === 'rename') {
      const name = await askText('给密钥改名', '名称', b.dataset.n);
      if (name === null) return;
      await api(`/api/keys/${id}/update`, { method: 'POST', body: JSON.stringify({ name }) });
      toast('已改名', 'ok');
    } else if (act === 'rotate') {
      if (!await askConfirm('重新生成密钥', '旧密钥会立刻失效，正在用它的客户端需要换成新密钥。', '重新生成')) return;
      const r = await api(`/api/keys/${id}/rotate`, { method: 'POST' });
      showNewKey(r.key, '密钥已重新生成');
      toast('已重新生成', 'ok');
    } else if (act === 'del') {
      if (!await askConfirm('删除密钥', '删除后用这把密钥的客户端会立刻收到 401，且无法恢复。', '删除')) return;
      await api(`/api/keys/${id}`, { method: 'DELETE' });
      toast('已删除', 'ok');
    }
    loadKeys();
  }));
}
function showNewKey(key, title) {
  $('#keyNewBox').hidden = false;
  $('#keyNewVal').textContent = key;
  $('#keyNewBox').querySelector('.keynew-head').innerHTML =
    `<i data-lucide="party-popper"></i>${title || '新密钥已生成，请立即复制保存'}`;
  icons();
}
$('#btnKeyNewClose').onclick = () => { $('#keyNewBox').hidden = true; };
$('#btnKeyNewCopy').onclick = async () => {
  const v = $('#keyNewVal').textContent;
  try { await navigator.clipboard.writeText(v); toast('已复制到剪贴板', 'ok'); }
  catch { // http 页面没有 clipboard 权限时退回选中
    const r = document.createRange(); r.selectNode($('#keyNewVal'));
    getSelection().removeAllRanges(); getSelection().addRange(r);
    toast('已选中，按 Ctrl+C 复制', 'info');
  }
};
$('#btnKeysRefresh').onclick = e => run(e.currentTarget, loadKeys);
$('#btnKeyNew').onclick = e => run(e.currentTarget, async () => {
  const name = await askText('生成新密钥', '名称', '默认',
    '给这把密钥起个名字，方便以后区分用途（例如 cherry-studio / 我的手机）。');
  if (name === null) return;
  const r = await api('/api/keys', { method: 'POST', body: JSON.stringify({ name: name.trim() || '默认' }) });
  showNewKey(r.key, '新密钥已生成，请立即复制保存');
  toast('已生成', 'ok');
  loadKeys();
});

/* ---------- api tab ---------- */
function fillApi() {
  const base = location.origin;
  $('#apiBase').textContent = base + '/v1';
  $('#apiBase2').textContent = base + '/v1';
  $('#curlSample').textContent =
`# OpenAI 兼容（流式）
curl -N ${base}/v1/chat/completions \\
  -H "Authorization: Bearer $WB_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${MODELS[0] || 'deepseek-v3'}","messages":[{"role":"user","content":"你好"}],"stream":true}'

# OpenAI 兼容（非流式，本代理把上游流聚合成完整 JSON）
curl ${base}/v1/chat/completions \\
  -H "Authorization: Bearer $WB_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${MODELS[0] || 'deepseek-v3'}","messages":[{"role":"user","content":"你好"}]}'

# Anthropic 兼容
curl ${base}/v1/messages \\
  -H "x-api-key: $WB_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${MODELS[0] || 'deepseek-v3'}","max_tokens":512,"messages":[{"role":"user","content":"你好"}]}'

# 模型列表
curl ${base}/v1/models -H "Authorization: Bearer $WB_API_KEY"`;
}

/* ---------- boot ---------- */
let bootHealthTimer = null;
function boot() {
  icons();
  health(); loadOverview(); loadHealthBars(); fillApi();
  loadModels(false).catch(() => {});
  if (!bootHealthTimer) bootHealthTimer = setInterval(() => { if (ME) health(); }, 30000);
}
icons();
checkAuth().then(ok => { if (ok) boot(); });

/* ---------- 自动注册（uoomsg） ---------- */
let autoRegPollTimer = null;

$('#btnUoomsgBalance').onclick = e => run(e.currentTarget, async () => {
  const d = await api('/api/uoomsg/balance');
  $('#uoomsgBalHint').textContent = `uoomsg 余额：${d.balance}`;
});

async function renderAutoRegTasks() {
  const d = await api('/api/auto_register/tasks');
  const tasks = (d.tasks || []).slice().reverse(); // 最新的在前
  const el = $('#autoRegTasks');
  if (!tasks.length) {
    el.innerHTML = '<div class="chat-empty" style="padding:12px 0">暂无任务</div>';
    return;
  }
  el.innerHTML = tasks.map(t => {
    const icon = t.status === 'done' ? '✓' : t.status === 'failed' ? '✗' : t.status === 'running' ? '⟳' : '…';
    const color = t.status === 'done' ? 'var(--ok)' : t.status === 'failed' ? 'var(--err)' : 'var(--acc)';
    const last = t.steps.length ? t.steps[t.steps.length - 1] : '';
    const masked = t.result && t.result.masked ? t.result.masked : '';
    return `<div class="task-row" data-tid="${t.id}" style="border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:6px;cursor:pointer" onclick="toggleTaskLog('${t.id}')">
      <div style="display:flex;gap:8px;align-items:center">
        <span style="color:${color};font-weight:700;min-width:16px">${icon}</span>
        <span style="flex:1;font-size:13px">${masked || `任务 ${t.id}`} <span style="color:var(--txt3);font-size:11px">${t.age}s 前</span></span>
        <span style="font-size:11px;color:var(--txt3)">${t.status}</span>
      </div>
      <div style="font-size:11px;color:var(--txt3);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${last}</div>
      <pre id="tasklog-${t.id}" style="display:none;margin-top:8px;font-size:11px;white-space:pre-wrap;color:var(--txt2)">${(t.steps || []).join('\n')}</pre>
    </div>`;
  }).join('');
}

function toggleTaskLog(tid) {
  const el = $(`#tasklog-${tid}`);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function startAutoRegPoll() {
  if (autoRegPollTimer) return;
  autoRegPollTimer = setInterval(async () => {
    await renderAutoRegTasks().catch(() => {});
    // 如果没有运行中的任务就停止轮询
    const d = await api('/api/auto_register/tasks').catch(() => ({ tasks: [] }));
    const running = (d.tasks || []).some(t => t.status === 'running' || t.status === 'pending');
    if (!running) {
      clearInterval(autoRegPollTimer);
      autoRegPollTimer = null;
      loadOverview().catch(() => {});
      loadInviteCodes().catch(() => {});
    }
  }, 3000);
}

$('#btnAutoReg').onclick = e => run(e.currentTarget, async () => {
  const inviteCode = ($('#autoRegInviteManual').value.trim() || $('#autoRegInviteSel').value || '');
  const label = $('#autoRegLabel').value.trim();
  const count = Math.max(1, Math.min(20, Number($('#autoRegCount').value) || 1));
  const r = await api('/api/auto_register/start', {
    method: 'POST',
    body: JSON.stringify({ invite_code: inviteCode, label, count }),
  });
  toast(`已启动 ${r.count || count} 个自动注册任务`, 'ok');
  await renderAutoRegTasks().catch(() => {});
  startAutoRegPoll();
});

$('#btnAutoRegRefresh').onclick = e => run(e.currentTarget, renderAutoRegTasks);

// 把 autoRegInviteSel 同步进 loadInviteCodes
const _origLoad = loadInviteCodes;
loadInviteCodes = async function () {
  const res = await _origLoad();
  const ok = (res || []).filter(c => c.code);
  const sel = $('#autoRegInviteSel');
  if (sel) {
    sel.innerHTML = '<option value="">不填</option>' + ok.map(c =>
      `<option value="${c.code}">${c.masked} · ${c.code}</option>`).join('');
  }
  return res;
};

// 进入 register tab 时刷一次任务列表
const _origTab = document.querySelectorAll ? null : null;
document.querySelectorAll('.tab[data-view]').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.dataset.view === 'register') {
      renderAutoRegTasks().catch(() => {});
      api('/api/uoomsg/balance').then(d => {
        $('#uoomsgBalHint').textContent = `uoomsg 余额：${d.balance}`;
      }).catch(() => {});
    }
  });
});
