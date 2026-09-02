"""Tests for :mod:`palmimo_companion_agent.main`'s typer CLI and the entry
point's output setup (:mod:`palmimo_companion_agent.output`).

Every front end (:mod:`.cli` / :mod:`.tui`) is monkeypatched at its own
module boundary, so these tests never build a real :class:`~palmimo_companion_agent.pipeline.wiring.Runtime`
(no LLM calls, no hardware, no Textual app actually running).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import pytest
from typer.testing import CliRunner

from palmimo_companion_agent.main import app
from palmimo_companion_agent.output import configure_output
from palmimo_companion_agent.pipeline.settings import PipelineSettings


runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("COMPANION_AGENT_HARDWARE", "COMPANION_AGENT_PORT", "COMPANION_AGENT_LOG_PATH"):
        monkeypatch.delenv(var, raising=False)


class _PoisonedModule:
    """Stands in for ``palmimo_companion_agent.pipeline.ui.tui`` in ``sys.modules``: any attribute access fails the test.

    A real ``sys.modules.pop("palmimo_companion_agent.pipeline.ui.tui", None)`` would work
    too, but forces a later test that imports the real module (directly, or
    via ``monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.tui...", ...)``) to
    re-run its top-level Textual imports mid test session -- which can race a
    still-shutting-down Textual App from an earlier test. Poisoning the
    already-imported module object in place (via ``monkeypatch.setitem``,
    auto-reverted) checks the same property (``--ui cli`` never touches the
    module) without ever re-importing or re-executing it.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"--ui cli must not access palmimo_companion_agent.pipeline.ui.tui (got .{name})")


def test_ui_cli_never_imports_the_tui_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "palmimo_companion_agent.pipeline.ui.tui", _PoisonedModule())
    captured: dict[str, PipelineSettings] = {}

    async def fake_run_cli(settings: PipelineSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli"])

    assert result.exit_code == 0, result.output
    assert "settings" in captured


def test_ui_defaults_to_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, PipelineSettings] = {}

    def fake_run_tui(settings: PipelineSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.tui.run_tui", fake_run_tui)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert "settings" in captured


def test_unknown_ui_value_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["--ui", "bogus"])

    assert result.exit_code != 0


