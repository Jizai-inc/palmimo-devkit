# Raspberry Pi Setup (Headless)

How to set up the Raspberry Pi that carries Palmimo **without wiring it to your
Mac**. Raspberry Pi Imager bakes SSH, WiFi, and your public key into the card **at
write time**, so the Pi is reachable over SSH the moment you insert the card and
power it on.

> This guide is the first concrete step toward running Palmimo **standalone on the
> Pi**, detached from a development PC — the execution model described in
> [explanation/architecture.md](../explanation/architecture.md#execution-model). Nothing about
> the architecture changes; only the host running `palmimo_sdk` moves from the PC to
> the Pi. It assumes the control loop (gait generation → servo writes, 60 Hz) runs
> entirely on the Pi, with the servo-bus USB adapter plugged straight into it.
> architecture.md remains the source of truth for the design goals and safety
> requirements.

## 0. Prerequisites

- [Raspberry Pi Imager](https://www.raspberrypi.com/software/) installed on your
  workstation
- A microSD card (or USB SSD) and a card reader
- The **SSID, password, and country code** of the WiFi network to join (e.g. `JP`)
- An SSH public key on your workstation (create one with `ssh-keygen -t ed25519` →
  `~/.ssh/id_ed25519.pub`)

> The DevKit ships with a **Raspberry Pi 5 (16 GB)** as its control host, already
> imaged. Sections 1-2 write a card from scratch — this is where your own Wi-Fi
> credentials go onto it — and section 3 installs the runtime the Quickstart
> assumes; use them to re-image the shipped card or to prepare a Pi of your own.
> The one hard requirement is a **64-bit (aarch64)** OS:
> dependencies such as `opencv-python` and `piper-plus` ship aarch64
> wheels but not armv7l, so a 32-bit Pi OS cannot resolve them — see
> [I/O runtime environment requirements](../explanation/architecture.md#io-runtime-environment-requirements).

## 1. Write the Card Headlessly

Use Imager's **OS customisation** to bake in everything needed to reach SSH without a
wired connection. This is the core of the guide.

1. Launch Imager → pick the target board under **Device**
2. Under **OS**, choose `Raspberry Pi OS (64-bit)` — Lite is enough, no GUI needed
3. Under **Storage**, select the SD card
4. **Next** → when asked whether to apply OS customisation, choose **Edit Settings**

> **Name the audio device rather than relying on the default.** A Lite image
> ships without the desktop audio stack that would otherwise pick an output
> for you, and ALSA numbers cards in registration order — a USB microphone
> array attached after boot lands behind the built-in HDMI outputs, so the
> default points somewhere with no speaker on it. Pass
> `SpeakerConfig(device_name_hint="ReSpeaker")` and
> `MicrophoneConfig(device_name_hint="ReSpeaker")` instead: the SDK resolves
> the hint against the card id, which does not move when the index does.
> Setting a default in `/etc/asound.conf` or `~/.asoundrc` works too, but it
> pins an index that the next replug can invalidate.

### Customisation Values

| Field | Value | Notes |
|---|---|---|
| Hostname | `palmimo` (→ `palmimo.local`) | Recommended so mDNS resolves the host |
| Username | Your choice (`<pi-user>` below) | Left free: nothing here depends on it, and a systemd user service you add for autostart can resolve the home directory via `%h` instead of a fixed name |
| Password | Any strong value | Key auth is used for login; this is the fallback for `sudo` |
| WiFi SSID / password | The values you noted | **This is what removes the need for wired LAN.** 2.4/5 GHz depends on the board |
| WiFi country code | `JP`, etc. | WiFi may stay disabled if this is left unset |
| Locale / timezone | `Asia/Tokyo`, etc. | Keeps log timestamps consistent |

Under the **Services** tab:

- **Enable SSH** → select **Allow public-key authentication only**
- Paste the contents of `~/.ssh/id_ed25519.pub` (add one key per person if several
  people need access)

> The security policy is **key authentication only**. Hand Imager the public key
> alone — **never commit private keys or passwords to the repository**.

5. **Save** → **Write**. When it finishes, move the card to the Pi.

## 2. First Boot and SSH

Insert the card and power the Pi on (**do not connect it to your Mac**). The first
boot takes one to two minutes to join WiFi and initialise.

From a machine on the same LAN:

```bash
ssh <pi-user>@palmimo.local
```

> If `palmimo.local` does not resolve, look the Pi's address up in your router's DHCP
> table and connect with `ssh <pi-user>@<IP>`. Registering it in `~/.ssh/config` makes
> everything afterwards easier:
>
> ```
> Host palmimo
>     HostName palmimo.local
>     User <pi-user>
>     ServerAliveInterval 60
> ```
>
> `ssh palmimo` then connects, and VS Code Remote-SSH can use the same host entry.

## 3. Prepare the Runtime (on the Pi)

```bash
# Access to the USB serial devices for the servo bus and the display. Requires re-login
sudo usermod -aG dialout "$USER"

# Lite ships without git, and the Quickstart clones this repository with it
sudo apt-get update
sudo apt-get install -y git

# Install uv (never pip — see AGENTS.md)
curl -LsSf https://astral.sh/uv/install.sh | sh
# Reopen the shell, or source ~/.bashrc, to pick up PATH
```

Log out and back in once so the `dialout` membership takes effect.

> That `PATH` entry only reaches interactive shells. A non-interactive SSH
> invocation — `ssh <pi-user>@palmimo.local '<command>'` — never reads
> `~/.bashrc`, so anything driving `uv` that way fails with
> `uv: command not found` and exit status 127. Wrap the remote command in a
> login shell instead, `ssh <pi-user>@palmimo.local 'bash -lc "<command>"'`, so
> the profile that puts `uv` on `PATH` is sourced first. An interactive
> `ssh palmimo` session is unaffected.

> **You normally do not need to specify a port.** The servo-bus USB adapter appears as
> `/dev/ttyACM*` on Linux, but the number can shift when devices are re-plugged or
> when several serial devices coexist (the face display is also a `ttyACM`).
> `find_servo_port()` identifies the device by string and VID matching, so a renumber
> does not affect it. Pass `--port` explicitly only when something specific requires
> pinning it.

### System Dependencies for the Microphone and Camera

The real microphone and camera backends need OS libraries that the Python
dependency sync does not install. Which ones you need depends on the app:

| App and dependency | Records / captures via | OS dependency |
|---|---|---|
| the SDK's `HeadCamera` | `opencv-python` | `v4l-utils` |
| the SDK's TTS (`palmimo_sdk.io.speaker`) | the `piper` binary | none beyond the voice models |
| the SDK's `MicStream` / the wake-word agent example | `sounddevice` | `libportaudio2` |

The camera path is the one every camera-using app shares. `opencv-python` needs a
V4L2 driver, read permission on `/dev/video<id>`, and — on a Lite image — the
OpenGL runtime its wheel links against. Without `libgl1` the import itself
fails with `ImportError: libGL.so.1: cannot open shared object file`, which
disables the MCP server's `capture` tool and stops the companion agent from
starting at all:

```bash
sudo apt-get update
sudo apt-get install -y v4l-utils libgl1
v4l2-ctl --list-devices                 # list connected cameras
```

Apps that use the SDK's `sounddevice`-based microphone capture (`MicStream`,
the wake-word agent example) additionally need the **PortAudio C library**,
without which startup fails with `OSError: PortAudio library not found`:

```bash
sudo apt-get install -y libportaudio2
```

Voice-model downloads for TTS are covered in the
[installation guide](installation.md#first-time-setup-for-voice-output).

## 4. Run Palmimo on the Pi

The code lives on the Pi and runs on the Pi — the control loop is never stretched
across the network. With the runtime in place, the
[Quickstart](../../README.md#-quickstart) owns what follows: clone the
repository, sync the workspace, and confirm the servo bus.

During development, `ssh palmimo` from your workstation and run those commands
on the Pi. In autonomous operation SSH is not needed at all: the goal is for the robot
to start from a safe initial pose on power-up and run with the PC detached (see
[Execution Model](../explanation/architecture.md#execution-model)). Autostart for a given
app is set up with its own systemd user service; none is committed to this repository.

## Appendix

### Migrating from an Older Hostname

There is no need to reflash the OS or the card on an existing Pi. Change only the
hostname over your current SSH connection; after the reboot, connect to
`palmimo.local` with whatever username already exists.

```bash
printf 'preserve_hostname: true\nmanage_etc_hosts: false\n' | sudo tee /etc/cloud/cloud.cfg.d/99-palmimo-hostname.cfg >/dev/null
sudo hostnamectl set-hostname palmimo
sudo sed -i 's/^127\.0\.1\.1.*/127.0.1.1 palmimo/' /etc/hosts
sudo reboot
```

Some Raspberry Pi OS configurations let cloud-init reapply the old hostname at boot.
The commands above hand management of the hostname and `/etc/hosts` to the OS itself,
so the old name does not come back after a reboot.

Leave the Linux username alone. Changing it would disturb the home directory, SSH,
and file ownership for no benefit. Connect as whatever user the card was imaged
with: `ssh <pi-user>@palmimo.local`.

### Why the Username Is Not Fixed

Nothing in this repository names the account. A systemd user unit you add for
autostart can resolve the home directory with `%h`, so it keeps working without
tying the OS account name to the product name.

## Related Documents

- [explanation/architecture.md](../explanation/architecture.md) — Pi execution model, safety
  design, and I/O runtime requirements (the design source of truth)
- [installation.md](installation.md) — dependencies, voice models, troubleshooting
