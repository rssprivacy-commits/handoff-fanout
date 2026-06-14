"""focusjump-fix S4 — ``handoff gc-singlepane`` janitor (hygiene, NON-correctness path).

The safety invariants under test (codex/deepseek highest-risk item = never clean a LIVE coordinator):
  * not-live + (aged OR .done) → candidate;
  * LIVE (window title carries the task) → never a candidate;
  * liveness UNKNOWN (probe ``None``) → never a candidate + ``--execute`` aborts (fail-safe);
  * ``--protect`` excludes; young + not-done → kept (retention buffer);
  * the substring trap (``sw-coord-p1`` vs ``…p12…``) is avoided by token-boundary matching;
  * ``--execute`` QUARANTINES (reversible move), never unlinks.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from handoff_fanout import gc_singlepane as gc


def _make_sidecar(home: Path, project: str, task: str, *, role="supervisor_succession",
                  done=False, age_days=5.0, with_ws=True) -> Path:
    queue = home / project / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    ws_path = None
    if with_ws:
        spdir = home / project / "singlepane"
        spdir.mkdir(parents=True, exist_ok=True)
        ws_path = spdir / f"{task}.handoff.code-workspace"
        ws_path.write_text("{}")
    sp = queue / f"{task}.singlepane"
    sp.write_text(json.dumps({"workspace": str(ws_path) if ws_path else "", "role": role}))
    if done:
        (queue / f"{task}.done").touch()
    # backdate the sidecar mtime so age tests are deterministic
    old = time.time() - age_days * 86400.0
    import os
    os.utime(sp, (old, old))
    return sp


def _tasks(records):
    return sorted(r["task"] for r in records)


# ─── _task_is_live: token-boundary matching (substring-trap guard) ──────────────


def test_task_is_live_boundary_not_substring():
    titles = ["handoff-fanout·sw-coord-p12·supervisor·deadbeef [singlepane]"]
    assert gc._task_is_live("sw-coord-p12", titles) is True
    assert gc._task_is_live("sw-coord-p1", titles) is False  # substring must NOT match


def test_task_is_live_coordinator_red_top_title():
    titles = ["🧭中枢·sw-coord-p26 — handoff.md"]
    assert gc._task_is_live("sw-coord-p26", titles) is True
    assert gc._task_is_live("sw-coord-p2", titles) is False


# ─── find_gc_candidates eligibility matrix (pure, injected liveness) ────────────


def test_dead_aged_notlive_is_candidate(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert _tasks(recs) == ["coord-12"]
    assert recs[0]["workspace"] is not None  # the ws file is bundled for quarantine


def test_live_coordinator_never_candidate(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-26", age_days=5.0)
    titles = ["proj·coord-26·supervisor_succession·abcdef [singlepane]"]
    recs = gc.find_gc_candidates(tmp_path, live_titles=titles, retention_days=1.0)
    assert recs == [], "a live coordinator must never be quarantined"


def test_liveness_unknown_fail_safe_no_candidates(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)
    recs = gc.find_gc_candidates(tmp_path, live_titles=None, retention_days=1.0)
    assert recs == [], "liveness unknown (None) → fail-safe, clean nothing"


def test_protect_excludes(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-26", age_days=5.0)
    recs = gc.find_gc_candidates(
        tmp_path, live_titles=[], retention_days=1.0, protect={"coord-26"}
    )
    assert recs == []


def test_young_not_done_kept_by_retention(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-25", age_days=0.2)  # younger than retention
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert recs == [], "young + not-done + not-live → kept (conservative buffer)"


def test_done_marker_candidate_regardless_of_age(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-7", age_days=0.01, done=True)
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert _tasks(recs) == ["coord-7"], ".done = terminal → eligible even if young (but still not-live)"


def test_non_supervisor_sidecar_ignored(tmp_path):
    _make_sidecar(tmp_path, "proj", "wkr-1", role="worker", age_days=5.0)
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert recs == []


def test_mixed_live_and_dead_only_dead_chosen(tmp_path):
    _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)  # dead
    _make_sidecar(tmp_path, "proj", "coord-26", age_days=0.5)  # live below
    titles = ["proj·coord-26·supervisor_succession·beef [singlepane]"]
    recs = gc.find_gc_candidates(tmp_path, live_titles=titles, retention_days=1.0)
    assert _tasks(recs) == ["coord-12"]


# ─── _quarantine: reversible move preserving relative path ──────────────────────


def test_quarantine_moves_reversibly(tmp_path):
    sp = _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)
    dest = gc._quarantine(tmp_path, sp, "gc-STAMP")
    assert not sp.exists(), "source moved out of queue/"
    assert dest.exists()
    assert dest == tmp_path / "_gc_quarantine" / "gc-STAMP" / "proj" / "queue" / "coord-12.singlepane"


# ─── main(): dry-run vs execute + fail-safe abort ───────────────────────────────


def test_main_dry_run_moves_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path))
    sp = _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)
    monkeypatch.setattr(gc, "_live_code_window_titles", lambda: [])

    rc = gc.main(["--project", "proj"])

    assert rc == 0
    assert sp.exists(), "dry-run must not move anything"
    out = capsys.readouterr().out
    assert "would quarantine" in out and "dry-run" in out


def test_main_execute_quarantines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path))
    sp = _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)
    ws = tmp_path / "proj" / "singlepane" / "coord-12.handoff.code-workspace"
    monkeypatch.setattr(gc, "_live_code_window_titles", lambda: [])

    rc = gc.main(["--project", "proj", "--execute"])

    assert rc == 0
    assert not sp.exists() and not ws.exists(), "execute quarantines both files"
    quar = tmp_path / "_gc_quarantine"
    assert quar.exists() and any(quar.rglob("coord-12.singlepane"))


def test_main_execute_aborts_when_liveness_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path))
    sp = _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0)
    monkeypatch.setattr(gc, "_live_code_window_titles", lambda: None)  # probe failed

    rc = gc.main(["--project", "proj", "--execute"])

    assert rc == 1, "execute must abort when liveness can't be proven"
    assert sp.exists(), "nothing moved on fail-safe abort"
    assert "ABORTED" in capsys.readouterr().err


# ─── P1-2 (codex re-audit): a foreign 'workspace' path must never be quarantined ────


def _make_polluted_sidecar(home: Path, project: str, task: str, ws_target: Path,
                           *, age_days=5.0) -> Path:
    """A stale supervisor sidecar whose ``workspace`` points OUTSIDE the canonical
    ``<home>/<project>/singlepane/<task>.handoff.code-workspace`` location."""
    queue = home / project / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    sp = queue / f"{task}.singlepane"
    sp.write_text(json.dumps({"workspace": str(ws_target), "role": "supervisor_succession"}))
    import os
    old = time.time() - age_days * 86400.0
    os.utime(sp, (old, old))
    return sp


def test_foreign_workspace_path_not_bundled(tmp_path, capsys):
    """A corrupted/polluted sidecar pointing ``workspace`` at an arbitrary existing file must NOT
    bundle that foreign file for quarantine — only the sidecar itself is eligible."""
    foreign = tmp_path / "victim.txt"
    foreign.write_text("do not move me")
    _make_polluted_sidecar(tmp_path, "proj", "coord-12", foreign, age_days=5.0)

    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)

    assert _tasks(recs) == ["coord-12"], "the sidecar itself is still eligible"
    assert recs[0]["workspace"] is None, "foreign workspace path must NOT be bundled for quarantine"
    assert foreign.exists(), "find_gc_candidates is read-only — foreign file untouched"
    assert "outside the canonical" in capsys.readouterr().err, "out-of-range path must WARN (no silent downgrade)"


def test_main_execute_never_moves_foreign_workspace(tmp_path, monkeypatch):
    """End-to-end: ``--execute`` quarantines the sidecar but NEVER the foreign file it points at."""
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path))
    foreign = tmp_path / "victim.txt"
    foreign.write_text("do not move me")
    sp = _make_polluted_sidecar(tmp_path, "proj", "coord-12", foreign, age_days=5.0)
    monkeypatch.setattr(gc, "_live_code_window_titles", lambda: [])

    rc = gc.main(["--project", "proj", "--execute"])

    assert rc == 0
    assert not sp.exists(), "the sidecar is quarantined"
    assert foreign.exists(), "the foreign file is NEVER moved (GC blast radius bounded to canonical path)"


def test_canonical_workspace_still_bundled(tmp_path):
    """Regression guard: a legitimate canonical workspace path is still bundled (fix didn't over-reject)."""
    _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0, with_ws=True)
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert recs[0]["workspace"] is not None, "the canonical workspace file is still eligible for quarantine"


def test_off_canonical_nonexistent_workspace_warns(tmp_path, capsys):
    """codex re-audit polish: an off-canonical workspace path that does NOT exist must still WARN
    (禁静默降级), while a canonical-but-already-deleted ws stays quiet (normal cleanup)."""
    ghost = tmp_path / "elsewhere" / "ghost.code-workspace"  # off-canonical, never created
    _make_polluted_sidecar(tmp_path, "proj", "coord-12", ghost, age_days=5.0)
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert recs[0]["workspace"] is None
    assert "outside the canonical" in capsys.readouterr().err


def test_canonical_deleted_workspace_silent(tmp_path, capsys):
    """A canonical workspace whose file was already deleted is normal cleanup → no WARN noise."""
    _make_sidecar(tmp_path, "proj", "coord-12", age_days=5.0, with_ws=True)
    # delete just the workspace file, keep the sidecar pointing at the canonical (now-missing) path
    (tmp_path / "proj" / "singlepane" / "coord-12.handoff.code-workspace").unlink()
    recs = gc.find_gc_candidates(tmp_path, live_titles=[], retention_days=1.0)
    assert recs[0]["workspace"] is None
    assert "outside the canonical" not in capsys.readouterr().err, "canonical-but-deleted must NOT warn"
