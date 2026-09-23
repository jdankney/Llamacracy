// SPDX-License-Identifier: AGPL-3.0-or-later
// Llamacracy SPA -- vanilla JS, no build step. Served by FastAPI at /.
// Styling lives in styles.css (hand-written; no CSS framework).
const $app = document.getElementById('app');

/* ------------------------------------------------------------------ api */
const api = {
  async get(p) { const r = await fetch(p); if (!r.ok) throw await err(r); return r.json(); },
  async post(p, b) {
    const r = await fetch(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
    if (!r.ok) throw await err(r); return r.json();
  },
  async put(p, b) {
    const r = await fetch(p, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
    if (!r.ok) throw await err(r); return r.json();
  },
  async del(p) { const r = await fetch(p, { method: 'DELETE' }); if (!r.ok) throw await err(r); return r.json(); },
  async upload(file) {
    const fd = new FormData(); fd.append('file', file);
    const r = await fetch('/api/uploads', { method: 'POST', body: fd });
    if (!r.ok) throw await err(r); return r.json();
  },
  // POST + read an SSE stream (EventSource can't POST)
  async *chatStream(body, signal) {
    const r = await fetch('/api/chat', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body), signal,
    });
    if (!r.ok) throw await err(r);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, i); buf = buf.slice(i + 2);
        const line = frame.split('\n').find(l => l.startsWith('data:'));
        if (line) yield JSON.parse(line.slice(5).trim());
      }
    }
  },
};
async function err(r) {
  let d; try { d = (await r.json()).detail; } catch { d = r.statusText; }
  const e = new Error(typeof d === 'string' ? d : JSON.stringify(d)); e.status = r.status; e.detail = d; return e;
}

/* --------------------------------------------------------------- helpers */
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k === 'value') e.value = v;          // property, not attribute (textarea/select)
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : String(kid));
  return e;
};
const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// Inline SVG icons (stroke-based, 24-unit grid). Emoji render differently on
// every platform; these don't.
const ICONS = {
  menu: 'M4 6h16M4 12h16M4 18h16',
  plus: 'M12 5v14M5 12h14',
  x: 'M18 6L6 18M6 6l12 12',
  check: 'M20 6L9 17l-5-5',
  copy: 'M9 9h11a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2zM5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
  search: 'M21 21l-4.3-4.3M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0z',
  clip: 'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48',
  send: 'M12 19V5M5 12l7-7 7 7',
  stop: 'M7 7h10v10H7z',
  chevron: 'M6 9l6 6 6-6',
  down: 'M12 5v14M19 12l-7 7-7-7',
  trash: 'M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6',
  compress: 'M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7',
  globe: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z',
  eye: 'M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  key: 'M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4',
  alert: 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01',
  pen: 'M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z',
  zap: 'M13 2L3 14h9l-1 8 10-12h-9l1-8z',
  code: 'M16 18l6-6-6-6M8 6l-6 6 6 6',
  bulb: 'M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.74V17h8v-2.26A7 7 0 0 0 12 2z',
};
const icon = (name, cls = '') => {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
  s.setAttribute('class', 'icon ' + cls);
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name]); s.append(p);
  return s;
};

// Copy the raw source (markdown/LaTeX as typed/generated, not rendered HTML)
// to the clipboard. The async Clipboard API needs a secure context, and this
// app is deliberately served over plain HTTP (the WireGuard/NetBird tunnel is
// the encryption layer) -- so `navigator.clipboard` is unavailable in most
// browsers here. Fall back to the old execCommand('copy') trick, which works
// on any origin.
async function copyText(btn, text) {
  let ok = false;
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch { /* fall through to the legacy path below */ }
  if (!ok) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');   // a readonly field doesn't summon the mobile keyboard on focus
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    document.body.removeChild(ta);
  }
  const orig = [...btn.childNodes];
  btn.replaceChildren(icon(ok ? 'check' : 'x', 'icon-sm'), ok ? 'Copied' : 'Failed');
  setTimeout(() => btn.replaceChildren(...orig), 1200);
}
const copyBtn = (text, label = 'Copy') => h('button', {
  type: 'button', class: 'btn btn-ghost btn-sm', title: 'Copy the raw markdown/LaTeX source',
  onclick: e => copyText(e.currentTarget, text),
}, icon('copy', 'icon-sm'), label);

// LaTeX -> KaTeX HTML, done BEFORE marked so markdown doesn't eat the
// backslashes in \(...\) and \[...\]. Each math span is pulled out, rendered,
// and swapped back in after marked via a placeholder markdown ignores.
// Handles $$…$$, \[…\], \(…\) and a guarded $…$ (skips currency-looking text).
// private-use code points: never appear in real text, survive marked untouched
const _KA = '\uE000', _KB = '\uE001';
function extractMath(src) {
  if (!window.katex) return { src, spans: [] };
  const spans = [];
  const stash = (tex, display, orig) => {
    try {
      spans.push(katex.renderToString(tex.trim(), { displayMode: display, throwOnError: false, output: 'html' }));
      return _KA + (spans.length - 1) + _KB;
    } catch { return orig; }
  };
  const code = [];                        // shield code so we don't scan $ inside it
  src = src.replace(/```[\s\S]*?```|`[^`\n]+`/g, m => (code.push(m), `\uE002${code.length - 1}\uE003`));
  src = src.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => stash(t, true, m));
  src = src.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => stash(t, true, m));
  src = src.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => stash(t, false, m));
  src = src.replace(/(^|[^\\$\d])\$(?!\s)((?:\\.|[^\\$\n])+?)\$(?!\d)/g,
    (m, pre, t) => (/\s$/.test(t) || /^[\s\d.,]*$/.test(t)) ? m : pre + stash(t, false, '$' + t + '$'));
  src = src.replace(/\uE002(\d+)\uE003/g, (_, i) => code[+i]);
  return { src, spans };
}

if (window.marked) marked.setOptions({ gfm: true, breaks: true });
// Markdown (+ math) -> sanitised HTML. marked + DOMPurify + KaTeX come from the
// CDN; if they didn't load we fall back to escaped text with fenced code
// blocks so a message is never unreadable.
function renderMD(src, math = true) {
  src = src || '';
  if (!src) return '';
  if (!window.marked || !window.DOMPurify) {
    return esc(src)
      .replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, c) => `<pre><code>${c.replace(/\n$/, '')}</code></pre>`)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .split(/\n{2,}/).map(p => p.startsWith('<pre>') ? p : `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
  }
  const { src: pre, spans } = math ? extractMath(src) : { src, spans: [] };
  let html = marked.parse(pre);
  if (spans.length) html = html.replace(new RegExp(_KA + '(\\d+)' + _KB, 'g'), (_, i) => spans[+i] ?? '');
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
}
// render markdown into a live element; the heavy passes (highlight, math) run
// only once the text has stopped streaming
function mdInto(el, src, finalize = true) {
  el.innerHTML = renderMD(src, finalize) || '<span class="faint">…</span>';
  if (finalize && window.hljs) {
    el.querySelectorAll('pre code').forEach(b => { try { hljs.highlightElement(b); } catch { /* unknown language */ } });
  }
  el.querySelectorAll('a[href]').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
}
const mdBlock = (src, attrs = {}, finalize = true) => { const el = h('div', { class: 'prose-chat', ...attrs }); mdInto(el, src, finalize); return el; };
const fmtDur = s => s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;
const untilStr = ts => { const d = ts * 1000 - Date.now(); return d <= 0 ? 'now' : fmtDur(d / 1000); };
const agoStr = ts => { const d = (Date.now() - ts * 1000) / 1000; return d < 60 ? 'just now' : fmtDur(d) + ' ago'; };
const credits = n => n < 10 ? n.toFixed(1) : Math.round(n).toLocaleString();
const money = n => n >= 0.01 ? '$' + n.toFixed(2) : n > 0 ? '$' + n.toFixed(4) : '$0';
const fmtDate = ts => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;


/* ------------------------------------------------------------ appearance */
// Per-user theme. A user picks four colours (or a preset that names four);
// every other shade on the page is derived from them here and written onto
// :root as the same custom properties styles.css defines, so no component
// knows themes exist. Saved server-side per account (PUT /api/me/prefs) so it
// follows the person across devices, and cached in localStorage so the page
// paints in the right colours before /api/me has answered.
const PRESETS = {
  llamacracy: { name: 'Llamacracy', bg: '#0e1117', surface: '#151a23', text: '#dbe0ea', accent: '#8ab4ff' },
  midnight:   { name: 'Midnight',   bg: '#000000', surface: '#0f1013', text: '#e6e6ea', accent: '#9aa8ff' },
  rose:       { name: 'Rosé',       bg: '#1c1216', surface: '#2a1b22', text: '#f4e4eb', accent: '#f59ac0' },
  lavender:   { name: 'Lavender',   bg: '#15122a', surface: '#201c3a', text: '#e7e2f8', accent: '#b69cff' },
  forest:     { name: 'Forest',     bg: '#0e1813', surface: '#15231c', text: '#dcebe2', accent: '#6fd3a0' },
  sunset:     { name: 'Sunset',     bg: '#1d140e', surface: '#2a1d14', text: '#f3e6da', accent: '#ff9f5a' },
  paper:      { name: 'Paper',      bg: '#f6f4ef', surface: '#ffffff', text: '#1f2328', accent: '#2f63d0' },
  blush:      { name: 'Blush',      bg: '#fcf1f5', surface: '#ffffff', text: '#3a2430', accent: '#c93a76' },
};
const CHAT_SIZES = { sm: ['Small', '13.5px'], md: ['Default', '14.5px'], lg: ['Large', '16px'], xl: ['Extra large', '17.5px'] };
const DEFAULT_LOOK = { preset: 'llamacracy', bg: null, surface: null, text: null, accent: null, chat_text: 'md' };
const LOOK_CACHE = 'llamacracy.appearance';

