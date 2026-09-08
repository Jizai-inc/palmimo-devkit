# palmimo-sdk

The Python SDK for [Palmimo](https://palmimo.dev), a six-legged tabletop AI
robot: a single facade over its motion engine, servo I/O, and peripherals
(face display, speaker, camera, mic). Every motion computes in dry-run, with
no hardware attached, before it ever reaches a servo.

## Installation

```
uv add palmimo-sdk      # or: pip install palmimo-sdk
```

The base install is compute-only — no hardware, audio, or vision
dependencies. Add an extra for what you need:

| Extra | Adds |
|---|---|
| `hardware` | `DynamixelDriver`, the bundled servo-bus backend |
| `face` | `FaceDisplay` (USB-CDC face-display board) |
| `speech` | `Speaker` TTS (piper-plus) |
| `voice` | `MicStream` capture + GTCRN noise removal |
| `agent` | LLM tool-calling layer (`palmimo_sdk.agent`) |
| `vision` | `HeadCamera` (OpenCV) |
| `mcp` | MCP server exposing the agent toolset |

```
uv add "palmimo-sdk[hardware,agent]"
```

## Quick example

```python
from palmimo_sdk import Palmimo

robot = Palmimo()  # compute-only: no driver attached, so nothing touches hardware
robot.forward()
for _ in range(100):
    positions = robot.step()  # {"leg_1_yaw": 2048, ...}
```

Attach a `DynamixelDriver` (from the `hardware` extra) to drive real servos;
see the API reference for the full surface.

## Links

- Documentation: https://docs.palmimo.dev
- Repository: https://github.com/Jizai-inc/palmimo-devkit
- API reference: https://docs.palmimo.dev/reference/api-reference/
- License: https://github.com/Jizai-inc/palmimo-devkit/blob/main/LICENSE (Apache-2.0)
