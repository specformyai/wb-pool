/* ============================================================
 * settings.js —— 设置页
 *
 * 汇总四组端点：
 *   /api/auth/*    改密码、登录态
 *   /api/admin/*   模型缓存状态、手动同步
 *   /api/models    模型清单
 *   /api/scheduler 后台定时任务
 * ============================================================ */

import {
  $, apiFetch, fmtInt, fmtTime, toast, escapeHtml, confirmDialog,
  skeleton, errorState, refreshIcons, el, copyText,
} from '@/shared.js';

const state = { auth: null, cache: null, models: null, jobs: null, err: '', loading: true };

async function loadAll() {
  const jobs = {
    auth: () => apiFetch('/api/auth/state'),
    cache: () => apiFetch('/api/admin/models-cache-status'),
    models: () => apiFetch('/api/models'),
    jobs: () => apiFetch('/api/scheduler'),
  };
  const entries = await Promise.all(
    Object.entries(jobs).map(async ([k, fn]) => {
      try { return [k, { ok: true, d: await fn() }]; }
      catch (e) { return [k, { ok: false, err: e.message }]; }
    })
  );
  return Object.fromEntries(entries);
}

/* ---------------------------------------------------------------- 模型缓存 */

function renderCache(c) {
  if (!c.ok) {
    return `<div class="card"><div class="card-h"><h3>模型缓存</h3></div>
      <div class="err-msg" style="color:var(--bad)">${escapeHtml(c.err)}</div></div>`;
  }
  const d = c.d;
  // exists=false 时走静态兜底表，这是「能用但不准」的状态，必须显式提示可以点同步
  const cls = !d.exists ? 'warn' : (d.expired ? 'warn' : 'ok');
  const txt = !d.exists ? '未建立' : (d.expired ? '已过期' : '有效');
  return `<div class="card">
    <div class="card-h">
      <div><h3>模型缓存</h3><p>/v1/models 的数据来源</p></div>
      <div class="acts">
        <span class="badge ${cls}">${txt}</span>
        <button class="btn sm" data-act="sync"><i data-lucide="refresh-cw"></i><span>立即同步</span></button>
      </div>
    </div>
    <div class="kv-grid">
      <div class="kv"><span>数据来源</span><b class="mono">${escapeHtml(d.source || '—')}</b></div>
      <div class="kv"><span>可用模型</span><b>${fmtInt(d.available_count)}</b></div>
      ${d.static_fallback_count != null
        ? `<div class="kv"><span>静态兜底表</span><b>${fmtInt(d.static_fallback_count)}</b></div>` : ''}
      ${d.synced_at ? `<div class="kv"><span>同步时间</span><b>${fmtTime(d.synced_at)}</b></div>` : ''}
      ${d.age_hours != null ? `<div class="kv"><span>缓存年龄</span><b>${d.age_hours} 小时</b></div>` : ''}
      ${d.in_fail_cooldown
        ? `<div class="kv"><span>失败冷却</span><b style="color:var(--warn)">进行中</b></div>` : ''}
    </div>
    ${d.message ? `<div class="note warn-note"><i data-lucide="info"></i>
      <span>${escapeHtml(d.message)}</span></div>` : ''}
    ${d.last_error ? `<div class="note bad-note"><i data-lucide="alert-circle"></i>
      <span>最近错误：${escapeHtml(d.last_error)}</span></div>` : ''}
  </div>`;
}

/* ---------------------------------------------------------------- 模型清单 */