const hexRgb = x => [1, 3, 5].map(i => parseInt(x.slice(i, i + 2), 16));
const rgbHex = a => '#' + a.map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('');
const mix = (a, b, t) => { const p = hexRgb(a), q = hexRgb(b); return rgbHex(p.map((v, i) => v + (q[i] - v) * t)); };
const rgba = (x, a) => `rgba(${hexRgb(x).join(', ')}, ${a})`;
const luminance = x => {
  const [r, g, b] = hexRgb(x).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
// WCAG contrast ratio: 4.5 is the usual floor for body text
const contrast = (a, b) => { const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

// the four colours actually in force for a saved appearance
function lookColors(a) {
  if (a.preset !== 'custom' && PRESETS[a.preset]) return PRESETS[a.preset];
  const d = PRESETS.llamacracy;
  return { bg: a.bg || d.bg, surface: a.surface || d.surface, text: a.text || d.text, accent: a.accent || d.accent };
}
function themeVars({ bg, surface, text, accent }) {
  const light = luminance(bg) > 0.4;
  // status colours tuned for the ground they sit on: the dark set turns to
  // pastel mush on white, and the light set sinks into a dark page
  const st = light
    ? { danger: '#c0392b', warn: '#9a6400', good: '#1d7f47', gold: '#8f6300' }
    : { danger: '#f27878', warn: '#f2c14e', good: '#6fd39a', gold: '#e0a92c' };
  const soft = light ? 0.12 : 0.14;
  return {
    '--ink': bg,
    '--ink-2': light ? mix(bg, '#000000', 0.04) : mix(bg, '#000000', 0.3),
    '--panel': surface,
    '--panel-2': mix(surface, text, 0.06),
    '--panel-3': mix(surface, text, 0.11),
    '--line': mix(surface, text, 0.13),
    '--line-strong': mix(surface, text, 0.22),
    '--text': text,
    '--text-strong': mix(text, light ? '#000000' : '#ffffff', 0.35),
    '--text-2': mix(text, bg, 0.3),
    '--text-3': mix(text, bg, 0.48),
    '--text-4': mix(text, bg, 0.62),
    '--accent': accent,
    '--accent-strong': mix(accent, light ? '#000000' : '#ffffff', 0.15),
    '--accent-ink': contrast(accent, '#ffffff') > contrast(accent, '#0b1220') ? '#ffffff' : '#0b1220',
    '--accent-soft': rgba(accent, soft),
    '--accent-line': rgba(accent, 0.4),
    '--danger': st.danger, '--danger-soft': rgba(st.danger, soft),
    '--warn': st.warn, '--warn-soft': rgba(st.warn, soft),
    '--good': st.good, '--good-soft': rgba(st.good, soft),
    '--gold': st.gold,
    '--code-bg': light ? '#161b22' : mix(bg, '#000000', 0.3),
    '--code-text': light ? '#e6edf3' : text,
    '--shadow': light ? '0 10px 30px rgba(20, 20, 40, .14), 0 1px 0 rgba(255, 255, 255, .6) inset'
                      : '0 10px 30px rgba(0, 0, 0, .45), 0 1px 0 rgba(255, 255, 255, .03) inset',
  };
}
const THEME_KEYS = Object.keys(themeVars(PRESETS.llamacracy));

function applyAppearance(a) {
  a = { ...DEFAULT_LOOK, ...(a || {}) };
  const root = document.documentElement;
  THEME_KEYS.forEach(k => root.style.removeProperty(k));
  const c = lookColors(a);
  // the brand palette is what styles.css already says -- leave it canonical
  if (a.preset !== 'llamacracy') for (const [k, v] of Object.entries(themeVars(c))) root.style.setProperty(k, v);
  root.style.setProperty('--chat-size', (CHAT_SIZES[a.chat_text] || CHAT_SIZES.md)[1]);
  const light = luminance(c.bg) > 0.4;
  root.style.colorScheme = light ? 'light' : 'dark';
  document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', light ? 'light' : 'dark');
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', c.surface);
}
function cachedAppearance() {
  try { return JSON.parse(localStorage.getItem(LOOK_CACHE) || 'null'); } catch { return null; }
}
function cacheAppearance(a) {
  try { localStorage.setItem(LOOK_CACHE, JSON.stringify(a)); } catch { /* private mode etc. */ }
}
applyAppearance(cachedAppearance());   // before the first paint

/* ----------------------------------------------------------------- state */
const S = {
  me: null, models: [], loadedModel: null, conversations: [],
  conv: null, messages: [], active: null, queue: { jobs: [], depth: 0 },
  usage: null, view: 'chat', pickerModel: null, sidebarOpen: false, ctxOpen: false,
  searchOn: false, thinkOn: false, pendingImage: null,
  apiKeys: [], newApiKey: null, apiKeyLabel: '',
  compacting: false,
  draft: '',            // composer text survives re-renders (model switch, stream end, ...)
  stick: true,          // thread is scrolled to the bottom -> keep following new tokens
};

/* ------------------------------------------------------------- SSE: queue */
function connectQueue() {
  const es = new EventSource('/api/queue/events');
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type !== 'queue') return;
    S.queue = d; S.loadedModel = d.loaded_model;
    S.models.forEach(m => m.resident = m.id === d.loaded_model);
    renderTopbar(); renderQueuePanel(); renderHint();
  };
  es.onerror = () => { /* browser auto-reconnects */ };
}

/* --------------------------------------------------------------- rendering */
function render() {
  const main = S.view === 'usage' ? usageView()
    : S.view === 'help' ? helpView()
    : S.view === 'admin' ? adminView() : chatView();
  $app.replaceChildren(
    topbar(),
    h('div', { class: 'app-body' }, S.view === 'admin' ? null : sidebar(), main),
    queuePanel(),
  );
  if (S.view === 'chat') {
    autosize(document.getElementById('composer'));
    if (S.stick) scrollThread(true);
  }
}
// true only on a real mouse/trackpad ("fine" pointer + actual hover) -- false
// on touch, so the composer auto-focus never summons the on-screen keyboard
// on a phone. Only called at moments where re-focusing is actually wanted
// (new chat, opening a conversation, right after hitting send) -- never from
// inside render() itself.
function hasFinePointer() {
  try { return matchMedia('(hover: hover) and (pointer: fine)').matches; } catch { return false; }
}
function focusComposer() {
  if (!hasFinePointer()) return;
  const ta = document.getElementById('composer');
  if (ta) ta.focus();
}
function autosize(ta) {
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + 'px';
}
function setView(v) {
  if (S.view === v) return;
  S.view = v; S.sidebarOpen = false;
  if (v === 'usage') { loadUsage(); loadApiKeys(); }
  if (v === 'admin') loadAdmin();
  render();
}

/* topbar */
function topbar() {
  const nav = [['chat', 'Chat'], ['usage', 'Account'], ['help', 'Help']];
  if (S.me?.is_admin) nav.push(['admin', 'Admin']);
  return h('header', { id: 'topbar', class: 'topbar' },
    h('button', {
      class: 'btn btn-ghost btn-icon menu-btn', 'aria-label': 'Menu', title: 'Conversations',
      onclick: () => { S.sidebarOpen = !S.sidebarOpen; render(); },
    }, icon('menu')),
    h('a', { class: 'brand', href: '#', onclick: e => { e.preventDefault(); setView('chat'); } },
      h('img', { src: '/assets/llamacracy-favicon.svg', alt: '' }),
      h('span', { class: 'wordmark' }, 'Llamacracy')),
    h('span', { id: 'loaded-badge', class: 'loaded-badge' }, loadedBadge()),
    h('div', { class: 'spacer' }),
    h('div', { class: 'gauges' },
      gauge('session', S.usage?.session, 'gauge-full'),
      gauge('week', S.usage?.weekly, 'gauge-full'),
      compactGauge()),
    h('nav', { class: 'nav', 'aria-label': 'Sections' },
      nav.map(([k, l]) => h('button', {
        class: S.view === k ? 'is-active' : '', 'aria-current': S.view === k ? 'page' : null,
        onclick: () => setView(k),
      }, l))),
    h('button', { class: 'who', title: `${S.me?.email || ''} · your account and appearance`, onclick: () => setView('usage') },
      S.me?.display_name || ''),
  );
}
function renderTopbar() {
  const t = document.getElementById('loaded-badge'); if (t) t.replaceChildren(loadedBadge());
  const g = document.querySelector('.gauges');
  if (g) g.replaceChildren(gauge('session', S.usage?.session, 'gauge-full'),
                           gauge('week', S.usage?.weekly, 'gauge-full'), compactGauge());
  const f = document.getElementById('sidebar-foot'); if (f) f.replaceChildren(...sidebarFootKids());
}
function loadedBadge() {
  const busy = S.queue.jobs?.some(j => j.position === 0);
  if (!S.loadedModel) return h('span', { class: 'badge', title: 'No model resident in VRAM' }, h('i', { class: 'dot' }), 'idle');
  return h('span', { class: 'badge is-on' + (busy ? ' is-busy' : ''), title: busy ? 'Generating' : 'Resident in VRAM' },
    h('i', { class: 'dot' }), modelName(S.loadedModel));
}
function gaugeTone(g) {
  return g.uncapped ? 'uncapped' : g.pct >= 90 ? 'danger' : g.pct >= 75 ? 'warn' : '';
}
function gauge(label, g, cls) {
  if (!g) return h('span');
  const title = `${credits(g.used)} / ${credits(g.cap)} credits` +
    (g.uncapped ? ' · uncapped (never blocked)' : g.reset_at ? ` · resets in ${untilStr(g.reset_at)}` : '');
  return h('button', { class: `gauge ${gaugeTone(g)} ${cls}`, title, onclick: () => setView('usage') },
    h('span', { class: 'lbl' }, label),
    h('span', { class: 'bar' }, h('i', { style: `width:${Math.min(100, g.pct)}%` })),
    h('span', { class: 'val' }, Math.round(g.pct) + '%' + (g.uncapped ? ' ∞' : '')));
}
// phones: one pill showing whichever limit is closer
function compactGauge() {
  const s = S.usage?.session, w = S.usage?.weekly;
  if (!s || !w) return h('span');
  const g = s.pct >= w.pct ? s : w;
  return h('button', { class: `gauge gauge-compact ${gaugeTone(g)}`, title: 'Usage', onclick: () => setView('usage') },
    h('span', { class: 'bar' }, h('i', { style: `width:${Math.min(100, g.pct)}%` })),
    h('span', { class: 'val' }, Math.round(g.pct) + '%'));
}

/* sidebar */
function convGroups() {
  const now = Date.now() / 1000, day = 86400;
  const startOfToday = new Date(); startOfToday.setHours(0, 0, 0, 0);
  const t0 = startOfToday.getTime() / 1000;
  const groups = [['Today', []], ['Yesterday', []], ['Previous 7 days', []], ['Older', []]];
  for (const c of S.conversations) {
    const ts = c.updated_at || c.created_at || now;
    const g = ts >= t0 ? 0 : ts >= t0 - day ? 1 : ts >= t0 - 7 * day ? 2 : 3;
    groups[g][1].push(c);
  }
  return groups.filter(([, cs]) => cs.length);
}
function sidebarFootKids() {
  const n = S.queue.depth || 0;
  return [
    h('div', { class: 'row', style: 'gap:8px' }, icon('zap', 'icon-sm'), n ? `${plural(n, 'job')} in queue` : 'Queue is empty',
      h('div', { class: 'spacer' }),
      S.loadedModel ? h('span', { class: 'faint', title: 'Resident model' }, modelName(S.loadedModel)) : null),
    n ? h('div', { class: 'qcard qcard-inline' }, queueRows()) : null,
    sourceLine(),
  ].filter(Boolean);
}
// The AGPL's network clause: everyone who uses the site is offered its source.
// SOURCE_URL points at the code this server actually runs (a fork sets its own).
function sourceLine() {
  const url = S.me?.source_url;
  if (!url) return null;
  return h('div', { class: 'source-line' },
    icon('code', 'icon-sm'),
    h('a', { href: url, target: '_blank', rel: 'noopener' }, 'Source code'),
    h('span', { class: 'faint' }, '· AGPL-3.0'));
}
function sidebar() {
  return [
    S.sidebarOpen ? h('div', { class: 'backdrop', onclick: () => { S.sidebarOpen = false; render(); } }) : null,
    h('aside', { class: 'sidebar' + (S.sidebarOpen ? ' is-open' : ''), 'aria-label': 'Conversations' },
      h('div', { class: 'sidebar-top' },
        h('button', { class: 'btn', onclick: newChat }, icon('plus'), 'New chat')),
      h('div', { class: 'convs' },
        S.conversations.length ? convGroups().map(([label, cs]) => [
          h('div', { class: 'group' }, label),
          cs.map(c => h('div', {
            class: 'conv' + (S.conv?.id === c.id ? ' is-active' : ''),
            role: 'button', tabindex: 0, title: c.title || 'untitled',
            onclick: () => openConv(c.id),
            onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openConv(c.id); } },
          },
            h('span', { class: 'title' }, c.title || 'untitled'),
            h('button', {
              class: 'del', 'aria-label': 'Delete conversation', title: 'Delete',
              onclick: e => { e.stopPropagation(); delConv(c.id); },
            }, icon('trash', 'icon-sm')))),
        ]) : h('div', { class: 'empty-note', style: 'padding:12px 10px' }, 'No conversations yet.')),
      h('div', { id: 'sidebar-foot', class: 'sidebar-foot' }, ...sidebarFootKids()),
    ),
  ];
}

