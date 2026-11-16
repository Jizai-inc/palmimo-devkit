---
name: palmimo
description: Control the Palmimo hexapod robot through its MCP tools -- locomotion, expressive motions, camera, and speech.
---

# Palmimo

Palmimo is a small hexapod robot reachable through MCP tools: `forward`,
`backward`, `turn`, `strafe`, `creep`, `dance`, `body_tilt`, `pushup`, `wave`,
`wave_both`, `clap`, `bow`, `stretch`, `nod`, `head_shake`, `sleep`,
`wake_up`, `look`, `look_center`, `set_face`, `show_emoji`, `say`, `capture`,
and `stop`.

## The server is not preemptive

Every tool call runs to completion before the next one starts, and calls are
serialized -- there is no way to interrupt or overlap them. `stop()` does
**not** cancel a motion already in flight; it only returns the robot to
neutral gradually after whatever is currently running finishes. Firing
several tool calls back-to-back does not make them run in parallel: they
simply queue.

Consequences for how to drive the robot:

- Call one tool at a time and wait for its result before deciding the next
  one. Do not "fire and forget" a sequence expecting to redirect mid-way.
- Keep individual motions short (a few seconds each -- e.g. a single
  `forward` or `turn` call) rather than one long-duration call, so you can
  re-evaluate (new camera frame, new instruction, obstacle) between steps
  instead of being locked into a long motion you can't cut short.
- If you need to change course, let the current call finish first; `stop`
  only helps once you're between calls, not mid-motion.

## Being expressive

Palmimo reads as more alive when speech is paired with a motion or
expression: combine `say` with `set_face`, `show_emoji`, `wave`, `nod`, or
`bow` rather than only ever talking. Use `look` / `look_center` to orient the
head before reacting to something, and `capture` to actually see what's
around before deciding to move -- don't drive blind.

## Safety and good taste

- Don't chain `dance` / `pushup` / `wave_both` repeatedly just because they
  are fun -- each one runs to completion and blocks everything else, so
  spamming them makes the robot unresponsive for real requests.
- On a desk or any surface with an edge nearby, prefer expressive/in-place
  motions (`wave`, `nod`, `bow`, `set_face`, `say`) over locomotion
  (`forward`, `backward`, `turn`, `strafe`, `creep`); only move the robot
  across a surface when you can see there's room via `capture`.
- If a command's intent is unclear or risky (e.g. "walk off the table"),
  prefer a safe, in-place response and say why, rather than executing it.
