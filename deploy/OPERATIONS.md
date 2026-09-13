# Llamacracy operations

Four systemd **user** units on `myhost` (one of them, `llamacracy-dex`,
just wraps a Docker container):

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
and peer re-enrols — no more hand-editing an IP into `docker-compose.yml`.
Just restart the affected unit (`systemctl --user restart llamacracy-dex` /
`llamacracy-auth`), or use `llamacracy restart` for both at once. The Dex
issuer + oauth2-proxy redirect are pinned to the NetBird **FQDN**
(`myhost.netbird.selfhosted`), which never changes.

## The `llamacracy` command

`deploy/install.sh` puts a `llamacracy` script on `PATH` (`~/.local/bin`,
symlinked to `deploy/llamacracy-cli.sh` so `git pull` always gives you the
latest version) that drives all four units together:

```bash
llamacracy up        # start everything, in the right order
llamacracy down       # stop everything
llamacracy restart    # down then up
llamacracy status     # one line per unit + a quick /healthz check
llamacracy logs        # follow all four units' logs, interleaved
llamacracy logs app    # or just one: swap|dex|app|auth
```

It just calls `systemctl --user {start,stop,restart}` with all four unit
names — systemd itself resolves the actual dependency order from each unit's
own `After=`/`Wants=` (see `deploy/systemd/*.service`), regardless of the
order given. Nothing here bypasses systemd; it's a shortcut, not a separate
supervisor.

