/*
 * 公共工具：主题、toast、API 包装、格式化、图标。
 *
 * 两个页面（上传页 / 分享页）共用这一份，避免同一段 fetch 写两遍。
 * 全部是 ES module，没有构建步骤 —— 页面直接 <script type="module">。
 */

/* ---------------------------------------------------------------- 挂载点 */

/*
 * 本服务可能挂在域名根上（`/s/<id>`、`/api/...`），也可能挂在子目录
 * （`/bfs/<id>`、`/bfs/api/...`）。页面由服务端注入这几个常量，
 * 前端所有地址都从它们拼 —— 绝不在代码里写死 `/api/shares`。
 *
 * 兜底值对应「根部署」，所以即使注入脚本被挡掉（比如自定义 CSP），
 * 页面仍然能用，只是不支持子目录部署。
 */
const BOOT = {
  base: window.__BFS_BASE__ ?? '',
  sharePrefix: window.__BFS_SHARE_PREFIX__ ?? '/s',
  apiPrefix: window.__BFS_API_PREFIX__ ?? '/api',
  loginPath: window.__BFS_LOGIN_PATH__ ?? '/login',
  gate: window.__BFS_GATE__ === true
};

/** 站内绝对路径（带挂载前缀）。 */
export function sitePath(path) {
  const clean = path.startsWith('/') ? path : `/${path}`;
  return `${BOOT.base}${clean}`;
}

/** API 地址。`path` 是不带 `/api` 的相对段，例如 `/shares/status`。 */
export function apiPath(path) {
  const clean = path.startsWith('/') ? path : `/${path}`;
  return `${BOOT.apiPrefix}${clean}`;
}

/** 分享页路径：`/bfs/<id>` 或 `/s/<id>`。 */
export function sharePath(shareId) {
  return `${BOOT.base}${BOOT.sharePrefix}/${shareId}`;
}

export { BOOT };

/* ---------------------------------------------------------------- 主题 */

const THEME_KEY = 'bfs.theme';

export function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem(THEME_KEY);
  } catch {
    /* 隐私模式 */
  }
  const theme = saved === 'light' || saved === 'dark' ? saved : 'dark';
  document.documentElement.dataset.theme = theme;
  return theme;
}

export function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* 写不了也不影响本页 */
  }
  return next;
}

/* ---------------------------------------------------------------- 图标 */

