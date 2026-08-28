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

const state = { auth: null, config: null, cache: null, models: null, jobs: null, err: '', loading: true };

async function loadAll() {
  const jobs = {
    auth: () => apiFetch('/api/auth/state'),
    config: () => apiFetch('/api/settings'),
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
  // 后端 /api/auth/state 只回 logged_in / user / default_password /
  // must_change_password / users。原来读的 enabled / auth_enabled / required
  // 三个键**都不存在** —— 徽章恒「未启用鉴权」，还常驻一条假的「没有登录保护」
  // 警告。WebUI 本来就是强制 session 鉴权的，真正该提醒的是默认密码没改。
  if (!a.ok) {
    return `<div class="card"><div class="card-h"><h3>账户安全</h3></div>
      <div class="err-msg" style="color:var(--bad)">${escapeHtml(a.err)}</div></div>`;
  }
  const d = a.d;
  const user = d.user || '';
  const isDefault = !!d.default_password;
  return `<div class="card">
    <div class="card-h"><div><h3>账户安全</h3>
      <p>WebUI 登录凭据</p></div>
      <span class="badge ${isDefault ? 'warn' : 'ok'}">${isDefault ? '仍是默认密码' : '密码已自定义'}</span></div>
    ${isDefault ? `<div class="note warn-note"><i data-lucide="shield-alert"></i>
      <span>当前还在用默认密码，服务端已锁住除登录/改密以外的所有接口。
      改完密码后功能才会解锁 —— 这道锁是为了避免有人把带默认密码的面板直接暴露到公网。</span></div>` : ''}
    <div class="kv-grid">
      ${user ? `<div class="kv"><span>当前登录</span><b class="mono">${escapeHtml(user)}</b></div>` : ''}
      <div class="kv"><span>管理员账号数</span><b>${fmtInt(d.users)}</b></div>
    </div>
    <div class="acts" style="display:flex;gap:8px;margin-top:14px">
      <button class="btn ${isDefault ? 'primary' : ''}" data-act="passwd">
        <i data-lucide="key-round"></i><span>${isDefault ? '立即修改密码' : '修改密码'}</span></button>
      <button class="btn ghost" data-act="logout">
        <i data-lucide="log-out"></i><span>退出登录</span></button>
    </div>
  </div>`;
}

function openPasswordModal() {
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
        // 两套键都发。这里原来只发 old_password/new_password，而有的部署后端
        // 只读 old/new —— 后端读到空串，报的却是「新密码至少 6 位」，看着像
        // 校验规则不对，完全指不到键名上（改密码功能因此一直是坏的）。
        // 多余的键会被忽略，所以同时发能兼容两边。
        const r = await apiFetch('/api/auth/password', {
          method: 'POST',
          body: { old: old, new: a, old_password: old, new_password: a },
        });
        close();
        // 改密成功后后端会作废该用户的所有 session。所以不能回头重载设置页
        // （那会让每个 /api/* 都 401，页面变成一片错误卡片），直接去登录页。
        toast('密码已修改，请用新密码登录', 'ok');
        const to = (r && typeof r.redirect === 'string' && r.redirect.startsWith('/'))
          ? r.redirect : '/login';
        setTimeout(() => location.replace(to), 600);
      } catch (err) {
        showErr(err.message);
        btn.disabled = false;
      }
    });
  });
}

/* ------------------------------------------------------------ 运行时配置 */

/* 每项配置的中文说明。key 与后端 SPEC 一一对应（spec_view() 给出全集），
   这里只补人类可读的标题/提示 —— 类型、范围、可选值一律用后端给的，
   前端不再抄一份 schema（抄了就会在后端改动后静默错配）。 */
