/**
 * 登录页。
 *
 * 一次性问清楚 /api/auth/session：后端配了哪些方式，就显示哪些控件。
 * 没配密码就藏掉密码框，没配 GitHub 就藏掉按钮 —— 页面本身不猜配置。
 *
 * 登录成功后回首页；如果带着 ?next=（比如直接点了个受保护的地址被弹回来），
 * 就回那个地址。`next` 只接受站内绝对路径，防止被当成开放重定向。
 */

import {
  api,
  icon,
  initTheme,
  mountTopbar,
  resolveUrl,
  sitePath
} from './app.js';

initTheme();
mountTopbar({ subtitle: '登录', home: false, minimal: false });

const $ = (selector) => document.querySelector(selector);

const MESSAGES = {
  denied: '你取消了 GitHub 授权',
  state: '登录已过期，请重新试一次',
  oauth: '和 GitHub 通信失败，请稍后再试',
  not_allowed: '这个 GitHub 账号没有权限'
};

function nextHref() {
  const raw = new URLSearchParams(location.search).get('next') || '';
  // 只认站内路径：`//evil.com` 会被浏览器当成协议相对地址
  if (!raw.startsWith('/') || raw.startsWith('//')) return sitePath('/');
  return raw;
}

function showError(message) {
  const box = $('#loginError');
  box.textContent = message;
  box.classList.remove('hidden');
}

async function boot() {
  document.querySelectorAll('[data-icon]').forEach((node) => {
    node.innerHTML = icon(node.dataset.icon);
  });
  $('#authIcon').innerHTML = icon('lock');
  $('#githubBtn').querySelector('.icon-wrap').innerHTML = icon('github');

  const params = new URLSearchParams(location.search);
  const error = params.get('error');
  if (error) showError(MESSAGES[error] || '登录失败，请重试');

  let info = { methods: { password: true, github: false } };
  try {
    info = await api('/api/auth/session', { noRedirect: true });
  } catch {
    /* 会话接口挂了也不挡住密码登录：服务端仍会校验 */
  }

  const { password, github } = info.methods || {};
  if (!password && !github) {
    showError('服务端未配置登录方式，请联系管理员');
    $('#submitBtn').disabled = true;
    return;
  }

  if (password) {
    $('#passwordField').classList.remove('hidden');
    // 配了用户名才显示用户名字段：多数部署只有一个密码
    if (info.username_required) {
      $('#userField').classList.remove('hidden');
    }
  } else {
    $('#passwordField').classList.add('hidden');
    $('#submitBtn').classList.add('hidden');
  }

  if (github) {
    $('#githubBtn').classList.remove('hidden');
    $('#githubBtn').href = resolveUrl('/api/auth/github/start');
    if (password) $('#divider').classList.remove('hidden');
  }

  if (password) $('#password').focus();
}

$('#loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#submitBtn');
  button.disabled = true;
  $('#loginError').classList.add('hidden');
  try {
    await api('/api/auth/login', {
      method: 'POST',
      noRedirect: true,
      body: JSON.stringify({
        username: $('#username').value || '',
        password: $('#password').value || ''
      })
    });
    location.replace(nextHref());
  } catch (err) {
    showError(err.message || '登录失败');
    button.disabled = false;
    $('#password').select();
  }
});

boot();
