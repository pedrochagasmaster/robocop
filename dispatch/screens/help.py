"""Help screen listing keyboard shortcuts organized by screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

QUICK_HELP = """\
[bold]Quick Reference[/]   [b]N[/] New Job   [b]V[/] View Logs   [b]C[/] Cancel   [b]/[/] Filter   [b]H[/] History   [b]B[/] Browse   [b]Q[/] Quit\
"""

HELP_TEXT = """\
[bold]Global[/]
  [b]Q[/]         Quit Dispatch
  [b]?[/]         Toggle this help screen
  [b]Ctrl+P[/]    Command palette (all destinations + kinit)
  [b]F2[/]        Collapse / expand the sidebar
  [b]Tab[/]       Move focus between controls

[bold]Overview (jobs cockpit)[/]
  [b]N[/]         New Job wizard
  [b]V[/]         View full logs for selected job
  [b]C[/]         Cancel selected job
  [b]/[/]         Filter jobs (Esc clears)
  [b]H[/]         Open History
  [b]B[/]         Open Impala Browser
  [b]\u2191\u2193 / J K[/]  Move selection; the preview pane follows
  [b]Enter[/]     Open detail for selected row

[bold]New Job[/]
  [b]\u2191\u2193[/]        Pick a SQL file from the launch directory
  [b]L[/]         Launch job (requires Kerberos)
  [b]P[/]         Preview generated SQL
  [b]E[/]         Edit SQL file in $EDITOR
  [b]K[/]         Refresh Kerberos (kinit)
  [b]M[/]         Toggle the legal-cells matrix
  [b]B / Esc[/]   Back to Overview

[bold]SQL Preview[/]
  [b]Y[/]           Copy SQL to clipboard
  [b]Enter / Esc[/] Back to form

[bold]Job Detail[/]
  [b]Space / F[/] Pause or resume log follow
  [b]g / G[/]     Jump to top / bottom of log
  [b]/[/]         Search log
  [b]Y[/]         Copy job ID
  [b]R[/]         Clone job into New Job (finished jobs)
  [b]C[/]         Cancel job (running jobs, with confirmation)
  [b]B / Esc[/]   Back

[bold]History[/]
  [b]S[/]         Cycle sort: date \u2192 state \u2192 table
  [b][ / ][/]     Previous / next page
  [b]Enter[/]     View logs for selected row
  [b]B / Esc[/]   Back

[bold]Browser[/]
  [b]S[/]         Load tables for schema + filter
  [b]O[/]         Cycle sort: name \u2192 size
  [b]Click [ ][/] Toggle table selection for bulk drop (shows [X] when selected)
  [b]A[/]         Select all loaded tables (toggle)
  [b]Enter[/]     Describe highlighted table
  [b]D[/]         Drop checked tables (type I AM SURE, then DROP)
  [b]B / Esc[/]   Back

[dim]Press Esc or ? to close.[/]\
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static(QUICK_HELP, id="help-quick")
            yield Static(HELP_TEXT, id="help-body")
