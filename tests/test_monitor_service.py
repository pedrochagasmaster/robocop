"""Tests for the Slice 4 background monitor service (``dispatch/monitor_service.py``).

Covers event-file replay (multi-statement monthly shape, pool-hop chain,
interleaved partial-line tail), poller sharing across subscribers, cadence
switching on subscribe/unsubscribe, terminal-stop (exactly one extra poll),
eviction retention, rebuild-from-sidecar-after-restart, and the invariant
that the service never writes any file. See
``docs/research/impala-monitoring-implementation-plan.md`` (Slice 4) for the
authoritative spec this file implements.

All tests use a fake client and a fake/controllable clock; no test sleeps
longer than ~0.1s and the background thread is only used in the one test
that exercises it directly (with tiny cadences and explicit joins).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dispatch import impala_monitor as im
from dispatch import monitor_service as ms

COORD_1 = "https://coordinator-1.internal.example:25443"
COORD_2 = "https://coordinator-2.internal.example:25443"
QID_1 = "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8"
QID_2 = "2a2b3c4d5e6f7081:9192a3b4c5d6e7f8"
QID_3 = "3a2b3c4d5e6f7081:9192a3b4c5d6e7f8"
QID_RETRY = "aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb"


def event_line(
    event_type: str,
    *,
    job_id: str = "job-1",
    shell_execution_id: str = "shell-1",
    seq: int = 1,
    pool: str = "default",
    ts: str = "2026-07-15T10:00:00.000Z",
    **extra: object,
) -> str:
    payload = {
        "v": 1,
        "type": event_type,
        "job_id": job_id,
        "shell_execution_id": shell_execution_id,
        "seq": seq,
        "pool": pool,
        "ts": ts,
        **extra,
    }
    return json.dumps(payload, sort_keys=True) + "\n"


def write_events(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines), encoding="utf-8")


def make_observation(
    *,
    phase: im.Phase = "running",
    raw_state: str | None = "RUNNING",
    availability_error: str | None = None,
    detail_url: str = "https://coordinator-1.internal.example:25443/query_stmt?query_id=x&json",
    observed_at: str = "2026-07-15T10:00:01Z",
) -> im.ImpalaObservation:
    return im.ImpalaObservation(
        raw_state=raw_state,
        phase=phase,
        pool="default",
        scan_progress=None,
        query_progress=None,
        queued_duration=None,
        bytes_read=None,
        rows_fetched=None,
        last_event=None,
        status_summary=None,
        detail_url=detail_url,
        observed_at=observed_at,
        availability_error=availability_error,
    )


class FakeMonitorClient:
    """Records every ``observe`` call and returns canned/queued responses."""

    def __init__(self) -> None:
        self.calls: list[im.QueryIdentity] = []
        self.responses: dict[tuple[str, str], list[im.ImpalaObservation]] = {}
        self.default_response: im.ImpalaObservation | None = None

    def queue(
        self, coordinator_base_url: str, query_id: str, *observations: im.ImpalaObservation
    ) -> None:
        key = (coordinator_base_url, query_id)
        self.responses.setdefault(key, []).extend(observations)

    def observe(self, identity: im.QueryIdentity) -> im.ImpalaObservation:
        self.calls.append(identity)
        key = (identity.coordinator_base_url, identity.query_id)
        queued = self.responses.get(key)
        if queued:
            if len(queued) > 1:
                return queued.pop(0)
            return queued[0]
        if self.default_response is not None:
            return self.default_response
        return make_observation()


class FakeClock:
    """Manually advanced monotonic clock; ``sleep`` just advances time."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds


# --------------------------------------------------------------------------
# Event-file replay
# --------------------------------------------------------------------------