Note: on `down`, `llamacracy-auth` (oauth2-proxy) occasionally reports
`failed (Result: timeout)` rather than a clean stop if a browser still has the
live queue view open (an SSE connection oauth2-proxy waits to drain before
exiting). Harmless — it's stateless, gets SIGKILLed a few seconds later
either way, and `up` clears the failed state on the next start. `llamacracy.service`
itself is not affected (`--timeout-graceful-shutdown 5` on uvicorn bounds its
own drain wait so it always exits cleanly within systemd's stop timeout).

## First install

```bash
netbird up                          # if not already connected

# 1. app + auth edge + dex, all scaffolded and installed in one pass
./deploy/install.sh                 # idempotent; creates deploy/dex/config.yaml
                                    # with a fresh secret if missing, installs
                                    # all four units + the `llamacracy` CLI

# 2. add yourself (and friends) to dex before anyone can actually log in
cd deploy/dex
./gen-hash.sh 'your-password'        # paste into the j4mes staticPasswords hash:
#   ...repeat gen-hash.sh + add a staticPasswords block per friend...
cd ../..
systemctl --user restart llamacracy-dex   # picks up the password you just added

# 3. bring the auth edge up now that dex is answering (install.sh usually
# already did this -- rerun if it warned the secret wasn't matched yet)
llamacracy up
curl -sf http://myhost.netbird.selfhosted:5556/.well-known/openid-configuration >/dev/null && echo "dex ok"
```

Friends reach it at **`http://myhost.netbird.selfhosted:4180`** while on the
NetBird network. They must use that FQDN, not the raw `100.x` address — the
login redirect is pinned to the FQDN and the bare IP will bounce-loop.

## Everyday commands

The whole stack at once (see "The `llamacracy` command" above):

```bash
llamacracy status      # one line per unit + a quick /healthz check
llamacracy up            # start everything
llamacracy down           # stop everything
llamacracy restart        # e.g. after NetBird reconnected / changed address
llamacracy logs            # follow all four, interleaved
llamacracy logs swap       # or just one: swap|dex|app|auth
```

By hand, one unit at a time (what `llamacracy` is calling under the hood):

```bash
systemctl --user status llama-swap llamacracy llamacracy-dex llamacracy-auth
journalctl --user -u llamacracy -f
journalctl --user -u llama-swap -f          # model load/swap detail

# restart after a code change (git pull)
cd ~/Documents/Coding/Llamacracy && uv sync
systemctl --user restart llamacracy

# restart after NetBird reconnected / changed address
systemctl --user restart llamacracy-dex llamacracy-auth

# stop everything
systemctl --user stop llamacracy-auth llamacracy llama-swap llamacracy-dex
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
user row (and their `/usage` page, credit counters) on first sign-in. To cut
someone off for good, remove their block and restart; to pause them, use the
admin dashboard → Controls → disable (no restart, keeps their history).

Changing a `userID` orphans that person's history (new `sub` = new user row),
so don't.

## Adding a model

1. Drop the GGUF under `~/models/<Name>/<file>.gguf`.
2. Benchmark it so the config and the UI estimates are real:
   ```bash
   python3 bench/phase0_bench.py --only <key>      # after adding it to bench/phase0_bench.py inventory()
   ```
   or, for a quick add without a full sweep, edit `bench/bench-results.json` by
   hand with rough numbers.
3. Regenerate the inference config + registry:
   ```bash
   python3 bench/gen_llamaswap_config.py
   ~/.local/bin/llama-swap -config config/llama-swap.yaml -validate
   ```
   (Add human metadata for it in `REGISTRY_META` in the generator first.)
4. `systemctl --user restart llama-swap llamacracy`

To **hide** a model from the chat picker: set `in_picker: false` in
`config/models.json` (or `kind: "fim"`), restart `llamacracy`.

## Adjusting limits and rates

All live in `.env` (gitignored). Edit, then `systemctl --user restart llamacracy`.

| Key | Meaning |
|---|---|
| `SESSION_CREDIT_LIMIT` | credits (= seconds of exclusive box time) per 5-hour session. Default 3600. |
| `WEEKLY_CREDIT_LIMIT` | rolling 7-day cap. Default 12000. |
| `LOAD_TIME_MULTIPLIER` | fraction of a cold-start's seconds that are billed. Default 0.5. |
| `MAX_TOKENS_PER_REQUEST` | hard output cap; bounds limit overshoot. Default 2048. |
| `ELECTRICITY_RATE` / `TOU_SCHEDULE` | $/kWh. TOU_SCHEDULE (JSON hour→rate) wins if set. |
| `NON_GPU_LOAD_WATTS` | added to measured GPU watts for the cost model. Default 110. |
| `MARKUP` | multiplier on `cost_usd`. Default 1.0 -- raise to 2-3x for non-trivial invoices. |
| `IDLE_TTL_MINUTES` | unload the resident model after this long idle. Default 15. |
| `IDLE_TTL_SECONDS` | seconds-granularity idle unload; wins over `IDLE_TTL_MINUTES` when set. `.env` ships 60. Short = frees VRAM fast but quick follow-ups re-pay the cold load. |
| `SEARXNG_URL` / `SEARCH_MAX_RESULTS` | local SearXNG instance + how many snippets get prepended when a user flips the search toggle on. Not metered (a local HTTP call, not GPU time). |
| `UPLOAD_DIR` / `UPLOAD_MAX_MB` | where attached images land on disk (never base64 in the DB) and the per-file size cap. Default `./data/uploads`, 8 MB. |
| `COMPACT_KEEP_RECENT` | messages always left verbatim when a user compacts a conversation; everything older gets folded into the running summary. Default 6. |
| `COMPACT_SUMMARY_MAX_TOKENS` | cap on the length of the generated summary itself. Default 600. |

**Per-user**, in the admin dashboard → Admin → Users tab (no restart):
- **Overrides** — type a number in the `sess` / `week` box and hit *set* to give
  someone a different cap (blank = fall back to the global default). Good for a
  boosted allowance for a specific project.
- **Uncapped** — toggle `∞ on`: that user is never blocked at enqueue. Their
  usage % is still tracked and shown (and will climb past 100%); the 75/90%
  warnings are silenced for them.
- **disable / enable** — hard-stop an account, keeping its history.

Changing a rate only affects **future** jobs -- `rate_used` and `cost_usd` are
frozen on each job row when it finishes.

## Continue.dev / any OpenAI-compatible tool

Each user generates their own key from **Usage → API access** in the web UI
(shown once — copy it then, it can't be viewed again; revoke and re-generate
if it leaks). It flows through the same FIFO queue and credit limits as the
web chat; the only difference is nothing gets saved to the web UI's chat
history (the IDE sends its own full message list every call, like the real
OpenAI API).

Continue.dev `config.yaml` (`~/.continue/config.yaml`):
```yaml
models:
  - name: Llamacracy - FamilyA 4B
    provider: openai
    model: fast-4b                                       # any id from /v1/models
    apiBase: http://myhost.netbird.selfhosted:4180/v1
    apiKey: llk_...                                        # from Usage -> API access
    roles: [chat, edit]
```
`GET /v1/models` lists the current picker models (same ids as `config/models.json`).
No `/v1/completions` in v1 — editor autocomplete isn't wired up (it would
contend with everyone else's chats on the same single-GPU FIFO queue; a fast
lane would be needed first).

A revoked/deleted key stops authenticating immediately (`/api/keys/{id}`,
DELETE, self-service from the same page).

## Mobile access (PWA)

Nothing to run — it's static files (`manifest.webmanifest` + meta tags in
`static/index.html`), served by the same app. Users install it themselves:
NetBird connected, open the site in Safari (iOS) or Chrome (Android), then
"Add to Home Screen" / "Install app". See [docs/WELCOME.md](../docs/WELCOME.md)
for the exact steps sent to users, and
[docs/DECISIONS.md](../docs/DECISIONS.md) for why there's deliberately no
service worker (needs a secure context; this app is plain HTTP by design).

## Backup

Everything that matters is `data/llamacracy.db` (SQLite WAL). Snapshot it:
```bash
sqlite3 data/llamacracy.db ".backup '/path/to/backup/llamacracy-$(date +%F).db'"
```

## Winter electricity rates

`TOU_SCHEDULE` is seeded with the utility **summer** rates. When the first
Nov–May bill arrives, update the off-peak / super-off-peak generation numbers
(delivery is flat year-round) and restart `llamacracy`.
