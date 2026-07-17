"""Background Impala monitor service.

Slice 4 of ``docs/research/impala-monitoring-implementation-plan.md``. This
module owns two responsibilities, and nothing else:

1. **Event-file replay.** Tail ``<job_dir>/monitor.events.jsonl`` (written by
   ``scr._common.run_impala_shell`` via the runner-supplied
   ``DISPATCH_MONITOR_EVENTS_PATH``, see
   ``docs/research/impala-debug-web-monitoring-contract.md``) and build the
   explicit hierarchy: shell executions (pool, relation) -> queries ->
   transparent-retry queries. A second ``query_discovered`` inside one shell
   is a sibling statement (multi-statement monthly jobs), never a retry; a
   new ``shell_started`` on another pool is an orchestrator fallback shell
   (pool-hop), never an Impala retry; only ``query_retried`` events create a
   transparent-retry child of the query that was in flight in that shell.
2. **Polling.** Share exactly one poller per live query across every
   subscriber, at 2s cadence while a Job Detail screen subscribes to that
   job and 30s otherwise, calling the injected, blocking
   ``dispatch.impala_monitor_http.ImpalaMonitorClient``. After a terminal
   observation (``succeeded``/``failed`` phases), poll once more, persist the
   final observation, and stop. Unknown-id/evicted responses (an
   ``availability_error`` observation) retain the last good observation and
   never synthesize success or failure.

This module never writes the manifest, the event file, or anything else on
disk — its only side effect is holding its own in-memory state and calling
the injected monitor client. Job-level state is never invented here; callers
that need it re-read the manifest via ``dispatch.manifest``/``dispatch.jobs``.

No Textual imports. The service runs its own daemon thread (see
``MonitorService.start``); an ``asyncio``-based app can instead drive
``MonitorService`` from a worker task by calling the same synchronous
subscribe/unsubscribe/snapshot API off its event loop, since nothing here
imports ``asyncio`` either.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .impala_monitor import ImpalaObservation, QueryIdentity, Relation

logger = logging.getLogger("dispatch.monitor_service")

FOREGROUND_POLL_SECONDS = 2.0
BACKGROUND_POLL_SECONDS = 30.0

_EVENT_TYPES = frozenset({"shell_started", "query_discovered", "query_retried", "shell_finished"})


class MonitorClient(Protocol):
    """The subset of ``ImpalaMonitorClient`` this service depends on.

    A ``Protocol`` (not an import of the concrete class) so tests can inject
    lightweight fakes without constructing real transports, and so this
    module does not need to import ``dispatch.impala_monitor_http`` (which
    pulls in ``urllib``/``ssl``) just to type-check.
    """

    def observe(self, identity: QueryIdentity) -> ImpalaObservation: ...


# --------------------------------------------------------------------------
# Hierarchy model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryAttempt:
    """One Impala query attempt: an initial statement or a transparent retry."""

    query_id: str
    coordinator_base_url: str
    relation: Relation
    shell_execution_id: str
    discovered_at: str
    seq: int
    retries: tuple[QueryAttempt, ...] = field(default_factory=tuple)
    observation: ImpalaObservation | None = None

    @property
    def identity_key(self) -> tuple[str, str]:
        return (self.coordinator_base_url, self.query_id)


@dataclass(frozen=True)
class ShellExecutionAttempt:
    """One ``impala-shell`` process: a pool, a relation to its sibling shells,
    and the ordered list of query statements it discovered (each with its own
    transparent-retry chain)."""

    shell_execution_id: str
    shell_relation: ShellRelation
    pool: str
    seq: int
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    queries: tuple[QueryAttempt, ...] = field(default_factory=tuple)


ShellRelation = Literal["initial", "orchestrator_pool_fallback", "unknown_legacy"]


@dataclass(frozen=True)
class OrchestratorCallAttempt:
    """One manifest orchestrator call and its explicitly related shells."""

    call_id: str
    index: int | None
    script: str | None
    seq: int
    shell_executions: tuple[ShellExecutionAttempt, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MonitorSnapshot:
    """Immutable, UI-facing snapshot of one job's monitoring state.

    Published by ``MonitorService`` and read by consumers (the Slice 5 TUI
    panel); never mutated in place.
    """

    job_id: str
    available: bool
    unavailable_reason: str | None
    orchestrator_calls: tuple[OrchestratorCallAttempt, ...] = field(default_factory=tuple)
    generation: int = 0


def _unavailable_snapshot(job_id: str, reason: str) -> MonitorSnapshot:
    return MonitorSnapshot(
        job_id=job_id, available=False, unavailable_reason=reason, orchestrator_calls=()
    )


# --------------------------------------------------------------------------
# Event-file replay
# --------------------------------------------------------------------------


@dataclass
class _MutableQuery:
    query_id: str
    coordinator_base_url: str
    relation: Relation
    shell_execution_id: str
    discovered_at: str
    seq: int
    retries: list[_MutableQuery] = field(default_factory=list)
    observation: ImpalaObservation | None = None

    def freeze(self) -> QueryAttempt:
        return QueryAttempt(
            query_id=self.query_id,
            coordinator_base_url=self.coordinator_base_url,
            relation=self.relation,
            shell_execution_id=self.shell_execution_id,
            discovered_at=self.discovered_at,
            seq=self.seq,
            retries=tuple(child.freeze() for child in self.retries),
            observation=self.observation,
        )


@dataclass
class _MutableShell:
    shell_execution_id: str
    pool: str
    shell_relation: ShellRelation
    seq: int
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    queries: list[_MutableQuery] = field(default_factory=list)

    def freeze(self) -> ShellExecutionAttempt:
        return ShellExecutionAttempt(
            shell_execution_id=self.shell_execution_id,
            shell_relation=self.shell_relation,
            pool=self.pool,
            seq=self.seq,
            started_at=self.started_at,
            finished_at=self.finished_at,
            returncode=self.returncode,
            queries=tuple(query.freeze() for query in self.queries),
        )


@dataclass
class _MutableCall:
    call_id: str
    index: int | None
    script: str | None
    seq: int
    shells: list[_MutableShell] = field(default_factory=list)

    def freeze(self) -> OrchestratorCallAttempt:
        return OrchestratorCallAttempt(
            call_id=self.call_id,
            index=self.index,
            script=self.script,
            seq=self.seq,
            shell_executions=tuple(shell.freeze() for shell in self.shells),
        )


class EventFileReplayError(Exception):
    """Reserved for future strict-mode replay; never raised in tolerant mode."""


class _HierarchyBuilder:
    """Builds the shell/query/retry hierarchy from a tolerant event stream.

    Rules (see module docstring and the research note's "Use three levels,
    not one overloaded status"):

    - ``shell_started`` opens a new sibling ``ShellExecutionAttempt``, keyed
      by ``shell_execution_id``. Shells are ordered by first-seen sequence.
    - ``query_discovered`` inside a shell appends a new sibling
      ``QueryAttempt`` under that shell (never a retry), in event order —
      this is what makes a second statement in a multi-statement monthly
      shell a sibling rather than a retry.
    - ``query_retried`` attaches a ``transparent_retry`` ``QueryAttempt`` as
      a child of the *most recently discovered* query in the same shell
      (the one whose coordinator/query lineage the retry continues), never
      creating a new shell.
    - ``shell_finished`` records the shell's return code and finish time.
    - Unknown event types/versions are skipped, not raised. A malformed
      (non-JSON, partial) line is skipped; replay resumes on the next
      newline-terminated line.
    """

    def __init__(self) -> None:
        self._shells: dict[str, _MutableShell] = {}
        self._shell_calls: dict[str, str] = {}
        self._calls: dict[str, _MutableCall] = {}
        self._call_order: list[str] = []
        self._seq_counter = 0

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        version = event.get("v")
        if version not in (1, 2):
            # Unknown/future event version: skip, don't crash.
            return
        event_type = event.get("type")
        if event_type not in _EVENT_TYPES:
            return
        shell_execution_id = event.get("shell_execution_id")
        if not isinstance(shell_execution_id, str) or not shell_execution_id:
            return

        self._seq_counter += 1
        seq = self._seq_counter

        call = self._call_for_event(version, shell_execution_id, event, seq)
        if call is None:
            return

        if event_type == "shell_started":
            self._handle_shell_started(call, shell_execution_id, event, seq, version)
        elif event_type == "shell_finished":
            self._handle_shell_finished(shell_execution_id, event)
        elif event_type == "query_discovered":
            self._handle_query_discovered(shell_execution_id, event, seq)
        elif event_type == "query_retried":
            self._handle_query_retried(shell_execution_id, event, seq)

    def _call_for_event(
        self, version: int, shell_execution_id: str, event: dict, seq: int
    ) -> _MutableCall | None:
        if version == 1:
            call_id = self._shell_calls.get(shell_execution_id)
            if call_id is None:
                call_id = f"legacy-{len(self._call_order) + 1:04d}-{shell_execution_id}"
                self._calls[call_id] = _MutableCall(call_id, None, None, seq)
                self._call_order.append(call_id)
                self._shell_calls[shell_execution_id] = call_id
            return self._calls[call_id]

        call_id = event.get("orchestrator_call_id")
        index = event.get("orchestrator_call_index")
        script = event.get("orchestrator_script")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or not isinstance(script, str)
            or not script
        ):
            return None
        existing_shell_call = self._shell_calls.get(shell_execution_id)
        if existing_shell_call is not None and existing_shell_call != call_id:
            return None
        call = self._calls.get(call_id)
        if call is None:
            call = _MutableCall(call_id, index, script, seq)
            self._calls[call_id] = call
            self._call_order.append(call_id)
        elif call.index != index or call.script != script:
            return None
        self._shell_calls[shell_execution_id] = call_id
        return call

    def _handle_shell_started(
        self, call: _MutableCall, shell_execution_id: str, event: dict, seq: int, version: int
    ) -> None:
        if shell_execution_id in self._shells:
            return
        pool = event.get("pool")
        pool = pool if isinstance(pool, str) else ""
        ts = event.get("ts")
        ts = ts if isinstance(ts, str) else None
        relation: ShellRelation = "unknown_legacy"
        if version == 2:
            candidate = event.get("shell_relation")
            if candidate not in ("initial", "orchestrator_pool_fallback"):
                return
            relation = candidate
        self._shells[shell_execution_id] = _MutableShell(
            shell_execution_id=shell_execution_id,
            pool=pool,
            shell_relation=relation,
            seq=seq,
            started_at=ts,
        )
        call.shells.append(self._shells[shell_execution_id])

    def _ensure_shell(self, shell_execution_id: str, seq: int) -> _MutableShell | None:
        # A shell can legitimately be referenced by a query event before its
        # shell_started line is written (e.g. truncated tail catching up
        # mid-write); tolerate that by lazily creating the shell record
        # rather than dropping the event.
        shell = self._shells.get(shell_execution_id)
        if shell is None:
            return None
        return shell

    def _handle_shell_finished(self, shell_execution_id: str, event: dict) -> None:
        shell = self._shells.get(shell_execution_id)
        if shell is None:
            return
        returncode = event.get("returncode")
        shell.returncode = returncode if isinstance(returncode, int) else None
        ts = event.get("ts")
        shell.finished_at = ts if isinstance(ts, str) else None

    def _handle_query_discovered(self, shell_execution_id: str, event: dict, seq: int) -> None:
        base = event.get("coordinator_base_url")
        qid = event.get("query_id")
        if not isinstance(base, str) or not isinstance(qid, str) or not base or not qid:
            return
        ts = event.get("ts")
        ts = ts if isinstance(ts, str) else ""
        shell = self._ensure_shell(shell_execution_id, seq)
        if shell is None:
            return
        shell.queries.append(
            _MutableQuery(
                query_id=qid,
                coordinator_base_url=base,
                relation="initial",
                shell_execution_id=shell_execution_id,
                discovered_at=ts,
                seq=seq,
            )
        )

    def _handle_query_retried(self, shell_execution_id: str, event: dict, seq: int) -> None:
        base = event.get("coordinator_base_url")
        qid = event.get("query_id")
        if not isinstance(base, str) or not isinstance(qid, str) or not base or not qid:
            return
        shell = self._shells.get(shell_execution_id)
        if shell is None or not shell.queries:
            # A retry with no prior discovered query in this shell has no
            # parent to attach to; drop it tolerantly rather than inventing
            # a synthetic parent.
            return
        ts = event.get("ts")
        ts = ts if isinstance(ts, str) else ""
        parent = shell.queries[-1]
        parent.retries.append(
            _MutableQuery(
                query_id=qid,
                coordinator_base_url=base,
                relation="transparent_retry",
                shell_execution_id=shell_execution_id,
                discovered_at=ts,
                seq=seq,
            )
        )

    def calls(self) -> list[_MutableCall]:
        """Return all orchestrator calls in first-event order."""
        return [self._calls[call_id] for call_id in self._call_order]

    def shells(self, call: _MutableCall) -> list[_MutableShell]:
        """Return every shell explicitly owned by ``call`` in event order."""
        return list(call.shells)

    def query_nodes(self) -> list[_MutableQuery]:
        """Return all query nodes recursively, including superseded parents."""
        nodes: list[_MutableQuery] = []

        def visit(query: _MutableQuery) -> None:
            nodes.append(query)
            for retry in query.retries:
                visit(retry)

        for call in self.calls():
            for shell in self.shells(call):
                for query in shell.queries:
                    visit(query)
        return nodes

    def leaf_queries(self) -> list[_MutableQuery]:
        """Return every query attempt that should currently be polled.

        A query with retries is superseded by its most recent retry (the
        live attempt continues under the retry's identity); a query with no
        retries is itself the live leaf.
        """
        leaves: list[_MutableQuery] = []

        def leaf(query: _MutableQuery) -> _MutableQuery:
            return leaf(query.retries[-1]) if query.retries else query

        for call in self.calls():
            for shell in self.shells(call):
                for query in shell.queries:
                    leaves.append(leaf(query))
        return leaves


def _read_replayable_lines(path: Path) -> list[str]:
    """Read complete lines from ``path``, tolerating a partial last line.

    Returns an empty list when the file is absent or unreadable (the caller
    treats that as "monitoring unavailable" — see ``replay_event_file``).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    # A partial last line (the writer was interrupted mid-append, or the
    # tail is racing a concurrent writer) has no trailing newline; drop it
    # rather than attempting to parse an incomplete JSON object. Complete
    # lines are always newline-terminated by _EventWriter.
    if text.endswith("\n"):
        lines = text.split("\n")
        lines = lines[:-1]  # drop the trailing empty string after the final \n
    else:
        lines = text.split("\n")[:-1]  # drop the trailing partial line
    return lines


def replay_event_file(path: Path) -> _HierarchyBuilder | None:
    """Replay ``path`` into a hierarchy builder, or ``None`` if unavailable.

    ``None`` means "monitoring unavailable": the file does not exist (no
    identity was ever captured for this job, e.g. an older job or a job
    whose orchestrator never called ``run_impala_shell``).
    """
    if not path.exists():
        return None
    builder = _HierarchyBuilder()
    for line in _read_replayable_lines(path):
        builder.feed_line(line)
    return builder


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class _RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_TERMINAL_PHASES = frozenset({"succeeded", "failed"})


@dataclass
class _QueryPollerState:
    """Mutable per-(job, query-identity) poller bookkeeping.

    One instance is shared by every subscriber currently interested in this
    query; the background thread loop reads/updates it directly (protected
    by the service's single lock), so there is exactly one poller and one
    ``observe`` call stream per live query regardless of subscriber count.
    """

    identity: QueryIdentity
    job_id: str
    last_observation: ImpalaObservation | None = None
    next_poll_at: float = 0.0
    stopped: bool = False
    terminal_extra_poll_done: bool = False


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


@dataclass
class _JobState:
    job_dir: Path
    subscriber_count: int = 0
    generation: int = 0
    builder: _HierarchyBuilder | None = None
    snapshot: MonitorSnapshot = field(
        default_factory=lambda: _unavailable_snapshot("", "monitoring unavailable")
    )


class MonitorService:
    """Background Impala monitor service.

    Owns a single daemon thread that periodically re-reads each subscribed
    job's ``monitor.events.jsonl``, refreshes each live query's poller
    according to the current cadence, and republishes an immutable
    ``MonitorSnapshot`` per job. Every public method is thread-safe.

    This service never writes any file. It only ever calls
    ``MonitorClient.observe`` (read-only, per the Slice 3 contract) and holds
    its own in-memory state.
    """

    def __init__(
        self,
        client: MonitorClient,
        *,
        clock: Clock | None = None,
        foreground_poll_seconds: float = FOREGROUND_POLL_SECONDS,
        background_poll_seconds: float = BACKGROUND_POLL_SECONDS,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else _RealClock()
        self._foreground_poll_seconds = foreground_poll_seconds
        self._background_poll_seconds = background_poll_seconds

        self._lock = threading.RLock()
        self._jobs: dict[str, _JobState] = {}
        self._pollers: dict[tuple[str, str, str], _QueryPollerState] = {}
        # keyed by (job_id, coordinator_base_url, query_id)

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Start the background daemon thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="dispatch-monitor-service", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        """Stop the background thread and wait for it to exit."""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    # -- subscription ----------------------------------------------------

    def subscribe(self, job_id: str, job_dir: Path) -> MonitorSnapshot:
        """Register interest in ``job_id`` and return the current snapshot.

        Raises the foreground poll cadence for every live query under this
        job for as long as at least one subscriber remains. Calling this
        repeatedly for the same job increments a reference count; each call
        must be paired with ``unsubscribe``.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                state = _JobState(job_dir=job_dir)
                self._jobs[job_id] = state
            was_foreground = state.subscriber_count > 0
            state.subscriber_count += 1
            self._refresh_job_locked(job_id, state)
            if not was_foreground:
                self._reschedule_job_pollers_locked(job_id, tighten=True)
            snapshot = state.snapshot
        self._wake_event.set()
        return snapshot

    def unsubscribe(self, job_id: str) -> None:
        """Release one subscription registered by ``subscribe``.

        Drops the poll cadence for this job's queries back to background
        once the subscriber count reaches zero. Never removes job state
        outright (a fresh ``snapshot()``/``subscribe()`` call must still see
        the last-known hierarchy), only the cadence classification.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            was_foreground = state.subscriber_count > 0
            state.subscriber_count = max(0, state.subscriber_count - 1)
            if was_foreground and state.subscriber_count == 0:
                self._reschedule_job_pollers_locked(job_id, tighten=False)
        self._wake_event.set()

    def _reschedule_job_pollers_locked(self, job_id: str, *, tighten: bool) -> None:
        """Re-anchor this job's non-stopped pollers to the new cadence.

        Called right after a subscriber-count transition (0 <-> >0) so a
        cadence switch takes effect immediately, instead of waiting for a
        poller's already-scheduled ``next_poll_at`` (computed under the old
        cadence) to elapse on its own.

        ``tighten=True`` (switching to the shorter foreground cadence on
        subscribe) only ever pulls a poll time *earlier* (``min`` with the
        new cadence's horizon): a brand-new poller created moments ago in
        this same refresh with ``next_poll_at`` already due (``<= now``) is
        left alone rather than being pushed out to ``now + cadence``, so
        subscribing never delays that query's very first poll.

        ``tighten=False`` (switching to the longer background cadence on
        unsubscribe) only ever pushes a poll time *later* (``max``): a
        poller that is already due now (e.g. mid-terminal-stop sequence)
        must not have its due poll cancelled just because the last
        subscriber left.
        """
        cadence = self._cadence_for(job_id)
        now = self._clock.monotonic()
        horizon = now + cadence
        for key, poller in self._pollers.items():
            if key[0] != job_id or poller.stopped:
                continue
            if tighten:
                poller.next_poll_at = min(poller.next_poll_at, horizon)
            else:
                poller.next_poll_at = max(poller.next_poll_at, horizon)

    def snapshot(self, job_id: str) -> MonitorSnapshot:
        """Return the current snapshot for ``job_id`` without subscribing.

        Returns an "unavailable" snapshot for a job this service has never
        seen (e.g. queried before the first ``subscribe``/refresh).
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return _unavailable_snapshot(job_id, "monitoring unavailable")
            return state.snapshot

    def register_job(self, job_id: str, job_dir: Path) -> MonitorSnapshot:
        """Ensure ``job_id`` is tracked (0 subscribers) and refresh its snapshot.

        Used to warm a job's hierarchy (e.g. background 30s cadence for a
        Running job not currently shown on Job Detail) without incrementing
        the subscriber count. Safe to call repeatedly.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                state = _JobState(job_dir=job_dir)
                self._jobs[job_id] = state
            self._refresh_job_locked(job_id, state)
            snapshot = state.snapshot
        self._wake_event.set()
        return snapshot

    # -- internal: replay + snapshot rebuild ------------------------------

    def _refresh_job_locked(self, job_id: str, state: _JobState) -> None:
        """Re-read the event file and rebuild this job's snapshot.

        Must be called while holding ``self._lock``. Reattaches any
        previously observed ``ImpalaObservation`` onto matching queries by
        identity so a replay never discards poll results already collected
        for a still-present query.
        """
        events_path = state.job_dir / "monitor.events.jsonl"
        previous_observations: dict[tuple[str, str], ImpalaObservation] = {}
        if state.builder is not None:
            for leaf in state.builder.leaf_queries():
                if leaf.observation is not None:
                    previous_observations[(leaf.coordinator_base_url, leaf.query_id)] = (
                        leaf.observation
                    )

        builder = replay_event_file(events_path)
        if builder is None:
            state.builder = None
            state.generation += 1
            state.snapshot = _unavailable_snapshot(job_id, "monitoring unavailable")
            self._prune_pollers_for_job(job_id, live_keys=set())
            return

        for leaf in builder.leaf_queries():
            key = (leaf.coordinator_base_url, leaf.query_id)
            if key in previous_observations:
                leaf.observation = previous_observations[key]

        state.builder = builder
        state.generation += 1
        state.snapshot = MonitorSnapshot(
            job_id=job_id,
            available=True,
            unavailable_reason=None,
            orchestrator_calls=tuple(call.freeze() for call in builder.calls()),
            generation=state.generation,
        )

        self._sync_pollers_locked(job_id, state, builder)

    def _sync_pollers_locked(
        self, job_id: str, state: _JobState, builder: _HierarchyBuilder
    ) -> None:
        live_keys: set[tuple[str, str, str]] = set()
        for leaf in builder.leaf_queries():
            poller_key = (job_id, leaf.coordinator_base_url, leaf.query_id)
            live_keys.add(poller_key)
            existing = self._pollers.get(poller_key)
            if existing is not None:
                if leaf.observation is None and existing.last_observation is not None:
                    leaf.observation = existing.last_observation
                continue
            identity = QueryIdentity(
                coordinator_base_url=leaf.coordinator_base_url,
                query_id=leaf.query_id,
                shell_execution_id=leaf.shell_execution_id,
                relation=leaf.relation,
                discovered_at=leaf.discovered_at,
            )
            self._pollers[poller_key] = _QueryPollerState(
                identity=identity,
                job_id=job_id,
                last_observation=leaf.observation,
                next_poll_at=self._clock.monotonic(),
            )
        self._prune_pollers_for_job(job_id, live_keys=live_keys)

    def _prune_pollers_for_job(self, job_id: str, *, live_keys: set[tuple[str, str, str]]) -> None:
        """Drop every poller for ``job_id`` that is no longer a live leaf.

        A poller stops being live the moment a later refresh discovers a
        ``query_retried`` (or any other hierarchy change) that supersedes it
        -- not only when it has already reached a terminal observation. A
        superseded poller must be removed unconditionally so it stops being
        polled, preserving "exactly one poller per live query"; leaving it
        behind until its (possibly never-reached) ``stopped`` flag is set
        would let it keep polling forever alongside the query that replaced
        it.
        """
        stale = [key for key in self._pollers if key[0] == job_id and key not in live_keys]
        for key in stale:
            del self._pollers[key]

    # -- internal: background loop ----------------------------------------

    def _cadence_for(self, job_id: str) -> float:
        state = self._jobs.get(job_id)
        if state is not None and state.subscriber_count > 0:
            return self._foreground_poll_seconds
        return self._background_poll_seconds

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            sleep_for = self._tick()
            self._wake_event.wait(timeout=max(sleep_for, 0.01))
            self._wake_event.clear()

    def _tick(self) -> float:
        """Run one scheduling pass: refresh job hierarchies, poll due queries.

        Returns the number of seconds until the next poller is due (used as
        the loop's sleep budget); callers should treat this as advisory.
        """
        with self._lock:
            job_ids = list(self._jobs.keys())
        for job_id in job_ids:
            with self._lock:
                state = self._jobs.get(job_id)
                if state is None:
                    continue
                self._refresh_job_locked(job_id, state)

        now = self._clock.monotonic()
        due: list[_QueryPollerState] = []
        with self._lock:
            for poller in self._pollers.values():
                if poller.stopped:
                    continue
                if poller.next_poll_at <= now:
                    due.append(poller)

        for poller in due:
            self._poll_one(poller)

        with self._lock:
            upcoming = [p.next_poll_at for p in self._pollers.values() if not p.stopped]
        if not upcoming:
            return self._background_poll_seconds
        soonest = min(upcoming) - self._clock.monotonic()
        return max(soonest, 0.0)

    def _poll_one(self, poller: _QueryPollerState) -> None:
        observation = self._client.observe(poller.identity)
        with self._lock:
            if poller.stopped:
                return
            if observation.availability_error is not None:
                # Never synthesize success/failure from disappearance;
                # retain the last good observation and surface the error.
                retained = poller.last_observation
                if retained is not None:
                    observation = _with_availability_error(retained, observation.availability_error)
                poller.last_observation = observation
            else:
                poller.last_observation = observation

            self._attach_observation_locked(poller.job_id, poller.identity, poller.last_observation)

            cadence = self._cadence_for(poller.job_id)
            if observation.phase in _TERMINAL_PHASES:
                if poller.terminal_extra_poll_done:
                    poller.stopped = True
                else:
                    poller.terminal_extra_poll_done = True
                    poller.next_poll_at = self._clock.monotonic() + cadence
            else:
                poller.next_poll_at = self._clock.monotonic() + cadence

    def _attach_observation_locked(
        self, job_id: str, identity: QueryIdentity, observation: ImpalaObservation | None
    ) -> None:
        state = self._jobs.get(job_id)
        if state is None or state.builder is None or observation is None:
            return
        for leaf in state.builder.leaf_queries():
            if (
                leaf.coordinator_base_url == identity.coordinator_base_url
                and leaf.query_id == identity.query_id
            ):
                leaf.observation = observation
                break
        state.generation += 1
        state.snapshot = MonitorSnapshot(
            job_id=job_id,
            available=True,
            unavailable_reason=None,
            orchestrator_calls=tuple(call.freeze() for call in state.builder.calls()),
            generation=state.generation,
        )

    # -- test/introspection helpers ---------------------------------------

    def poller_count(self, job_id: str | None = None) -> int:
        """Return the number of active (non-stopped) pollers, optionally scoped."""
        with self._lock:
            return sum(
                1
                for key, poller in self._pollers.items()
                if not poller.stopped and (job_id is None or key[0] == job_id)
            )

    def run_pending(self) -> float:
        """Synchronously run one scheduling pass (for tests with fake clocks).

        Equivalent to one iteration of the background loop's body, without
        needing a real thread. Returns the same advisory sleep budget as the
        internal loop.
        """
        return self._tick()


def _with_availability_error(
    observation: ImpalaObservation, availability_error: str
) -> ImpalaObservation:
    return ImpalaObservation(
        raw_state=observation.raw_state,
        phase=observation.phase,
        pool=observation.pool,
        scan_progress=observation.scan_progress,
        query_progress=observation.query_progress,
        queued_duration=observation.queued_duration,
        bytes_read=observation.bytes_read,
        rows_fetched=observation.rows_fetched,
        last_event=observation.last_event,
        status_summary=observation.status_summary,
        detail_url=observation.detail_url,
        observed_at=observation.observed_at,
        availability_error=availability_error,
    )
