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


def v2_event_line(
    event_type: str,
    *,
    call_id: str = "call-0001",
    call_index: int = 1,
    script: str = "download_to_csv.py",
    shell_relation: str = "initial",
    **kwargs: object,
) -> str:
    payload = json.loads(event_line(event_type, **kwargs))
    payload.update(
        v=2,
        orchestrator_call_id=call_id,
        orchestrator_call_index=call_index,
        orchestrator_script=script,
        shell_relation=shell_relation,
    )
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
        self.coordinator_discovery_calls: list[str] = []
        self.discovery_criteria_calls: list[im.DiscoveryCriteria] = []
        self.discovered_identity: im.QueryIdentity | None = None
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

    def discover_coordinators(self, seed_base_url: str) -> list[str]:
        self.coordinator_discovery_calls.append(seed_base_url)
        return [seed_base_url]

    def discover(self, criteria: im.DiscoveryCriteria) -> im.QueryIdentity:
        self.discovery_criteria_calls.append(criteria)
        if self.discovered_identity is None:
            raise RuntimeError("no discovery result")
        return self.discovered_identity


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
    def test_tail_skips_unchanged_body_and_reads_only_appended_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        initial = event_line("shell_started")
        write_events(events_path, [initial])
        real_open = Path.open
        open_count = 0
        read_sizes: list[int] = []

        class CountingReader:
            def __init__(self, wrapped: object) -> None:
                self.wrapped = wrapped

            def __enter__(self) -> CountingReader:
                self.wrapped.__enter__()  # type: ignore[attr-defined]
                return self

            def __exit__(self, *args: object) -> object:
                return self.wrapped.__exit__(*args)  # type: ignore[attr-defined]

            def seek(self, offset: int) -> int:
                return self.wrapped.seek(offset)  # type: ignore[attr-defined,no-any-return]

            def read(self, size: int = -1) -> bytes:
                read_sizes.append(size)
                return self.wrapped.read(size)  # type: ignore[attr-defined,no-any-return]

        def counting_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
            nonlocal open_count
            opened = real_open(path, mode, *args, **kwargs)
            if path == events_path and mode == "rb":
                open_count += 1
                return CountingReader(opened)
            return opened

        monkeypatch.setattr(Path, "open", counting_open)
        service = ms.MonitorService(FakeMonitorClient(), clock=FakeClock())
        service.register_job("job-1", tmp_path)
        assert open_count == 1
        for _ in range(10):
            service.run_pending()
        assert open_count == 1

        suffix = event_line("shell_finished", returncode=0)
        size_before = events_path.stat().st_size
        with real_open(events_path, "a", encoding="utf-8") as handle:
            handle.write(suffix)
        appended_bytes = events_path.stat().st_size - size_before
        service.register_job("job-1", tmp_path)
        assert open_count == 2
        assert read_sizes[-1] == appended_bytes

    def test_truncation_replays_once_then_returns_to_unchanged_fast_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started"),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1),
            ],
        )
        real_open = Path.open
        body_opens = 0

        def counting_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
            nonlocal body_opens
            if path == events_path and mode == "rb":
                body_opens += 1
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)
        service = ms.MonitorService(FakeMonitorClient(), clock=FakeClock())
        service.register_job("job-1", tmp_path)
        with real_open(events_path, "w", encoding="utf-8") as handle:
            handle.write(event_line("shell_started", shell_execution_id="replacement"))
        snapshot = service.register_job("job-1", tmp_path)
        service.register_job("job-1", tmp_path)

        assert body_opens == 2
        shell = snapshot.orchestrator_calls[0].shell_executions[0]
        assert shell.shell_execution_id == "replacement"

    def test_v2_groups_pool_fallback_shells_under_the_same_call(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                v2_event_line("shell_started", shell_execution_id="shell-a"),
                v2_event_line(
                    "query_discovered",
                    shell_execution_id="shell-a",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                ),
                v2_event_line(
                    "shell_started",
                    shell_execution_id="shell-b",
                    pool="adhoc",
                    shell_relation="orchestrator_pool_fallback",
                ),
                v2_event_line(
                    "query_discovered",
                    shell_execution_id="shell-b",
                    pool="adhoc",
                    shell_relation="orchestrator_pool_fallback",
                    coordinator_base_url=COORD_2,
                    query_id=QID_2,
                ),
            ],
        )

        builder = ms.replay_event_file(events_path)
        assert builder is not None
        calls = builder.calls()
        assert [(call.call_id, call.index, call.script) for call in calls] == [
            ("call-0001", 1, "download_to_csv.py")
        ]
        assert [shell.shell_relation for shell in builder.shells(calls[0])] == [
            "initial",
            "orchestrator_pool_fallback",
        ]

    def test_v2_separate_manifest_calls_are_not_fallbacks(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                v2_event_line("shell_started", shell_execution_id="shell-a"),
                v2_event_line(
                    "shell_started",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-b",
                ),
            ],
        )

        builder = ms.replay_event_file(events_path)
        assert builder is not None
        calls = builder.calls()
        assert [call.call_id for call in calls] == ["call-0001", "call-0002"]
        assert [call.shells[0].shell_relation for call in calls] == ["initial", "initial"]

    def test_v2_conflicting_call_metadata_is_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                v2_event_line("shell_started", shell_execution_id="shell-a"),
                v2_event_line(
                    "query_discovered",
                    shell_execution_id="shell-a",
                    script="conflicting.py",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                ),
            ],
        )

        builder = ms.replay_event_file(events_path)
        assert builder is not None
        assert builder.calls()[0].shells[0].queries == []

    def test_mixed_v1_and_v2_never_invents_legacy_lineage(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started", shell_execution_id="legacy-a"),
                event_line("shell_started", shell_execution_id="legacy-b"),
                v2_event_line("shell_started", shell_execution_id="v2-a"),
            ],
        )

        builder = ms.replay_event_file(events_path)
        assert builder is not None
        calls = builder.calls()
        assert len(calls) == 3
        assert calls[0].shells[0].shell_relation == "unknown_legacy"
        assert calls[1].shells[0].shell_relation == "unknown_legacy"
        assert calls[0].call_id != calls[1].call_id
        assert calls[2].shells[0].shell_relation == "initial"

    def test_absent_file_means_monitoring_unavailable(self, tmp_path: Path) -> None:
        builder = ms.replay_event_file(tmp_path / "monitor.events.jsonl")
        assert builder is None

    def test_absent_file_via_service_snapshot(self, tmp_path: Path) -> None:
        client = FakeMonitorClient()
        service = ms.MonitorService(client, clock=FakeClock())
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is False
        assert snapshot.unavailable_reason == "monitoring unavailable"
        assert snapshot.orchestrator_calls == ()

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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        assert sum(len(builder.shells(call)) for call in builder.calls()) == 1

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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
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
        shells = [shell for call in builder.calls() for shell in builder.shells(call)]
        assert len(shells) == 1
        assert [q.query_id for q in shells[0].queries] == [QID_1]

    def test_empty_file_produces_empty_hierarchy_not_unavailable(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        events_path.write_text("", encoding="utf-8")
        builder = ms.replay_event_file(events_path)
        assert builder is not None
        assert builder.calls() == []

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
        full_query_line = event_line(
            "query_discovered", coordinator_base_url=COORD_1, query_id=QID_1, seq=2
        )
        partial_prefix = full_query_line[:35]
        events_path.write_text(
            event_line("shell_started", pool="default", seq=1) + partial_prefix,
            encoding="utf-8",
        )
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is True
        assert len(snapshot.orchestrator_calls) == 1
        assert snapshot.orchestrator_calls[0].shell_executions[0].queries == ()

        # Writer completes the same append-only line.
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(full_query_line[len(partial_prefix) :])
        snapshot = service.register_job("job-1", tmp_path)
        assert snapshot.available is True
        shell = snapshot.orchestrator_calls[0].shell_executions[0]
        assert len(shell.queries) == 1
        assert shell.queries[0].query_id == QID_1


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

    def test_superseded_attempt_keeps_failure_and_gets_one_final_confirmation(
        self, tmp_path: Path
    ) -> None:
        events_path = self._job_with_one_running_query(tmp_path) / "monitor.events.jsonl"
        client = FakeMonitorClient()
        client.queue(
            COORD_1,
            QID_1,
            make_observation(phase="failed", raw_state="EXCEPTION"),
            make_observation(
                phase="unknown", raw_state=None, availability_error="Unknown query id"
            ),
        )
        client.queue(COORD_1, QID_RETRY, make_observation(phase="running"))
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock, foreground_poll_seconds=2.0)
        service.subscribe("job-1", tmp_path)
        service.run_pending()

        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                event_line(
                    "query_retried",
                    coordinator_base_url=COORD_1,
                    query_id=QID_RETRY,
                    seq=3,
                )
            )
        service.run_pending()

        parent = service.snapshot("job-1").orchestrator_calls[0].shell_executions[0].queries[0]
        assert parent.observation is not None
        assert parent.observation.raw_state == "EXCEPTION"
        assert parent.observation.phase == "failed"
        assert parent.observation.availability_error == "Unknown query id"
        assert parent.retries[0].observation is not None
        assert [call.query_id for call in client.calls].count(QID_1) == 2

        clock.advance(10.0)
        service.run_pending()
        assert [call.query_id for call in client.calls].count(QID_1) == 2


