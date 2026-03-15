# nmead

Read GPS/NMEA position from various sources and forward to multiple sinks.

**Sources:**
- **rigctld** — IC-705 (or any Hamlib rig) via rigctld TCP
- **gps2ip** — iOS GPS2IP app via TCP NMEA stream

**Sinks:**
- **console** — Terminal output with position, grid, optional NMEA sentences
- **wsjtx** — Send Maidenhead grid locator to WSJT-X via UDP

## Prerequisites

- **Hamlib** with `rigctld` (for rigctld source)
- **GPS2IP** iOS app (for gps2ip source)

## Setup

```bash
uv sync
```

## Quick start

```bash
# IC-705 via rigctld (defaults: localhost:4532, console output)
uv run python main.py

# iOS GPS2IP as source
uv run python main.py --source gps2ip --source-host 192.168.1.100 --source-port 11123

# Read once, with NMEA output
uv run python main.py --once --nmea

# Send to WSJT-X + console output
uv run python main.py --console --wsjtx

# GPS2IP → console + WSJT-X on localhost
uv run python main.py --source gps2ip --source-host 192.168.50.162 --source-port 11123 --wsjtx --console

# WSJT-X on another machine
uv run python main.py --wsjtx --wsjtx-host 192.168.1.50 --wsjtx-port 2237

# Use a config file
uv run python main.py -c nmead.toml
```

## Configuration file

Copy `nmead.example.toml` to `nmead.toml` and edit. CLI flags override config values.

```toml
[general]
interval = 2.0

[source]
type = "rigctld"
host = "127.0.0.1"
port = 4532

[[sink]]
type = "console"
nmea = false

[[sink]]
type = "wsjtx"
host = "127.0.0.1"
port = 2237
```

## CLI options

| Flag             | Default       | Description                           |
|------------------|---------------|---------------------------------------|
| `-c, --config`   | —             | Path to TOML config file              |
| `--interval`     | `2.0`         | Poll interval in seconds              |
| `--once`         | off           | Read position once and exit           |
| `--source`       | `rigctld`     | GPS source type: `rigctld`, `gps2ip`  |
| `--source-host`  | varies        | Source host address                   |
| `--source-port`  | varies        | Source TCP port                       |
| `--console`      | (default)     | Enable console output sink            |
| `--nmea`         | off           | Include NMEA sentences in console     |
| `--wsjtx`        | off           | Enable WSJT-X UDP sink                |
| `--wsjtx-host`   | `127.0.0.1`  | WSJT-X UDP host                       |
| `--wsjtx-port`   | `2237`        | WSJT-X UDP port                       |

## rigctld setup

```bash
# Linux
rigctld -m 3085 -r /dev/ttyUSB0 -s 115200

# Windows
rigctld -m 3085 -r COM9 -s 115200
```

Model 3085 = IC-705. Adjust serial port and baud rate for your setup.