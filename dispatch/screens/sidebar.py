"""Sidebar navigation widget shared across all screens."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class NavItem(Static):
    """A single clickable navigation item."""

    class Selected(Message):
        def __init__(self, item_id: str) -> None:
            super().__init__()
            self.item_id = item_id

    active = reactive(False)

    def __init__(self, label: str, item_id: str, icon: str = "") -> None:
        display = f"{icon} {label}" if icon else label
        super().__init__(display)
        self.item_id = item_id

    def on_click(self) -> None:
        self.post_message(self.Selected(self.item_id))

    def watch_active(self, value: bool) -> None:
        self.set_class(value, "nav-active")


NAV_ITEMS = [
    ("Overview", "overview", "\u2302"),
    ("New Job", "new_job", "\u229e"),
    ("View Logs", "view_logs", "\U0001f4c4"),
    ("History", "history", "\U0001f552"),
    ("Browse", "browse", "\U0001f4c2"),
]


class Sidebar(Widget):
    """Left-side navigation panel."""

    active_screen = reactive("overview")

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar-inner"):
            yield Static("robocop / [bold cyan]Dispatch[/]", id="sidebar-brand")
            with Vertical(id="sidebar-nav"):
                for label, item_id, icon in NAV_ITEMS:
                    yield NavItem(label, item_id, icon)
            with Vertical(id="sidebar-footer"):
                yield Static("[dim]Q Quit   ? Help[/]")

    def watch_active_screen(self, value: str) -> None:
        for child in self.query(NavItem):
            child.active = child.item_id == value

    def on_nav_item_selected(self, event: NavItem.Selected) -> None:
        self.active_screen = event.item_id