class TestBackgroundRegistration:
    def test_background_cadence_tightens_for_detail_without_duplicate_poller(
        self, tmp_path: Path
    ) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started"),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1),
            ],
        )
        client = FakeMonitorClient()
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock)

        service.sync_background_jobs({("job-1", tmp_path)})
        service.run_pending()
        assert len(client.calls) == 1
        assert service.poller_count("job-1") == 1
        clock.advance(29)
        service.run_pending()
        assert len(client.calls) == 1
        clock.advance(1)
        service.run_pending()
        assert len(client.calls) == 2

        service.subscribe("job-1", tmp_path)
        assert service.poller_count("job-1") == 1
        clock.advance(2)
        service.run_pending()
        assert len(client.calls) == 3
        service.unsubscribe("job-1")
        clock.advance(2)
        service.run_pending()
        assert len(client.calls) == 3
        clock.advance(28)
        service.run_pending()
        assert len(client.calls) == 4

    def test_unlisted_terminal_job_prunes_after_final_confirmation(self, tmp_path: Path) -> None:
        events_path = tmp_path / "monitor.events.jsonl"
        write_events(
            events_path,
            [
                event_line("shell_started"),
                event_line("query_discovered", coordinator_base_url=COORD_1, query_id=QID_1),
            ],
        )
        terminal = make_observation(phase="succeeded", raw_state="FINISHED")
        client = FakeMonitorClient()
        client.queue(COORD_1, QID_1, terminal, terminal)
        clock = FakeClock()
        service = ms.MonitorService(client, clock=clock)
        service.sync_background_jobs({("job-1", tmp_path)})
        service.run_pending()
        service.sync_background_jobs(set())
        assert service.snapshot("job-1").available
        clock.advance(30)
        service.run_pending()
        assert not service.snapshot("job-1").available


