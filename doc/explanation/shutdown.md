# Stopping the robot

Every program in this repository that drives a Palmimo has one obligation when
it is asked to stop: reach `Palmimo.disconnect()`, whose last statement is the
driver disconnect that cuts servo torque. Nothing else about a shutdown is
safety-critical.

Missing it is not a cosmetic failure. There is no thermal protection: the neck
servos hold the head up against gravity, and a stop that leaves them energised
has been measured climbing 26 °C → 45 °C → 57 °C with nobody near the robot.

This page records how the stop is delivered, why the delivery differs per entry
point, and what is shared.

## The shape of it

A stop has two halves. A signal arrives and has to become something the program
can act on — that is the *delivery*, and it takes one of three shapes depending
on what the program's own loop is doing. Whichever shape ran, the program then
unwinds into a `finally` that calls `shutdown.park(robot)`, which ignores
further stop signals, runs `Palmimo.disconnect()`, and cuts torque.

`palmimo_sdk.shutdown` owns the second half for everyone. The first half stays
per entry point, because it is the part that genuinely must differ.

## Picking the delivery: does this loop await while idle?

| Answer | Shape | Why |
|---|---|---|
| Yes — it sits in an `await` | `loop_stop_on_signals` (`loop.add_signal_handler`) | The handler runs as an ordinary callback *in* the loop, so it can do real work: cancel the task that is waiting, set the event the session watches. |
| No — it blocks synchronously (a mic queue, a blocking read) | `stop_flag_on_signals` | A cancellation is delivered at an `await` the loop never reaches, so it would sit unread. The handler sets a flag; the loop decides when to look. |
| There is no loop of ours — control is inside a third-party runner | `interrupt_on_signals` | Textual and uvicorn own the loop. Converting the signal to `KeyboardInterrupt` gets our own `finally` back, which is all we need. |

The three are **correctly different**. Picking one idiom for all of them breaks
at least one:

- An in-loop handler on the wake-word agent would never fire: `listen_loop`
  blocks in `Subscription.__next__` → `queue.Queue.get()` and reaches no
  `await` while waiting for audio. This is exactly why Ctrl-C did not stop that
  agent at all (3/3 on hardware) — `asyncio.Runner`'s own SIGINT handler calls
  `task.cancel()`, which was recorded and never delivered.
- A flag on the realtime session would sit unread until `--seconds` elapsed: its
  services block on the socket, the camera queue, or a mic read rather than
  polling one — past a service manager's stop timeout, which then SIGKILLs the
  process with the park unrun.
- An exception raised from an OS handler unwinds at whatever bytecode happened
  to be executing, which loses a cooperative loop's own idea of where it is safe
  to stop, and makes anything the handler touches a re-entrancy hazard (a
  `print` landing inside another `print` raises `RuntimeError: reentrant call
  inside <_io.BufferedWriter>`).

## Where this is used today

This page describes the shared half and the rule for picking a delivery. The
entry points in this repository have **not** all moved onto it yet:

| Entry point | Today |
|---|---|
| A user script (`with Palmimo() as robot:`) | Covered. `Palmimo.disconnect()` enforces the rule on its own — see below — without the caller touching this module. |
| MCP server (`palmimo_sdk/mcp/__main__.py`) | Parks on a terminating signal, using its own equivalent of `interrupt_on_signals` plus `_signals.signals_ignored`. It moves onto this module in a follow-up; the behaviour does not change when it does. |
| companion TUI / CLI / realtime, wake-word agent | Not yet. Each carries its own stop handling, and the gaps are known — the wake-word agent's is described above. They move onto this module one at a time. |

Adding the module before its callers is deliberate: it is the half that has to
be identical everywhere, and a guard that only some entry points can reach is
worth having before every entry point can reach it. The migrations are
mechanical once the shape is picked from the table above.

## The signal set

`STOP_SIGNALS` is SIGINT, SIGTERM and SIGHUP.

SIGHUP is not exotic. The robot is normally driven over ssh, and a dropped
session delivers it — as does closing the terminal window. Its default
disposition kills the process just as silently as SIGTERM's.

SIGINT is absent from `TERMINATING_SIGNALS` because Python already delivers it
as a `KeyboardInterrupt`; installing a second route to the same outcome is
churn. It *is* in `STOP_SIGNALS`, and it is in the set the park ignores — once
the park has begun, Ctrl-C is one more way to skip the torque-off.

## The window before the loop exists

Torque goes on at `Palmimo.connect()`, which is *before* any of the loops above
is running. That connect is seconds — a bus handshake on 21 servos plus a wake
glide — and every entry point used to spend it with no handler installed, so a
signal there took the default disposition and killed the process with the servos
already energised. A stop window therefore has to open *before* the connect, not
after it.

That window sets a flag; it does not cancel. The connect runs on a worker
thread, and a thread cannot be cancelled: abandoning the `await` would leave
that thread driving the servo bus while the park started driving it too. So the
connect is allowed to finish — it is bounded — and the stop is honoured at the
next checkpoint, which skips opening a session nobody asked for.

Reading that flag decides which shape the window uses. `loop_stop_on_signals`
delivers through an ordinary loop callback, queued behind whatever the ready
queue already held — so a flag read straight after the `await` it interrupted
can still be false, intermittently and in an order-dependent way. So the startup
windows use `stop_flag_on_signals` instead: its flag is set by the C-level
handler, and is therefore already true when the call returns. The in-loop shape
is for *doing* something in the loop, never for a checkpoint.

## The rule the shared half enforces

**The park is the only uninterruptible stretch, and every path reaches it.**

- A teardown that has to reach a park holds `signals_ignored(*TERMINATING_SIGNALS)`
  over itself, not just over the park at its end. Leaving a delivery context
  manager restores SIGTERM/SIGHUP to `SIG_DFL` — asyncio's
  `remove_signal_handler` resets to the default regardless of what it replaced —
  and `SIG_DFL` terminates without unwinding. A teardown is seconds of settling
  and task cancellation, so that gap is real.
- SIGINT is deliberately left out of that ignore. It unwinds *into* the
  teardown rather than past it, so a second Ctrl+C during task cancellation
  *shortens* the shutdown without skipping the torque-off — which keeps the
  operator's escape from a slow one.
- `shutdown.park()` ignores stop signals for its own duration (`SIG_IGN`, not a
  mask: a blocked signal stays pending and fires on unblock, which would just
  move the kill to immediately after the park). The way out of a park that will
  not finish is SIGKILL, which nothing here can or should intercept.
- `Palmimo.disconnect()` carries the same rule one level down, for callers that
  never touch this module: a `BaseException` from a teardown step is held, the
  remaining steps run, torque is cut, and only then is it re-raised.

## What this does not promise

A signalled stop is orderly, not guaranteed safe.

- SIGKILL and power loss run no code at all.
- The park takes seconds (a ~2.5 s neck-release ramp, plus the leg return), so
  anything counting down kills the process part-way through it. An MCP stdio
  client allows 4.0 s from stdin close to SIGKILL; the MCP entry point measures
  its own shutdown against that and says on stderr when it overran, rather than
  shortening the park to fit.
- Torque remaining enabled after an unclean stop is accepted behaviour. The
  README says so, and names unplugging the AC adapter as the only certain way to
  make the robot safe to handle.
