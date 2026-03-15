# rigtop

Ham radio rig dashboard — GPS, frequency, mode, and meters via Hamlib rigctld.

Full-screen TUI with rig info, meter bars, GPS position, and Maidenhead grid.
Auto-starts `rigctld`, auto-falls back to GPS2IP when the rig has no GPS fix.

## Features

- **rigctld** — frequency, mode, passband, PTT, meters (ALC, SWR, power, etc.)
- **GPS** — position from rig via rigctld, with iOS GPS2IP fallback
- **TUI** — full-screen dashboard with rich (default output)
- **Console** — plain text output
- **WSJT-X** — sends Maidenhead grid locator via UDP
- **rigctld launcher** — auto-starts rigctld from config (model, serial port, baud)

## Prerequisites

- Python 3.14+, [uv](https://docs.astral.sh/uv/)
- [Hamlib](https://hamlib.github.io/) with `rigctld` on PATH
- (Optional) [GPS2IP](https://apps.apple.com/app/gps-2-ip/id408625926) iOS app

## Setup

```bash
uv sync
```

## Quick start

```bash
# Just run it — TUI + meters + auto rigctld + auto GPS fallback from rigtop.toml
uv run rigtop

# Plain console output instead of TUI
uv run rigtop --console

# Downgrade to console with NMEA sentences
uv run rigtop --console --nmea

# Add WSJT-X grid forwarding
uv run rigtop --wsjtx

# Skip auto-starting rigctld (already running externally)
uv run rigtop --no-rigctld

# Disable GPS fallback
uv run rigtop --no-gps

# Override rig connection
uv run rigtop --rig-host 192.168.1.50 --rig-port 4532

# Verbose logging (DEBUG also sets rigctld -vvvvv)
uv run rigtop --log-level DEBUG

# Read once and exit
uv run rigtop --once --console

# Explicit config file
uv run rigtop -c /path/to/rigtop.toml
```

## Configuration

Copy `rigtop.example.toml` to `rigtop.toml` and edit. The file is auto-discovered
in the current directory. CLI flags override config values.

```toml
[general]
interval = 2.0        # poll interval in seconds
once = false           # read once and exit
meters = true          # show rig meters
log_level = "WARNING"  # DEBUG, INFO, WARNING, ERROR

[rig]
name = "default"
host = "127.0.0.1"
port = 4532

[rigctld]
model = 3085           # Hamlib model (3085 = IC-705)
serial_port = "COM9"
baud_rate = 19200

[gps_fallback]
host = "192.168.50.162"
port = 11123

[[sink]]
type = "tui"

[[sink]]
type = "wsjtx"
host = "127.0.0.1"
port = 2237
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `-c, --config` | auto `rigtop.toml` | Path to TOML config file |
| `--interval` | `2.0` | Poll interval in seconds |
| `--once` | off | Read once and exit |
| `--rig-host` | `127.0.0.1` | rigctld host address |
| `--rig-port` | `4532` | rigctld TCP port |
| `--no-rigctld` | off | Don't auto-start rigctld |
| `--log-level` | `WARNING` | Log level (also controls rigctld `-v`) |
| `--no-gps` | off | Disable GPS fallback |
| `--gps-host` | from config | GPS2IP host (enables fallback) |
| `--gps-port` | `11123` | GPS2IP TCP port |
| `--console` | off | Plain console output instead of TUI |
| `--nmea` | off | Include NMEA sentences (console mode) |
| `--no-meters` | off | Disable rig meters |
| `--wsjtx` | off | Send grid to WSJT-X via UDP |
| `--wsjtx-host` | `127.0.0.1` | WSJT-X UDP host |
| `--wsjtx-port` | `2237` | WSJT-X UDP port |

## rigctld

rigtop auto-starts `rigctld` when `[rigctld]` is in the config. The log level
maps to rigctld verbosity: WARNING → `-v`, INFO → `-vvv`, DEBUG → `-vvvvv`.

To run rigctld manually instead:

```bash
# Windows
rigctld -m 3085 -r COM9 -s 19200 -T 127.0.0.1 -t 4532 -vvv

# Linux
rigctld -m 3085 -r /dev/ttyUSB0 -s 19200 -T 127.0.0.1 -t 4532 -vvv
```

Model 3085 = IC-705. Use `--no-rigctld` to skip auto-launch.