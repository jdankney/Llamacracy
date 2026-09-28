# Design decisions

Why Llamacracy is shaped the way it is: every non-obvious choice, with the
reasoning, so you can tell which ones to keep when you adapt it. The
[SPEC.md](SPEC.md) is the brief these were made against.

## Architecture

- **llama-swap owns the models; the app never spawns `llama-server`.** The
  app talks OpenAI-compatible HTTP to llama-swap, which loads, swaps and
  unloads. Chosen over `llama-server`'s own router mode for its `groups`
  (co-residency control), the explicit `/running` + unload endpoints the queue
  needs, and `-watch-config`, which picks up a regenerated config without a
  restart.
- **FastAPI + one SQLite connection (WAL) behind one asyncio lock.** Writes
  are tiny (job and message rows) and the queue is serial anyway. Timestamps
  are epoch floats throughout, so the rolling-weekly query is a plain indexed
  `SUM ... WHERE finished_at >= ?`.
- **Vanilla JS front-end, no build step, no framework.** One `app.js`, one
  hand-written `styles.css`. Markdown, sanitising, code highlighting and
  math come from cdnjs as classic scripts, with a regex fallback so a blocked
  CDN degrades to readable text instead of nothing.
- **Generated config from a declarative inventory.** Your models live in
  `bench/inventory.json`; the benchmark measures them; the generator writes
  both the llama-swap config and the app's registry from those two files.
  Nothing about a specific box is committed.

## The queue

- **Strict global FIFO, one inference at a time. No priority, no reordering,
  no coalescing.** Deliberate: fairness that everyone can predict, and a VRAM
  budget that never has to be shared.
- The resident model is always read from llama-swap's `/running`, never
  guessed, so the "cold start ~N s" hint and the load-time billing are honest.
- Per-model load times are learned (EWMA over observed cold starts), seeded
  from the benchmark.
- Cancelling a `generating` job really aborts the upstream request: the
  worker breaks out of the stream and the httpx context manager closes the
  connection.
- Idle TTL unloads the resident model after N minutes with an empty queue.
  llama-swap's own `ttl` is only a backstop.
- `SMALL_MODEL_FAST_LANE` is wired (jobs carry a `lane`, only exclusive-lane
  seconds are full price) but not implemented; everything runs exclusive.

## Metering and cost

- **1 credit = 1 second of exclusive box time**, measured from the app's own
  timers, never estimated. Only token counts may be estimated, and such rows
  are flagged.
- `credits = active_seconds + load_seconds * LOAD_TIME_MULTIPLIER`. Loads
  are charged to whoever triggered them, at half rate by default. Failed
  loads and upstream errors cost nothing. Cancelled jobs pay for the seconds
  actually consumed. Queue waiting is never charged.
- **Limits are checked at enqueue, not after a queue wait** (rejecting
  someone after they waited is hostile), and before anything about the
  request is persisted, so a rejection leaves no orphan rows. Overshoot is
  allowed: an admitted job runs to completion even past the cap, bounded by
  its output cap (`MAX_TOKENS_PER_REQUEST`, or the Think or `/v1` cap).
- Sessions open on a user's first request and run a fixed 5 hours, never
  extended by activity. The weekly limit is a rolling 7-day sum, not a
  calendar week. The 429 carries which limit was hit and the exact reset time
  (for weekly: the moment the rolling sum first drops back under the cap).
- `cost_usd = credits × watts × rate × MARKUP`, where watts is the measured
  mean GPU draw during the job plus a fixed `NON_GPU_LOAD_WATTS`, and rate
  comes from the time-of-use table for the hour the job ran. The rate and the
  cost are frozen on the job row; historical costs are never recomputed.
- Vision turns have **no synthetic surcharge**: they are genuinely slower
  (image prompt-eval, usually a model swap), so the honest extra cost falls
  out of the measured seconds.

## Identity