/* chat view */
function chatView() {
  const thread = h('div', {
    id: 'thread', class: 'thread',
    onscroll: e => {
      const t = e.target;
      const stick = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
      if (stick !== S.stick) { S.stick = stick; renderJump(); }
    },
  },
    h('div', { class: 'thread-inner' },
      S.messages.length ? threadItems() : emptyState(),
      S.active ? activeBubble() : null));
  return h('main', { class: 'main' },
    h('div', { class: 'main', style: 'position:relative' }, thread, h('div', { id: 'jump-slot' }, jumpPill())),
    composer(),
  );
}
function emptyState() {
  const m = S.models.find(x => x.id === S.pickerModel);
  return h('div', { class: 'empty' },
    h('img', { src: '/assets/llamacracy-logo.svg', alt: '' }),
    h('h2', {}, 'What are we working on?'),
    h('p', {}, m ? `${m.display} is selected. Pick another model below, or just start typing.` : 'Pick a model below and say something.'),
    h('div', { class: 'hints' },
      h('span', { class: 'hint' }, icon('globe', 'icon-sm'), 'Search adds live web results to a message'),
      h('span', { class: 'hint' }, icon('eye', 'icon-sm'), 'Vision models can read an attached image'),
      h('span', { class: 'hint' }, h('kbd', {}, 'Enter'), 'send', h('kbd', {}, 'Shift+Enter'), 'newline'),
      h('button', { class: 'hint hint-link', onclick: () => setView('help') },
        icon('info', 'icon-sm'), 'New here? Start with Help')));
}
function jumpPill() {
  if (S.stick || !S.messages.length) return null;
  return h('button', { class: 'jump', onclick: () => { S.stick = true; scrollThread(true); renderJump(); } },
    icon('down', 'icon-sm'), 'Jump to latest');
}
function renderJump() { const s = document.getElementById('jump-slot'); if (s) s.replaceChildren(jumpPill() || ''); }

// message bubbles, with a divider dropped in exactly where compaction cut
// history off. Nothing is hidden -- every original message still renders;
// the divider just marks the line the model no longer sees verbatim.
function threadItems() {
  const boundary = S.conv?.compact_boundary_id;
  if (!boundary) return S.messages.map(msgBubble);
  const items = [];
  let shown = false;
  for (const m of S.messages) {
    if (!shown && (m.id == null || m.id > boundary)) { items.push(compactDivider()); shown = true; }
    items.push(msgBubble(m));
  }
  if (!shown) items.push(compactDivider());
  return items;
}
function compactDivider() {
  const summary = S.conv?.context_summary;
  if (!summary) return null;
  return h('div', { class: 'divider' },
    h('details', {},
      h('summary', {}, icon('compress', 'icon-sm'), 'Earlier conversation compacted into a summary', icon('chevron', 'icon-sm')),
      h('div', { class: 'summary-body' }, mdBlock(summary))));
}
function tokenMeta(m) {
  if (m.completion_tokens == null) return '';
  return `${(m.prompt_tokens || 0).toLocaleString()} in · ${m.completion_tokens.toLocaleString()} out` +
    (m.usage_estimated ? ' (est.)' : '');
}
function msgBubble(m) {
  if (m.role === 'user') {
    return h('div', { class: 'msg msg-user' },
      h('div', { class: 'bubble' },
        m.image ? h('img', { class: 'attach', src: m.image.url, alt: 'Attached image' }) : null,
        mdBlock(m.content)),
      searchChip(m.search),
      h('div', { class: 'msg-foot' }, copyBtn(m.content)));
  }
  return h('div', { class: 'msg msg-assistant' },
    h('div', { class: 'head' },
      h('img', { src: '/assets/llamacracy-favicon.svg', alt: '' }),
      h('span', { class: 'name' }, modelName(m.model_id) || 'Assistant'),
      m.created_at ? h('span', { title: new Date(m.created_at * 1000).toLocaleString() },
        new Date(m.created_at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })) : null),
    m.reasoning ? thoughtBlock(m.reasoning, {
      seconds: m.thinking_seconds, open: !!m._thoughtOpen, onToggle: o => { m._thoughtOpen = o; },
    }) : null,
    m.content ? mdBlock(m.content, { class: 'prose-chat body' })
      : h('div', { class: 'body muted small' },
        'It used all its room thinking and never got to the answer. Ask again, or turn Think off for this one.'),
    h('div', { class: 'msg-foot' },
      h('span', { class: 'meta' }, tokenMeta(m)),
      copyBtn(m.content)));
}
// "Thought for 1m 26s": the model's reasoning, folded away under one line
const fmtThought = sec => sec == null ? 'Thought'
  : sec < 1 ? 'Thought for a moment'
  : sec < 60 ? `Thought for ${Math.round(sec)}s`
  : `Thought for ${Math.floor(sec / 60)}m ${String(Math.round(sec % 60)).padStart(2, '0')}s`;
function thoughtBlock(text, { seconds = null, live = false, open = false, onToggle, bodyId } = {}) {
  const label = live
    ? h('span', { id: 'think-label' }, `Thinking… ${Math.max(0, Math.round(seconds || 0))}s`)
    : h('span', {}, fmtThought(seconds));
  const body = live
    ? mdBlock(text, { id: bodyId, class: 'prose-chat thought-body' }, false)
    : mdBlock(text, { class: 'prose-chat thought-body' });
  return h('details', {
    class: 'thought' + (live ? ' is-live' : ''), open,
    ontoggle: e => onToggle && onToggle(e.target.open),
  },
    h('summary', {}, live ? h('i', { class: 'pulse' }) : icon('bulb', 'icon-sm'), label, icon('chevron', 'icon-sm')),
    body);
}
function searchChip(sr) {
  if (!sr) return null;
  if (!sr.ok) return h('div', { class: 'sources' }, icon('globe', 'icon-sm'), ' search unavailable — answered without it');
  if (!sr.results.length) return h('div', { class: 'sources' }, icon('globe', 'icon-sm'), ` no results for "${sr.query}"`);
  return h('details', { class: 'sources' },
    h('summary', {}, icon('globe', 'icon-sm'), `${plural(sr.results.length, 'source')} for "${sr.query}"`, icon('chevron', 'icon-sm')),
    h('ol', {}, sr.results.map(r => h('li', {},
      h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, r.title || r.url)))));
}
function activeBubble() {
  const a = S.active;
  const status = a.state === 'queued' ? (a.position > 0 ? `Queued · position ${a.position}` : 'Queued')
    : a.state === 'loading_model' ? `Loading ${modelName(a.model)}${a.eta ? ` · ~${Math.round(a.eta)}s` : ''}`
    : a.state === 'generating' ? 'Generating' : a.state;
  return h('div', { class: 'msg msg-assistant', id: 'active-msg' },
    h('div', { class: 'head' },
      h('img', { src: '/assets/llamacracy-favicon.svg', alt: '' }),
      h('span', { class: 'name' }, modelName(a.model)),
      h('span', { class: 'status' }, h('i', { class: 'pulse' }), status),
      h('div', { class: 'spacer' }),
      h('button', { class: 'btn btn-ghost btn-sm btn-danger', onclick: cancelActive }, icon('stop', 'icon-sm'), 'Stop')),
    a.reasoning ? thoughtBlock(a.reasoning, {
      live: a.thoughtSeconds == null, seconds: a.thoughtSeconds ?? thinkElapsed(), bodyId: 'think-body',
      open: !!a.thinkOpen, onToggle: o => { a.thinkOpen = o; },
    }) : null,
    mdBlock(a.text, { id: 'active-body', class: 'prose-chat body' }, false));
}
const thinkElapsed = () => S.active?.thinkStart ? (Date.now() - S.active.thinkStart) / 1000 : 0;
let _thinkTick = null;
function startThinkTicker() {
  clearInterval(_thinkTick);
  _thinkTick = setInterval(() => {
    const el = document.getElementById('think-label');
    if (!S.active || S.active.thoughtSeconds != null || !el) { clearInterval(_thinkTick); return; }
    el.textContent = `Thinking… ${Math.round(thinkElapsed())}s`;
  }, 1000);
}
const modelName = id => S.models.find(m => m.id === id)?.display || id;

/* ------------------------------------------------------ context accounting */
// rough token estimate for text we have no exact usage for (~3.6 chars/token,
// a middle ground for English + code). Exact counts come from job usage.
const estTok = s => Math.ceil((s || '').length / 3.6);
// placeholder image cost -- actual vision-token count varies by encoder/tiling
// and isn't benchmarked; once a turn actually returns usage, prompt_tokens
// (exact) takes over from this guess automatically (see contextInfo below).
const IMG_TOK_ESTIMATE = 600;
const fmtTok = n => n >= 10000 ? Math.round(n / 1000) + 'K'
  : n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(Math.round(n));
const fmtCtx = n => Math.round(n / 1024) + 'K';   // model windows are 1024-multiples

// what the NEXT send will cost against the picked model's context window
function contextInfo() {
  const m = S.models.find(x => x.id === S.pickerModel);
  if (!m) return null;
  const thinking = S.thinkOn && m.thinking;
  const limit = m.ctx;
  const reply = thinking ? Math.min(S.me?.limits?.thinking_max_tokens || 8192, Math.floor(limit / 2))
    : (m.max_tokens_default || 2048);
  const boundary = S.conv?.compact_boundary_id || 0;

  // last message with real usage, *after* the compact boundary, = exact cost
  // of everything up to and incl. it. Usage recorded before a compaction is
  // stale -- the prompt it was billed for no longer reflects what gets sent
  // -- so it no longer counts as the exact baseline once a boundary exists.
  let base = 0, exact = false, from = 0, foundExact = false;
  for (let i = S.messages.length - 1; i >= 0; i--) {
    const msg = S.messages[i];
    if (msg.role === 'assistant' && msg.prompt_tokens != null && msg.completion_tokens != null
        && (msg.id == null || msg.id > boundary)) {
      base = msg.prompt_tokens + msg.completion_tokens;
      exact = !msg.usage_estimated;
      // completion_tokens counts the reply's thinking, which is never sent
      // back to the model: take our estimate of it off (and say it's approx)
      if (msg.reasoning) { base = Math.max(msg.prompt_tokens, base - estTok(msg.reasoning)); exact = false; }
      from = i + 1;
      foundExact = true;
      break;
    }
  }
  let tail = 0;
  if (!foundExact && boundary && S.conv?.context_summary) {
    tail += estTok(S.conv.context_summary) + 8;   // the summary itself, once
  }
  for (let i = from; i < S.messages.length; i++) {
    const msg = S.messages[i];
    if (msg.id != null && msg.id <= boundary) continue;   // folded into the summary, not resent
    tail += estTok(msg.content) + 4 + (msg.image || msg.image_upload_id ? IMG_TOK_ESTIMATE : 0);
  }
  const estimated = (!foundExact && S.messages.length > 0) || (!exact && S.messages.length > 0) || tail > 0;

  const draftTok = (S.draft ? estTok(S.draft) + 4 : 0) + (S.pendingImage ? IMG_TOK_ESTIMATE : 0);

  const used = base + tail;
  const projected = used + draftTok;          // the prompt the next send will build
  const keep = S.me?.limits?.compact_keep_recent ?? 6;
  const uncompacted = S.messages.filter(msg => msg.id == null || msg.id > boundary).length;
  return {
    m, limit, reply, thinking, used, draftTok, projected,
    afterReply: projected + reply, exact, estimated,
    pct: projected / limit,
    over: projected >= limit,                  // prompt itself won't fit
    tight: projected < limit && projected + reply > limit,  // reply may get truncated
    compactable: !!S.conv && uncompacted > keep + 1,
  };
}

