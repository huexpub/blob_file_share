/**
 * 分享页（接收端）。
 *
 * 目标：**验证码之后只剩内容**。
 *
 *   有提取码 -> 全屏弹窗（背景虚化，4 格）-> 输对了就展开
 *   没提取码 -> 直接展开
 *
 *   展开后页面上只有：
 *     * 顶栏：BFS 快传 品牌 + 配色开关（不可点击、无站内跳转）
 *     * 正文（代码高亮 / Markdown / PDF / 图片 / 固件 hexdump / 纯文本）
 *     * 一个必需的动作：文本「复制」、文件「下载」
 *
 * 不显示形态徽章、大小、sha256、剩余次数、"阅后即焚"横幅 —— 收件人只关心
 * 「东西是什么、怎么拿走」，其余都是噪音。发送者才需要那些元信息，那在历史里。
 *
 * **关闭浏览器页面 = 消费**：
 *   打开时 `claim` 把正文取到浏览器但不扣次数；关页面时 `pagehide` +
 *   `sendBeacon` 调 `commit`，服务端才扣。切后台也算（移动端常不触发 pagehide）。
 *   浏览器崩溃时 beacon 发不出去，由服务端 `claim_expires_at`（180s）兜底。
 */

import {
  api,
  BOOT,
  copyText,
  escapeHtml,
  formatBytes,
  icon,
  initTheme,
  isImage,
  isMarkdown,
  isPdf,
  isProbablyText,
  mountTopbar,
  resolveUrl,
  toast
} from './app.js';

initTheme();
mountTopbar({ subtitle: '', home: false, minimal: true });

const $ = (selector) => document.querySelector(selector);

function shareIdFromLocation() {
  const path = location.pathname.replace(/\/+$/, '');
  const prefix = BOOT.sharePrefix.replace(/\/+$/, '');
  if (prefix && path.startsWith(prefix)) {
    const rest = path.slice(prefix.length).replace(/^\/+/, '');
    return decodeURIComponent(rest.split('/')[0] || '');
  }
  return decodeURIComponent(path.split('/').pop() || '');
}

const shareId = shareIdFromLocation();

let share = null;
let content = null;
let claimToken = '';
let revealed = false;

/* ---------------------------------------------------------------- 状态 */

function showPanel(name) {
  ['loading', 'dead', 'content'].forEach((id) => {
    $(`#${id}`).classList.toggle('hidden', id !== name);
  });
}

const DEAD_COPY = {
  burned: ['内容已焚毁', '内容已从服务器删除，无法恢复。'],
  exhausted: ['内容已焚毁', '内容已从服务器删除，无法恢复。'],
  expired: ['链接已过期', '内容已被自动清理。'],
  not_found: ['链接不存在', '检查一下链接是否完整。'],
  gone: ['内容已焚毁', '内容已从服务器删除，无法恢复。']
};

function showDead(code) {
  const [title, text] = DEAD_COPY[code] || ['内容不可用', '这份分享已不可用。'];
  $('#deadTitle').textContent = title;
  $('#deadText').textContent = text;
  showPanel('dead');
}

$('#deadIcon').innerHTML = icon('fire', 'icon icon--lg');
$('#gateIcon').innerHTML = icon('lock', 'icon icon--lg');

/* ---------------------------------------------------------------- 提取码 */

const codeBoxes = Array.from(document.querySelectorAll('.code-box'));
const codeValue = () => codeBoxes.map((box) => box.value).join('');

function clearCode() {
  codeBoxes.forEach((box) => {
    box.value = '';
  });
  codeBoxes[0]?.focus();
}

