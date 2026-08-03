from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tools.prod_tui import robocop_tmux
from tools.prod_tui.robocop_tmux import (
    AuthenticationError,
    ProdTuiConfig,
    SessionGoneError,
    TmuxDriver,
    load_config,
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_module_docstring_matches_authenticated_pane_model() -> None:
    assert robocop_tmux.__doc__ is not None
    assert "already-authenticated tmux pane" in robocop_tmux.__doc__
    assert "run_remote()" in robocop_tmux.__doc__
    assert "direct SSH via ``_ssh()``" not in robocop_tmux.__doc__


def test_build_pane_command_ssh_into_repo_with_tty() -> None:
    driver = TmuxDriver(
        "user@edge",
        "robocop-prod-test",
        "/ads_storage/dispatch",
        ssh_options="-p 2222 -o StrictHostKeyChecking=no",
    )
    pane_cmd = driver._build_pane_command()
    assert pane_cmd.startswith("ssh -t -p 2222 -o StrictHostKeyChecking=no user@edge")
    assert "cd /ads_storage/dispatch && exec bash -l" in pane_cmd
    if os.name == "nt":
        assert '"cd /ads_storage/dispatch && exec bash -l"' in pane_cmd
        assert "'cd /ads_storage/dispatch" not in pane_cmd


def test_start_session_runs_local_tmux_new_session() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo", width=120, height=40)
    # Shell-first startup works with psmux on Windows, then sends SSH into it.
    with patch.object(driver, "_tmux", return_value=_completed()) as tmux, \
            patch.object(driver, "capture_screen", return_value="user@edge:/repo$ "):
        ready = driver.start_session()
    assert ready is True
    commands = [call.args[0] for call in tmux.call_args_list]
    assert ["kill-session", "-t", "session"] in commands
    new_session = next(c for c in commands if c[0] == "new-session")
    assert new_session[:6] == ["new-session", "-d", "-s", "session", "-x", "120"]
    assert not any("exec bash -l" in part for part in new_session)
    send_ssh = next(c for c in commands if c[0] == "send-keys" and any("exec bash -l" in part for part in c))
    assert send_ssh[-1] == "Enter"


def test_send_keys_appends_enter_for_shell_commands() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch.object(driver, "_tmux", return_value=_completed()) as tmux:
        driver.send_keys("ls")
    assert tmux.call_args.args[0] == ["send-keys", "-t", "session", "ls", "Enter"]


def test_send_text_sends_literal_text_then_enter_key() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch.object(driver, "_tmux", return_value=_completed()) as tmux:
        driver.send_text("hello world")
    calls = [call.args[0] for call in tmux.call_args_list]
    assert calls == [
        ["send-keys", "-l", "-t", "session", "hello world"],
        ["send-keys", "-t", "session", "Enter"],
    ]


def test_capture_screen_returns_stripped_stdout() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch.object(driver, "_tmux", return_value=_completed("screen\n\n")):
        assert driver.capture_screen() == "screen"


def test_stop_session_is_idempotent() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch.object(driver, "_tmux", return_value=_completed(returncode=1)) as tmux:
        driver.stop_session()
    assert tmux.call_args.kwargs["check"] is False


def test_attach_uses_local_tmux_attach() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch("subprocess.run", return_value=_completed()) as run:
        driver.attach()
    assert run.call_args.args[0] == ["tmux", "attach", "-t", "session"]


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("SSH did not produce a prompt"),
        AuthenticationError("Edge Node rejected the credential"),
        SessionGoneError("tmux session disappeared"),
    ],
)
def test_start_command_reports_start_failures_without_traceback(capsys, exc: Exception) -> None:
    driver = SimpleNamespace(start_session=Mock(side_effect=exc), stop_session=Mock())
    config = SimpleNamespace(session_name="robocop-prod-test")

    with patch.object(robocop_tmux, "driver_from_config_path", return_value=(config, driver)):
        assert robocop_tmux.main(["start", "--config", "config.yaml"]) == 2

    captured = capsys.readouterr()
    assert "Failed to start tmux/SSH session" in captured.err
    assert str(exc) in captured.err
    assert "Traceback" not in captured.err
    driver.stop_session.assert_called_once_with()


def test_start_command_auth_prompt_prints_current_send_command(capsys) -> None:
    driver = SimpleNamespace(start_session=Mock(return_value=False))
    config = SimpleNamespace(
        session_name="robocop-prod-test",
        terminal_width=120,
        terminal_height=40,
        host="user@edge",
        repo_path="/repo",
    )

    with patch.object(robocop_tmux, "driver_from_config_path", return_value=(config, driver)):
        assert robocop_tmux.main(["start", "--config", "config.yaml"]) == 0

    captured = capsys.readouterr()
    assert "Send your PASSCODE with:" in captured.out
    assert "py -m tools.prod_tui tmux send --config config.yaml <YOUR_PASSCODE>" in captured.out
    assert "py tools/prod_tui/robocop_tmux.py" not in captured.out