const ICONS = {
  upload:
    '<path d="M12 16V4m0 0L7 9m5-5 5 5" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/>',
  file: '<path d="M6 3h7l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/><path d="M13 3v5h5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>',
  text: '<path d="M5 5h14M5 10h14M5 15h9" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/>',
  download:
    '<path d="M12 4v11m0 0 4.5-4.5M12 15l-4.5-4.5" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 17v1a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="0" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M15 5H5a1 1 0 0 0-1 1v10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>',
  sun: '<circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="1.6" fill="none"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  moon: '<path d="M20 13.5A7.5 7.5 0 1 1 10.5 4a6 6 0 0 0 9.5 9.5Z" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linejoin="round"/>',
  fire: '<path d="M12 21c3.9 0 6.5-2.4 6.5-6 0-4.4-4-6.3-5.3-10.5-2 1.6-2.5 3.6-2.2 5.3-1.4-.6-2.2-1.7-2.6-3-1.3 2-2.9 4.4-2.9 8.2 0 3.6 2.6 6 6.5 6Z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>',
  rocket:
    '<path d="M13.5 4.5c2.5-1.6 5-1.5 6-1.4.1 1 .2 3.5-1.4 6-1.3 2-3.4 3.9-5.6 5.2l-3.8-3.8c1.3-2.2 3.2-4.3 5.2-5.6Z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/><path d="M8.7 10.5 5.5 11l-2 2 3 .6M13.5 15.3l-.5 3.2-2 2-.6-3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M7 17c-1 1-1.4 2.6-1.4 2.6s1.6-.4 2.6-1.4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/><circle cx="15.2" cy="8.8" r="1.1" fill="currentColor"/>',
  paste:
    '<rect x="6" y="4" width="12" height="17" rx="1" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M9 4V3h6v1" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/><path d="M9 10h6M9 14h6M9 18h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  image:
    '<rect x="3" y="5" width="18" height="14" rx="1" stroke="currentColor" stroke-width="1.5" fill="none"/><circle cx="8.5" cy="10" r="1.5" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="m4 17 5-4.5 4 3.5 3-2.5 4 3.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  alert:
    '<path d="M12 4 2.8 20h18.4L12 4Z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/><path d="M12 10v4m0 3v.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  link: '<path d="M9.5 14.5 14.5 9.5M8 12l-1.8 1.8a3.5 3.5 0 0 0 5 5L13 17M16 12l1.8-1.8a3.5 3.5 0 0 0-5-5L11 7" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/>',
  chip: '<rect x="7" y="7" width="10" height="10" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M10 3v3m4-3v3m-4 12v3m4-3v3M3 10h3m-3 4h3m12-4h3m-3 4h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  check:
    '<path d="M4 12.5 9.5 18 20 6.5" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  arrowLeft:
    '<path d="M19 12H5m0 0 6-6m-6 6 6 6" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  history:
    '<path d="M12 8v4l3 2" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/><path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/><path d="M3 4.5V9h4.5" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  close:
    '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  lock:
    '<rect x="4.5" y="10.5" width="15" height="10" rx="1" stroke="currentColor" stroke-width="1.6" fill="none"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/>',
  settings:
    '<circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.6" fill="none"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  logout:
    '<path d="M15 17v1a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/><path d="M11 12h9m0 0-3-3m3 3-3 3" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  github:
    '<path d="M12 2.2a9.8 9.8 0 0 0-3.1 19.1c.5.1.7-.2.7-.5v-1.7c-2.7.6-3.3-1.3-3.3-1.3-.5-1.1-1.1-1.4-1.1-1.4-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.4-1.1.7-1.3-2.2-.2-4.5-1.1-4.5-4.9 0-1.1.4-2 1-2.7-.1-.2-.4-1.2.1-2.5 0 0 .8-.3 2.7 1a9.3 9.3 0 0 1 4.9 0c1.9-1.3 2.7-1 2.7-1 .5 1.3.2 2.3.1 2.5.6.7 1 1.6 1 2.7 0 3.8-2.3 4.7-4.5 4.9.4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A9.8 9.8 0 0 0 12 2.2Z" fill="currentColor"/>',
  git:
    '<path d="M21.4 11.1 12.9 2.6a1.9 1.9 0 0 0-2.7 0L8.4 4.4l2.2 2.2a2.6 2.6 0 0 1 3.3 3.3l2.1 2.1a2.6 2.6 0 1 1-1.6 1.6l-2-2a2.6 2.6 0 0 1-2.8-3.4L7.4 5.7l-5 5a1.9 1.9 0 0 0 0 2.7l8.5 8.5a1.9 1.9 0 0 0 2.7 0l7.8-7.8a1.9 1.9 0 0 0 0-3Z" fill="currentColor"/><circle cx="12" cy="8" r="1" fill="currentColor"/><circle cx="16.5" cy="15.5" r="1" fill="currentColor"/>'
};

export function icon(name, cls = 'icon') {
  const body = ICONS[name] || '';
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
}

/* ---------------------------------------------------------------- toast */

let toastHost = null;

export function toast(message, kind = 'ok', ms = 2400) {
  if (!toastHost) {
    toastHost = document.createElement('div');
    toastHost.className = 'toast-host';
    document.body.appendChild(toastHost);
  }
  const el = document.createElement('div');
  el.className = `toast toast--${kind}`;
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .18s ease';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 200);
  }, ms);
}

/* ---------------------------------------------------------------- API */

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message || code);
    this.code = code;
    this.status = status;
  }
}

