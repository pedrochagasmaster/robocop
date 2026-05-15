"""Textual application shell for Dispatch."""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static

from . import config
from .version import __version__
from .screens.dashboard import DashboardScreen

APP_CSS = """
/* ── Global ── */
Screen {
    layout: horizontal;
    background: $surface;
}

/* ── Sidebar ── */
Sidebar {
    width: 28;
    dock: left;
    background: #111111;
    border-right: vkey $primary-background-darken-2;
}

#sidebar-inner {
    height: 1fr;
}

#sidebar-brand {
    padding: 1 2;
    text-style: bold;
    color: $text;
    background: #0d0d0d;
}

#sidebar-nav {
    padding: 1 0;
    height: auto;
}

NavItem {
    padding: 0 2;
    height: 3;
    content-align-vertical: middle;
    color: $text-muted;
}

NavItem:hover {
    background: $primary 15%;
    color: $text;
}

NavItem.nav-active {
    background: $primary 30%;
    color: $text;
    text-style: bold;
}

#sidebar-footer {
    dock: bottom;
    height: auto;
    padding: 1 2;
}

/* ── Main content area ── */
#main-content {
    width: 1fr;
}

/* ── Stats row ── */
#stats-row {
    layout: horizontal;
    height: auto;
    padding: 1 0;
}

.stat-card {
    width: 1fr;
    height: auto;
    border: round $primary-background-darken-1;
    padding: 0 1;
    margin: 0 1;
    content-align: center middle;
}

.stat-card .stat-label {
    color: $accent;
    text-style: bold;
    text-align: center;
}

.stat-card .stat-value {
    text-align: center;
    text-style: bold;
}

.stat-card .stat-sub {
    color: $text-muted;
    text-align: center;
}

.stat-green {
    color: $success;
}

.stat-red {
    color: $error;
}

.stat-yellow {
    color: $warning;
}

/* ── Section headers ── */
.section-title {
    color: $accent;
    text-style: bold;
    padding: 1 1 0 1;
}

/* ── Tables ── */
DataTable {
    height: auto;
    max-height: 16;
    margin: 0 1;
    scrollbar-size: 1 1;
}

DataTable > .datatable--header {
    text-style: bold;
    color: $accent;
}

/* ── Job ID input section ── */
#job-id-section {
    height: auto;
    padding: 0 1;
    margin: 1 0 0 0;
}

#job-id-section Static {
    color: $accent;
}

/* ── Button rows ── */
.button-row {
    layout: horizontal;
    height: auto;
    padding: 1 1;
    align-horizontal: center;
}

.button-row Button {
    margin: 0 1;
}

/* ── Show-more link ── */
.show-more {
    text-align: right;
    padding: 0 2;
    color: $text-muted;
}

/* ── Dashboard ── */
#dashboard-content {
    height: 1fr;
    overflow-y: auto;
    padding: 0;
}

/* ── New Job screen ── */
#new-job-content {
    height: 1fr;
    overflow-y: auto;
    padding: 0 1;
}

#matrix-panel {
    height: auto;
    padding: 1;
    margin: 1;
    border: round $primary-background-darken-1;
}

#matrix-panel .section-title {
    padding: 0 0 1 0;
}

#matrix-table {
    height: auto;
    max-height: 8;
    margin: 0;
}

#info-panel {
    height: auto;
    padding: 1;
    margin: 0 1;
    border: round $accent 30%;
}

#info-panel Static {
    height: auto;
}

.info-text {
    color: $text-muted;
}

.warn-text {
    color: $warning;
}

#form-grid {
    layout: grid;
    grid-size: 2 6;
    grid-gutter: 1;
    height: auto;
    padding: 1;
    grid-columns: 1fr 1fr;
}

#form-grid .field-label {
    color: $accent;
    height: 1;
    padding: 0;
}

#form-grid Input {
    height: 3;
}

#kerberos-status {
    color: $text-muted;
    padding: 0 1;
    text-align: right;
    dock: top;
    height: 1;
}

#warning-text {
    height: auto;
    padding: 0 1;
    color: $warning;
}

#launch-info {
    color: $text-muted;
    padding: 0 2;
    height: auto;
}

/* ── Preview screen ── */
#preview-content {
    height: 1fr;
    padding: 0;
}

#preview-header {
    height: auto;
    padding: 1 2;
    layout: horizontal;
    background: $surface-darken-1;
}

#preview-header Static {
    width: 1fr;
}

#preview-meta {
    text-align: right;
    color: $accent;
}

#sql-display {
    height: 1fr;
    margin: 0 1;
    border: round $primary-background-darken-1;
    overflow-y: auto;
}

#sql-display Static {
    padding: 0 1;
}

#preview-footer-info {
    height: auto;
    layout: horizontal;
    padding: 1;
    background: $surface-darken-1;
    margin: 0 1;
}

#preview-footer-info Static {
    width: 1fr;
    padding: 0 1;
    color: $text-muted;
}

/* ── Job Detail screen ── */
#job-detail-content {
    height: 1fr;
    padding: 0;
}

#job-summary-panel {
    height: auto;
    padding: 1;
    margin: 1;
    border: round $primary-background-darken-1;
}

#job-summary-panel .section-title {
    padding: 0 0 1 0;
}

#summary-grid {
    layout: horizontal;
    height: auto;
}

#summary-grid Vertical {
    width: 1fr;
}

#summary-grid Static {
    height: auto;
}

.summary-label {
    color: $text-muted;
}

.summary-value {
    color: $text;
}

#log-header {
    layout: horizontal;
    height: auto;
    padding: 0 1;
}

#log-header Static {
    width: 1fr;
}

#log-streaming {
    text-align: right;
    color: $success;
}

#log-panel {
    height: 1fr;
    margin: 0 1;
    border: round $primary-background-darken-1;
    overflow-y: auto;
    padding: 0 1;
    min-height: 8;
}

#log-panel RichLog {
    height: 1fr;
}

#job-status-line {
    height: auto;
    padding: 0 2;
}

/* ── History screen ── */
#history-content {
    height: 1fr;
    padding: 0;
}

#search-row {
    layout: horizontal;
    height: auto;
    padding: 1;
}

#search-row Input {
    width: 1fr;
    margin: 0 1;
}

#history-table {
    height: 1fr;
    margin: 0 1;
}

#pagination {
    layout: horizontal;
    height: auto;
    padding: 0 2;
}

#page-info {
    width: 1fr;
    color: $text-muted;
}

#page-controls {
    width: 1fr;
    text-align: right;
    color: $text-muted;
}

/* ── Browser screen ── */
#browser-content {
    height: 1fr;
    padding: 0;
}

#browser-split {
    layout: horizontal;
    height: 1fr;
}

#browser-left {
    width: 3fr;
    margin: 0 1;
}

#browser-right {
    width: 2fr;
    margin: 0 1;
    border-left: vkey $primary-background-darken-2;
    padding: 0 1;
}

#browser-left DataTable {
    height: 1fr;
}

#file-preview-title {
    color: $accent;
    text-style: bold;
    padding: 1 0 0 0;
}

#file-preview-path {
    color: $text-muted;
    padding: 0;
}

#file-meta {
    height: auto;
    padding: 1 0;
}

#file-meta Static {
    height: auto;
    color: $text-muted;
}

#file-preview-code {
    height: 1fr;
    border: round $primary-background-darken-1;
    overflow-y: auto;
    padding: 0 1;
}

#browser-status {
    layout: horizontal;
    height: auto;
    padding: 0 1;
}

#browser-status Static {
    width: 1fr;
}

#browser-count {
    text-align: right;
    color: $text-muted;
}

#browser-help {
    color: $text-muted;
    padding: 0 1;
}

/* ── Startup splash ── */
#startup {
    width: 80;
    border: round $primary;
    padding: 1 2;
}

.muted {
    color: $text-muted;
}
"""


class DispatchApp(App[None]):
    """Server-side TUI for Impala Job launch and supervision."""

    CSS = APP_CSS

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.launch_cwd = Path.cwd()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="startup"):
            yield Static(f"Dispatch v{__version__}", id="title")
            yield Static("Server-side Impala Job launcher")
            yield Static(f"Launch-time CWD: {self.launch_cwd}", classes="muted")
            yield Static(self._version_banner(), id="version-banner")
        yield Footer()

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.launch_cwd))

    def _version_banner(self) -> str:
        try:
            installed = config.installed_version_path().read_text(encoding="utf-8").strip()
        except OSError:
            return "Install state: missing config/version; run install.sh"
        if installed != __version__:
            return f"Warning: installed version {installed}, deployed version {__version__}"
        return "Install state: current"