def test_at_shell_prompt_true_for_bash_prompt() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    assert driver.at_shell_prompt("[e176097@hde2stl020003 ~]$ ") is True


def test_at_shell_prompt_false_when_tui_chrome_visible() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # A prompt-like '$' inside the TUI must not be mistaken for a shell prompt.
    screen = "Dispatch \u2014 Impala jobs\n  Active Jobs\n  Search: foo$ "
    assert driver.at_shell_prompt(screen) is False


def test_return_to_shell_returns_immediately_when_already_at_prompt() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch.object(driver, "capture_screen", return_value="user@edge:~$ "), \
            patch.object(driver, "send_key") as send_key:
        assert driver.return_to_shell() is True
    send_key.assert_not_called()


def test_return_to_shell_pops_subscreen_with_escape() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # A pushed sub-screen (footer shows "esc Back") must be popped with Escape,
    # never with q (which would type into a focused Input) or C-c.
    captures = [
        "Browse Impala Metadata\n esc Back",
        "Browse Impala Metadata\n esc Back",
        "user@edge:~$ ",
    ] + ["user@edge:~$ "] * 10
    with patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_key") as send_key, \
            patch("time.sleep"):
        assert driver.return_to_shell(timeout=5) is True
    keys = [call.args[0] for call in send_key.call_args_list]
    assert keys and keys[0] == "Escape"
    assert "C-c" not in keys


def test_return_to_shell_quits_dashboard_with_q() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # The Overview/dashboard is the app's top screen; q quits Dispatch cleanly.
    captures = [
        "Dispatch \u2014 Impala jobs\n Jobs \u00b7 running first \u00b7 last 7 days\n n New Job  b Browse",
        "user@edge:~$ ",
    ] + ["user@edge:~$ "] * 10
    with patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_key") as send_key, \
            patch("time.sleep"):
        assert driver.return_to_shell(timeout=5) is True
    keys = [call.args[0] for call in send_key.call_args_list]
    assert keys and keys[0] == "q"
    assert "C-c" not in keys


def test_return_to_shell_clears_leftover_input_with_ctrl_u() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # Pane is back at a shell, but a stray key left a half-typed command and a
    # tab-completion menu as the last line. It must be cleared with C-u, not by
    # spamming app keys (q / Escape).
    captures = [
        "[user@edge tmp]$ quot\nquota  quotaoff  quotastats",
        "[user@edge tmp]$ ",
    ] + ["[user@edge tmp]$ "] * 10
    with patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_key") as send_key, \
            patch("time.sleep"):
        assert driver.return_to_shell(timeout=5) is True
    keys = [call.args[0] for call in send_key.call_args_list]
    assert keys and keys[0] == "C-u"
    assert "q" not in keys and "Escape" not in keys


def test_capture_screen_raises_session_gone_when_pane_dead() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")

    def fake_tmux(argv, *, check=True, capture_output=True):
        if argv[0] == "capture-pane":
            return _completed(returncode=1)
        if argv[0] == "has-session":
            return _completed(returncode=1)  # session is gone
        return _completed()

    with patch.object(driver, "_tmux", side_effect=fake_tmux):
        with pytest.raises(SessionGoneError):
            driver.capture_screen()


_PASSCODE_PROMPT = (
    "Oracle 8.10 - MC 26.4\n"
    "(e176097@hde2stl020003.mastercard.int) Enter PASSCODE:"
)


def test_start_session_sends_passcode_and_reaches_shell() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # First capture: auth prompt. After the passcode is sent: a shell prompt.
    captures = [
        _PASSCODE_PROMPT,
        "[e176097@hde2stl020003 dispatch]$ ",
    ] + ["[e176097@hde2stl020003 dispatch]$ "] * 5
    with patch.object(driver, "_tmux", return_value=_completed()), \
            patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_keys"), \
            patch.object(driver, "send_key"), \
            patch("time.sleep"):
        assert driver.start_session(passcode="12345678", connect_timeout=5) is True


def test_start_session_fails_fast_when_passcode_rejected() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # sshd re-displays the prompt: two PASSCODE lines means the code was refused.
    rejected = _PASSCODE_PROMPT + "\n(e176097@hde2stl020003.mastercard.int) Enter PASSCODE:"
    captures = [_PASSCODE_PROMPT] + [rejected] * 8
    with patch.object(driver, "_tmux", return_value=_completed()), \
            patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_keys"), \
            patch.object(driver, "send_key"), \
            patch("time.sleep"):
        with pytest.raises(AuthenticationError):
            driver.start_session(passcode="00000000", connect_timeout=5)


