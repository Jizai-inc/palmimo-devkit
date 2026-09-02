"""``palmimo-companion-agent`` console-script entry point.

Dispatches to the headless CLI (:mod:`.pipeline.ui.cli`) or the Textual TUI (:mod:`.pipeline.ui.tui`)
based on ``--ui``. Both front ends are imported lazily, inside the branch
that needs them: in particular, ``--ui cli`` never imports :mod:`.pipeline.ui.tui`, so a
headless run (e.g. over SSH, or under a test harness) never pulls in Textual.

Both front ends today are the pipeline runtime's, so this loads
:class:`~.pipeline.settings.PipelineSettings`. A future realtime/ runtime
would need its own ``--ui`` choice (or a separate entry point) wired in here
alongside its own settings type.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from .output import configure_output
from .pipeline.settings import load_settings


app = typer.Typer(add_completion=False)


@app.command()
def main(
    ui: str = typer.Option(
        "tui", "--ui", help="Front end to run: 'tui' (default, Textual) or 'cli' (headless, JSONL stdout)."
    ),
    hardware: bool | None = typer.Option(
        None,
        "--hardware/--no-hardware",
        help="Attach real hardware peripherals (env: COMPANION_AGENT_HARDWARE; default: true)",
    ),
    port: str | None = typer.Option(
        None, help="Servo bus serial port, e.g. /dev/ttyACM0 (env: COMPANION_AGENT_PORT; default: auto-detected)"
    ),
    log_path: Path | None = typer.Option(
        None,
        "--log-path",
        help="JSONL event log file path (env: COMPANION_AGENT_LOG_PATH; default: disabled)",
    ),
) -> None:
    """Palmimo companion agent: guarded speech + an idle/respond conductor loop, with a TUI or headless CLI front end."""
    if ui not in ("tui", "cli"):
        raise typer.BadParameter(f"--ui must be 'tui' or 'cli' (got {ui!r}).")

    overrides: dict[str, object] = {}
    if hardware is not None:
        overrides["hardware"] = hardware
    if port is not None:
        overrides["port"] = port
    if log_path is not None:
        overrides["log_path"] = log_path
    settings = load_settings(**overrides)

    try:
        if ui == "cli":
            from .pipeline.ui.cli import run_cli

            asyncio.run(run_cli(settings))
        else:
            from .pipeline.ui.tui import run_tui

            run_tui(settings)
    except RuntimeError as exc:
        # settings.hardware=True asks build_runtime for the full peripheral
        # set and Runtime.start()/connect() for an all-or-nothing SDK
        # connect() (see wiring.py) -- either can raise here. The hint is
        # only useful when hardware was actually requested; --no-hardware
        # is already the "try without it" this points at.
        if settings.hardware:
            print(
                "Error: failed to start with hardware attached. To try compute-only instead, pass --no-hardware.",
                file=sys.stderr,
            )
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1) from exc


def run() -> None:
    """Console-script entry point (``[project.scripts]``): set output up, then invoke the typer app.

    :func:`~palmimo_companion_agent.output.configure_output` runs before the CLI
    so that everything the agent prints -- including a startup error raised by
    the command below -- reaches a redirected stdout as it happens. Only the
    application configures this; importing the package changes nothing.
    """
    configure_output()
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