- **oauth2-proxy in front, the app writes no auth code.** It trusts the
  forwarded headers because it only ever listens on 127.0.0.1 with
  oauth2-proxy in front, and refuses to serve at all if the headers are
  missing and `DEV_MODE` is unset.
- **Users are keyed on the OIDC `sub`, never the email.** Emails can change;
  `sub` cannot. Admin status comes from `ADMIN_EMAILS` in config, checked
  against the identity on every request, not from a database flag.
- **Our own Dex, not NetBird's.** NetBird's combined server embeds a Dex but
  exposes no way to register a third OAuth client
  ([netbirdio/netbird#5335](https://github.com/netbirdio/netbird/issues/5335)).
  A separate Dex in Docker, published on the NetBird interface only, keeps
  the whole chain inside the tunnel and adds nothing to the public internet.
  Rejected: a public IdP with passkeys (needs HTTPS and a public login
  page), GitHub as the provider (external dependency, everyone needs an
  account).
- Issuer and redirect URL are pinned to the peer's NetBird **FQDN**, which is
  stable across reconnects; only the bind address is discovered at each
  start. Users must use the FQDN: a bare-IP visit gets a cookie-host mismatch
  and loops.
- **Plain HTTP is deliberate.** WireGuard is the encryption layer. This also
  means `navigator.clipboard` and service workers are unavailable (both need
  a secure context), which shaped two front-end choices below.
- **API keys** for IDE tools are per-user bearer tokens, shown once, stored
  as a sha256 hash only, like a GitHub PAT. Those two `/v1` paths are carved
  out of oauth2-proxy so an IDE gets a clean 401 instead of a login redirect.
  Admins can revoke any single key without disabling the account.

## Inference layer

- `-np 1` everywhere: llama-server defaults to 4 parallel slots and splits
  the KV cache across them. Under strict FIFO one slot means KV = 1 × ctx.
- **Reasoning is off by default and a per-message toggle** (a "token / energy
  saver" default with an opt-in). `--reasoning-budget 0` does not stop models
  that emit `<think>` by default; `chat_template_kwargs.enable_thinking` does.
  That flag used to be forced off by a llama-swap `filters.setParams`, but a
  filter overrides the request, so no user could ever turn thinking on. The app
  now sends the flag on every request to a model marked `thinking: true` in the
  inventory: on only when the user flipped Think (or an API client asked for
  it), and the queue fills in "off" for anything that didn't say, so no path
  can make a model think by accident. The generator no longer emits the
  filter.
- **A thinking turn gets its own, larger output budget**
  (`THINKING_MAX_TOKENS`, default 8192). Reasoning and answer share
  `max_tokens`, and a model can think for thousands of tokens, so the everyday
  cap would leave no room for the answer. It is also the overshoot bound for
  those turns. The thinking is saved with the reply (shown folded under
  "Thought for 1m 26s", timed from its first token to the answer's first
  token) but never resent as history, so it costs context only on the turn
  that produced it. A reply that spent its whole budget thinking is still
  saved, because the user paid for that reasoning.
- Flash attention defaults to on for every model: quantised (`q8_0`) KV
  requires it in llama.cpp, and it was faster for prompt processing on
  everything measured. The benchmark still compares on and off, and an
  inventory entry can set `serve.fa` if a model disagrees.
- **A FIM (code infill) model must never be chattable.** Two independent
  guards: `unlisted` in llama-swap and `kind: fim` / `in_picker: false` in
  the registry.
- **Vision on demand.** A model with a `vision` block gets a second,
  unlisted llama-swap entry (same weights + `--mmproj`). A turn with an image
  swaps the job's model id to that variant; because the model id already
  flows through billing, swap groups and display names, that one
  substitution gets everything else right for free. Only the current turn's
  image is sent; earlier images are not resent on every follow-up.
- **Documents are text by the time anyone sends them.** An upload is
  extracted to Markdown immediately (headings, lists and tables kept, pages
  and slides marked) and the text is stored beside the original. Like search,
  that is CPU work outside the queue, so a send never waits on a parser and
  extraction costs no credits; the model pays only, in prompt-processing
  seconds, for what it is sent. Parsing runs in a child process that caps its
  own memory and CPU, reads XML through defusedxml and refuses zip archives
  that would inflate past 300 MB, under a wall-clock timeout: the web process
  never parses an untrusted file. The parsers are pure Python (pypdf,
  openpyxl, and hand-written readers for docx/pptx/ODF) so a plain `uv sync`
  is the whole install. Legacy binary formats (.doc, .xls, .ppt) are refused
  with a "save it as PDF or .docx" message rather than handed to LibreOffice.
- **A document stays in the conversation.** It is sent ahead of its message
  on every later turn of that branch, the way the message text is, because a
  follow-up about page 3 needs page 3. (Images are only sent with their own
  turn: re-encoding them every turn is expensive and the answer about them is
  already in the history.) llama.cpp's prompt cache makes the repeat cheap
  while the model stays loaded. Compaction folds documents away with their
  messages; the summary is told their names. A send whose documents would not
  fit the model's context is refused up front, in words, rather than failed
  by llama.cpp after a queue wait, unless the model can look things up (next
  point). Originals are only ever served as
  downloads (`application/octet-stream`, `nosniff`), since an uploaded .html
  or .svg rendered inline would run as this site.
- **The `/v1` chat endpoint is a raw passthrough, not a modelled one.** The
  request body is forwarded as it arrived, with only the `max_tokens` cap
  applied and the model's sampling defaults filled in where the client left
  them out, and llama-swap's response bytes
  are streamed back unaltered. Only `model` and the presence of `messages` are
  validated. This is a correction. The endpoint originally declared a pydantic
  body of `model`/`messages`/`stream`/`max_tokens`, and pydantic's default
  `extra="ignore"` meant a client's `tools` array was silently dropped, so
  llama.cpp never rendered a tool section and models answered with a prose
  description of the call they wanted to make; the same modelled body also
  rejected the `content: null` assistant turn that carries `tool_calls`.
  Billing never needs to understand the payload: a sniffer reads `usage` and
  `timings` off the same bytes without touching them, and falls back to the
  usual estimate if it finds neither.
- **Over `/v1` the client owns the tool loop.** Each round trip from an IDE
  arrives as its own job and queues normally: Llamacracy runs no tools on an
  API caller's behalf.
- **In the web UI, an agent turn is one job that keeps the slot.** This
  reverses the first design, which refused tool loops in the web UI because
  they "hold the queue" and make billing murky. Both costs turned out smaller
  than the alternative. Released between steps, the slot would let another
  user's prompt evict llama.cpp's single cache slot (`-np 1`), and every
  step would then re-read the whole conversation: on a MoE model at ~250
  prompt tokens/s that is minutes of GPU per turn, paid by everyone waiting.
  Held, only the new tool result is read each step. Billing stays simple:
  the turn is one job and is charged for the time it held the slot,
  including the seconds spent waiting on a search, because nobody else
  could use the GPU then either. Limits keep it bounded: `AGENT_MAX_STEPS`
  rounds of tool calls (6), `AGENT_MAX_SECONDS` of wall clock (240), a cap
  on each tool result, one output budget shared by every step (so the
  overshoot past a credit limit stays bounded), and a context check that
  stops tool use while there is still room to answer. Hitting any limit
  sends one last request with `tool_choice: "none"`, so a turn always ends
  in an answer. Only that answer is kept as history: the trail of steps and
  the numbered sources are stored with the reply for display, the way
  reasoning is, not resent on later turns.
- **Tools are read-only outside the conversation.** A tool result is text
  from the web or a file, returned to the model as a `tool` message and
  framed in the system prompt as facts to weigh, not instructions; no tool
  can reach anything that would make an injected instruction dangerous.
- **Long documents are looked up, not truncated.** A model with `tools`
  gets its documents whole while they fit in `DOC_INLINE_FRACTION` (half) of
  its context; past that the biggest are replaced by a short
  `<document ref="d1" …>` stand-in and the model gets `doc_outline`,
  `doc_search` and `doc_read`. It is the pattern coding agents use on a
  repository: find, then read the part that matters. A question about
  section 4 of a 100-page PDF then costs a few thousand prompt tokens
  instead of 50,000 (three-plus minutes of prompt processing on a MoE model
  here). Search is keyword scoring over paragraphs, not embeddings: an
  embedding model would need the GPU, and so a model swap, mid-turn.
  Documents a compaction folded away stay reachable the same way, listed in
  the system prompt. Truncating a long document silently was never an
  option: the model would answer confidently about text it never saw.
- **Workspace files are versions tied to messages, not a folder.** Every
  write the model makes is a new row pointing at the user message whose
  turn wrote it, and a turn sees the newest version of each path among the
  messages on its own branch. That makes workspaces fork exactly like
  conversations: editing a message starts a branch from the files as they
  were, and flipping back restores the later ones, with no copying and
  nothing ever lost. Paths are names, validated and stored in the database,
  never places on disk; files are text, capped (40 files, 200K characters
  each) and served only as plain-text downloads. Edits are an exact,
  must-be-unique find-and-replace, the way coding agents edit, because a
  small model rewriting a whole file to change one line drops the rest of
  it. Files is a toggle (every tool schema costs prompt tokens, and a chat
  that doesn't need files shouldn't invite the model to make some), but
  once a branch has files the tools stay on, so a follow-up can always
  reach what an earlier turn wrote.
- **A picture the model asks for arrives as a user turn.** OpenAI-style
  tool results are text, so `view_image` returns a line of text and the
  image itself is appended as the next user message, the one place a chat
  template accepts one. Earlier photos get short names (`img1`, `img2`)
  listed in the system prompt, which replaces the old rule that a model only
  ever sees a photo in its own message. Web images are fetched by the same
  public-address-only fetcher as pages and accepted only as PNG, JPEG or
  WebP by their bytes, not their headers; the UI shows thumbnails only for
  the user's own uploads and never loads a remote image. These turns need
  the `-vision` entry, so they run there, and a resident `-vision` entry is
  reused for plain turns (it is the same weights plus the projector) rather
  than swapped out, provided its context is no smaller.
- **`read_page` fetches public addresses only, pinned per hop.** A model
  choosing URLs is a server-side request forgery risk by construction, on a
  machine that sits in a home network. Each hop's host is resolved, every
  address must be global unicast (loopback, private, link-local, CGNAT,
  multicast and reserved ranges are refused, v4 and v6, including
  v4-mapped), and the request then goes to that checked IP with Host and SNI
  keeping the name, so a second DNS answer can't differ from the checked
  one. Redirects are followed by hand and each is checked again; the
  environment's proxy settings are ignored; the body is capped. Online PDFs
  go through the same sandboxed extractor as uploads. A page is fetched once
  per turn and read in parts, like a long document.
- **On `/v1` the client's sampling wins and the output cap is larger.** In
  the web UI nobody picks a temperature, so the inventory's values are
  final. An editor sets them on purpose (a low temperature for its apply
  model), so on `/v1` the inventory only fills in what the client left out.
  This reverses the first version, which forced the inventory's values on
  every caller. The cap is `API_MAX_TOKENS_PER_REQUEST` (default 8192)
  rather than the 2048 chat cap, because an apply model that is cut off
  halfway through rewriting a file leaves the file truncated. It still bounds
  how far one API call can overshoot a limit.
- **Context per model is maxed out** by a benchmark sweep that walks a
  ladder of (ctx, KV type, expert offload) and keeps the largest that fits.
  On a small-VRAM card the ceiling is usually system RAM, not VRAM, and
  `q8_0` KV was faster than `f16` at equal or larger context.

## Front-end

- **Markdown via marked + DOMPurify + highlight.js; DOMPurify is not
  optional.** marked passes raw HTML through, so a model emitting
  `<img onerror=…>` would otherwise run. Links are forced to
  `target=_blank rel=noopener`.
- **Math is extracted before markdown**, rendered with KaTeX, and swapped
  back in through private-use-codepoint placeholders, so markdown never eats
  the backslashes in `\(...\)`. Heavy passes (highlighting, math) run once
  on the settled message, not per streamed token.
- **Copy copies the raw source**, not the rendered HTML, and falls back to
  `execCommand('copy')` because the async Clipboard API needs a secure
  context.
- **The context ring** projects the next send against the model's window:
  exact token usage from the last reply plus an estimate for anything after
  it and the draft. Compaction makes earlier usage stale, so the ring stops
  treating it as exact once a boundary exists.
- **Compact history is on demand only**, never automatic: no surprise
  credit spend, no surprise memory loss. Nothing is deleted; folded messages
  stay visible with a divider showing the summary the model now sees.
- **Editing forks the conversation instead of rewriting it.** Messages form a
  tree (`parent_id`); each message remembers which child is shown
  (`active_child_id`) and the conversation which first message is
  (`active_root_id`). An edit is a new sibling of the original with its own
  reply, and switching versions only moves a pointer, so every branch keeps
  its own follow-ups and nothing is ever deleted. Only the active path is
  sent to the model. Compaction summaries moved from the conversation onto
  the boundary message, so a summary applies exactly to the paths through
  it: a fork from above the cut keeps its full history rather than
  inheriting a summary of messages it never had. Older, linear
  conversations are migrated to single-branch trees at startup. Only your
  own messages can be edited; there is no "regenerate" for replies (yet),
  though the tree would take one without changes.
- **Search is a toggle; with it on, a capable model drives it.** A model
  marked `tools: true` in the inventory runs the agent loop (see Inference
  layer): it writes its own queries, searches again when the first try
  misses, and cites numbered sources. Every other model keeps the original
  behaviour, because small models are unreliable at deciding when and how to
  call a tool: one query built from the message, top snippets prepended to
  that turn only, one normal inference. The search call itself is not
  metered either way.
- **Installable PWA without a service worker.** Registering one needs HTTPS
  or literally `localhost`; iOS "Add to Home Screen" predates that
  requirement and works fine over plain HTTP, and there is no meaningful
  offline mode for live inference anyway.
- **Per-user appearance lives on the server, derived from four colours.** A
  user picks a preset or their own background, panel, text and accent colours
  (plus a chat text size). Every other shade (hovers, lines, muted text,
  status colours tuned for light or dark grounds) is computed from those
  four in `app.js` and written onto `:root` as the same custom properties
  `styles.css` defines, so no component knows themes exist and no rule may
  hard-code a colour. Prefs are stored per account (`users.prefs_json`) so a
  theme follows the person to every device, and cached in `localStorage` so
  the page paints in the right colours before `/api/me` answers. Colours are
  validated as strict `#rrggbb` on write because they end up in CSS. Code
  blocks keep a dark ground under every theme, since highlight.js's colours
  are a dark theme. It lives on the renamed **Account** tab (was Usage)
  rather than a fifth tab the phone top bar has no room for.
- The composer auto-focus only fires on devices with a fine pointer, so a
  phone never gets its keyboard summoned by a re-render.

## Operations

- Four systemd **user** units with linger enabled, so nothing needs a login
  session. `install.sh` is idempotent and re-runnable after every pull.
- The auth and Dex units discover the NetBird interface address on every
  start, so a peer re-enrol needs a restart, not a config edit.
- uvicorn runs with `--timeout-graceful-shutdown 5`: an open SSE connection
  never closes on its own, and without the bound the unit would hang on
  SIGTERM until systemd killed it.
- The `llamacracy` CLI is a thin wrapper that hands all four unit names to
  `systemctl` and lets systemd's own ordering resolve the rest.