async function parseError(res) {
  let code = `http_${res.status}`;
  let message = res.statusText || '请求失败';
  try {
    const body = await res.json();
    if (body && body.error) {
      code = body.error;
      message = body.message || message;
    }
  } catch {
    /* 非 JSON 响应：保留状态码 */
  }
  return new ApiError(code, message, res.status);
}

/** 把代码里写的 `/api/...` 改写成实际挂载点（子目录部署时是 `/bfs/api/...`）。 */
export function resolveUrl(path) {
  if (path.startsWith('/api/') || path === '/api') {
    return BOOT.apiPrefix + path.slice(4);
  }
  return path;
}

export async function api(path, { noRedirect = false, ...options } = {}) {
  const res = await fetch(resolveUrl(path), {
    credentials: 'same-origin',
    headers:
      options.body && !(options.body instanceof FormData)
        ? { 'Content-Type': 'application/json' }
        : undefined,
    ...options
  });
  if (!res.ok) {
    const error = await parseError(res);
    // 登录态过期：直接把整页带去登录，别让用户对着一个红色 toast 猜
    if (error.status === 401 && !noRedirect) redirectToLogin();
    throw error;
  }
  const type = res.headers.get('content-type') || '';
  if (type.includes('application/json')) return res.json();
  return res.text();
}

/** 跳到登录页，并带上回跳地址。 */
export function redirectToLogin() {
  const next = location.pathname + location.search;
  location.replace(`${BOOT.loginPath}?next=${encodeURIComponent(next)}`);
}

/** 问一次登录状态：页面决定要不要显示「退出」、要不要等人登录。 */
export async function authSession() {
  if (!BOOT.gate) return { gate: false, authenticated: true };
  try {
    return await api('/api/auth/session', { noRedirect: true });
  } catch {
    return { gate: true, authenticated: false, methods: {} };
  }
}

/** 退出登录。 */
export async function logout() {
  try {
    await api('/api/auth/logout', { method: 'POST', noRedirect: true });
  } catch {
    /* 反正都要回登录页 */
  }
  location.replace(BOOT.loginPath);
}

/** 带上传进度的 POST（fetch 本身没有进度事件，用 XHR）。 */
export function uploadWithProgress(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', resolveUrl(path));
    xhr.withCredentials = true;
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    });
    xhr.addEventListener('load', () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body);
      } else {
        // 上传走到这里说明登录态过期了：跳登录比报「上传失败」有用
        if (xhr.status === 401) redirectToLogin();
        reject(
          new ApiError(
            body?.error || `http_${xhr.status}`,
            body?.message || `上传失败（${xhr.status}）`,
            xhr.status
          )
        );
      }
    });
    xhr.addEventListener('error', () =>
      reject(new ApiError('network', '网络错误，上传中断', 0))
    );
    xhr.addEventListener('abort', () =>
      reject(new ApiError('aborted', '已取消', 0))
    );
    xhr.send(formData);
  });
}

/* ---------------------------------------------------------------- 格式化 */

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

