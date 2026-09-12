// Llamacracy SPA -- vanilla JS, no build step. Served by FastAPI at /.
const $app = document.getElementById('app');

/* ------------------------------------------------------------------ api */
const api = {
  async get(p) { const r = await fetch(p); if (!r.ok) throw await err(r); return r.json(); },
  async post(p, b) {
    const r = await fetch(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
    if (!r.ok) throw await err(r); return r.json();
  },
  async del(p) { const r = await fetch(p, { method: 'DELETE' }); if (!r.ok) throw await err(r); return r.json(); },
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
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid.nodeType ? kid : String(kid));
  return e;
};
const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// LaTeX -> KaTeX HTML, done BEFORE marked so markdown doesn't eat the
// backslashes in \(...\) and \[...\]. Each math span is pulled out, rendered,
// and swapped back in after marked via a placeholder markdown ignores.
// Handles $$…$$, \[…\], \(…\) and a guarded $…$ (skips currency-looking text).
const _KA = '', _KB = '';
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
  src = src.replace(/```[\s\S]*?```|`[^`\n]+`/g, m => (code.push(m), `${code.length - 1}`));
  src = src.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => stash(t, true, m));
  src = src.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => stash(t, true, m));
  src = src.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => stash(t, false, m));
  src = src.replace(/(^|[^\\$\d])\$(?!\s)((?:\\.|[^\\$\n])+?)\$(?!\d)/g,
    (m, pre, t) => (/\s$/.test(t) || /^[\s\d.,]*$/.test(t)) ? m : pre + stash(t, false, '$' + t + '$'));
  src = src.replace(/(\d+)/g, (_, i) => code[+i]);
  return { src, spans };
}

if (window.marked) marked.setOptions({ gfm: true, breaks: true });
// Markdown (+ math) -> sanitised HTML. marked + DOMPurify + KaTeX come from the
// CDN (like Tailwind); if they didn't load we fall back to escaped text with
// fenced code blocks so a message is never unreadable.
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
  el.innerHTML = renderMD(src, finalize) || '<span class="text-zinc-600">…</span>';
  if (finalize && window.hljs) {
    el.querySelectorAll('pre code').forEach(b => { try { hljs.highlightElement(b); } catch { /* unknown language */ } });
  }
  el.querySelectorAll('a[href]').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
}
const mdBlock = (src, attrs = {}, finalize = true) => { const el = h('div', { class: 'prose-chat', ...attrs }); mdInto(el, src, finalize); return el; };
const fmtDur = s => s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;
const untilStr = ts => { const d = ts * 1000 - Date.now(); return d <= 0 ? 'now' : fmtDur(d / 1000); };
const credits = n => n < 10 ? n.toFixed(1) : Math.round(n).toLocaleString();
const money = n => n >= 0.01 ? '$' + n.toFixed(2) : n > 0 ? '$' + n.toFixed(4) : '$0';

/* ----------------------------------------------------------------- state */
const S = {
  me: null, models: [], loadedModel: null, conversations: [],
  conv: null, messages: [], active: null, queue: { jobs: [], depth: 0 },
  usage: null, view: 'chat', pickerModel: null, sidebarOpen: false, ctxOpen: false,
  searchOn: false,
};

/* ------------------------------------------------------------- SSE: queue */
function connectQueue() {
  const es = new EventSource('/api/queue/events');
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type !== 'queue') return;
    S.queue = d; S.loadedModel = d.loaded_model;
    renderTopbar(); renderQueuePanel();
    // reflect model residentness in picker
    S.models.forEach(m => m.resident = m.id === d.loaded_model);
  };
  es.onerror = () => { /* browser auto-reconnects */ };
}

/* --------------------------------------------------------------- rendering */
function render() {
  const main = S.view === 'usage' ? usageView() : S.view === 'admin' ? adminView() : chatView();
  $app.replaceChildren(topbar(), h('div', { class: 'flex-1 flex min-h-0' },
    S.view === 'admin' ? null : sidebar(), main,
  ), queuePanel());
  if (S.view === 'chat') { const ta = $app.querySelector('#composer'); if (ta) ta.focus(); scrollThread(); }
}

/* topbar */
function topbar() {
  return h('header', { id: 'topbar', class: 'shrink-0 h-12 border-b border-line flex items-center gap-3 px-3 bg-panel' },
    h('button', { class: 'md:hidden text-zinc-400', onclick: () => { S.sidebarOpen = !S.sidebarOpen; render(); } }, '☰'),
    h('img', { src: '/assets/llamacracy-favicon.svg', alt: '', width: 24, height: 24, class: 'shrink-0' }),
    h('span', { class: 'font-semibold tracking-tight' }, 'Llamacracy'),
    h('span', { id: 'loaded-badge' }, loadedBadge()),
    h('div', { class: 'flex-1' }),
    gauge('session', S.usage?.session), gauge('week', S.usage?.weekly),
    h('button', {
      class: 'text-sm px-2 py-1 rounded ' + (S.view === 'usage' ? 'bg-panel2 text-accent' : 'text-zinc-400 hover:text-zinc-200'),
      onclick: () => { S.view = S.view === 'usage' ? 'chat' : 'usage'; if (S.view === 'usage') loadUsage(); render(); },
    }, 'Usage'),
    S.me?.is_admin ? h('button', {
      class: 'text-sm px-2 py-1 rounded ' + (S.view === 'admin' ? 'bg-panel2 text-accent' : 'text-zinc-400 hover:text-zinc-200'),
      onclick: () => { S.view = S.view === 'admin' ? 'chat' : 'admin'; if (S.view === 'admin') loadAdmin(); render(); },
    }, 'Admin') : null,
    h('span', { class: 'text-sm text-zinc-500 hidden sm:block' }, S.me?.display_name || ''),
  );
}
function renderTopbar() { const t = document.getElementById('loaded-badge'); if (t) t.replaceChildren(loadedBadge()); }
function loadedBadge() {
  const m = S.models.find(x => x.id === S.loadedModel);
  return S.loadedModel
    ? h('span', { class: 'text-xs px-2 py-0.5 rounded-full bg-panel2 border border-line text-good' }, '● ' + (m?.display || S.loadedModel))
    : h('span', { class: 'text-xs px-2 py-0.5 rounded-full bg-panel2 border border-line text-zinc-500' }, '○ idle');
}
function gauge(label, g) {
  if (!g) return h('span');
  const col = g.uncapped ? 'text-accent'
    : g.pct >= 90 ? 'text-danger' : g.pct >= 75 ? 'text-warn' : 'text-zinc-400';
  return h('div', { class: 'text-xs ' + col, title: `${credits(g.used)} / ${credits(g.cap)} credits` +
      (g.uncapped ? ' · uncapped (never blocked)' : g.reset_at ? ` · resets in ${untilStr(g.reset_at)}` : '') },
    h('span', { class: 'hidden sm:inline' }, label + ' '),
    h('span', { class: 'font-mono' }, Math.round(g.pct) + '%' + (g.uncapped ? ' ∞' : '')));
}

/* sidebar */
function sidebar() {
  return h('aside', {
    class: 'w-64 shrink-0 border-r border-line bg-panel flex flex-col ' +
      (S.sidebarOpen ? 'absolute z-20 h-full' : 'hidden') + ' md:flex md:static',
  },
    h('div', { class: 'p-2' },
      h('button', {
        class: 'w-full text-sm rounded bg-panel2 hover:bg-line border border-line py-2',
        onclick: newChat,
      }, '+ New chat')),
    h('div', { class: 'flex-1 overflow-y-auto px-1' },
      S.conversations.map(c => h('div', {
        class: 'group flex items-center rounded px-2 py-1.5 text-sm cursor-pointer ' +
          (S.conv?.id === c.id ? 'bg-panel2 text-zinc-100' : 'text-zinc-400 hover:bg-panel2'),
        onclick: () => openConv(c.id),
      },
        h('span', { class: 'truncate flex-1' }, c.title || 'untitled'),
        h('button', {
          class: 'opacity-0 group-hover:opacity-100 text-zinc-600 hover:text-danger px-1',
          onclick: e => { e.stopPropagation(); delConv(c.id); },
        }, '×')))),
    h('div', { class: 'p-2 text-xs text-zinc-600 border-t border-line' },
      `${S.queue.depth} in queue`),
  );
}

/* chat view */
function chatView() {
  return h('main', { class: 'flex-1 flex flex-col min-w-0' },
    h('div', { id: 'thread', class: 'flex-1 overflow-y-auto px-4 py-4 space-y-4' },
      S.messages.length ? S.messages.map(msgBubble)
        : h('div', { class: 'text-center text-zinc-600 mt-16 text-sm flex flex-col items-center gap-3' },
            h('img', { src: '/assets/llamacracy-logo.svg', alt: 'Llamacracy', width: 128, height: 128, class: 'opacity-90' }),
            h('div', {}, 'Pick a model and say something.')),
      S.active ? activeBubble() : null),
    composer(),
  );
}
function msgBubble(m) {
  const mine = m.role === 'user';
  return h('div', { class: 'flex ' + (mine ? 'justify-end' : 'justify-start') },
    h('div', { class: 'max-w-[46rem] rounded-lg px-3 py-2 text-sm ' +
        (mine ? 'bg-accent/15 border border-accent/30' : 'bg-panel border border-line') },
      mdBlock(m.content),
      mine ? searchChip(m.search) : null,
      m.completion_tokens != null ? h('div', { class: 'mt-1 text-[11px] text-zinc-600' },
        `${m.model_id || ''} · ${m.prompt_tokens || 0}+${m.completion_tokens} tok` +
        (m.usage_estimated ? ' (est)' : '')) : null));
}
function searchChip(sr) {
  if (!sr) return null;
  if (!sr.ok) return h('div', { class: 'mt-1 text-[11px] text-zinc-600' },
    `🔍 search unavailable — answered without it`);
  if (!sr.results.length) return h('div', { class: 'mt-1 text-[11px] text-zinc-600' },
    `🔍 no results for "${sr.query}"`);
  return h('details', { class: 'mt-1 text-[11px] text-zinc-500' },
    h('summary', { class: 'cursor-pointer hover:text-zinc-300 select-none' },
      `🔍 ${sr.results.length} source${sr.results.length > 1 ? 's' : ''} for "${sr.query}"`),
    h('ul', { class: 'mt-1 space-y-0.5 pl-4 list-disc marker:text-zinc-700' },
      sr.results.map(r => h('li', {},
        h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer',
                class: 'text-accent hover:underline' }, r.title || r.url)))));
}
function activeBubble() {
  const a = S.active;
  const status = a.state === 'queued' ? `queued · position ${a.position}`
    : a.state === 'loading_model' ? `loading ${modelName(a.model)}${a.eta ? ` · ~${Math.round(a.eta)}s` : ''}`
    : a.state === 'generating' ? 'generating…' : a.state;
  return h('div', { class: 'flex justify-start' },
    h('div', { class: 'max-w-[46rem] rounded-lg px-3 py-2 text-sm bg-panel border border-line w-full' },
      h('div', { class: 'flex items-center gap-2 text-[11px] text-zinc-500 mb-1' },
        h('span', { class: 'inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse' }),
        status,
        h('div', { class: 'flex-1' }),
        h('button', { class: 'text-zinc-500 hover:text-danger', onclick: cancelActive }, 'cancel')),
      mdBlock(a.text, { id: 'active-body' }, false)));
}
const modelName = id => S.models.find(m => m.id === id)?.display || id;

/* ------------------------------------------------------ context accounting */
// rough token estimate for text we have no exact usage for (~3.6 chars/token,
// a middle ground for English + code). Exact counts come from job usage.
const estTok = s => Math.ceil((s || '').length / 3.6);
const fmtTok = n => n >= 10000 ? Math.round(n / 1000) + 'K'
  : n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(Math.round(n));
const fmtCtx = n => Math.round(n / 1024) + 'K';   // model windows are 1024-multiples

// what the NEXT send will cost against the picked model's context window
function contextInfo() {
  const m = S.models.find(x => x.id === S.pickerModel);
  if (!m) return null;
  const limit = m.ctx, reply = m.max_tokens_default || 2048;

  // last message with real usage = exact cost of everything up to and incl. it;
  // anything after it (a dangling user turn, a model that skipped usage) is estimated
  let base = 0, exact = false, from = 0;
  for (let i = S.messages.length - 1; i >= 0; i--) {
    const msg = S.messages[i];
    if (msg.role === 'assistant' && msg.prompt_tokens != null && msg.completion_tokens != null) {
      base = msg.prompt_tokens + msg.completion_tokens;
      exact = !msg.usage_estimated;
      from = i + 1;
      break;
    }
  }
  let tail = 0;
  for (let i = from; i < S.messages.length; i++) tail += estTok(S.messages[i].content) + 4;
  const estimated = (from === 0 && S.messages.length > 0) || (!exact && S.messages.length > 0) || tail > 0;

  const el = document.getElementById('composer');
  const draftTok = el && el.value ? estTok(el.value) + 4 : 0;

  const used = base + tail;
  const projected = used + draftTok;          // the prompt the next send will build
  return {
    m, limit, reply, used, draftTok, projected,
    afterReply: projected + reply, exact, estimated,
    pct: projected / limit,
    over: projected >= limit,                  // prompt itself won't fit
    tight: projected < limit && projected + reply > limit,  // reply may get truncated
  };
}

function contextRing(ci) {
  if (!ci) return h('span');
  const pctNum = Math.round(100 * ci.projected / ci.limit);
  const u = Math.min(100, 100 * ci.used / ci.limit);
  const d = Math.min(100 - u, 100 * ci.draftTok / ci.limit);
  const r = Math.min(Math.max(0, 100 - u - d), 100 * ci.reply / ci.limit);
  const main = ci.over ? '#ef6f6f' : ci.tight ? '#f2c14e' : '#7c9cff';
  const bg = `conic-gradient(${main} 0 ${u}%, #f2c14e ${u}% ${u + d}%, ` +
            `#3a3f4b ${u + d}% ${u + d + r}%, #23262e ${u + d + r}% 100%)`;
  return h('button', {
    id: 'ctx-ring', type: 'button', class: 'ctx-ring',
    style: `background:${bg}`,
    title: `${modelName(ci.m.id)} context: ~${fmtTok(ci.projected)} / ${fmtCtx(ci.limit)}` +
           (ci.estimated ? ' (partly estimated)' : ''),
    onclick: () => { S.ctxOpen = !S.ctxOpen; refreshCtx(); },
  }, h('span', {}, (pctNum > 999 ? '999' : pctNum) + '%'));
}

