# Changelog

## 0.2.0 (2026-09-23)

### Added

- **Appearance, per user.** Themes and custom colours (background, text,
  accent, surface) plus a chat text size, under **Account → Appearance**. They
  are saved to the account, so they follow you to other devices.
- **Think toggle** for models that can reason first (`"thinking": true` in the
  inventory). It is off by default. A reply that thought shows "Thought for
  1m 26s"; click it to read the reasoning. A turn with Think on gets its own
  output budget, `THINKING_MAX_TOKENS` (default 8192).
- **Edit a sent message.** The conversation forks instead of being
  overwritten: the new version gets a fresh reply, and ‹ 1 / 2 › switches
  between versions. Compaction is per branch.
- **Continue.dev example** in `examples/continue/`: a dedicated apply model,
  the key kept in a `.env` file, and a rule that stops small models wiping
  files.
- `API_MAX_TOKENS_PER_REQUEST` (default 8192): the output cap on `/v1`.
- CI: ruff, a front-end syntax check and the test suite on every push.

### Changed

- On `/v1`, the client's sampling values win. The inventory only fills in
  what a request leaves out. The web UI still uses the inventory's values.
- `/v1` replies are capped at `API_MAX_TOKENS_PER_REQUEST` rather than the
  2048 chat cap, so an apply model can rewrite a whole file.
- The **Usage** tab is now **Account**.
- The generated llama-swap config no longer forces thinking off with a
  `setParams` filter; Llamacracy decides per request.

### Upgrading from 0.1.0

The database migrates itself on startup; existing conversations become
single-branch trees. The llama-swap config generator changed, so regenerate
it **after** restarting the app:

```bash
git pull
uv sync && ./deploy/install.sh && systemctl --user restart llamacracy
python3 bench/gen_llamaswap_config.py
```

To offer Think on a model, add `"thinking": true` to its inventory entry
before regenerating.

## 0.1.0 (2026-09-23)

First public release, under AGPL-3.0-or-later: the FIFO queue, credit
metering, the chat UI, the admin dashboard, web search, vision, compaction,
the PWA and the OpenAI-compatible `/v1` endpoint with tool calling.