const RING = { used: 'var(--accent)', draft: 'var(--warn)', reply: 'var(--line-strong)', free: 'var(--panel-3)' };
function contextRing(ci) {
  if (!ci) return h('span');
  const pctNum = Math.round(100 * ci.projected / ci.limit);
  const u = Math.min(100, 100 * ci.used / ci.limit);
  const d = Math.min(100 - u, 100 * ci.draftTok / ci.limit);
  const r = Math.min(Math.max(0, 100 - u - d), 100 * ci.reply / ci.limit);
  const main = ci.over ? 'var(--danger)' : ci.tight ? 'var(--warn)' : RING.used;
  const bg = `conic-gradient(${main} 0 ${u}%, ${RING.draft} ${u}% ${u + d}%, ` +
            `${RING.reply} ${u + d}% ${u + d + r}%, ${RING.free} ${u + d + r}% 100%)`;
  return h('button', {
    id: 'ctx-ring', type: 'button', class: 'ctx-ring' + (S.ctxOpen ? ' is-open' : ''),
    style: `background:${bg}`, 'aria-expanded': S.ctxOpen ? 'true' : 'false',
    'aria-label': 'Context window usage',
    title: `${modelName(ci.m.id)} context: ~${fmtTok(ci.projected)} / ${fmtCtx(ci.limit)}` +
           (ci.estimated ? ' (partly estimated)' : ''),
    onclick: () => { S.ctxOpen = !S.ctxOpen; refreshCtx(0); },
  }, h('span', {}, (pctNum > 999 ? '999' : pctNum) + '%'));
}

function contextBreakdown(ci) {
  if (!ci || !S.ctxOpen) return null;
  const row = (color, label, tok) => h('div', { class: 'ctx-row' },
    h('i', { class: 'sw', style: `background:${color}` }),
    h('span', {}, label),
    h('span', { class: 'n' }, fmtTok(tok)),
    h('span', { class: 'p' }, Math.round(100 * tok / ci.limit) + '%'));
  const free = Math.max(0, ci.limit - ci.projected - ci.reply);
  return h('div', { class: 'ctx-panel' },
    h('div', { class: 'ctx-head' },
      h('span', {}, h('strong', {}, modelName(ci.m.id)), ` · ${fmtTok(ci.limit)} context` +
        (ci.estimated ? ' · ~estimate' : '')),
      h('span', { class: 'mono' }, `${fmtTok(ci.projected)} used`)),
    row(RING.used, 'Conversation', ci.used),
    ci.draftTok ? row(RING.draft, 'Your draft', ci.draftTok) : null,
    row(RING.reply, ci.thinking ? 'Reserved for thinking + reply' : 'Reserved for reply', ci.reply),
    row(RING.free, 'Free', free),
    ci.over ? h('div', { class: 'ctx-note danger' },
      'Over the window — compact the history, trim the chat, start a new one, or pick a bigger-context model.')
      : ci.tight ? h('div', { class: 'ctx-note warn' },
        'Close to full — the reply may be cut short. Compacting frees room, or a bigger-context model has more.')
      : null,
    ci.compactable ? h('div', { class: 'ctx-compact' },
      h('button', {
        type: 'button', disabled: S.compacting, class: 'btn btn-sm',
        onclick: compactConversation,
      }, icon('compress', 'icon-sm'), S.compacting ? 'Compacting…' : 'Compact history'),
      h('div', { class: 'hint' },
        `Summarizes everything except the last ${S.me?.limits?.compact_keep_recent ?? 6} messages ` +
        'using this model. One real inference — billed like any other reply.')) : null);
}

let _ctxTimer = null;
function refreshCtx(delay = 120) {
  clearTimeout(_ctxTimer);
  _ctxTimer = setTimeout(() => {
    const ci = contextInfo();
    const slot = document.getElementById('ctx-slot');
    if (slot) slot.replaceChildren(contextRing(ci));
    const bd = document.getElementById('ctx-breakdown');
    if (bd) bd.replaceChildren(contextBreakdown(ci) || '');
  }, delay);
}
function renderHint() { const el = document.getElementById('compose-hint'); if (el) el.replaceChildren(...composeHintKids()); }

async function onPickImage(e) {
  const file = e.target.files[0]; e.target.value = '';
  if (!file) return;
  const maxMb = S.me?.limits?.upload_max_mb || 8;
  if (file.size > maxMb * 1024 * 1024) { flashError(`Image too large (max ${maxMb} MB)`); return; }
  try {
    const up = await api.upload(file);
    S.pendingImage = { id: up.id, url: URL.createObjectURL(file) };
    render();
  } catch (e2) { flashError(e2.message); }
}

function composeHintKids() {
  const m = S.pickerModel && S.models.find(x => x.id === S.pickerModel);
  if (!m) return [];
  const kids = [
    h('span', { class: m.resident ? 'good' : '', style: m.resident ? 'color:var(--good)' : '' },
      m.resident ? '● loaded' : `cold start ~${fmtDur(m.cold_load_s)}`),
    h('span', { class: 'sep' }, '·'),
    h('span', {}, `~${Math.round(m.tok_s)} tok/s`),
  ];
  if (m.blurb) kids.push(h('span', { class: 'sep blurb' }, '·'), h('span', { class: 'blurb' }, m.blurb));
  kids.push(h('span', { class: 'kbd' }, 'Enter to send · Shift+Enter for a new line'));
  return kids;
}

function composer() {
  const m = S.pickerModel && S.models.find(x => x.id === S.pickerModel);
  const tierLabel = t => t === 'daily' ? 'daily driver' : t;
  return h('div', { class: 'composer' },
    h('div', { class: 'composer-inner' },
      h('div', { id: 'ctx-breakdown' }, contextBreakdown(contextInfo())),
      h('form', { class: 'compose-card', onsubmit: sendMessage },
        S.pendingImage ? h('div', { class: 'attach-preview' },
          h('img', { src: S.pendingImage.url, alt: 'Pending attachment' }),
          h('span', { style: 'flex:1' }, 'Image attached — sent with your next message (routes to the vision variant).'),
          h('button', {
            type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Remove image',
            onclick: () => { S.pendingImage = null; render(); },
          }, icon('x', 'icon-sm'))) : null,
        h('textarea', {
          id: 'composer', rows: 1, placeholder: 'Message Llamacracy…', value: S.draft,
          'aria-label': 'Message',
          oninput: e => { S.draft = e.target.value; autosize(e.target); refreshCtx(); },
          onkeydown: e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(e); } },
        }),
        h('div', { class: 'compose-tools' },
          h('span', { class: 'select-wrap' },
            h('select', {
              class: 'select', 'aria-label': 'Model',
              onchange: e => {
                S.pickerModel = e.target.value;
                const nm = S.models.find(x => x.id === S.pickerModel);
                if (!nm?.vision) S.pendingImage = null;   // no longer attachable on this model
                render();
              },
            }, S.models.filter(md => md.in_picker).map(md => h('option',
              { value: md.id, selected: md.id === S.pickerModel },
              `${md.display}${md.tier ? ` · ${tierLabel(md.tier)}` : ''}${md.vision ? ' · vision' : ''}${md.thinking ? ' · thinks' : ''}`))),
            icon('chevron', 'icon-sm')),
          h('button', {
            type: 'button', title: 'Search the web before answering (one query, injected as context)',
            class: 'btn btn-sm' + (S.searchOn ? ' is-on' : ''), 'aria-pressed': S.searchOn ? 'true' : 'false',
            onclick: () => { S.searchOn = !S.searchOn; render(); },
          }, icon('globe', 'icon-sm'), h('span', { class: 'tool-label' }, 'Search')),
          h('button', {
            type: 'button',
            title: m?.thinking
              ? 'Let the model think before it answers: better at tricky problems, but slower and uses more credits'
              : 'This model can\'t think out loud. Pick one marked "thinks"',
            disabled: !m?.thinking,
            class: 'btn btn-sm' + (S.thinkOn && m?.thinking ? ' is-on' : ''),
            'aria-pressed': S.thinkOn && m?.thinking ? 'true' : 'false',
            onclick: () => { S.thinkOn = !S.thinkOn; render(); },
          }, icon('bulb', 'icon-sm'), h('span', { class: 'tool-label' }, 'Think')),
          h('input', {
            type: 'file', id: 'img-input', hidden: true,
            accept: 'image/png,image/jpeg,image/webp', onchange: onPickImage,
          }),
          h('button', {
            type: 'button',
            title: m?.vision ? 'Attach an image' : 'Switch to a vision model to attach images',
            'aria-label': 'Attach image', disabled: !m?.vision,
            class: 'btn btn-sm btn-icon' + (S.pendingImage ? ' is-on' : ''),
            onclick: () => document.getElementById('img-input').click(),
          }, icon('clip', 'icon-sm')),
          h('div', { class: 'spacer' }),
          h('span', { id: 'ctx-slot' }, contextRing(contextInfo())),
          S.active
            ? h('button', { type: 'button', class: 'btn btn-icon btn-danger', 'aria-label': 'Stop generating', title: 'Stop', onclick: cancelActive }, icon('stop'))
            : h('button', { type: 'submit', class: 'btn btn-primary btn-icon', 'aria-label': 'Send', title: 'Send (Enter)' }, icon('send')))),
      h('div', { id: 'compose-hint', class: 'compose-hint' }, ...composeHintKids()),
      h('div', { class: 'disclaimer' },
        'Llamacracy can make mistakes. Check anything important.')),
  );
}

/* queue panel (bottom-right, collapsible) */
function queuePanel() {
  return h('div', { id: 'qpanel', class: 'qpanel' }, queuePanelInner());
}
function renderQueuePanel() {
  const p = document.getElementById('qpanel'); if (p) p.replaceChildren(queuePanelInner());
  const f = document.getElementById('sidebar-foot'); if (f) f.replaceChildren(...sidebarFootKids());
}
function queueRows() {
  const jobs = S.queue.jobs || [];
  const st = j => j.state === 'generating' ? 'generating' : j.state === 'loading_model' ? 'loading' : j.cold_start ? 'cold' : 'waiting';
  return h('div', { class: 'qlist' },
    jobs.map(j => h('div', { class: 'qrow' + (j.owner === S.me?.display_name ? ' is-mine' : '') },
      h('span', { class: 'pos' + (j.position === 0 ? ' is-active' : '') }, j.position === 0 ? '▶' : j.position),
      h('span', { class: 'qwho' }, `${j.owner} · ${modelName(j.model)}`),
      h('span', { class: 'state' }, st(j)))));
}
// floating card -- phones only (the sidebar footer shows the same list on desktop)
function queuePanelInner() {
  const jobs = S.queue.jobs || [];
  if (!jobs.length) return h('span');
  return h('div', { class: 'qcard', role: 'status', 'aria-live': 'polite' },
    h('div', { class: 'qhead' },
      icon('zap', 'icon-sm'), h('strong', {}, 'Queue'), `· ${jobs.length}`,
      h('div', { class: 'spacer' }),
      S.loadedModel ? h('span', { style: 'color:var(--good)' }, modelName(S.loadedModel)) : null),
    queueRows());
}

