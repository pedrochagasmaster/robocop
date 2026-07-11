"""Textual application shell for Dispatch."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, SystemCommand
from textual.reactive import reactive

from . import config, kerberos, process, setup_logging, telemetry

if TYPE_CHECKING:
    from textual.screen import Screen
from .screens.browser import BrowserScreen
from .screens.dashboard import DashboardScreen
from .screens.help import HelpScreen
from .screens.history import HistoryScreen
from .screens.job_detail import JobDetailScreen
from .screens.new_job import NewJobScreen
from .screens.sidebar import NavItem
from .version import __version__

logger = logging.getLogger("dispatch.app")

MIN_WIDTH = 80
MIN_HEIGHT = 24


class DispatchApp(App[None]):
    """Server-side TUI for Impala Job launch and supervision."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("question_mark", "help", "Help"),
        ("f2", "toggle_sidebar", "Sidebar"),
    ]

    kerberos_ttl: reactive[int | None] = reactive(None)

    def action_help(self) -> None:
        telemetry.emit("screen_view", {"screen": "help"})
        self.push_screen(HelpScreen())

    def action_toggle_sidebar(self) -> None:
        from .screens.sidebar import Sidebar

        for sidebar in self.screen.query(Sidebar):
            sidebar.toggle_collapsed()

    def __init__(self) -> None:
        super().__init__()
        setup_logging()
        # Dispatch runs over SSH/VPN where animation frames translate into
        # extra terminal writes and visible lag; disable them unless the user
        # explicitly opted in via TEXTUAL_ANIMATIONS.
        if "TEXTUAL_ANIMATIONS" not in os.environ:
            self.animation_level = "none"
        self.launch_cwd = Path.cwd()
        self.title = "Dispatch"
        self.sub_title = f"Impala jobs \u00b7 {self._short_cwd()}"
        self._too_small_warned = False
        logger.info(
            "Dispatch %s starting, cwd=%s, data_root=%s",
            __version__,
            self.launch_cwd,
            config.data_root(),
        )

    def _short_cwd(self, max_len: int = 40) -> str:
        text = str(self.launch_cwd)
        if len(text) <= max_len:
            return text
        return f"\u2026{text[-max_len:]}"

    async def on_mount(self) -> None:
        version_warning = self._build_version_warning()
        if version_warning:
            self.notify(version_warning, severity="warning", timeout=0)

        if not config.dispatch_home().exists():
            logger.error("Dispatch home %s does not exist", config.dispatch_home())
            self.notify(
                "Dispatch is not installed for this user. Run install.sh to set up.",
                severity="error",
                timeout=0,
            )

        self._check_terminal_size()
        telemetry.note_session_start(cwd=self.launch_cwd)
        telemetry.emit("screen_view", {"screen": "overview"})
        self.push_screen(DashboardScreen())
        await self.refresh_kerberos()
        self.set_interval(60.0, self.refresh_kerberos)
        self._maybe_open_test_prefill()

    def on_unmount(self) -> None:
        telemetry.note_session_end()

    def _maybe_open_test_prefill(self) -> None:
        """Opt-in test seam: when ``DISPATCH_TEST_PREFILL`` names a JSON file,
        open the New Job screen pre-filled from it.

        This has no effect in normal use (the variable is unset) and only reuses
        the existing prefill path. It lets the production smoke harness drive a
        deterministic launch without relying on fragile keystroke navigation of
        the radio sets over a high-latency SSH PTY.
        """
        path = os.environ.get("DISPATCH_TEST_PREFILL")
        if not path:
            return
        try:
            prefill = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring DISPATCH_TEST_PREFILL (%s): %s", path, exc)
            return
        if isinstance(prefill, dict):
            self.call_after_refresh(self.open_new_job_prefill, prefill)

    def on_resize(self) -> None:
        self._check_terminal_size()

    def _check_terminal_size(self) -> None:
        too_small = self.size.width < MIN_WIDTH or self.size.height < MIN_HEIGHT
        if too_small and not self._too_small_warned:
            self.notify(
                f"Terminal too small ({self.size.width}\u00d7{self.size.height}). "
                f"Minimum: {MIN_WIDTH}\u00d7{MIN_HEIGHT}. Some layouts may break.",
                severity="warning",
                timeout=10,
            )
        self._too_small_warned = too_small

    async def refresh_kerberos(self) -> None:
        """Refresh the app-wide Kerberos TTL snapshot (mirrored by sidebars)."""
        self.kerberos_ttl = await kerberos.ticket_ttl_seconds()

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Power layer: every destination and key maintenance action is one
        fuzzy search away in the command palette."""
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Overview",
            "Jobs cockpit: running and recent jobs with live logs",
            lambda: self.open_top_level("overview"),
        )
        yield SystemCommand(
            "New Job",
            "Launch a SQL file as an Impala job",
            lambda: self.open_top_level("new_job"),
        )
        yield SystemCommand(
            "History",
            "Finished jobs older than 7 days",
            lambda: self.open_top_level("history"),
        )
        yield SystemCommand(
            "Browse metadata",
            "SHOW TABLES, DESCRIBE, and DROP in Impala",
            lambda: self.open_top_level("browse"),
        )
        yield SystemCommand(
            "Refresh Kerberos (kinit)",
            "Suspend the UI and run kinit",
            self._kinit_from_palette,
        )

    async def _kinit_from_palette(self) -> None:
        with self.suspend():
            process.run_interactive("kinit")
        await self.refresh_kerberos()
        if self.kerberos_ttl is not None:
            self.notify(f"Kerberos refreshed: {self.kerberos_ttl // 60}m", severity="information")
        else:
            self.notify("Kerberos ticket still missing", severity="warning")

    def _build_version_warning(self) -> str:
        try:
            installed = config.installed_version_path().read_text(encoding="utf-8").strip()
        except OSError:
            return (
                f"Install incomplete: version file missing. Run install.sh. (running {__version__})"
            )
        if installed != __version__:
            return (
                f"Version mismatch: installed {installed}, running {__version__}. Run install.sh."
            )
        return ""

    def on_nav_item_selected(self, event: NavItem.Selected) -> None:
        item_id = event.item_id
        current = self.screen

        if self._sidebar_destination_for_screen(current) == item_id:
            return

        if item_id == "view_logs":
            job_id = self._selected_job_id_from_screen(current)
            if job_id and job_id != "__empty__":
                self.call_after_refresh(self._open_job_detail_from_sidebar, job_id)
            else:
                self.notify(
                    "Please select a job from the Overview or History table first.",
                    severity="warning",
                )
            return

        self.call_after_refresh(self.open_top_level, item_id)

    def open_top_level(self, item_id: str) -> None:
        """Open a top-level destination while keeping the stack anchored on Overview."""
        if self._sidebar_destination_for_screen(self.screen) == item_id:
            return
        self._pop_to_dashboard()

        if item_id == "overview":
            telemetry.emit("screen_view", {"screen": "overview"})
            return
        screen_name = {
            "new_job": "new_job",
            "history": "history",
            "browse": "browser",
        }.get(item_id, item_id)
        telemetry.emit("screen_view", {"screen": screen_name})
        self.push_screen(self._build_top_level_screen(item_id))

    def open_job_detail(self, job_id: str, *, cancel_on_mount: bool = False) -> None:
        telemetry.emit("screen_view", {"screen": "job_detail"})
        self.push_screen(JobDetailScreen(job_id, cancel_on_mount=cancel_on_mount))

    def open_new_job_prefill(self, prefill: dict) -> None:
        self._pop_to_dashboard()
        telemetry.emit("screen_view", {"screen": "new_job"})
        self.push_screen(NewJobScreen(self.launch_cwd, prefill=prefill))

    def _open_job_detail_from_sidebar(self, job_id: str) -> None:
        self._pop_to_dashboard()
        self.open_job_detail(job_id)

    def _build_top_level_screen(self, item_id: str):
        if item_id == "new_job":
            return NewJobScreen(self.launch_cwd)
        if item_id == "history":
            return HistoryScreen()
        if item_id == "browse":
            return BrowserScreen()
        raise ValueError(f"Unknown top-level destination: {item_id}")

    def _pop_to_dashboard(self) -> None:
        while len(self.screen_stack) > 2:
            self.pop_screen()

    @staticmethod
    def _sidebar_destination_for_screen(current: object) -> str | None:
        if isinstance(current, DashboardScreen):
            return "overview"
        if isinstance(current, NewJobScreen):
            return "new_job"
        if isinstance(current, HistoryScreen):
            return "history"
        if isinstance(current, BrowserScreen):
            return "browse"
        if isinstance(current, JobDetailScreen):
            return "view_logs"
        return None

    @staticmethod
    def _selected_job_id_from_screen(current: object) -> str | None:
        if isinstance(current, DashboardScreen):
            return current._selected_job_id()
        if isinstance(current, HistoryScreen):
            return current._selected_job_id()
        return None
