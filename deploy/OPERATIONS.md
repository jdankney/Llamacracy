# Llamacracy operations

Four systemd **user** units on the box (one of them, `llamacracy-dex`, just
wraps a Docker container):

| Process | What | Binds |
|---|---|---|
| `llama-swap.service` | inference layer, loads/swaps GGUF models | `127.0.0.1:8091` |
| `llamacracy.service` | FastAPI app (queue, metering, SPA) | `127.0.0.1:8000` |
| `llamacracy-dex.service` | Dex (docker, `deploy/dex/`) — identity provider, the user list | `wt0:5556` (NetBird) |
| `llamacracy-auth.service` | oauth2-proxy, the auth edge | `wt0:4180` (NetBird) |

Everything is on the NetBird network; nothing is on the public internet.
NetBird's *own* embedded IdP can't take extra OAuth clients
([netbirdio/netbird#5335](https://github.com/netbirdio/netbird/issues/5335)),
so Llamacracy runs its own Dex.

Both `llamacracy-dex` and `llamacracy-auth` discover the current `wt0` address
themselves at every start (`ExecStartPre`), so they survive NetBird reconnects
and peer re-enrols. Just restart the affected unit, or use `llamacracy restart`
for everything at once. The Dex issuer + oauth2-proxy redirect are pinned to
the NetBird **FQDN** (`<peer>.netbird.selfhosted`, see `netbird status`),
which never changes.

## The `llamacracy` command

`deploy/install.sh` puts a `llamacracy` script on `PATH` (`~/.local/bin`,
symlinked to `deploy/llamacracy-cli.sh` so `git pull` always gives you the
latest version) that drives all four units together:

```bash
llamacracy up          # start everything, in the right order
llamacracy down        # stop everything
llamacracy restart     # down then up
llamacracy status      # one line per unit + a quick /healthz check
llamacracy logs        # follow all four units' logs, interleaved
llamacracy logs app    # or just one: swap|dex|app|auth
```

It just calls `systemctl --user {start,stop,restart}` with all four unit
names — systemd resolves the actual dependency order from each unit's own
`After=`/`Wants=`. Nothing here bypasses systemd; it's a shortcut, not a
separate supervisor.

Note: on `down`, `llamacracy-auth` (oauth2-proxy) occasionally reports
`failed (Result: timeout)` rather than a clean stop if a browser still has the
live queue view open (an SSE connection oauth2-proxy waits to drain before
exiting). Harmless — it's stateless, gets SIGKILLed a few seconds later
either way, and `up` clears the failed state on the next start.

## First install

```bash
netbird up                          # if not already connected

./deploy/install.sh                 # idempotent. Scaffolds .env, bench/inventory.json,
                                    # deploy/oauth2-proxy.env and deploy/dex/config.yaml
                                    # from their examples (FQDN + secrets filled in),
                                    # generates config/, installs all four units + the CLI

$EDITOR bench/inventory.json        # your models (bench/README.md)
python3 bench/gen_llamaswap_config.py
$EDITOR .env                        # ADMIN_EMAILS, electricity rate

cd deploy/dex
./gen-hash.sh 'your-password'       # one staticPasswords entry per person in config.yaml
cd ../..
llamacracy restart
llamacracy status
```

Friends reach it at **`http://<your-fqdn>:4180`** while on the NetBird
network. They must use that FQDN, not the raw `100.x` address — the login
redirect is pinned to the FQDN and the bare IP will bounce-loop.

## Everyday commands

The whole stack at once (see above):

```bash
llamacracy status
llamacracy restart        # e.g. after NetBird reconnected / changed address
llamacracy logs swap      # model load/swap detail
```

By hand, one unit at a time (what `llamacracy` is calling under the hood):

```bash
systemctl --user status llama-swap llamacracy llamacracy-dex llamacracy-auth
journalctl --user -u llamacracy -f

# after a code change (git pull)
uv sync && ./deploy/install.sh && systemctl --user restart llamacracy
# ...and if the pull touched bench/gen_llamaswap_config.py, regenerate AFTER
# the restart (llama-swap runs with -watch-config and reloads it by itself)
python3 bench/gen_llamaswap_config.py

# after NetBird reconnected / changed address
systemctl --user restart llamacracy-dex llamacracy-auth
```

## Adding / removing a user

Users live in `deploy/dex/config.yaml` under `staticPasswords`. One block each:

```yaml
  - email: friend@example.com
    username: friend
    userID: "u-friend"          # unique + stable: it becomes the OIDC sub
    hash: "$2a$10$..."          # deploy/dex/gen-hash.sh 'their-password'
```

Then `systemctl --user restart llamacracy-dex`. Llamacracy creates the
user row (and their Account page, credit counters) on first sign-in. To cut
someone off for good, remove their block and restart; to pause them, use the
admin dashboard → Controls → disable (no restart, keeps their history).

Changing a `userID` orphans that person's history (new `sub` = new user row),
so don't.

## Adding, changing or removing a model

All model definitions live in `bench/inventory.json` (gitignored). Full
schema and workflow: [bench/README.md](../bench/README.md). The short version:

```bash
$EDITOR bench/inventory.json                 # add an entry (with a `seed` block) or delete one
python3 bench/gen_llamaswap_config.py        # rewrites config/llama-swap.yaml + models.json
llama-swap -config config/llama-swap.yaml -validate
systemctl --user restart llama-swap llamacracy
```

Benchmark when convenient so the queue's estimates and the cost model use
measured numbers instead of your seeds:

```bash
python3 bench/phase0_bench.py --only <key>
python3 bench/gen_llamaswap_config.py
systemctl --user restart llama-swap llamacracy
```

To **hide** a model from the chat picker without removing it: set
`"in_picker": false` in its inventory entry and regenerate.

## Adjusting limits and rates

All live in `.env` (gitignored). Edit, then `systemctl --user restart llamacracy`.

| Key | Meaning |
|---|---|
| `SESSION_CREDIT_LIMIT` | credits (= seconds of exclusive box time) per 5-hour session. Default 3600. |
| `WEEKLY_CREDIT_LIMIT` | rolling 7-day cap. Default 12000. |
| `LOAD_TIME_MULTIPLIER` | fraction of a cold-start's seconds that are billed. Default 0.5. |
| `MAX_TOKENS_PER_REQUEST` | hard output cap; bounds limit overshoot. Default 2048. |
| `THINKING_MAX_TOKENS` | output cap for a turn with Think on (reasoning and answer share it). Default 8192. |
| `API_MAX_TOKENS_PER_REQUEST` | output cap on the `/v1` endpoint, where an editor's apply model may rewrite a whole file. Default 8192. |
| `ELECTRICITY_RATE` / `TOU_SCHEDULE` | $/kWh. TOU_SCHEDULE (JSON hour→rate) wins if set. |
| `NON_GPU_LOAD_WATTS` | added to measured GPU watts for the cost model. Default 110. |
| `MARKUP` | multiplier on `cost_usd`. Default 1.0 -- raise to 2-3x for non-trivial invoices. |
| `IDLE_TTL_MINUTES` | unload the resident model after this long idle. Default 15. |
| `IDLE_TTL_SECONDS` | seconds-granularity idle unload; wins over `IDLE_TTL_MINUTES` when set. Short = frees VRAM fast but quick follow-ups re-pay the cold load. |
| `SEARXNG_URL` / `SEARCH_MAX_RESULTS` | local SearXNG instance + how many snippets get prepended when a user flips the search toggle on. Not metered. |
| `UPLOAD_DIR` / `UPLOAD_MAX_MB` | where attached images land on disk and the per-file size cap. Default `./data/uploads`, 8 MB. |
| `COMPACT_KEEP_RECENT` | messages always left verbatim when a user compacts a conversation. Default 6. |
| `COMPACT_SUMMARY_MAX_TOKENS` | cap on the length of the generated summary itself. Default 600. |

**Per-user**, in the admin dashboard → Admin → Controls tab (no restart):
- **Limit overrides** — type a number in the `session` / `week` box and hit
  *Set* to give someone a different cap (blank = fall back to the global
  default). Good for a boosted allowance for a specific project.
- **Uncapped** — that user is never blocked at enqueue. Their usage % is still
  tracked and shown (and will climb past 100%); the 75/90% warnings are
  silenced for them.
- **Disable / Enable** — hard-stop an account, keeping its history.

Changing a rate only affects **future** jobs -- `rate_used` and `cost_usd` are
frozen on each job row when it finishes.

## Continue.dev / any OpenAI-compatible tool

Each user generates their own key from **Account → API access** in the web UI
(shown once — copy it then, it can't be viewed again; revoke and re-generate
if it leaks). It flows through the same FIFO queue and credit limits as the
web chat; the only difference is nothing gets saved to the web UI's chat
history (the IDE sends its own full message list every call, like the real
OpenAI API).

Continue.dev `config.yaml` (`~/.continue/config.yaml`):
```yaml
models:
  - name: Llamacracy
    provider: openai
    model: <model-id>                                      # any id from /v1/models
    apiBase: http://<your-fqdn>:4180/v1
    apiKey: llk_...                                        # from Account -> API access
    roles: [chat, edit, apply]
```
For a fuller setup (a dedicated apply model, the key kept in a `.env` file,
a rule that stops small models wiping files), start from
[`examples/continue/`](../examples/continue/).

`GET /v1/models` lists the current picker models (the keys in your
inventory). No `/v1/completions` — editor autocomplete isn't wired up (it
would contend with everyone else's chats on the same single-GPU FIFO queue).

**Thinking** is off for API calls too, unless the request sets
`chat_template_kwargs: {enable_thinking: true}` (in Continue:
`requestOptions.extraBodyProperties`). A request that does gets the larger
`THINKING_MAX_TOKENS` budget, since the reasoning and the answer share it.

**Sampling and length.** The inventory's sampling values only fill in what
the request leaves out, so a `temperature` set in the editor is the one used.
Replies are capped at `API_MAX_TOKENS_PER_REQUEST` (default 8192), not the
2048 chat cap, so an apply model can rewrite a whole file.

**Tool calling works** — agent mode, edit tools, the lot.
`/v1/chat/completions` forwards the request body to llama-swap untouched apart
from the token cap and the model's sampling defaults, so `tools`,
`tool_choice` and tool-result messages pass straight through and the reply
comes back with real `tool_calls`. Whether a given model is any *good* at it
is a property of the model, not of Llamacracy — the smaller ones will call the
wrong tool or invent arguments.

Mind the queue, though: an agent turn is one job per round trip, and each one
takes its place in the FIFO behind whoever else is chatting. A twelve-step
edit loop is twelve queue entries, each billed for the seconds it actually
uses. Fine on your own; less fine while three people are mid-answer.

A revoked key stops authenticating immediately. **Admin → API keys** lists
every key issued across all users (owner, label, created, last used) with its
own revoke button, so a leaked key can be killed without touching the rest of
that account. Admin → Users → disable blocks the account entirely.

## Mobile access (PWA)

Nothing to run — it's static files (`manifest.webmanifest` + meta tags in
`static/index.html`), served by the same app. Users install it themselves:
NetBird connected, open the site in Safari (iOS) or Chrome (Android), then
"Add to Home Screen" / "Install app". See [docs/WELCOME.md](../docs/WELCOME.md)
for the steps to send to users, and [docs/DECISIONS.md](../docs/DECISIONS.md)
for why there's deliberately no service worker.

## Backup

Everything that matters is `data/llamacracy.db` (SQLite WAL) plus
`data/uploads/`. Snapshot the database:
```bash
sqlite3 data/llamacracy.db ".backup '/path/to/backup/llamacracy-$(date +%F).db'"
```

## Seasonal electricity rates

If your tariff changes with the season, update `ELECTRICITY_RATE` /
`TOU_SCHEDULE` when the new bill arrives and restart `llamacracy`. Past jobs
keep the rate they were billed at.
