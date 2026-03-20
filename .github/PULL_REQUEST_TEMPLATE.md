## Summary

<!-- What does this PR do? One paragraph is fine. -->

## Type of change

- [ ] Bug fix
- [ ] New feature / sink
- [ ] Refactor (no behaviour change)
- [ ] Docs / config
- [ ] CI / tooling

## Testing

<!-- How was this tested? rigctld connection, TUI smoke, --once mode, etc. -->

- [ ] Imported cleanly (`python -m rigtop --help`)
- [ ] Tested against a live rig / rigctld
- [ ] Tested `--once` / `--console` modes
- [ ] Ruff passes (`ruff check rigtop/ && ruff format --check rigtop/`)

## Ham radio notes

<!-- Callsign, rig model, operating mode tested — helps reviewers replicate. -->
<!-- Leave blank if not applicable. -->

## Checklist

- [ ] New config fields have pydantic validators
- [ ] New sinks inherit `PositionSink` and use `@register_sink`
- [ ] Socket/serial errors handled with `OSError` / `ConnectionError`
- [ ] No PTT can be left stuck ON (check `TxWatchdog` if touching PTT logic)
- [ ] `loguru` used for logging (no bare `print` for debug output)