class TestEventFileReplay:
    def test_absent_file_means_monitoring_unavailable(self, tmp_path: Path) -> None:
        builder = ms.replay_event_file(tmp_path / "monitor.events.jsonl")
        assert builder is None

    def test_absent_file_via_service_snapshot(self, tmp_path: Path) -> None:
        client = FakeMonitorClient()
        service = ms.MonitorService(client, clock=FakeClock())
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is False
        assert snapshot.unavailable_reason == "monitoring unavailable"
        assert snapshot.shell_executions == ()

    def test_single_shell_single_query(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default"),
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                ),
                event_line("shell_finished", returncode=0),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        assert shells[0].pool == "default"
        assert shells[0].returncode == 0
        assert len(shells[0].queries) == 1
        assert shells[0].queries[0].query_id == QID_1
        assert shells[0].queries[0].relation == "initial"

    def test_multi_statement_monthly_shape_second_discovery_is_sibling(
        self, tmp_path: Path
    ) -> None:
        """A monthly script emits multiple query_discovered in ONE shell.

        The second monitor URL is the next statement, not a retry.
        """
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                    seq=2,
                ),
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_2,
                    seq=3,
                ),
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_3,
                    seq=4,
                ),
                event_line("shell_finished", returncode=0, seq=5),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        queries = shells[0].queries
        assert [q.query_id for q in queries] == [QID_1, QID_2, QID_3]
        assert all(q.relation == "initial" for q in queries)
        assert all(q.retries == [] for q in queries)

    def test_pool_hop_chain_is_sibling_shells_not_retries(self, tmp_path: Path) -> None:
        """A new shell_started on another pool is an orchestrator fallback
        shell (sibling), never conflated with an Impala transparent retry."""
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", shell_execution_id="shell-a", pool="default", seq=1),
                event_line(
                    "query_discovered",
                    shell_execution_id="shell-a",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                    seq=2,
                ),
                event_line("shell_finished", shell_execution_id="shell-a", returncode=1, seq=3),
                event_line(
                    "shell_started", shell_execution_id="shell-b", pool="adhoc_small", seq=4
                ),
                event_line(
                    "query_discovered",
                    shell_execution_id="shell-b",
                    coordinator_base_url=COORD_2,
                    query_id=QID_2,
                    seq=5,
                ),
                event_line("shell_finished", shell_execution_id="shell-b", returncode=0, seq=6),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 2
        assert shells[0].pool == "default"
        assert shells[0].queries[0].query_id == QID_1
        assert shells[1].pool == "adhoc_small"
        assert shells[1].queries[0].query_id == QID_2
        # Neither query carries a transparent_retry relation or child.
        assert shells[0].queries[0].relation == "initial"
        assert shells[1].queries[0].relation == "initial"
        assert shells[0].queries[0].retries == []

    def test_query_retried_creates_transparent_retry_child(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                    seq=2,
                ),
                event_line(
                    "query_retried",
                    coordinator_base_url=COORD_1,
                    query_id=QID_RETRY,
                    seq=3,
                ),
                event_line("shell_finished", returncode=0, seq=4),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        query = shells[0].queries[0]
        assert query.query_id == QID_1
        assert len(query.retries) == 1
        retry = query.retries[0]
        assert retry.query_id == QID_RETRY
        assert retry.relation == "transparent_retry"
        # Only the retry is the current leaf/live query.
        leaves = builder.leaf_queries()
        assert len(leaves) == 1
        assert leaves[0].query_id == QID_RETRY

    def test_query_retried_with_no_prior_query_is_dropped_tolerantly(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line(
                    "query_retried",
                    coordinator_base_url=COORD_1,
                    query_id=QID_RETRY,
                    seq=2,
                ),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        assert shells[0].queries == []

    def test_unknown_event_version_is_skipped_not_crashed(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        future_event = json.dumps(
            {
                "v": 2,
                "type": "query_discovered",
                "job_id": "job-1",
                "shell_execution_id": "shell-1",
                "coordinator_base_url": COORD_1,
                "query_id": QID_2,
                "some_new_field": {"nested": True},
            }
        )
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                    seq=2,
                ),
                future_event + "\n",
                event_line("shell_finished", returncode=0, seq=3),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        # Only the v1 query is present; the v2 event was skipped.
        assert [q.query_id for q in shells[0].queries] == [QID_1]

    def test_unknown_event_type_is_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        weird = json.dumps(
            {
                "v": 1,
                "type": "some_future_event_type",
                "job_id": "job-1",
                "shell_execution_id": "shell-1",
            }
        )
        write_events(
            events_path,
            [event_line("shell_started", pool="default", seq=1), weird + "\n"],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        assert len(builder.shells()) == 1

    def test_malformed_json_line_is_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                "{not valid json at all\n",
                event_line(
                    "query_discovered",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                    seq=2,
                ),
            ],
        )
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        assert [q.query_id for q in shells[0].queries] == [QID_1]

    def test_partial_last_line_is_tolerated(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        complete = event_line("shell_started", pool="default", seq=1) + event_line(
            "query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2
        )
        partial = '{"v": 1, "type": "query_discovered", "job_id": "job-1", "shell_execu'
        events_path.write_text(complete + partial, encoding="utf-8")

        builder = ms.replay_event_file(events_path)
        assert builder is not None
        shells = builder.shells()
        assert len(shells) == 1
        assert [q.query_id for q in shells[0].queries] == [QID_1]

    def test_empty_file_produces_empty_hierarchy_not_unavailable(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        events_path.write_text("", encoding="utf-8")
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        assert builder.shells() == []

    def test_interleaved_partial_line_tail_across_refreshes(self, tmp_path: Path) -> None:
        """Simulates a service repeatedly re-reading a file while a writer
        is mid-append: the tail must never crash and must reach the full
        hierarchy once the writer finishes."""
        events_path = tmp_path / "monitor.events.jsonl"
        client = FakeMonitorClient()
        service = ms.MonitorService(client, clock=FakeClock())

        # First refresh: nothing written yet (file absent).
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is False

        # Writer appends shell_started, then a partial query_discovered line.
        events_path.write_text(
            event_line("shell_started", pool="default", seq=1) + '{"v": 1, "type": "query_discove',
            encoding="utf-8",
        )
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is True
        assert len(snapshot.shell_executions) == 1
        assert snapshot.shell_executions[0].queries == ()

        # Writer completes the line and appends more.
        events_path.write_text(
            event_line("shell_started", pool="default", seq=1)
            + event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2),
            encoding="utf-8",
        )
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is True
        assert len(snapshot.shell_executions[0].queries) == 1
        assert snapshot.shell_executions[0].queries[0].query_id == QID_1


# --------------------------------------------------------------------------
# Polling: sharing, cadence, terminal-stop, eviction
# --------------------------------------------------------------------------


class TestPolling:
    def _job_with_one_running_query(self, tmp_path: Path) -> Path:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2),
            ],
        )
        return tmp_path

    def test_poller_shared_across_two_subscribers_one_client_call_stream(
        self, tmp_path: Path
    ) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        client.default_response = make_observation(phase="running")
        clock = FakeClock()
        service = ms.MonitorService(
            client, clock=clock, foreground_poll_seconds=2.0, background_poll_seconds=30.0
        )

        service.subscribe("job-1", job_dir)
        service.subscribe("job-1", job_dir)  # second subscriber, same job

        service.run_pending()
        assert len(client.calls) == 1  # exactly one call despite two subscribers
        assert service.poller_count("job-1") == 1

        clock.advance(2.0)
        service.run_pending()
        assert len(client.calls) == 2  # still one poller, one call per tick

    def test_cadence_switches_from_foreground_to_background_on_unsubscribe(
        self, tmp_path: Path
    ) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        client.default_response = make_observation(phase="running")
        clock = FakeClock()
        service = ms.MonitorService(
            client, clock=clock, foreground_poll_seconds=2.0, background_poll_seconds=30.0
        )

        service.subscribe("job-1", job_dir)
        service.run_pending()
        assert len(client.calls) == 1

        # Foreground cadence: due again after 2s, not yet at 1s.
        clock.advance(1.0)
        service.run_pending()
        assert len(client.calls) == 1
        clock.advance(1.0)
        service.run_pending()
        assert len(client.calls) == 2

        service.unsubscribe("job-1")
        # Background cadence now: next poll is 30s out, not 2s.
        clock.advance(2.0)
        service.run_pending()
        assert len(client.calls) == 2  # not yet due at background cadence
        clock.advance(28.0)
        service.run_pending()
        assert len(client.calls) == 3

    def test_cadence_switches_back_to_foreground_on_resubscribe(self, tmp_path: Path) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        client.default_response = make_observation(phase="running")
        clock = FakeClock()
        service = ms.MonitorService(
            client, clock=clock, foreground_poll_seconds=2.0, background_poll_seconds=30.0
        )
        service.register_job("job-1", job_dir)  # 0 subscribers -> background
        service.run_pending()
        call_count = len(client.calls)

        clock.advance(2.0)
        service.run_pending()
        assert len(client.calls) == call_count  # still background cadence, not due

        service.subscribe("job-1", job_dir)
        clock.advance(2.0)
        service.run_pending()
        assert len(client.calls) == call_count + 1  # now due at foreground cadence

    def test_terminal_observation_polls_exactly_once_more_then_stops(self, tmp_path: Path) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        client.queue(
            COORD_1,
            QID_1,
            make_observation(phase="running"),
            make_observation(phase="succeeded", raw_state="FINISHED"),
            make_observation(phase="succeeded", raw_state="FINISHED"),
        )
        clock = FakeClock()
        service = ms.MonitorService(
            client, clock=clock, foreground_poll_seconds=2.0, background_poll_seconds=30.0
        )
        service.subscribe("job-1", job_dir)

        service.run_pending()  # running
        assert service.poller_count("job-1") == 1
        assert len(client.calls) == 1

        clock.advance(2.0)
        service.run_pending()  # succeeded: terminal, one extra poll scheduled
        assert len(client.calls) == 2
        assert service.poller_count("job-1") == 1  # not stopped yet

        clock.advance(2.0)
        service.run_pending()  # the one extra poll
        assert len(client.calls) == 3
        assert service.poller_count("job-1") == 0  # stopped after extra poll

        # No further polling even after much more time passes.
        clock.advance(100.0)
        service.run_pending()
        assert len(client.calls) == 3

        snapshot = service.snapshot("job-1")
        leaf_observation = snapshot.shell_executions[0].queries[0].observation
        assert leaf_observation is not None
        assert leaf_observation.phase == "succeeded"

    def test_terminal_failed_also_stops_after_one_extra_poll(self, tmp_path: Path) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        client.queue(
            COORD_1,
            QID_1,
            make_observation(phase="failed", raw_state="EXCEPTION"),
            make_observation(phase="failed", raw_state="EXCEPTION"),
        )
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)
        service.subscribe("job-1", job_dir)

        service.run_pending()
        assert service.poller_count("job-1") == 1
        clock.advance(2.0)
        service.run_pending()
        assert service.poller_count("job-1") == 0
        assert len(client.calls) == 2

    def test_eviction_retains_last_good_observation_and_sets_availability_error(
        self, tmp_path: Path
    ) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        good = make_observation(phase="running", raw_state="RUNNING")
        evicted = make_observation(
            phase="unknown", raw_state=None, availability_error="Unknown query id"
        )
        client.queue(COORD_1, QID_1, good, evicted)
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)
        service.subscribe("job-1", job_dir)

        service.run_pending()
        snapshot = service.snapshot("job-1")
        observation = snapshot.shell_executions[0].queries[0].observation
        assert observation is not None
        assert observation.phase == "running"
        assert observation.availability_error is None

        clock.advance(2.0)
        service.run_pending()
        snapshot = service.snapshot("job-1")
        observation = snapshot.shell_executions[0].queries[0].observation
        assert observation is not None
        # Retains the last good phase/state...
        assert observation.phase == "running"
        assert observation.raw_state == "RUNNING"
        # ...but surfaces the availability error. Never synthesized into
        # success or failure.
        assert observation.availability_error == "Unknown query id"

    def test_eviction_before_any_good_observation_surfaces_unknown_availability(
        self, tmp_path: Path
    ) -> None:
        job_dir = self._job_with_one_running_query(tmp_path)
        client = FakeMonitorClient()
        client.default_response = make_observation(
            phase="unknown", raw_state=None, availability_error="monitoring unavailable"
        )
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)
        service.subscribe("job-1", job_dir)

        service.run_pending()
        snapshot = service.snapshot("job-1")
        observation = snapshot.shell_executions[0].queries[0].observation
        assert observation is not None
        assert observation.phase == "unknown"
        assert observation.availability_error == "monitoring unavailable"
        # Never synthesized into success or failure.
        assert observation.phase not in ("succeeded", "failed")

    def test_transparent_retry_query_is_the_one_polled(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2),
                event_line(
                    "query_retried", coordinator_base_url=COORD_1, query_id=QID_RETRY, seq=3
                ),
            ],
        )
        client = FakeMonitorClient()
        client.default_response = make_observation(phase="running")
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)
        service.subscribe("job-1", tmp_path)

        service.run_pending()
        assert len(client.calls) == 1
        assert client.calls[0].query_id == QID_RETRY
        assert client.calls[0].relation == "transparent_retry"

    def test_retry_discovered_after_initial_poller_already_active_supersedes_it(
        self, tmp_path: Path
    ) -> None:
        """A retry arriving in a *later* refresh cycle must prune the poller
        for the query it supersedes, not just skip creating a duplicate.

        Regression test: previously ``_prune_pollers_for_job`` only removed a
        poller once it was both non-live *and* already marked ``stopped``
        (a flag only ever set by a terminal observation). A poller for a
        query superseded mid-flight by a ``query_retried`` event discovered
        in a subsequent refresh is never terminal and was therefore never
        pruned, leaving two pollers alive (and being polled) for what the
        hierarchy says is one live query.
        """
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2),
            ],
        )
        client = FakeMonitorClient()
        client.default_response = make_observation(phase="running")
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)
        service.subscribe("job-1", tmp_path)

        # First refresh cycle: only QID_1 is known, its poller is created and
        # polled once.
        service.run_pending()
        assert len(client.calls) == 1
        assert client.calls[0].query_id == QID_1
        assert service.poller_count("job-1") == 1

        # The retry is discovered only now, in a later cycle -- QID_1's
        # poller already exists and has already been polled.
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(
                event_line(
                    "query_retried",
                    coordinator_base_url=COORD_1,
                    query_id=QID_RETRY,
                    seq=3,
                )
            )

        clock.advance(2.0)
        service.run_pending()

        # Exactly one live poller remains: the retry's. QID_1's poller must
        # have been pruned, not left running alongside it.
        assert service.poller_count("job-1") == 1
        snapshot = service.snapshot("job-1")
        leaf = snapshot.shell_executions[0].queries[0]
        assert leaf.retries[-1].query_id == QID_RETRY

        # Only the retry gets polled from here on -- QID_1 must never be
        # observed again.
        clock.advance(2.0)
        service.run_pending()
        polled_ids = {call.query_id for call in client.calls}
        assert QID_RETRY in polled_ids
        assert all(call.query_id != QID_1 for call in client.calls[1:])