function contextBreakdown(ci) {
  if (!ci || !S.ctxOpen) return null;
  const row = (dot, label, tok) => h('div', { class: 'flex items-center gap-2 py-0.5' },
    h('span', { class: 'inline-block w-2 h-2 rounded-sm', style: `background:${dot}` }),
    h('span', { class: 'flex-1' }, label),
    h('span', { class: 'font-mono text-zinc-400' }, fmtTok(tok)),
    h('span', { class: 'font-mono text-zinc-600 w-9 text-right' }, Math.round(100 * tok / ci.limit) + '%'));
  const free = Math.max(0, ci.limit - ci.projected - ci.reply);
  return h('div', { class: 'mb-2 rounded border border-line bg-panel2 p-2 text-[11px] text-zinc-400' },
    h('div', { class: 'flex justify-between mb-1 text-zinc-500' },
      h('span', {}, `${modelName(ci.m.id)} · ${fmtTok(ci.limit)} context` +
        (ci.estimated ? ' · ~estimate' : '')),
      h('span', { class: 'font-mono' }, `${fmtTok(ci.projected)} used`)),
    row('#7c9cff', 'Conversation', ci.used),
    ci.draftTok ? row('#f2c14e', 'Your draft', ci.draftTok) : null,
    row('#3a3f4b', 'Reserved for reply', ci.reply),
    row('#23262e', 'Free', free),
    ci.over ? h('div', { class: 'mt-1 text-danger' },
      'Over the window — trim the chat, start a new one, or pick a bigger-context model.')
      : ci.tight ? h('div', { class: 'mt-1 text-warn' },
        'Close to full — the reply may be cut short. A bigger-context model has more room.')
      : null);
}