const CFG_META = {
  uoomsg_token: {
    title: '接码平台 Token',
    hint: '自动注册要用它取手机号收验证码。没有就只能手动注册（自己的手机号）。',
    group: '接码平台',
  },
  proxy_mode:  { title: '代理模式', hint: 'off 直连；fixed 固定一个；rotate 在出口表里轮换。', group: '网络出口' },
  proxy_host:  { title: '出口主机', hint: '代理监听在哪台机器上，通常是本机。', group: '网络出口' },
  proxy_url:   { title: '固定代理地址', hint: '仅 fixed 模式用，形如 http://127.0.0.1:8080。', group: '网络出口' },
  proxy_exits: { title: '出口表', hint: '轮换用的端口清单，在「代理池」页增删。', group: '网络出口' },
  checkin_cron: { title: '签到时间', hint: '五段 cron。上游常在 00:03~00:21 就把奖励发成裂变包，签到只会拿到「今天已签到」。', group: '定时任务' },
  balance_interval_min: { title: '余额刷新间隔', hint: '单位分钟。太长会让面板显示陈旧余额，调度器照发请求必吃额度用尽。', group: '定时任务' },
  timezone: { title: '时区', hint: '「今天」按这个时区算 —— 签到判重、今日到账都依赖它。改完要重启才彻底生效。', group: '定时任务' },
  verify_below_credits: { title: '实时校验阈值', hint: '余额低于此值的账号在发请求前补查一次真实余额。设太高会给每个请求加一次往返。', group: '账号调度' },
  verify_stale_sec: { title: '余额数据保鲜期', hint: '单位秒。这么久内查过的就不重复查。', group: '账号调度' },
  auth_fail_limit: { title: '鉴权失败容忍次数', hint: '连续失败这么多次才判定账号失效。', group: '账号调度' },
  expiring_soon_h: { title: '即将到期阈值', hint: '单位小时。套餐剩余时间少于此值的账号会被优先轮换掉。', group: '账号调度' },
};

const CFG_GROUPS = ['接码平台', '网络出口', '定时任务', '账号调度'];

/* 来源徽章：让人一眼看出这个值是代码默认、环境变量给的、还是在面板上改过的。
   开源项目里这点很重要 —— 「我明明在 .env 里写了怎么没生效」多半是被
   runtime 覆盖了，不标出来根本查不出。 */
const SRC_LABEL = { default: ['idle', '默认值'], env: ['acc', '环境变量'], runtime: ['ok', '面板设置'] };

function cfgRow(spec, values) {
  const key = spec.key;
  const meta = CFG_META[key] || { title: key, hint: '' };
  const src = values[key + '__source'] || 'default';
  const [srcCls, srcTxt] = SRC_LABEL[src] || SRC_LABEL.default;
  const v = values[key];

  let control;
  if (spec.type === 'exits') {
    // 出口表有专门的增删 UI 在代理池页，这里只报数量并指路，不做第二套编辑器
    const n = Array.isArray(v) ? v.length : 0;
    control = `<div class="cfg-static">${n ? fmtInt(n) + ' 个出口' : '未配置'}
      <span class="dim">· 在「代理池」页增删</span></div>`;
  } else if (spec.secret) {
    // 密钥永远不回显明文，只说「配没配」。留空 = 不修改，这样用户改别的项时
    // 不会因为密码框是空的就把已存的 token 抹掉。
    const set = v && v.set;
    control = `<input class="inp" data-cfg="${escapeHtml(key)}" type="password"
        autocomplete="off" placeholder="${set ? '已配置 ' + escapeHtml(v.hint || '') + '，留空则不改' : '尚未配置'}" />
      ${set ? `<button class="btn ghost sm" data-cfgclear="${escapeHtml(key)}"
        title="清空这个密钥"><i data-lucide="eraser"></i></button>` : ''}`;
  } else if (spec.choices) {
    control = `<select data-cfg="${escapeHtml(key)}">${spec.choices.map((c) =>
      `<option value="${escapeHtml(c)}"${String(v) === String(c) ? ' selected' : ''}>${escapeHtml(c)}</option>`
    ).join('')}</select>`;
  } else if (spec.type === 'int') {
    control = `<input class="inp" data-cfg="${escapeHtml(key)}" type="number"
      value="${escapeHtml(String(v ?? ''))}"
      ${spec.min != null ? `min="${spec.min}"` : ''} ${spec.max != null ? `max="${spec.max}"` : ''} />`;
  } else {
    control = `<input class="inp" data-cfg="${escapeHtml(key)}" type="text"
      value="${escapeHtml(String(v ?? ''))}" />`;
  }

  return `<div class="cfg-row">
    <div class="cfg-lab">
      <b>${escapeHtml(meta.title)}</b>
      <span class="badge nodot ${srcCls}">${srcTxt}</span>
      ${spec.env ? `<code class="dim">${escapeHtml(spec.env)}</code>` : ''}
      ${meta.hint ? `<p class="cfg-hint">${escapeHtml(meta.hint)}</p>` : ''}
    </div>
    <div class="cfg-ctl">${control}</div>
  </div>`;
}