codeBoxes.forEach((box, index) => {
  box.addEventListener('input', () => {
    box.value = box.value.replace(/\D/g, '').slice(-1);
    if (box.value && index < codeBoxes.length - 1) codeBoxes[index + 1].focus();
    if (codeValue().length === codeBoxes.length) unlock();
  });
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Backspace' && !box.value && index > 0) codeBoxes[index - 1].focus();
    if (event.key === 'Enter') unlock();
  });
  box.addEventListener('paste', (event) => {
    const digits = (event.clipboardData?.getData('text') || '').replace(/\D/g, '');
    if (!digits) return;
    event.preventDefault();
    const slice = digits.slice(0, codeBoxes.length);
    codeBoxes.forEach((b, i) => {
      b.value = slice[i] || '';
    });
    if (slice.length === codeBoxes.length) unlock();
    else codeBoxes[Math.min(slice.length, codeBoxes.length - 1)].focus();
  });
});

function showGate() {
  $('#gateMask').classList.remove('hidden');
  document.body.classList.add('modal-open');
  clearCode();
}

function hideGate() {
  $('#gateMask').classList.add('hidden');
  document.body.classList.remove('modal-open');
}

async function unlock() {
  const code = codeValue();
  if (code.length < 4) return;
  try {
    share = await api(`/api/shares/${shareId}/unlock`, {
      method: 'POST',
      body: JSON.stringify({ code })
    });
    hideGate();
    await reveal();
  } catch (error) {
    const banner = $('#codeError');
    banner.textContent = error.message || '提取码不正确';
    banner.classList.remove('hidden');
    clearCode();
  }
}

/* ---------------------------------------------------------------- 取正文 */

