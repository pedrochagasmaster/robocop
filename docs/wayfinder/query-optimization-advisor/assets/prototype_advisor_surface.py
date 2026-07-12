"""PROTOTYPE — advisor surface exploration for ticket 0006. Throwaway.

Run interactively:

    python docs/wayfinder/query-optimization-advisor/assets/prototype_advisor_surface.py

Generate SVG screenshots (written next to this file):

    python docs/wayfinder/query-optimization-advisor/assets/prototype_advisor_surface.py --shots

Keys inside the prototype: 1/2/3 switch the mock finding set
(clean / advisory-only / with errors), P opens the Preview surface,
L runs the launch gate, Esc/B goes back, Q quits.

No real analysis happens here: findings are hard-coded examples following the
locked rule catalog's two-part wording shape. The question this prototype
answers: where findings surface (New Job badge + Preview panel + launch
gate) and how they read at production terminal sizes.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import dispatch
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, RichLog, Static

sys.path.insert(0, str(Path(dispatch.__file__).resolve().parents[1]))

from dispatch.screens.preview import sql_syntax  # noqa: E402

MOCK_SQL = """\
SELECT *
FROM core.cut_clear_dtl_enc AS clr
INNER JOIN [BROADCAST] core.cut_clear_dtl_enc AS big
  ON clr.dw_acct_id = big.dw_acct_id
