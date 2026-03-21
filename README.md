# rigtop

Ham radio rig dashboard — GPS, frequency, mode, and meters via Hamlib rigctld.

Full-screen TUI with rig control, meter bars, GPS position, Maidenhead grid,
APRS (RF + internet), and GPS forwarding to external apps.
Auto-starts `rigctld`, auto-falls back to GPS2IP when the rig has no GPS fix.

## Features

- **TUI dashboard** — full-screen rich terminal UI with GPS, rig meters, APRS traffic, and connection status
- **Rig control** — frequency, mode, passband, PTT, and meter readings (ALC, SWR, S-meter, power, etc.) via rigctld
- **GPS** — position from rig via rigctld, with iOS GPS2IP fallback
- **APRS-IS** — beacon live GPS to APRS-IS (aprs.fi), receive nearby traffic with server-side filter
- **Direwolf RF** — receive local APRS RF decodes from Direwolf via KISS TCP
- **NMEA output** — feed GGA+RMC sentences to Direwolf, PinPoint, or any NMEA consumer via serial or TCP
- **gpsd server** — gpsd-compatible JSON server (protocol 3.x) for Xastir, YAAC, cgps, gpspipe, etc.
- **WSJT-X** — sends Maidenhead grid locator via UDP
- **Console** — plain text output mode with optional NMEA sentences
- **rigctld launcher** — auto-starts rigctld from config (model, serial port, baud, PTT)
- **CI-V proxy** — Icom CI-V serial proxy so HRD/other CAT software shares the rig via rigctld
- **TUI commands** — vim-style `:command` interface for rig control, APRS toggling, log filtering
- **TX watchdog** — forces PTT off if the radio transmits continuously beyond a timeout (protects against stuck TX)

## Prerequisites