async function reveal() {
  if (revealed) return;
  try {
    const res = await fetch(resolveUrl(`/api/shares/${shareId}/claim`), {
      method: 'POST',
      credentials: 'same-origin'
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const error = new Error(body.message || '打开失败');
      error.code = body.error;
      throw error;
    }
    claimToken = res.headers.get('X-Claim-Token') || '';
    const contentTypeHeader = res.headers.get('content-type') || '';

    if (contentTypeHeader.includes('application/json')) {
      // 多文件分享：服务端只回清单，文件本身按需逐个取
      const manifest = await res.json();
      content = {
        type: 'bundle',
        text: manifest.text || '',
        files: manifest.files || [],
        detected: (manifest.share && manifest.share.detected) || {}
      };
    } else if (share.kind === 'text') {
      const text = await res.text();
      let html = '';
      let detected = share.detected || {};
      try {
        const rendered = await api('/api/inspect', {
          method: 'POST',
          body: JSON.stringify({ text: text.slice(0, 200 * 1024), render: true })
        });
        html = rendered.html;
        detected = { ...detected, ...rendered.detected };
      } catch {
        html = '';
      }
      content = { type: 'text', text, html, detected };
    } else {
      content = { type: 'file', blob: await res.blob() };
    }

    renderBody();
    revealed = true;
    showPanel('content');
  } catch (error) {
    showDead(error.code || 'gone');
  }
}

/* ---------------------------------------------------------------- 渲染 */

function renderBody() {
  const host = $('#viewer');
  const found = content.detected || share.detected || {};

  if (content.type === 'bundle') {
    renderBundle();
    return;
  }

  if (content.type === 'text') {
    const text = content.text ?? '';
    host.innerHTML = content.html || `<pre class="plain">${escapeHtml(text)}</pre>`;
    renderActions([{ kind: 'copy', text }]);
    return;
  }

  const blob = content.blob;
  const type = share.content_type || blob.type;
  const name = share.file_name || 'download.bin';
  const actions = [{ kind: 'download', blob, name }];

  if (isPdf(name, type)) {
    host.innerHTML = `<iframe src="${URL.createObjectURL(blob)}" title="PDF"></iframe>`;
  } else if (isImage(name, type)) {
    host.innerHTML = `<img src="${URL.createObjectURL(blob)}" alt="${escapeHtml(name)}" />`;
  } else if (isProbablyText(name, type)) {
    // .md / .txt / .log / .py / .csv 这类：浏览器能读就渲染出来，
    // 只给一个下载按钮等于让人先存盘再看，没必要。
    renderTextFile(blob, name, type, actions);
    return;
  } else {
    // 固件 / 压缩包 / 可执行文件：不显示 hexdump。
    // 十六进制对「我要把它拿走」这个真实意图毫无帮助，只给名称、大小和下载。
    host.innerHTML = `
      <div class="viewer-fallback">
        ${icon('file', 'icon icon--lg')}
        <strong>${escapeHtml(name)}</strong>
        <span class="muted-2">${formatBytes(blob.size)} · 无法预览，请下载</span>
        <a class="btn btn--primary btn--sm viewer-fallback-dl" download="${escapeHtml(name)}"
           href="${URL.createObjectURL(blob)}">下载文件</a>
      </div>`;
  }

  renderActions(actions);
}

/**
 * 多文件分享：正文在上，文件清单在下。
 *
 * 一次发布可能同时带文本和若干文件。文本照常按识别结果渲染，
 * 文件给一行一个：能内联预览的（图片 / PDF / 文本）给「预览」，
 * 其余直接下载。全部文件打包下载作为主操作。
 */
async function renderBundle() {
  const host = $('#viewer');
  const text = content.text || '';
  const files = content.files || [];
  let textHtml = '';

  if (text.trim()) {
    try {
      const rendered = await api('/api/inspect', {
        method: 'POST',
        body: JSON.stringify({ text: text.slice(0, 200 * 1024), render: true })
      });
      textHtml = rendered.html;
    } catch {
      textHtml = `<pre class="plain">${escapeHtml(text)}</pre>`;
    }
  }

  // 重名文件标上序号，否则列表里两行一模一样、下载下来也叫同名
  const seen = new Map();
  const labels = files.map((item) => {
    const base = item.file_name || `file-${item.idx + 1}`;
    const key = base.toLowerCase();
    const count = (seen.get(key) || 0) + 1;
    seen.set(key, count);
    if (count === 1) return { base, label: base, dup: false };
    // 第二次出现才改：先回头补第一个，再给后来的编号
    return { base, label: withSuffix(base, count), dup: true };
  });
  // 有重复时，把「第一份」也标成 -1，避免出现「report.txt / report-2.txt」这种不对称
  const hasDup = labels.some((item) => item.dup);
  if (hasDup) {
    const seen2 = new Map();
    labels.forEach((item) => {
      const key = item.base.toLowerCase();
      const n = (seen2.get(key) || 0) + 1;
      seen2.set(key, n);
      item.label = withSuffix(item.base, n);
    });
  }

  const rows = files
    .map((item, index) => {
      const name = item.file_name;
      const label = labels[index].label;
      const canPreview =
        isImage(name, item.content_type) ||
        isPdf(name, item.content_type) ||
        isProbablyText(name, item.content_type);
      return `
      <li class="bundle-item">
        <span class="bundle-icon">${icon(
          isImage(name, item.content_type) ? 'image' : 'file'
        )}</span>
        <span class="bundle-name truncate" title="${escapeHtml(label)}">${escapeHtml(
          label
        )}</span>
        <span class="bundle-size muted-2">${formatBytes(item.size)}</span>
        <span class="bundle-ops">
          ${
            canPreview
              ? `<button class="btn btn--ghost btn--sm" data-preview="${index}" type="button">预览</button>`
              : ''
          }
          <button class="btn btn--ghost btn--sm" data-file="${index}" type="button">下载</button>
        </span>
      </li>`;
    })
    .join('');

  host.innerHTML = `
    ${textHtml}
    <div class="bundle">
      <div class="bundle-head">
        <span>${files.length} 个文件</span>
        <span class="muted-2">共 ${formatBytes(
          files.reduce((sum, item) => sum + (item.size || 0), 0)
        )}</span>
      </div>
      <ul class="bundle-list">${rows}</ul>
      <div class="bundle-preview hidden" id="bundlePreview"></div>
    </div>`;

  const actions = [{ kind: 'download-bundle' }];
  if (text.trim()) actions.unshift({ kind: 'copy', text });
  renderActions(actions);

  host.querySelectorAll('[data-file]').forEach((button) => {
    button.addEventListener('click', () =>
      downloadBundleFile(Number(button.dataset.file))
    );
  });
  host.querySelectorAll('[data-preview]').forEach((button) => {
    button.addEventListener('click', () =>
      previewBundleFile(Number(button.dataset.preview))
    );
  });
}

/** 打包下载：服务端把这份分享里的所有文件压成一个 zip 流回来。 */
function downloadAll() {
  const url =
    resolveUrl(`/api/shares/${shareId}/download`) +
    `?claim_token=${encodeURIComponent(claimToken)}`;
  // 用隐藏 iframe 触发下载，避免把当前页面导航走
  const frame = document.createElement('iframe');
  frame.style.display = 'none';
  frame.src = url;
  document.body.appendChild(frame);
  setTimeout(() => frame.remove(), 60000);
  toast('开始下载压缩包', 'ok');
}

async function fetchBundleFile(idx) {
  const url = resolveUrl(`/api/shares/${shareId}/file/${idx}`) +
    `?claim_token=${encodeURIComponent(claimToken)}`;
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error('文件已不可用');
  return res.blob();
}

async function downloadBundleFile(idx) {
  try {
    const item = (content.files || [])[idx];
    const blob = await fetchBundleFile(idx);
    const name = document.querySelector(`.bundle-item:nth-child(${idx + 1}) .bundle-name`);
    // 用列表上显示的那个名字（重名时已带 -1/-2），下载下来才不会互相覆盖
    downloadSpec(blob, name ? name.textContent.trim() : item ? item.file_name : `file-${idx + 1}`);
  } catch (error) {
    toast(error.message || '下载失败', 'err');
  }
}

/** `report.txt` + 2 -> `report-2.txt`（保留扩展名，便于系统识别类型） */
function withSuffix(name, n) {
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return `${name}-${n}`;
  return `${name.slice(0, dot)}-${n}${name.slice(dot)}`;
}

async function previewBundleFile(idx) {
  const item = (content.files || [])[idx];
  const box = $('#bundlePreview');
  if (!item || !box) return;
  box.classList.remove('hidden');
  box.innerHTML = '<div class="viewer-loading">读取中…</div>';
  try {
    const blob = await fetchBundleFile(idx);
    const name = item.file_name;
    const type = item.content_type || blob.type;
    if (isImage(name, type)) {
      box.innerHTML = `<img src="${URL.createObjectURL(blob)}" alt="${escapeHtml(name)}" />`;
    } else if (isPdf(name, type)) {
      box.innerHTML = `<iframe src="${URL.createObjectURL(blob)}" title="PDF"></iframe>`;
    } else {
      const text = await blob.text();
      try {
        const rendered = await api('/api/inspect', {
          method: 'POST',
          body: JSON.stringify({
            text: text.slice(0, 200 * 1024),
            render: true,
            filename: name
          })
        });
        box.innerHTML = rendered.html;
      } catch {
        box.innerHTML = `<pre class="plain">${escapeHtml(text)}</pre>`;
      }
    }
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    box.innerHTML = `<div class="field-note">${escapeHtml(
      error.message || '预览失败'
    )}</div>`;
  }
}

/**
 * 操作栏：**只渲染一次**，之后按描述重建。
 *
 * 只显示图标，文字在鼠标悬停时出现（`title` + CSS tooltip）。
 * 理由：接收端的主体是内容，按钮不该抢注意力；图标足够表意，
 * 悬停时才需要确认「这到底是复制还是下载」。
 */
function renderActions(specs) {
  const bar = $('#viewerFoot');
  bar.innerHTML = '';
  for (const spec of specs) {
    const label =
      spec.kind === 'copy'
        ? '复制'
        : spec.kind === 'download-bundle'
          ? '全部下载'
          : '下载';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'rail-btn';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.innerHTML = `${icon(spec.kind === 'copy' ? 'copy' : 'download')}<span>${label}</span>`;
    button.addEventListener('click', () => {
      if (spec.kind === 'copy') copySpec(spec.text);
      else if (spec.kind === 'download-bundle') downloadAll();
      else downloadSpec(spec.blob, spec.name);
    });
    bar.appendChild(button);
  }

  // 「回到顶部」就排在复制/下载下面，共用同一列。
  // 之前给它单独算 fixed 坐标，纯属自找麻烦：同一个容器里排队即可。
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'rail-btn rail-btn--top hidden';
  button.id = 'backToTop';
  button.title = '回到顶部';
  button.setAttribute('aria-label', '回到顶部');
  button.innerHTML = `${icon('rocket')}<span>回到顶部</span>`;
  button.addEventListener('click', () => window.scrollTo(0, 0));
  bar.appendChild(button);
  updateBackToTop();

  // 等一帧再量：刚 append 的按钮还没参与排版，offsetWidth 会是 0。
  setTimeout(placeRail, 0);
}

/**
 * 把操作栏放到「内容区起始位置」的右上角。
 *
 * 它是 `position: fixed`，脱离文档流（内容怎么滚它都不动），
 * 所以位置不能靠布局自动算，得手动对齐内容卡片的右边缘。
 * 触发时机：渲染完成、窗口尺寸变化。滚动不用管 —— 正是「不动」才要 fixed。
 */
/**
 * 把操作栏钉在「内容区起始位置」的右上角。
 *
 * 它是 `position: fixed`：**位置一旦算好就不再变**，内容怎么滚它都不动 ——
 * 这正是之前踩的坑：在 scroll 里重算 `top`，内容滚上去后 `box.top` 变成负数，
 * 被夹到 12px 就成了「跑到顶栏上」。所以现在只在这几种时机重算：
 *   * 首次渲染 / 内容重新渲染
 *   * 窗口尺寸变化
 *   * 内容高度变化（图片、PDF 加载完成）
 *
 * 竖直方向同理：只在首次定位时取内容顶部，之后保持不动。
 */
function placeRail() {
  const rail = $('#viewerFoot');
  const card = $('#viewer');
  if (!rail || !card) return;
  if (!rail.children.length) {
    rail.style.display = 'none';
    return;
  }
  rail.style.display = '';

  const box = card.getBoundingClientRect();
  if (!box.width) return; // 还没排版

  const gap = 14;
  const width = rail.offsetWidth || 40;
  // 贴内容右边缘；视口放不下就退到视口内
  const left = Math.min(box.right + gap, window.innerWidth - width - 12);
  rail.style.left = `${Math.max(8, left)}px`;

  // 首次定位：对齐内容顶部（已经滚过就退到视口上缘，别飘到顶栏外）
  if (!rail.dataset.placed) {
    rail.style.top = `${Math.max(12, box.top)}px`;
    rail.dataset.placed = '1';
  }
  clampRail();
}

/** 只保证操作栏不超出视口底部，不动它的 top 基线。 */
function clampRail() {
  const rail = $('#viewerFoot');
  if (!rail) return;
  const top = parseFloat(rail.style.top || '0');
  const maxTop = Math.max(12, window.innerHeight - rail.offsetHeight - 12);
  if (top > maxTop) rail.style.top = `${maxTop}px`;
}

window.addEventListener('resize', placeRail);
// 内容里可能有图片 / PDF 撑开后高度变化，位置要跟着重算
if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(placeRail).observe(document.body);
}

