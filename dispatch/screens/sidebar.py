"""Sidebar navigation widget shared across all screens."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..formatting import format_kerberos_ttl
from ..version import __version__


class NavItem(Static):
    """A single clickable navigation item."""

    class Selected(Message):
        def __init__(self, item_id: str) -> None:
            super().__init__()
            self.item_id = item_id

    active = reactive(False)

    def __init__(self, label: str, item_id: str, icon: str = "") -> None:
        self._label = label
        self._icon = icon
        super().__init__(self._display_text(label, icon, collapsed=False))
        self.item_id = item_id

    @staticmethod
    def _display_text(label: str, icon: str, collapsed: bool) -> str:
        if collapsed:
            return icon or label[:1]
        return f"{icon} {label}" if icon else label

    def set_collapsed(self, collapsed: bool) -> None:
        self.update(self._display_text(self._label, self._icon, collapsed))

    def on_click(self) -> None:
        self.post_message(self.Selected(self.item_id))

    def watch_active(self, value: bool) -> None:
        self.set_class(value, "nav-active")


class KerberosChip(Static):
    """Compact Kerberos TTL indicator shown in the sidebar footer."""

    ttl_seconds: reactive[int | None] = reactive(None)
    collapsed = reactive(False)

    def render(self) -> str:
        ttl = format_kerberos_ttl(self.ttl_seconds)
        if self.collapsed:
            return "\u26a0" if self.ttl_seconds is None else "\u2713"
        return f"KRB {ttl}"

    def watch_ttl_seconds(self, value: int | None) -> None:
        self.remove_class("krb-missing", "krb-low")
        if value is None:
            self.add_class("krb-missing")
        elif value < 3600:
            self.add_class("krb-low")


# BMP Unicode symbols (SSH-safe, no multi-byte emoji)
NAV_ITEMS = [
    ("Overview", "overview", "\u2302"),
    ("New Job", "new_job", "\u229e"),
    ("View Logs", "view_logs", "\u25b8"),
    ("History", "history", "\u25f7"),
    ("Browse", "browse", "\u2630"),
]


class Sidebar(Widget):
    """Left-side navigation panel."""

    active_screen = reactive("overview")
    collapsed = reactive(False)
    # User's explicit Ctrl+B preference, honored at widths >= 100. ``None`` means
    # no manual choice yet, so width drives the state. Narrow widths (< 100)
    # always force-collapse regardless of this preference.
    _manual_override: bool | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar-inner"):
            yield Static("[bold]Dispatch[/]", id="sidebar-brand")
            with Vertical(id="sidebar-nav"):
                for label, item_id, icon in NAV_ITEMS:
                    yield NavItem(label, item_id, icon)
            with Vertical(id="sidebar-footer"):
                yield KerberosChip(id="sidebar-krb")
                yield Static(f"v{__version__}", id="sidebar-version")
                yield Static("[dim]? help[/]", id="sidebar-help")

    def on_mount(self) -> None:
        self._sync_collapse_from_app()
        self.watch(self.app, "size", self._on_app_resize, init=False)
        # The app owns the single Kerberos TTL snapshot; mirror it reactively.
        if hasattr(type(self.app), "kerberos_ttl"):
            self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)

    def _on_kerberos_change(self, value: int | None) -> None:
        self.query_one(KerberosChip).ttl_seconds = value

    def _on_app_resize(self) -> None:
        self._sync_collapse_from_app()

    def on_resize(self) -> None:
        self._sync_collapse_from_app()

    def _sync_collapse_from_app(self) -> None:
        if self.app is None:
            return
        if self.app.size.width < 100:
            target = True  # too narrow to fit labels; always collapse
        elif self._manual_override is not None:
            target = self._manual_override  # respect the user's Ctrl+B choice
        else:
            target = False
        if target != self.collapsed:
            self.collapsed = target

    def toggle_collapsed(self) -> None:
        self._manual_override = not self.collapsed
        self.collapsed = not self.collapsed

    def watch_collapsed(self, value: bool) -> None:
        self.set_class(value, "sidebar-collapsed")
        brand = self.query_one("#sidebar-brand", Static)
        brand.update("[bold]D[/]" if value else "[bold]Dispatch[/]")
        self.query_one("#sidebar-version", Static).display = not value
        help_line = self.query_one("#sidebar-help", Static)
        help_line.update("[dim]?[/]" if value else "[dim]? help[/]")
        self.query_one(KerberosChip).collapsed = value
        for child in self.query(NavItem):
            child.set_collapsed(value)

    def watch_active_screen(self, value: str) -> None:
        for child in self.query(NavItem):
            child.active = child.item_id == value