/* usage view */
function usageView() {
  const u = S.usage;
  if (!u) return h('main', { class: 'main' }, h('div', { class: 'page muted' }, 'Loading…'));
  return h('main', { class: 'main' }, h('div', { class: 'page page-narrow' },
    h('h1', {}, 'Your account'),
    appearanceCard(),
    h('h2', { class: 'section-h' }, 'Usage'),
    h('div', { class: 'grid grid-2' },
      usageCard('Session', u.session, `${S.me?.limits?.session_window_hours ?? 5}-hour window`),
      usageCard('This week', u.weekly, 'rolling 7 days')),
    h('div', { class: 'card', style: 'margin-top:14px' },
      h('div', { class: 'card-title' }, 'Last 30 days · credits per day' +
        ((u.daily || []).length ? ` · peak ${credits(Math.max(...u.daily.map(r => r.credits)))}` : '')),
      dailyChart(u.daily || [])),
    h('div', { class: 'table-wrap', style: 'margin-top:14px' },
      h('table', {},
        h('thead', {}, h('tr', {},
          ...['Model', 'Requests', 'Credits', 'Tokens out', 'Cost'].map(t => h('th', {}, t)))),
        h('tbody', {}, (u.per_model || []).length ? (u.per_model || []).map(r => h('tr', {},
          h('td', {}, modelName(r.model_id)),
          h('td', { class: 'mono' }, r.requests),
          h('td', { class: 'mono' }, credits(r.credits)),
          h('td', { class: 'mono' }, (r.completion_tokens || 0).toLocaleString()),
          h('td', { class: 'mono' }, money(r.cost_usd))))
          : h('tr', {}, h('td', { class: 'muted', colspan: 5 }, 'No requests yet.'))))),
    u.estimated_fraction > 0.03 ? h('div', { class: 'small', style: 'margin-top:10px;color:var(--warn)' },
      `${(u.estimated_fraction * 100).toFixed(1)}% of recent credits are from estimated token counts.`) : null,
    apiKeysCard(),
  ));
}
// ---- Appearance: presets, four colour pickers, chat text size
const LOOK_FIELDS = [['bg', 'Background'], ['surface', 'Panels'], ['text', 'Text'], ['accent', 'Accent']];
let _lookTimer = null;
function setLook(patch, { save = true, rerender = true } = {}) {
  const a = { ...DEFAULT_LOOK, ...(S.me?.prefs?.appearance || {}), ...patch };
  S.me.prefs = { ...(S.me.prefs || {}), appearance: a };
  applyAppearance(a);
  if (save) {
    cacheAppearance(a);
    clearTimeout(_lookTimer);
    _lookTimer = setTimeout(async () => {
      try { await api.put('/api/me/prefs', S.me.prefs); }
      catch (e) { flashError('Your appearance could not be saved: ' + e.message); }
    }, 400);
  }
  if (rerender) { const el = document.getElementById('look-card'); if (el) el.replaceWith(appearanceCard()); }
}
function appearanceCard() {
  const a = { ...DEFAULT_LOOK, ...(S.me?.prefs?.appearance || {}) };
  const c = lookColors(a);
  const four = { bg: c.bg, surface: c.surface, text: c.text, accent: c.accent };
  const art = p => h('span', { class: 'swatch-art', style: `background:${p.bg}` },
    h('i', { style: `background:${p.surface}` }),
    h('b', { style: `background:${p.text}` }),
    h('u', { style: `background:${p.accent}` }));
  const swatch = (key, p) => h('button', {
    type: 'button', class: 'swatch' + (a.preset === key ? ' is-on' : ''),
    'aria-pressed': a.preset === key ? 'true' : 'false',
    onclick: () => setLook({ preset: key, bg: null, surface: null, text: null, accent: null }),
  }, art(p), h('span', { class: 'swatch-name' }, p.name));
  const picker = ([field, label]) => h('label', { class: 'color-field' },
    h('input', {
      type: 'color', value: c[field], 'aria-label': label,
      // live preview while the picker is open; commit + save once it closes
      oninput: e => setLook({ preset: 'custom', ...four, [field]: e.target.value }, { save: false, rerender: false }),
      onchange: e => setLook({ preset: 'custom', ...four, [field]: e.target.value }),
    }),
    h('span', {}, label));
  const worst = Math.min(contrast(c.text, c.bg), contrast(c.text, c.surface));
  return h('div', { id: 'look-card', class: 'card look' },
    h('div', { class: 'look-head' },
      h('div', {},
        h('h3', {}, 'Appearance'),
        h('p', { class: 'sub' }, 'Make Llamacracy yours. This only changes how it looks for you, and it follows you to every device you sign in on.')),
      a.preset !== 'llamacracy' || a.chat_text !== 'md'
        ? h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => setLook(DEFAULT_LOOK) }, 'Reset') : null),
    h('div', { class: 'look-label' }, 'Themes'),
    h('div', { class: 'swatches' },
      Object.entries(PRESETS).map(([k, p]) => swatch(k, p)),
      a.preset === 'custom' ? h('button', { type: 'button', class: 'swatch is-on', 'aria-pressed': 'true' },
        art(four), h('span', { class: 'swatch-name' }, 'Custom')) : null),
    h('div', { class: 'look-label' }, 'Your own colours'),
    h('div', { class: 'color-fields' }, LOOK_FIELDS.map(picker)),
    worst < 4.5 ? h('div', { class: 'look-warn' }, icon('alert', 'icon-sm'),
      `Text may be hard to read on these colours (contrast ${worst.toFixed(1)}:1, aim for 4.5 or more).`) : null,
    h('div', { class: 'look-label' }, 'Chat text size'),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Chat text size' },
      Object.entries(CHAT_SIZES).map(([k, [label]]) => h('button', {
        type: 'button', class: a.chat_text === k ? 'is-on' : '', 'aria-pressed': a.chat_text === k ? 'true' : 'false',
        onclick: () => setLook({ chat_text: k }),
      }, label))));
}
function usageCard(title, g, sub) {
  const tone = gaugeTone(g);
  return h('div', { class: 'card stat ' + tone },
    h('div', { class: 'head' }, h('span', { class: 'lbl' }, title), h('span', { class: 'sub muted small' }, sub)),
    h('div', { class: 'big' }, credits(g.used), h('small', {}, `/ ${credits(g.cap)} credits`)),
    h('div', { class: 'meter' }, h('i', { style: `width:${Math.min(100, g.pct)}%` })),
    h('div', { class: 'foot' },
      h('span', {}, Math.round(g.pct) + '% used' + (g.uncapped ? ' · uncapped' : '')),
      g.reset_at ? h('span', {}, 'resets in ' + untilStr(g.reset_at)) : null));
}
// One series, one hue. Fills the full 30-day range so gaps read as zero days.
function dailyChart(rows) {
  const day = 86400, today = Math.floor(Date.now() / 1000 / day) * day;
  const byDay = Object.fromEntries(rows.map(r => [r.day, r]));
  const days = Array.from({ length: 30 }, (_, i) => today - (29 - i) * day);
  const max = Math.max(1, ...rows.map(r => r.credits));
  if (!rows.length) return h('div', { class: 'empty-note' }, 'No activity in the last 30 days.');
  const chart = h('div', { class: 'chart' });
  let tip = null;
  const showTip = (bar, d, r) => {
    hideTip();
    tip = h('div', { class: 'tip' }, h('b', {}, credits(r?.credits || 0)), ' credits ',
      h('span', {}, `· ${money(r?.cost_usd || 0)} · ${fmtDate(d)}`));
    chart.append(tip);
    const cb = chart.getBoundingClientRect(), bb = bar.getBoundingClientRect();
    tip.style.left = (bb.left - cb.left + bb.width / 2) + 'px';
    tip.style.top = (bb.bottom - cb.top - (r ? bb.height * Math.max(0.02, r.credits / max) : 2)) + 'px';
  };
  const hideTip = () => { if (tip) { tip.remove(); tip = null; } };
  chart.append(
    h('div', { class: 'plot' }, days.map(d => {
      const r = byDay[d];
      const bar = h('div', {
        class: 'bar' + (r ? '' : ' is-empty'), tabindex: 0,
        'aria-label': `${fmtDate(d)}: ${credits(r?.credits || 0)} credits`,
        onmouseenter: e => showTip(e.currentTarget, d, r), onmouseleave: hideTip,
        onfocus: e => showTip(e.currentTarget, d, r), onblur: hideTip,
      }, h('i', { style: `height:${r ? Math.max(2, 100 * r.credits / max) : 2}%` }));
      return bar;
    })),
    h('div', { class: 'axis' }, h('span', {}, fmtDate(days[0])), h('span', {}, 'today')));
  return chart;
}

/* API keys (Phase 8.2) -- for Continue.dev and other OpenAI-compatible tools.
   Same queue + credits as the web chat; the key is just a different door in. */
function apiKeysCard() {
  const base = window.location.origin + '/v1';
  return h('div', { class: 'card', style: 'margin-top:14px' },
    h('h3', {}, icon('key', 'icon-sm'), ' API access'),
    h('p', { class: 'sub' },
      'OpenAI-compatible endpoint for tools like Continue.dev — same queue and credits as the web chat. Base URL ',
      h('code', { class: 'inline' }, base),
      ', model is any id from the picker (e.g. ',
      h('code', { class: 'inline' }, S.pickerModel || S.models[0]?.id || 'model-id'), ').'),
    S.newApiKey ? h('div', { class: 'keyreveal' },
      h('div', { class: 'small' }, 'Copy this now — it will not be shown again.'),
      h('code', {}, S.newApiKey),
      h('div', { class: 'row' },
        copyBtn(S.newApiKey, 'Copy key'),
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => { S.newApiKey = null; render(); } }, 'Done'))) : null,
    h('div', { class: 'row', style: 'margin-bottom:12px' },
      h('input', {
        type: 'text', class: 'input', style: 'flex:1;min-width:10rem', placeholder: 'Label (e.g. laptop)', value: S.apiKeyLabel,
        'aria-label': 'Key label', oninput: e => { S.apiKeyLabel = e.target.value; },
        onkeydown: e => { if (e.key === 'Enter') genApiKey(); },
      }),
      h('button', { type: 'button', class: 'btn btn-primary', onclick: genApiKey }, icon('plus', 'icon-sm'), 'Generate key')),
    S.apiKeys.length
      ? h('div', { class: 'keylist' }, S.apiKeys.map(k => h('div', { class: 'keyrow' },
          h('div', { class: 'lbl' },
            h('div', {}, k.label || h('span', { class: 'muted' }, '(unlabeled)')),
            h('div', { class: 'small faint' },
              `created ${new Date(k.created_at * 1000).toLocaleDateString()}` +
              (k.last_used_at ? ` · last used ${new Date(k.last_used_at * 1000).toLocaleDateString()}` : ' · never used'))),
          h('button', { type: 'button', class: 'btn btn-ghost btn-sm btn-danger', onclick: () => revokeApiKey(k.id) }, 'Revoke'))))
      : h('div', { class: 'empty-note' }, 'No API keys yet.'));
}
async function loadApiKeys() {
  try { S.apiKeys = (await api.get('/api/keys')).keys; if (S.view === 'usage') render(); } catch { /* ignore */ }
}
async function genApiKey() {
  try {
    const r = await api.post('/api/keys', { label: S.apiKeyLabel });
    S.newApiKey = r.key; S.apiKeyLabel = '';
    await loadApiKeys(); render();
  } catch (e) { flashError(e.message); }
}
async function revokeApiKey(id) {
  if (!await confirmModal('Revoke this API key?', 'Anything using it will start getting 401s immediately.', 'Revoke')) return;
  try {
    await api.del('/api/keys/' + id);
    S.apiKeys = S.apiKeys.filter(k => k.id !== id);
    render();
  } catch (e) { flashError(e.message); }
}

/* ------------------------------------------------------------------- help */
// Written for people who have never used this and shouldn't have to learn the
// word "token". Everything is derived from /api/models and /api/me, so adding
// or removing a model in bench/inventory.json updates this page with it.
const W_PER_TOK = 0.75;          // rough English average
const W_PER_PAGE = 500;          // words on a typical printed page
const pagesFor = ctx => Math.max(1, Math.round(ctx * W_PER_TOK / W_PER_PAGE));
const wordsSec = tok => Math.max(1, Math.round((tok || 0) * W_PER_TOK));
const waitFor = s => s < 3 ? 'instant' : `~${Math.round(s)}s`;
const pickable = () => S.models.filter(m => m.in_picker);

