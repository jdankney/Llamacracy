# Llamacracy operations

Three systemd **user** units on `myhost`, plus one Docker container:

| Process | What | Binds |
|---|---|---|
| `llama-swap.service` | inference layer, loads/swaps GGUF models | `127.0.0.1:8091` |
| `llamacracy.service` | FastAPI app (queue, metering, SPA) | `127.0.0.1:8000` |
| `llamacracy-auth.service` | oauth2-proxy, the auth edge | `wt0:4180` (NetBird) |
| `llamacracy-dex` (docker, `deploy/dex/`) | identity provider — the user list | `wt0:5556` (NetBird) |

Everything is on the NetBird network; nothing is on the public internet.
NetBird's *own* embedded IdP can't take extra OAuth clients
([netbirdio/netbird#5335](https://github.com/netbirdio/netbird/issues/5335)),
so Llamacracy runs its own Dex.

`llamacracy-auth` reads the `wt0` address at start, so it survives NetBird
reconnects (it just needs a restart: `systemctl --user restart llamacracy-auth`).
The Dex issuer + oauth2-proxy redirect are pinned to the NetBird **FQDN**
(`myhost.netbird.selfhosted`), which doesn't change on reconnect — but the
Dex container's port binding is a literal IP (`deploy/dex/docker-compose.yml`),
so after a peer **re-enrol** update that line and `docker compose up -d`.
`install.sh` rewrites it to the current `wt0` address on each run.

## First install

```bash
netbird up                          # if not already connected

# 1. identity provider
cd deploy/dex
cp config.yaml.example config.yaml
sed -i "s/REPLACE_WITH_openssl_rand_hex_32/$(openssl rand -hex 32)/" config.yaml
./gen-hash.sh 'your-password'        # paste into the j4mes staticPasswords hash:
#   ...repeat gen-hash.sh + add a staticPasswords block per friend...
docker compose up -d
curl -sf http://myhost.netbird.selfhosted:5556/.well-known/openid-configuration >/dev/null && echo "dex ok"
cd ../..

# 2. app + auth edge
./deploy/install.sh                 # idempotent; picks up the dex client secret,
                                    # generates the cookie secret, installs units
# if it reports the client secret wasn't matched, set OAUTH2_PROXY_CLIENT_SECRET
# in deploy/oauth2-proxy.env to the `secret:` from deploy/dex/config.yaml, then:
systemctl --user enable --now llamacracy-auth.service
```

Friends reach it at **`http://myhost.netbird.selfhosted:4180`** while on the
NetBird network. They must use that FQDN, not the raw `100.x` address — the
login redirect is pinned to the FQDN and the bare IP will bounce-loop.

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
( cd ~/Documents/Coding/Llamacracy/deploy/dex && docker compose stop )

# dex logs / restart
docker logs -f llamacracy-dex
( cd ~/Documents/Coding/Llamacracy/deploy/dex && docker compose restart )
```

## Adding / removing a user

Users live in `deploy/dex/config.yaml` under `staticPasswords`. One block each:

```yaml
  - email: friend@example.com
    username: friend
    userID: "u-friend"          # unique + stable: it becomes the OIDC sub
    hash: "$2a$10$..."          # deploy/dex/gen-hash.sh 'their-password'
```

Then `cd deploy/dex && docker compose restart dex`. Llamacracy creates the
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
