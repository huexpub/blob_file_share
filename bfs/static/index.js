/**
 * 发送端。
 *
 * 三块：
 *   1. 粘贴面（Ctrl+V 识别 + 预览）
 *   2. 「生成链接」一个按钮 -> 参数弹窗 -> 生成 -> 自动复制 -> 结果弹窗
 *   3. 左侧历史：本机发过的分享，带「是否已被读取」状态
 *
 * 历史为什么存 localStorage 而不是纯内存：
 *   它是发送端的账本，关掉页面就没了等于没有。列表里没有正文，只有 id / 形态 /
 *   时间 / 次数；正文在服务端，读一次就没了。代价是换台机器看不到历史 ——
 *   但这本来就是本机视角。
 *
 * manage_token 与历史一起存：否则刷新之后就再也没法焚毁自己刚发的东西。
 */

import {
  api,
  authSession,
  BOOT,
  copyText,
  escapeHtml,
  formatBytes,
  formatCountdown,
  formatTime,
  icon,
  initTheme,
  isHexLike,
  mountTopbar,
  redirectToLogin,
  toast,
  uploadWithProgress
} from './app.js';

initTheme();

/*
 * 顶栏分两种形态，取决于是不是「OAuth 管理模式」：
 *
 *   OAuth 模式    登录用户有账户，历史入库、可管 CI token
 *                 -> 顶栏给「历史 / 设置」两个独立页面入口
 *   其它模式      公开 或 仅管理员密码，没有账户这回事
 *                 -> 保持原样：历史用右侧抽屉，不显示设置
 *
 * 所以这里不能先 mountTopbar 再判断，必须先问清楚再渲染。
 */
async function bootTopbar() {
  if (!BOOT.gate) {
    mountTopbar({ subtitle: '快传', history: true });
    return;
  }
  const info = await authSession();
  if (!info.authenticated) {
    redirectToLogin();
    return;
  }
  managedHistory = Boolean(info.methods?.oauth);
  if (managedHistory) {
    mountTopbar({ subtitle: '快传', nav: true });
  } else {
    mountTopbar({ subtitle: '快传', history: true });
  }
}


const $ = (selector) => document.querySelector(selector);

const HISTORY_KEY = 'bfs.history';
const OPTIONS_KEY = 'bfs.sendOptions';
const HISTORY_LIMIT = 40;
let managedHistory = false;
let historyOwner = '';

let draft = '';
let files = [];
let forcedKind = '';
let inspectSeq = 0;
let inspectTimer = 0;
/** 当前结果弹窗对应的那一批分享 */
let currentBatch = [];
let history = loadHistory();

// 顶栏形态取决于是否 OAuth 管理模式，所以放在变量初始化之后再问
bootTopbar();

/* ---------------------------------------------------------------- 图标 */

function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((node) => {
    node.innerHTML = icon(node.dataset.icon);
    node.classList.add('icon-wrap');
  });
}

hydrateIcons();

/* ---------------------------------------------------------------- 历史存取 */

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item) => item && item.id) : [];
  } catch {
    return [];
  }
}

function saveHistory() {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, HISTORY_LIMIT)));
  } catch {
    /* 隐私模式：本次会话内仍可用 */
  }
}

function remember(share) {
  if (managedHistory) {
    refreshManagedHistory();
    return;
  }
  const found = share.detected || {};
  const entry = {
    id: share.id,
    kind: found.kind || '',
    label: found.kind_label || (share.kind === 'text' ? '文本' : '文件'),
    language: found.language_label || '',
    fileName: share.file_name || '',
    size: share.size,
    createdAt: (share.created_at || Date.now() / 1000) * 1000,
    expiresAt: share.expires_at,
    maxReads: share.max_reads,
    readCount: share.read_count || 0,
    remaining: share.remaining,
    state: share.state || 'active',
    url: share.url,
    extractCode: share.extract_code || '',
    manageToken: share.manage_token || '',
    otaUrl: ''
  };
  if (!share.manage_token) {
    // 从历史重开时不会带凭证，别把已有的覆盖掉
    const existing = history.find((item) => item.id === entry.id);
    if (existing) entry.manageToken = existing.manageToken;
  }
  history = [entry, ...history.filter((item) => item.id !== entry.id)].slice(0, HISTORY_LIMIT);
  saveHistory();
  renderHistory();
}

