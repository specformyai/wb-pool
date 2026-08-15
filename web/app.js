/* wb-pool WebUI */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const LS = 'wbpool_admin_key';
let KEY = localStorage.getItem(LS) || '';
let MODELS = [];
let regSession = null, regTimer = null;

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

/* ---------- fetch ---------- */
async function api(path, opts = {}) {
  const h = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (KEY) h['Authorization'] = `Bearer ${KEY}`;
  const r = await fetch(path, { ...opts, headers: h });
  const txt = await r.text();
  let data; try { data = JSON.parse(txt); } catch { data = { raw: txt }; }
  if (!r.ok) {
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
  if (v === 'proxy') loadProxy();
  if (v === 'overview') loadOverview();
  if (v === 'chat') loadChatAccounts().catch(() => {});
  if (v === 'api') fillApi();
});

/* ---------- key ---------- */
$('#adminKey').value = KEY;
$('#saveKey').onclick = () => {
  KEY = $('#adminKey').value.trim();
  localStorage.setItem(LS, KEY);
  toast('密钥已保存', 'ok');
  health(); loadOverview();
};
$('#adminKey').addEventListener('keydown', e => { if (e.key === 'Enter') $('#saveKey').click(); });

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

async function loadOverview() {
  let d;
  try { d = await api('/api/pool'); } catch (e) { return; }
  const s = d.stats;
  $('#heroCredits').textContent = fmt(s.credits_total, 2);
  $('#heroUsable').textContent = s.usable;
  $('#heroTotal').textContent = s.total;
  $('#heroReq').textContent = fmt(s.requests);
  $('#heroSpent').textContent = fmt(s.credits_spent, 2);

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
async function loadPool() {
  const d = await api('/api/pool').catch(e => { toast(e.message, 'err'); return null; });
  if (!d) return;
  const tb = $('#poolTable tbody');
  tb.innerHTML = d.accounts.length ? d.accounts.map(a => `
    <tr>
      <td><div style="font-family:'JetBrains Mono',monospace">${a.masked}</div>
          <div style="font-size:11px;color:var(--txt3)">${a.label || a.uid.slice(0, 10) || '—'}</div></td>
      <td><span class="badge ${a.status}">${ST[a.status] || a.status}</span></td>
      <td class="num">${fmt(a.credits_total, 2)}</td>
      <td class="num">${fmt(a.credits_spent, 3)}</td>
      <td class="num">${a.request_count}</td>
      <td class="num">${a.expires_in_h > 0 ? a.expires_in_h + ' h' : '已过期'}</td>
      <td style="font-size:11px;color:var(--txt3)">${a.registered_at ? a.registered_at.slice(0, 10) : '—'}</td>
      <td style="font-size:11.5px;color:var(--txt3)">${a.last_checkin || '—'}</td>
      <td class="err-cell" title="${(a.last_error || '').replace(/"/g, '&quot;')}">${a.last_error || '—'}</td>
      <td><div class="row-actions">
        <button class="btn tiny ghost" data-act="ci" data-p="${a.phone}" title="单独签到"><i data-lucide="calendar-check-2"></i></button>
        <button class="btn tiny ghost" data-act="rt" data-p="${a.phone}" title="刷新 token"><i data-lucide="refresh-cw"></i></button>
        <button class="btn tiny ghost" data-act="tg" data-p="${a.phone}" data-s="${a.status === 'disabled' ? 'active' : 'disabled'}" title="启用/停用"><i data-lucide="power"></i></button>
        <button class="btn tiny danger" data-act="rm" data-p="${a.phone}" title="移除"><i data-lucide="trash-2"></i></button>
      </div></td>
    </tr>`).join('') : '<tr><td colspan="10" style="text-align:center;color:var(--txt3);padding:30px">池子是空的</td></tr>';
  icons();

  tb.querySelectorAll('button[data-act]').forEach(b => b.onclick = () => run(b, async () => {
    const p = b.dataset.p, act = b.dataset.act;
    if (act === 'ci') {
      const r = await api('/api/pool/checkin_one', { method: 'POST', body: JSON.stringify({ phone: p }) });
      if (r.skipped || r.already) toast(`${r.masked || p} 今天已签到`, 'info');
      else toast(r.ok ? `签到成功 +${r.credit} 积分，余额 ${r.credits_total}` : `签到失败: ${r.error}`, r.ok ? 'ok' : 'err');
    } else if (act === 'rm') {
      if (!confirm(`确认从池中移除 ${p}？`)) return;
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
const esc = s => String(s).replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
$('#btnChatSend').onclick = send;
$('#chatInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
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
icons();
health(); loadOverview(); fillApi();
loadModels(false).catch(() => {});
setInterval(health, 30000);

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
  const r = await api('/api/auto_register/start', {
    method: 'POST',
    body: JSON.stringify({ invite_code: inviteCode, label }),
  });
  toast(`自动注册任务已启动（${r.task_id}）`, 'ok');
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
