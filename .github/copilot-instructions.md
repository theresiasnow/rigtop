# Copilot Instructions — rigtop

## Project overview

**rigtop** is a ham radio rig dashboard — a Python 3.14+ terminal application that
polls a radio transceiver via `rigctld` (Hamlib), reads GPS position, and displays
a live TUI built with `rich`. It also beacons APRS position packets to APRS-IS and
forwards GPS sentences to external apps.

## Architecture

```
rigtop/
  cli.py          — argparse entry point; builds AppResources, calls app.run()
  app.py          — polling loop; TxWatchdog, collect_meters, resolve_position
  config.py       — pydantic TOML models (Config, SinkConfig, RigConfig, …)
  geo.py          — maidenhead grid and position formatting
  sources/
    rigctld.py    — TCP client for rigctld (freq, mode, PTT, GPS, meters)
    gps2ip.py     — NMEA over TCP from gps2ip iOS app
    direwolf.py   — KISS TCP client for Direwolf TNC
  sinks/
    tui.py        — full-screen rich dashboard (TuiSink)
    aprsis.py     — APRS-IS beacon + receive (AprsIsSink)
    nmea.py       — NMEA GGA/RMC output over serial or TCP
    console.py    — plain text stdout sink
    wsjtx.py      — WSJT-X UDP grid update
    gpsd.py       — gpsd-compatible JSON server
    civ_proxy.py  — CI-V serial proxy for Icom radios
```

## Key conventions

- **Python 3.14+** — use modern syntax: `X | Y` unions, `match`, PEP 695 type aliases where appropriate
- **Pydantic v2** for all config models; use `model_validator(mode="after")` not `root_validator`
- **loguru** for logging (`logger.info`, `logger.warning`, etc.); never use `print` for log output
- **rich** for all TUI rendering; avoid raw ANSI escape codes
- **uv** for package management; `uv sync` / `uv run`; never `pip install` directly in dev
- **ruff** for linting + formatting (line length 100, target py314)
- All sinks inherit `PositionSink` from `rigtop/sinks/__init__.py` and register via `@register_sink("name")`
- GPS priority: rig → gps2ip fallback → static position from config
- PTT and meter polling are always from rigctld, never from GPS sources

## Ham radio domain context

- **rigctld** — Hamlib daemon; rigtop connects via TCP (default 4532)
- **PTT** — Push-to-talk; TX=transmitting, RX=receiving
- **APRS** — Automatic Packet Reporting System; position beacons sent to APRS-IS servers
- **Maidenhead grid** — 6-character locator (e.g. JP90qd) computed from lat/lon
- **Direwolf** — software TNC that decodes RF APRS packets via soundcard
- **CI-V** — Icom's serial rig control protocol
- **S-meter** — received signal strength; S9 = -73 dBm reference, each S-unit = 6 dB
- **SWR** — Standing Wave Ratio; 1.0 = perfect, >3.0 = dangerous

## Commit messages

All commits **must** follow [Conventional Commits](https://www.conventionalcommits.org/) format, enforced by commitizen (`cz check`):

```
type(scope)?: short description
```

Valid types: `build`, `bump`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test`

Examples:
```
feat(tui): add waterfall panel
fix(rigctld): handle None response from get_level
chore: initial planning notes
```

This applies to **every** commit, including planning and scaffolding commits. The CI `commit-lint` job runs `cz check` on every PR and will fail on non-conforming messages.

## Code review focus areas

When reviewing PRs, pay attention to:
1. **Thread safety** — the TUI sink is updated from the poll thread; `_key_listener` runs in a daemon thread
2. **Socket error handling** — rigctld, APRS-IS, and Direwolf connections must handle `OSError` / `ConnectionError` gracefully and attempt reconnect
3. **PTT state** — never leave PTT stuck ON; watchdog in `TxWatchdog` guards this
4. **Config validation** — new config fields should use pydantic validators, not runtime checks
5. **Sink registration** — new sinks must use `@register_sink("type_name")` and inherit `PositionSink`