/* ---------------------------------------------------------------- 历史渲染 */

function remainingOf(entry) {
  if (entry.remaining !== undefined) return entry.remaining;
  if (entry.maxReads < 0) return null;
  return Math.max(0, entry.maxReads - entry.readCount);
}

function stateBadge(entry) {
  const remaining = remainingOf(entry);
  if (entry.state === 'burned' || (remaining !== null && remaining <= 0)) {
    return '<span class="tag tag--danger">已焚毁</span>';
  }
  if (entry.state === 'expired') return '<span class="tag tag--warn">已过期</span>';
  if (remaining === null) return '<span class="tag tag--accent">不限次数</span>';
  if (!entry.readCount) return '<span class="tag tag--ok">待读取</span>';
  return `<span class="tag tag--warn">已读 ${entry.readCount} 次</span>`;
}

function renderHistory() {
  const badge = document.querySelector('#historyBadge');
  if (badge) {
    // 顶栏只提示「有还没被读的」，不暴露条数细节
    const pending = history.filter(
      (entry) => entry.state === 'active' && !entry.readCount
    ).length;
    badge.textContent = String(pending);
    badge.classList.toggle('hidden', pending === 0);
  }

  // OAuth 模式用 /history 独立页面，本页没有抽屉可渲染
  const list = $('#historyList');
  if (!list) return;

  const count = $('#historyCount');
  if (count) count.textContent = history.length ? `${history.length} 条` : '';

  if (!history.length) {
    list.innerHTML = `
      <div class="history-empty">
        ${icon('link')}
        <span>还没有发过东西</span>
      </div>`;
    window.clearInterval(pollTimer);
    return;
  }

  list.innerHTML = history
    .map((entry) => {
      const path = entry.url.replace(/^https?:\/\/[^/]+/, '');
      const sub = [entry.label, entry.language, formatBytes(entry.size)]
        .filter(Boolean)
        .join(' · ');
      return `
      <button class="history-item" data-id="${escapeHtml(entry.id)}" type="button">
        <div class="history-item-top">
          <span class="truncate">${escapeHtml(entry.fileName || entry.label || '分享')}</span>
          ${stateBadge(entry)}
        </div>
        <div class="history-item-sub truncate">${escapeHtml(sub)}</div>
        <div class="history-item-foot">
          <span class="mono truncate">${escapeHtml(path)}</span>
          <span class="muted-2">${formatTime((entry.createdAt || 0) / 1000)}</span>
        </div>
      </button>`;
    })
    .join('');

  list.querySelectorAll('[data-id]').forEach((button) => {
    button.addEventListener('click', () => {
      const entry = history.find((item) => item.id === button.dataset.id);
      if (entry) reopen(entry);
    });
  });

  if (history.some((entry) => entry.state === 'active')) scheduleStatusPoll();
}

let pollTimer = 0;
let polling = false;

async function refreshManagedHistory() {
  if (!managedHistory) return;
  try {
    const data = await api('/api/shares/mine');
    historyOwner = data.owner || '';
    history = (data.items || []).map((share) => ({
      id: share.id,
      kind: share.detected?.kind || '',
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
      url: share.url,
      extractCode: '',
      manageToken: '',
      otaUrl: ''
    }));
    renderHistory();
  } catch {
    /* 请求端会统一处理失效登录；不打断上传主流程 */
  }
}

// OAuth 模式下历史存在服务端：进来就先拉一次，让顶栏角标有数。
// 非 OAuth 模式读的是 localStorage，已经在 loadHistory() 里读好了。
if (managedHistory) {
  history = [];
  refreshManagedHistory();
}

/* ---------------------------------------------------------------- 抽屉开关 */
//
// 只有非 OAuth 模式才有抽屉（OAuth 模式用 /history 独立页面）。
// 这里全部用空值保护，避免在另一套形态下报错。

function openHistory() {
  const drawer = $('#historyDrawer');
  if (!drawer) return;
  drawer.classList.add('is-open');
  $('#historyMask')?.classList.remove('hidden');
  document.body.classList.add('drawer-open');
  renderHistory();
  refreshStatuses();
}

