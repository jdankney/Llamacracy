# SPDX-License-Identifier: AGPL-3.0-or-later
"""The queue -- the core of the project.

Strict global FIFO. Exactly one inference runs at a time. A single async worker
task drains the queue; every submission goes through it. No parallelism, no
reordering, no priority, no coalescing.

    queued -> loading_model -> generating -> done | error | cancelled | limit_exceeded

Live queue state is pushed to every subscriber over SSE. The currently-loaded
model is always sourced from llama-swap's /running, never guessed.

SMALL_MODEL_FAST_LANE (default off) would add a second concurrent lane for
small models; jobs already carry a `lane` and only exclusive-lane seconds are
full price. The second lane is not implemented yet -- everything is exclusive.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from . import quotes, verify
from .config import Settings, get_settings
from .db import Database, get_db, now
from .power import GpuPowerSampler
from .registry import Registry, get_registry
from .tools import Toolbox, parse_args
from .upstream import Upstream, UpstreamError, UsageSniffer, get_upstream

log = logging.getLogger("llamacracy.queue")

ANSWER_ROOM_TOKENS = 2048   # context an agent keeps free for its final answer
# Below this much budget left, a step can't write a useful tool call (a short
# program is a few hundred tokens): answer instead of starting one that the
# cap cuts off mid-call.
STEP_FLOOR_TOKENS = 384
DRAFT_EVERY_S = 0.15        # how often a tool call being written is shown
# Sent after the tool results when a limit ends the turn. tool_choice "none"
# alone isn't enough: some chat templates still let the model write a tool
# call out as plain text, which would then be the answer.
FINAL_NUDGE = ("(No more tool calls this turn. Write your final answer now from the results "
               "above; if they weren't enough, say what you found and what's still missing.)")
CUT_NUDGE = ("(Your last tool call was cut off: the reply ran out of room before you finished "
             "writing it, so it didn't run. Don't say it ran or describe its output.)")
# naming a new chat (QueueManager._name_chat)
TITLE_ASK = ("Give this conversation a short title for a chat history list: 3 to 6 words, in "
             "the conversation's language. Reply with the title only: no quotes, no full stop, "
             "no explanation.")
TITLE_MAX_TOKENS = 32
TITLE_TIMEOUT_S = 20.0
TITLE_MAX_CHARS = 60


class JobState(enum.StrEnum):
    QUEUED = "queued"
    LOADING_MODEL = "loading_model"
    GENERATING = "generating"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"


TERMINAL = {JobState.DONE, JobState.ERROR, JobState.CANCELLED, JobState.LIMIT_EXCEEDED}


@dataclass
class Job:
    user_id: int
    owner_name: str
    owner_email: str
    model_id: str
    payload: dict                      # OpenAI chat payload (messages + sampling)
    conversation_id: str | None = None
    parent_message_id: int | None = None  # the user message this reply answers
    # /v1 passthrough: forward upstream's bytes to the caller untouched instead
    # of decoding them into token events. Billing still comes off the same
    # clocks; only the shape of what crosses the wire differs.
    raw_passthrough: bool = False
    # Agent turn: the model may call these tools, and the job keeps the slot
    # while it does (see _pump_agent). None = one plain inference.
    agent: Toolbox | None = None
    # a new chat's first turn: once the reply is written, the same model names
    # the chat. This is the stand-in title it has until then (see _name_chat).
    title_from: str | None = None
    session_id: int | None = None       # session active at enqueue (set by metering)
    lane: str = "exclusive"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: JobState = JobState.QUEUED

    queued_at: float = field(default_factory=now)
    picked_at: float | None = None
    load_started_at: float | None = None
    gen_started_at: float | None = None
    finished_at: float | None = None

    load_seconds: float = 0.0
    gen_seconds: float = 0.0
    occupancy_seconds: float = 0.0
    cold_start: bool = False

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_estimated: bool = False
    last_timings: dict | None = None
    error: str | None = None

    # billing (filled in by metering)
    credits: float = 0.0
    cost_usd: float = 0.0
    rate_used: float | None = None
    gpu_watts_mean: float | None = None

    content_parts: list[str] = field(default_factory=list)
    # thinking (reasoning) the model did before answering: kept for display
    # and timed from its first token to the first token of the answer
    reasoning_parts: list[str] = field(default_factory=list)
    think_started_at: float | None = None
    think_ended_at: float | None = None
    think_accum_s: float = 0.0          # earlier thinking spans (an agent thinks between steps)
    # agent turns: what the model did on the way to its answer, shown as a
    # trail above it -- [{"type": "tool", ...} | {"type": "note", "text": ...}]
    trail: list[dict] = field(default_factory=list)
    context_tokens: int | None = None   # the conversation's size when the turn began
    _chunks: asyncio.Queue = field(default_factory=asyncio.Queue)
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)
    _done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def content(self) -> str:
        # a model told it may not call tools sometimes writes the call out as
        # text (some templates' <tool_call><function=...> markup): that isn't an answer
        return _LEAKED_CALL.sub("", "".join(self.content_parts)).strip()

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)

    @property
    def thinking_seconds(self) -> float | None:
        if self.think_started_at is None:
            return None
        end = self.think_ended_at or self.finished_at or now()
        return round(max(0.0, self.think_accum_s + end - self.think_started_at), 1)

    def tools_record(self) -> dict | None:
        """The agent's trail and numbered sources, as stored on the reply."""
        if self.agent is None:
            return None
        rec = {"steps": self.trail, "sources": self.agent.sources}
        if self.agent.workspace is not None and self.agent.workspace.changed:
            rec["files"] = list(self.agent.workspace.changed.values())
        if self.agent.figures:
            rec["figures"] = list(self.agent.figures)
        if self.agent.practice:
            rec["practice"] = list(self.agent.practice)     # cards; their keys stay server-side
        if self.agent.python_outputs:
            # which of the answer's numbers came out of the code it ran
            # given: what the user wrote, and the constants the code was written with
            # (m = 1.673e-27), which are in plain view in the code
            givens = [_text_of(m.get("content")) for m in self.payload.get("messages", [])
                      if m.get("role") == "user"] + self.agent.python_code
            chk = verify.check(self.content, self.agent.python_outputs, givens)
            if chk:
                rec["check"] = chk
        if self.agent.study:
            # nothing looked up: the answer is the model's memory, as of its training
            looked = {"doc_outline", "doc_search", "doc_read", "web_search", "read_page", "image_search"}
            if not any(s.get("name") in looked for s in self.trail if s.get("type") == "tool"):
                asked = _text_of((self.payload.get("messages") or [{}])[-1].get("content"))
                rec["memory"] = {"timely": bool(_TIMELY.search(asked))}
        if self.agent.documents or self.agent.seen:
            # are its quotations word for word in what it could read?
            said = [_text_of(m.get("content")) for m in self.payload.get("messages", [])
                    if m.get("role") == "user"]
            q = quotes.check(self.content, self.agent.documents,
                             self.agent.seen + said)
            if q:
                rec["quotes"] = q
        return rec

    def public(self, position: int) -> dict:
        return {
            "id": self.id,
            "owner": self.owner_name,
            "model": self.model_id,
            "state": self.state.value,
            "position": position,
            "cold_start": self.cold_start,
            "queued_at": self.queued_at,
            "gen_started_at": self.gen_started_at,
        }


