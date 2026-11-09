/**
 * 分享历史（独立页面）。
 *
 * 从抽屉改成整页表格的理由：登录之后历史会长到几十上百条，
 * 抽屉那点宽度既放不下列，也不方便对比。表格能一眼扫完状态和有效期。
 *
 * 两种模式：
 *   * OAuth 管理模式 —— 数据来自 `/api/shares/mine`，删除即焚毁；
 *   * 公开 / 仅密码模式 —— 没有账户概念，退回读 localStorage（只读状态）。
 */

import {
  api,
  authSession,
  copyText,
  escapeHtml,
  formatBytes,
  formatCountdown,
  formatTime,
  icon,
  initTheme,
  mountTopbar,
  sharePath,
  toast
} from './app.js';

initTheme();
mountTopbar({ subtitle: '历史', nav: true });

const $ = (selector) => document.querySelector(selector);

const LOCAL_KEY = 'bfs.history';
let managed = false;
let rows = [];

function localRows() {
  try {
    const parsed = JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function stateCell(row) {
  if (row.state === 'burned') return '<span class="tag tag--danger">已焚毁</span>';
  if (row.state === 'expired') return '<span class="tag tag--warn">已过期</span>';
  if (row.state === 'exhausted') return '<span class="tag tag--warn">已读尽</span>';
  if (row.remaining === null) return '<span class="tag tag--accent">不限次数</span>';
  if (!row.readCount) return '<span class="tag tag--ok">待读取</span>';
  return `<span class="tag tag--warn">已读 ${row.readCount} 次</span>`;
}

function render() {
  const body = $('#historyBody');
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="table-empty">还没有分享记录。</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const name = row.fileName || row.label || '文本';
      const kind = [row.label, row.language].filter(Boolean).join(' · ');
      const expire = row.expiresAt
        ? `${formatTime(row.expiresAt)}<span class="cell-sub">${formatCountdown(row.expiresAt)}</span>`
        : '不过期';
      const dead = row.state === 'burned' || row.state === 'expired';
      return `
      <tr data-id="${escapeHtml(row.id)}">
        <td class="col-name"><span class="cell-strong truncate">${escapeHtml(name)}</span>
          <span class="cell-sub mono">${escapeHtml(sharePath(row.id))}</span></td>
        <td class="col-kind">${escapeHtml(kind || '—')}</td>
        <td class="col-size mono">${formatBytes(row.size)}</td>
        <td class="col-state">${stateCell(row)}</td>
        <td class="col-time">${formatTime((row.createdAt || 0) / 1000)}</td>
        <td class="col-expire">${expire}</td>
        <td class="col-ops">
          <button class="btn btn--ghost btn--sm btn--icon" data-copy="${escapeHtml(row.url)}"
                  title="复制链接" ${dead ? 'disabled' : ''}>${icon('copy')}</button>
          <a class="btn btn--ghost btn--sm btn--icon" href="${escapeHtml(row.url)}"
             target="_blank" rel="noopener" title="打开" ${dead ? 'aria-disabled="true" tabindex="-1"' : ''}>${icon('link')}</a>
          ${
            managed
              ? `<button class="btn btn--ghost btn--sm btn--icon btn--danger" data-burn="${escapeHtml(row.id)}"
                       title="焚毁并删除" ${row.state === 'burned' ? 'disabled' : ''}>${icon('fire')}</button>`
              : ''
          }
        </td>
      </tr>`;
    })
    .join('');

  body.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const ok = await copyText(button.dataset.copy);
      toast(ok ? '已复制链接' : '复制失败', ok ? 'ok' : 'err');
    });
  });

  body.querySelectorAll('[data-burn]').forEach((button) => {
    button.addEventListener('click', async () => {
      const id = button.dataset.burn;
      if (!window.confirm('焚毁后内容立即删除且无法恢复，确定吗？')) return;
      button.disabled = true;
      try {
        await api(`/api/shares/${encodeURIComponent(id)}`, { method: 'DELETE' });
        toast('已焚毁', 'ok');
        await load();
      } catch (error) {
        button.disabled = false;
        toast(error.message || '焚毁失败', 'err');
      }
    });
  });
}

async function load() {
  if (!managed) {
    rows = localRows().map((entry) => ({
      id: entry.id,
      label: entry.label,
      language: entry.language,
      fileName: entry.fileName,
      size: entry.size,
      createdAt: entry.createdAt,
      expiresAt: entry.expiresAt,
      maxReads: entry.maxReads,
      readCount: entry.readCount,
      remaining: entry.remaining,
      state: entry.state,
      url: entry.url
    }));
    render();
    return;
  }
  try {
    const data = await api('/api/shares/mine');
    rows = (data.items || []).map((share) => ({
      id: share.id,
      label: share.detected?.kind_label || (share.kind === 'text' ? '文本' : '文件'),
      language: share.detected?.language || '',
      fileName: share.file_name || '',
      size: share.size,
      createdAt: (share.created_at || 0) * 1000,
      expiresAt: share.expires_at,
      maxReads: share.max_reads,
      readCount: share.read_count || 0,
      remaining: share.remaining,
      state: share.state,
      url: share.url || sharePath(share.id)
    }));
    render();
  } catch (error) {
    $('#historyBody').innerHTML = `<tr><td colspan="7" class="table-empty">${
      escapeHtml(error.message || '加载失败')
    }</td></tr>`;
  }
}

async function boot() {
  const info = await authSession();
  managed = Boolean(info.methods?.oauth && info.authenticated);

  if (!managed) {
    $('#gateHint').classList.remove('hidden');
    $('#historyIntro').textContent = '仅显示本机浏览器记录过的链接';
    $('#historyFootNote').textContent = '记录存在本机 localStorage，不包含内容正文。';
  } else {
    $('#historyIntro').textContent = `当前账户：${info.subject || ''}`;
    $('#historyFootNote').textContent = '删除即焚毁，内容会立即从磁盘移除。';
    $('#clearAll').classList.remove('hidden');
  }
  await load();
}

$('#refreshBtn').addEventListener('click', load);

$('#clearAll').addEventListener('click', async () => {
  if (!rows.length) return;
  if (!window.confirm('会焚毁当前账户名下的全部分享，且无法恢复。确定吗？')) return;
  try {
    const result = await api('/api/shares/mine', { method: 'DELETE' });
    toast(`已焚毁 ${result.burned} 条`, 'ok');
    await load();
  } catch (error) {
    toast(error.message || '操作失败', 'err');
  }
});

boot();
