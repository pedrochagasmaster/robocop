from __future__ import annotations

import json
import sys
from pathlib import Path

from tools.prod_tui import __main__ as prod_tui_cli
from tools.prod_tui import latency_probe


def test_latency_summary_reports_median_p95_and_raw_samples() -> None:
    result = latency_probe._summary([10.0, 20.0, 30.0, 40.0])
    assert result["n"] == 4
    assert result["median_ms"] == 25.0
    assert result["p95_ms"] == 40.0
    assert result["samples_ms"] == [10.0, 20.0, 30.0, 40.0]


def test_latency_comparison_reports_positive_improvement() -> None:
    baseline = {"measurements": {"browser_toggle_ms": {"median_ms": 100.0}}}
    candidate = {"measurements": {"browser_toggle_ms": {"median_ms": 40.0}}}
    compared = latency_probe._compare(candidate, baseline)
    assert compared["browser_toggle_ms"] == {
        "baseline_median_ms": 100.0,
        "candidate_median_ms": 40.0,
        "delta_ms": -60.0,
        "improvement_pct": 60.0,
    }


def test_top_level_latency_command_dispatches(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_latency_main(argv: list[str]) -> int:
        calls.append(argv)
        return 5

    monkeypatch.setattr(latency_probe, "main", fake_latency_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["python -m tools.prod_tui", "latency", "--config", "config.yaml", "--samples", "3"],
    )
    assert prod_tui_cli.main() == 5
    assert calls == [["--config", "config.yaml", "--samples", "3"]]


def test_latency_guide_documents_ab_comparison_and_safety() -> None:
    guide = Path("tools/prod_tui/LATENCY.md").read_text(encoding="utf-8")
    assert "python -m tools.prod_tui latency" in guide
    assert "--baseline-report" in guide
    assert "does not launch or cancel Jobs" in guide
    assert "does not DROP tables" in guide
    assert "capture_overhead_ms" in guide


def test_latency_main_writes_report_and_comparison(tmp_path: Path, monkeypatch) -> None:
    report = {
        "host": "user@edge",
        "deployment_sha": "a" * 40,
        "measurements": {
            "browser_toggle_ms": {"n": 1, "median_ms": 40.0, "p95_ms": 40.0}
        },
        "notes": {},
    }
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"measurements": {"browser_toggle_ms": {"median_ms": 100.0}}}),
        encoding="utf-8",
    )
    output = tmp_path / "candidate.json"
    monkeypatch.setattr(latency_probe, "load_config", lambda _path: object())
    monkeypatch.setattr(latency_probe, "run_probe", lambda *_args, **_kwargs: dict(report))

    assert (
        latency_probe.main(
            [
                "--config",
                "ignored.yaml",
                "--json-report",
                str(output),
                "--baseline-report",
                str(baseline),
            ]
        )
        == 0
    )
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["comparison"]["browser_toggle_ms"]["improvement_pct"] == 60.0
