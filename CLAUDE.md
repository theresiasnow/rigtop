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
  direwolf_launcher.py  # start/stop Direwolf (ConPTY on Windows, pipe on Linux/Mac)
  rigctld_launcher.py   # start/stop rigctld subprocess
  discovery.py          # LAN scan for radio services
tests/
  test_geo.py      # geo functions
  test_config.py   # TOML loading, validation, multi-rig
  test_app.py      # TxWatchdog, resolve_position, collect_meters
  test_buffers.py  # AprsBuffer, MessageBuffer, DirewolfBuffer
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
- Multiple rigs: use `[[rig]]` + `[rig.rigctld]` in TOML; `cfg.select_rig(name)` switches active rig
- `except (A, B):` — always use tuple form; bare `except A, B:` silently only catches `A` in Python 3

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

## PR workflow

For every feature or fix:

1. **Branch** — always work on a `feat/` or `fix/` branch, never commit directly to `main`
2. **Implement** — lint (`uv run ruff check rigtop/`) and test (`uv run pytest tests/`) before pushing
3. **PR** — create a pull request with a clear summary and test plan
4. **Copilot review** — assign `copilot-pull-request-reviewer[bot]` as reviewer:
   ```
   gh pr edit <number> --add-reviewer "copilot-pull-request-reviewer[bot]"
   ```
5. **Check review** — read Copilot's comments and address any issues before merging:
   ```
   gh api repos/theresiasnow/rigtop/pulls/<number>/reviews --jq '.[] | {user: .user.login, state, body}'
   gh api repos/theresiasnow/rigtop/pulls/<number>/comments --jq '.[] | {path, line, body}'
   ```

## Running

```
uv run rigtop                    # full TUI (first rig)
uv run rigtop --rig <name>       # select rig by name from [[rig]] config
uv run rigtop --console          # plain console mode
uv run rigtop --scan             # LAN scan
uv run pytest tests/             # unit tests
uv run ruff check rigtop/        # lint
```