let _ctxTimer = null;
function refreshCtx() {
  clearTimeout(_ctxTimer);
  _ctxTimer = setTimeout(() => {
    const ci = contextInfo();
    const slot = document.getElementById('ctx-slot');
    if (slot) slot.replaceChildren(contextRing(ci));
    const bd = document.getElementById('ctx-breakdown');
    if (bd) bd.replaceChildren(contextBreakdown(ci) || '');
  }, 120);
}

function composer() {
  const m = S.pickerModel && S.models.find(x => x.id === S.pickerModel);
  return h('div', { class: 'shrink-0 border-t border-line bg-panel p-3' },
    h('div', { class: 'max-w-[48rem] mx-auto' },
      h('div', { class: 'flex items-center gap-2 mb-2 flex-wrap' },
        h('select', {
          class: 'bg-panel2 border border-line rounded px-2 py-1 text-sm',
          onchange: e => { S.pickerModel = e.target.value; render(); },
        }, S.models.map(md => h('option', { value: md.id, selected: md.id === S.pickerModel || null },
          `${md.display}${md.tier ? `  [${md.tier}]` : ''}`))),
        m ? h('span', { class: 'text-xs text-zinc-500' },
          m.resident ? '● loaded' : `cold start ~${fmtDur(m.cold_load_s)}`,
          ` · ~${Math.round(m.tok_s)} tok/s`) : null,
        h('button', {
          type: 'button', title: 'Search the web before answering (one query, injected as context)',
          class: 'text-xs px-2 py-1 rounded border ' + (S.searchOn
            ? 'bg-accent/20 border-accent/40 text-accent'
            : 'border-line text-zinc-500 hover:text-zinc-300'),
          onclick: () => { S.searchOn = !S.searchOn; render(); },
        }, '🔍 Search' + (S.searchOn ? ': on' : '')),
        h('div', { class: 'flex-1' }),
        h('span', { id: 'ctx-slot' }, contextRing(contextInfo()))),
      m?.blurb ? h('div', { class: 'text-[11px] text-zinc-600 mb-2' }, m.blurb) : null,
      h('div', { id: 'ctx-breakdown' }, contextBreakdown(contextInfo())),
      h('form', { class: 'flex gap-2 items-end', onsubmit: sendMessage },
        h('textarea', {
          id: 'composer', rows: 1, placeholder: 'Message…',
          class: 'flex-1 bg-panel2 border border-line rounded px-3 py-2 text-sm resize-none max-h-40',
          oninput: e => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px'; refreshCtx(); },
          onkeydown: e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(e); } },
        }),
        h('button', {
          class: 'bg-accent/20 border border-accent/40 text-accent rounded px-4 py-2 text-sm disabled:opacity-40',
          disabled: S.active ? 'true' : null,
        }, 'Send'))),
  );
}

