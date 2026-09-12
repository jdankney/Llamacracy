# Dex — Llamacracy's identity provider

NetBird's combined server embeds its own Dex, but it only registers the
dashboard + CLI clients and there's no supported hook to add a third
([netbirdio/netbird#5335](https://github.com/netbirdio/netbird/issues/5335)).
So Llamacracy runs its **own** Dex, here, in Docker on myhost, published on
the NetBird interface only. Login → app all stays on the WireGuard tunnel.

```
friend's browser ──http──► oauth2-proxy (wt0:4180)
        │                        │
        └───auth redirect───► Dex (wt0:5556) ──password check──► staticPasswords
```

## First setup (on myhost)

```bash
cd ~/Documents/Coding/Llamacracy/deploy/dex
cp config.yaml.example config.yaml

# 1. client secret — same value goes in ../oauth2-proxy.env
openssl rand -hex 32                       # paste into config.yaml `secret:`

# 2. one staticPasswords entry per person
./gen-hash.sh 'alice-password'             # paste into a staticPasswords `hash:`

systemctl --user enable --now llamacracy-dex.service
curl -sf http://myhost.netbird.selfhosted:5556/.well-known/openid-configuration | head -c 80
```

`llamacracy-dex.service` (installed by `../install.sh`, or copy it into
`~/.config/systemd/user/` yourself) discovers wt0's current address on every
start and passes it to `docker compose up -d` as `DEX_BIND_ADDR` — see the
comment at the top of `docker-compose.yml`. Running `docker compose` directly
still works, but needs that variable exported first.

Then set `OAUTH2_PROXY_CLIENT_SECRET` in `../oauth2-proxy.env` to the same
`openssl rand -hex 32` value and run `../install.sh` (or, if already installed,
`systemctl --user restart llamacracy-auth`).

## Add / remove a user

Edit `config.yaml` `staticPasswords`, then:

```bash
docker compose restart dex
```

Nothing else — Llamacracy creates the user row on their first sign-in, keyed on
the Dex `sub` (derived from `userID`, so keep those stable).

## If wt0's address changes (peer re-enrol)

`myhost.netbird.selfhosted` keeps working, and so does Dex — just restart
the unit and it re-discovers wt0's current address:

```bash
systemctl --user restart llamacracy-dex
```

(or nothing at all: it re-discovers on every boot too). This used to require
hand-editing a literal IP into `docker-compose.yml`; it doesn't anymore.

## Notes

- `config.yaml` is gitignored (holds the client secret + password hashes).
  `config.yaml.example` is the template.
- Dex data (its SQLite DB — signing keys, auth codes) lives in the
  `dex-data` Docker volume. Back it up if you care about not re-issuing keys;
  losing it just forces everyone to log in again.
- Plain HTTP is deliberate — the WireGuard layer already encrypts it, and
  WebAuthn/passkeys (which would need HTTPS) aren't in play here.