def test_no_hardware_flag_sets_settings_hardware_false(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, PipelineSettings] = {}

    async def fake_run_cli(settings: PipelineSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli", "--no-hardware"])

    assert result.exit_code == 0, result.output
    assert captured["settings"].hardware is False


def test_hardware_flag_defaults_to_settings_default_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting --hardware/--no-hardware must not force True over an env-configured False."""
    monkeypatch.setenv("COMPANION_AGENT_HARDWARE", "false")
    captured: dict[str, PipelineSettings] = {}

    async def fake_run_cli(settings: PipelineSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli"])

    assert result.exit_code == 0, result.output
    assert captured["settings"].hardware is False


def test_port_and_log_path_flags_reach_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, PipelineSettings] = {}

    async def fake_run_cli(settings: PipelineSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli", "--port", "/dev/ttyACM0", "--log-path", "/tmp/companion-events.jsonl"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.port == "/dev/ttyACM0"
    assert settings.log_path == Path("/tmp/companion-events.jsonl")


def test_a_runtime_error_from_run_cli_exits_nonzero_with_a_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cli(settings: PipelineSettings) -> None:
        raise RuntimeError("Missing required API key environment variable(s): GEMINI_API_KEY.")

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli"])

    assert result.exit_code == 1
    assert "GEMINI_API_KEY" in result.output


def test_a_connect_failure_with_hardware_enabled_prints_the_no_hardware_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hardware-connect RuntimeError (see wiring.py's all-or-nothing policy) must not be swallowed,
    and must point the user at --no-hardware as the compute-only fallback."""

    async def fake_run_cli(settings: PipelineSettings) -> None:
        raise RuntimeError("Servo bus not found. Check connection or specify with --port.")

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli"])

    assert result.exit_code == 1
    assert "--no-hardware" in result.output
    assert "Servo bus not found" in result.output  # the SDK's original error is still shown


def test_a_connect_failure_with_no_hardware_does_not_print_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-hardware is already the fallback the hint would point at -- suggesting it again would be noise."""

    async def fake_run_cli(settings: PipelineSettings) -> None:
        raise RuntimeError("some unrelated failure")

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = runner.invoke(app, ["--ui", "cli", "--no-hardware"])

    assert result.exit_code == 1
    assert "--no-hardware" not in result.output
    assert "some unrelated failure" in result.output


def test_a_connect_failure_from_run_tui_also_prints_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TUI front end reports a connect failure through the same top-level handling as the CLI."""

    def fake_run_tui(settings: PipelineSettings) -> None:
        raise RuntimeError("Servo bus not found. Check connection or specify with --port.")

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.tui.run_tui", fake_run_tui)

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "--no-hardware" in result.output
    assert "Servo bus not found" in result.output


# --- configure_output, the entry point's output setup ---

#: Ceiling for every wait around the probe process, so a stdout that stays
#: block-buffered fails this test instead of hanging the whole suite.
PROBE_TIMEOUT_SECONDS = 20.0

#: Runs in a child process whose stdout is a pipe -- the exact condition
#: (stdout is not a TTY) under which Python block-buffers. It blocks on stdin
#: forever, so the parent can only read the printed line if it was flushed
#: while the process was still running.
LINE_BUFFERING_PROBE = """\
import sys

from palmimo_companion_agent.output import configure_output

configure_output()
assert sys.stdout.line_buffering is True
print("ready")
sys.stdin.read()
"""


def _first_line_before_exit(probe: str) -> str:
    """Run *probe* with stdout on a pipe and return its first line without waiting for exit."""
    # PYTHONUNBUFFERED would make the probe pass no matter what configure_output
    # does, so drop it from the child's environment.
    env = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    process = subprocess.Popen(
        [sys.executable, "-c", probe],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    reader = ThreadPoolExecutor(max_workers=1)
    try:
        try:
            return reader.submit(process.stdout.readline).result(timeout=PROBE_TIMEOUT_SECONDS)
        except FuturesTimeoutError:
            pytest.fail(f"nothing readable within {PROBE_TIMEOUT_SECONDS}s: stdout is still block-buffered")
    finally:
        process.kill()  # the probe blocks on stdin by design; nothing waits for it to finish
        process.wait(timeout=PROBE_TIMEOUT_SECONDS)
        # Closing the pipe ends the pending readline(), so the reader thread is
        # already unblocked and this returns immediately.
        reader.shutdown(wait=False)


def test_configure_output_makes_a_printed_line_readable_before_the_process_exits() -> None:
    """The regression this guards: with stdout redirected, output stayed in the buffer and the agent looked dead."""
    assert _first_line_before_exit(LINE_BUFFERING_PROBE) == "ready\n"


def test_configure_output_does_not_raise_when_stdout_is_not_a_text_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pytest's own capture stream subclasses TextIOWrapper, so running under
    # capsys does NOT reach the guard. A substitute without reconfigure() does,
    # and is what a harness or an embedding host actually installs.
    class _NoReconfigure:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())

    configure_output()  # skips the line-buffering step rather than raising

    assert not hasattr(sys.stdout, "reconfigure")  # the substitute is still in place


def test_every_console_script_configures_output() -> None:
    """An entry point that skips the setup is one whose output silently disappears under a redirect.

    This project ships two of them -- the chat front ends and ``palmimo-realtime``
    -- so the check reads them out of ``pyproject.toml`` rather than naming them
    here: a script added later is covered the moment it is declared.

    Reading ``co_names`` off the compiled function, rather than searching its
    source text, is what makes this a check on the call: a mention in a
    docstring or a commented-out line does not put the name there.
    """
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    scripts: dict[str, str] = pyproject["project"]["scripts"]
    assert scripts, "this project declares no console scripts"

    for script, target in scripts.items():
        module_name, _, function_name = target.partition(":")
        entry_point = getattr(importlib.import_module(module_name), function_name)
        assert "configure_output" in entry_point.__code__.co_names, (
            f"console script {script!r} ({target}) does not call configure_output()"
        )