// "I want to ... " -> which model, and why. Computed, never hard-coded.
function modelPicks() {
  const ms = pickable();
  if (!ms.length) return [];
  const top = f => [...ms].sort((a, b) => f(b) - f(a))[0];
  const tier = t => ms.filter(m => m.tier === t);
  const daily = tier('daily')[0] || top(m => m.tok_s);
  const fastest = top(m => m.tok_s);
  const longest = top(m => m.ctx);
  const seers = ms.filter(m => m.vision);
  const rank = { heavy: 3, daily: 2, fast: 1 };
  const bestSeer = seers.length
    ? [...seers].sort((a, b) => (rank[b.tier] || 0) - (rank[a.tier] || 0) || b.ctx - a.ctx)[0]
    : null;
  const heavy = tier('heavy');
  const strongest = heavy.length ? [...heavy].sort((a, b) => b.ctx - a.ctx)[0] : longest;

  const rows = [
    ['Just chatting, or a normal question', daily,
     'The all-rounder. Quick to answer and good at most things.'],
    ['You want the answer right now', fastest,
     `The quickest to reply — about ${wordsSec(fastest.tok_s)} words a second.`],
    ['Pasting in something long', longest,
     `Holds the most at once, roughly ${pagesFor(longest.ctx)} pages of text.`],
  ];
  if (bestSeer) rows.push(['Sharing a photo or a screenshot', bestSeer,
    seers.length > 1
      ? `The best of the ${seers.length} that can see pictures. The others are quicker if you're in a hurry.`
      : 'The one model here that can look at pictures you attach.']);
  rows.push(['Looking something up on the web', daily,
    'Any model can search. This one is fast and has room for the results.']);
  // a thinker for puzzles: the strongest model that can think, else any that can
  const thinkers = ms.filter(m => m.thinking);
  const bestThinker = thinkers.length
    ? [...thinkers].sort((a, b) => (rank[b.tier] || 0) - (rank[a.tier] || 0) || b.ctx - a.ctx)[0] : null;
  if (strongest && strongest.id !== daily.id) rows.push(['A hard or fiddly question', strongest,
    'The strongest one here. Slower to wake up, usually worth the wait.' +
    (strongest.thinking ? ' Turn on Think for the really tricky ones.' : '')]);
  if (bestThinker && bestThinker.id !== strongest?.id) rows.push(['Maths, logic, or a puzzle', bestThinker,
    'Turn on Think and it works the problem through before answering.']);
  return rows;
}

function statusStrip() {
  const n = S.queue.depth || 0;
  return h('div', { class: 'card status-strip' },
    h('div', { class: 'row' },
      h('span', { class: 'badge is-on' }, h('i', { class: 'dot' }), 'Online'),
      S.loadedModel
        ? h('span', {}, h('strong', {}, modelName(S.loadedModel)), ' is warmed up and ready to go')
        : h('span', { class: 'muted' }, 'Nothing loaded right now — your first message wakes a model up'),
      h('div', { class: 'spacer' }),
      h('span', { class: n ? '' : 'muted' },
        n === 0 ? 'Nobody waiting' : n === 1 ? '1 request in the queue' : `${n} requests in the queue`)));
}

function helpView() {
  const ms = pickable();
  const seers = ms.filter(m => m.vision);
  const lim = S.me?.limits || {};
  const sessMin = Math.round((lim.session_credit_limit || 0) / 60);
  const weekHr = ((lim.weekly_credit_limit || 0) / 3600).toFixed(1);

  const tool = (ic, label, what) => h('tr', {},
    h('td', {}, h('span', { class: 'btn-demo' }, icon(ic, 'icon-sm'), label)),
    h('td', {}, what));

  return h('main', { class: 'main' }, h('div', { class: 'page page-narrow help' },
    h('h1', {}, 'How this works'),
    h('p', { class: 'lead' },
      'This is a private AI chat running on one computer at home. Pick a model, ask it ' +
      'something, and the answer types itself out. Everything below is live — it updates ' +
      'when the models change.'),
    statusStrip(),

    h('h2', {}, 'Which model should I use?'),
    h('p', {}, 'Short answer: the one already selected is a good default. If you want to be picky:'),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'I want to…'), h('th', {}, 'Use'), h('th', {}, 'Why'))),
      h('tbody', {}, modelPicks().map(([want, m, why]) => h('tr', {},
        h('td', { class: 'want' }, want),
        h('td', { class: 'pick' }, m.display),
        h('td', { class: 'muted' }, why)))))),

    h('h2', {}, 'All the models, side by side'),
    // data-label lets this collapse into one card per model on a phone, where
    // six columns would otherwise scroll off the right edge unnoticed
    h('div', { class: 'table-wrap model-wrap' }, h('table', { class: 'model-table' },
      h('thead', {}, h('tr', {},
        h('th', {}, 'Model'), h('th', {}, 'Best for'), h('th', {}, 'Photos'), h('th', {}, 'Thinks'),
        h('th', {}, 'Holds about'), h('th', {}, 'Types at'), h('th', {}, 'Wake-up'))),
      h('tbody', {}, ms.map(m => h('tr', {},
        h('td', { 'data-label': 'Model' }, h('span', { class: 'pick' }, m.display),
          m.tier ? h('span', { class: 'tag' }, m.tier) : null),
        h('td', { class: 'muted', 'data-label': 'Best for' }, m.blurb || '—'),
        h('td', { 'data-label': 'Photos' }, m.vision
          ? h('span', { class: 'yes' }, icon('eye', 'icon-sm'), 'Yes')
          : h('span', { class: 'no' }, '—')),
        h('td', { 'data-label': 'Thinks' }, m.thinking
          ? h('span', { class: 'yes' }, icon('bulb', 'icon-sm'), 'Yes')
          : h('span', { class: 'no' }, '—')),
        h('td', { class: 'num', 'data-label': 'Holds about' }, `~${pagesFor(m.ctx)} pages`),
        h('td', { class: 'num', 'data-label': 'Types at' }, `~${wordsSec(m.tok_s)} words/sec`),
        h('td', { class: 'num muted', 'data-label': 'Wake-up' }, waitFor(m.cold_load_s)))))),
    ),
    h('div', { class: 'legend' },
      h('span', {}, h('strong', {}, 'Holds about'), ' — how much text it can keep in mind at once, you and it combined.'),
      h('span', {}, h('strong', {}, 'Wake-up'), ' — the one-off pause when a model has to load. Only if it isn\'t already running.')),

    h('h2', {}, 'Photos and documents'),
    h('p', {},
      seers.length
        ? `${seers.length} of the ${ms.length} models can see pictures — the ones marked Yes ` +
          'in the table above. Pick one of those, then use the paperclip to attach a photo, ' +
          'a screenshot, or a picture of a page, and ask about it.'
        : 'None of the models loaded right now can see pictures.'),
    h('p', {},
      'There is no file upload for documents — paste the text straight into the message box. ' +
      'The only real limit is the "holds about" column above, so for anything long, pick a ' +
      'model near the top of that list. A photo of a document works too, on the models marked Yes.'),
    h('p', { class: 'muted' },
      'One quirk worth knowing: a model only looks at the picture in the message you attached ' +
      'it to. Ask your follow-up questions about it in that same message, or attach it again.'),

    h('h2', {}, 'Letting it think'),
    h('p', {},
      ms.some(m => m.thinking)
        ? 'Some models can think a problem through before they answer: the ones marked Yes under ' +
          'Thinks above. Pick one, turn on Think (the lightbulb by the message box), and you\'ll see ' +
          '"Thinking…" with a timer while it works, then "Thought for 1m 26s" once it starts ' +
          'answering. Tap that line to read what it was thinking.'
        : 'None of the models here can think out loud, so the Think button stays greyed out.'),
    h('p', {},
      'It helps with maths, logic puzzles, planning, code, and questions with a catch. For chatting ' +
      'and everyday questions, leave it off: the answer is just as good and arrives much sooner.'),
    h('div', { class: 'callout' },
      h('strong', {}, 'What it costs. '),
      'Thinking is slower (often a minute or two before the answer starts), and since credits are ' +
      'seconds of the computer working for you, a thinking answer can cost several times as much. ' +
      'It also takes up room: while it answers, its thoughts count towards how much it can hold, ' +
      'which is why the little circle jumps when you switch Think on. Only the answer is kept ' +
      'for the rest of the conversation, not the thinking, so it won\'t fill a chat up faster later.'),
    h('p', { class: 'muted' },
      'Think stays on until you turn it off. If a reply says it used all its room thinking, ask ' +
      'again with Think off, or split the question into smaller pieces.'),

    h('h2', {}, 'The buttons around the message box'),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('tbody', {},
        tool('globe', 'Search', 'Searches the web first and hands the results to the model before it answers. Off unless you turn it on, and it doesn\'t cost you anything.'),
        tool('bulb', 'Think', 'Lets the model work the problem through before answering. Slower and uses more credits, so save it for tricky questions. Greyed out on models that can\'t.'),
        tool('clip', 'Attach', 'Adds a photo. Greyed out unless the model you\'ve picked can see.'),
        tool('compress', 'Compact', 'In a very long chat, folds the older part into a short summary so there\'s room to keep going. Nothing is deleted.'),
        tool('stop', 'Stop', 'Cuts a reply short and hands the computer straight back to whoever\'s next.')))),
    h('p', { class: 'muted' },
      'The little circle next to them fills up as a conversation gets long. When it turns ' +
      'amber or red, start a new chat or hit Compact.'),

    h('h2', {}, 'Make it yours'),
    h('p', {},
      'Open ', h('button', { class: 'link-btn', onclick: () => setView('usage') }, 'Account'),
      ' (at the top, or tap your name) and look under Appearance. Pick one of the themes, or ' +
      'choose your own background, panel, text and accent colours, and make the chat text bigger ' +
      'or smaller. It only changes how Llamacracy looks for you, not for anyone else, and it is ' +
      'saved to your account, so your phone and your laptop match. Reset puts it all back.'),

    h('h2', {}, 'Waiting your turn'),
    h('p', {},
      'There is exactly one computer and it does one thing at a time, in the order people ask. ' +
      'If someone else is mid-answer you\'ll see your place in the queue, and it\'s usually ' +
      'seconds. Switching to a different model means putting the old one away and fetching the ' +
      'new one, which is the wake-up time in the table.'),

    lim.session_credit_limit ? h('div', {},
      h('h2', {}, 'Credits'),
      h('p', {},
        'Usage is counted in seconds of the computer actually writing for you. Reading your ' +
        'message is free, waiting in the queue is free.'),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('tbody', {},
          h('tr', {}, h('td', {}, 'In any ' + (lim.session_window_hours || 5) + '-hour stretch'),
            h('td', { class: 'num' }, `about ${sessMin} minutes of writing`)),
          h('tr', {}, h('td', {}, 'Across a week'),
            h('td', { class: 'num' }, `about ${weekHr} hours of writing`))))),
      h('p', { class: 'muted' },
        'That is far more than it sounds — a long reply is a few seconds. If you ever do run ' +
        'out, a started answer always finishes, you just wait a while before starting another. ' +
        'Your own numbers are on the Account page, and the limits can be raised on request.')) : null,

    h('h2', {}, 'If something looks wrong'),
    h('details', {},
      h('summary', {}, icon('alert', 'icon-sm'), 'The site won\'t load at all'),
      h('p', {}, 'Check the VPN app is connected — this site only exists inside it and is ' +
        'invisible from the normal internet. If it is connected and the page still won\'t ' +
        'load, turn DNS on in the VPN app, or just ask.')),
    h('details', {},
      h('summary', {}, icon('alert', 'icon-sm'), 'It\'s taking ages'),
      h('p', {}, 'Either someone is ahead of you in the queue, or the model you picked is ' +
        'waking up. Both are shown on screen while they happen. Picking a model near the top ' +
        'of the speed column avoids most of it.')),
    h('details', {},
      h('summary', {}, icon('alert', 'icon-sm'), 'It forgot what we were talking about'),
      h('p', {}, 'Each conversation is separate, and very long ones eventually run out of ' +
        'room — that is what the Compact button is for. Starting a fresh chat for a new topic ' +
        'also works well.')),
    h('details', {},
      h('summary', {}, icon('alert', 'icon-sm'), 'It said something wrong'),
      h('p', {}, 'It will, confidently. These are small models running on one home graphics ' +
        'card, not the big commercial ones. Check anything that matters, and use the Search ' +
        'toggle for anything recent or factual.')),

    S.me?.source_url ? h('div', {},
      h('h2', {}, 'About Llamacracy'),
      h('p', {},
        'Llamacracy is free software, released under the ',
        h('a', { href: 'https://www.gnu.org/licenses/agpl-3.0.html', target: '_blank', rel: 'noopener' },
          'GNU Affero General Public License'),
        ', version 3 or later. You are welcome to read, copy and change it. The code this ' +
        'site runs is ',
        h('a', { href: S.me.source_url, target: '_blank', rel: 'noopener' }, 'available here'),
        '.')) : null,
  ));
}

