"""v5.4 mandate gate: terminal-status exemption + project-scoped roll-out + --no-dedupe.

Added 2026-06-01 (reconcile-handoff-cli-v5.4). The global dump entry was rewired to
re-exec the engine so HANDOFF_RETRO_MANDATE / HANDOFF_AUDIT_MANDATE finally take effect
for the auto-continue self-propagation chain. Two safety refinements come with that:

  1. NARROW terminal exemption — a ``--status done`` / ``--status blocked`` closure with
     NO ``--retro-evidence`` is not gated (no successor task ⇒ retro/audit semantics don't
     apply; same rationale as the pre-existing batch_done/batch_blocked exemption). But a
     terminal dump that DOES supply ``--retro-evidence`` (``handoff audit-close --status
     done``) is still validated — the exemption must not silently weaken attested closures.

  2. Project-scoped roll-out (``mandate_projects`` config) — a shared config.json drives
     every project under one HANDOFF_HOME, so routing the global entry to the engine must
     not brick siblings whose handoff templates don't yet pass evidence. Only listed
     projects enforce the env mandate on a no-evidence dump; unlisted ones run legacy.

Gate VERDICT is tested via ``--dry-run`` (the retro gate runs BEFORE the dry-run early
return, so the exit code reflects the gate alone — no .uri/clipboard/notify side effects).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from handoff_fanout import dump

TASK = "tt-gate-task"
PROJECT = "ptest"
_OMIT = object()  # sentinel: omit the mandate_projects key entirely (enforce-everywhere)


def _git_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    return ws


def _home(tmp_path: Path, mandate_projects: object = _OMIT) -> Path:
    """A HANDOFF_HOME. ``mandate_projects`` omitted (``_OMIT`` sentinel) ⇒ no key
    (enforce-everywhere). Pass a list (incl. ``[]``) to write the key."""
    home = tmp_path / "handoff"
    home.mkdir()
    cfg: dict = {}
    if mandate_projects is not _OMIT:
        cfg["mandate_projects"] = mandate_projects
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return home


def _run(home: Path, ws: Path, monkeypatch, *, status="active", project=PROJECT,
         mandate=True, bypass=False, extra=None) -> int:
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    if bypass:
        monkeypatch.setenv("HANDOFF_RETRO_BYPASS", "1")
    else:
        monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    if mandate:
        monkeypatch.setenv("HANDOFF_RETRO_MANDATE", "1")
        monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    else:
        monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
        monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    argv = ["--task", TASK, "--next", "n", "--project", project,
            "--workspace", str(ws), "--status", status, "--dry-run"]
    return dump.main(argv + (extra or []))


# ── narrow terminal-status exemption ─────────────────────────────────────────
def test_active_no_evidence_is_gated_under_mandate(tmp_path, monkeypatch):
    ws, home = _git_ws(tmp_path), _home(tmp_path)
    assert _run(home, ws, monkeypatch, status="active") != 0   # active needs evidence


def test_done_no_evidence_is_exempt_under_mandate(tmp_path, monkeypatch):
    ws, home = _git_ws(tmp_path), _home(tmp_path)
    assert _run(home, ws, monkeypatch, status="done") == 0      # terminal, no successor


def test_blocked_no_evidence_is_exempt_under_mandate(tmp_path, monkeypatch):
    ws, home = _git_ws(tmp_path), _home(tmp_path)
    assert _run(home, ws, monkeypatch, status="blocked") == 0   # stuck session can report


def test_done_with_supplied_evidence_is_still_validated(tmp_path, monkeypatch):
    # NARROW: supplying --retro-evidence opts a terminal dump back INTO the gate.
    # A nonexistent evidence path fails validation → non-zero (exemption did NOT skip).
    ws, home = _git_ws(tmp_path), _home(tmp_path)
    bad = str(home / "does-not-exist.json")
    assert _run(home, ws, monkeypatch, status="done", extra=["--retro-evidence", bad]) != 0


# ── --no-dedupe deprecated no-op (backward-compat with old global callers) ────
def test_no_dedupe_accepted_as_noop(tmp_path, monkeypatch):
    # Old standalone global had --no-dedupe; engine didn't → routing would crash with
    # argparse SystemExit(2). It must now be accepted + ignored (exact task IDs).
    ws, home = _git_ws(tmp_path), _home(tmp_path)
    rc = _run(home, ws, monkeypatch, status="active", mandate=False, extra=["--no-dedupe"])
    assert rc == 0   # parses (no SystemExit 2) + legacy passes when mandate off


# ── project-scoped mandate roll-out ──────────────────────────────────────────
def test_unlisted_project_takes_legacy_path(tmp_path, monkeypatch):
    ws, home = _git_ws(tmp_path), _home(tmp_path, mandate_projects=["erp-system"])
    # project=ptest is NOT in the allowlist → no-evidence active dump runs legacy.
    assert _run(home, ws, monkeypatch, status="active", project="ptest") == 0


def test_listed_project_enforces_mandate(tmp_path, monkeypatch):
    ws, home = _git_ws(tmp_path), _home(tmp_path, mandate_projects=["ptest"])
    assert _run(home, ws, monkeypatch, status="active", project="ptest") != 0


def test_empty_allowlist_fails_closed_enforces_everywhere(tmp_path, monkeypatch):
    # FAIL-CLOSED (codex R2-P1): an EMPTY list must NOT silently disable the mandate —
    # an accidental empty (e.g. last project removed) would be a silent-non-enforcement
    # footgun. Empty ⇒ unconfigured ⇒ enforce everywhere.
    ws, home = _git_ws(tmp_path), _home(tmp_path, mandate_projects=[])
    assert _run(home, ws, monkeypatch, status="active", project="ptest") != 0


def test_typo_string_allowlist_fails_closed(tmp_path, monkeypatch):
    # A bare string (JSON typo: "mandate_projects": "erp-system") must NOT char-iterate
    # into ['e','r','p',...] and silently disable enforcement for erp-system. Non-list
    # ⇒ unconfigured ⇒ enforce everywhere.
    ws, home = _git_ws(tmp_path), _home(tmp_path, mandate_projects="erp-system")
    assert _run(home, ws, monkeypatch, status="active", project="erp-system") != 0


def test_absent_allowlist_enforces_everywhere(tmp_path, monkeypatch):
    # Key omitted entirely → mandate_projects_configured False → honor the global flip.
    ws, home = _git_ws(tmp_path), _home(tmp_path)
    assert _run(home, ws, monkeypatch, status="active", project="anything-goes") != 0


def test_explicit_evidence_always_runs_gate_even_unlisted(tmp_path, monkeypatch):
    # Supplying --retro-evidence opts IN regardless of the allowlist (never silently
    # ignored). A bad evidence path then fails validation → non-zero.
    ws, home = _git_ws(tmp_path), _home(tmp_path, mandate_projects=["erp-system"])
    bad = str(home / "nope.json")
    rc = _run(home, ws, monkeypatch, status="active", project="ptest",
              extra=["--retro-evidence", bad])
    assert rc != 0


def test_bypass_reaches_gate_even_for_unlisted_project(tmp_path, monkeypatch):
    # HANDOFF_RETRO_BYPASS must NOT be short-circuited by the mandate_projects skip
    # (codex R2-P1): a bypass has to reach the gate so its override.json validation +
    # bypass-debt recording run. With no override.json the bypass path fails → non-zero;
    # the key point is rc != 0 proves the gate RAN (didn't legacy-skip to 0).
    ws, home = _git_ws(tmp_path), _home(tmp_path, mandate_projects=["erp-system"])
    rc = _run(home, ws, monkeypatch, status="active", project="ptest", bypass=True)  # unlisted
    assert rc != 0


# ── config parsing ───────────────────────────────────────────────────────────
def test_config_parses_mandate_projects(tmp_path):
    from handoff_fanout import config as _config
    home = _home(tmp_path, mandate_projects=["a", "b"])
    cfg = _config.load(home)
    assert cfg.mandate_projects == ["a", "b"]
    assert cfg.mandate_projects_configured is True


def test_config_absent_mandate_projects_is_unconfigured(tmp_path):
    from handoff_fanout import config as _config
    home = _home(tmp_path)  # key omitted
    cfg = _config.load(home)
    assert cfg.mandate_projects == []
    assert cfg.mandate_projects_configured is False


def test_config_empty_list_is_unconfigured_fail_closed(tmp_path):
    from handoff_fanout import config as _config
    cfg = _config.load(_home(tmp_path, mandate_projects=[]))
    assert cfg.mandate_projects == []
    assert cfg.mandate_projects_configured is False   # empty ⇒ enforce everywhere


def test_config_string_typo_is_unconfigured_no_char_iteration(tmp_path):
    from handoff_fanout import config as _config
    cfg = _config.load(_home(tmp_path, mandate_projects="erp-system"))
    assert cfg.mandate_projects == []                 # NOT ['e','r','p',...]
    assert cfg.mandate_projects_configured is False   # non-list ⇒ enforce everywhere


def test_config_all_invalid_entries_is_unconfigured(tmp_path):
    from handoff_fanout import config as _config
    cfg = _config.load(_home(tmp_path, mandate_projects=["", None, 123]))
    assert cfg.mandate_projects == []
    assert cfg.mandate_projects_configured is False
