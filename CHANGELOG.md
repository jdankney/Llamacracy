# Changelog

## Unreleased

Finding, naming and keeping chats, and replies that survive Stop.

### Added

- **Search your chats.** A box above the chat list looks through titles and
  everything said in them, and shows the line that matched. `Ctrl+K` (`⌘K`)
  jumps to it.
- **Rename and download.** The `⋯` next to a chat (or a study session)
  renames it, downloads it as a Markdown file, or deletes it. A title you
  set is never replaced by the model's.
- **Retry.** Under any reply: asks the same thing again and keeps both
  answers, with the `‹ 1 / 2 ›` arrows to flip between them. It uses the
  model picked at the time, so it's also how to ask a second model.
- **Stop keeps what was written.** A reply you stop stays in the chat,
  marked Stopped, instead of disappearing on the next load.
- **Drafts survive a reload.** A message you were still writing is in the
  box when you come back (kept in the browser, cleared on send).
- `Ctrl+Shift+O` starts a new chat.

### Fixed

- **A message whose reply never came could break the next one.** After a
  reply was stopped before its first word, or failed, the next message
  was sent as a second user turn in a row, which several chat templates
  refuse. The two now go as one turn.
- Pressing Enter on a chat's delete button also opened the chat.

### Upgrading

Nothing to do: the one new column (`messages.stopped`) is added at startup.

## 0.4.0 (2026-11-03)

Lock a model in VRAM, chats that name themselves, timestamps, and a
review of the whole app.

### Added

- **About you.** Each person can give their name and a few lines about
  themselves under **Account → About you** (what they do, what they're into,
  how they like answers). It goes to the model with their own chats and
  study sessions, so replies can feel more personal. A switch, on by
  default, stops sending it without deleting it. Nothing is sent until
  someone writes something, and it never goes over `/v1`.
- **Lock a model in VRAM.** Admin → Live → **Model lock**: pick a model and
  it stays loaded. The idle timer stops unloading it, and llama-swap's own
  `ttl` backstop is held off too. Someone using another model still swaps it
  out, as always. Once the queue has been idle for the idle timeout
  (`IDLE_TTL_MINUTES`), the locked model is loaded back. The lock survives restarts. **Unlock**, or
  **Force-unload**, releases it. The top bar's model badge shows a padlock
  while the locked model is resident.
- **Smart chat titles.** After a new chat's first reply, the model that
  wrote it names the chat in a few words ("Sourdough Starter Rescue"),
  instead of the title being the first 50 characters of the message. It
  happens in the same job on the already-loaded model, so it takes about a
  second and is billed like the rest of the reply. If anything goes wrong,
  the old title stays. New setting: `SMART_TITLES` (default on).
- **The markdown and math libraries are bundled** (marked, DOMPurify,
  highlight.js, KaTeX, under `llamacracy/static/assets/vendor/`) instead of
  loaded from cdnjs: chats render with no internet, a slow CDN can't hold the
  page blank, and the page only runs code from your own server.
- **Date and time on every message**, yours and the model's: "Today,
  3:42 PM", "Yesterday, 9:05 AM", "Mon, Oct 5, 3:42 PM". Hover for the full
  timestamp.

### Fixed

From a review of the whole app before this release:

- **Deleting a chat while its reply was being written stopped the queue
  for everyone** until a restart. Deleting now stops that chat's jobs
  first, and a job whose cleanup fails no longer takes the worker with it.
- **Model output could load remote images and draw forms.** A hostile page
  or document could get the model to write an image whose URL carried the
  chat away, or a fake sign-in box. Rendered markdown now keeps no images,
  forms, media or inline styles, and the page sends a Content-Security-Policy.
- **A practice problem's answer reached the browser** in the live trail
  while the tutor wrote it. The answer, solution, code and quote now stay
  on the server until the student has tried.
- **Multiple choice with numeric options** could grade a wrong option right
  ("1" when the answer was the second option, "2").
- **A fresh `.env` copied from `.env.example` stopped the app from
  starting** (`IDLE_TTL_SECONDS=` was read as a number).
- **oauth2-proxy is now v7.15.5** (was v7.6.0, affected by CVE-2025-54576),
  checked against the release's checksum, and upgraded by `install.sh` when
  the pinned version changes. The `/healthz` skip rule is anchored.