/* ---------------------------------------------------------------- 回到顶部 */

/**
 * 小火箭：内容超过一屏才出现，就排在复制/下载下面（同一个操作栏）。
 *
 * 判定用「需要滚动多少才能看完」，而不是固定像素 —— 一屏高度因设备而异。
 * 滚回顶部附近就自动隐藏，避免按钮上下闪。
 */
function updateBackToTop() {
  const button = $('#backToTop');
  if (!button) return;
  const oneScreen = window.innerHeight;
  const scrollable = document.documentElement.scrollHeight - oneScreen;
  // 内容刚超过一屏时还不算「很长」，留一点余量
  const longEnough = scrollable > oneScreen * 0.25;
  const notAtTop = window.scrollY > oneScreen * 0.35;
  button.classList.toggle('hidden', !(longEnough && notAtTop));
  // 按钮出现/消失会改变操作栏高度，重新夹一下别超出视口底部
  clampRail();
}

window.addEventListener('scroll', updateBackToTop, { passive: true });
window.addEventListener('resize', updateBackToTop);

async function copySpec(text) {
  const ok = await copyText(text);
  toast(ok ? '已复制' : '复制失败，请手动选择', ok ? 'ok' : 'err');
}

function downloadSpec(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast('已开始下载', 'ok');
}

