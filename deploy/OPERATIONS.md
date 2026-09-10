# Llamacracy operations

Three systemd **user** units on `myhost`:

| Unit | What | Binds |
|---|---|---|
| `llama-swap.service` | inference layer, loads/swaps GGUF models | `127.0.0.1:8091` |
| `llamacracy.service` | FastAPI app (queue, metering, SPA) | `127.0.0.1:8000` |
| `llamacracy-auth.service` | oauth2-proxy, the only public-facing process | `wt0:4180` (NetBird) |

`llamacracy-auth` reads the `wt0` address at start, so it survives NetBird
reconnects (it just needs a restart: `systemctl --user restart llamacracy-auth`).

## First install

```bash
netbird up                      # if not already connected
./deploy/install.sh             # idempotent; prints the OIDC redirect URI to register
# register  http://<wt0>:4180/oauth2/callback  in the NetBird IdP,
# put CLIENT_ID / CLIENT_SECRET in deploy/oauth2-proxy.env, then:
systemctl --user enable --now llamacracy-auth.service
```

Friends reach it at `http://<wt0-addr>:4180` (or `http://myhost.netbird.selfhosted:4180`)
while on the NetBird network.

## Everyday commands

```bash
# status / logs
systemctl --user status llama-swap llamacracy llamacracy-auth
journalctl --user -u llamacracy -f
journalctl --user -u llama-swap -f          # model load/swap detail

# restart after a code change (git pull)
cd ~/Documents/Coding/Llamacracy && uv sync
systemctl --user restart llamacracy

# restart after NetBird reconnected / changed address
systemctl --user restart llamacracy-auth

# stop everything
systemctl --user stop llamacracy-auth llamacracy llama-swap
```

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

**Per-user** overrides (session/weekly caps, disable) are in the admin
dashboard → Controls, no restart needed.

Changing a rate only affects **future** jobs -- `rate_used` and `cost_usd` are
frozen on each job row when it finishes.

## Backup

Everything that matters is `data/llamacracy.db` (SQLite WAL). Snapshot it:
```bash
sqlite3 data/llamacracy.db ".backup '/path/to/backup/llamacracy-$(date +%F).db'"
```

## Winter electricity rates

`TOU_SCHEDULE` is seeded with the utility **summer** rates. When the first
Nov–May bill arrives, update the off-peak / super-off-peak generation numbers
(delivery is flat year-round) and restart `llamacracy`.