- **Stop works at once**, including during a cold load, a long prompt or a
  tool run, and a job stopped while its model loads is billed at the load
  rate. An admin's Kill of a queued job now tells its window, and the
  window recovers if a stream ends without saying how.
- **One person can have at most 4 requests waiting or running**
  (`MAX_JOBS_PER_USER`), so parallel requests can't run far past a credit
  limit. Uncapped users skip it.
- **The admin's Queue impact report** no longer freezes everyone's replies
  while it runs, and is much faster on a long history.
- **Photo turns can use tools again**: a picture's base64 was counted as
  text, which made the turn look full before it started.
- **Tools models aren't refused over documents** as a chat grows: the
  biggest move to lookup instead.
- `/v1`: `n_predict` can no longer lift the output cap, and images must be
  sent as `data:` URLs (llama-server would otherwise fetch links itself).
- Numbers: halves round up when grading (12.5 → 13); the "came from the
  code" check reads 10²³ and SI-spaced thousands, and catches a sign the
  code never printed. A cut-off document says so in its text.
- The UI: New chat and opening another chat wait for the reply; the queue
  connection reconnects after the app restarts; the admin Live tab follows
  the queue; a waiting message's place in line updates; the phone layout of
  message footers fits at 320px.
- Smaller things: search results that aren't web links are dropped, IPv6
  forms of private addresses are refused by the page fetcher, image uploads
  stop reading at the size cap, `gen-hash.sh` asks for the password instead
  of taking it on the command line, `install.sh` finishes a half-done setup
  on its next run and keeps secrets files private (mode 600), and the
  generator runs on Python older than 3.12.

### Upgrading

- Re-run `./deploy/install.sh` (it upgrades oauth2-proxy), then
  `llamacracy restart`.
- The database needs nothing: the new `app_settings` table and an index on
  `jobs` are created on start. Titles of existing chats are left as they are.

## 0.3.0 (2026-10-04)

Documents, a model that looks things up and reads what it finds, a
workspace, a Python sandbox for computing and charts, and Study.

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

- **It does the math with code.** Models that use tools get Python (sympy,
  numpy, scipy, pint for units, matplotlib, networkx) and are told to
  calculate with it rather than in their heads. "Ran Python" in the steps
  above a reply opens the code and its output; charts it draws appear under
  the reply. Below the answer, the server says whether its numbers came from
  that code, and names any that didn't. The code runs in a bubblewrap
  sandbox: no network, no view of the server, a private scratch folder, and
  limits on time, memory, CPU and processes. New settings: `PYTHON_TOOL`,
  `SANDBOX_PYTHON`, `SANDBOX_TIMEOUT_SECONDS` (20), `SANDBOX_MEMORY_MB`
  (1024).
- **Charts from your data.** Spreadsheets attached to a chat (CSV, TSV,
  Excel, JSON), and a course's, are mounted read-only at `/data/<name>` in
  the sandbox, so the model charts and analyses the actual file instead of
  numbers copied into the prompt. The sandbox gains pandas, seaborn, openpyxl
  and Pillow, and chart defaults (150 dpi, constrained layout, light grid)
  from `deploy/sandbox-matplotlibrc`.
- **Diagrams, SVG, and your photos.** The sandbox also has schemdraw
  (circuit diagrams), matplotlib-venn, squarify (treemaps) and wordcloud. A
  picture saved as SVG is shown too: it's converted to PNG with
  `rsvg-convert` inside the sandbox (install librsvg for it). And the chat's
  photos are readable by the code at `/data/img1.png`, `/data/img2.jpg`, ...
  (the names the model already uses for them), so it can crop, resize or
  mark up a picture you sent.
- **Study.** A Study section (at `/study`) where each person adds their own
  courses: a name, a few lines about the class, and its materials (syllabus,
  notes, slides, homework, old exams). A session runs a tutor on a model
  that looks things up: it asks what you know, gives hints before answers,
  works from your materials and names the file and page it used, and says
  when an answer is general knowledge instead. Tick "It uses math or
  numbers" and every number must come from Python. Courses, their materials
  and their sessions are private to their owner, and sessions are listed
  under their course rather than among the chats. New settings: `STUDY`
  (on) and `STUDY_MODEL` (which model a new course starts on).
- **Study modes.** Above the message box in a session: Guide me (hints and
  questions, the default), Explain (the whole idea with a worked example),
  Check my work (finds the first mistake and lets you fix it) and Quiz me
  (questions from your materials, one at a time, with a score). The starter
  prompts pick the matching mode, and a session remembers its last.