class QueueManager:
    def __init__(self, db: Database, upstream: Upstream, registry: Registry,
                 settings: Settings):
        self.db = db
        self.up = upstream
        self.reg = registry
        self.cfg = settings
        self._pending: list[Job] = []
        self._active: Job | None = None
        self._by_id: dict[str, Job] = {}
        self._wake = asyncio.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._loaded_model: str | None = None
        self._idle_since: float = time.time()
        self._load_ewma: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()   # fire-and-forget persists; kept referenced until done
        self._idle_poll_s = 10.0    # cheap local check; keeps a short IDLE_TTL responsive
        self._running_poll_s = 10.0
        # The GPU slot. The worker holds it for a whole job; the idle monitor
        # takes it to preload or keep alive the locked model, so neither can
        # ever talk to llama-swap while the other is mid-request.
        self._slot = asyncio.Lock()
        # Admin lock: this model stays in VRAM. The idle timer reloads it
        # instead of unloading, and a keepalive holds off llama-swap's ttl.
        self.locked_model: str | None = None
        self._keepalive_s = 60.0          # well inside llama-swap's ttl backstop
        self._touched_at = 0.0
        self._touch_retry_at = 0.0        # after a failed preload, wait before the next try
        self._preload_now: str | None = None   # just locked: load it once the queue is empty
        self._arrived = asyncio.Event()   # a job was submitted (cuts a preload short)
        self._keepalive_timeout_s = 10.0
        self._touch_backoff_s = 300.0
        # hook points filled in by metering
        self.check_limits = None       # async (user_id, model_id) -> LimitDecision | None
        self.finalize_billing = None   # async (job) -> None

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        rows = await self.db.fetch_all("SELECT model_id, ewma_load_seconds FROM model_load_stats")
        self._load_ewma = {r["model_id"]: r["ewma_load_seconds"] for r in rows}
        for m in self.reg.all():
            self._load_ewma.setdefault(m.key, m.seed_cold_load_s)
        row = await self.db.fetch_one("SELECT value FROM app_settings WHERE key = 'locked_model'")
        locked = json.loads(row["value"]) if row else None
        if locked is not None and not self.lockable(locked):
            # the model left the inventory since it was locked
            log.warning("locked model %s is no longer in the inventory; lock released", locked)
            await self._save_lock(None)
            locked = None
        self.locked_model = locked
        self._loaded_model = await self.up.loaded_model()
        self._tasks = [
            asyncio.create_task(self._worker(), name="queue-worker"),
            asyncio.create_task(self._idle_monitor(), name="idle-monitor"),
            asyncio.create_task(self._running_poller(), name="running-poller"),
        ]
        log.info("queue started; loaded model = %s", self._loaded_model)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    # -- estimates ----------------------------------------------------------
    def load_estimate(self, model_id: str) -> float:
        return round(self._load_ewma.get(model_id, 10.0), 1)

    def _update_load_ewma(self, model_id: str, observed: float) -> None:
        prev = self._load_ewma.get(model_id, observed)
        val = 0.7 * prev + 0.3 * observed
        self._load_ewma[model_id] = val
        t = asyncio.create_task(self._persist_load_ewma(model_id, val))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _persist_load_ewma(self, model_id: str, val: float) -> None:
        await self.db.execute(
            "INSERT INTO model_load_stats (model_id, ewma_load_seconds, samples, updated_at) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET "
            "  ewma_load_seconds = excluded.ewma_load_seconds, "
            "  samples = model_load_stats.samples + 1, "
            "  updated_at = excluded.updated_at",
            (model_id, val, now()),
        )

    # -- submission / cancellation ----------------------------------------
    async def submit(self, job: Job) -> None:
        self._by_id[job.id] = job
        self._pending.append(job)
        await self._persist_job(job)
        self._wake.set()
        self._arrived.set()
        await self._broadcast()
        log.info("job %s queued (user=%s model=%s pos=%d)",
                 job.id, job.owner_email, job.model_id, len(self._pending))

    async def cancel(self, job_id: str, user_id: int) -> bool:
        job = self._by_id.get(job_id)
        if job is None or job.user_id != user_id:
            return False
        if job is self._active:
            job._cancel.set()
            return True
        if job in self._pending:
            await self.drop_queued(job)
            return True
        return False

    async def drop_queued(self, job: Job) -> None:
        """Take a job that never ran out of the queue: the one way it's done
        (a user's Stop, an admin's Kill, a deleted chat), so every waiter
        hears the end of it -- the stream gets `cancelled` and its close, and
        anything awaiting `_done` (compaction) is released."""
        if job not in self._pending:
            return
        self._pending.remove(job)
        job.state = JobState.CANCELLED
        job.finished_at = now()
        await self._emit(job, {"type": "cancelled"})
        await job._chunks.put(None)
        job._done.set()
        await self._persist_job(job)
        self._forget_later(job)
        await self._broadcast()

    async def drop_conversation(self, conv_id: str) -> None:
        """A conversation is being deleted: its queued jobs go, and a running
        one is stopped and cut loose from it, so its reply (or title) is
        never written back into a conversation that no longer exists."""
        for job in [j for j in self._pending if j.conversation_id == conv_id]:
            await self.drop_queued(job)
        job = self._active
        if job is not None and job.conversation_id == conv_id:
            job.conversation_id = None
            job._cancel.set()

    def _forget_later(self, job: Job, after_s: float = 120.0) -> None:
        # finished jobs leave the index once a late cancel can't need them:
        # they hold their whole payload (images, documents) until then
        asyncio.get_running_loop().call_later(after_s, self._by_id.pop, job.id, None)

    def snapshot(self) -> dict:
        jobs = []
        if self._active is not None:
            jobs.append(self._active.public(0))
        for i, j in enumerate(self._pending, start=1):
            jobs.append(j.public(i))
        return {
            "type": "queue",
            "loaded_model": self._loaded_model,
            "locked_model": self.locked_model,
            "depth": len(self._pending) + (1 if self._active else 0),
            "jobs": jobs,
            "ts": now(),
        }

    def jobs_of(self, user_id: int) -> int:
        """How many of this user's jobs are queued or running."""
        active = self._active is not None and self._active.user_id == user_id
        return int(active) + sum(1 for j in self._pending if j.user_id == user_id)

    def position_of(self, job_id: str) -> int | None:
        if self._active and self._active.id == job_id:
            return 0
        for i, j in enumerate(self._pending, start=1):
            if j.id == job_id:
                return i
        return None

    # -- SSE fan-out ------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        q.put_nowait(self.snapshot())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _broadcast(self) -> None:
        snap = self.snapshot()
        for q in list(self._subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                self._subscribers.discard(q)

    async def _emit(self, job: Job, event: dict) -> None:
        await job._chunks.put(event)

    # -- worker ---------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            while not self._pending:
                self._wake.clear()
                await self._wake.wait()
            # the job stays queued (and unbilled) while a preload holds the slot
            async with self._slot:
                if self._pending:
                    try:
                        await self._take(self._pending.pop(0))
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 -- the worker must outlive any one job
                        log.exception("queue worker: a job's cleanup failed")

    async def _take(self, job: Job) -> None:
        """Run one job in the slot, start to finish."""
        if job.state in TERMINAL:
            return
        self._active = job
        job.picked_at = now()
        await self._broadcast()
        try:
            await self._run(job)
        except UpstreamError as e:
            job.state = JobState.ERROR
            job.error = e.detail[:500]
            await self._emit(job, {"type": "error", "detail": str(e)})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", job.id)
            job.state = JobState.ERROR
            job.error = repr(e)
            await self._emit(job, {"type": "error", "detail": repr(e)})
        finally:
            self._active = None
            self._idle_since = time.time()
            try:
                await self._finalize(job)
            except Exception:  # noqa: BLE001 -- the waiters below must still hear the end
                log.exception("finalizing job %s failed", job.id)
            finally:
                job._done.set()
                await job._chunks.put(None)
                self._forget_later(job)
                await self._broadcast()

    async def _mark_generating(self, job: Job, t_start: float,
                               sampler: GpuPowerSampler) -> None:
        """First real output seen: stop the cold-load clock, flip the job to
        generating and start sampling GPU power. Shared by both pumps so a /v1
        passthrough is billed off exactly the same timers as a web chat."""
        t_first = now()
        job.gen_started_at = t_first
        self._loaded_model = job.model_id
        if job.cold_start:
            # true load time = wall-to-first-token minus llama.cpp's
            # measured prompt-eval; refined again from final timings below
            job.load_seconds = max(0.0, t_first - t_start)
        job.state = JobState.GENERATING
        sampler.start()
        await self._broadcast()

    async def _on_reasoning(self, job: Job, text: str) -> None:
        if job.think_started_at is None:
            job.think_started_at = now()
        elif job.think_ended_at is not None:
            # thinking again after a tool call: the clock picks up where it stopped
            job.think_accum_s += job.think_ended_at - job.think_started_at
            job.think_started_at, job.think_ended_at = now(), None
        job.reasoning_parts.append(text)
        await self._emit(job, {"type": "reasoning", "text": text})

    async def _end_thinking(self, job: Job) -> None:
        """Output that isn't thinking has started: the UI can swap
        "Thinking..." for "Thought for Ns" right now."""
        if job.think_started_at is not None and job.think_ended_at is None:
            job.think_ended_at = now()
            await self._emit(job, {"type": "thought", "seconds": job.thinking_seconds})

    async def _pump_chat(self, job: Job, t_start: float,
                         sampler: GpuPowerSampler) -> None:
        """The web UI's path: decode upstream chunks into token events."""
        async for chunk in self.up.stream_chat({**job.payload, "model": job.model_id}):
            if job._cancel.is_set():
                break
            if job.gen_started_at is None and (chunk.content_delta or chunk.reasoning_delta):
                await self._mark_generating(job, t_start, sampler)
            if chunk.reasoning_delta:
                await self._on_reasoning(job, chunk.reasoning_delta)
            if chunk.content_delta:
                await self._end_thinking(job)
                job.content_parts.append(chunk.content_delta)
                await self._emit(job, {"type": "token", "text": chunk.content_delta})
            if chunk.usage:
                job.prompt_tokens = chunk.usage.get("prompt_tokens")
                job.completion_tokens = chunk.usage.get("completion_tokens")
            if chunk.timings:
                job.last_timings = chunk.timings

    async def _pump_agent(self, job: Job, t_start: float,
                          sampler: GpuPowerSampler) -> None:
        """An agent turn: infer, run whatever tools the model asked for,
        append their results, infer again -- until it answers without asking
        for a tool, or a limit says answer now.

        It is ONE job holding the exclusive slot for the whole turn. Handing
        the slot back between steps would let another user's prompt evict
        llama.cpp's cache (-np 1), and every step would then re-read the whole
        conversation from scratch. Time spent waiting on a tool is therefore
        billed like the rest of the turn: nobody else could use the GPU then.

        Limits: `max_steps` rounds of tool calls, a wall-clock deadline, and
        the model's context window. Hitting any of them sends one last request
        with tool_choice "none", a note saying so, and thinking off, so the
        turn always ends in an answer. The output budget (max_tokens) is
        shared by every step, which also keeps the overshoot past a credit
        limit bounded. With Think on, a step may not think its way through
        the whole budget: it is cut at the budget less an answer's worth, or
        at the deadline, and the final request answers instead.
        """
        box = job.agent
        assert box is not None
        messages = list(job.payload["messages"])
        budget = int(job.payload.get("max_tokens") or 2048)
        ctx = self.reg.require(job.model_id).ctx
        ctk = job.payload.get("chat_template_kwargs") or {}
        thinking = bool(ctk.get("enable_thinking"))
        t0 = now()
        out_tokens = prompt_total = 0
        cut_off = False                 # the last step ran out of room or time mid-thought
        cut_call = False                # ...or mid-way through writing a tool call
        for step in range(box.max_steps + 1):
            # stop calling tools while there is still room to answer: an
            # answer's worth, not the whole (thinking-sized) output budget
            est_prompt = _estimate_prompt(messages)
            room = min(budget - out_tokens, ANSWER_ROOM_TOKENS)
            final = (cut_off or step == box.max_steps or now() - t0 > box.deadline_s
                     or est_prompt + room > ctx
                     or (step > 0 and budget - out_tokens < STEP_FLOOR_TOKENS))
            left = budget - out_tokens
            if thinking and not final:
                left -= min(ANSWER_ROOM_TOKENS, budget // 4)
            payload = {**job.payload, "messages": messages, "model": job.model_id,
                       "tools": box.schemas(), "max_tokens": max(256, left)}
            if final:
                payload["tool_choice"] = "none"
                if step:
                    nudge = FINAL_NUDGE + (" " + CUT_NUDGE if cut_call else "")
                    payload["messages"] = [*messages, {"role": "user", "content": nudge}]
                if thinking:
                    payload["chat_template_kwargs"] = {**ctk, "enable_thinking": False}
            elif step == 0 and box.lookup_first:
                # it has to look something up before it may answer at all
                payload["tool_choice"] = "required"
            calls: dict[int, dict] = {}
            drafts: dict[int, str] = {}     # the code shown so far, per call being written
            last_draft = 0.0
            text: list[str] = []
            reasoning: list[str] = []
            finish = None
            closed = False
            # aclosing: leaving the loop early must close the HTTP stream at
            # once, or llama.cpp keeps generating and the next step queues
            # behind it on the single slot
            async with contextlib.aclosing(self.up.stream_chat(payload)) as stream:
                async for chunk in stream:
                    if job._cancel.is_set():
                        break
                    if (not final and chunk.reasoning_delta and not text and not calls
                            and now() - t0 > box.deadline_s):
                        cut_off = closed = True     # still thinking at the deadline: answer instead
                        break
                    finish = chunk.finish_reason or finish
                    if job.gen_started_at is None and (chunk.content_delta or chunk.reasoning_delta
                                                       or chunk.tool_call_deltas):
                        await self._mark_generating(job, t_start, sampler)
                    if chunk.reasoning_delta:
                        reasoning.append(chunk.reasoning_delta)
                        await self._on_reasoning(job, chunk.reasoning_delta)
                    if chunk.content_delta:
                        await self._end_thinking(job)
                        text.append(chunk.content_delta)
                        job.content_parts.append(chunk.content_delta)
                        await self._emit(job, {"type": "token", "text": chunk.content_delta})
                    for d in chunk.tool_call_deltas or []:
                        await self._end_thinking(job)
                        c = calls.setdefault(d.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        fn = d.get("function") or {}
                        c["id"] = d.get("id") or c["id"]
                        c["name"] += fn.get("name") or ""
                        c["arguments"] += fn.get("arguments") or ""
                    if chunk.tool_call_deltas and now() - last_draft >= DRAFT_EVERY_S:
                        last_draft = now()
                        await self._emit_drafts(job, step, calls, drafts)
                    if chunk.usage:
                        if step == 0:
                            job.context_tokens = chunk.usage.get("prompt_tokens")
                        prompt_total += chunk.usage.get("prompt_tokens") or 0
                        out_tokens += chunk.usage.get("completion_tokens") or 0
                        job.prompt_tokens, job.completion_tokens = prompt_total, out_tokens
                    if chunk.timings and job.last_timings is None:
                        # only the first step's timings: the cold-load split in
                        # _run subtracts that step's prompt processing
                        job.last_timings = chunk.timings
            if calls and not job._cancel.is_set():
                await self._emit_drafts(job, step, calls, drafts)
            if closed:
                # no usage chunk arrives from a stream we closed: count what it thought
                out_tokens += int(len("".join(reasoning)) / 3.6)
                job.completion_tokens = out_tokens
            elif not final and not calls and not text and finish == "length":
                cut_off = True          # thought through its share of the budget
            elif not final and calls and finish == "length":
                # the cap cut it off mid-call: its arguments are half a JSON
                # object. Running it would only fail ("bad arguments"), and a
                # retry with what's left of the budget is cut at the same
                # place, so show it as cut off and answer instead.
                cut_off = cut_call = True
                said = "".join(text).rsplit("</think>", 1)[-1].strip()
                if said:
                    job.trail.append({"type": "note", "text": said})
                job.content_parts.clear()
                for i in sorted(calls):
                    c = calls[i]
                    c["id"] = c["id"] or f"call_{step}_{i}"
                    args = _loose_json(c["arguments"])
                    label = box.label(c["name"], args)
                    await self._emit(job, {"type": "tool_start", "id": c["id"], "name": c["name"],
                                           "label": label, "said": said})
                    said = ""
                    rec = {"type": "tool", "id": c["id"], "name": c["name"], "label": label,
                           "args": box.shown_args(c["name"], args), "ok": False,
                           "summary": "cut off", "ms": 0}
                    job.trail.append(rec)
                    await self._emit(job, {**rec, "type": "tool_end", "sources": []})
            if cut_off and not final and not job._cancel.is_set():
                continue
            if job._cancel.is_set() or not calls or final:
                return

            # a tool step: whatever it said first was a preamble, not the answer.
            # With thinking off, some models still think a line and close it with
            # a bare </think> that llama.cpp leaves in the content: drop that part.
            said = "".join(text).rsplit("</think>", 1)[-1].strip()
            if said:
                job.trail.append({"type": "note", "text": said})
            job.content_parts.clear()
            ordered = [calls[i] for i in sorted(calls)]
            for i, c in enumerate(ordered):
                c["id"] = c["id"] or f"call_{step}_{i}"
            turn = {"role": "assistant", "content": said or None,
                    "tool_calls": [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"],
                                                 "arguments": _echo_args(c["arguments"])}}
                                   for c in ordered]}
            if reasoning:
                # templates that keep a turn's reasoning across its tool calls read this
                turn["reasoning_content"] = "".join(reasoning)
            messages.append(turn)
            seen: list[tuple[str, str]] = []       # (name the model used, data URL)
            for c in ordered:
                if job._cancel.is_set():
                    return
                args = _loose_json(c["arguments"])
                label = box.label(c["name"], args)
                await self._emit(job, {"type": "tool_start", "id": c["id"], "name": c["name"],
                                       "label": label, "said": said})
                said = ""
                t_tool = now()
                res = await box.run(c["name"], c["arguments"])
                step_rec = {"type": "tool", "id": c["id"], "name": c["name"], "label": label,
                            "args": box.shown_args(c["name"], args), "ok": res.ok,
                            "summary": res.summary,
                            "ms": round((now() - t_tool) * 1000), **res.extra}
                seen += [(str(args.get("image") or "image"), u) for u in res.images]
                job.trail.append(step_rec)
                await self._emit(job, {**step_rec, "type": "tool_end", "sources": res.sources})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": res.text})
            if box.max_steps - step - 1 == 1:
                # models that don't count their own calls search on and on otherwise
                messages[-1]["content"] += ("\n\n(You have one more round of tool calls this "
                                            "turn; after that you must answer.)")
            if seen:
                # a tool result is text only; pictures the model asked to see
                # arrive as the next user turn, the one place images can go
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "The picture(s) you asked to see: "
                                             + ", ".join(name for name, _ in seen)},
                    *({"type": "image_url", "image_url": {"url": u}} for _, u in seen)]})

    async def _emit_drafts(self, job: Job, step: int, calls: dict[int, dict],
                           drafts: dict[int, str]) -> None:
        """Show tool calls while the model is still writing them: a long
        program takes as long to write as a long answer, and without this the
        step only appears once it's finished. `tool_draft` carries the name
        and, for code, the newly written part of it."""
        box = job.agent
        for i in sorted(calls):
            c = calls[i]
            if not c["name"]:
                continue
            code = _draft_code(c["arguments"]) if box is None or box.shows_code(c["name"]) else ""
            shown = drafts.get(i)
            if shown is None or len(code) > len(shown):
                drafts[i] = code
                await self._emit(job, {"type": "tool_draft", "key": f"{step}.{i}", "name": c["name"],
                                       "delta": code[len(shown or ""):]})

    async def _pump_raw(self, job: Job, t_start: float,
                        sampler: GpuPowerSampler) -> None:
        """The /v1 path: forward upstream's bytes verbatim as `raw` events and
        read nothing out of them but the numbers the job row needs. Tool calls,
        logprobs, extra choices -- none of it has to be understood here to
        survive the trip, which is the whole point of the passthrough.

        `job.content_parts` stays empty: a /v1 job has no conversation to
        persist, so the reply is never reassembled on this side at all.
        """
        payload = {**job.payload, "model": job.model_id}
        sniff = UsageSniffer()

        if payload.get("stream"):
            async for raw in self.up.stream_raw(payload):
                if job._cancel.is_set():
                    break
                sniff.feed(raw)
                if job.gen_started_at is None and sniff.saw_output:
                    await self._mark_generating(job, t_start, sampler)
                await self._emit(job, {"type": "raw", "data": raw})
        else:
            # No first-token edge to watch for, so sample across the whole
            # upstream call and back-date the generation start from llama.cpp's
            # own timings below -- the postamble then derives load/gen seconds
            # from gen_started_at exactly as it does for a streamed job.
            job.state = JobState.GENERATING
            sampler.start()
            await self._broadcast()
            body = await self.up.post_raw(payload)
            sniff.feed_body(body)
            self._loaded_model = job.model_id
            await self._emit(job, {"type": "raw", "data": body})

        if sniff.usage:
            job.prompt_tokens = sniff.usage.get("prompt_tokens")
            job.completion_tokens = sniff.usage.get("completion_tokens")
        if sniff.timings:
            job.last_timings = sniff.timings
        if job.gen_started_at is None and sniff.timings:
            # Nothing marked the start of generation: either a non-streamed
            # call, or a stream whose output we could not recognise. Timings
            # mean the request did finish, so back-date from llama.cpp's own
            # measurement and let the postamble split load from gen as usual.
            # Without timings it is left None, so a job cancelled mid-load
            # never feeds a bogus sample into the load EWMA.
            predicted_s = (sniff.timings.get("predicted_ms") or 0) / 1000.0
            job.gen_started_at = max(t_start, now() - predicted_s)

    async def _name_chat(self, job: Job) -> None:
        """A new chat's title, written by the model that just answered, while
        it still holds the slot: no second model and no load, about a second.

        The request is the conversation so far plus the reply and one more
        question, so llama.cpp's prompt cache already holds all of it but the
        question, and the chat's next turn still finds its history cached.
        For that, the prompt must start the same as the turn's own did: an
        agent turn's tools go along (with tool_choice "none"), since chat
        templates write the tool list near the top. A picture is left out
        rather than read again; its text stays, as the next turn's history
        has it. Anything that goes wrong leaves the stand-in title; nothing
        here can fail the reply."""
        msgs = [{**m, "content": _text_of(m.get("content"))}
                if isinstance(m.get("content"), list) else m
                for m in job.payload.get("messages") or []]
        convo = [*msgs, {"role": "assistant", "content": job.content},
                 {"role": "user", "content": TITLE_ASK}]
        payload = {"model": job.model_id, "messages": convo,
                   "max_tokens": TITLE_MAX_TOKENS, "temperature": 0.3}
        if job.agent is not None:
            payload["tools"] = job.agent.schemas()
            payload["tool_choice"] = "none"
        m = self.reg.get(job.model_id)
        if m is not None and m.thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        async def ask() -> str:
            out: list[str] = []
            async with contextlib.aclosing(self.up.stream_chat(payload)) as stream:
                async for chunk in stream:
                    if job._cancel.is_set():
                        return ""
                    out.append(chunk.content_delta or "")
            return "".join(out)

        try:
            title = clean_title(await asyncio.wait_for(ask(), TITLE_TIMEOUT_S))
        except Exception as e:  # noqa: BLE001 -- a title is never worth a failed reply
            log.warning("naming chat %s failed: %r", job.conversation_id, e)
            return
        if not title or job.conversation_id is None:
            return
        # only over the stand-in: a title the user set meanwhile stays theirs
        await self.db.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND title = ?",
            (title, job.conversation_id, job.title_from))
        await self._emit(job, {"type": "title", "conversation_id": job.conversation_id,
                               "title": title})

    def _default_thinking_off(self, job: Job) -> None:
        """Every request to a model that can reason says explicitly whether it
        should. Endpoints set it when the user asked; anything that didn't say
        gets thinking OFF here, so no code path (compaction, an API client, a
        future endpoint) can make a model think by accident. This used to be a
        llama-swap setParams filter, but a filter overrides the request, which
        made the Think toggle impossible."""
        m = self.reg.get(job.model_id)
        if m is None or not m.thinking:
            return
        ctk = job.payload.get("chat_template_kwargs")
        ctk = dict(ctk) if isinstance(ctk, dict) else {}
        if "enable_thinking" not in ctk:
            ctk["enable_thinking"] = False
            job.payload = {**job.payload, "chat_template_kwargs": ctk}

    async def _run(self, job: Job) -> None:
        self._default_thinking_off(job)
        loaded = await self.up.loaded_model()
        job.cold_start = loaded != job.model_id
        t_start = now()
        if job.cold_start:
            job.state = JobState.LOADING_MODEL
            job.load_started_at = t_start
            await self._emit(job, {"type": "loading",
                                   "eta_s": self.load_estimate(job.model_id)})
            await self._broadcast()

        sampler = GpuPowerSampler()
        pump = (self._pump_raw if job.raw_passthrough
                else self._pump_agent if job.agent is not None else self._pump_chat)
        await self._pump_until_stopped(pump, job, t_start, sampler)
        t_answered = now()
        if job.think_started_at is not None and job.think_ended_at is None:
            job.think_ended_at = t_answered   # thought right up to the end (or was cut off)
        # decided now: a Stop pressed while the chat is being named only stops
        # the naming, it doesn't throw away the finished answer
        cancelled = job._cancel.is_set()

        prompt_s = 0.0
        if job.last_timings:
            prompt_s = (job.last_timings.get("prompt_ms") or 0) / 1000.0
        if job.cold_start and job.gen_started_at:
            job.load_seconds = max(0.0, (job.gen_started_at - t_start) - prompt_s)
            self._update_load_ewma(job.model_id, job.load_seconds)
        elif job.cold_start:
            # stopped (or failed) before the model was even up: all of it was
            # loading, billed at the load rate -- not as generation
            job.load_seconds = t_answered - t_start
        if job.gen_started_at:
            # the title's second below is billed (the slot is held for it) but
            # isn't generation: gen_seconds, and the tok/s made from it, stop here
            job.gen_seconds = t_answered - job.gen_started_at

        job.state = JobState.CANCELLED if cancelled else JobState.DONE

        if job.completion_tokens is None:
            # usage block missing (aborted / upstream quirk): estimate from
            # measured tok/s and flag the row
            job.usage_estimated = True
            tg = self.reg.require(job.model_id).seed_tg_tok_s
            job.completion_tokens = max(0, round(job.gen_seconds * tg))

        await self._emit(job, {
            "type": "done",
            "state": job.state.value,
            "model": job.model_id,
            "prompt_tokens": job.prompt_tokens,
            "completion_tokens": job.completion_tokens,
            "usage_estimated": job.usage_estimated,
            "load_seconds": round(job.load_seconds, 2),
            "gen_seconds": round(job.gen_seconds, 2),
            "thinking_seconds": job.thinking_seconds,
            "tools": job.tools_record(),
        })
        # The answer is complete and the UI has it: the new chat is named
        # after that, so a Stop now can't take the reply off the screen. The
        # title arrives as one more event before the stream closes.
        if job.title_from is not None and job.content and not cancelled:
            await self._name_chat(job)

        await sampler.stop()
        job.gpu_watts_mean = sampler.mean
        job.finished_at = now()
        job.occupancy_seconds = job.finished_at - t_start

    async def _pump_until_stopped(self, pump, job: Job, t_start: float,
                                  sampler: GpuPowerSampler) -> None:
        """Run the pump, but let Stop (or an admin's Kill) end it at once.
        The pumps only look at the cancel flag when a chunk arrives, and
        during a cold load, a long prompt or a tool run none does for a long
        time. Cancelling the task closes the upstream request (llama.cpp
        stops) and kills a running sandbox."""
        task = asyncio.create_task(pump(job, t_start, sampler))
        stop = asyncio.create_task(job._cancel.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            stop.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return
        task.result()       # the pump's own error, if it had one

    # -- finalisation ---------------------------------------------------------
    async def _finalize(self, job: Job) -> None:
        if job.finished_at is None:
            job.finished_at = now()
        # exclusive box-time this job actually held (0 if it never left the queue)
        if job.picked_at is not None and job.occupancy_seconds == 0.0:
            job.occupancy_seconds = max(0.0, job.finished_at - job.picked_at)
        if self.finalize_billing is not None:
            try:
                await self.finalize_billing(job)
            except Exception:  # noqa: BLE001
                log.exception("billing finalize failed for job %s", job.id)
        await self._persist_job(job)
        # a reply that spent its whole budget thinking still gets saved: the
        # user paid for that reasoning and should be able to read it
        if job.conversation_id and job.state == JobState.DONE and (job.content or job.reasoning):
            await self._persist_assistant_message(job)

    async def _persist_job(self, job: Job) -> None:
        await self.db.execute(
            "INSERT INTO jobs (id, user_id, conversation_id, session_id, model_id, state, lane, "
            "  queued_at, picked_at, load_started_at, gen_started_at, finished_at, load_seconds, "
            "  gen_seconds, occupancy_seconds, credits, cost_usd, rate_used, gpu_watts_mean, "
            "  prompt_tokens, completion_tokens, usage_estimated, cold_start, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  state=excluded.state, picked_at=excluded.picked_at, "
            "  load_started_at=excluded.load_started_at, "
            "  gen_started_at=excluded.gen_started_at, finished_at=excluded.finished_at, "
            "  load_seconds=excluded.load_seconds, gen_seconds=excluded.gen_seconds, "
            "  occupancy_seconds=excluded.occupancy_seconds, credits=excluded.credits, "
            "  cost_usd=excluded.cost_usd, rate_used=excluded.rate_used, "
            "  gpu_watts_mean=excluded.gpu_watts_mean, prompt_tokens=excluded.prompt_tokens, "
            "  completion_tokens=excluded.completion_tokens, "
            "  usage_estimated=excluded.usage_estimated, cold_start=excluded.cold_start, "
            "  error=excluded.error",
            (job.id, job.user_id, job.conversation_id, job.session_id, job.model_id,
             job.state.value,
             job.lane, job.queued_at, job.picked_at, job.load_started_at, job.gen_started_at,
             job.finished_at, job.load_seconds, job.gen_seconds, job.occupancy_seconds,
             job.credits, job.cost_usd, job.rate_used, job.gpu_watts_mean, job.prompt_tokens,
             job.completion_tokens, int(job.usage_estimated), int(job.cold_start),
             job.error),
        )

    async def _persist_assistant_message(self, job: Job) -> None:
        tools = job.tools_record()
        await self.db.execute(
            "INSERT INTO messages (conversation_id, role, content, model_id, "
            "  prompt_tokens, completion_tokens, usage_estimated, reasoning, "
            "  thinking_seconds, tools_json, parent_id, created_at) "
            "VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # an agent reply's prompt_tokens is the conversation it started
            # from, not the sum over its steps: that is what the UI's context
            # ring needs (the jobs row keeps the full count for billing)
            (job.conversation_id, job.content, job.model_id,
             job.context_tokens if tools else job.prompt_tokens,
             job.completion_tokens, int(job.usage_estimated), job.reasoning or None,
             job.thinking_seconds, json.dumps(tools) if tools else None,
             job.parent_message_id, now()),
        )
        # the reply becomes what's shown under the message it answers, even if
        # the user flipped to another branch while it was being written
        if job.parent_message_id is not None:
            await self.db.execute(
                "UPDATE messages SET active_child_id = "
                "(SELECT MAX(id) FROM messages WHERE parent_id = ?) WHERE id = ?",
                (job.parent_message_id, job.parent_message_id),
            )
        await self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now(), job.conversation_id),
        )

    # -- background tasks -------------------------------------------------
    # -- admin lock ----------------------------------------------------------
    def lockable(self, model_id: str) -> bool:
        m = self.reg.get(model_id)
        return m is not None and m.kind == "chat"

    async def _save_lock(self, model_id: str | None) -> None:
        if model_id is None:
            await self.db.execute("DELETE FROM app_settings WHERE key = 'locked_model'")
        else:
            await self.db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('locked_model', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (json.dumps(model_id), now()))

    async def set_locked(self, model_id: str | None) -> None:
        """Keep `model_id` in VRAM (None releases the lock). Jobs for other
        models still swap it out as usual; once the queue has been idle for
        IDLE_TTL, the idle monitor loads the locked model back instead of
        leaving the GPU empty. Saved, so it outlives a restart."""
        if model_id is not None and not self.lockable(model_id):
            raise ValueError(f"unknown model: {model_id}")
        await self._save_lock(model_id)
        self.locked_model = model_id
        self._touch_retry_at = 0.0
        log.info("model lock -> %s", model_id)
        await self._broadcast()
        # an admin locking a model wants it now: load it on the monitor's next
        # tick (once the queue is empty), not after the current model's idle TTL
        self._preload_now = model_id if self._loaded_model != model_id else None

    async def force_unload(self) -> bool:
        """The admin's "free VRAM now": also releases the lock, or the idle
        monitor would put the model straight back."""
        if self.locked_model is not None:
            await self.set_locked(None)
        # not in the slot: like before the lock existed, this acts at once,
        # even on a model that's mid-reply
        ok = await self.up.unload_all()
        if ok:
            self._loaded_model = None
            await self._broadcast()
        return ok

    async def _touch_locked(self, model_id: str) -> None:
        """Preload the locked model, or just reset llama-swap's ttl if it's
        already resident. Runs in the slot, and only on an empty queue.

        A preload gives way: if a job arrives while it runs, the request is
        dropped and the job goes ahead (it waited, unbilled, for a moment at
        most). It's also bounded, so a model that hangs while loading can't
        hold the queue: three times its usual load time, a minute at least."""
        async with self._slot:
            if self._active or self._pending or self.locked_model != model_id:
                return
            preload = self._loaded_model != model_id
            if not preload:
                ok = await self.up.touch(model_id, timeout=self._keepalive_timeout_s)
                self._touched_at = time.time()
                if not ok:
                    self._touch_retry_at = time.time() + self._touch_backoff_s
                return
            log.info("idle -> loading locked model %s", model_id)
            self._arrived.clear()
            limit = max(60.0, 3 * self.load_estimate(model_id))
            touch = asyncio.create_task(self.up.touch(model_id, timeout=limit))
            arrived = asyncio.create_task(self._arrived.wait())
            try:
                await asyncio.wait({touch, arrived}, timeout=limit + 5,
                                   return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                touch.cancel()          # shutting down: don't leave the request behind
                raise
            finally:
                arrived.cancel()
            if not touch.done():
                # a job came in (or the load hung): let the queue have the GPU
                touch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await touch
                if not self._arrived.is_set():
                    self._touch_retry_at = time.time() + self._touch_backoff_s
                return
            self._touched_at = time.time()
            if not touch.result():
                self._touch_retry_at = time.time() + self._touch_backoff_s
                return
            if self._preload_now == model_id:
                self._preload_now = None
            # unlocked or force-unloaded meanwhile: /running has the truth
            if self.locked_model == model_id:
                self._loaded_model = model_id
                await self._broadcast()

    # -- background tasks -------------------------------------------------
    async def _idle_monitor(self) -> None:
        while True:
            await asyncio.sleep(self._idle_poll_s)
            try:
                await self._idle_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- one bad tick mustn't end idle unloads for good
                log.exception("idle monitor tick failed")

    async def _idle_tick(self) -> None:
        ttl = self.cfg.idle_ttl_effective_seconds
        if self._active or self._pending:
            return
        idle_for = time.time() - self._idle_since
        locked = self.locked_model
        if locked is not None:
            if self._loaded_model == locked:
                if time.time() - self._touched_at >= self._keepalive_s:
                    await self._touch_locked(locked)
            elif ((self._loaded_model is None or idle_for > ttl
                   or self._preload_now == locked)
                  and time.time() >= self._touch_retry_at):
                # another model has had its idle time: the locked one goes back in
                await self._touch_locked(locked)
            return
        if self._loaded_model and idle_for > ttl:
            log.info("idle %.0fs -> unloading %s", ttl, self._loaded_model)
            if await self.up.unload_all():
                self._loaded_model = None
                await self._broadcast()

    async def _running_poller(self) -> None:
        while True:
            await asyncio.sleep(self._running_poll_s)
            if self._active:
                continue
            try:
                m = await self.up.loaded_model()
            except Exception:  # noqa: BLE001 -- a garbled /running mustn't stop the polling
                log.exception("polling llama-swap /running failed")
                continue
            if m != self._loaded_model:
                self._loaded_model = m
                await self._broadcast()


IMAGE_TOKENS = 600       # what one picture costs in the prompt, roughly (the UI uses the same)


def _estimate_prompt(messages: list[dict]) -> int:
    """Rough prompt size in tokens. A picture counts as IMAGE_TOKENS, not as
    the length of its base64: a phone photo's data URL alone would look
    bigger than the whole context and end the turn before it could use a tool."""
    pictures = 0

    def strip(m: dict) -> dict:
        nonlocal pictures
        c = m.get("content")
        if not isinstance(c, list):
            return m
        pictures += sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
        return {**m, "content": [p for p in c if not (isinstance(p, dict) and p.get("type") == "image_url")]}

    text = json.dumps([strip(m) for m in messages])
    return int(len(text) / 3.6) + pictures * IMAGE_TOKENS


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


_CODE_ARG = re.compile(r'"code"\s*:\s*"')
_JSON_ESC = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", '"': '"', "\\": "\\", "/": "/"}


def _draft_code(raw: str) -> str:
    """The "code" argument of a tool call still being written, as far as it
    goes: decoded up to the last complete character, "" before it starts."""
    m = _CODE_ARG.search(raw)
    if not m:
        return ""
    out, s, i = [], raw, m.end()
    while i < len(s):
        ch = s[i]
        if ch == '"':
            break
        if ch == "\\":
            if i + 1 >= len(s):
                break
            e = s[i + 1]
            if e == "u":
                if i + 6 > len(s):
                    break
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(_JSON_ESC.get(e, e))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_LEAKED_CALL = re.compile(r"<tool_call>.*?(?:</tool_call>|$)|<function=[\w-]+>.*?(?:</function>|$)", re.DOTALL)

# a question about something that changes: memory is the wrong place for it
_TIMELY = re.compile(r"\b(latest|current(ly)?|recent(ly)?|today|now|nowadays|newest|new|this "
                     r"(year|month|week|semester|term)|20[2-9]\d|up[- ]to[- ]date|still|anymore)\b",
                     re.IGNORECASE)


_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)
_TITLE_LABEL = re.compile(r"^\s*(?:chat |conversation )?title\s*[:\-–]\s*", re.IGNORECASE)