/* queue panel (bottom-right, collapsible) */
function queuePanel() {
  return h('div', { id: 'qpanel', class: 'fixed bottom-3 right-3 z-30' }, queuePanelInner());
}
function renderQueuePanel() { const p = document.getElementById('qpanel'); if (p) p.replaceChildren(queuePanelInner()); }
function queuePanelInner() {
  const jobs = S.queue.jobs || [];
  if (!jobs.length) return h('span');
  return h('div', { class: 'w-72 bg-panel border border-line rounded-lg shadow-xl text-xs overflow-hidden' },
    h('div', { class: 'px-3 py-1.5 border-b border-line text-zinc-400 flex' },
      h('span', { class: 'flex-1' }, `Queue · ${jobs.length}`),
      S.loadedModel ? h('span', { class: 'text-good' }, '● ' + modelName(S.loadedModel)) : null),
    h('div', { class: 'max-h-52 overflow-y-auto' },
      jobs.map(j => h('div', {
        class: 'px-3 py-1.5 flex items-center gap-2 border-b border-line/50 ' +
          (j.owner === S.me?.display_name ? 'bg-accent/5' : ''),
      },
        h('span', { class: 'font-mono ' + (j.position === 0 ? 'text-accent' : 'text-zinc-600') },
          j.position === 0 ? '▶' : j.position),
        h('span', { class: 'flex-1 truncate' }, `${j.owner} · ${modelName(j.model)}`),
        h('span', { class: 'text-zinc-500' }, j.state === 'generating' ? 'gen' : j.state === 'loading_model' ? 'load' : j.cold_start ? 'cold' : 'wait')))));
}