- **Quotation check.** When an answer quotes a document, a web page or a
  course's materials, the server looks for each quotation word for word in
  what the model could read (forgiving about case, punctuation, PDF line
  breaks and "…" omissions) and, when it cites a page, on that page. Under
  the answer: a check mark when they all match, or a warning naming any
  that don't, and any on a different page than it says.

- **Practice.** A fifth study mode: the tutor writes a set of problems,
  shown as cards you answer in the chat. Every numeric key in a math course
  comes with the Python that computes it, which the server runs; a problem
  whose code disagrees with its stated answer is sent back before you see
  it (on a live run that caught four wrong keys in one set of five). A key
  drawn from the materials can carry its quote, which must be there word
  for word. Numbers (3.2, 16/5, 3*pi/4, sqrt(2), with or without the unit)
  and multiple choice are graded by the server; written answers show the
  model answer and you mark yourself. Keys stay hidden until you're right
  or ask. The course page shows first-try results by topic.
- **Courses search the web for anything current**, unless turned off per
  course: the tutor is told what its training can't know and to look
  those things up, citing what it finds. An answer that looked nothing up
  says so underneath, more firmly when the question sounds time-sensitive.
- In a study session the tutor has to look something up before it
  answers: on a session's first turn, and on every Quiz and Practice turn.

- **Math renders as it's written.** Equations in a streaming reply render as
  soon as they close, instead of all at once at the end; the one still being
  written shows as dimmed LaTeX until it does.

- **You see the code being written.** A tool call shows up the moment the
  model starts writing it: "Writing Python" with the last few lines of code
  appearing as they're written, which then becomes the step that runs. A
  long program used to leave a silent gap of up to a minute.

### Fixed

- LaTeX in tool arguments: models write `\frac` and `\theta` with one
  backslash, which JSON reads as a form feed and a tab, and `\int` isn't
  JSON at all. Arguments are repaired before parsing.

- Scrolling up while a reply is being written no longer jumps: the thread
  kept resetting to the top on every redraw, and a small scroll up was
  pulled straight back down. Reopening a chat whose last answer has a
  picture now lands at the bottom.

- The auth edge listened on port 80, not the 4180 everything else (the
  installer, the example redirect URL, the docs) expects. It's 4180 again,
  and `LLAMACRACY_AUTH_PORT` in `deploy/oauth2-proxy.env` changes it.

- A turn that wrote a long program could break: the reply budget (shared
  by every step) ran out partway through a tool call, the half-written call
  was run anyway and failed, and each retry was cut off at the same place
  until the turn gave up with no answer. A call cut off by the budget is
  now shown as cut off and not run, the turn answers once its budget is
  nearly spent, and turns that can run code get the thinking-sized budget
  (`THINKING_MAX_TOKENS`), since one chart program can be 1,500 tokens.

- Sign-in sometimes didn't come up after a reboot. The user units start
  a few seconds before NetBird creates wt0, so Dex and the auth edge were
  skipped outright, and Docker's own boot-time start of the Dex container
  failed to bind its port and left it running with the port unpublished:
  a later start of the unit didn't fix it, and the auth edge looped on
  "connection refused". Both units now wait for wt0's address and retry
  until it's there, and Dex's container is recreated on every start.

### Upgrading from 0.2.0

Before updating:

- For `run_python`, install bubblewrap (`bwrap`) and rsvg-convert (package
  `librsvg` or `librsvg2-bin`). Without bubblewrap everything else works and
  the tool just isn't offered; without rsvg-convert, pictures a model saves
  as SVG aren't shown.
- If your auth edge is on port 80 on purpose (that was the shipped unit's
  port), add `LLAMACRACY_AUTH_PORT=80` to `deploy/oauth2-proxy.env`.
- Add `"tools": true` to the inventory entries that handle tool calls well:
  they get searching, reading, the workspace and `run_python`.

Then:

```bash
git pull
uv sync && ./deploy/install.sh     # new dependencies, the sandbox, the units
python3 bench/gen_llamaswap_config.py
systemctl --user restart llamacracy llamacracy-dex llamacracy-auth
```

The database migrates itself on start. `install.sh` builds the sandbox's
Python in `data/sandbox`; `./deploy/setup-sandbox.sh` rebuilds just that.

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