function closeHistory() {
  const drawer = $('#historyDrawer');
  if (!drawer) return;
  drawer.classList.remove('is-open');
  $('#historyMask')?.classList.add('hidden');
  document.body.classList.remove('drawer-open');
}

function toggleHistory() {
  const drawer = $('#historyDrawer');
  if (!drawer) return;
  if (drawer.classList.contains('is-open')) closeHistory();
  else openHistory();
}

$('#historyMask')?.addEventListener('click', closeHistory);
$('#historyClose')?.addEventListener('click', closeHistory);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeHistory();
});
// 顶栏是异步挂载的（要先问登录形态），所以用事件委托而不是直接绑定：
// 模块顶层执行时 #historyToggle 还不存在。
document.addEventListener('click', (event) => {
  if (event.target.closest('#historyToggle')) toggleHistory();
});

$('#historyClear')?.addEventListener('click', () => {
  if (!history.length) return;
  if (!window.confirm('清空本机的发送记录？（不会焚毁已发出的内容）')) return;
  history = [];
  saveHistory();
  renderHistory();
  toast('已清空发送记录', 'ok');
});


function scheduleStatusPoll() {
  window.clearInterval(pollTimer);
  pollTimer = window.setInterval(refreshStatuses, 15_000);
  refreshStatuses();
}

/** 主动查一次状态：这样「待读取」会自己变成「已读」，不用手动刷新。 */
async function refreshStatuses() {
  if (polling) return;
  const ids = history.filter((entry) => entry.state === 'active').map((entry) => entry.id);
  if (!ids.length) {
    window.clearInterval(pollTimer);
    return;
  }
  polling = true;
  try {
    const data = await api('/api/shares/status', {
      method: 'POST',
      body: JSON.stringify({ ids }),
      noRedirect: true
    });
    patchHistory(data.items || []);
  } catch {
    /* 断网时静默，下一轮再试 */
  } finally {
    polling = false;
  }
}

/* ---------------------------------------------------------------- 重开历史 */

function reopen(entry) {
  if (managedHistory) {
    const item = currentBatch.find((share) => share.id === entry.id);
    if (item) showResult(item, { reused: true });
    else window.open(entry.url, '_blank', 'noopener');
    return;
  }
  const isFile = ['binary', 'image'].includes(entry.kind) || Boolean(entry.fileName);
  currentBatch = [
    {
      id: entry.id,
      url: entry.url,
      extract_code: entry.extractCode,
      manage_token: entry.manageToken,
      kind: isFile ? 'file' : 'text',
      file_name: entry.fileName || null,
      size: entry.size,
      max_reads: entry.maxReads,
      read_count: entry.readCount,
      remaining: remainingOf(entry),
      state: entry.state,
      expires_at: entry.expiresAt,
      detected: { kind: entry.kind, kind_label: entry.label, language_label: entry.language }
    }
  ];
  showResult(currentBatch[0], { reused: true });
}

/* ---------------------------------------------------------------- 输入 */

const pasteArea = $('#pasteArea');

pasteArea.addEventListener('input', () => {
  draft = pasteArea.value;
  syncEmptyState();
  scheduleInspect();
});

pasteArea.addEventListener('paste', (event) => {
  const clipboard = event.clipboardData;
  if (!clipboard) return;
  const pasted = Array.from(clipboard.items)
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter(Boolean);
  if (!pasted.length) return;
  event.preventDefault();
  addFiles(pasted, { fromPaste: true });
});

const dropzone = $('#dropzone');
['dragenter', 'dragover'].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add('is-over');
  })
);
['dragleave', 'drop'].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove('is-over');
  })
);
dropzone.addEventListener('drop', (event) => {
  const dropped = event.dataTransfer?.files;
  if (dropped?.length) addFiles(dropped);
});
dropzone.addEventListener('click', (event) => {
  if (event.target === pasteArea) return;
  $('#fileInput').click();
});
$('#pickFile').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', () => {
  addFiles($('#fileInput').files);
  $('#fileInput').value = '';
});

