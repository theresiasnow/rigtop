## v0.7.2 (2026-03-24)

### Fix

- **build**: add freeze_support() and limit OpenBLAS threads for PyInstaller

## v0.7.1 (2026-03-24)

## v0.7.0 (2026-03-24)

### Feat

- **build**: PyInstaller + Inno Setup Windows installer
- **gps**: auto-reconnect GPS2IP fallback every 30s
- **rigctld**: show connected CAT client names in connection bar
- **config**: per-rig rigctld config and --rig startup selection

### Fix

- **except**: correct bare except clauses to tuple form

## v0.6.1 (2026-03-22)

## v0.6.0 (2026-03-22)

### Feat

- **config**: per-rig capability hints for multi-radio support

## v0.5.0 (2026-03-22)

### Feat

- **direwolf**: control TBEACON via rigtop-owned derived config

## v0.4.1 (2026-03-22)

### Fix

- **tui**: make :beacon work without APRS-IS sink (Direwolf TBEACON)

## v0.4.0 (2026-03-21)

### Feat

- **tui**: rig control pane with waterfall and DSP controls
- name field for sinks; nmea label + toggle all instances
- add gpsd toggle command; clarify nmea vs gpsd in help/placeholder
- beacon respects rigtop.toml setting
- show beacon on/off indicator in station panel
- print shutdown progress to console on quit
- add :beacon [on|off] runtime toggle for APRS-IS position beaconing
- show traffic/messages panes in packet mode
- move rigctld to ConnectionBar; add :send alias; reorder panes
- restore APRS traffic and message panes (shown only in APRS mode)
- add CQ/IARU zones, country from GPS; align ConnectionBar columns
- Textual TUI with full command system, sink toggles, and badge fixes
- static GPS fallback + enabled flag for GPS sources\n\nAdd [gps_static] config section with lat/lon/alt for fixed stations\nwhen neither rig GPS nor gps2ip fallback have a fix.\n\nBoth [gps_fallback] and [gps_static] now support an `enabled` field\n(default true) to toggle without removing the config section."
- Direwolf KISS client, TUI layout overhaul, ruff linting\n\nNew features:\n- Direwolf KISS TCP client for local RF APRS decodes (3-color scheme:\n  yellow=RF-local, green=RF-gated, cyan=internet-only)\n- TUI Station pane: radio name, altitude, uptime counter\n- Position dataclass now includes optional altitude\n- Altitude parsed from rigctld and gps2ip (GGA) responses\n\nTUI improvements:\n- Symmetric layout: Rig/Meters | Station side-by-side\n- Connections panel now full-width row below top panels\n- Removed log pane (TuiLogBuffer, :log/:clear commands)\n- Renamed GPS pane to Station\n\nCode quality:\n- Added ruff linter (E/F/W/I/UP/B/SIM/RUF rules)\n- StrEnum for all enum classes (Python 3.14)\n- Sorted imports, datetime.UTC, collections.abc.Callable\n- Added *.log to .gitignore"
- add GPS sinks (direwolf, gpsd) and TUI connections pane\n\n- Add direwolf NMEA sink with TCP and serial port modes\n- Add gpsd-compatible JSON protocol sink (v3.14)\n- Add pyserial as regular dependency for Windows COM port output\n- Add connections() method to PositionSink for status reporting\n- Add Connections pane to TUI showing COM/TCP/UDP client status\n- Add CLI flags for --direwolf, --gpsd and their options\n- Add atexit handler for rigctld cleanup\n- Configure sinks: direwolf COM10/COM12, gpsd :2947, wsjtx :2237"
- k9s-style command bar, rigctld stderr capture, new commands
- dynamic log panel that fills remaining terminal height
- add serial port and PTT config support for rigctld

### Fix

- **tui**: gate :packet on behind bbs.enabled; fix misleading docs
- **tui**: prevent ATT/Pre/NB/NR from resetting after button press (#11)
- **tui**: restore beacon indicator for disabled AprsIS sinks
- show all sink toggles in command bar placeholder
- restore packet on/off in command bar placeholder
- beacon indicator requires sink connected AND enabled
- beacon defaults to false — opt-in, not opt-out
- station panel — tab-stop columns, split Grid/Zones/Country, add Pos label
- clean up station panel — remove duplicate coords, align labels
- add beacon to command bar placeholder
- show "(no packets received)" in packet mode instead of "(no APRS traffic)"
- add :send tooltip — placeholder, help text, and suggester hint
- give CI-V proxy its own rigctld connection to prevent HRD lockups

### Refactor

- move name + enabled to PositionSink base class
- remove beacon field — sink enabled controls beacon state
- extract classes/functions from app.py and cli.py, expand linting
- move main.py into package, add :data and :aprs QSY commands
- package structure with source/sink abstractions and CLI
