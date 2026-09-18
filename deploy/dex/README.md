# Dex — Llamacracy's identity provider

NetBird's combined server embeds its own Dex, but it only registers the
dashboard + CLI clients and there's no supported hook to add a third
([netbirdio/netbird#5335](https://github.com/netbirdio/netbird/issues/5335)).
So Llamacracy runs its **own** Dex, here, in Docker on the same box, published
on the NetBird interface only. Login → app all stays on the WireGuard tunnel.

```
friend's browser ──http──► oauth2-proxy (wt0:4180)
        │                        │
        └───auth redirect───► Dex (wt0:5556) ──password check──► staticPasswords
```

## First setup

`../install.sh` does the scaffolding: it copies `config.yaml.example` to
`config.yaml`, fills in your NetBird FQDN, generates the client secret and
mirrors it into `../oauth2-proxy.env`. What is left for you is the users:

```bash
cd deploy/dex
./gen-hash.sh 'alice-password'             # paste into a staticPasswords `hash:`
$EDITOR config.yaml                        # one entry per person
systemctl --user restart llamacracy-dex
curl -sf http://<your-fqdn>:5556/.well-known/openid-configuration | head -c 80
```

Doing it by hand instead: `cp config.yaml.example config.yaml`, replace
`REPLACE_FQDN`, set `secret:` to `openssl rand -hex 32`, put the same value in
`../oauth2-proxy.env` as `OAUTH2_PROXY_CLIENT_SECRET`.

`llamacracy-dex.service` discovers wt0's current address on every start and
passes it to `docker compose up -d` as `DEX_BIND_ADDR` — see the comment at
the top of `docker-compose.yml`. Running `docker compose` directly still
works, but needs that variable exported first.

## Add / remove a user

Edit `config.yaml` `staticPasswords`, then:

```bash
systemctl --user restart llamacracy-dex
```

Nothing else — Llamacracy creates the user row on their first sign-in, keyed on
the Dex `sub` (derived from `userID`, so keep those stable).

## If wt0's address changes (peer re-enrol)

The FQDN keeps working, and so does Dex — just restart the unit and it
re-discovers wt0's current address:

```bash
systemctl --user restart llamacracy-dex
```

(or nothing at all: it re-discovers on every boot too).

## Notes

- `config.yaml` is gitignored (holds the client secret + password hashes +
  your FQDN). `config.yaml.example` is the template.
- Dex data (its SQLite DB — signing keys, auth codes) lives in the
  `dex-data` Docker volume. Back it up if you care about not re-issuing keys;
  losing it just forces everyone to log in again.
- Plain HTTP is deliberate — the WireGuard layer already encrypts it, and
  WebAuthn/passkeys (which would need HTTPS) aren't in play here.
