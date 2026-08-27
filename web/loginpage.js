/* ============================================================
 * loginpage.js —— 独立 /login 页的登录 + 强制改密流程
 *
 * 和旧的 login.js 的区别：
 *   login.js     是「在主界面上盖一层 overlay」的登录门，URL 停在 /
 *   loginpage.js 是 /login 这个真实页面自己的控制器，登录成功后跳 /
 *
 * 为什么要拆开：登录需要一个可收藏、可被反代单独放行、退出后地址栏
 * 如实反映状态的 URL。overlay 方案做不到这三点。
 *
 * 会话仍由后端 HttpOnly cookie 管理，这里不接触也不存储任何凭证。
 * ============================================================ */

import { el, ASSET, apiFetch, toast, refreshIcons } from '@/shared.js';

const AUTH_STATE = '/api/auth/state';
const AUTH_LOGIN = '/api/auth/login';
const AUTH_PASSWORD = '/api/auth/password';

/** 登录成功后要去哪：支持 /login?next=/xxx，但只允许站内相对路径 */
function nextTarget() {
  const raw = new URLSearchParams(location.search).get('next') || '/';
  // 只接受以单个 / 开头的路径。'//evil.com' 会被浏览器当协议相对 URL
  // 跳到外站，是个开放重定向漏洞，必须挡掉。
  return /^\/(?!\/)/.test(raw) ? raw : '/';
}

export async function mountLoginPage(root) {
  let state = null;
  try {
    state = await apiFetch(AUTH_STATE);
  } catch (err) {
    renderFatal(root, err, () => mountLoginPage(root));
    return;
  }

  // 已登录 + 还在用默认密码 → 直接进改密表单，这是「强制改密」的落点。
  // 后端那道中间件此时会 403 掉其它接口，所以不能放人进主界面。
  if (state && state.logged_in === true) {
    if (state.must_change_password) {
      renderChangePassword(root, state.user || 'admin');
    } else {
      location.replace(nextTarget());
    }
    return;
  }

  renderLoginForm(root, state);
}

/* ------------------------------------------------------------------ 登录表单 */

function renderLoginForm(root, state) {
  const userInp = el('input', {
    class: 'inp', id: 'login-user', name: 'username', type: 'text',
    autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false',
    required: '', placeholder: '请输入用户名',
  });
  const passInp = el('input', {
    class: 'inp', id: 'login-pass', name: 'password', type: 'password',
    autocomplete: 'current-password', required: '', placeholder: '请输入密码',
  });

  const errText = el('span', { class: 'login-error-text' });
  const errBox = el('div', { class: 'login-error', role: 'alert', hidden: '' }, [
    el('i', { 'data-lucide': 'alert-circle' }), errText,
  ]);
  const submitBtn = el('button', { class: 'btn primary login-submit', type: 'submit' }, ['登录']);

  let busy = false;
  const showErr = (msg) => { errText.textContent = msg; errBox.hidden = false; };
  const setBusy = (on) => {
    busy = on;
    submitBtn.disabled = on;
    userInp.disabled = on;
    passInp.disabled = on;
    submitBtn.textContent = on ? '登录中…' : '登录';
  };

  const form = el('form', { class: 'login-form' }, [
    el('label', { class: 'field', for: 'login-user' }, [el('span', { text: '用户名' }), userInp]),
    el('label', { class: 'field', for: 'login-pass' }, [el('span', { text: '密码' }), passInp]),
    submitBtn,
    errBox,
  ]);

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (busy) return;
    const user = userInp.value.trim();
    const password = passInp.value;
    if (!user || !password) {
      showErr('请输入用户名和密码');
      (user ? passInp : userInp).focus();
      return;
    }
    errBox.hidden = true;
    setBusy(true);
    try {
      const res = await apiFetch(AUTH_LOGIN, { method: 'POST', body: { user, password } });
      // 仍是默认密码 → 不放进主界面，就地转成改密表单
      if (res && res.default_password) {
        toast('首次登录，请先修改默认密码', 'bad');
        renderChangePassword(root, (res && res.user) || user);
        return;
      }
      location.replace(nextTarget());
    } catch (err) {
      showErr(err && err.message ? err.message : '登录失败，请稍后再试');
      passInp.value = '';
      setBusy(false);
      passInp.focus();
    }
  });

  root.replaceChildren(card([
    brand(),
    el('p', { class: 'login-sub dim', text: '账号池网关 · 管理后台' }),
    form,
    hintFirstRun(state),
  ]));
  refreshIcons();
  userInp.focus();
}

/** 首次部署时给一行提示，省得用户翻文档找默认账号 */
function hintFirstRun(state) {
  if (!state || state.default_password !== true) return null;
  return el('p', {
    class: 'login-sub dim',
    style: 'margin-top:4px;font-size:12px',
    text: '首次部署的默认账号是 admin / admin，登录后必须修改密码。',
  });
}

/* --------------------------------------------------------------- 强制改密表单 */