function addFiles(list, { fromPaste = false } = {}) {
  const added = [];
  for (const file of Array.from(list || [])) {
    if (files.some((existing) => existing.name === file.name && existing.size === file.size)) {
      continue;
    }
    files.push(file);
    added.push(file);
  }
  if (!added.length) return;
  renderFiles();
  updateFirmwareHint();
  if (fromPaste) {
    toast(`已加入 ${added.length} 个文件`, 'ok');
    const image = added.find((file) => file.type.startsWith('image/'));
    if (image) previewLocalImage(image);
  }
}

function renderFiles() {
  const host = $('#fileStrip');
  if (!files.length) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  host.classList.remove('hidden');
  host.innerHTML = files
    .map(
      (file, index) => `
      <div class="file-chip">
        <span class="icon-wrap" data-icon="${
          isHexLike(file.name) ? 'chip' : file.type.startsWith('image/') ? 'image' : 'file'
        }"></span>
        <span class="name truncate">${escapeHtml(file.name)}</span>
        <span class="size">${formatBytes(file.size)}</span>
        <button class="btn btn--ghost btn--sm btn--icon" data-remove="${index}" title="移除">×</button>
      </div>`
    )
    .join('');
  hydrateIcons(host);
  host.querySelectorAll('[data-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      files.splice(Number(button.dataset.remove), 1);
      renderFiles();
      updateFirmwareHint();
      syncEmptyState();
    });
  });
}

function updateFirmwareHint() {
  $('#fwNote').classList.toggle(
    'hidden',
    !files.some((file) => isHexLike(file.name))
  );
}

function previewLocalImage(file) {
  const url = URL.createObjectURL(file);
  $('#previewWrap').classList.remove('hidden');
  $('#previewHint').textContent = `${formatBytes(file.size)} · 本地预览`;
  $('#preview').innerHTML = `<img src="${url}" alt="${escapeHtml(file.name)}" />`;
}

function syncEmptyState() {
  const hasContent = Boolean(draft.trim()) || files.length > 0;
  $('#captureEmpty').classList.toggle('hidden', hasContent);
  pasteArea.classList.toggle('has-content', Boolean(draft));
  if (!hasContent) {
    $('#previewWrap').classList.add('hidden');
    $('#detectTag').textContent = '等待内容';
    $('#detectTag').className = 'tag';
    $('#detectDetail').textContent = '';
  }
}

/* ---------------------------------------------------------------- 识别 */

api('/api/limits')
  .then((limits) => {
    if (!limits) return;
    // 上限写在空状态浮层里，不再动 placeholder：
    // placeholder 与浮层是同一块区域，窗口一矮就会互相压住。
    $('#captureLimits').textContent =
      `单次 ≤ ${Math.floor(limits.max_text_bytes / 1024)} KB · ` +
      `文件 ≤ ${Math.floor(limits.max_file_bytes / (1024 * 1024))} MB`;
  })
  .catch(() => null);

async function loadKinds() {
  try {
    const data = await api('/api/inspect', {
      method: 'POST',
      body: JSON.stringify({ text: 'x', render: false })
    });
    const select = $('#kindOverride');
    for (const item of data.kinds || []) {
      const option = document.createElement('option');
      option.value = item.kind;
      option.textContent = item.label;
      select.appendChild(option);
    }
  } catch {
    /* 拿不到就只留自动识别 */
  }
}
loadKinds();

$('#kindOverride').addEventListener('change', (event) => {
  forcedKind = event.target.value;
  scheduleInspect(0);
});

function scheduleInspect(delay = 250) {
  window.clearTimeout(inspectTimer);
  inspectTimer = window.setTimeout(runInspect, delay);
}

async function runInspect() {
  const text = draft;
  if (!text.trim()) {
    if (!files.length) syncEmptyState();
    return;
  }
  const seq = ++inspectSeq;
  $('#detectTag').textContent = '识别中…';
  $('#detectTag').className = 'tag tag--accent';
  $('#detectDetail').textContent = '';
  try {
    const result = await api('/api/inspect', {
      method: 'POST',
      body: JSON.stringify({ text, force: forcedKind || null, render: true })
    });
    if (seq !== inspectSeq) return;
    applyInspection(result);
  } catch (error) {
    if (seq !== inspectSeq) return;
    $('#detectTag').textContent = '识别失败';
    $('#detectTag').className = 'tag tag--danger';
    $('#detectDetail').textContent = error.message || '';
  }
}

