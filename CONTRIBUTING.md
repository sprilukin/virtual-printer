# Contributing

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pre-commit install
```

## Checks

CI runs these on every push; run them before opening a PR:

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/vulture
.venv/bin/pip-audit
```

## Pull requests

- Keep changes focused and add tests for new behavior.
- Describe what changed and why in the PR description.

## Contributing profiles

- Only submit output of `vp scrub`. Read it once before committing.
- Never attach raw captures: they contain your printer's real address, serial and MAC.
- Run `vp validate` and make sure your profile loads.