function renderChangePassword(root, user) {
  const oldInp = el('input', {
    class: 'inp', id: 'pw-old', type: 'password',
    autocomplete: 'current-password', required: '', placeholder: '当前密码',
  });
  const newInp = el('input', {
    class: 'inp', id: 'pw-new', type: 'password',
    autocomplete: 'new-password', required: '', placeholder: '新密码（至少 6 位）',
  });
  const new2Inp = el('input', {
    class: 'inp', id: 'pw-new2', type: 'password',
    autocomplete: 'new-password', required: '', placeholder: '再次输入新密码',
  });

  const errText = el('span', { class: 'login-error-text' });
  const errBox = el('div', { class: 'login-error', role: 'alert', hidden: '' }, [
    el('i', { 'data-lucide': 'alert-circle' }), errText,
  ]);
  const submitBtn = el('button', { class: 'btn primary login-submit', type: 'submit' }, ['修改并继续']);

  let busy = false;
  const showErr = (msg) => { errText.textContent = msg; errBox.hidden = false; };
  const setBusy = (on) => {
    busy = on;
    [submitBtn, oldInp, newInp, new2Inp].forEach((x) => { x.disabled = on; });
    submitBtn.textContent = on ? '提交中…' : '修改并继续';
  };

  const form = el('form', { class: 'login-form' }, [
    el('label', { class: 'field', for: 'pw-old' }, [el('span', { text: '当前密码' }), oldInp]),
    el('label', { class: 'field', for: 'pw-new' }, [el('span', { text: '新密码' }), newInp]),
    el('label', { class: 'field', for: 'pw-new2' }, [el('span', { text: '确认新密码' }), new2Inp]),
    submitBtn,
    errBox,
  ]);

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (busy) return;
    const oldPw = oldInp.value;
    const newPw = newInp.value;
    if (!oldPw || !newPw) { showErr('请填写当前密码和新密码'); return; }
    if (newPw.length < 6) { showErr('新密码至少 6 位'); newInp.focus(); return; }
    if (newPw !== new2Inp.value) {
      showErr('两次输入的新密码不一致');
      new2Inp.value = '';
      new2Inp.focus();
      return;
    }
    if (newPw === oldPw) { showErr('新密码不能和当前密码相同'); newInp.focus(); return; }

    errBox.hidden = true;
    setBusy(true);
    try {
      // 两套键都发。老后端（比如还没升级的部署）只读 old/new，新后端两套都收，
      // 多余的键会被忽略 —— 所以同时发能兼容两边。
      //
      // 这不是洁癖：实测过一台只认 old/new 的部署，前端发 old_password/new_password
      // 时后端读到空字符串，报的却是「新密码至少 6 位」，看着像是校验规则不对，
      // 完全指不到键名上。同时发就没这个歧义了。
      await apiFetch(AUTH_PASSWORD, {
        method: 'POST',
        body: {
          old: oldPw, new: newPw,
          old_password: oldPw, new_password: newPw,
        },
      });
      // 改密会踢掉所有 session，所以必须重新登录 —— 回到登录表单而不是跳主界面
      toast('密码已修改，请用新密码登录');
      root.replaceChildren();
      await mountLoginPage(root);
    } catch (err) {
      showErr(err && err.message ? err.message : '修改失败，请稍后再试');
      setBusy(false);
      oldInp.focus();
    }
  });

  root.replaceChildren(card([
    brand(),
    el('div', { class: 'login-error', role: 'alert' }, [
      el('i', { 'data-lucide': 'shield-alert' }),
      el('span', { text: `账号 ${user} 仍在使用默认密码，修改后才能使用其他功能。` }),
    ]),
    form,
  ]));
  refreshIcons();
  oldInp.focus();
}

/* --------------------------------------------------------- 连不上后端的错误态 */

function renderFatal(root, err, retry) {
  const retryBtn = el('button', { class: 'btn primary login-submit', type: 'button' }, [
    el('i', { 'data-lucide': 'refresh-cw' }), el('span', { text: '重试' }),
  ]);
  retryBtn.addEventListener('click', async () => {
    retryBtn.disabled = true;
    retryBtn.textContent = '连接中…';
    await retry();
  });

  root.replaceChildren(card([
    brand(false),
    el('div', { class: 'login-fatal' }, [
      el('i', { 'data-lucide': 'server-off' }),
      el('p', { class: 'login-fatal-title', text: '无法连接后端服务' }),
      el('p', {
        class: 'login-fatal-detail dim',
        text: err && err.message ? err.message : '网络异常，请检查服务后重试',
      }),
      retryBtn,
    ]),
  ]));
  refreshIcons();
  retryBtn.focus();
}

/* ------------------------------------------------------------------ 小构件 */

function brand(withTitle = true) {
  const kids = [
    el('img', { src: ASSET + 'icon-192.png', alt: 'wb-pool 图标', width: '52', height: '52' }),
  ];
  if (withTitle) {
    kids.push(el('h1', { class: 'login-title mono', id: 'login-title', text: 'wb-pool' }));
  }
  return el('div', { class: 'login-brand' }, kids);
}

function card(children) {
  return el('div', {
    class: 'login-card',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': 'login-title',
  }, children.filter(Boolean));
}