def test_start_session_fails_fast_on_permission_denied() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    denied = _PASSCODE_PROMPT + "\nPermission denied (keyboard-interactive)."
    captures = [_PASSCODE_PROMPT] + [denied] * 8
    with patch.object(driver, "_tmux", return_value=_completed()), \
            patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_keys"), \
            patch.object(driver, "send_key"), \
            patch("time.sleep"):
        with pytest.raises(AuthenticationError):
            driver.start_session(passcode="00000000", connect_timeout=5)


def test_run_remote_returns_output_and_exit_code() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    captured = {}

    def fake_send_keys(keys: str, *, literal: bool = False) -> None:
        captured["cmd"] = keys

    with patch.object(driver, "return_to_shell", return_value=True), \
            patch.object(driver, "send_keys", side_effect=fake_send_keys), \
            patch.object(driver, "send_key"), \
            patch.object(driver, "wait_for") as wait_for:
        # Simulate the pane echoing the command and printing the sentinel.
        def fake_wait(pattern, **kwargs):
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            return f"some output\n__RC_{nonce}_0__\n"
        wait_for.side_effect = fake_wait
        screen, code = driver.run_remote("which dispatch")
    assert code == 0
    assert "which dispatch" in captured["cmd"]
    # The sentinel uses a split literal so the echoed command cannot match.
    assert "__RC''_" in captured["cmd"]


def test_run_remote_parses_nonzero_exit_code() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    with patch.object(driver, "return_to_shell", return_value=True), \
            patch.object(driver, "send_keys"), \
            patch.object(driver, "send_key"), \
            patch.object(driver, "wait_for") as wait_for:
        def fake_wait(pattern, **kwargs):
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            return f"boom\n__RC_{nonce}_7__\n"
        wait_for.side_effect = fake_wait
        _, code = driver.run_remote("false", ensure_shell=False)
    assert code == 7


def test_type_command_confirmed_waits_for_echo_then_enter() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # First poll: only a partial (dropped-char) echo; second: full command.
    captures = [
        "[user@edge tmp]$ ispatch",
        "[user@edge tmp]$ dispatch",
    ] + ["[user@edge tmp]$ dispatch"] * 5
    sent_keys: list[str] = []
    with patch.object(driver, "capture_screen", side_effect=captures), \
            patch.object(driver, "send_keys"), \
            patch.object(driver, "send_key", side_effect=lambda k: sent_keys.append(k)), \
            patch("time.sleep"):
        assert driver.type_command_confirmed("dispatch", confirm_timeout=5) is True
    # Enter is only pressed after the full command echoed.
    assert sent_keys[-1] == "Enter"
    assert "C-u" in sent_keys


def test_type_command_confirmed_falls_back_to_enter_after_retries() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    # Echo never shows the command; it should still submit as a last resort.
    with patch.object(driver, "capture_screen", return_value="[user@edge tmp]$ ispatch"), \
            patch.object(driver, "send_keys"), \
            patch.object(driver, "send_key") as send_key, \
            patch("time.sleep"):
        assert driver.type_command_confirmed("dispatch", confirm_timeout=0.1, retries=2) is False
    assert send_key.call_args_list[-1].args[0] == "Enter"


def test_load_config_fallback_parses_simple_yaml(tmp_path, monkeypatch) -> None:
    """Fallback parser must run when PyYAML is absent, independent of workstation env."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.delenv("DISPATCH_EMAIL", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'host: "user@edge"\nrepo_path: "/repo"\nterminal_width: 132\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config == ProdTuiConfig(host="user@edge", repo_path="/repo", terminal_width=132)


def test_from_mapping_uses_dispatch_email_when_operator_email_missing(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DISPATCH_EMAIL", "env-operator@example.com")
    config = ProdTuiConfig.from_mapping({"host": "user@edge", "repo_path": "/repo"})
    assert config.operator_email == "env-operator@example.com"


def test_from_mapping_uses_dispatch_email_when_operator_email_blank(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DISPATCH_EMAIL", "env-operator@example.com")
    config = ProdTuiConfig.from_mapping(
        {"host": "user@edge", "repo_path": "/repo", "operator_email": ""}
    )
    assert config.operator_email == "env-operator@example.com"


def test_from_mapping_prefers_nonempty_yaml_operator_email(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_EMAIL", "env-operator@example.com")
    config = ProdTuiConfig.from_mapping(
        {
            "host": "user@edge",
            "repo_path": "/repo",
            "operator_email": "yaml-operator@example.com",
        }
    )
    assert config.operator_email == "yaml-operator@example.com"


def test_from_mapping_operator_email_empty_without_yaml_or_env(monkeypatch) -> None:
    monkeypatch.delenv("DISPATCH_EMAIL", raising=False)
    config = ProdTuiConfig.from_mapping({"host": "user@edge", "repo_path": "/repo"})
    assert config.operator_email == ""