/* ---------------------------------------------------------------- actions */
function scrollThread(force = false) {
  const t = document.getElementById('thread'); if (!t) return;
  if (force || S.stick) t.scrollTop = t.scrollHeight;
}

async function boot() {
  try {
    S.me = await api.get('/api/me');
    const look = { ...DEFAULT_LOOK, ...(S.me.prefs?.appearance || {}) };
    S.me.prefs = { ...(S.me.prefs || {}), appearance: look };
    applyAppearance(look); cacheAppearance(look);
  }
  catch (e) {
    $app.replaceChildren(h('div', { class: 'page' }, h('div', { class: 'card', style: 'max-width:28rem;margin:10vh auto' },
      h('h3', { style: 'color:var(--danger)' }, 'Sign-in problem'),
      h('p', { class: 'sub' }, e.message))));
    return;
  }
  const mods = await api.get('/api/models');
  S.models = mods.models; S.loadedModel = mods.loaded_model;
  const pickable = S.models.filter(m => m.in_picker);
  S.pickerModel = pickable.find(m => m.tier === 'daily')?.id || pickable[0]?.id;
  render();                       // paint the shell as soon as we can
  focusComposer();
  try { S.conversations = (await api.get('/api/conversations')).conversations; } catch {}
  await loadUsage();
  connectQueue();
  render();
  setInterval(() => { if (S.view === 'chat') renderTopbar(); }, 30000);
}
async function loadUsage() {
  try { S.usage = await api.get('/api/usage'); renderTopbar(); if (S.view === 'usage') render(); } catch {}
}

function newChat() {
  S.conv = null; S.messages = []; S.view = 'chat'; S.sidebarOpen = false; S.ctxOpen = false; S.stick = true;
  render(); focusComposer();
}
async function openConv(id) {
  let d;
  try { d = await api.get('/api/conversations/' + id); } catch (e) { flashError(e.message); return; }
  d.messages.forEach(m => {
    if (m.search_json) { try { m.search = JSON.parse(m.search_json); } catch { /* ignore */ } }
    if (m.image_upload_id) m.image = { url: '/api/uploads/' + m.image_upload_id };
  });
  S.conv = d.conversation; S.messages = d.messages; S.view = 'chat'; S.sidebarOpen = false; S.stick = true;
  if (d.conversation.model_id && S.models.some(m => m.id === d.conversation.model_id))
    S.pickerModel = d.conversation.model_id;
  render();
  focusComposer();
}
async function delConv(id) {
  const c = S.conversations.find(x => x.id === id);
  if (!await confirmModal('Delete this conversation?', `"${c?.title || 'untitled'}" and any attached images will be removed. This can't be undone.`, 'Delete')) return;
  try { await api.del('/api/conversations/' + id); } catch (e) { flashError(e.message); return; }
  S.conversations = S.conversations.filter(x => x.id !== id);
  if (S.conv?.id === id) newChat(); else render();
}
async function compactConversation() {
  if (!S.conv || S.compacting) return;
  S.compacting = true; render();
  try {
    const r = await api.post(`/api/conversations/${S.conv.id}/compact`, {});
    // nothing about the individual messages changes (they're never deleted --
    // just excluded from what's sent from here on); only the conversation's
    // own boundary/summary move, so no reload is needed.
    S.conv.compact_boundary_id = r.compact_boundary_id;
    S.conv.context_summary = r.context_summary;
    flashInfo(`Compacted ${plural(r.compacted_count, 'message')} into a summary (${credits(r.credits)} credits).`);
  } catch (e) {
    flashError(e.message);
  } finally {
    S.compacting = false;
    render(); refreshCtx(0);
  }
}

let aborter = null;
async function sendMessage(e) {
  e.preventDefault();
  if (S.active) return;
  const text = S.draft.trim(); if (!text) return;
  S.draft = '';
  const img = S.pendingImage; S.pendingImage = null;
  S.messages.push({ role: 'user', content: text, image: img ? { url: img.url } : undefined });
  S.active = { state: 'queued', position: '?', model: S.pickerModel, text: '' };
  S.stick = true;
  render();
  focusComposer();

  aborter = new AbortController();
  const pm = S.models.find(x => x.id === S.pickerModel);
  const body = { model: S.pickerModel, message: text, search: S.searchOn, think: !!(S.thinkOn && pm?.thinking) };
  if (img) body.image_id = img.id;
  if (S.conv) body.conversation_id = S.conv.id;
  let accepted = false;
  try {
    for await (const ev of api.chatStream(body, aborter.signal)) {
      if (ev.type === 'accepted') {
        accepted = true;
        S.active.jobId = ev.job_id; S.active.position = ev.position;
        if (!S.conv) {
          S.conv = { id: ev.conversation_id, model_id: S.pickerModel, title: text.slice(0, 50), updated_at: Date.now() / 1000 };
          S.conversations.unshift(S.conv);
        } else {
          S.conv.updated_at = Date.now() / 1000;
        }
        render();
      } else if (ev.type === 'search') {
        const u = S.messages[S.messages.length - 1];
        if (u && u.role === 'user') u.search = ev;
        render();
      } else if (ev.type === 'loading') { S.active.state = 'loading_model'; S.active.eta = ev.eta_s; render(); }
      else if (ev.type === 'token') {
        const first = S.active.state !== 'generating';
        S.active.state = 'generating'; S.active.text += ev.text;
        if (first) render(); else { const b = document.getElementById('active-body'); if (b) { mdInto(b, S.active.text, false); scrollThread(); } }
        // (math + highlight are applied on the final render, see the 'done' branch)
      }
      else if (ev.type === 'reasoning') {
        const first = !S.active.reasoning;
        S.active.state = 'generating';
        S.active.reasoning = (S.active.reasoning || '') + ev.text;
        if (first) { S.active.thinkStart = Date.now(); render(); startThinkTicker(); }
        else { const b = document.getElementById('think-body'); if (b) { mdInto(b, S.active.reasoning, false); scrollThread(); } }
      }
      else if (ev.type === 'thought') { S.active.thoughtSeconds = ev.seconds; render(); }
      else if (ev.type === 'done') {
        S.messages.push({
          // ev.model is the model actually dispatched (may be a model's
          // -vision variant if this turn carried an image), not just the
          // picker's selection -- matches what's persisted server-side.
          role: 'assistant', content: S.active.text, model_id: ev.model || S.active.model,
          reasoning: S.active.reasoning || null, thinking_seconds: ev.thinking_seconds,
          prompt_tokens: ev.prompt_tokens, completion_tokens: ev.completion_tokens,
          usage_estimated: ev.usage_estimated, created_at: Date.now() / 1000,
        });
        S.active = null; loadUsage(); render();
      } else if (ev.type === 'error') { flashError(ev.detail || 'Generation error'); S.active = null; render(); }
      else if (ev.type === 'cancelled') { S.active = null; render(); }
    }
  } catch (e2) {
    S.active = null;
    if (e2.name !== 'AbortError') {
      // the send never made it into the queue: hand the text back so it
      // isn't lost, and drop the optimistic bubble
      if (!accepted) {
        S.messages.pop(); S.draft = text; S.pendingImage = img;
      }
      if (e2.status === 429) limitModal(e2.detail);
      else flashError(e2.message);
    }
  }
  S.active = S.active || null; render();
}
async function cancelActive() {
  if (S.active?.jobId) { try { await api.post(`/api/jobs/${S.active.jobId}/cancel`); } catch {} }
  if (aborter) aborter.abort();
  S.active = null; render();
}

/* ------------------------------------------------------------------ admin */
S.admin = { tab: 'live', live: null, users: null, usage: null, impact: null, billing: null, apiKeys: null };
async function loadAdmin() {
  const t = S.admin.tab;
  try {
    if (t === 'live') S.admin.live = await api.get('/api/admin/live');
    if (t === 'users' || t === 'controls') S.admin.users = (await api.get('/api/admin/users')).users;
    if (t === 'usage') S.admin.usage = await api.get('/api/admin/usage');
    if (t === 'impact') S.admin.impact = (await api.get('/api/admin/queue-impact')).impact;
    if (t === 'billing') S.admin.billing = await api.get('/api/admin/billing');
    if (t === 'api-keys') S.admin.apiKeys = (await api.get('/api/admin/api-keys')).keys;
  } catch (e) { flashError(e.message); }
  render();
}
function adminView() {
  const tabs = [['live', 'Live'], ['users', 'Users'], ['usage', 'Usage'], ['impact', 'Queue impact'], ['billing', 'Billing'], ['api-keys', 'API keys'], ['controls', 'Controls']];
  return h('main', { class: 'main' }, h('div', { class: 'page', style: 'padding:0' },
    h('div', { class: 'tabs', role: 'tablist' },
      tabs.map(([k, l]) => h('button', {
        role: 'tab', 'aria-selected': S.admin.tab === k ? 'true' : 'false',
        class: S.admin.tab === k ? 'is-active' : '',
        onclick: () => { S.admin.tab = k; loadAdmin(); },
      }, l))),
    h('div', { class: 'admin-body' }, adminBody())));
}
function adminBody() {
  const a = S.admin;
  if (a.tab === 'live') return adminLive(a.live);
  if (a.tab === 'users') return adminUsers(a.users, false);
  if (a.tab === 'controls') return adminUsers(a.users, true);
  if (a.tab === 'usage') return adminUsage(a.usage);
  if (a.tab === 'impact') return adminImpact(a.impact);
  if (a.tab === 'billing') return adminBilling(a.billing);
  if (a.tab === 'api-keys') return adminApiKeys(a.apiKeys);
}
const loading = () => h('div', { class: 'muted' }, 'Loading…');
function card(title, ...kids) {
  return h('div', { class: 'card' }, h('div', { class: 'card-title' }, title), ...kids);
}
function table(cols, rows) {
  return h('div', { class: 'table-wrap' },
    h('table', {},
      h('thead', {}, h('tr', {},
        cols.map(c => h('th', { class: c.mono ? 'mono' : '' }, c.label)))),
      h('tbody', {}, rows.map(r => h('tr', { class: r._hl ? 'is-hl' : '' },
        cols.map(c => h('td', { class: c.mono ? 'mono' : '' }, c.get(r))))))));
}

