/**
 * 设置页。
 *
 * 目前只有两块：账户信息 + CI Token 管理。
 * 单独成页（而不是塞进历史抽屉）的理由：token 是「配置」，历史是「记录」，
 * 两者生命周期完全不同，混在一起既难找也不好继续加东西。
 */

import {
  api,
  authSession,
  copyText,
  escapeHtml,
  formatTime,
  icon,
  initTheme,
  logout,
  mountTopbar,
  toast
} from './app.js';

initTheme();
mountTopbar({ subtitle: '设置', nav: true });

const $ = (selector) => document.querySelector(selector);

const METHOD_LABELS = { github: 'GitHub', gitea: 'Gitea', password: '密码' };

function renderTokens(items, max) {
  const body = $('#tokenBody');
  $('#tokenQuota').textContent = max ? `${items.length} / ${max}` : '';
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="4" class="table-empty">还没有 token。</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (item) => `
      <tr>
        <td class="cell-strong">${escapeHtml(item.label || '(无备注)')}</td>
        <td class="muted-2">${formatTime(item.created_at)}</td>
        <td class="muted-2">${item.last_used_at ? formatTime(item.last_used_at) : '未使用'}</td>
        <td class="col-ops">
          <button class="btn btn--ghost btn--sm btn--danger" data-revoke="${escapeHtml(
            item.label || ''
          )}" type="button">撤销</button>
        </td>
      </tr>`
    )
    .join('');

  body.querySelectorAll('[data-revoke]').forEach((button) => {
    button.addEventListener('click', async () => {
      const label = button.dataset.revoke;
      const hint = label ? `「${label}」` : '这个 token';
      if (!window.confirm(`撤销${hint}？使用它的流水线会立即失败。`)) return;
      button.disabled = true;
      try {
        await api(`/api/tokens?label=${encodeURIComponent(label)}`, { method: 'DELETE' });
        toast('已撤销', 'ok');
        await loadTokens();
      } catch (error) {
        button.disabled = false;
        toast(error.message || '撤销失败', 'err');
      }
    });
  });
}

async function loadTokens() {
  try {
    const data = await api('/api/tokens');
    renderTokens(data.items || [], data.max);
  } catch (error) {
    $('#tokenBody').innerHTML = `<tr><td colspan="4" class="table-empty">${escapeHtml(
      error.message || '加载失败'
    )}</td></tr>`;
  }
}

function hideTokenOutput() {
  $('#tokenValue').value = '';
  $('#tokenNew').classList.add('hidden');
}

async function boot() {
  document.querySelectorAll('[data-icon]').forEach((node) => {
    node.innerHTML = icon(node.dataset.icon);
  });

  const info = await authSession();
  const methods = info.methods || {};
  const oauth = Boolean(methods.oauth && info.authenticated);

  $('#accountName').textContent = info.subject || (info.authenticated ? '匿名' : '未登录');
  $('#accountMethods').textContent =
    Object.keys(METHOD_LABELS)
      .filter((key) => methods[key])
      .map((key) => METHOD_LABELS[key])
      .join(' / ') || '未配置';
  $('#accountBase').textContent = location.origin + (window.__BFS_BASE__ || '');

  if (!oauth) {
    // 没有 OAuth 就没有「账户」，也就没有 token 可言
    $('#gateHint').classList.remove('hidden');
    $('#tokenCard').classList.add('hidden');
    $('#logoutBtn').classList.add('hidden');
    return;
  }

  $('#settingsIntro').textContent = '账户与 CI 集成';
  $('#tokenCard').classList.remove('hidden');
  await loadTokens();
}

$('#tokenCreate').addEventListener('click', async () => {
  const button = $('#tokenCreate');
  button.disabled = true;
  try {
    const created = await api('/api/tokens', {
      method: 'POST',
      body: JSON.stringify({ label: $('#tokenLabel').value.trim() })
    });
    $('#tokenValue').value = created.token;
    $('#tokenNew').classList.remove('hidden');
    $('#tokenLabel').value = '';
    await loadTokens();
    // 明文只此一次，顺手复制到剪贴板省一步
    await copyText(created.token);
  } catch (error) {
    toast(error.message || '创建失败', 'err');
  } finally {
    button.disabled = false;
  }
});

$('#tokenCopy').addEventListener('click', async () => {
  const ok = await copyText($('#tokenValue').value);
  toast(ok ? '已复制' : '复制失败，请手动选择', ok ? 'ok' : 'err');
});

$('#tokenNew').addEventListener('click', (event) => {
  // 点「复制」不算关闭
  if (event.target.closest('#tokenCopy')) return;
  hideTokenOutput();
});

$('#logoutBtn').addEventListener('click', () => logout());

boot();
