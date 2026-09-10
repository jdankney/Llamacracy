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

docker compose up -d
curl -sf http://myhost.netbird.selfhosted:5556/.well-known/openid-configuration | head -c 80
```

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

`myhost.netbird.selfhosted` keeps working, but the compose **port binding**
is a literal IP. Update it in `docker-compose.yml` to the new
`ip -4 -o addr show wt0` address and `docker compose up -d`.

## Notes

- `config.yaml` is gitignored (holds the client secret + password hashes).
  `config.yaml.example` is the template.
- Dex data (its SQLite DB — signing keys, auth codes) lives in the
  `dex-data` Docker volume. Back it up if you care about not re-issuing keys;
  losing it just forces everyone to log in again.
- Plain HTTP is deliberate — the WireGuard layer already encrypts it, and
  WebAuthn/passkeys (which would need HTTPS) aren't in play here.
