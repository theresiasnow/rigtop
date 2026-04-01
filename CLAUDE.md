# rigtop — Claude Code instructions

## Project layout

```
rigtop/
  app.py           # poll loop, TxWatchdog, resolve_position, collect_meters
  cli.py           # main() + phase helper functions, AppResources dataclass
  config.py        # Pydantic config models + TOML loader
  geo.py           # maidenhead, NMEA sentence builders, format_position
  zones.py         # CQ/IARU zone + country lookup from lat/lon (reverse_geocoder)
  sources/
    rigctld.py     # TCP connection to rigctld
    gps2ip.py      # iOS GPS fallback over TCP
    direwolf.py    # Direwolf KISS TCP client
  sinks/
    tui.py         # Textual TUI (RigtopApp, StationPanel, RigPanel, ConnectionBar)
    aprsis.py      # APRS-IS TCP sink + beacon
    nmea.py        # NMEA serial / TCP server sink
    gpsd.py        # gpsd JSON TCP server sink
    wsjtx.py       # WSJT-X UDP grid update sink
    civ_proxy.py   # Icom CI-V serial proxy (dedicated rigctld socket)
    console.py     # plain stdout sink
  direwolf_launcher.py  # start/stop Direwolf (ConPTY on Windows, pipe on Linux/Mac)
  rigctld_launcher.py   # start/stop rigctld subprocess
  discovery.py          # LAN scan for radio services
tests/
  test_geo.py      # geo functions
  test_config.py   # TOML loading, validation, multi-rig
  test_app.py      # TxWatchdog, resolve_position, collect_meters
  test_buffers.py  # AprsBuffer, MessageBuffer, DirewolfBuffer
```

## Key conventions

- All `connections()` dicts must have: `label`, `kind`, `status`, and optionally `address`, `clients`
- `address` is a separate field (host:port or device path) — do NOT embed it in `label`
- `TxWatchdog`, `resolve_position`, `collect_meters` live in `app.py` — use them from there
- Zone lookups always go through `rigtop.zones.lookup(lat, lon)` (cached, offline)
- Config is loaded once in `cli.main()` — pass values down, don't re-read TOML at runtime
- Multiple rigs: use `[[rig]]` + `[rig.rigctld]` in TOML; `cfg.select_rig(name)` switches active rig
- `except (A, B):` — always use tuple form; bare `except A, B:` silently only catches `A` in Python 3
- Type checker: mypy (not yet enforced on all files)

## Running

```
uv run rigtop                    # full TUI (first rig)
uv run rigtop --rig <name>       # select rig by name from [[rig]] config
uv run rigtop --console          # plain console mode
uv run rigtop --scan             # LAN scan
uv run pytest tests/             # unit tests
uv run ruff check rigtop/        # lint
```

## PR workflow

1. **Branch** — `feat/` or `fix/`, never `main` (hotfixes only)
2. **Lint + test** — `uv run ruff check rigtop/` and `uv run pytest tests/`
3. **PR** — `gh pr create` with summary and test plan
4. **Copilot review** — `gh pr edit <number> --add-reviewer copilot`
5. **Check review** — read and address all comments:
   ```bash
   gh api repos/theresiasnow/rigtop/pulls/<number>/reviews --jq '.[] | {user: .user.login, state, body}'
   gh api repos/theresiasnow/rigtop/pulls/<number>/comments --jq '.[] | {path, line, body}'
   ```
6. **Check CI** — fix any failing jobs before merging:
   ```bash
   gh pr checks <number>
   gh run view <run-id> --log-failed
   ```