function renderModels(m, filter = '') {
  if (!m.ok) {
    return `<div class="card"><div class="card-h"><h3>模型清单</h3></div>
      <div class="err-msg" style="color:var(--bad)">${escapeHtml(m.err)}</div></div>`;
  }
  const d = m.d;
  const details = d.details || [];
  const q = filter.trim().toLowerCase();
  const list = q ? details.filter((x) =>
    String(x.id).toLowerCase().includes(q) ||
    String(x.owned_by || '').toLowerCase().includes(q)) : details;

  const rows = list.map((x) => `<tr>
    <td><span class="mono">${escapeHtml(x.id)}</span></td>
    <td>${x.owned_by ? `<span class="badge nodot idle">${escapeHtml(x.owned_by)}</span>` : '—'}</td>
    <td class="num">${x.context_length ? fmtInt(x.context_length) : '—'}</td>
    <td><button class="btn icon sm" data-copy="${escapeHtml(x.id)}"
      title="复制模型 ID"><i data-lucide="copy"></i></button></td>
  </tr>`).join('');

  return `<div class="card" style="padding:0;overflow:hidden">
    <div class="card-h" style="padding:16px 18px 0;margin-bottom:12px">
      <div><h3>模型清单</h3>
        <p>${fmtInt(details.length)} 个可用${d.probed_at ? ' · ' + fmtTime(d.probed_at) + '探测' : ''}
        ${d.source ? ' · 来源 ' + escapeHtml(d.source) : ''}</p></div>
      <div class="acts" style="min-width:190px">
        <input type="search" id="mdlQ" placeholder="筛选模型或厂商…" value="${escapeHtml(filter)}" />
      </div>
    </div>
    ${d.error ? `<div class="note bad-note" style="margin:0 18px 12px">
      <i data-lucide="alert-circle"></i><span>${escapeHtml(d.error)}</span></div>` : ''}
    ${list.length
      ? `<div class="tbl-scroll" style="max-height:420px;overflow-y:auto"><table class="tbl">
          <thead><tr><th>模型 ID</th><th>厂商</th><th class="num">上下文</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
      : `<div class="empty-state"><i data-lucide="search-x"></i>
          <div class="t">没有匹配的模型</div><div class="s">换个关键词试试</div></div>`}
  </div>`;
}

/* ---------------------------------------------------------------- 定时任务 */

function renderJobs(j) {
  if (!j.ok) {
    return `<div class="card"><div class="card-h"><h3>后台任务</h3></div>
      <div class="err-msg" style="color:var(--bad)">${escapeHtml(j.err)}</div></div>`;
  }
  const list = j.d.jobs || [];
  return `<div class="card">
    <div class="card-h"><div><h3>后台任务</h3>
      <p>APScheduler 注册的定时作业</p></div>
      <span class="badge acc">${fmtInt(list.length)}</span></div>
    ${list.length ? `<div class="job-list">${list.map((x) => `
      <div class="job">
        <div class="job-ic"><i data-lucide="clock"></i></div>
        <div style="min-width:0">
          <div class="job-n">${escapeHtml(x.name || x.id)}</div>
          <div class="job-s mono">${escapeHtml(x.id)}</div>
        </div>
        <div class="job-t">${x.next_run && x.next_run !== 'None'
          ? escapeHtml(x.next_run) : '<span style="color:var(--fg-3)">未排期</span>'}</div>
      </div>`).join('')}</div>`
      : `<div class="empty-state"><i data-lucide="calendar-off"></i>
          <div class="t">没有定时任务</div></div>`}
  </div>`;
}

/* ---------------------------------------------------------------- 账户安全 */

function renderAuth(a) {
  const enabled = a.ok ? !!(a.d.enabled ?? a.d.auth_enabled ?? a.d.required) : false;
  const user = a.ok ? (a.d.user ?? a.d.username ?? '') : '';
  return `<div class="card">
    <div class="card-h"><div><h3>账户安全</h3>
      <p>WebUI 登录凭据</p></div>
      <span class="badge ${enabled ? 'ok' : 'warn'}">${enabled ? '已启用鉴权' : '未启用鉴权'}</span></div>
    ${!enabled ? `<div class="note warn-note"><i data-lucide="shield-alert"></i>
      <span>当前后台没有登录保护。如果这个服务暴露在公网上，任何人都能读到账号池和 API Key，
      建议配置管理员密码后再对外开放。</span></div>` : ''}
    ${user ? `<div class="kv-grid"><div class="kv"><span>当前登录</span>
      <b class="mono">${escapeHtml(user)}</b></div></div>` : ''}
    <div class="acts" style="display:flex;gap:8px;margin-top:14px">
      <button class="btn" data-act="passwd"><i data-lucide="key-round"></i><span>修改密码</span></button>
      ${enabled ? `<button class="btn ghost" data-act="logout">
        <i data-lucide="log-out"></i><span>退出登录</span></button>` : ''}
    </div>
  </div>`;
}

function openPasswordModal(onDone) {
  const html = `
    <h3>修改密码</h3>
    <p class="modal-msg">改完会立即生效，当前会话可能需要重新登录。</p>
    <div class="modal-body">
      <div class="field"><label>当前密码</label>
        <input type="password" id="pwOld" autocomplete="current-password" /></div>
      <div class="field"><label>新密码</label>
        <input type="password" id="pwNew" autocomplete="new-password" /></div>
      <div class="field"><label>确认新密码</label>
        <input type="password" id="pwNew2" autocomplete="new-password" /></div>
      <div id="pwErr" class="note bad-note" hidden><i data-lucide="alert-circle"></i><span></span></div>
    </div>
    <div class="modal-foot">
      <button class="btn ghost" data-close>取消</button>
      <button class="btn primary" data-act="save">保存</button>
    </div>`;
  import('@/shared.js').then(({ openModal }) => {
    const { box, close } = openModal(html, { size: 'sm' });
    const showErr = (m) => {
      const n = box.querySelector('#pwErr');
      n.hidden = false;
      n.querySelector('span').textContent = m;
    };
    box.querySelector('[data-act="save"]').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const old = box.querySelector('#pwOld').value;
      const a = box.querySelector('#pwNew').value;
      const b = box.querySelector('#pwNew2').value;
      if (!a) return showErr('新密码不能为空');
      if (a !== b) return showErr('两次输入的新密码不一致');
      if (a.length < 6) return showErr('新密码至少 6 位');
      btn.disabled = true;
      try {
        await apiFetch('/api/auth/password', { method: 'POST', body: { old_password: old, new_password: a } });
        close();
        toast('密码已修改', 'ok');
        onDone?.();
      } catch (err) {
        showErr(err.message);
        btn.disabled = false;
      }
    });
  });
}

/* ---------------------------------------------------------------- 渲染 */

let modelFilter = '';

function render(root) {
  if (state.loading && !state.auth) {
    root.innerHTML = `<div class="page-head"><div><h1>设置</h1>
      <p>鉴权、模型缓存与后台任务</p></div></div>
      <div class="card">${skeleton(5)}</div><div class="card" style="margin-top:14px">${skeleton(5)}</div>`;
    refreshIcons();
    return;
  }
  if (state.err) {
    root.innerHTML = `<div class="page-head"><div><h1>设置</h1></div></div>`;
    root.appendChild(errorState(state.err, () => boot(root)));
    refreshIcons();
    return;
  }
  root.innerHTML = `
    <div class="page-head">
      <div><h1>设置</h1><p>鉴权、模型缓存与后台任务</p></div>
      <div class="acts">
        <button class="btn ghost" data-act="reload"><i data-lucide="rotate-cw"></i><span>刷新</span></button>
      </div>
    </div>
    <div class="set-grid">
      ${renderAuth(state.auth)}
      ${renderCache(state.cache)}
    </div>
    <div style="margin-top:14px">${renderModels(state.models, modelFilter)}</div>
    <div style="margin-top:14px">${renderJobs(state.jobs)}</div>`;
  refreshIcons();
  bind(root);
}

function bind(root) {
  root.querySelector('[data-act="reload"]')?.addEventListener('click', () => boot(root));
  root.querySelector('[data-act="passwd"]')?.addEventListener('click', () =>
    openPasswordModal(() => boot(root)));

  root.querySelector('[data-act="logout"]')?.addEventListener('click', async () => {
    if (!(await confirmDialog('退出登录', '确认退出当前会话？', { danger: false, okText: '退出' }))) return;
    try {
      await apiFetch('/api/auth/logout', { method: 'POST' });
      toast('已退出', 'ok');
      setTimeout(() => location.reload(), 600);
    } catch (e) { toast(e.message, 'err'); }
  });

  root.querySelector('[data-act="sync"]')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.querySelector('span').textContent = '同步中…';
    try {
      const r = await apiFetch('/api/admin/sync-models', { method: 'POST' });
      toast(`同步完成，${fmtInt(r?.available_count ?? r?.count ?? 0)} 个模型`, 'ok');
      await boot(root);
    } catch (err) {
      toast(err.message, 'err');
      btn.disabled = false;
      btn.querySelector('span').textContent = '立即同步';
    }
  });

  // 搜索框保持焦点与光标位置：重渲染会丢焦点，所以只重画模型卡片区域
  const q = root.querySelector('#mdlQ');
  if (q) {
    q.addEventListener('input', (e) => {
      modelFilter = e.target.value;
      const pos = e.target.selectionStart;
      const box = q.closest('.card').parentElement;
      box.innerHTML = renderModels(state.models, modelFilter);
      refreshIcons();
      bind(root);
      const nq = root.querySelector('#mdlQ');
      if (nq) { nq.focus(); nq.setSelectionRange(pos, pos); }
    });
  }

  root.querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', async () => {
      const ok = await copyText(b.dataset.copy);
      toast(ok ? '已复制 ' + b.dataset.copy : '复制失败', ok ? 'ok' : 'err');
    }));
}

async function boot(root) {
  state.loading = true;
  render(root);
  try {
    const d = await loadAll();
    Object.assign(state, d);
    state.err = '';
  } catch (e) {
    state.err = e.message;
  } finally {
    state.loading = false;
    render(root);
  }
}

export function mount(root) { boot(root); }
export function unmount() { /* 无轮询，无需清理 */ }