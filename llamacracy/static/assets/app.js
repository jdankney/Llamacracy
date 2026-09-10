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
function mdLite(s) {
  s = esc(s);
  s = s.replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, c) => `<pre><code>${c.replace(/\n$/, '')}</code></pre>`);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  return s.split(/\n{2,}/).map(p => p.startsWith('<pre>') ? p : `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}
const fmtDur = s => s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;
const untilStr = ts => { const d = ts * 1000 - Date.now(); return d <= 0 ? 'now' : fmtDur(d / 1000); };
const credits = n => n < 10 ? n.toFixed(1) : Math.round(n).toLocaleString();
const money = n => n >= 0.01 ? '$' + n.toFixed(2) : n > 0 ? '$' + n.toFixed(4) : '$0';

/* ----------------------------------------------------------------- state */
const S = {
  me: null, models: [], loadedModel: null, conversations: [],
  conv: null, messages: [], active: null, queue: { jobs: [], depth: 0 },
  usage: null, view: 'chat', pickerModel: null, sidebarOpen: false,
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
  $app.replaceChildren(topbar(), h('div', { class: 'flex-1 flex min-h-0' },
    sidebar(),
    S.view === 'usage' ? usageView() : chatView(),
  ), queuePanel());
  if (S.view === 'chat') { const ta = $app.querySelector('#composer'); if (ta) ta.focus(); scrollThread(); }
}

/* topbar */
function topbar() {
  return h('header', { id: 'topbar', class: 'shrink-0 h-12 border-b border-line flex items-center gap-3 px-3 bg-panel' },
    h('button', { class: 'md:hidden text-zinc-400', onclick: () => { S.sidebarOpen = !S.sidebarOpen; render(); } }, '☰'),
    h('span', { class: 'font-semibold tracking-tight' }, 'Llamacracy'),
    h('span', { id: 'loaded-badge' }, loadedBadge()),
    h('div', { class: 'flex-1' }),
    gauge('session', S.usage?.session), gauge('week', S.usage?.weekly),
    h('button', {
      class: 'text-sm px-2 py-1 rounded ' + (S.view === 'usage' ? 'bg-panel2 text-accent' : 'text-zinc-400 hover:text-zinc-200'),
      onclick: () => { S.view = S.view === 'usage' ? 'chat' : 'usage'; if (S.view === 'usage') loadUsage(); render(); },
    }, 'Usage'),
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
  const col = g.pct >= 90 ? 'text-danger' : g.pct >= 75 ? 'text-warn' : 'text-zinc-400';
  return h('div', { class: 'text-xs ' + col, title: `${credits(g.used)} / ${credits(g.cap)} credits` +
      (g.reset_at ? ` · resets in ${untilStr(g.reset_at)}` : '') },
    h('span', { class: 'hidden sm:inline' }, label + ' '),
    h('span', { class: 'font-mono' }, Math.round(g.pct) + '%'));
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
        : h('div', { class: 'text-center text-zinc-600 mt-20 text-sm' }, 'Pick a model and say something.'),
      S.active ? activeBubble() : null),
    composer(),
  );
}
function msgBubble(m) {
  const mine = m.role === 'user';
  return h('div', { class: 'flex ' + (mine ? 'justify-end' : 'justify-start') },
    h('div', { class: 'max-w-[46rem] rounded-lg px-3 py-2 text-sm ' +
        (mine ? 'bg-accent/15 border border-accent/30' : 'bg-panel border border-line') },
      h('div', { class: 'prose-chat', html: mdLite(m.content) }),
      m.completion_tokens != null ? h('div', { class: 'mt-1 text-[11px] text-zinc-600' },
        `${m.model_id || ''} · ${m.prompt_tokens || 0}+${m.completion_tokens} tok` +
        (m.usage_estimated ? ' (est)' : '')) : null));
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
      h('div', { id: 'active-body', class: 'prose-chat', html: a.text ? mdLite(a.text) : '<span class="text-zinc-600">…</span>' })));
}
const modelName = id => S.models.find(m => m.id === id)?.display || id;

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
          ` · ~${Math.round(m.tok_s)} tok/s`) : null),
      m?.blurb ? h('div', { class: 'text-[11px] text-zinc-600 mb-2' }, m.blurb) : null,
      h('form', { class: 'flex gap-2 items-end', onsubmit: sendMessage },
        h('textarea', {
          id: 'composer', rows: 1, placeholder: 'Message…',
          class: 'flex-1 bg-panel2 border border-line rounded px-3 py-2 text-sm resize-none max-h-40',
          oninput: e => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px'; },
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
  const body = { model: S.pickerModel, message: text };
  if (S.conv) body.conversation_id = S.conv.id;
  try {
    for await (const ev of api.chatStream(body, aborter.signal)) {
      if (ev.type === 'accepted') {
        S.active.jobId = ev.job_id; S.active.position = ev.position;
        if (!S.conv) { S.conv = { id: ev.conversation_id, model_id: S.pickerModel, title: text.slice(0, 50) }; S.conversations.unshift(S.conv); }
        render();
      } else if (ev.type === 'loading') { S.active.state = 'loading_model'; S.active.eta = ev.eta_s; render(); }
      else if (ev.type === 'token') {
        const first = S.active.state !== 'generating';
        S.active.state = 'generating'; S.active.text += ev.text;
        if (first) render(); else { const b = document.getElementById('active-body'); if (b) { b.innerHTML = mdLite(S.active.text); scrollThread(); } }
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
