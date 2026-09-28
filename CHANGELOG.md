# Changelog

## Unreleased

### Added

- **Documents.** Attach PDFs, Word (.docx), LibreOffice (.odt, .ods, .odp),
  Excel (.xlsx), PowerPoint (.pptx), text, Markdown, CSV, HTML and source
  files, up to five per message, on any model. Use the paperclip, drag them
  onto the message box, or paste. The text is extracted once, at upload,
  in a sandboxed child process. A document stays part of the conversation,
  and clicking its name shows exactly the text the model reads. Before you
  send, the context ring counts it and a note says roughly how long the model
  will take to read it. New settings: `DOCUMENT_MAX_MB` (25) and
  `DOCUMENT_MAX_CHARS` (1,200,000).
- **The model runs its own searches.** With Search on, a model marked
  `"tools": true` in the inventory decides what to look up, searches again
  if the first results miss, and cites numbered sources (the `[1]` links
  go to the page). A live trail shows each step, and stays folded above the
  reply afterwards. The whole turn is one queued job, bounded by
  `AGENT_MAX_STEPS` (6) and `AGENT_MAX_SECONDS` (240). Models without the
  flag keep the one-search behaviour. The Help page has a new "Looks things
  up" column.
- **Reading pages and long documents.** With Search on, those models can
  also open a result (or any link, including an online PDF) and read it, a
  part at a time. Pages are fetched only from public addresses, checked on
  every redirect, so a model can't be steered into the local network. A
  document too big to send whole (over `DOC_INLINE_FRACTION` of the model's
  context, default half) now works on those models instead of being refused:
  they get a short stand-in and look through it with outline, search and
  read tools, quoting page numbers. Documents a compaction folded away stay
  reachable the same way.
- **Files.** Turn on Files and a model that uses tools can save its work
  (a draft, a plan, CSV, code) to the chat's workspace, and change it when
  you ask ("make the intro shorter") with an exact edit rather than a
  rewrite. Files appear under the reply that wrote them, open in a viewer,
  and download as plain text; the folder button lists them all. Every write
  is kept as a version tied to its message, so files fork with the chat:
  an edited message's branch starts from the files as they were.
- **Pictures in agent turns.** A tool-using model with a vision variant can
  look again at a photo from earlier in the chat (it used to see a picture
  only in the message it came with), and with Search on it can find pictures
  on the web and look at them. Such turns run on the `-vision` entry, and
  once that entry is loaded, plain turns stay on it instead of swapping the
  model back. `AGENT_IMAGES=false` turns this off.

### Upgrading

- Add `"tools": true` to the inventory entries that handle tool calls well,
  then rerun `python3 bench/gen_llamaswap_config.py` and restart llamacracy.
- New dependencies (pypdf, openpyxl, defusedxml): run `uv sync`. The
  database migrates itself on start.

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
- The built-in `ELECTRICITY_RATE` default is now 0.30, matching
  `.env.example`. It only matters if your `.env` doesn't set a rate.
- Docs audit: after changing models, restart only `llamacracy` (llama-swap
  reloads its own config), and the Help page now says correctly that reading
  your message is part of the billed time.

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