- Python 3.14+, [uv](https://docs.astral.sh/uv/)
- [Hamlib](https://hamlib.github.io/) with `rigctld` on PATH
- (Optional) [GPS2IP](https://apps.apple.com/app/gps-2-ip/id408625926) iOS app for fallback GPS
- (Optional) [Direwolf](https://github.com/wb2osz/direwolf) for APRS RF
- (Optional) [VSPD](https://www.eltima.com/products/vspdxp/) or com0com for virtual serial port pairs (Windows)

## Setup

```bash
uv sync
```

## Quick start

```bash
# Just run it — TUI + meters + all sinks from rigtop.toml
uv run rigtop

# Plain console output instead of TUI
uv run rigtop --console

# Skip auto-starting rigctld (already running externally)
uv run rigtop --no-rigctld

# Disable GPS fallback
uv run rigtop --no-gps

# Disable rig meters
uv run rigtop --no-meters

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

See [rigtop.example.toml](rigtop.example.toml) for a fully commented reference.

### General settings

```toml
[general]
interval = 2.0        # poll interval in seconds
once = false          # read once and exit
meters = true         # show rig meters
log_level = "WARNING"  # DEBUG, INFO, WARNING, ERROR
```

### Rig connection

```toml
[rig]
name = "default"
host = "127.0.0.1"
port = 4532
```

### rigctld launcher

Auto-starts `rigctld` as a subprocess. Remove this section or use `--no-rigctld` to skip.

```toml
[rigctld]
model = 3085           # Hamlib model (3085 = IC-705)
serial_port = "COM9"   # COM port or /dev/ttyUSB0
baud_rate = 19200
data_bits = 8
stop_bits = 1
serial_parity = "None"    # None, Odd, Even, Mark, Space
serial_handshake = "None" # None, XONXOFF, Hardware
dtr_state = "Unset"       # Unset, ON, OFF
rts_state = "Unset"       # Unset, ON, OFF
ptt_type = "RIG"          # RIG, RIGMICDATA, DTR, RTS, Parallel, CM108, GPIO, GPION, None
ptt_pathname = ""          # PTT device path (blank = same as rig)
ptt_share = false          # share PTT port with other apps
```

### GPS fallback

Falls back to iOS GPS2IP when the rig has no GPS fix. Remove this section or use `--no-gps` to disable.

```toml
[gps_fallback]
host = "192.168.1.100"
port = 11123
```

### APRS settings

QSY rig to the APRS frequency and mode when `:aprs on` is used. Optional.

```toml
[aprs]
enabled = false       # true = QSY on startup; false = only on :aprs on
freq = 144.800        # MHz (EU: 144.800, NA: 144.390)
qsy_mode = "FM"
```

### Packet BBS

Quick QSY to a packet BBS frequency via `:packet on`. Saves and restores previous freq/mode. Optional.

```toml
[bbs]
enabled = false       # true = QSY on startup; false = only on :packet on
freq = 144.675        # packet frequency in MHz
mode = "PKTFM"        # rig mode (PKTFM = 1200 baud FM)
```

### Static GPS position

Hard-coded position used when neither rig GPS nor fallback have a fix. Useful for fixed/home stations.

```toml
[gps_static]
enabled = true
lat = 59.329          # latitude (decimal degrees)
lon = 18.069          # longitude (decimal degrees)
alt = 28.0            # altitude in metres (optional)
```

### Direwolf

Receive local RF APRS decodes from Direwolf. Packets decoded by your radio appear
in the APRS pane in yellow. Optional — remove this section if you don't run Direwolf.
Set `install_path` to let rigtop start/stop Direwolf on demand (:aprs on / :bbs on).

```toml
[direwolf]
host = "127.0.0.1"
port = 8001           # Direwolf KISS TCP port
install_path = "c:\\direwolf"  # set to enable launcher
```

### TX watchdog

Forces PTT off if the radio transmits continuously for longer than `tx_timeout` seconds.
Protects against stuck TX from VOX loops, stuck PTT buttons, or software bugs.
The TUI shows a full-screen alert and a blinking **WD** badge when the watchdog trips.
Remove this section to disable.

```toml
[watchdog]
tx_timeout = 120   # seconds (minimum 10, default 120)
```

### Sinks

Sinks are output destinations. Use `[[sink]]` (double brackets) for each one.
Multiple sinks run simultaneously. Set `enabled = false` to disable a sink without removing it.
Add `name = "label"` to distinguish multiple instances of the same type in the connection bar.

```toml
# Full-screen TUI dashboard (default)
[[sink]]
type = "tui"

# WSJT-X grid locator via UDP
[[sink]]
type = "wsjtx"
port = 2237

# NMEA GPS feed via serial (Windows — virtual COM port pair)
[[sink]]
type = "nmea"
name = "Direwolf"
device = "COM10"      # rigtop writes here; consumer reads the other end

# NMEA GPS feed via TCP (Linux)
[[sink]]
type = "nmea"
port = 10110          # Direwolf: GPSNMEA host=localhost:10110

# gpsd-compatible JSON server
[[sink]]
type = "gpsd"
port = 2947           # gpspipe -w localhost:2947

# APRS-IS beacon + receiver
[[sink]]
type = "aprsis"
enabled = false       # set true to connect at startup
callsign = "N0CALL-1"
server = "euro.aprs2.net"
passcode = "12345"
interval = 120                      # beacon interval in seconds (min 30)
comment = "rigtop"
aprs_filter = "r/59.2/18.1/200"    # server filter (blank = auto from GPS)

# Icom CI-V proxy — lets HRD/other CAT software share the rig via rigctld
# [[sink]]
# type = "civ_proxy"
# device = "COM14"      # virtual serial port (rigtop writes here)
# baudrate = 19200
# rig_name = "IC-705"   # used for auto CI-V address lookup

# Plain console output
# [[sink]]
# type = "console"
# nmea = false
```

## Sink types

| Type | Protocol | Default port | Description |
|------|----------|-------------|-------------|
| `tui` | — | — | Full-screen terminal dashboard (default) |
| `console` | — | — | Plain text output to stdout |
| `wsjtx` | UDP | 2237 | Maidenhead grid locator to WSJT-X |
| `nmea` | TCP or serial | 10110 | NMEA GGA+RMC sentences for Direwolf, PinPoint, etc. |
| `gpsd` | TCP (JSON) | 2947 | gpsd protocol 3.x server for Xastir, YAAC, cgps |
| `aprsis` | TCP | 14580 | APRS-IS position beacon + traffic receiver |
| `civ_proxy` | serial | — | Icom CI-V proxy — share rig with HRD/other CAT via rigctld |

### NMEA sink

The NMEA sink outputs standard GGA and RMC sentences. Two modes:

- **TCP** (default): Listens on a port; clients connect to receive sentences.
  Works on all platforms. Direwolf config: `GPSNMEA host=localhost:10110`
- **Serial**: Writes to a COM/tty port. Required for Windows Direwolf.
  Use a virtual serial port pair (VSPD or com0com) — rigtop writes one end,
  the consumer reads the other. Direwolf config: `GPSNMEA COM11`

### gpsd sink

Implements the gpsd JSON protocol (subset): `VERSION`, `WATCH`, `DEVICES`, `POLL`, `TPV`.
Compatible with any gpsd client library: `gpspipe -w`, `cgps -s`, Xastir, YAAC, libgps.

### APRS-IS sink

Connects to an APRS-IS Tier 2 server and:
- **Beacons** live GPS position at the configured interval (minimum 30s)
- **Receives** nearby APRS traffic using a server-side filter
- Auto-generates a range filter from the first beacon position if none is configured
- Displays incoming traffic in a dedicated TUI pane
- Tracks receive count and connection status; shows **APRS IG** badge in title bar when connected
- `:beacon on/off` controls outgoing position sends without disconnecting the receiver

## TUI commands

The TUI uses a vim-style command interface. Press `:` to enter command mode.

| Command | Arguments | Description |
|---------|-----------|-------------|
| `:freq` | `[Hz\|MHz]` | Show or set rig frequency. Values < 1 MHz are treated as MHz. |
| `:mode` | `[MODE [passband]]` | Show or set rig mode (FM, USB, LSB, CW, AM, …) |
| `:data` | `[on\|off]` | Toggle data mode (e.g. USB → PKTUSB, FM → PKTFM) |
| `:aprs` | `[on\|off]` | Show status or toggle all APRS sinks (NMEA + APRS-IS) |
| `:aprsis` | `[on\|off]` | Toggle APRS-IS (internet gateway) only |
| `:packet` | `[on\|off]` | QSY to packet BBS frequency/mode and start Direwolf |
| `:nmea` | `[on\|off]` | Toggle all NMEA sinks |
| `:gpsd` | `[on\|off]` | Toggle gpsd server sink |
| `:civ` | `[on\|off]` | Toggle CI-V proxy sink |
| `:beacon` | `[on\|off]` | Enable/disable outgoing APRS-IS position beacon |
| `:dw` | `[aprs\|bbs]` | Show Direwolf status or switch config profile |
| `:msg` / `:send` | `CALL text` | Send an APRS message via APRS-IS |
| `:scan` | | Scan LAN for radio services (rigctld, etc.) |
| `:info` | | Show rig connection info, frequency, mode, grid |
| `:help` | | Show command list |
| `:q` / `:quit` | | Exit rigtop |

Tab completion is supported. Press `Esc` to cancel.

### TUI title bar badges

| Badge | Colour | Meaning |
|-------|--------|---------|
| **APRS RF** | — | NMEA sink active; Direwolf/PinPoint receiving GPS |
| **APRS IG** | — | APRS-IS internet gateway connected |
| **APRS** | — | APRS mode active without IS or RF detail |
| **⚠ WATCHDOG** | — | TX watchdog tripped — PTT was forced off |

### TUI panels

- **Rig / Meters** — frequency, mode, PTT indicator, meter bars (S-meter, ALC, SWR, power, etc.) with colour-coded warnings
- **GPS** — position in degrees/minutes, decimal, Maidenhead grid, GPS source
- **Connections** — status of all sinks (serial, TCP, UDP) with client counts
- **APRS** — incoming APRS traffic feed (yellow = local RF via Direwolf, green = RF-gated via APRS-IS, cyan = internet-only) with packet counts
- **Log** — filtered log output from rigtop and rigctld stderr
- **Command bar** — hint bar showing available commands, status messages

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `-c, --config` | auto `rigtop.toml` | Path to TOML config file |
| `--log-level` | `WARNING` | Log level (also controls rigctld verbosity) |
| `--console` | off | Plain console output instead of TUI |
| `--once` | off | Read once and exit |
| `--no-rigctld` | off | Don't auto-start rigctld |
| `--no-gps` | off | Disable GPS fallback |
| `--no-meters` | off | Disable rig meters |

## rigctld

rigtop auto-starts `rigctld` when `[rigctld]` is in the config. Rigctld stderr
is logged to `rigtop.log`.

To run rigctld manually instead, use `--no-rigctld`:

```bash
# Windows
rigctld -m 3085 -r COM9 -s 19200 -T 127.0.0.1 -t 4532 -vvv

# Linux
rigctld -m 3085 -r /dev/ttyUSB0 -s 19200 -T 127.0.0.1 -t 4532 -vvv
```

Common Hamlib models: 3085 = IC-705, 3073 = IC-7300, 3060 = IC-9700.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   rigctld    │◄────│  rigtop      │────►│  Direwolf    │
│  (Hamlib)    │     │  main loop   │     │  KISS TCP    │
└──────┬───────┘     └──────┬───────┘     └──────────────┘
       │                    │               RF decodes ▲
  GPS, freq, mode,     polls every N sec               │
  meters, PTT              │
       │              ┌─────┴──────────────────────┐
       ▼              │         Sinks               │
┌──────────────┐      │                             │
│ GPS fallback │      │  ┌─────┐ ┌──────┐ ┌──────┐ │
│  (GPS2IP)    │      │  │ TUI │ │ NMEA │ │ gpsd │ │
└──────────────┘      │  └─────┘ └──────┘ └──────┘ │
                      │  ┌───────┐ ┌────────────┐  │
                      │  │WSJT-X │ │  APRS-IS   │  │
                      │  └───────┘ └────────────┘  │
                      └────────────────────────────┘
```

### Sources

- **RigctldSource** — connects to rigctld via TCP. Reads GPS position, frequency,
  mode, passband, PTT, and meter levels. Also supports `set_freq()`, `set_mode()`,
  and `set_ptt()` (used by the TX watchdog).
- **Gps2ipSource** — connects to iOS GPS2IP app via TCP. Parses NMEA GGA/RMC
  sentences for position. Used as fallback when the rig has no GPS fix.
- **DirewolfClient** — connects to Direwolf KISS TCP port. Decodes AX.25 UI frames
  to TNC2 text and feeds them into the shared APRS buffer (shown in yellow in the TUI).

### Sink plugin system

Sinks are registered via the `@register_sink("name")` decorator. The factory
`create_sink()` in `rigtop/sinks/__init__.py` creates instances from config dicts,
filtering constructor kwargs via `inspect.signature` so each sink only receives
the parameters it accepts.

## APRS setup example (Windows + IC-705)

This example uses APRS RF via Direwolf and APRS-IS via rigtop simultaneously.

### Requirements

- IC-705 connected via WLAN (remote control) or USB
- [Direwolf](https://github.com/wb2osz/direwolf) for 1200 baud AFSK
- [VSPD](https://www.eltima.com/products/vspdxp/) for virtual COM port pairs
- Virtual audio cable (e.g. VB-Cable) to route rig audio to Direwolf

### Virtual serial ports

Create two VSPD pairs:
- **COM10 ↔ COM11** — rigtop writes NMEA to COM10, Direwolf reads COM11
- **COM12 ↔ COM13** — rigtop writes NMEA to COM12, PinPoint reads COM13

### rigtop.toml

```toml
[aprs]
freq = 144.800
qsy_mode = "FM"

[[sink]]
type = "tui"

[[sink]]
type = "nmea"
device = "COM10"    # → Direwolf reads COM11

[[sink]]
type = "nmea"
device = "COM12"    # → PinPoint reads COM13

[[sink]]
type = "aprsis"
callsign = "N0CALL-1"
server = "euro.aprs2.net"
passcode = "12345"
interval = 120
aprs_filter = "r/59.2/18.1/200"
```

### Direwolf config

```
GPSNMEA COM11
MODEM 1200
```

The rig must be on 144.800 MHz FM (not data mode) for standard 1200 baud APRS.

## Logging

Logs are written to `rigtop.log` in the current directory (5 MB rotation, 3 files retained).

## Project structure

```
rigtop/
  cli.py                   Entry point, CLI parsing, sink/source wiring
  app.py                   Poll loop, TxWatchdog, resolve_position, collect_meters
  config.py                TOML config loader, Pydantic models
  geo.py                   Maidenhead, NMEA sentence builders, coordinate formatting
  zones.py                 CQ/IARU zone + country lookup from lat/lon
  rigctld_launcher.py      Spawn and manage rigctld subprocess
  direwolf_launcher.py     Start/stop Direwolf via winpty (Windows PTY)
  discovery.py             LAN scan for radio services
  sources/
    __init__.py            Position dataclass, GpsSource ABC, registry
    rigctld.py             RigctldSource — rig GPS, freq, mode, meters, PTT
    gps2ip.py              Gps2ipSource — iOS GPS2IP NMEA stream
    direwolf.py            DirewolfClient — KISS TCP client for RF decodes
  sinks/
    __init__.py            PositionSink ABC, registry, create_sink factory
    tui.py                 RigtopApp — rich full-screen Textual dashboard
    console.py             ConsoleSink — plain text output
    nmea.py                NmeaSink — NMEA GGA+RMC via serial or TCP
    gpsd.py                GpsdSink — gpsd JSON protocol server
    wsjtx.py               WsjtxSink — WSJT-X grid via UDP
    aprsis.py              AprsIsSink — APRS-IS beacon + receiver
    civ_proxy.py           CivProxySink — Icom CI-V serial proxy
```

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the phased development plan, feature ideas, and
contribution guidance.

## License

MIT