def clean_title(raw: str) -> str:
    """The model's answer, as a title: its first line, without the wrapping
    models like to add ("Title:", quotes, bold, a full stop), at most
    TITLE_MAX_CHARS. "" if nothing usable is left."""
    t = _LEAKED_CALL.sub("", _THINK_BLOCK.sub("", raw or "").rsplit("</think>", 1)[-1])
    line = next((x for x in t.splitlines() if x.strip()), "")
    line = _TITLE_LABEL.sub("", line.replace("**", "").replace("__", ""))
    line = re.sub(r"\s+", " ", line).strip().strip("*_#`\"'“”‘’«»「」 ").strip()
    line = line.rstrip(".,;:。").strip()
    if len(line) > TITLE_MAX_CHARS:
        cut = line[:TITLE_MAX_CHARS + 1].rsplit(" ", 1)[0]
        line = (cut if len(cut) >= TITLE_MAX_CHARS // 2 else line[:TITLE_MAX_CHARS]).rstrip() + "…"
    return line if len(line) >= 2 else ""


def _echo_args(raw: str) -> str:
    """Arguments as sent back in the history. llama.cpp re-parses them to
    render the chat template and fails the whole request (HTTP 500) on
    malformed JSON, so a call whose arguments were cut off or garbled goes
    back as {}; its tool result already told the model what went wrong."""
    try:
        v = parse_args(raw)
    except ValueError:
        return "{}"
    # re-serialized: what parse_args repaired (a raw newline, LaTeX) parses there too
    return json.dumps(v)


def _loose_json(raw: str) -> dict:
    """Tool arguments for display; the tool itself re-parses them."""
    try:
        return parse_args(raw)
    except ValueError:
        return {"raw": (raw or "")[:200]}


_qm: QueueManager | None = None


def get_queue() -> QueueManager:
    if _qm is None:
        raise RuntimeError("queue not started")
    return _qm


async def start_queue() -> QueueManager:
    global _qm
    _qm = QueueManager(get_db(), get_upstream(), get_registry(), get_settings())
    await _qm.start()
    return _qm


async def stop_queue() -> None:
    global _qm
    if _qm is not None:
        await _qm.stop()
        _qm = None
