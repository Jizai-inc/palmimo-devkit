# Servo Flight Recorder

A servo can latch a hardware error and cut its own torque before the host has
explained what happened. The recorder keeps the telemetry around that event:
a short current and position-gap rise can suggest a snag, while a longer
current and temperature trend can suggest sustained load. These are diagnostic
clues, rather than an automatic diagnosis or a replacement for the safety guards.

[The API reference](../reference/api-reference.md#servo-flight-recorder) owns the
configuration, destination rules, lifecycle contract, JSONL fields, and log levels.

## Keep history without continuous disk writes

Normal operation only updates bounded memory rings. Connecting without an open
incident inspects the existing destination but creates no directory, and idle
samples never become a file by themselves. This preserves the lead-up to an
incident without turning ordinary robot use into continuous storage traffic.

Two timescales fit different questions. The fast ring holds 240 leg sweeps at a
nominal 30 Hz, about eight seconds, to expose a brief current surge or a growing
position gap. The context ring holds 36 entries at a nominal 1 Hz, combining
recent leg telemetry, a pending neck sweep, and known hardware-error latches.
It preserves the slower thermal and load trend without duplicating every neck
reading at leg-sweep frequency. Both reuse guard telemetry; context has its own
timer so it can still record neck and error context when there are no leg sweeps.

The rings are bounded by entry count rather than elapsed time. A slower loop
spans more seconds with fewer details, and a new connection has less history.
For a filled fast ring at nominal rate, the budget is
`history >= detection delay + post-trigger tail + useful lead-up`.
With regular stepping at at least 30 fps, the scheduling delay is nominally at
most `1 + 3 / fps` seconds, allowing one frame to notice that the poll is due
and two to defer it, excluding bus duration and stalls. At 30 fps this
leaves about `8 - 1.10 - 2 = 4.90` seconds before the physical event, or
`8 - 1.10 - 4 = 2.90` seconds with the longest folded tail. Both leave room for
a two-second lead-up. The context budget is 30 preceding entries, up to four
tail entries, one trigger entry, and one spare: 36. These are sizing estimates;
the timestamps and actual captured tail in each file determine its coverage.

A single long ring at the fast rate would enlarge each synchronous dump.
Periodic disk streaming would add storage traffic without an incident.
A background writer would add lifecycle and shutdown coordination; bounded
synchronous writes keep ownership with the facade.

## Detect the servo's verdict without widening telemetry reads

Hardware_Error_Status is at register 70, apart from the normal telemetry span
126–146. The recorder therefore makes a separate one-byte status read across
all axes at a nominal one-second interval. It prefers a frame without a leg
sweep, but waits at most two frames after becoming due. Widening every telemetry
span would transfer many unused bytes, and adding a status read to every leg
sweep would spend bus time on a latched value that changes infrequently. Widening
only neck reads would leave the legs uncovered.

The trigger time is when the host observes the latch, not the instant the servo
cuts torque. A gap in `step()` calls, including a direct wake or sleep glide,
can make that delay longer than the nominal poll schedule. For a single trigger
axis, `last_clean_t` and zero bracket the physical latch between the last zero
response and detection. With multiple triggering axes the header aggregates
their clean times; it is not a separate precise event-time band for every axis.
The [header definition](../reference/api-reference.md#files-and-header) specifies
that aggregation and the meaning of unknown timing. Wall-clock time identifies
the file, while monotonic offsets keep clock adjustments out of sample timing.

## One capture for a continuing latch

A latch normally produces one file. New axes detected while its window is open
join that capture, allowing a developing multi-axis event to stay together.
After the window closes, a newly latched different axis can open another file.
Additional bits on an already latched axis are logged rather than producing
repeated captures of the same continuing condition.

Recovery requires that axis itself to answer with zero hardware-error bits.
Silence, an unreached axis, or a failed read supplies no evidence of recovery.
This avoids repeatedly recording one accident when communication is intermittent;
an unrelated healthy axis cannot re-arm a still-latched one. Recovery within an
open window does not shorten its tail, preserving the aftermath as well.

Each axis's first successful response establishes its connection baseline. A
latch already present then is logged but produces no file: there is no useful
pre-event history to save. Waiting for one complete response from every axis
would let a permanently silent axis prevent baselining all the others. Retaining
known latches across reconnects also keeps an unchanged condition from becoming
a new incident on each connection. The recorder never sends REBOOT: clearing a
hardware-error latch requires a servo reboot or power cycle, which is a separate
recovery decision.

## Finish torque shutdown before storage work

During ordinary stepping, a due capture is written after that frame's goal
write. During disconnect, parking may itself call `step()`, but persistence is
deferred until after the driver disconnect attempt. This ordering puts the
normal torque-off operation ahead of disk latency; if driver disconnect raises,
the capture is still flushed, without claiming torque-off succeeded. An early
flush can save a shorter tail, which the header exposes rather than pretending
the full window was observed. The [shutdown model](shutdown.md) explains the
facade's wider cleanup ordering.

Publishing uses a temporary file, file flush and fsync, then atomic rename.
A reader of the published name sees an absent file or a complete JSONL file,
rather than a half-written capture. A temporary file can remain after interruption
or publication failure. Syncing the incident directory strengthens persistence
of the rename on POSIX; it does not guarantee survival of every power loss.
The [persistence contract](../reference/api-reference.md#persistence-failures-and-logs)
describes the returned paths and failure cases. Write time belongs in logs,
since it cannot be known until after writing the capture itself.

## Preserve evidence within its owner's storage lifetime

The default destination follows XDG state-directory resolution, keeping robot
state separate from source code; the exact path and overrides are in
[configuration and destination](../reference/api-reference.md#configuration-and-destination).
On the shipped image, an app's HOME is within its own app storage, so the
fallback destination belongs to that app and disappears when the app is removed.
It is not a fleet-wide archive.

The recorder does not automatically delete captures. Age alone cannot identify
which incident still matters, and an automatic retention limit could erase the
evidence being investigated. Connection-time size and incomplete-file warnings
make accumulation visible without changing it. The separate
[per-connection capture limit](../reference/api-reference.md#capture-and-flushing)
bounds repeated write stalls if a brownout repeatedly resets a servo and it
latches again; it does not delete the evidence already saved.

## Recording depends on the enabled guards

The recorder reuses telemetry instead of starting another leg reader. Disabling
all three leg guard channels removes leg telemetry from dumps; neck telemetry
and hardware-error context can still be captured. Unsupported leg telemetry
has the same limitation. Unsupported hardware-error reading disables recording
for that connection, while the motion and guards retain their own behavior.
The [configuration contract](../reference/api-reference.md#configuration-and-destination)
provides the separate switch for disabling recording itself.
