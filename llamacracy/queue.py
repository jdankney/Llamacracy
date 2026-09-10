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
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .db import Database, get_db, now
from .power import GpuPowerSampler
from .registry import Registry, get_registry
from .upstream import Upstream, UpstreamError, get_upstream

log = logging.getLogger("llamacracy.queue")


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

    # billing (filled by metering in Phase 3)
    credits: float = 0.0
    cost_usd: float = 0.0
    rate_used: float | None = None
    gpu_watts_mean: float | None = None

    content_parts: list[str] = field(default_factory=list)
    _chunks: asyncio.Queue = field(default_factory=asyncio.Queue)
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)
    _done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

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
        self._idle_poll_s = 30.0
        self._running_poll_s = 10.0
        # hook points filled by metering (Phase 3)
        self.check_limits = None       # async (user_id, model_id) -> LimitDecision | None
        self.finalize_billing = None   # async (job) -> None

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        rows = await self.db.fetch_all("SELECT model_id, ewma_load_seconds FROM model_load_stats")
        self._load_ewma = {r["model_id"]: r["ewma_load_seconds"] for r in rows}
        for m in self.reg.all():
            self._load_ewma.setdefault(m.key, m.seed_cold_load_s)
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
        asyncio.create_task(self._persist_load_ewma(model_id, val))

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
            self._pending.remove(job)
            job.state = JobState.CANCELLED
            job.finished_at = now()
            await self._emit(job, {"type": "cancelled"})
            await job._chunks.put(None)
            await self._persist_job(job)
            await self._broadcast()
            return True
        return False

    def snapshot(self) -> dict:
        jobs = []
        if self._active is not None:
            jobs.append(self._active.public(0))
        for i, j in enumerate(self._pending, start=1):
            jobs.append(j.public(i))
        return {
            "type": "queue",
            "loaded_model": self._loaded_model,
            "depth": len(self._pending) + (1 if self._active else 0),
            "jobs": jobs,
            "ts": now(),
        }

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
            job = self._pending.pop(0)
            if job.state in TERMINAL:
                continue
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
                await self._finalize(job)
                job._done.set()
                await job._chunks.put(None)
                await self._broadcast()

    async def _run(self, job: Job) -> None:
        loaded = await self.up.loaded_model()
        job.cold_start = loaded != job.model_id
        t_start = now()
        if job.cold_start:
            job.state = JobState.LOADING_MODEL
            job.load_started_at = t_start
            await self._emit(job, {"type": "loading",
                                   "eta_s": self.load_estimate(job.model_id)})
            await self._broadcast()

        first = False
        sampler = GpuPowerSampler()
        async for chunk in self.up.stream_chat({**job.payload, "model": job.model_id}):
            if job._cancel.is_set():
                break
            if not first and (chunk.content_delta or chunk.reasoning_delta):
                first = True
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
            if chunk.content_delta:
                job.content_parts.append(chunk.content_delta)
                await self._emit(job, {"type": "token", "text": chunk.content_delta})
            if chunk.reasoning_delta:
                await self._emit(job, {"type": "reasoning", "text": chunk.reasoning_delta})
            if chunk.usage:
                job.prompt_tokens = chunk.usage.get("prompt_tokens")
                job.completion_tokens = chunk.usage.get("completion_tokens")
            if chunk.timings:
                job.last_timings = chunk.timings

        await sampler.stop()
        job.gpu_watts_mean = sampler.mean

        t_end = now()
        job.finished_at = t_end
        job.occupancy_seconds = t_end - t_start

        prompt_s = 0.0
        if job.last_timings:
            prompt_s = (job.last_timings.get("prompt_ms") or 0) / 1000.0
        if job.cold_start and job.gen_started_at:
            job.load_seconds = max(0.0, (job.gen_started_at - t_start) - prompt_s)
            self._update_load_ewma(job.model_id, job.load_seconds)
        if job.gen_started_at:
            job.gen_seconds = t_end - job.gen_started_at

        if job._cancel.is_set():
            job.state = JobState.CANCELLED
        else:
            job.state = JobState.DONE

        if job.completion_tokens is None:
            # usage block missing (aborted / upstream quirk): estimate from
            # measured tok/s and flag the row
            job.usage_estimated = True
            tg = self.reg.require(job.model_id).seed_tg_tok_s
            job.completion_tokens = max(0, round(job.gen_seconds * tg))

        await self._emit(job, {
            "type": "done",
            "state": job.state.value,
            "prompt_tokens": job.prompt_tokens,
            "completion_tokens": job.completion_tokens,
            "usage_estimated": job.usage_estimated,
            "load_seconds": round(job.load_seconds, 2),
            "gen_seconds": round(job.gen_seconds, 2),
        })

    # -- finalisation ---------------------------------------------------------
    async def _finalize(self, job: Job) -> None:
        if job.finished_at is None:
            job.finished_at = now()
        # exclusive box-time this job actually held (0 if it never left the queue)
        if job.picked_at is not None and job.occupancy_seconds == 0.0:
            job.occupancy_seconds = max(0.0, job.finished_at - job.picked_at)
        if self.finalize_billing is not None:
            try:
                await self.finalize_billing(job)   # Phase 3
            except Exception:  # noqa: BLE001
                log.exception("billing finalize failed for job %s", job.id)
        await self._persist_job(job)
        if job.conversation_id and job.state == JobState.DONE and job.content:
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
        await self.db.execute(
            "INSERT INTO messages (conversation_id, role, content, model_id, "
            "  prompt_tokens, completion_tokens, usage_estimated, created_at) "
            "VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?)",
            (job.conversation_id, job.content, job.model_id, job.prompt_tokens,
             job.completion_tokens, int(job.usage_estimated), now()),
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now(), job.conversation_id),
        )

    # -- background tasks -------------------------------------------------
    async def _idle_monitor(self) -> None:
        while True:
            await asyncio.sleep(self._idle_poll_s)
            ttl = self.cfg.idle_ttl_minutes * 60
            if self._active or self._pending:
                continue
            if self._loaded_model and time.time() - self._idle_since > ttl:
                log.info("idle %ds -> unloading %s", ttl, self._loaded_model)
                if await self.up.unload_all():
                    self._loaded_model = None
                    await self._broadcast()

    async def _running_poller(self) -> None:
        while True:
            await asyncio.sleep(self._running_poll_s)
            if self._active:
                continue
            m = await self.up.loaded_model()
            if m != self._loaded_model:
                self._loaded_model = m
                await self._broadcast()


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
