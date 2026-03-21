# rigtop — Claude Code instructions

## Python / tooling

This project uses **uv** for all Python tasks. Always prefer:

```
uv run python   # instead of python / .venv/Scripts/python.exe
uv run pytest   # instead of python -m pytest
uv run ruff     # instead of python -m ruff
uv run pylint   # instead of python -m pylint
```

If `uv run` fails because `rigtop.exe` is locked (app is running), add `--no-sync` to skip the package rebuild:

```
uv run --no-sync pytest tests/
uv run --no-sync python -c "..."
```

Never call `.venv/Scripts/python.exe` directly.

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
  direwolf_launcher.py  # start/stop Direwolf via winpty (Windows PTY)
  rigctld_launcher.py   # start/stop rigctld subprocess
  discovery.py          # LAN scan for radio services
tests/
  test_geo.py      # 22 tests — geo functions
  test_config.py   # 21 tests — TOML loading, validation
  test_app.py      # 19 tests — TxWatchdog, resolve_position, collect_meters
  test_buffers.py  # 30 tests — AprsBuffer, MessageBuffer, DirewolfBuffer
```

## Code style

- Line length: 100
- Linter: ruff (select E, F, W, I, UP, B, SIM, RUF, PTH, PIE, TRY, G, C4, PERF)
- Type checker: mypy (not yet enforced on all files)
- No docstrings or type annotations required on code you didn't write

## Key conventions

- All `connections()` dicts must have: `label`, `kind`, `status`, and optionally `address`, `clients`
- `address` is a separate field (host:port or device path) — do NOT embed it in `label`
- `TxWatchdog`, `resolve_position`, `collect_meters` live in `app.py` — use them from there
- Zone lookups always go through `rigtop.zones.lookup(lat, lon)` (cached, offline)
- Config is loaded once in `cli.main()` — pass values down, don't re-read TOML at runtime

## Commit messages

All commits **must** follow the [Conventional Commits](https://www.conventionalcommits.org/) format enforced by commitizen:

```
type(scope)?: short description
```

Valid types: `build`, `bump`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test`

Examples:
```
feat(tui): add waterfall panel
fix(rigctld): handle None response from get_level
ci: only build wheel on main branch
```

The CI `commit-lint` job runs `cz check` on every PR and will fail on non-conforming messages. This applies to all commits — including auto-generated ones (e.g. from Copilot suggestions). If a bad commit lands on the branch, rebase and amend before pushing.

## Running

```
uv run rigtop               # full TUI
uv run rigtop --console     # plain console mode
uv run rigtop --scan        # LAN scan
uv run pytest tests/        # unit tests
uv run ruff check rigtop/   # lint
```