export function formatTime(unixSeconds) {
  if (!unixSeconds) return '—';
  const date = new Date(unixSeconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

export function formatCountdown(unixSeconds) {
  if (!unixSeconds) return '永不过期';
  const diff = unixSeconds - Math.floor(Date.now() / 1000);
  if (diff <= 0) return '已过期';
  const days = Math.floor(diff / 86400);
  const hours = Math.floor((diff % 86400) / 3600);
  const minutes = Math.floor((diff % 3600) / 60);
  const seconds = diff % 60;
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

/** 剩余次数的可读文案（null = 不限）。 */
export function formatRemaining(value) {
  return value === null || value === undefined ? '不限' : String(value);
}

const TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'log', 'csv', 'tsv', 'json', 'yaml', 'yml', 'toml', 'ini',
  'cfg', 'conf', 'env', 'xml', 'html', 'htm', 'css', 'scss', 'js', 'mjs', 'cjs', 'ts',
  'tsx', 'jsx', 'vue', 'py', 'pyi', 'c', 'h', 'cc', 'cpp', 'hpp', 'cs', 'java', 'kt',
  'go', 'rs', 'rb', 'php', 'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd', 'sql', 'make',
  'cmake', 'dockerfile', 'gitignore', 'gitattributes', 'editorconfig', 's', 'asm',
  'ld', 'v', 'sv', 'vhd', 'ino', 'dts', 'dtsi', 'map', 'lst', 'hex', 'patch', 'diff'
]);

export function extensionOf(name) {
  if (!name) return '';
  const base = name.toLowerCase();
  if (!base.includes('.')) return base;
  return base.slice(base.lastIndexOf('.') + 1);
}

export function isMarkdown(name, contentType = '') {
  const ext = extensionOf(name);
  return ext === 'md' || ext === 'markdown' || contentType.includes('markdown');
}

export function isPdf(name, contentType = '') {
  return extensionOf(name) === 'pdf' || contentType.includes('pdf');
}

export function isImage(name, contentType = '') {
  if (contentType.startsWith('image/')) return true;
  return ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif'].includes(
    extensionOf(name)
  );
}

export function isHexLike(name) {
  return ['bin', 'hex', 'elf', 'uf2', 'img', 'fw'].includes(extensionOf(name));
}

export function isProbablyText(name, contentType = '') {
  if (contentType.startsWith('text/') || contentType.includes('json')) return true;
  return TEXT_EXTENSIONS.has(extensionOf(name));
}

/* ---------------------------------------------------------------- 其他 */

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 非安全上下文（http + 内网 IP）里 clipboard API 不可用，退回老办法
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

/** 把 text 渲染成 HTML：Markdown 走服务端 markdown-it，其余走 <pre>。 */
export async function renderText(text, { markdown = false } = {}) {
  if (!markdown) {
    const pre = document.createElement('pre');
    pre.textContent = text;
    return pre.outerHTML;
  }
  try {
    const { html } = await api('/api/render/markdown', {
      method: 'POST',
      body: JSON.stringify({ text })
    });
    return html;
  } catch {
    const pre = document.createElement('pre');
    pre.textContent = text;
    return pre.outerHTML;
  }
}

/**
 * 取「按识别结果渲染好的 HTML」。
 *
 * 分享页的正文一律走这个接口 —— 渲染器只在服务端有一份，
 * 上传页的预览与分享页的正文因此必然一致。
 */
export async function fetchRendered(shareId, { preview = false } = {}) {
  const res = await fetch(
    resolveUrl(`/api/shares/${encodeURIComponent(shareId)}/render${preview ? '?preview=1' : ''}`),
    { credentials: 'same-origin' }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.error || `http_${res.status}`, body.message, res.status);
  }
  return res.json();
}

/** 十六进制转储，只取前 limit 字节（体积大的固件不至于卡死浏览器）。 */
export function hexdump(bytes, { limit = 4096, width = 16 } = {}) {
  const view = bytes.subarray(0, limit);
  const lines = [];
  for (let offset = 0; offset < view.length; offset += width) {
    const slice = view.subarray(offset, offset + width);
    const hex = Array.from(slice, (b) => b.toString(16).padStart(2, '0')).join(' ');
    const ascii = Array.from(slice, (b) =>
      b >= 32 && b < 127 ? String.fromCharCode(b) : '.'
    ).join('');
    lines.push(
      `<span class="offset">${offset.toString(16).padStart(8, '0')}</span>  ${hex.padEnd(
        width * 3 - 1
      )}  ${ascii.replace(/&/g, '&amp;').replace(/</g, '&lt;')}`
    );
  }
  if (bytes.length > limit) {
    lines.push(`… 仅显示前 ${limit} 字节，共 ${bytes.length} 字节（下载后可看全部）`);
  }
  return lines.join('\n');
}