function applyInspection(result) {
  const { kind, kind_label: label, language_label: langLabel, signals, meta, confidence } =
    result.detected;

  $('#detectTag').textContent = langLabel ? `${label} · ${langLabel}` : label;
  $('#detectTag').className = 'tag ' + (kind === 'text' ? 'tag--warn' : 'tag--accent');
  $('#detectDetail').textContent = describeSignals(kind, signals, meta, confidence);

  $('#previewWrap').classList.remove('hidden');
  $('#previewHint').textContent = result.truncated ? '仅预览前 200 KB' : previewSizeHint(meta);
  $('#preview').innerHTML = result.html;
  if (result.truncated) {
    $('#preview').insertAdjacentHTML(
      'beforeend',
      '<p class="field-note" style="padding: 0 16px 14px">内容较大，预览只渲染了前 200 KB；发出的仍是全文。</p>'
    );
  }
  if (!forcedKind) $('#kindOverride').value = '';
}

function describeSignals(kind, signals, meta, confidence) {
  const parts = [];
  if (kind === 'text') parts.push('没识别出结构，按纯文本发');
  if (meta && meta.lines) parts.push(`${meta.lines} 行`);
  if (meta && meta.levels) {
    const order = ['FATAL', 'ERROR', 'WARN', 'INFO', 'DEBUG', 'TRACE'];
    const summary = order
      .filter((level) => meta.levels[level])
      .map((level) => `${meta.levels[level]} ${level}`)
      .join(' · ');
    if (summary) parts.push(summary);
  }
  if (meta && meta.additions !== undefined) parts.push(`+${meta.additions} / −${meta.deletions}`);
  if (meta && meta.rows !== undefined) parts.push(`${meta.rows} 行 × ${meta.columns} 列`);
  if (confidence !== undefined) parts.push(`${Math.round(confidence * 100)}%`);
  if (!parts.length && signals) parts.push(...signals.slice(0, 2));
  return parts.join(' · ');
}

function previewSizeHint(meta) {
  if (!meta || !meta.lines) return '';
  return `${meta.lines} 行 · ${meta.chars} 字符`;
}

/* ---------------------------------------------------------------- 参数弹窗 */