# --------------------------------------------------------------------------
# Rebuild-from-sidecar-after-restart
# --------------------------------------------------------------------------


class TestRestartRecovery:
    def test_fresh_service_instance_over_same_file_reaches_same_hierarchy(
        self, tmp_path: Path
    ) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", shell_execution_id="shell-a", pool="default", seq=1),
                event_line(
                    "query_discovered",
                    shell_execution_id="shell-a",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                    seq=2,
                ),
                event_line(
                    "query_retried",
                    shell_execution_id="shell-a",
                    coordinator_base_url=COORD_1,
                    query_id=QID_RETRY,
                    seq=3,
                ),
                event_line("shell_finished", shell_execution_id="shell-a", returncode=1, seq=4),
                event_line("shell_started", shell_execution_id="shell-b", pool="adhoc", seq=5),
                event_line(
                    "query_discovered",
                    shell_execution_id="shell-b",
                    coordinator_base_url=COORD_2,
                    query_id=QID_2,
                    seq=6,
                ),
            ],
        )

        # First "TUI session": a service instance builds the hierarchy.
        client_1 = FakeMonitorClient()
        client_1.default_response = make_observation(phase="running")
        clock_1 = FakeClock()
        service_1 = ms.MonitorService(client_1, clock=clock_1, foreground_poll_seconds=2.0)
        first_snapshot = service_1.subscribe("job-1", tmp_path)
        service_1.run_pending()

        # "TUI restart": a brand-new service instance, no shared state,
        # rebuilt purely from the sidecar file.
        client_2 = FakeMonitorClient()
        client_2.default_response = make_observation(phase="running")
        clock_2 = FakeClock()
        service_2 = ms.MonitorService(client_2, clock=clock_2, foreground_poll_seconds=2.0)
        second_snapshot = service_2.subscribe("job-1", tmp_path)
        service_2.run_pending()

        def hierarchy_shape(snapshot: ms.MonitorSnapshot) -> list[tuple]:
            return [
                (
                    shell.pool,
                    shell.returncode,
                    [
                        (
                            query.query_id,
                            query.relation,
                            [(r.query_id, r.relation) for r in query.retries],
                        )
                        for query in shell.queries
                    ],
                )
                for shell in snapshot.shell_executions
            ]

        assert hierarchy_shape(first_snapshot) == hierarchy_shape(second_snapshot)
        assert hierarchy_shape(second_snapshot) == [
            ("default", 1, [(QID_1, "initial", [(QID_RETRY, "transparent_retry")])]),
            ("adhoc", None, [(QID_2, "initial", [])]),
        ]

        # Both fresh services reach exactly one poll for each live leaf
        # query (shell-a's retry, shell-b's initial query).
        assert len(client_1.calls) == 2
        assert len(client_2.calls) == 2
        polled_ids_1 = sorted(identity.query_id for identity in client_1.calls)
        polled_ids_2 = sorted(identity.query_id for identity in client_2.calls)
        assert polled_ids_1 == polled_ids_2 == sorted([QID_RETRY, QID_2])


