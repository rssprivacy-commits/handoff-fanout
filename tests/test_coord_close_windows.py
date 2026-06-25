"""req3 B — coord-close-windows.py enhancements: close-by-WID (B1) + the §6(b) stats
delimiter fix (B2), with all existing safety invariants preserved (B3).

The tool lives at ``~/.claude-handoff/supervisor-monitor/coord-close-windows.py`` (a
non-git runtime tool, deploy-audited separately). These tests load it by path and mock
the GUI side (winlist / osascript / goto) so nothing real ever closes. If the tool is not
deployed on this machine the module is skipped (environmental, like the DX_SPAWN_SH tests).
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

CCW_PATH = Path.home() / ".claude-handoff" / "supervisor-monitor" / "coord-close-windows.py"
if not CCW_PATH.exists():
    pytest.skip("coord-close-windows.py not deployed on this machine", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("coord_close_windows", CCW_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ccw = _load()


def _proc(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _win(title, wid, desktop=1):
    return {"title": title, "window_number": wid, "desktop": desktop}


WORKER = "handoff-fanout · req3-autoclose-engine · worker · 184f6d9d2b3830af [worktree] — 审计"
COORD = "🧭中枢·handoff-fanout · sw-coord-p69 · supervisor_succession · 336a45e5ac3a777c [singlepane] — x"
AI_TITLED = "审计交接义务工作文件 — .handoff (Workspace)"


# ─── B3: existing identity parse still works ─────────────────────────────────


def test_parse_title_structured_worker():
    proj, tid, is_coord, nonce = ccw.parse_title(WORKER)
    assert proj == "handoff-fanout"
    assert tid == "req3-autoclose-engine"
    assert is_coord is False
    assert nonce == "184f6d9d2b3830af"


def test_parse_title_coordinator_flagged():
    _proj, _tid, is_coord, _nonce = ccw.parse_title(COORD)
    assert is_coord is True


# ─── B2: the stats delimiter fix ─────────────────────────────────────────────


def test_b2_close_osa_has_delimiter_fix():
    # The §6(b) bug was a bare `list as string` (no comma) → undercount. The fix sets the
    # AppleScript text-item delimiter to "," before the join.
    assert 'text item delimiters to ","' in ccw.CLOSE_OSA


def test_b2_close_by_tokens_parses_multiple(monkeypatch):
    monkeypatch.setattr(ccw, "_run", lambda *a, **k: _proc("CLOSED=aaa,bbb||BLOCKED=ccc"))
    closed, blocked = ccw.close_by_tokens(["aaa", "bbb", "ccc"])
    assert closed == ["aaa", "bbb"]  # both counted (was 1 with the bug)
    assert blocked == ["ccc"]


# ─── B1: close-by-WID ────────────────────────────────────────────────────────


def test_close_by_wid_osa_uses_exact_equality_and_indices():
    # WID close matches by EXACT equality (not `contains`) and returns ARGV indices.
    assert "is equal to (item idx of argv)" in ccw.CLOSE_BY_WID_OSA
    assert 'text item delimiters to ","' in ccw.CLOSE_BY_WID_OSA


def test_close_by_titles_maps_indices_to_titles(monkeypatch):
    monkeypatch.setattr(ccw, "_run", lambda *a, **k: _proc("CLOSED=1,3||BLOCKED=2"))
    closed, blocked = ccw.close_by_titles(["t1", "t2", "t3"])
    assert closed == ["t1", "t3"]
    assert blocked == ["t2"]


def test_close_by_titles_ignores_out_of_range_index(monkeypatch):
    monkeypatch.setattr(ccw, "_run", lambda *a, **k: _proc("CLOSED=1,9||BLOCKED="))
    closed, blocked = ccw.close_by_titles(["only"])
    assert closed == ["only"]
    assert blocked == []


def test_classify_wids_target_protected_skipped():
    windows = [
        _win(WORKER, 100, desktop=5),
        _win(COORD, 200, desktop=4),
        _win(AI_TITLED, 300, desktop=5),  # unique AI title → closable target
        _win("dup", 301), _win("dup", 302),  # non-unique titles → both skipped
    ]
    targets, protected, skipped = ccw.classify_wids(windows, [100, 200, 300, 301, 999])
    target_wids = {wid for _w, wid in targets}
    assert target_wids == {100, 300}  # worker + unique AI-titled
    assert [wid for _w, wid in protected] == [200]  # coordinator hard-protected
    skipped_map = dict(skipped)
    assert skipped_map[301] == "title-empty-or-not-unique"
    assert skipped_map[999] == "wid-not-found"


def test_classify_wids_never_targets_coordinator():
    windows = [_win(COORD, 200)]
    targets, protected, _skipped = ccw.classify_wids(windows, [200])
    assert targets == []
    assert [wid for _w, wid in protected] == [200]


def test_close_wids_active_space_maps_back(monkeypatch):
    windows = [_win(WORKER, 100), _win(AI_TITLED, 300)]
    monkeypatch.setattr(ccw, "probe_windows", lambda: windows)
    # both titles "close" successfully
    monkeypatch.setattr(ccw, "close_by_titles", lambda titles: (list(titles), []))
    closed, blocked, skipped = ccw.close_wids_active_space([100, 300])
    assert set(closed) == {100, 300}
    assert blocked == []
    assert skipped == []


# ─── B1: full flow (dry-run vs execute) — NOTHING real closes (all mocked) ───


def test_run_close_wid_dryrun_never_closes(monkeypatch, capsys):
    windows = [_win(WORKER, 100, desktop=5)]
    monkeypatch.setattr(ccw, "probe_windows", lambda: windows)
    monkeypatch.setattr(ccw, "detect_active_desktop", lambda w: 1)
    called = {"goto": 0, "close": 0}

    def _goto(n):
        called["goto"] += 1
        return True

    def _close(_t):
        called["close"] += 1
        return ([], [])

    monkeypatch.setattr(ccw, "goto", _goto)
    monkeypatch.setattr(ccw, "close_by_titles", _close)
    ccw._run_close_wid("handoff-fanout", [100], execute=False)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert called["goto"] == 0  # dry-run touches no desktop
    assert called["close"] == 0


def test_run_close_wid_execute_closes_and_restores(monkeypatch, capsys):
    windows = [_win(WORKER, 100, desktop=5)]
    monkeypatch.setattr(ccw, "probe_windows", lambda: windows)
    monkeypatch.setattr(ccw, "detect_active_desktop", lambda w: 9)
    gotos = []
    monkeypatch.setattr(ccw, "goto", lambda n: (gotos.append(n), True)[1])
    monkeypatch.setattr(ccw, "close_by_titles", lambda titles: (list(titles), []))
    ccw._run_close_wid("handoff-fanout", [100], execute=True)
    out = capsys.readouterr().out
    assert "closed wid 100" in out
    assert 5 in gotos  # went to the worker desktop
    assert gotos[-1] == 9  # restored the owner's active desktop last
    assert "RESULT: closed 1" in out


def test_run_close_wid_execute_failed_goto_skips(monkeypatch, capsys):
    windows = [_win(WORKER, 100, desktop=5)]
    monkeypatch.setattr(ccw, "probe_windows", lambda: windows)
    monkeypatch.setattr(ccw, "detect_active_desktop", lambda w: 9)
    closed_called = {"n": 0}

    def _close(_t):
        closed_called["n"] += 1
        return (_t, [])

    monkeypatch.setattr(ccw, "goto", lambda n: n == 9)  # only restore-goto succeeds
    monkeypatch.setattr(ccw, "close_by_titles", _close)
    ccw._run_close_wid("handoff-fanout", [100], execute=True)
    out = capsys.readouterr().out
    assert "goto desktop 5 FAILED" in out
    assert closed_called["n"] == 0  # fail-closed: never closed on a failed Space switch


def test_run_close_wid_coordinator_never_closes(monkeypatch, capsys):
    windows = [_win(COORD, 200, desktop=4)]
    monkeypatch.setattr(ccw, "probe_windows", lambda: windows)
    monkeypatch.setattr(ccw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(ccw, "goto", lambda n: True)
    monkeypatch.setattr(ccw, "close_by_titles", lambda t: (list(t), []))
    ccw._run_close_wid("handoff-fanout", [200], execute=True)
    out = capsys.readouterr().out
    assert "PROTECTED" in out
    assert "No closable target" in out
