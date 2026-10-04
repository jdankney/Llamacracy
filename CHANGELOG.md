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

### Fixed

- LaTeX in tool arguments: models write `\frac` and `\theta` with one
  backslash, which JSON reads as a form feed and a tab, and `\int` isn't
  JSON at all. Arguments are repaired before parsing.

- Scrolling up while a reply is being written no longer jumps: the thread
  kept resetting to the top on every redraw, and a small scroll up was
  pulled straight back down. Reopening a chat whose last answer has a
  picture now lands at the bottom.

- Sign-in sometimes didn't come up after a reboot. The user units start
  a few seconds before NetBird creates wt0, so Dex and the auth edge were
  skipped outright, and Docker's own boot-time start of the Dex container
  failed to bind its port and left it running with the port unpublished:
  a later start of the unit didn't fix it, and the auth edge looped on
  "connection refused". Both units now wait for wt0's address and retry
  until it's there, and Dex's container is recreated on every start.

### Upgrading

- For `run_python`: install bubblewrap (`bwrap`), then rerun
  `./deploy/install.sh`, which now builds the sandbox's Python in
  `data/sandbox` (or run `./deploy/setup-sandbox.sh` on its own). Without
  it, everything else works and the tool just isn't offered.
- Add `"tools": true` to the inventory entries that handle tool calls well,
  then rerun `python3 bench/gen_llamaswap_config.py` and restart llamacracy.
- New dependencies (pypdf, openpyxl, defusedxml): run `uv sync`. The
  database migrates itself on start.
- Rerun `./deploy/install.sh` (or copy the two units by hand), then
  `systemctl --user daemon-reload` and restart `llamacracy-dex` and
  `llamacracy-auth`, for the boot fix.

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