/**
 * 文本类文件（.md / .txt / .log / .py / .csv …）：交给服务端识别后渲染。
 *
 * 复用 `/api/inspect` 而不是本地判断语法：那套识别器（代码高亮、日志分级、
 * Markdown、CSV 表格）已经在发送端验证过，接收端没理由再写一遍。
 * 识别失败时退回 `<pre>`，至少内容还看得见。
 *
 * 不自己包 `.prose`：`render_html` 对 Markdown 已经输出 `<div class="prose">`，
 * 再包一层会让内边距和字号叠加两遍。
 */
async function renderTextFile(blob, name, type, actions) {
  const host = $('#viewer');
  host.innerHTML = '<div class="viewer-loading">读取中…</div>';
  let text = '';
  try {
    text = await blob.text();
  } catch {
    host.innerHTML = `<div class="viewer-fallback">${icon('file')}<strong>${escapeHtml(
      name
    )}</strong><span class="muted-2">无法读取这个文件</span></div>`;
    return;
  }
  try {
    const { html, detected } = await api('/api/inspect', {
      method: 'POST',
      body: JSON.stringify({
        text: text.slice(0, 200 * 1024),
        render: true,
        // 带上文件名：`.md` 即使正文没有标题也应按 Markdown 渲染
        filename: name
      })
    });
    host.innerHTML = html;
    content.detected = { ...(content.detected || {}), ...detected };
  } catch {
    host.innerHTML = `<pre class="plain">${escapeHtml(text)}</pre>`;
  }
// 文本类文件既能复制也能下载
  renderActions([{ kind: 'copy', text }, ...actions]);
}