# --------------------------------------------------------------------------
# Invariant: never writes anything
# --------------------------------------------------------------------------


class TestNeverWrites:
    def test_service_never_writes_the_event_file_or_any_other_file(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2),
            ],
        )
        before_mtime = events_path.stat().st_mtime
        before_size = events_path.stat().st_size
        before_files = sorted(p.name for p in tmp_path.iterdir())

        client = FakeMonitorClient()
        client.queue(
            COORD_1,
            QID_1,
            make_observation(phase="running"),
            make_observation(phase="succeeded", raw_state="FINISHED"),
            make_observation(phase="succeeded", raw_state="FINISHED"),
        )
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)

        service.subscribe("job-1", tmp_path)
        service.run_pending()
        clock.advance(2.0)
        service.run_pending()  # terminal
        clock.advance(2.0)
        service.run_pending()  # extra poll
        service.unsubscribe("job-1")
        service.register_job("job-1", tmp_path)

        after_mtime = events_path.stat().st_mtime
        after_size = events_path.stat().st_size
        after_files = sorted(p.name for p in tmp_path.iterdir())

        assert before_mtime == after_mtime
        assert before_size == after_size
        assert before_files == after_files

    def test_no_monitor_service_source_line_opens_a_file_for_writing(self) -> None:
        """Static tripwire: no ``open(...)``/``Path.write_*``/``mkdir`` call
        anywhere in the module. Guards the invariant even if a future edit
        adds a new code path."""
        import inspect

        source = inspect.getsource(ms)
        forbidden = ["write_text(", "write_bytes(", '"w")', "'w')", "mkdir(", "os.remove"]
        for token in forbidden:
            assert token not in source, f"found forbidden write-like call: {token!r}"

    def test_service_does_not_touch_manifest_module(self, tmp_path: Path) -> None:
        """The service must re-derive nothing from and never call into
        dispatch.manifest/dispatch.jobs write paths; it only reads the
        sidecar file itself."""
        import inspect

        source = inspect.getsource(ms)
        assert "manifest.update" not in source
        assert "manifest.create_job" not in source


