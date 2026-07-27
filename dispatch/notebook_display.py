"""Jupyter rendering for the notebook API: HTML views and the live watch view.

Pure presentation. Jobs arrive as duck-typed objects rather than an imported
type so this module stays free of any dependency on :mod:`dispatch.notebook`,
which imports it.

Outside an IPython kernel every function still returns markup, and
:class:`LiveView` degrades to printing one line per state change, so the same
notebook code runs in a plain script.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

from .runtime import is_jupyter_notebook

try:  # Optional: self-updating output exists only inside IPython kernels.
    from IPython import display as ipython_display
except ImportError:
    ipython_display = None

STATE_COLORS = {
    "Pending": "#8a6d00",
    "Running": "#0b5cad",
    "Succeeded": "#1a7f37",
    "Failed": "#b42318",
    "Cancelled": "#57606a",
}


def format_duration(seconds: float | None) -> str:
    """Human-readable elapsed time, or ``--`` when the Job has not started."""
    if seconds is None:
        return "--"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def state_html(state: str) -> str:
    color = STATE_COLORS.get(state, "#57606a")
    return f"<span style='color:{color};font-weight:600'>{html.escape(state)}</span>"


def job_html(job: Any) -> str:
    """One Job as a label/value table."""
    rows = [
        ("Job ID", html.escape(job.id)),
        ("State", state_html(job.state)),
        ("Source", html.escape(f"{job.source or '--'} {job.source_detail or ''}".strip())),
        (
            "Destination",
            html.escape(f"{job.destination or '--'} {job.destination_detail or ''}".strip()),
        ),
        ("Elapsed", format_duration(job.elapsed_seconds)),
        ("Exit code", "--" if job.exit_code is None else str(job.exit_code)),
    ]
    cells = "".join(
        f"<tr><th style='text-align:left;padding:2px 12px 2px 0'>{html.escape(label)}</th>"
        f"<td style='text-align:left'>{value}</td></tr>"
        for label, value in rows
    )
    return f"<table style='border:none'><tbody>{cells}</tbody></table>"


def job_list_html(jobs: Sequence[Any]) -> str:
    """Several Jobs as one table."""
    if not jobs:
        return "<em>No Jobs.</em>"
    headers = ("Job ID", "State", "Source", "Destination", "Elapsed", "Exit")
    head = "".join(
        f"<th style='text-align:left;padding-right:12px'>{name}</th>" for name in headers
    )
    body = []
    for job in jobs:
        cells = (
            html.escape(job.id),
            state_html(job.state),
            html.escape(job.source or "--"),
            html.escape(job.destination or "--"),
            format_duration(job.elapsed_seconds),
            "--" if job.exit_code is None else str(job.exit_code),
        )
        body.append(
            "<tr>"
            + "".join(f"<td style='padding-right:12px'>{cell}</td>" for cell in cells)
            + "</tr>"
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def watch_html(job: Any, log_lines: Sequence[str]) -> str:
    """One Job's state plus its log tail, for the live watch view."""
    log = html.escape("\n".join(log_lines)) or "(no log output yet)"
    return (
        "<div style='font-family:monospace'>"
        f"<div><b>{html.escape(job.id)}</b> {state_html(job.state)} "
        f"&middot; {html.escape(job.destination or '--')} "
        f"&middot; {format_duration(job.elapsed_seconds)}</div>"
        "<pre style='margin:4px 0 0;padding:8px;background:#f6f8fa;"
        f"max-height:18em;overflow:auto'>{log}</pre>"
        "</div>"
    )


class LiveView:
    """Render repeated Job snapshots in place (Jupyter) or as change lines (stdout)."""

    def __init__(self, *, rich: bool | None = None) -> None:
        available = ipython_display is not None
        self._rich = (is_jupyter_notebook() and available) if rich is None else (rich and available)
        self._handle: Any = None
        self._last = ""

    def update(self, job: Any, log_lines: Sequence[str]) -> None:
        if self._rich:
            payload = ipython_display.HTML(watch_html(job, log_lines))
            if self._handle is None:
                self._handle = ipython_display.display(payload, display_id=True)
            else:
                self._handle.update(payload)
            return
        summary = f"{job.id} {job.state} ({format_duration(job.elapsed_seconds)})"
        if summary != self._last:
            print(summary, flush=True)
            self._last = summary
