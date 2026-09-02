"""Textual TUI front end for the companion agent.

Deferred-imported from :mod:`palmimo_companion_agent.main` only when
``--ui tui`` (the default) is chosen -- ``--ui cli`` never imports this
module, so :mod:`palmimo_companion_agent.pipeline.ui.cli` never pulls in Textual (see its
own docstring). Deliberately minimal: one scrollback log plus one input line,
no color-coding beyond a plain per-kind label and no audit panel -- this is a
companion-agent example, not a reproduction of any richer internal tool.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog

from ..wiring import Runtime, build_runtime


if TYPE_CHECKING:
    from ..history import Event
    from ..settings import PipelineSettings


def _format(event: Event) -> str:
    """Render one History :class:`~palmimo_companion_agent.pipeline.history.Event` as a RichLog line.

    Discriminates on ``event.kind`` (narrows the union for mypy). A
    :class:`~palmimo_companion_agent.pipeline.history.ToolExecEvent` whose result is
    ``""`` or ``"interrupted"`` (a plain motion confirmation, not something
    worth reading) is skipped -- returning ``""`` tells the caller not to
    write a line.
    """
    if event.kind == "keyboard":
        return f"[bold cyan]you[/]: {escape(event.text)}"
    if event.kind == "mic_input":
        label = "heard" if event.directed else "overheard"
        return f"[bold cyan]{label}[/]: {escape(event.text)}"
    if event.kind == "camera":
        return f"[yellow]vision[/]: {escape(event.summary)}"
    if event.kind == "system_note":
        return f"[dim]{escape(event.text)}[/]"
    if event.kind == "agent_thought":
        return f"[green]companion (thinking)[/]: {escape(event.text)}"
    if event.kind == "tool_exec":
        if not event.result or event.result == "interrupted":
            return ""
        thought = f" ({escape(event.thought)})" if event.thought else ""
        return f"[bold magenta]companion[/]{thought} -> {escape(event.name)}: {escape(event.result)}"
    return escape(str(event))


def _is_exit_command(text: str) -> bool:
    """Whether *text* is the ``/exit`` command exactly (matching the CLI's own rule)."""
    return text.strip() == "/exit"


class CompanionAgentApp(App):
    """Minimal Textual front end: a scrollback log plus one input line."""

    TITLE = "palmimo-companion-agent"
    CSS = """
    #log { height: 1fr; border: round $accent; }
    #cmd { dock: bottom; }
    """

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="log", wrap=True, markup=True)
        yield Input(id="cmd", placeholder="Type an instruction (/exit to quit)")
        yield Footer()

    def on_mount(self) -> None:
        self.runtime.history.subscribe(self._on_event)
        # The robot is already connected (see run_tui) -- this only launches
        # the background tasks; Runtime.start()'s own connect() call is a
        # no-op the second time around.
        self.run_worker(self.runtime.start(), name="runtime-start")
        self.query_one("#cmd", Input).focus()

    def _on_event(self, event: Event) -> None:
        rendered = _format(event)
        if rendered:
            self.query_one("#log", RichLog).write(rendered)

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        message.input.value = ""
        if not text:
            return
        if _is_exit_command(text):
            await self._graceful_shutdown()
            return
        self.runtime.conductor.submit_user_text(text)

    async def action_quit(self) -> None:
        """Textual's default quit action (Ctrl+C etc.) also goes through graceful shutdown."""
        await self._graceful_shutdown()

    async def _graceful_shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.runtime.aclose()
        self.exit()


def run_tui(settings: PipelineSettings) -> None:
    """Console-script entry point for ``--ui tui`` (the default).

    Builds the runtime and connects the robot on a plain, throwaway event
    loop *before* Textual's own loop takes over: a hardware connect failure
    must propagate synchronously out of this function so ``main()``'s error
    handling can report it and exit non-zero. Raising from inside a Textual
    worker (which is where ``Runtime.start()`` would otherwise run, via
    ``on_mount``) does not reach the caller of ``App.run()`` -- Textual
    handles worker exceptions itself instead of re-raising them here.
    """
    runtime = build_runtime(settings)
    asyncio.run(runtime.connect())
    CompanionAgentApp(runtime).run()


__all__ = ["CompanionAgentApp", "run_tui"]
