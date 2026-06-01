#!/usr/bin/python3
# CANONICAL global `dump-handoff.py` re-exec shim — handoff-fanout.
#
# This is the single source of truth for the deployed global entry point
# ``~/.local/bin/dump-handoff.py`` (synced by ``install/install.sh --sync-dump``)
# and for each project's ``scripts/dump-handoff.py`` copy. It exists to close the
# v5.4 version-drift gap: the pre-A4 standalone global never routed to the engine,
# so the retro/audit mandate gate silently never fired for the auto-continue
# self-propagation chain even though HANDOFF_RETRO_MANDATE / HANDOFF_AUDIT_MANDATE
# were flipped ON. This shim makes EVERY ``dump-handoff.py`` invocation run the
# real ``handoff_fanout`` engine, so the gate truly takes effect.
#
# Why a re-exec shim (not an in-process ``import handoff_fanout``):
#   * Claude Code sessions put a tob uv-shim first on PATH; it exits 1 for any bare
#     ``python3``. A ``/usr/bin/python3`` shebang (this file) is invoked by absolute
#     path, so it is immune — but /usr/bin/python3 is 3.9 and CANNOT import the
#     engine (``requires-python >=3.11``). So we keep the 3.9-safe launcher shebang
#     and ``os.execv`` a 3.11+ interpreter that HAS the engine.
#   * The canonical engine install is the ERP venv (``pip install -e`` editable —
#     owner law: handoff-fanout engine development happens under the ERP project so
#     source edits take effect immediately). Routing all projects through it is fine:
#     the engine is project-agnostic (project = --project or basename(cwd)).
#
# Override the engine interpreter with ``HANDOFF_ENGINE_PYTHON=/abs/python``.
from __future__ import annotations

import os
import sys

# The established multi-project handoff tree on this box. Virtually always already
# set via ~/.zshenv + launchctl; setdefault only matters if the env is missing.
os.environ.setdefault("HANDOFF_HOME", os.path.expanduser("~/.claude-handoff"))

# Resolve an interpreter that can import the engine (py>=3.11 with handoff_fanout).
# Order: explicit override → ERP venv (canonical editable install). Extend the list
# when another project hosts the engine; do NOT fall back to a bare ``python3``
# (would hit the uv-shim / a 3.9 system python without the engine). Each candidate
# must be an EXECUTABLE FILE — a stale dir / non-exec path is skipped, not handed to
# execv to blow up with a traceback.
_CANDIDATES = [
    os.environ.get("HANDOFF_ENGINE_PYTHON"),
    os.path.expanduser("~/Projects/erp-system/.venv/bin/python"),
]


def _usable(c: str | None) -> bool:
    return bool(c) and os.path.isfile(c) and os.access(c, os.X_OK)


_engine_py = next((c for c in _CANDIDATES if _usable(c)), None)
if not _engine_py:
    sys.stderr.write(
        "❌ handoff engine interpreter not found / not executable.\n"
        "   Tried HANDOFF_ENGINE_PYTHON + ~/Projects/erp-system/.venv/bin/python.\n"
        "   Install the engine (editable): uv pip install -e ~/Projects/handoff-fanout\n"
        "   or set HANDOFF_ENGINE_PYTHON=/abs/path/to/py311+/python.\n"
    )
    sys.exit(1)

# Replace this process with the engine's dump module. Use ``-m handoff_fanout.dump``
# (the dump module's own __main__ → full ``handoff-dump`` argparse) NOT
# ``-m handoff_fanout dump`` — the latter routes through cli.py whose empty ``dump``
# subparser swallows ``--help`` (would show only ``-h``). Direct module = full args +
# correct ``--help``. On a missing module the target python emits a clear
# ``No module named handoff_fanout``; we keep the launcher thin (no pre-spawn probe).
try:
    os.execv(_engine_py, [_engine_py, "-m", "handoff_fanout.dump", *sys.argv[1:]])
except OSError as _e:  # pragma: no cover — exec almost never returns
    sys.stderr.write(f"❌ failed to exec handoff engine ({_engine_py}): {_e}\n")
    sys.exit(1)