/* usage view */
function usageView() {
  const u = S.usage;
  if (!u) return h('main', { class: 'flex-1 p-8 text-zinc-600' }, 'loading…');
  const bars = u.daily || [];
  const max = Math.max(1, ...bars.map(b => b.credits));
  return h('main', { class: 'flex-1 overflow-y-auto p-6 max-w-3xl' },
    h('h1', { class: 'text-lg font-semibold mb-4' }, 'Your usage'),
    h('div', { class: 'grid sm:grid-cols-2 gap-3 mb-6' },
      usageCard('Session', u.session, '5-hour window'),
      usageCard('This week', u.weekly, 'rolling 7 days')),
    h('div', { class: 'bg-panel border border-line rounded-lg p-4 mb-6' },
      h('div', { class: 'text-sm text-zinc-400 mb-2' }, 'Last 30 days (credits/day)'),
      h('div', { class: 'flex items-end gap-1 h-28' },
        bars.length ? bars.map(b => h('div', {
          class: 'flex-1 max-w-[24px] bg-accent/40 rounded-t', style: `height:${Math.max(2, 100 * b.credits / max)}%`,
          title: `${credits(b.credits)} credits · ${money(b.cost_usd)}`,
        })) : h('div', { class: 'text-xs text-zinc-600 self-center' }, 'no activity yet'))),
    h('div', { class: 'bg-panel border border-line rounded-lg overflow-hidden' },
      h('table', { class: 'w-full text-sm' },
        h('thead', { class: 'text-zinc-500 text-xs' }, h('tr', {},
          ...['Model', 'Requests', 'Credits', 'Tokens', 'Cost'].map(t =>
            h('th', { class: 'text-left font-normal px-3 py-2' }, t)))),
        h('tbody', {}, (u.per_model || []).map(r => h('tr', { class: 'border-t border-line' },
          h('td', { class: 'px-3 py-2' }, modelName(r.model_id)),
          h('td', { class: 'px-3 py-2' }, r.requests),
          h('td', { class: 'px-3 py-2 font-mono' }, credits(r.credits)),
          h('td', { class: 'px-3 py-2' }, (r.completion_tokens || 0).toLocaleString()),
          h('td', { class: 'px-3 py-2' }, money(r.cost_usd))))))),
    u.estimated_fraction > 0.03 ? h('div', { class: 'mt-3 text-xs text-warn' },
      `${(u.estimated_fraction * 100).toFixed(1)}% of recent credits are from estimated token counts.`) : null,
  );
}
function usageCard(title, g, sub) {
  const col = g.pct >= 90 ? 'bg-danger' : g.pct >= 75 ? 'bg-warn' : 'bg-accent';
  return h('div', { class: 'bg-panel border border-line rounded-lg p-4' },
    h('div', { class: 'flex justify-between text-sm' }, h('span', {}, title),
      h('span', { class: 'text-zinc-500 text-xs' }, sub)),
    h('div', { class: 'text-2xl font-mono mt-1' }, credits(g.used),
      h('span', { class: 'text-sm text-zinc-600' }, ' / ' + credits(g.cap))),
    h('div', { class: 'h-1.5 bg-panel2 rounded mt-2 overflow-hidden' },
      h('div', { class: col + ' h-full', style: `width:${Math.min(100, g.pct)}%` })),
    g.reset_at ? h('div', { class: 'text-xs text-zinc-600 mt-1' }, 'resets in ' + untilStr(g.reset_at)) : null);
}

/* ---------------------------------------------------------------- actions */
function scrollThread() { const t = document.getElementById('thread'); if (t) t.scrollTop = t.scrollHeight; }

async function boot() {
  try { S.me = await api.get('/api/me'); }
  catch (e) { $app.replaceChildren(h('div', { class: 'p-8 text-danger' }, 'Auth error: ' + e.message)); return; }
  const mods = await api.get('/api/models');
  S.models = mods.models; S.loadedModel = mods.loaded_model;
  S.pickerModel = S.models.find(m => m.tier === 'daily')?.id || S.models[0]?.id;
  render();                       // paint the shell as soon as we can
  try { S.conversations = (await api.get('/api/conversations')).conversations; } catch {}
  await loadUsage();
  connectQueue();
  render();
  setInterval(() => { if (S.view === 'chat') renderTopbar(); }, 30000);
}
async function loadUsage() {
  try { S.usage = await api.get('/api/usage'); renderTopbar(); if (S.view === 'usage') render(); } catch {}
}