export function downloadBlob(blob, fileName) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 给浏览器一点时间真正开始下载再释放
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/** 顶栏（两个页面共用）。 */
export function mountTopbar({ subtitle, backHref, home = true, history = false, minimal = false, nav = false } = {}) {
  const bar = document.querySelector('.topbar');
  if (!bar) return;
  const themeName = document.documentElement.dataset.theme;
  // 分享页是对外只读的（`home: false`）：受链接者不该被引到上传页，
  // 所以品牌只做标识、不是链接。
  const brand = `${icon('fire', 'icon')}<span>BFS</span>${
    subtitle ? `<span class="brand-sub">${subtitle}</span>` : ''
  }`;
  // minimal：保留品牌标识（对方要能看出这是谁发的工具），但去掉导航与跳转
  if (minimal) {
    bar.innerHTML = `
      <span class="brand">
        ${icon('fire', 'icon')}
        <span>BFS</span>
        <span class="brand-sub">快传</span>
      </span>
      <div class="topbar-actions">
        <button class="btn btn--ghost btn--sm btn--icon" id="themeToggle" title="切换配色">
          ${icon(themeName === 'dark' ? 'sun' : 'moon')}
        </button>
      </div>`;
    bar.querySelector('#themeToggle').addEventListener('click', (event) => {
      const next = toggleTheme();
      event.currentTarget.innerHTML = icon(next === 'dark' ? 'sun' : 'moon');
    });
    return;
  }
  const logoutSlot = home
    ? `<button class="btn btn--ghost btn--sm btn--icon hidden" id="logoutBtn" title="退出登录">
         ${icon('logout')}
       </button>`
    : '';
  // nav：管理模式下顶栏给出「历史 / 设置」两个独立页面入口。
  // 不再把 CI token 塞进历史抽屉 —— 那是两件事，混在一起既难找也不好扩展。
  const navSlot = nav
    ? `<a class="btn btn--ghost btn--sm" href="${escapeHtml(sitePath('/history'))}" title="分享历史">
         ${icon('history')}<span class="btn-label">历史</span>
       </a>
       <a class="btn btn--ghost btn--sm" href="${escapeHtml(sitePath('/settings'))}" title="设置">
         ${icon('settings')}<span class="btn-label">设置</span>
       </a>`
    : '';
  bar.innerHTML = `
    ${
      backHref
        ? `<a class="btn btn--ghost btn--sm btn--icon" href="${escapeHtml(
            backHref
          )}" title="返回">${icon('arrowLeft')}</a>`
        : ''
    }
    ${
      home
        ? `<a class="brand" href="${escapeHtml(sitePath('/'))}">${brand}</a>`
        : `<span class="brand">${brand}</span>`
    }
    <div class="topbar-actions">
      ${
        history
          ? `<button class="btn btn--ghost btn--sm" id="historyToggle" title="发送历史">
              ${icon('history')}<span class="btn-label">历史</span>
              <span class="topbar-badge hidden" id="historyBadge"></span>
            </button>`
          : ''
      }
      ${navSlot}
      <button class="btn btn--ghost btn--sm btn--icon" id="themeToggle" title="切换配色">
        ${icon(themeName === 'dark' ? 'sun' : 'moon')}
      </button>
      ${logoutSlot}
    </div>
  `;
  bar.querySelector('#themeToggle').addEventListener('click', (event) => {
    const next = toggleTheme();
    event.currentTarget.innerHTML = icon(next === 'dark' ? 'sun' : 'moon');
  });

  // 只有开启闸门且确实登录过才给「退出」；公开模式（没配密码）没有退出的概念
  const logoutBtn = bar.querySelector('#logoutBtn');
  if (logoutBtn) {
    authSession().then((info) => {
      if (!info.gate || !info.authenticated) return;
      logoutBtn.classList.remove('hidden');
      logoutBtn.addEventListener('click', () => logout());
    });
  }
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