function adminLive(d) {
  if (!d) return loading();
  const g = d.gpu;
  return h('div', { class: 'stack' },
    h('div', { class: 'grid grid-4' },
      card('Loaded model', h('div', { class: 'big' }, d.loaded_model ? modelName(d.loaded_model) : h('span', { class: 'muted' }, 'idle'))),
      card('VRAM', g.available ? h('div', { class: 'big' }, `${(g.vram_used_mib / 1024).toFixed(1)} / ${(g.vram_total_mib / 1024).toFixed(1)} GB`) : h('span', { class: 'muted' }, 'n/a')),
      card('GPU temp · power', g.available ? h('div', { class: 'big' }, `${g.temp_c}°C · ${g.power_w}W`) : h('span', { class: 'muted' }, 'n/a')),
      card('GPU util', g.available ? h('div', { class: 'big' }, `${g.util_pct}%`) : h('span', { class: 'muted' }, 'n/a'))),
    card('Queue · ' + d.queue.depth, d.queue.jobs.length ? table(
      [{ label: '#', get: r => r.position, mono: 1 }, { label: 'Owner', get: r => r.owner },
       { label: 'Model', get: r => modelName(r.model) }, { label: 'State', get: r => r.state },
       { label: '', get: r => h('button', { class: 'btn btn-ghost btn-sm btn-danger', onclick: () => adminPost(`/api/admin/jobs/${r.id}/kill`) }, 'Kill') }],
      d.queue.jobs) : h('div', { class: 'empty-note' }, 'Empty')),
    card('Active sessions', d.active_sessions.length ? table(
      [{ label: 'User', get: r => r.email }, { label: 'Credits', get: r => credits(r.credits_used), mono: 1 },
       { label: 'Started', get: r => new Date(r.started_at * 1000).toLocaleTimeString() },
       { label: 'Expires', get: r => 'in ' + untilStr(r.expires_at) }],
      d.active_sessions) : h('div', { class: 'empty-note' }, 'None')),
    h('div', {}, h('button', { class: 'btn btn-warn', onclick: async () => {
      if (await confirmModal('Force-unload the current model?', 'Frees VRAM now. The next request pays a cold start.', 'Unload'))
        adminPost('/api/admin/unload');
    } }, 'Force-unload current model')));
}

function adminUsers(rows, controls) {
  if (!rows) return loading();
  const cols = [
    { label: 'User', get: r => h('span', {}, r.email,
        r.is_admin ? h('span', { class: 'tag gold' }, 'admin') : null,
        r.uncapped ? h('span', { class: 'tag accent' }, 'uncapped') : null,
        r.disabled ? h('span', { class: 'tag' }, 'disabled') : null) },
    { label: 'Session', mono: 1, get: r => r.session.pct + '%' },
    { label: 'Week', mono: 1, get: r => r.weekly.pct + '%' },
    { label: 'All-time tokens', mono: 1, get: r => r.all_time.tokens.toLocaleString() },
    { label: 'All-time cost', mono: 1, get: r => money(r.all_time.cost_usd) },
    { label: 'Last active', get: r => r.last_active_at ? agoStr(r.last_active_at) : '—' },
  ];
  if (controls) cols.push(
    { label: 'Limit overrides', get: r => h('span', { class: 'controls-cell' },
      h('input', { class: 'input', placeholder: 'session', 'aria-label': 'Session override', value: r.session_override ?? '', id: `so-${r.id}` }),
      h('input', { class: 'input', placeholder: 'week', 'aria-label': 'Weekly override', value: r.weekly_override ?? '', id: `wo-${r.id}` }),
      h('button', { class: 'btn btn-sm', onclick: () => saveLimits(r.id) }, 'Set')) },
    { label: 'Uncapped', get: r => h('button', {
      class: 'btn btn-sm' + (r.uncapped ? ' is-on' : ''),
      onclick: () => adminPost(`/api/admin/users/${r.id}/uncapped`, { uncapped: !r.uncapped }),
    }, r.uncapped ? 'On' : 'Off') },
    { label: '', get: r => h('button', {
      class: 'btn btn-ghost btn-sm ' + (r.disabled ? '' : 'btn-danger'),
      onclick: () => adminPost(`/api/admin/users/${r.id}/disabled`, { disabled: !r.disabled }),
    }, r.disabled ? 'Enable' : 'Disable') });
  rows.forEach(r => r._hl = r.session.pct >= 90 || r.weekly.pct >= 90);
  return table(cols, rows);
}
async function saveLimits(id) {
  try {
    await api.post(`/api/admin/users/${id}/limits`, {
      session_override: document.getElementById(`so-${id}`).value || null,
      weekly_override: document.getElementById(`wo-${id}`).value || null,
    });
    flashInfo('Limits saved.');
    loadAdmin();
  } catch (e) { flashError(e.message); }
}

function adminUsage(d) {
  if (!d) return loading();
  const perUser = Object.entries(d.by_day_user.reduce((m, r) => (m[r.email] = (m[r.email] || 0) + r.credits, m), {}))
    .sort((a, b) => b[1] - a[1]);
  return h('div', { class: 'stack' },
    card('Per model · 30 days', table(
      [{ label: 'Model', get: r => modelName(r.model_id) },
       { label: 'Requests', mono: 1, get: r => r.requests },
       { label: 'Occupancy', mono: 1, get: r => fmtDur(r.occupancy_seconds) },
       { label: 'Mean tok/req', mono: 1, get: r => r.mean_tokens },
       { label: 'Mean tok/s', mono: 1, get: r => r.mean_tok_s ?? '—' },
       { label: 'Cold rate', mono: 1, get: r => Math.round(r.cold_rate * 100) + '%' },
       { label: 'Credits', mono: 1, get: r => credits(r.credits) },
       { label: 'Cost', mono: 1, get: r => money(r.cost_usd) }],
      d.by_model)),
    card('Credits by user · 30 days', perUser.length ? table(
      [{ label: 'User', get: r => r[0] }, { label: 'Credits', mono: 1, get: r => credits(r[1]) }], perUser)
      : h('div', { class: 'empty-note' }, 'No activity yet.')));
}

function adminImpact(rows) {
  if (!rows) return loading();
  return h('div', { class: 'stack' },
    h('p', { class: 'sub' }, 'Seconds of wait each person inflicted on others while their jobs held the box (strict-FIFO fairness).'),
    rows.length ? table([{ label: 'User', get: r => r.email },
           { label: 'Wait inflicted', mono: 1, get: r => fmtDur(r.wait_inflicted_s) },
           { label: 'Jobs delayed', mono: 1, get: r => r.jobs_delayed }], rows)
      : h('div', { class: 'empty-note' }, 'Nobody has had to wait on anyone yet.'));
}

function adminBilling(d) {
  if (!d) return loading();
  return h('div', { class: 'stack' },
    h('div', { class: 'row' },
      h('a', { href: '/api/admin/billing/export.csv', class: 'btn' }, 'Export CSV'),
      h('span', { class: 'small muted' }, 'Period: last 30 days')),
    card('Cost per user · this period', table(
      [{ label: 'User', get: r => r.email },
       { label: 'Jobs', mono: 1, get: r => r.jobs },
       { label: 'Credits', mono: 1, get: r => credits(r.credits) },
       { label: 'Cost', mono: 1, get: r => money(r.cost) },
       { label: '', get: r => h('button', { class: 'btn btn-ghost btn-sm', onclick: () => makeInvoice(r.user_id, d.period) }, 'Draft invoice') }],
      d.per_user)),
    card('Invoices', d.invoices.length ? table(
      [{ label: 'User', get: r => r.email },
       { label: 'Period', get: r => new Date(r.period_start * 1000).toLocaleDateString() + ' – ' + new Date(r.period_end * 1000).toLocaleDateString() },
       { label: 'Credits', mono: 1, get: r => credits(r.total_credits) },
       { label: 'Cost', mono: 1, get: r => money(r.total_cost_usd) },
       { label: 'Status', get: r => h('span', { class: 'tag' + (r.status === 'paid' ? ' accent' : ''), style: 'margin:0' }, r.status) },
       { label: '', get: r => h('span', { class: 'row' },
         ['sent', 'paid'].filter(s => s !== r.status).map(s => h('button', { class: 'btn btn-ghost btn-sm', onclick: () => setInvoice(r.id, s) }, 'Mark ' + s))) }],
      d.invoices) : h('div', { class: 'empty-note' }, 'None yet.')));
}
function adminApiKeys(rows) {
  if (!rows) return loading();
  return h('div', { class: 'stack' },
    h('p', { class: 'sub' }, 'Every Continue.dev / OpenAI-compatible key across all users. Revoking kills it immediately — the holder gets 401s and has to generate a new one from their own Account page.'),
    rows.length ? table([
      { label: 'User', get: r => r.email },
      { label: 'Label', get: r => r.label || h('span', { class: 'muted' }, '(unlabeled)') },
      { label: 'Created', get: r => new Date(r.created_at * 1000).toLocaleDateString() },
      { label: 'Last used', get: r => r.last_used_at ? agoStr(r.last_used_at) : 'never' },
      { label: '', get: r => h('button', { class: 'btn btn-ghost btn-sm btn-danger', onclick: () => revokeAnyApiKey(r.id) }, 'Revoke') },
    ], rows) : h('div', { class: 'empty-note' }, 'No API keys issued yet.'));
}
async function revokeAnyApiKey(id) {
  if (!await confirmModal('Revoke this API key?', 'The holder will get 401s immediately.', 'Revoke')) return;
  try { await api.del('/api/admin/api-keys/' + id); flashInfo('Key revoked.'); loadAdmin(); }
  catch (e) { flashError(e.message); }
}

async function makeInvoice(user_id, period) {
  await adminPost('/api/admin/billing/invoice', { user_id, period_start: period.start, period_end: period.end });
}
async function setInvoice(id, status) { await adminPost(`/api/admin/billing/invoice/${id}/status`, { status }); }

async function adminPost(path, body) {
  try { await api.post(path, body || {}); loadAdmin(); }
  catch (e) { flashError(e.message); }
}

/* ---------------------------------------------------------- toasts, modals */
function toastRoot() {
  let r = document.getElementById('toasts');
  if (!r) { r = h('div', { id: 'toasts', class: 'toasts', 'aria-live': 'polite' }); document.body.append(r); }
  return r;
}
function toast(kind, msg) {
  const t = h('div', { class: 'toast ' + kind, role: kind === 'error' ? 'alert' : 'status' },
    icon(kind === 'error' ? 'alert' : 'check', 'icon-sm'), h('span', {}, msg));
  toastRoot().append(t);
  setTimeout(() => t.remove(), kind === 'error' ? 5000 : 3500);
}
const flashError = msg => toast('error', msg);
const flashInfo = msg => toast('info', msg);

function modal(kids, onClose) {
  const overlay = h('div', { class: 'overlay', onclick: e => { if (e.target === overlay) close(); } },
    h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' }, ...kids));
  const onKey = e => { if (e.key === 'Escape') close(); };
  const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); onClose?.(); };
  document.addEventListener('keydown', onKey);
  document.body.append(overlay);
  (overlay.querySelector('[autofocus]') || overlay.querySelector('button'))?.focus();
  return close;
}
function confirmModal(title, text, okLabel = 'OK') {
  return new Promise(resolve => {
    let result = false;
    const close = modal([
      h('h3', {}, title),
      h('p', {}, text),
      h('div', { class: 'actions' },
        h('button', { class: 'btn btn-ghost', autofocus: true, onclick: () => close() }, 'Cancel'),
        h('button', { class: 'btn btn-danger', onclick: () => { result = true; close(); } }, okLabel)),
    ], () => resolve(result));
  });
}
function limitModal(detail) {
  const d = typeof detail === 'object' ? detail : {};
  const close = modal([
    h('h3', { style: 'color:var(--warn)' }, icon('alert'), `${d.limit === 'weekly' ? 'Weekly' : 'Session'} limit reached`),
    h('p', {}, `You've used ${credits(d.used)} of ${credits(d.cap)} credits.`),
    d.reset_at ? h('p', {}, `Resets in ${untilStr(d.reset_at)} (${new Date(d.reset_at * 1000).toLocaleString()}).`) : null,
    h('p', { class: 'small faint' }, 'Your message is still in the composer.'),
    h('div', { class: 'actions' }, h('button', { class: 'btn btn-primary', onclick: () => close() }, 'OK')),
  ]);
}

// global keys: Escape closes the drawer / context panel
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (S.sidebarOpen) { S.sidebarOpen = false; render(); }
  else if (S.ctxOpen) { S.ctxOpen = false; refreshCtx(0); }
});

boot();
