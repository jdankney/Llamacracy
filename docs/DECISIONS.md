# Design decisions

Why Llamacracy is shaped the way it is: every non-obvious choice, with the
reasoning, so you can tell which ones to keep when you adapt it. The
[SPEC.md](SPEC.md) is the brief these were made against.

## Architecture

- **llama-swap owns the models; the app never spawns `llama-server`.** The
  app talks OpenAI-compatible HTTP to llama-swap, which loads, swaps and
  unloads. Chosen over `llama-server`'s own router mode for its `groups`
  (co-residency control), explicit `/running` + unload endpoints the queue
  needs, and `filters.setParams` for clean server-side request shaping.
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
  `MAX_TOKENS_PER_REQUEST`.
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
- Flash attention is on for every model: quantised (`q8_0`) KV requires it in
  llama.cpp, and it was faster for prompt processing on everything measured.
  No toggle is exposed.
- **A FIM (code infill) model must never be chattable.** Two independent
  guards: `unlisted` in llama-swap and `kind: fim` / `in_picker: false` in
  the registry.
- **Vision on demand.** A model with a `vision` block gets a second,
  unlisted llama-swap entry (same weights + `--mmproj`). A turn with an image
  swaps the job's model id to that variant; because the model id already
  flows through billing, swap groups and display names, that one
  substitution gets everything else right for free. Only the current turn's
  image is sent; earlier images are not resent on every follow-up.
- **The `/v1` chat endpoint is a raw passthrough, not a modelled one.** The
  request body is forwarded as it arrived, with only the model's sampling
  defaults and the `max_tokens` cap merged in, and llama-swap's response bytes
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
- **Tool loops are acceptable over `/v1` but not in the web UI**, because the
  IDE owns the loop. Each round trip arrives as its own job and queues
  normally, so an agent turn is N ordinary FIFO entries rather than one job
  holding the GPU across N inferences.
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
- **Search is a toggle, never model-driven.** Small local models are
  unreliable at deciding when to call a tool, tool loops thrash the context
  and hold the queue, and billing across N inferences gets murky. One query,
  top snippets prepended to that turn only, one normal inference. Not metered.
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