class TestIdentityRecovery:
    def _job_with_one_running_query(self, tmp_path: Path) -> Path:
        return TestPolling._job_with_one_running_query(self, tmp_path)

    def _recoverable_job(self, tmp_path: Path) -> None:
        job_manifest = {
            "schema_version": 1,
            "id": "job-1",
            "tool": "dispatch",
            "user": "user_a",
            "source": {"type": "SqlFile", "sql_path_at_launch": "input.sql"},
            "destination": {
                "type": "Table+Csv",
                "schema": "db_a",
                "table_name": "table_a",
                "csv_path": "out.csv",
            },
            "params": {},
            "orchestrator_calls": [
                {"script": "Query_Impala_Parametrized.py", "argv": ["python", "first.py"]},
                {
                    "script": "download_to_csv.py",
                    "argv": [
                        "python",
                        "download_to_csv.py",
                        "--table-name",
                        "db_a.table_a",
                        "--output-file",
                        "out.csv",
                    ],
                },
            ],
            "state": "Running",
            "pid": 123,
            "started_at": "2026-07-15T10:00:00Z",
            "finished_at": None,
            "exit_code": None,
        }
        (tmp_path / "manifest.json").write_text(json.dumps(job_manifest), encoding="utf-8")
        write_events(
            tmp_path / "monitor.events.jsonl",
            [
                v2_event_line(
                    "shell_started",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-recover",
                )
            ],
        )

    def test_criteria_are_exactly_derived_and_unique_identity_is_attached(
        self, tmp_path: Path
    ) -> None:
        self._recoverable_job(tmp_path)
        client = FakeMonitorClient()
        client.discovered_identity = im.QueryIdentity(
            coordinator_base_url=COORD_1,
            query_id=QID_1,
            shell_execution_id="shell-recover",
            relation="initial",
            discovered_at="2026-07-15T10:00:30Z",
        )
        service = ms.MonitorService(client, clock=FakeClock())
        service.sync_background_jobs({("job-1", tmp_path)})

        criteria = service.recovery_criteria("job-1", "call-0002")
        assert criteria.statement_prefix == "select * from db_a.table_a;"
        assert criteria.statement_type == "QUERY"
        assert criteria.database == "db_a"
        assert criteria.orchestrator_call_id == "call-0002"
        assert criteria.orchestrator_call_index == 2
        snapshot = service.recover_identity("job-1", "call-0002", criteria, seed_url=COORD_1)

        query = snapshot.orchestrator_calls[0].shell_executions[0].queries[0]
        assert query.query_id == QID_1
        assert client.coordinator_discovery_calls == [COORD_1]
        assert client.discovery_criteria_calls == [criteria]
        with (tmp_path / "monitor.events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                v2_event_line(
                    "shell_finished",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-recover",
                    returncode=0,
                )
            )
        service.run_pending()
        assert service.snapshot("job-1").orchestrator_calls[0].shell_executions[0].queries

        # A bounded truncation replay reattaches only the in-memory recovery;
        # a fresh process would have no such map and must recover again.
        write_events(
            tmp_path / "monitor.events.jsonl",
            [
                v2_event_line(
                    "shell_started",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-recover",
                )
            ],
        )
        service.run_pending()
        assert service.snapshot("job-1").orchestrator_calls[0].shell_executions[0].queries

    def test_recovery_refuses_weakened_or_context_free_criteria(self, tmp_path: Path) -> None:
        self._recoverable_job(tmp_path)
        client = FakeMonitorClient()
        service = ms.MonitorService(client, clock=FakeClock())
        service.sync_background_jobs({("job-1", tmp_path)})
        criteria = service.recovery_criteria("job-1", "call-0002")
        weakened = im.DiscoveryCriteria(**{**criteria.__dict__, "statement_prefix": "select *"})
        with pytest.raises(ms.IdentityRecoveryError, match="identity unavailable/ambiguous"):
            service.recover_identity("job-1", "call-0002", weakened, seed_url=COORD_1)

    def test_one_missing_fallback_shell_is_recoverable_from_captured_seed(
        self, tmp_path: Path
    ) -> None:
        self._recoverable_job(tmp_path)
        write_events(
            tmp_path / "monitor.events.jsonl",
            [
                v2_event_line(
                    "shell_started",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-initial",
                ),
                v2_event_line(
                    "query_discovered",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-initial",
                    coordinator_base_url=COORD_1,
                    query_id=QID_1,
                ),
                v2_event_line(
                    "shell_started",
                    call_id="call-0002",
                    call_index=2,
                    script="download_to_csv.py",
                    shell_execution_id="shell-recover",
                    shell_relation="orchestrator_pool_fallback",
                ),
            ],
        )
        client = FakeMonitorClient()
        client.discovered_identity = im.QueryIdentity(
            coordinator_base_url=COORD_2,
            query_id=QID_2,
            shell_execution_id="shell-recover",
            relation="initial",
            discovered_at="2026-07-15T10:00:30Z",
        )
        service = ms.MonitorService(client, clock=FakeClock())
        service.sync_background_jobs({("job-1", tmp_path)})
        criteria = service.recovery_criteria("job-1", "call-0002")

        snapshot = service.recover_identity("job-1", "call-0002", criteria)

        assert client.coordinator_discovery_calls == [COORD_1]
        assert snapshot.orchestrator_calls[0].shell_executions[1].queries[0].query_id == QID_2

    def test_query_file_call_is_not_eligible(self, tmp_path: Path) -> None:
        self._recoverable_job(tmp_path)
        data = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        data["orchestrator_calls"][1]["argv"] = [
            "python",
            "download_to_csv.py",
            "--query-file",
            "job.sql",
        ]
        (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        service = ms.MonitorService(FakeMonitorClient(), clock=FakeClock())
        service.sync_background_jobs({("job-1", tmp_path)})
        with pytest.raises(ms.IdentityRecoveryError, match="identity unavailable/ambiguous"):
            service.recovery_criteria("job-1", "call-0002")

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
        leaf_observation = snapshot.orchestrator_calls[0].shell_executions[0].queries[0].observation
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
        observation = snapshot.orchestrator_calls[0].shell_executions[0].queries[0].observation
        assert observation is not None
        assert observation.phase == "running"
        assert observation.availability_error is None

        clock.advance(2.0)
        service.run_pending()
        snapshot = service.snapshot("job-1")
        observation = snapshot.orchestrator_calls[0].shell_executions[0].queries[0].observation
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
        observation = snapshot.orchestrator_calls[0].shell_executions[0].queries[0].observation
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
        leaf = snapshot.orchestrator_calls[0].shell_executions[0].queries[0]
        assert leaf.retries[-1].query_id == QID_RETRY

        # QID_1 received exactly one immediate final confirmation poll when
        # superseded; only the retry gets polled from here on.
        assert [call.query_id for call in client.calls].count(QID_1) == 2
        clock.advance(2.0)
        service.run_pending()
        polled_ids = {call.query_id for call in client.calls}
        assert QID_RETRY in polled_ids
        assert [call.query_id for call in client.calls].count(QID_1) == 2


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
                for call in snapshot.orchestrator_calls
                for shell in call.shell_executions
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
            job_id="job-1", available=True, unavailable_reason=None, orchestrator_calls=()
        )
        with pytest.raises(Exception):
            snapshot.job_id = "job-2"  # type: ignore[misc]

        shell = ms.ShellExecutionAttempt(
            shell_execution_id="s1", shell_relation="initial", pool="default", seq=1
        )
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