function newChat() { S.conv = null; S.messages = []; S.view = 'chat'; S.sidebarOpen = false; render(); }
async function openConv(id) {
  const d = await api.get('/api/conversations/' + id);
  d.messages.forEach(m => { if (m.search_json) { try { m.search = JSON.parse(m.search_json); } catch { /* ignore */ } } });
  S.conv = d.conversation; S.messages = d.messages; S.view = 'chat'; S.sidebarOpen = false;
  if (d.conversation.model_id && S.models.some(m => m.id === d.conversation.model_id))
    S.pickerModel = d.conversation.model_id;
  render();
}
async function delConv(id) {
  await api.del('/api/conversations/' + id);
  S.conversations = S.conversations.filter(c => c.id !== id);
  if (S.conv?.id === id) newChat(); else render();
}

let aborter = null;
async function sendMessage(e) {
  e.preventDefault();
  if (S.active) return;
  const ta = document.getElementById('composer');
  const text = ta.value.trim(); if (!text) return;
  ta.value = ''; ta.style.height = 'auto';
  S.messages.push({ role: 'user', content: text });
  S.active = { state: 'queued', position: '?', model: S.pickerModel, text: '' };
  render();

  aborter = new AbortController();
  const body = { model: S.pickerModel, message: text, search: S.searchOn };
  if (S.conv) body.conversation_id = S.conv.id;
  try {
    for await (const ev of api.chatStream(body, aborter.signal)) {
      if (ev.type === 'accepted') {
        S.active.jobId = ev.job_id; S.active.position = ev.position;
        if (!S.conv) { S.conv = { id: ev.conversation_id, model_id: S.pickerModel, title: text.slice(0, 50) }; S.conversations.unshift(S.conv); }
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
      else if (ev.type === 'reasoning') { /* thinking hidden in v1 */ }
      else if (ev.type === 'done') {
        S.messages.push({
          role: 'assistant', content: S.active.text, model_id: S.active.model,
          prompt_tokens: ev.prompt_tokens, completion_tokens: ev.completion_tokens,
          usage_estimated: ev.usage_estimated,
        });
        S.active = null; loadUsage(); render();
      } else if (ev.type === 'error') { flashError(ev.detail || 'generation error'); S.active = null; render(); }
      else if (ev.type === 'cancelled') { S.active = null; render(); }
    }
  } catch (e2) {
    S.active = null;
    if (e2.status === 429) limitModal(e2.detail);
    else flashError(e2.message);
  }
  S.active = S.active || null; render();
}
async function cancelActive() {
  if (S.active?.jobId) { try { await api.post(`/api/jobs/${S.active.jobId}/cancel`); } catch {} }
  if (aborter) aborter.abort();
  S.active = null; render();
}

/* ------------------------------------------------------------------ admin */
S.admin = { tab: 'live', live: null, users: null, usage: null, impact: null, billing: null };
async function loadAdmin() {
  const t = S.admin.tab;
  try {
    if (t === 'live') S.admin.live = await api.get('/api/admin/live');
    if (t === 'users' || t === 'controls') S.admin.users = (await api.get('/api/admin/users')).users;
    if (t === 'usage') S.admin.usage = await api.get('/api/admin/usage');
    if (t === 'impact') S.admin.impact = (await api.get('/api/admin/queue-impact')).impact;
    if (t === 'billing') S.admin.billing = await api.get('/api/admin/billing');
  } catch (e) { flashError(e.message); }
  render();
}
function adminView() {
  const tabs = [['live', 'Live'], ['users', 'Users'], ['usage', 'Usage'], ['impact', 'Queue impact'], ['billing', 'Billing'], ['controls', 'Controls']];
  return h('main', { class: 'flex-1 overflow-y-auto' },
    h('div', { class: 'flex gap-1 border-b border-line px-4 sticky top-0 bg-ink z-10' },
      tabs.map(([k, l]) => h('button', {
        class: 'px-3 py-2 text-sm border-b-2 ' + (S.admin.tab === k ? 'border-accent text-accent' : 'border-transparent text-zinc-400'),
        onclick: () => { S.admin.tab = k; loadAdmin(); },
      }, l))),
    h('div', { class: 'p-4' }, adminBody()));
}
function adminBody() {
  const a = S.admin;
  if (a.tab === 'live') return adminLive(a.live);
  if (a.tab === 'users') return adminUsers(a.users, false);
  if (a.tab === 'controls') return adminUsers(a.users, true);
  if (a.tab === 'usage') return adminUsage(a.usage);
  if (a.tab === 'impact') return adminImpact(a.impact);
  if (a.tab === 'billing') return adminBilling(a.billing);
}
function card(title, ...kids) {
  return h('div', { class: 'bg-panel border border-line rounded-lg p-4' },
    h('div', { class: 'text-xs text-zinc-500 mb-2' }, title), ...kids);
}
function table(cols, rows) {
  return h('div', { class: 'bg-panel border border-line rounded-lg overflow-x-auto' },
    h('table', { class: 'w-full text-sm' },
      h('thead', { class: 'text-zinc-500 text-xs' }, h('tr', {},
        cols.map(c => h('th', { class: 'text-left font-normal px-3 py-2' }, c.label)))),
      h('tbody', {}, rows.map(r => h('tr', { class: 'border-t border-line ' + (r._hl ? 'bg-danger/10' : '') },
        cols.map(c => h('td', { class: 'px-3 py-2 ' + (c.mono ? 'font-mono' : '') }, c.get(r))))))));
}

function adminLive(d) {
  if (!d) return 'loading…';
  const g = d.gpu;
  return h('div', { class: 'space-y-4' },
    h('div', { class: 'grid sm:grid-cols-4 gap-3' },
      card('Loaded model', h('div', { class: 'text-lg' }, d.loaded_model || 'idle')),
      card('VRAM', g.available ? h('div', { class: 'text-lg font-mono' }, `${(g.vram_used_mib / 1024).toFixed(1)} / ${(g.vram_total_mib / 1024).toFixed(1)} GB`) : 'n/a'),
      card('GPU temp / power', g.available ? h('div', { class: 'text-lg font-mono' }, `${g.temp_c}°C · ${g.power_w}W`) : 'n/a'),
      card('GPU util', g.available ? h('div', { class: 'text-lg font-mono' }, `${g.util_pct}%`) : 'n/a')),
    card('Queue (' + d.queue.depth + ')', d.queue.jobs.length ? table(
      [{ label: '#', get: r => r.position, mono: 1 }, { label: 'Owner', get: r => r.owner },
       { label: 'Model', get: r => modelName(r.model) }, { label: 'State', get: r => r.state },
       { label: '', get: r => h('button', { class: 'text-danger text-xs', onclick: () => adminPost(`/api/admin/jobs/${r.id}/kill`) }, 'kill') }],
      d.queue.jobs) : h('div', { class: 'text-zinc-600 text-sm' }, 'empty')),
    card('Active sessions', table(
      [{ label: 'User', get: r => r.email }, { label: 'Credits', get: r => credits(r.credits_used), mono: 1 },
       { label: 'Started', get: r => new Date(r.started_at * 1000).toLocaleTimeString() },
       { label: 'Expires', get: r => untilStr(r.expires_at) }],
      d.active_sessions)),
    h('button', { class: 'text-sm bg-panel2 border border-line rounded px-3 py-1.5 text-warn', onclick: () => adminPost('/api/admin/unload') }, 'Force-unload current model'));
}

function adminUsers(rows, controls) {
  if (!rows) return 'loading…';
  const cols = [
    { label: 'User', get: r => r.email + (r.is_admin ? ' ★' : '') + (r.uncapped ? ' ∞' : '') },
    { label: 'Session %', mono: 1, get: r => r.session.pct + '%' },
    { label: 'Week %', mono: 1, get: r => r.weekly.pct + '%' },
    { label: 'All-time tok', mono: 1, get: r => r.all_time.tokens.toLocaleString() },
    { label: 'All-time $', mono: 1, get: r => money(r.all_time.cost_usd) },
    { label: 'Last active', get: r => r.last_active_at ? untilStr(r.last_active_at) + ' ago' : '—' },
  ];
  if (controls) cols.push(
    { label: 'Overrides', get: r => h('span', {},
      h('input', { class: 'w-20 bg-panel2 border border-line rounded px-1 text-xs', placeholder: 'sess', value: r.session_override ?? '', id: `so-${r.id}` }),
      h('input', { class: 'w-20 bg-panel2 border border-line rounded px-1 text-xs ml-1', placeholder: 'week', value: r.weekly_override ?? '', id: `wo-${r.id}` }),
      h('button', { class: 'text-accent text-xs ml-1', onclick: () => saveLimits(r.id) }, 'set')) },
    { label: 'Uncapped', get: r => h('button', {
      class: 'text-xs ' + (r.uncapped ? 'text-accent' : 'text-zinc-500'),
      onclick: () => adminPost(`/api/admin/users/${r.id}/uncapped`, { uncapped: !r.uncapped }),
    }, r.uncapped ? '∞ on' : 'off') },
    { label: '', get: r => h('button', {
      class: 'text-xs ' + (r.disabled ? 'text-good' : 'text-danger'),
      onclick: () => adminPost(`/api/admin/users/${r.id}/disabled`, { disabled: !r.disabled }),
    }, r.disabled ? 'enable' : 'disable') });
  rows.forEach(r => r._hl = r.session.pct >= 90 || r.weekly.pct >= 90);
  return table(cols, rows);
}
async function saveLimits(id) {
  try {
    await api.post(`/api/admin/users/${id}/limits`, {
      session_override: document.getElementById(`so-${id}`).value || null,
      weekly_override: document.getElementById(`wo-${id}`).value || null,
    });
    loadAdmin();
  } catch (e) { flashError(e.message); }
}

function adminUsage(d) {
  if (!d) return 'loading…';
  return h('div', { class: 'space-y-4' },
    card('Per-model (30 days)', table(
      [{ label: 'Model', get: r => modelName(r.model_id) },
       { label: 'Requests', mono: 1, get: r => r.requests },
       { label: 'Occupancy', mono: 1, get: r => fmtDur(r.occupancy_seconds) },
       { label: 'Mean tok/req', mono: 1, get: r => r.mean_tokens },
       { label: 'Mean tok/s', mono: 1, get: r => r.mean_tok_s ?? '—' },
       { label: 'Cold rate', mono: 1, get: r => Math.round(r.cold_rate * 100) + '%' },
       { label: 'Credits', mono: 1, get: r => credits(r.credits) },
       { label: 'Cost', mono: 1, get: r => money(r.cost_usd) }],
      d.by_model)),
    card('Credits by user × day', h('div', { class: 'text-xs text-zinc-500' },
      d.by_day_user.length + ' data points · ' +
      Object.entries(d.by_day_user.reduce((m, r) => (m[r.email] = (m[r.email] || 0) + r.credits, m), {}))
        .map(([e, c]) => `${e}: ${credits(c)}`).join('  ·  '))));
}

function adminImpact(rows) {
  if (!rows) return 'loading…';
  return h('div', {},
    h('p', { class: 'text-xs text-zinc-500 mb-3' }, 'Seconds of wait each person inflicted on others while their jobs held the box (strict-FIFO fairness).'),
    table([{ label: 'User', get: r => r.email },
           { label: 'Wait inflicted', mono: 1, get: r => fmtDur(r.wait_inflicted_s) },
           { label: 'Jobs delayed', mono: 1, get: r => r.jobs_delayed }], rows));
}

function adminBilling(d) {
  if (!d) return 'loading…';
  return h('div', { class: 'space-y-4' },
    h('div', { class: 'flex items-center gap-2' },
      h('a', { href: '/api/admin/billing/export.csv', class: 'text-sm bg-panel2 border border-line rounded px-3 py-1.5' }, 'Export CSV'),
      h('span', { class: 'text-xs text-zinc-500' }, `period: last 30 days`)),
    card('Cost per user (this period)', table(
      [{ label: 'User', get: r => r.email },
       { label: 'Jobs', mono: 1, get: r => r.jobs },
       { label: 'Credits', mono: 1, get: r => credits(r.credits) },
       { label: 'Cost', mono: 1, get: r => money(r.cost) },
       { label: '', get: r => h('button', { class: 'text-accent text-xs', onclick: () => makeInvoice(r.user_id, d.period) }, 'draft invoice') }],
      d.per_user)),
    card('Invoices', d.invoices.length ? table(
      [{ label: 'User', get: r => r.email },
       { label: 'Period', get: r => new Date(r.period_start * 1000).toLocaleDateString() + '–' + new Date(r.period_end * 1000).toLocaleDateString() },
       { label: 'Credits', mono: 1, get: r => credits(r.total_credits) },
       { label: 'Cost', mono: 1, get: r => money(r.total_cost_usd) },
       { label: 'Status', get: r => r.status },
       { label: '', get: r => h('span', {},
         ['sent', 'paid'].map(s => h('button', { class: 'text-xs text-accent mr-2', onclick: () => setInvoice(r.id, s) }, 'mark ' + s))) }],
      d.invoices) : h('div', { class: 'text-zinc-600 text-sm' }, 'none yet')));
}
async function makeInvoice(user_id, period) {
  await adminPost('/api/admin/billing/invoice', { user_id, period_start: period.start, period_end: period.end });
  loadAdmin();
}
async function setInvoice(id, status) { await adminPost(`/api/admin/billing/invoice/${id}/status`, { status }); loadAdmin(); }

async function adminPost(path, body) {
  try { await api.post(path, body || {}); loadAdmin(); }
  catch (e) { flashError(e.message); }
}

function flashError(msg) {
  const t = h('div', { class: 'fixed top-14 left-1/2 -translate-x-1/2 z-50 bg-danger/20 border border-danger/50 text-danger text-sm px-3 py-2 rounded' }, msg);
  document.body.append(t); setTimeout(() => t.remove(), 4000);
}
function limitModal(detail) {
  const d = typeof detail === 'object' ? detail : {};
  const overlay = h('div', { class: 'fixed inset-0 z-50 bg-black/60 flex items-center justify-center', onclick: e => { if (e.target === overlay) overlay.remove(); } },
    h('div', { class: 'bg-panel border border-line rounded-lg p-5 max-w-sm text-sm' },
      h('div', { class: 'text-warn font-semibold mb-2' }, `${d.limit === 'weekly' ? 'Weekly' : 'Session'} limit reached`),
      h('p', { class: 'text-zinc-400' }, `You've used ${credits(d.used)} of ${credits(d.cap)} credits.`),
      d.reset_at ? h('p', { class: 'text-zinc-400 mt-1' }, `Resets in ${untilStr(d.reset_at)} (${new Date(d.reset_at * 1000).toLocaleString()}).`) : null,
      h('button', { class: 'mt-3 text-accent', onclick: () => overlay.remove() }, 'OK')));
  document.body.append(overlay);
}

boot();
