# Contributing to MultiDrop

Thanks for helping! Bug reports, device reports and pull requests are all
welcome.

## Reporting a bug

Open an issue with:

- your OS (Linux distribution or Windows version) and how you installed MultiDrop
- the other device (model, OS version, app: Quick Share, LocalSend, AirDrop, browser)
- what you did, what you expected and what happened
- the log: `multidrop -v …` on Linux, `%LOCALAPPDATA%\multidrop\multidrop.log`
  on Windows

**Device reports** help a lot too: "Galaxy S24 → Windows 11 via Quick Share
works" is useful information, because we can't test every phone.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest -q
```

The test suite runs on Linux and Windows (CI runs both). Tests that need
real hardware or Linux-only parts (AirDrop radio, GTK, BlueZ) are skipped
elsewhere.

Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how the parts fit
together) and [docs/RESEARCH.md](docs/RESEARCH.md) (protocol details).

## Pull requests

- Keep a pull request to one topic; smaller ones get reviewed faster.
- Add or update tests for behaviour changes.
- Match the surrounding code: plain names, short comments that explain *why*.
- Describe what you tested, and on which devices. Say so if something is only
  tested in simulation.

By contributing you agree that your work is licensed under
[GPL-3.0-or-later](LICENSE), like the rest of MultiDrop.
