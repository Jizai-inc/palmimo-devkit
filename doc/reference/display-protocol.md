# Face Display Protocol — v2

The face display is an RP2350 board with a 480×480 round panel, connected to the
host over USB CDC. Firmware v2 holds no faces of its own: it stores **assets**
(images, GIFs, and vector animations converted from Lottie) that the platform
writes into its flash, plays them on request, and reports taps. This page is
the contract between that firmware and its host-side clients — `palmimo_sdk`,
the Portal, and the platform's firmware and asset tools.

Firmware v1 (0.1.x) speaks a different protocol and is still shipped for apps
built on an SDK that predates v2; the platform switches between the two (see
[Firmware selection](#firmware-selection)). A v2 client tells the two apart with
`HELLO`.

## Transport and line grammar

- USB CDC; the baud rate setting is ignored.
- A **command** is an ASCII line ending in `\n`. A line ending in `\r\n` is
  rejected with `ERR LINE CRLF`. An empty line is ignored and gets no reply.
  Keywords are uppercase; arguments are separated by one space.
- The firmware sends three kinds of lines:
  - **Final reply** — exactly one per command, `OK …` or `ERR <COMMAND> <REASON>`.
    `VERSION`, `STAT`, `PING`, and the [compatibility commands](#compatibility-commands)
    keep their v1 reply lines instead (`VERSION …`, `STAT …`, `PONG`, `OK IDLE`, …).
  - **Transfer progress** — only between a transfer command and its final
    reply: `OK XFER BEGIN <bytes> ACK=4096`, `OK XFER SEND <bytes> <crc32>`, and
    `ACK <total>` (see [Transfers](#transfers)).
  - **Events** — `EVT …`, sent at any time, including between progress lines.
    A client skips events while it waits for a reply.
- Every line the firmware sends is at most 1,024 bytes.
- A client sends the next command only after the previous one's final reply.
- When the host drops DTR (closes the port), the firmware discards any partial
  line and any transfer in progress, stops playback and `STREAM`, and shows the
  idle screen; the power state does not change. (A `FRAME` shown through the
  compatibility commands keeps its v1 behaviour: the sleep screen.)

## Identifiers and numbers

- An **asset id** is `<owner>/<name>`, at most 127 bytes:
  - Standard assets use the owner `palmimo` (`palmimo/happy`). Names starting
    with `system.` are the [system screens](#system-screens).
  - An app's assets use its app ID as the owner (`zip.my-app/welcome`). An app
    ID is at most 81 bytes, and an app asset `<name>` matches
    `^[a-z0-9][a-z0-9._-]{0,39}$`, so every app asset id fits.
- A **slot id** (`sid`, a Lottie slot an app may change) matches `^[a-z0-9_]{1,32}$`.
- `<crc32>` is CRC-32 (IEEE, as in zlib) written as 8 lowercase hex digits.
  `<bytes>` and `<total>` are decimal.
- Slot numbers and flash addresses are internal and never appear on the wire.

## Commands

| Command | Final reply | Notes |
|---|---|---|
| `HELLO` | `OK HELLO proto=2 fw=<version> caps=<cap,…>` | Firmware v1 replies `ERR HELLO (try LIST)`; a v2 client stops there with a message that names the platform's firmware switch. |
| `VERSION` | `VERSION <version>` | Same line as v1; platform tools compare it to their declaration verbatim. |
| `STAT` | `STAT key=value …` | Every v1 key, including exactly one `power=ON\|OFF`, plus `proto=2`, `hz=`, `play=<asset id\|->`, `stream=OFF\|ON`, `margin_us=` (smallest band margin since the last `PLAY`). |
| `PING` | `PONG` | |
| `POWER ON` / `POWER OFF` | `OK POWER <ON\|OFF>` | `OFF` stops playback and `STREAM` and shows the sleep screen. |
| `WAKE` | `OK WAKE` | Plays the wake screen, then the idle screen. |
| `BL <0-100>` | `OK BL <n>` | Backlight. |
| `ASSET PUT <id> <bytes> <crc32>` | `OK ASSET <id>` | A [transfer](#transfers). Platform tools only. Stops playback first. |
| `ASSET DEL <id>` | `OK ASSET DEL <id>` | Platform tools only. Stops playback first. `ERR ASSET UNKNOWN` if absent. |
| `ASSET LIST [<index>]` | `OK ASSET LIST next=<index\|-> <entry> …` | Entries are `<id>:<kind>:<bytes>:<crc32>` with `kind` `vector`, `image`, or `gif`, in a stable order. `<index>` (decimal, default `0`) is the position of the first entry to list; entries that do not fit the line continue at `next`. |
| `ASSET INFO <id>` | `OK ASSET INFO <id> kind=<kind> bytes=<n> crc32=<crc32> params=<sid>/<dim>,…\|-` | The slots an app may set with `PARAM`. `ERR ASSET UNKNOWN` if absent. |
| `ASSET READ <id>` | `OK ASSET READ <id>` | A [read transfer](#transfers). |
| `PLAY <id> [hold_ms]` | `OK PLAY <id>` | Plays an asset, looping. With `hold_ms`, returns to the idle screen afterwards. Leaves `STREAM` and `FRAME`. Resets every `PARAM`. `ERR PLAY UNKNOWN` if absent; `ERR PLAY POWEROFF` while the power is off; `ERR PLAY BUSY` while the power dialog is open. |
| `STOP` | `OK STOP` | Idle screen. Leaves `STREAM` and `FRAME`. Resets every `PARAM`. |
| `PARAM <sid> <v1> [v2 …]` | `OK PARAM <sid>` | Overrides a slot of the playing vector asset. Values are decimal floats; the count must match the slot's dimension. `ERR PARAM NOSLOT` when the playing asset has no such slot (for example, after a standard asset was replaced with an image); clients treat this as a warning. |
| `STREAM <w> <h> RGB565 <crc32>` | `OK STREAM` | A [transfer](#transfers) of one frame of live video, `1 ≤ w, h ≤ 169`, RGB565 little-endian, `w × h × 2` bytes. Shown when complete. `ERR STREAM POWEROFF` while the power is off. |

Platform-only commands are not exposed by `palmimo_sdk`; the firmware cannot
tell its senders apart.

### Compatibility commands

Until the Portal's QR reader moves to `STREAM`, v2 also accepts v1's `FRAME …`
commands with their v1 replies, `IDLE` (replies `OK IDLE`, same as `STOP`), and
`EXPR SLEEPING` (replies `OK SLEEPING`, shows the sleep screen). No other v1
command is accepted.

## Transfers

`ASSET PUT` and `STREAM` carry a body of raw bytes after the command line:

1. The host sends the command line.
2. The firmware sends `OK XFER BEGIN <bytes> ACK=4096`, or a final `ERR …`
   (and the transfer does not start).
3. The host sends exactly `<bytes>` raw bytes — no escaping, no line breaks
   added. It may run at most 16 KiB ahead of the last `ACK`.
4. The firmware sends `ACK <total>` each time the bytes received cross a
   multiple of 4,096, and once more for the final partial block if `<bytes>`
   is not a multiple of 4,096.
5. The firmware always consumes the declared length, then checks the CRC and
   the asset format and sends the final reply. A client decides success on the
   final reply, never on the last `ACK`.

- **Stall**: if no byte arrives for 2 seconds mid-body, the firmware sends
  `ERR XFER TIMEOUT` and discards everything — bytes and commands — until the
  host drops DTR and reconnects. There is no other way to abort a transfer.
- **Failure leaves the old state**: a failed `ASSET PUT` (CRC, format, flash,
  stall, disconnect, power loss) keeps the previous asset under that id; a
  failed `STREAM` keeps the previous frame.
- **Space**: `ERR ASSET NOSPACE` when the asset does not fit in one contiguous
  free run; the platform then repacks (see the platform's asset sync).

`ASSET READ <id>` reverses the direction: the firmware sends
`OK XFER SEND <bytes> <crc32>`, then the raw bytes, then the final
`OK ASSET READ <id>`. The host reads all of it before its next command.

A `STREAM` frame is centred on the panel and scaled up by
`floor(338 / max(w, h))`. The firmware keeps the frame being received in
framebuffer memory the round panel never shows, and switches the whole frame in
one panel refresh, so old and new frames never mix.

## Events

| Event | When |
|---|---|
| `EVT TAP <x> <y>` | A tap was released. Coordinates are panel pixels, 0–479. Which menu item it hit is the client's job. |
| `EVT POWER ON` / `EVT POWER OFF` | The power state changed from the on-screen dialog. |
| `EVT TOUCH REINIT` / `OFF` / `ON` | The touch watchdog acted. |

While an asset plays or `STREAM` is shown, taps are only reported: the firmware
does not count them toward its own gestures (the power dialog and the debug
screen). On the idle and sleep screens it does, as in v1.

## System screens

The idle, sleep, and wake screens play the standard assets
`palmimo/system.idle`, `palmimo/system.sleep`, and `palmimo/system.wake` when
they are stored and valid, and the firmware's built-in screens otherwise.
The power dialog and the screen shown when no asset is stored are always
built in.

## Display timing

The panel refreshes at 61.5 Hz. The firmware draws each 40-row band after the
panel has read it and before it reads it again, so drawing never tears; an
animation updates every second refresh (about 30 frames per second) when a frame
takes longer than one refresh to draw. GIF frame delays are rounded to whole
refreshes — author motion at about 30 fps (33 ms) or faster to avoid visible
strobing on high-contrast moving edges.

## Firmware selection

`palmimo_sdk` ships a file that declares the face firmware it requires. When an
app starts, the platform reads that declaration from the app's installed
`palmimo_sdk` (without running the app) and, if it differs from the firmware
on the device, writes the required firmware first — about three minutes,
shown in the Portal.

The declaration is `palmimo_sdk/display_firmware.json` inside the installed package:

```json
{"schema": 1, "protocol": 2}
```

The platform reads it as a file from the app's environment (the installed
distribution's `RECORD` locates it), follows no symlinks, and reads at most
4 KiB. An app whose environment has `palmimo_sdk` without the declaration (or
with an unreadable one) requires protocol 1; an app without `palmimo_sdk` does
not cause a switch.
