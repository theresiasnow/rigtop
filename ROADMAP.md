# rigtop — Product Roadmap

This document captures the current state of rigtop, ideas surfaced by users and
contributors, and a phased plan to evolve the project into a polished, broadly
useful ham-radio dashboard.

---

## Current state (v0.x)

rigtop started as a thin Hamlib `rigctld` wrapper with a rich TUI for frequency
and meter display.  Over time it has grown into a multi-purpose ham-radio station
integration layer:

| Area | What exists today |
|------|-------------------|
| **Rig control** | Frequency, mode, passband, PTT, and meters (S-meter, ALC, SWR, power, …) via rigctld; auto-launches rigctld subprocess |
| **GPS** | Position from rig (rigctld), iOS GPS2IP fallback, static config fallback |
| **TUI** | Full-screen rich dashboard: rig/meters panel, GPS, connections, APRS traffic feed, log pane, vim-style `:command` bar |
| **APRS** | APRS-IS beacon + receive; Direwolf KISS TCP for RF decodes; smart iGate colour-coding in TUI |
| **GPS forwarding** | NMEA GGA/RMC over serial or TCP; gpsd-compatible JSON server |
| **Digital modes** | WSJT-X Maidenhead grid via UDP; BBS/packet QSY via `:bbs` command |
| **Safety** | TX watchdog — forces PTT off after configurable timeout |
| **Extensibility** | Sink plugin system (`@register_sink`); source registry |

---

## Themes and ideas

The items below are unordered brainstorm input gathered from the issue thread and
general ham-radio operator needs.  They feed the phased plan that follows.

### Rig control
- VFO A/B switching, split operation
- Memory-channel recall, store, and scan
- Band-stacking register support
- Band-plan–aware automatic mode selection on QSY
- Multi-rig support (connect to several rigctld instances simultaneously)
- Rotor control via `rotctld` (azimuth/elevation display and control)
- CAT command passthrough / CI-V proxy improvements

### GPS & position
- Android GPS input (Bluetooth NMEA, "Share GPS" style apps)
- gpsd *client* mode — consume an existing `gpsd` instance instead of only serving one
- USB GPS dongle support via direct serial NMEA parsing
- Bluetooth GPS receiver support

### APRS
- APRS message inbox and outbox with acknowledgement tracking
- APRS object / item transmission (fixed infrastructure beacons)
- Weather station beacon (WX frames from a connected weather sensor)
- Full iGate mode — gate received RF packets to APRS-IS
- SmartBeaconing — speed- and heading-adaptive beacon interval
- Configurable APRS symbol, overlay, and comment
- Multi-SSID / multi-callsign support (e.g., separate home and mobile beacons)

### Packet & digital modes
- Full BBS client — connect, read and send mail via a connected TNC
- Winlink / VARA modem integration for email over radio
- JS8Call heartbeat monitoring and grid display
- FT8/FT4 frequency awareness from WSJT-X UDP (show recent activity in TUI)

### Logging & contesting
- ADIF QSO log export
- Integration with popular logging software (Log4OM, HAMRS, etc.)
- Contest-mode overlay: worked grids, multipliers, dupe check
- DX cluster spot integration (hamAlert, DX Summit) — highlight spots in TUI

### Notifications
- Desktop notifications for incoming APRS messages addressed to the operator
- Audio alert for specific callsigns or events (DX spots, APRS messages)

### Platform & deployment
- `pip`/`pipx` installable package on PyPI
- Windows installer (PyInstaller-based `.exe` bundle)
- macOS ARM (`aarch64`) testing and documentation
- Raspberry Pi–optimised configuration and installation guide
- Docker image for headless deployment
- `systemd` unit file and Windows service wrapper
- Headless / daemon mode with a lightweight HTTP status endpoint

### Developer experience
- Expanded test coverage (unit tests for sources, sinks, geo calculations)
- CI pipeline (lint + test on Linux, Windows, macOS)
- Typed public API for sink/source authors
- Example / skeleton sink for contributors

---

## Phased roadmap

### Phase 1 — Stability & packaging  *(near-term)*

Goals: make rigtop easy to install and reliable enough for daily use.

- [ ] Publish to PyPI (`uv build` → `pip install rigtop`)
- [ ] Windows PyInstaller bundle with embedded `rigctld`
- [ ] Automated CI: ruff lint + pytest on Linux and Windows
- [ ] Unit tests for `geo.py` (Maidenhead, NMEA sentences)
- [ ] Unit tests for config validation (pydantic models)
- [ ] Improve error messages and recovery on rigctld/GPS disconnect
- [ ] Document `civ_proxy` sink and `discovery.py` in README

### Phase 2 — Rig control & usability  *(mid-term)*

Goals: make rigtop useful as a primary rig-control front-end.

- [ ] VFO A/B switching and split operation via `:vfo` command
- [ ] Memory-channel operations (`:mem recall N`, `:mem store N`)
- [ ] Band-plan auto-mode on `:freq` QSY
- [ ] Configurable APRS symbol and overlay in config + `:aprs symbol` command
- [ ] SmartBeaconing (replace fixed interval with speed/heading algorithm)
- [ ] gpsd *client* source (consume external gpsd — complements the existing server sink)
- [ ] Android GPS input (TCP NMEA from Share GPS or similar)

### Phase 3 — APRS feature parity  *(mid-term)*

Goals: make rigtop a first-class APRS station client.

- [ ] APRS message inbox and outbox with ACK tracking
- [ ] Full RF iGate mode (gate Direwolf decodes to APRS-IS)
- [ ] APRS object / item transmission
- [ ] WX beacon support (read a weather API or serial sensor)
- [ ] Multi-SSID beaconing

### Phase 4 — Packet, digital & logging  *(longer-term)*

Goals: expand beyond APRS into the broader digital-modes ecosystem.

- [ ] BBS connect/read/send client over KISS TNC
- [ ] Winlink integration (pass GPS position and send messages)
- [ ] JS8Call status monitoring (heartbeat, grid, heard list)
- [ ] FT8/FT4 frequency display from WSJT-X UDP
- [ ] ADIF QSO log export
- [ ] DX cluster integration with TUI spot pane

### Phase 5 — Headless & ecosystem  *(future)*

Goals: make rigtop work beyond the terminal and integrate with the wider ham ecosystem.

- [ ] Lightweight HTTP status endpoint (JSON + minimal HTML page)
- [ ] Docker image for Raspberry Pi and x86-64
- [ ] `systemd` unit file and Windows service wrapper
- [ ] REST/WebSocket API for external integrations
- [ ] Rotor control via `rotctld` (azimuth/elevation panel in TUI)
- [ ] Multi-rig support (connect to several rigctld instances)
- [ ] Desktop notification sink (OS-level alerts for messages)

---

## Contributing

Pull requests, feature requests, and bug reports are welcome.  Please open an
issue before starting work on a large feature so the scope can be agreed on.

For development setup see the README.  All code is formatted and linted with
**ruff** (`uv run ruff check . && uv run ruff format .`).