/* ---------------------------------------------------------------- 关闭 = 消费 */

let committed = false;

function commitOnce() {
  if (committed || !claimToken) return;
  committed = true;
  const url = resolveUrl(
    `/api/shares/${shareId}/commit?claim_token=${encodeURIComponent(claimToken)}`
  );
  if (navigator.sendBeacon) {
    navigator.sendBeacon(url);
    return;
  }
  try {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, false);
    xhr.send();
  } catch {
    /* 忽略 */
  }
}

window.addEventListener('pagehide', commitOnce);

/*
 * 「切到后台」不等于「关闭页面」。
 *
 * 点击下载时浏览器会弹出保存对话框，窗口同样进入 hidden；更别说
 * 用户在另一个标签页里待一会儿。原来的实现一 hidden 就 commit，
 * 于是「下载一个文件 → 再点另一个」就变成「已经消费过了」。
 *
 * 现在只在真正离开时提交：
 *   * pagehide（关标签、导航离开、移动端切走）—— 唯一可靠的信号；
 *   * 以及超过 30 秒仍处于 hidden 的「疑似关闭」兜底，
 *     避免移动端 pagehide 不触发时永远不消费。
 */
let hiddenTimer = 0;
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    hiddenTimer = window.setTimeout(commitOnce, 30_000);
  } else {
    // 回来了：撤销待提交的兜底，这只是切了个窗口
    window.clearTimeout(hiddenTimer);
    hiddenTimer = 0;
  }
});
window.addEventListener('beforeunload', () => window.clearTimeout(hiddenTimer));
window.addEventListener('pageshow', (event) => {
  if (event.persisted && committed) showDead('burned');
});

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  if (!shareId) {
    showDead('not_found');
    return;
  }
  try {
    share = await api(`/api/shares/${encodeURIComponent(shareId)}`);
  } catch (error) {
    showDead(error.code || 'not_found');
    return;
  }

  if (share.state !== 'active') {
    showDead(share.state);
    return;
  }

  if (share.has_extract_code && !share.unlocked) {
    showPanel('loading');
    $('#loading').classList.add('hidden');
    showGate();
    return;
  }
  await reveal();
}

boot();