function loadOptions() {
  try {
    const raw = localStorage.getItem(OPTIONS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function applyStoredOptions() {
  const stored = loadOptions();
  if (!stored) return;
  if (stored.ttl !== undefined) $('#ttl').value = String(stored.ttl);
  if (stored.reads !== undefined) $('#reads').value = String(stored.reads);
  if (stored.noCode !== undefined) $('#noCode').checked = Boolean(stored.noCode);
}

function readOptions() {
  return {
    ttl: Number($('#ttl').value),
    reads: Number($('#reads').value),
    // 留空 = 让服务端自动生成（默认行为）
    code: $('#code').value.trim(),
    noCode: $('#noCode').checked
  };
}

function optionsSummary() {
  const { ttl, reads, code, noCode } = readOptions();
  const ttlText = ttl === 0 ? '不过期' : ttl >= 24 ? `${ttl / 24} 天` : `${ttl} 小时`;
  const readText = reads < 0 ? '不限次数' : `${reads} 次`;
  const codeText = noCode ? '无提取码' : code ? `码 ${code}` : '自动生成提取码';
  return `${ttlText} · ${readText} · ${codeText}`;
}

function openOptions() {
  $('#optionsMask').classList.remove('hidden');
  $('#optionsSummary').textContent = optionsSummary();
  updateFirmwareHint();
}

function closeOptions() {
  $('#optionsMask').classList.add('hidden');
}

$('#genCode').addEventListener('click', () => {
  $('#code').value = String(Math.floor(Math.random() * 10000)).padStart(4, '0');
  $('#noCode').checked = false;
  $('#optionsSummary').textContent = optionsSummary();
});

$('#noCode').addEventListener('change', (event) => {
  $('#code').disabled = event.target.checked;
  if (event.target.checked) $('#code').value = '';
  $('#optionsSummary').textContent = optionsSummary();
});

['#ttl', '#reads', '#code'].forEach((selector) => {
  $(selector).addEventListener('input', () => {
    $('#optionsSummary').textContent = optionsSummary();
  });
});

$('#optionsClose').addEventListener('click', closeOptions);
$('#optionsMask').addEventListener('click', (event) => {
  if (event.target === $('#optionsMask')) closeOptions();
});

/* ---------------------------------------------------------------- 生成 */

$('#submitBtn').addEventListener('click', () => {
  $('#errorText').textContent = '';
  if (!draft.trim() && !files.length) {
    $('#errorText').textContent = '还没有内容：粘贴一段文字，或拖入一个文件';
    pasteArea.focus();
    return;
  }
  applyStoredOptions();
  openOptions();
});

$('#confirmBtn').addEventListener('click', async () => {
  const { ttl, reads, code, noCode } = readOptions();
  if ($('#rememberOptions').checked) {
    try {
      // 只记「有效期 / 次数 / 是否要码」这些纯偏好；具体提取码是敏感的且一次性的，
      // 记下来等于以后每条都用同一个码，反而更弱。
      localStorage.setItem(OPTIONS_KEY, JSON.stringify({ ttl, reads, noCode }));
    } catch {
      /* 忽略 */
    }
  }
  closeOptions();
  await send({ ttl, reads, code, noCode });
});

async function send({ ttl, reads, code, noCode }) {
  const button = $('#submitBtn');
  button.disabled = true;
  $('#progressWrap').classList.remove('hidden');
  $('#progressBar').style.width = '0%';

  try {
    // 一次发布 = 一条分享。粘贴的文字和拖进来的文件全部放进同一个请求，
    // 服务端据此建一条记录：一个链接、一个提取码、一次焚毁。
    const form = new FormData();
    for (const file of files) form.append('file', file);
    if (draft.trim()) form.append('text', draft);
    form.append('ttl_hours', String(ttl));
    form.append('max_reads', String(reads));
    if (noCode) form.append('no_code', '1');
    else if (code) form.append('extract_code', code);
    if (forcedKind) form.append('kind', forcedKind);

    const parts = [];
    if (files.length) parts.push(`${files.length} 个文件`);
    if (draft.trim()) parts.push('文本');
    $('#progressText').textContent = `上传中（${parts.join(' + ') || '内容'}）…`;

    const created = await uploadWithProgress('/api/shares', form, (loaded, total) => {
      $('#progressBar').style.width = `${total ? Math.round((loaded / total) * 100) : 0}%`;
    });

    // 发完就清场：内容已经出去了，留着草稿只会让人以为还没发
    draft = '';
    files = [];
    pasteArea.value = '';
    $('#preview').innerHTML = '';
    renderFiles();
    updateFirmwareHint();
    syncEmptyState();

    if (!created || !created.id) return;
    remember(created);
    currentBatch = [created];
    // 复制成功就到此为止：消息已经在剪贴板里，页面上不再回显一遍
    // （回显等于让用户去核对一段已经复制好的文字，纯属多余）。
    await autoCopy([created]);
  } catch (error) {
    $('#errorText').textContent = error.message || '生成失败';
    toast(error.message || '生成失败', 'err');
  } finally {
    button.disabled = false;
    $('#progressWrap').classList.add('hidden');
    $('#progressText').textContent = '';
  }
}

/**
 * 生成「发给对方的那段话」。
 *
 * 只发一个光秃秃的链接会显得像钓鱼（对方不知道是什么、要不要点），
 * 所以带一句人话说明是什么东西；提取码单独一行，方便对方一眼抄下来。
 */
const KIND_PHRASE = {
  code: (found) =>
    found.language_label ? `一段 ${found.language_label} 代码` : '一段代码',
  log: () => '一段日志',
  diff: () => '一份 Diff',
  table: () => '一张表格',
  json: () => '一段 JSON',
  markdown: () => '一份 Markdown',
  stacktrace: () => '一段报错堆栈',
  urls: () => '一组链接',
  image: (found, share) =>
    share.file_name ? `一张图片（${share.file_name}）` : '一张图片',
  binary: (found, share) =>
    share.file_name ? `一个文件（${share.file_name}）` : '一个文件',
  text: () => '一段文本'
};

function describeShare(share) {
  const count = share.file_count || 0;
  const hasText = Boolean(
    share.detected && share.detected.kind && share.detected.kind !== 'binary'
  );

  // 一次分享里的文件数优先：它才是对方最关心的「这是几个东西」
  if (count > 1) {
    return `一个包含 ${count} 个文件的分享`;
  }
  if (count === 1 && hasText) {
    const name = share.file_name || (share.files && share.files[0]?.file_name);
    return name ? `一段文本和一个文件（${name}）` : '一段文本和一个文件';
  }
  if (count === 1) {
    const name = share.file_name || (share.files && share.files[0]?.file_name);
    return name ? `一个文件（${name}）` : '一个文件';
  }

  const found = share.detected || {};
  const phrase = KIND_PHRASE[found.kind];
  if (phrase) return phrase(found, share);
  return '一段内容';
}

function shareMessage(share) {
  const lines = [`我分享给你${describeShare(share)}：`, share.url];
  // 多文件分享附上清单，对方一眼知道包里有什么
  const names = (share.files || []).map((item) => item.file_name);
  if (names.length > 1) lines.push(`文件：${names.join('、')}`);
  if (share.extract_code) lines.push(`分享码 ${share.extract_code}`);
  // 一次性链接要提前说，否则对方随手关掉就再也打不开了
  if (share.max_reads === 1) lines.push('阅后即焚，打开看完就没了。');
  else if (share.max_reads > 1) lines.push(`可打开 ${share.max_reads} 次。`);
  return lines.join('
');
}

/* ---------------------------------------------------------------- 生成后的轻提示 */

/**
 * 复制并给出反馈。
 *
 * 复制成功就不弹窗（消息已经在剪贴板里，再点「确定」纯属打断）；
 * 复制失败才展开结果弹窗，否则用户拿不到那段话。
 */
async function autoCopy(created) {
  // 一次发布只有一条分享，所以永远只有一段说明
  const message = shareMessage(created[0]);
  const ok = await copyText(message);
  if (ok) {
    toast('已复制分享说明', 'ok', 2600);
    return true;
  }
  showResult(created[0]);
  toast('自动复制被浏览器拦住了，请手动复制', 'warn', 4200);
  return false;
}

/* ---------------------------------------------------------------- 结果弹窗 */

function showResult(share, { count = 1, reused = false } = {}) {
  const found = share.detected || {};
  $('#resultTitle').textContent = reused
    ? '这条分享'
    : count > 1
      ? `已生成 ${count} 条`
      : '已生成';
  $('#shareUrl').value = share.url;
  $('#shareCode').textContent = share.extract_code || '';
  $('#codeRow').style.display = share.extract_code ? 'flex' : 'none';

  if (count > 1) {
    $('#resultMeta').innerHTML = currentBatch
      .map(
        (item) => `
        <dt>${escapeHtml(item.file_name || item.detected?.kind_label || '文本')}</dt>
        <dd><span class="mono">${item.url}</span>
          ${item.extract_code ? `<span class="muted-2"> · 码 ${item.extract_code}</span>` : ''}
          <span class="muted-2"> · ${formatBytes(item.size)}</span></dd>`
      )
      .join('');
  } else {
    const sizeLine = `${formatBytes(share.size)}${
      found.meta && found.meta.lines ? ` · ${found.meta.lines} 行` : ''
    }`;
    $('#resultMeta').innerHTML = `
      <dt>形态</dt><dd>${escapeHtml(found.kind_label || '—')}${
        found.language_label ? ` · ${escapeHtml(found.language_label)}` : ''
      }</dd>
      <dt>大小</dt><dd>${sizeLine}</dd>
      <dt>有效期</dt><dd>${
        share.expires_at ? formatTime(share.expires_at) : '不过期'
      }${
        share.expires_at
          ? ` <span class="muted-2">（剩 ${formatCountdown(share.expires_at)}）</span>`
          : ''
      }</dd>
      <dt>可读次数</dt><dd>${share.max_reads < 0 ? '不限' : share.max_reads + ' 次'}</dd>`;
  }

  const entry = history.find((item) => item.id === share.id);
  if (!reused && share.kind === 'file' && isHexLike(share.file_name)) {
    createOta(share);
  } else if (entry && entry.otaUrl) {
    $('#otaBlock').style.display = 'block';
    $('#otaUrl').value = entry.otaUrl;
  } else {
    $('#otaBlock').style.display = 'none';
  }

  $('#newShare').textContent = reused ? '关闭' : '继续传';
  $('#resultMask').classList.remove('hidden');
}

function closeResult() {
  $('#resultMask').classList.add('hidden');
}

$('#resultClose').addEventListener('click', closeResult);
$('#resultMask').addEventListener('click', (event) => {
  if (event.target === $('#resultMask')) closeResult();
});
$('#newShare').addEventListener('click', () => {
  closeResult();
  pasteArea.focus();
});

async function createOta(share) {
  try {
    const ota = await api(`/api/shares/${share.id}/ota`, {
      method: 'POST',
      body: JSON.stringify({ manage_token: share.manage_token, label: 'device', max_uses: 1 })
    });
    $('#otaBlock').style.display = 'block';
    $('#otaUrl').value = ota.url;
    const entry = history.find((item) => item.id === share.id);
    if (entry) {
      entry.otaUrl = ota.url;
      saveHistory();
    }
  } catch (error) {
    $('#otaBlock').style.display = 'none';
    toast(`直链创建失败：${error.message}`, 'warn');
  }
}

/* ---------------------------------------------------------------- 结果操作 */

async function copyOrToast(text) {
  if (!text) return;
  const ok = await copyText(text);
  toast(ok ? '已复制' : '复制失败，请手动选择', ok ? 'ok' : 'err');
}

$('#copyLink').addEventListener('click', () => copyOrToast($('#shareUrl').value));
$('#copyCode').addEventListener('click', () => copyOrToast($('#shareCode').textContent));
$('#copyOta').addEventListener('click', () => copyOrToast($('#otaUrl').value));
$('#openLink').addEventListener('click', () => window.open($('#shareUrl').value, '_blank'));

$('#burnBtn').addEventListener('click', async () => {
  const targets = currentBatch.filter((share) => share.manage_token);
  if (!targets.length) {
    toast('这条分享没有可用的管理凭证', 'warn');
    return;
  }
  if (!window.confirm('焚毁后内容立即删除且无法恢复，确定吗？')) return;
  let failed = 0;
  for (const share of targets) {
    try {
      await api(`/api/shares/${share.id}/burn`, {
        method: 'POST',
        body: JSON.stringify({ manage_token: share.manage_token })
      });
      const entry = history.find((item) => item.id === share.id);
      if (entry) {
        entry.state = 'burned';
        entry.remaining = 0;
      }
    } catch {
      failed += 1;
    }
  }
  saveHistory();
  renderHistory();
  toast(failed ? `已处理，${failed} 个失败` : '已焚毁', failed ? 'warn' : 'ok');
  closeResult();
});

/* ---------------------------------------------------------------- 清空 / 键盘 */

$('#clearBtn').addEventListener('click', () => {
  draft = '';
  files = [];
  forcedKind = '';
  $('#kindOverride').value = '';
  pasteArea.value = '';
  $('#preview').innerHTML = '';
  renderFiles();
  updateFirmwareHint();
  syncEmptyState();
  pasteArea.focus();
});

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    $('#submitBtn').click();
  }
  if (event.key === 'Escape') {
    closeOptions();
    closeResult();
  }
  if ((event.ctrlKey || event.metaKey) && event.key === 'v') {
    const target = event.target;
    const inOtherField =
      target instanceof HTMLElement &&
      (['code', 'kindOverride'].includes(target.id) || target.tagName === 'SELECT');
    if (!inOtherField && document.activeElement !== pasteArea) pasteArea.focus();
  }
});

/* ---------------------------------------------------------------- 启动 */

applyStoredOptions();
syncEmptyState();
renderHistory();
pasteArea.focus();