# --------------------------------------------------------------------------
# Background thread smoke test (real thread, tiny cadence, explicit stop)
# --------------------------------------------------------------------------


class TestBackgroundThread:
    def test_start_and_stop_real_daemon_thread(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", pool="default", seq=1),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2),
            ],
        )
        client = FakeMonitorClient()
        client.default_response = make_observation(phase="running")
        service = ms.MonitorService(
            client, foreground_poll_seconds=0.02, background_poll_seconds=0.05
        )
        service.start()
        assert service._thread is not None
        assert service._thread.is_alive()
        try:
            service.subscribe("job-1", tmp_path)
            deadline = time.monotonic() + 2.0
            while len(client.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(client.calls) >= 2
        finally:
            service.unsubscribe("job-1")
            service.stop(timeout=2.0)
        assert not service._thread.is_alive()

    def test_start_is_idempotent(self) -> None:
        client = FakeMonitorClient()
        service = ms.MonitorService(client)
        service.start()
        first_thread = service._thread
        service.start()
        assert service._thread is first_thread
        service.stop(timeout=2.0)


# --------------------------------------------------------------------------
# Module hygiene
# --------------------------------------------------------------------------


class TestModuleHygiene:
    def test_importing_monitor_service_pulls_in_no_textual_modules(self) -> None:
        # Check the import in a fresh subprocess interpreter. Mutating this
        # process's sys.modules instead (deleting textual entries) leaves two
        # generations of textual alive and breaks every Textual test that
        # later runs in the same pytest-xdist worker.
        probe = (
            "import sys\n"
            "import dispatch.monitor_service\n"
            "bad = [m for m in sys.modules\n"
            "       if m == 'textual' or m.startswith('textual.')]\n"
            "if bad:\n"
            "    print('pulled in: ' + ', '.join(sorted(bad)))\n"
            "    sys.exit(1)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, (
            f"importing dispatch.monitor_service in a fresh interpreter loaded "
            f"forbidden modules: {result.stdout}{result.stderr}"
        )

    def test_module_source_has_no_textual_or_asyncio_imports(self) -> None:
        source = Path(ms.__file__).read_text(encoding="utf-8")
        for token in ("import textual", "from textual", "import asyncio", "from asyncio"):
            assert token not in source, f"found forbidden import token {token!r}"

    def test_snapshot_and_hierarchy_dataclasses_are_frozen(self) -> None:
        snapshot = ms.MonitorSnapshot(
            job_id="job-1", available=True, unavailable_reason=None, shell_executions=()
        )
        with pytest.raises(Exception):
            snapshot.job_id = "job-2"  # type: ignore[misc]

        shell = ms.ShellExecutionAttempt(shell_execution_id="s1", pool="default", seq=1)
        with pytest.raises(Exception):
            shell.pool = "other"  # type: ignore[misc]

        query = ms.QueryAttempt(
            query_id=QID_1,
            coordinator_base_url=COORD_1,
            relation="initial",
            shell_execution_id="s1",
            discovered_at="2026-07-15T10:00:00Z",
            seq=1,
        )
        with pytest.raises(Exception):
            query.query_id = "other"  # type: ignore[misc]
