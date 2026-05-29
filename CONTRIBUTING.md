# Contributing to handoff-fanout

Thanks for your interest. This project is small enough that contributions are evaluated by the maintainer directly; there's no triage rotation or RFC process.

## Ground rules

1. **One concern per PR.** A doc fix, a bugfix, and a feature are three PRs, not one.
2. **Tests for behavior, not for coverage.** Every new branch in the state machine needs a test that would fail without it. We don't chase line-coverage numbers.
3. **No new runtime dependencies.** This is a zero-dep tool by design. If you think you need a dep, open an issue first.
4. **Cross-platform.** macOS and Linux are first-class. Windows is best-effort (no `flock`, no launchd) — patches welcome, but don't break the POSIX path.

## Dev setup

This repo ships a `uv.lock`, so `uv` is the recommended toolchain:

```bash
git clone https://github.com/rssprivacy-commits/handoff-fanout.git
cd handoff-fanout
uv sync --extra dev --extra lint     # creates .venv with pytest + pytest-asyncio + ruff
```

The `dev` and `lint` extras are NOT installed by a bare `uv sync` — without
them `.venv/bin/python -m pytest` fails with `No module named pytest`, and a
`uv run --with pytest` shortcut still omits `pytest-asyncio` (you'll see a
spurious `Unknown config option: asyncio_mode` warning). Always include both
extras.

Plain pip works too:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,lint]"
```

The editable install also wires up the `handoff-*` console scripts so you can test them locally without re-installing.

## Running tests

```bash
pytest                    # full suite
pytest -k orphan          # subset
pytest -x                 # stop at first failure
pytest tests/test_atomic.py -v
```

Tests are isolated via `tests/conftest.py` (the `isolated_handoff_home` fixture pins `HANDOFF_HOME` to a `tmp_path`). No test ever touches the real `~/.handoff/`.

### Skipping tests appropriately

A few tests are POSIX-only (those exercising `flock`). They self-skip on platforms missing `fcntl.LOCK_EX`. Do not add `@pytest.mark.skip` without a one-line justification in the decorator.

## Lint

```bash
ruff check src/ tests/
ruff format --check src/ tests/
```

CI runs both; PRs failing either will be auto-flagged.

## Project layout

```
src/handoff_fanout/
    __init__.py
    __main__.py            # python -m handoff_fanout
    cli.py                 # unified `handoff <subcommand>` dispatcher
    config.py              # Config dataclass + load() from $HANDOFF_HOME/config.json
    templates.py           # markdown builders (no Jinja dependency)
    atomic.py              # atomic_create / write_with_fsync / acquire_dir_lock
    dump.py                # single-task + fan-out queue file generation
    watchdog.py            # periodic scanner
    heartbeat.py           # fan-in heartbeat daemon + Amdahl metrics
    safe_commit.py         # hijack-safe git commit wrapper
    git_guard/
        __init__.py        # exposes git_guard_dir() resolver
        git                # the actual PATH-injected shell script

tests/
    conftest.py            # isolated_handoff_home + git_repo fixtures
    test_smoke.py          # `handoff --version` doesn't crash
    test_atomic.py         # Layer 4
    test_git_guard.py      # Layer 1
    test_safe_commit.py    # Layer 3 (unit-level)
    test_handoff_orphan.py # Layer 5 + dump (black-box)
    test_handoff_hijack.py # Layers 2+3 combined (black-box)

docs/
    PROTOCOL.md            # wire format spec
    ARCHITECTURE.md        # 5-layer walk-through

install/
    install.sh             # idempotent installer
    git-hooks/
        pre-commit         # Layer 2 hook (HANDOFF_EXPECTED_FILES check)
    launchd/
        com.handoff-fanout.watchdog.plist
    examples/
        config.json        # template config

.github/workflows/
    ci.yml                 # Python 3.11/3.12/3.13 × ubuntu/macos matrix
```

## Adding a new layer or scan mode

If you're touching the safety story (a new defense layer, a new watchdog scan mode, a new role), there's a longer process:

1. Open an issue describing the failure scenario the layer prevents.
2. Add a black-box test in `tests/test_handoff_orphan.py` or `tests/test_handoff_hijack.py` that **reproduces the failure without your fix**.
3. Implement the layer.
4. Update `docs/ARCHITECTURE.md` with the new layer's section.
5. Update `docs/PROTOCOL.md` if the wire format changed.

## Commit messages

Conventional-commits style:

```
feat(dump): support --next-after-fanin field in manifest
fix(watchdog): handle missing batch_dir during last-one-out check
docs(protocol): document orphan-sub-task scan mode
test(hijack): cover --no-verify bypass path
chore(ci): bump checkout action to v5
```

The scope is the affected module (`dump`, `watchdog`, `heartbeat`, `safe-commit`, `git-guard`, `atomic`, `config`, `templates`, `cli`).

## Issue & PR etiquette

- Reproduction steps in the bug report are the most valuable thing you can provide.
- If you're proposing a feature, sketch the user-facing API (CLI flags, env vars, config keys) in the issue first. We'd rather argue about the surface area before code than after.
- We don't squash-merge silently. PRs land with a coherent commit history; if your branch has WIP commits, please `rebase -i` them down before requesting review.

## Release process (maintainer notes)

```
1. Update CHANGELOG.md, move [Unreleased] items into a new [vX.Y.Z] section.
2. Bump pyproject.toml `version = "X.Y.Z"`.
3. git commit -m "chore(release): vX.Y.Z"
4. git tag -a vX.Y.Z -m "vX.Y.Z"
5. git push && git push --tags
6. CI builds the wheel + sdist; the release workflow drafts a GitHub Release.
7. Edit the draft release notes from CHANGELOG, then publish.
```

## License

By contributing, you agree your contribution is licensed under the project's MIT license. No CLA.
