// login.js — 登录门：应用启动路由之前先确认 wb_session 登录态。
// 会话由后端 HttpOnly cookie 管理，前端不接触、不存储任何凭证或 token。

import { el, ASSET, apiFetch, toast, refreshIcons } from '@/shared.js';

const AUTH_STATE = '/api/auth/state';
const AUTH_LOGIN = '/api/auth/login';

/**
 * 启动前置检查：
 *  - 已登录：直接 resolve(true)，不渲染任何节点；
 *  - 未登录：渲染登录页并接管视口，直到登录成功才 resolve(true)；
 *  - 状态接口本身失败：渲染可重试的错误态，不放行、也不抛未捕获异常。
 */
export async function requireLogin() {
  let state = null;
  let stateErr = null;
  try {
    state = await apiFetch(AUTH_STATE);
  } catch (err) {
    stateErr = err;
  }
  if (state && state.logged_in === true) return true;

  return new Promise((resolve) => {
    document.body.classList.add('login-mode');
    const overlay = el('div', { class: 'login-overlay' });
    document.body.appendChild(overlay);

    // 登录成功：拆除登录层、还主界面，然后放行
    const finish = () => {
      overlay.remove();
      document.body.classList.remove('login-mode');
      resolve(true);
    };

    // 错误态点「重试」时复用：重新拉登录态，再决定进表单还是直接放行
    const boot = async () => {
      let s = null;
      try {
        s = await apiFetch(AUTH_STATE);
      } catch (err) {
        renderFatal(overlay, err, boot);
        return;
      }
      if (s && s.logged_in === true) {
        finish();
        return;
      }
      renderForm(overlay, finish);
    };

    if (stateErr) renderFatal(overlay, stateErr, boot);
    else renderForm(overlay, finish);
  });
}

/* 渲染登录表单卡片 */
function renderForm(overlay, finish) {
  const userInp = el('input', {
    class: 'inp',
    id: 'login-user',
    name: 'username',
    type: 'text',
    autocomplete: 'username',
    autocapitalize: 'none',
    spellcheck: 'false',
    required: '',
    placeholder: '请输入用户名',
  });
  const passInp = el('input', {
    class: 'inp',
    id: 'login-pass',
    name: 'password',
    type: 'password',
    autocomplete: 'current-password',
    required: '',
    placeholder: '请输入密码',
  });

  const errText = el('span', { class: 'login-error-text' });
  const errBox = el('div', { class: 'login-error', role: 'alert', hidden: '' }, [
    el('i', { 'data-lucide': 'alert-circle' }),
    errText,
  ]);

  const submitBtn = el('button', { class: 'btn primary login-submit', type: 'submit' }, ['登录']);

  let busy = false;
  const showErr = (msg) => {
    errText.textContent = msg;
    errBox.hidden = false;
  };
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
    if (busy) return; // 防重复提交

    const user = userInp.value.trim();
    const password = passInp.value;
    if (!user || !password) {
      // required 之外的兜底：空表单不发请求
      showErr('请输入用户名和密码');
      (user ? passInp : userInp).focus();
      return;
    }

    errBox.hidden = true;
    setBusy(true);
    try {
      const res = await apiFetch(AUTH_LOGIN, {
        method: 'POST',
        body: { user, password },
      });
      const name = (res && res.user) || user;
      const needReset = !!(res && res.default_password);
      finish(); // 先拆除登录层，避免遮挡 toast
      toast('欢迎回来，' + name);
      if (needReset) toast('你还在用默认密码，建议去设置页修改', 'bad');
    } catch (err) {
      // 后端 401 时 err.message 即「用户名或密码不对」
      showErr(err && err.message ? err.message : '登录失败，请稍后再试');
      passInp.value = '';
      setBusy(false);
      passInp.focus();
    }
  });

  const card = el('div', {
    class: 'login-card',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': 'login-title',
  }, [
    el('div', { class: 'login-brand' }, [
      el('img', { src: ASSET + 'icon-192.png', alt: 'wb-pool 图标', width: '52', height: '52' }),
      el('h1', { class: 'login-title mono', id: 'login-title', text: 'wb-pool' }),
      el('p', { class: 'login-sub dim', text: '账号池网关 · 管理后台' }),
    ]),
    form,
  ]);

  overlay.replaceChildren(card);
  refreshIcons();
  userInp.focus();
}

/* 渲染「连不上后端」的可重试错误态 */
function renderFatal(overlay, err, retry) {
  const retryBtn = el('button', { class: 'btn primary login-submit', type: 'button' }, [
    el('i', { 'data-lucide': 'refresh-cw' }),
    el('span', { text: '重试' }),
  ]);
  retryBtn.addEventListener('click', async () => {
    retryBtn.disabled = true;
    retryBtn.textContent = '连接中…';
    await retry(); // retry 内部会重渲染 overlay（再次失败 / 进表单 / 直接放行）
  });

  const card = el('div', {
    class: 'login-card',
    role: 'alertdialog',
    'aria-labelledby': 'login-fatal-title',
  }, [
    el('div', { class: 'login-brand' }, [
      el('img', { src: ASSET + 'icon-192.png', alt: 'wb-pool 图标', width: '52', height: '52' }),
    ]),
    el('div', { class: 'login-fatal' }, [
      el('i', { 'data-lucide': 'server-off' }),
      el('p', { class: 'login-fatal-title', id: 'login-fatal-title', text: '无法连接后端服务' }),
      el('p', {
        class: 'login-fatal-detail dim',
        text: err && err.message ? err.message : '网络异常，请检查服务后重试',
      }),
      retryBtn,
    ]),
  ]);

  overlay.replaceChildren(card);
  refreshIcons();
  retryBtn.focus();
}