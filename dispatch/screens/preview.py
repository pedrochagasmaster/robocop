"""SQL preview screen with syntax highlighting and metadata."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .sidebar import Sidebar


def _numbered_sql(body: str) -> str:
    """Add line numbers via Rich markup."""
    lines = body.splitlines()
    width = len(str(len(lines)))
    result = []
    for i, line in enumerate(lines, 1):
        num = f"[dim]{i:>{width}}[/]"
        result.append(f"{num} \u2502 {line}")
    return "\n".join(result)


class PreviewScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("l", "launch", "Launch"),
    ]

    def __init__(
        self,
        title: str,
        body: str,
        *,
        schema: str = "",
        table: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self.body = body
        self.schema = schema
        self.table = table

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        sidebar = Sidebar()
        sidebar.active_screen = "new_job"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="preview-content"):
                yield Static(
                    "[dim]\u2039[/] New Job / [bold cyan]" + self._title + "[/]",
                    classes="section-title",
                )

                with Horizontal(id="preview-header"):
                    target = self.schema + "." + self.table if self.schema and self.table else ""
                    yield Static("[bold]Target: " + target + "[/]" if target else "")
                    meta = ""
                    if self.schema:
                        meta = "[cyan]Schema:[/] " + self.schema + "  [cyan]Table:[/] " + self.table
                    yield Static(meta, id="preview-meta")

                with Vertical(id="sql-display"):
                    yield Static(_numbered_sql(self.body), id="preview-body")

                with Horizontal(id="preview-footer-info"):
                    yield Static("[dim]Source Type: table[/]")
                    dest_label = ""
                    if self.schema:
                        dest_label = "[dim]Destination: " + self.schema + "." + self.table + "[/]"
                    yield Static(dest_label)

                with Horizontal(classes="button-row"):
                    yield Button("Launch [L]", id="launch", variant="primary")
                    yield Button("Back [Esc]", id="back", variant="default")
        yield Footer()

    def action_launch(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "launch":
            self.action_launch()