WHERE clr.merchant_name REGEXP '^ABC.*'
UNION
SELECT * FROM core.archive_clear_dtl
"""


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning" | "info"
    rule: str
    ref: str
    detection: str
    remediation: str


ERROR_SET = [
    Finding(
        "error",
        "R07",
        "G#3/#4",
        r"\[BROADCAST] hint found on shuffle-recommended core.cut_clear_dtl_enc (line 3)",
        r"Use \[SHUFFLE] per the recommended join-strategy list (2026-07-10).",
    ),
    Finding(
        "warning",
        "R02",
        "G#1",
        "No dw_process_date predicate found for core.cut_clear_dtl_enc in this query block",
        "Add a dw_process_date filter to this query block.",
    ),
    Finding(
        "info",
        "R12",
        "\u00a78",
        "REGEXP operator found in a predicate (line 5)",
        "LIKE 'ABC%' may suffice and avoids the regex engine.",
    ),
    Finding(
        "info",
        "R13",
        "\u00a78",
        "UNION without ALL found (line 6)",
        "UNION ALL avoids the deduplication overhead if duplicates are acceptable.",
    ),
]

ADVISORY_SET = [finding for finding in ERROR_SET if finding.severity != "error"]

SEVERITY_MARKUP = {
    "error": "[bold red]error[/]",
    "warning": "[yellow]warning[/]",
    "info": "[cyan]info[/]",
}


def badge(findings: list[Finding]) -> str:
    """Worst-severity badge + counts, label-first so color is never the only cue."""
    if not findings:
        return "[green]Advisor: clean[/]"
    counts = {
        severity: sum(1 for f in findings if f.severity == severity)
        for severity in ("error", "warning", "info")
    }
    parts = " \u00b7 ".join(
        f"{count} {severity}{'s' if count > 1 else ''}"
        for severity, count in counts.items()
        if count
    )
    worst = next(sev for sev in ("error", "warning", "info") if counts[sev])
    color = {"error": "red", "warning": "yellow", "info": "cyan"}[worst]
    return f"[{color}]Advisor: {worst}[/] [dim]({parts})[/]"


def finding_lines(finding: Finding) -> str:
    head = (
        f"{SEVERITY_MARKUP[finding.severity]} [bold]{finding.rule}[/]"
        f" [dim]\u00b7 {finding.ref}[/]  {finding.detection}"
    )
    return f"{head}\n      [dim]-> {finding.remediation}[/]"


class LaunchGateModal(ModalScreen[bool]):
    """Enforcement policy surface: errors pause on explicit proceed/cancel."""

    BINDINGS = [("y", "proceed", "Launch anyway"), ("n", "cancel", "Cancel"), ("escape", "cancel", "Cancel")]

    def __init__(self, errors: list[Finding]) -> None:
        super().__init__()
        self.errors = errors

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog", classes="danger"):
            yield Static("[bold red]Advisor found error findings[/]", id="confirm-title")
            body = "\n\n".join(finding_lines(f) for f in self.errors)
            yield Static(
                body + "\n\n[dim]The SQL launches exactly as written \u2014 Dispatch never rewrites it.[/]",
                id="confirm-body",
            )
            yield Static(
                "[bold]Y[/] launches anyway; [bold]N[/] or [bold]Esc[/] cancels.",
                id="confirm-help",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Launch anyway", id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_proceed(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PreviewProtoScreen(Screen[None]):
    """Preview SQL surface: findings panel docked under the highlighted SQL."""

    BINDINGS = [("b", "app.pop_screen", "Back"), ("escape", "app.pop_screen", "Back")]

    def __init__(self, findings: list[Finding]) -> None:
        super().__init__()
        self.findings = findings

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="main-content"):
            with Vertical(id="preview-content"):
                yield Static(
                    "[dim]\u2039 New Job /[/] [bold]Preview SQL \u00b7 monthly_pull.sql[/]",
                    classes="section-title",
                )
                with Horizontal(id="preview-header"):
                    yield Static("[bold]Target:[/] aa_enc.dispatch_result")
                    yield Static("SqlFile \u2192 Table", id="preview-meta")
                with Vertical(id="sql-display"):
                    yield RichLog(id="preview-body", highlight=False, markup=False)
                yield Static(
                    "[bold]Advisor findings[/] [dim]\u00b7 static analysis \u00b7 manual v2.0[/]",
                    classes="section-title",
                )
                yield Static(self._findings_markup(), id="findings-panel")
            with Horizontal(classes="action-bar"):
                yield Static(badge(self.findings), id="preview-status", classes="action-status")
                yield Button("Back [Esc]", id="back", variant="primary")
        yield Footer()

    def _findings_markup(self) -> str:
        if not self.findings:
            return "[green]No findings \u2014 nothing in the manual's checklist fired.[/]"
        return "\n".join(finding_lines(f) for f in self.findings)

    def on_mount(self) -> None:
        self.query_one("#preview-body", RichLog).write(sql_syntax(MOCK_SQL))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()


class NewJobProtoScreen(Screen[None]):
    """New Job surface: advisor badge lives in the validation summary line."""

    BINDINGS = [
        ("1", "set_state('clean')", "Clean"),
        ("2", "set_state('advisory')", "Advisory"),
        ("3", "set_state('errors')", "Errors"),
        ("p", "preview", "Preview SQL"),
        ("l", "launch", "Launch"),
        ("q", "app.quit", "Quit"),
    ]

    FINDING_SETS = {"clean": [], "advisory": ADVISORY_SET, "errors": ERROR_SET}

    def __init__(self) -> None:
        super().__init__()
        self.state = "errors"

    @property
    def findings(self) -> list[Finding]:
        return self.FINDING_SETS[self.state]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="main-content"):
            with Vertical(id="new-job-content"):
                yield Static("[bold]New Job[/] [dim](prototype \u2014 keys 1/2/3 switch mock findings)[/]", classes="section-title")
                yield Static("Source: [bold]SqlFile[/]   Destination: [bold]Table[/]")
                yield Static("SQL File: monthly_pull.sql   Schema: aa_enc   Table: dispatch_result")
                yield Static("")
                yield Static("[green]\u2713[/] SQL file exists  [green]\u2713[/] Email  [green]\u2713[/] Kerberos", id="warning-text")
            with Horizontal(classes="action-bar"):
                yield Static("", id="validation-summary", classes="action-status")
                yield Button("Preview SQL [P]", id="preview", variant="default")
                yield Button("Launch [L]", id="launch", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        ready = "[green]Ready to launch[/]"
        self.query_one("#validation-summary", Static).update(
            f"{ready}  \u00b7  {badge(self.findings)}"
        )

    def action_set_state(self, state: str) -> None:
        self.state = state
        self._refresh_summary()

    def action_preview(self) -> None:
        self.app.push_screen(PreviewProtoScreen(self.findings))

    def action_launch(self) -> None:
        errors = [f for f in self.findings if f.severity == "error"]
        if errors:
            self.app.push_screen(LaunchGateModal(errors), callback=self._on_gate)
        else:
            self.notify("Launched (mock)", severity="information")

    def _on_gate(self, proceed: bool | None) -> None:
        if proceed:
            self.notify("Launched anyway (mock)", severity="warning")
        else:
            self.notify("Launch cancelled", severity="information")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preview":
            self.action_preview()
        elif event.button.id == "launch":
            self.action_launch()


class AdvisorSurfacePrototype(App[None]):
    CSS_PATH = str(Path(dispatch.__file__).resolve().parent / "app.tcss")
    TITLE = "Dispatch \u2014 advisor surface prototype"

    def on_mount(self) -> None:
        self.push_screen(NewJobProtoScreen())


async def take_shots() -> None:
    out_dir = Path(__file__).resolve().parent
    app = AdvisorSurfacePrototype()
    async with app.run_test(size=(104, 32)) as pilot:
        screen = pilot.app.screen
        assert isinstance(screen, NewJobProtoScreen)
        await pilot.pause()
        app.save_screenshot(str(out_dir / "prototype_newjob_badge_errors.svg"))
        await pilot.press("1")
        await pilot.pause()
        app.save_screenshot(str(out_dir / "prototype_newjob_badge_clean.svg"))
        await pilot.press("3", "p")
        await pilot.pause()
        app.save_screenshot(str(out_dir / "prototype_preview_findings.svg"))
        await pilot.press("escape", "l")
        await pilot.pause()
        app.save_screenshot(str(out_dir / "prototype_launch_gate.svg"))
        await pilot.press("escape")
    app_small = AdvisorSurfacePrototype()
    async with app_small.run_test(size=(80, 24)) as pilot:
        await pilot.press("p")
        await pilot.pause()
        app_small.save_screenshot(str(out_dir / "prototype_preview_80x24.svg"))
    print("wrote 5 SVG screenshots to", out_dir)


if __name__ == "__main__":
    if "--shots" in sys.argv:
        asyncio.run(take_shots())
    else:
        AdvisorSurfacePrototype().run()