function renderConfig(c) {
  if (!c.ok) {
    return `<div class="card"><div class="card-h"><h3>运行时配置</h3></div>
      <div class="err-msg" style="color:var(--bad)">${escapeHtml(c.err)}</div></div>`;
  }
  const specs = c.d.spec || [];
  const values = c.d.settings || {};
  const byGroup = new Map();
  for (const s of specs) {
    const g = (CFG_META[s.key] || {}).group || '其它';
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push(s);
  }
  // 已知分组按既定顺序排，未登记的分组兜在后面（后端加了新 key 也不会消失）
  const order = [...CFG_GROUPS.filter((g) => byGroup.has(g)),
    ...[...byGroup.keys()].filter((g) => !CFG_GROUPS.includes(g))];
  const changed = specs.filter((s) => (values[s.key + '__source'] || 'default') === 'runtime').length;

  return `<div class="card">
    <div class="card-h">
      <div><h3>运行时配置</h3>
        <p>改完即时生效，不用重启。优先级：面板设置 &gt; 环境变量 &gt; 代码默认值</p></div>
      <div class="acts">
        ${changed ? `<span class="badge ok">${fmtInt(changed)} 项已自定义</span>` : ''}
        <button class="btn ghost sm" data-act="cfgReset"><i data-lucide="rotate-ccw"></i><span>全部恢复默认</span></button>
        <button class="btn primary sm" data-act="cfgSave"><i data-lucide="save"></i><span>保存</span></button>
      </div>
    </div>
    ${order.map((g) => `<div class="cfg-group">
      <div class="cfg-gt">${escapeHtml(g)}</div>
      ${byGroup.get(g).map((s) => cfgRow(s, values)).join('')}
    </div>`).join('')}
    <div class="note" id="cfgMsg" hidden><i data-lucide="info"></i><span></span></div>
  </div>`;
}

/** 收集表单里被改动的项。密钥留空表示「不改」，必须排除，否则会把已存的 token 抹掉。 */
function collectConfig(root) {
  const out = {};
  root.querySelectorAll('[data-cfg]').forEach((inp) => {
    const key = inp.dataset.cfg;
    const spec = (state.config?.d?.spec || []).find((s) => s.key === key);
    if (!spec) return;
    const raw = inp.value;
    if (spec.secret) {
      if (raw.trim()) out[key] = raw.trim();     // 只有真填了才提交
      return;
    }
    const cur = state.config.d.settings[key];
    if (spec.type === 'int') {
      if (raw.trim() === '') return;
      const n = Number(raw);
      if (Number.isFinite(n) && n !== Number(cur)) out[key] = n;
    } else if (raw !== String(cur ?? '')) {
      out[key] = raw;
    }
  });
  return out;
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
    <div style="margin-top:14px">${renderConfig(state.config)}</div>
    <div style="margin-top:14px">${renderModels(state.models, modelFilter)}</div>
    <div style="margin-top:14px">${renderJobs(state.jobs)}</div>`;
  refreshIcons();
  bind(root);
}

function bind(root) {
  root.querySelector('[data-act="reload"]')?.addEventListener('click', () => boot(root));
  // 改密成功后会跳登录页（session 已被后端作废），不需要回调重载本页
  root.querySelector('[data-act="passwd"]')?.addEventListener('click', () =>
    openPasswordModal());

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

  root.querySelector('[data-act="cfgSave"]')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const upd = collectConfig(root);
    if (!Object.keys(upd).length) { toast('没有改动', 'ok'); return; }
    btn.disabled = true;
    try {
      const r = await apiFetch('/api/settings', { method: 'POST', body: upd });
      toast(`已保存 ${r.changed.length} 项，立即生效`, 'ok');
      await boot(root);
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
      btn.disabled = false;
    }
  });

  root.querySelector('[data-act="cfgReset"]')?.addEventListener('click', async () => {
    const ok = await confirmDialog('恢复默认配置',
      '会清掉所有在面板上改过的配置，回落到环境变量或代码默认值。'
      + '接码 token 也会被清空，需要重新填。', { okText: '全部恢复' });
    if (!ok) return;
    try {
      await apiFetch('/api/settings/reset', { method: 'POST', body: {} });
      toast('已恢复默认', 'ok');
      await boot(root);
    } catch (e) { toast('恢复失败：' + e.message, 'err'); }
  });

  root.querySelectorAll('[data-cfgclear]').forEach((b) =>
    b.addEventListener('click', async () => {
      const key = b.dataset.cfgclear;
      if (!(await confirmDialog('清空密钥', `确定清掉 ${key}？相关功能会立刻停用。`,
        { okText: '清空' }))) return;
      try {
        await apiFetch('/api/settings/reset', { method: 'POST', body: { keys: [key] } });
        toast('已清空', 'ok');
        await boot(root);
      } catch (e) { toast('清空失败：' + e.message, 'err'); }
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