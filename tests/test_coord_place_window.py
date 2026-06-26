"""req2 — coord-place-window.py: the placement twin of coord-close-windows.py.

The tool lives at ``~/.claude-handoff/supervisor-monitor/coord-place-window.py`` once the
coordinator has deploy-audited it (build worker writes it to ``staging-place/`` first).
These tests load it by path — preferring the deployed location, falling back to staging so
the build worker can self-verify before deploy — and mock the GUI side (winlist / osascript /
goto / Rectangle) so nothing real ever moves. If the tool is on neither path the module is
skipped (environmental, like the DX_SPAWN_SH / coord-close-windows tests).

Coverage:
  • pure helpers — slot mapping, slot resolution, identity (find/unique/self), bounds delta
  • parse_title — the shared structured-title parser (fresh copy must still parse correctly)
  • osacompile — EVERY AppleScript constant must COMPILE (the close-windows p70 bug was an
    actuator that never compiled, slipping through string-match-only tests)
  • flows — poll_resolve (one/ambiguous/zero), run_place (dry-run / execute / fail-closed goto
    / RED LINE #4 refuse), run_self (refuse non-coord frontmost / fire on coord) — all mocked
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_CANDIDATES = [
    Path.home() / ".claude-handoff" / "supervisor-monitor" / "coord-place-window.py",  # deployed
    Path.home() / ".claude-handoff" / "staging-place" / "coord-place-window.py",       # pre-deploy
]
CPW_PATH = next((p for p in _CANDIDATES if p.exists()), None)
if CPW_PATH is None:
    pytest.skip("coord-place-window.py not deployed/staged on this machine", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("coord_place_window", CPW_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cpw = _load()


def _win(title, wid, desktop=1):
    return {"title": title, "window_number": wid, "desktop": desktop}


WORKER = "handoff-fanout · req3-autoclose-engine · worker · 184f6d9d2b3830af [worktree] — 审计"
COORD = "🧭中枢·handoff-fanout · sw-coord-p69 · supervisor_succession · 336a45e5ac3a777c [singlepane] — x"


# ── shared parser (fresh copy must behave like close-windows') ───────────────


def test_parse_title_structured_worker():
    proj, tid, is_coord, nonce = cpw.parse_title(WORKER)
    assert proj == "handoff-fanout"
    assert tid == "req3-autoclose-engine"
    assert is_coord is False
    assert nonce == "184f6d9d2b3830af"


def test_parse_title_coordinator_flagged():
    proj, _tid, is_coord, _nonce = cpw.parse_title(COORD)
    assert proj == "handoff-fanout"
    assert is_coord is True


# ── pure helpers: slot mapping + resolution ──────────────────────────────────


def test_slot_for_role():
    assert cpw.slot_for_role("coord", None) == "right-half"
    assert cpw.slot_for_role("worker", 0) == "top-left"     # even
    assert cpw.slot_for_role("worker", 1) == "bottom-left"  # odd
    assert cpw.slot_for_role("worker", 2) == "top-left"     # alternating
    with pytest.raises(ValueError):
        cpw.slot_for_role("worker", None)   # worker needs an index
    with pytest.raises(ValueError):
        cpw.slot_for_role("worker", -1)     # index must be >= 0
    with pytest.raises(ValueError):
        cpw.slot_for_role("bogus", 0)


def test_resolve_slot():
    assert cpw.resolve_slot("top-left", None, None) == "top-left"   # explicit wins
    assert cpw.resolve_slot(None, "coord", None) == "right-half"
    assert cpw.resolve_slot(None, "worker", 0) == "top-left"
    assert cpw.resolve_slot(None, "worker", 1) == "bottom-left"
    assert cpw.resolve_slot(None, None, None) is None              # caller defaults/errors
    with pytest.raises(ValueError):
        cpw.resolve_slot("top-left", "coord", None)   # mutually exclusive
    with pytest.raises(ValueError):
        cpw.resolve_slot("maximize", None, None)      # not in VALID_SLOTS


def test_valid_slots_whitelist():
    assert cpw.VALID_SLOTS == ("right-half", "top-left", "bottom-left")


# ── pure helpers: identity ───────────────────────────────────────────────────


def test_find_by_task_exact_fields():
    ws = [_win(WORKER, 100), _win(COORD, 200),
          _win("handoff-fanout · req3-autoclose-engine-extra · worker · " + "a" * 16 + " [x] — y", 300)]
    hits = cpw.find_by_task(ws, "handoff-fanout", "req3-autoclose-engine")
    assert [w["window_number"] for w in hits] == [100]  # not the -extra suffix (EXACT field, not substring)
    assert cpw.find_by_task(ws, "erp-system", "req3-autoclose-engine") == []  # wrong project


def test_find_by_wid():
    ws = [_win(WORKER, 100), _win(COORD, 200)]
    assert [w["window_number"] for w in cpw.find_by_wid(ws, 200)] == [200]
    assert cpw.find_by_wid(ws, 999) == []


def test_title_unique():
    ws = [_win(WORKER, 100), _win("dup", 300), _win("dup", 301)]
    assert cpw.title_unique(ws, WORKER) is True
    assert cpw.title_unique(ws, "dup") is False   # two windows share it → ambiguous
    assert cpw.title_unique(ws, "") is False       # empty never unique


def test_is_self_placeable():
    assert cpw.is_self_placeable(COORD, "handoff-fanout") is True
    assert cpw.is_self_placeable(WORKER, "handoff-fanout") is False        # not a coordinator
    assert cpw.is_self_placeable(COORD, "erp-system") is False             # another chain (RED LINE #4)
    assert cpw.is_self_placeable("plain title — no fields", "handoff-fanout") is False


# ── pure helpers: bounds delta (honest success reporting) ────────────────────


def test_parse_bounds():
    assert cpw.parse_bounds("1028,39,1028,1290") == (1028, 39, 1028, 1290)
    assert cpw.parse_bounds("NOTFOUND") is None
    assert cpw.parse_bounds("") is None
    assert cpw.parse_bounds(None) is None
    assert cpw.parse_bounds("1,2,3") is None   # wrong arity


def test_bounds_changed():
    assert cpw.bounds_changed("0,0,100,100", "1,0,100,100") is True
    assert cpw.bounds_changed("0,0,100,100", "0,0,100,100") is False
    assert cpw.bounds_changed(None, "0,0,100,100") is None      # unreadable → can't verify
    assert cpw.bounds_changed("NOTFOUND", "0,0,100,100") is None


# ── osacompile: EVERY AppleScript actuator must COMPILE (close-windows p70 bug class) ──


@pytest.mark.parametrize("const_name", ["RAISE_OSA", "BOUNDS_BY_TITLE_OSA", "SELF_TITLE_OSA", "BOUNDS_FRONT_OSA"])
def test_applescript_constant_compiles(const_name):
    osacompile = shutil.which("osacompile")
    if osacompile is None:
        pytest.skip("osacompile unavailable (non-mac CI) — syntax check skipped")
    source = getattr(cpw, const_name)
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"{const_name}.applescript"
        src.write_text(source)
        out = Path(d) / f"{const_name}.scpt"
        r = subprocess.run([osacompile, "-o", str(out), str(src)], capture_output=True, text=True)
    assert r.returncode == 0, f"{const_name} failed to compile: {r.stderr.strip()}"


# ── flow: poll_resolve ───────────────────────────────────────────────────────


def test_poll_resolve_exactly_one(monkeypatch):
    monkeypatch.setattr(cpw, "probe_windows", lambda: [_win(WORKER, 100)])
    w, _ws, err = cpw.poll_resolve("handoff-fanout", "req3-autoclose-engine", None, 0.0)
    assert err is None
    assert w["window_number"] == 100


def test_poll_resolve_ambiguous_fails_fast(monkeypatch):
    monkeypatch.setattr(cpw, "probe_windows", lambda: [_win(WORKER, 100), _win(WORKER, 101)])
    w, _ws, err = cpw.poll_resolve("handoff-fanout", "req3-autoclose-engine", None, 0.0)
    assert w is None
    assert "ambiguous" in err


def test_poll_resolve_zero_after_deadline(monkeypatch):
    monkeypatch.setattr(cpw, "probe_windows", lambda: [])
    w, _ws, err = cpw.poll_resolve("handoff-fanout", "no-such-task", None, 0.0)
    assert w is None
    assert "no window matches" in err


def test_poll_resolve_by_wid(monkeypatch):
    monkeypatch.setattr(cpw, "probe_windows", lambda: [_win(WORKER, 100)])
    w, _ws, err = cpw.poll_resolve("handoff-fanout", None, 100, 0.0)
    assert err is None and w["window_number"] == 100


# ── flow: run_place (dry-run / execute / fail-closed / RED LINE #4) — all mocked ──


def test_run_place_dryrun_never_acts(monkeypatch, capsys):
    win = _win("handoff-fanout · sw-foo · worker · " + "b" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(cpw, "probe_windows", lambda: [win])
    monkeypatch.setattr(cpw, "detect_active_desktop", lambda w: 9)
    acted = {"goto": 0, "fire": 0, "rect": 0}
    monkeypatch.setattr(cpw, "goto", lambda n: (acted.__setitem__("goto", acted["goto"] + 1), True)[1])
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: acted.__setitem__("fire", acted["fire"] + 1))
    monkeypatch.setattr(cpw, "rectangle_running", lambda: (acted.__setitem__("rect", acted["rect"] + 1), True)[1])
    cpw.run_place("handoff-fanout", "sw-foo", None, "top-left", 0.0, execute=False)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert acted == {"goto": 0, "fire": 0, "rect": 0}  # dry-run touches nothing (not even Rectangle preflight)


def test_run_place_execute_gotos_raises_fires_restores(monkeypatch):
    win = _win("handoff-fanout · sw-foo · worker · " + "c" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(cpw, "probe_windows", lambda: [win])
    monkeypatch.setattr(cpw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    gotos = []
    monkeypatch.setattr(cpw, "goto", lambda n: (gotos.append(n), True)[1])
    raised = []
    monkeypatch.setattr(cpw, "raise_window", lambda t: raised.append(t))
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    bounds = iter(["0,0,100,100", "1028,39,1028,1290"])
    monkeypatch.setattr(cpw, "capture_bounds_by_title", lambda t: next(bounds))
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    cpw.run_place("handoff-fanout", "sw-foo", None, "top-left", 0.0, execute=True)
    assert 5 in gotos       # switched to the target's desktop
    assert gotos[-1] == 9   # restored the owner's active desktop LAST
    assert raised and raised[0] == win["title"]
    assert fired == ["top-left"]


def test_run_place_failed_goto_never_fires(monkeypatch):
    win = _win("handoff-fanout · sw-foo · worker · " + "d" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(cpw, "probe_windows", lambda: [win])
    monkeypatch.setattr(cpw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    monkeypatch.setattr(cpw, "goto", lambda n: False)   # Space switch fails
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    with pytest.raises(SystemExit):
        cpw.run_place("handoff-fanout", "sw-foo", None, "top-left", 0.0, execute=True)
    assert fired == []   # fail-closed: never fired on the wrong Space


def test_run_place_refuses_other_chain(monkeypatch):
    # RED LINE #4: a window of another project, resolved by --wid, is HARD REFUSED.
    win = _win("erp-system · sw-foo · worker · " + "e" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(cpw, "probe_windows", lambda: [win])
    monkeypatch.setattr(cpw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    with pytest.raises(SystemExit):
        cpw.run_place("handoff-fanout", None, 100, "top-left", 0.0, execute=True)
    assert fired == []


def test_run_place_non_unique_title_fail_closed(monkeypatch):
    # Post-goto the stable WID maps to a title shared by another window → fail-closed, no fire.
    win = _win("dupe-title", 100, desktop=9)   # desktop == active → no goto, but title non-unique
    other = _win("dupe-title", 101, desktop=9)

    # poll_resolve resolves by WID (unique here); the uniqueness gate trips on the fresh re-probe.
    monkeypatch.setattr(cpw, "probe_windows", lambda: [win, other])
    monkeypatch.setattr(cpw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    monkeypatch.setattr(cpw, "parse_title", lambda t: ("handoff-fanout", "sw-foo", False, None))  # pass project gate
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    with pytest.raises(SystemExit):
        cpw.run_place("handoff-fanout", None, 100, "top-left", 0.0, execute=True)
    assert fired == []


# ── flow: run_self (frontmost; refuse non-coord) — mocked ────────────────────


def test_run_self_refuses_non_coord_frontmost(monkeypatch):
    monkeypatch.setattr(cpw, "read_frontmost_title", lambda: WORKER)  # a worker window, not 🧭
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    with pytest.raises(SystemExit):
        cpw.run_self("handoff-fanout", "right-half", execute=True)
    assert fired == []


def test_run_self_refuses_other_chain_coord(monkeypatch):
    monkeypatch.setattr(cpw, "read_frontmost_title", lambda: COORD)  # 🧭 but handoff-fanout
    with pytest.raises(SystemExit):
        cpw.run_self("erp-system", "right-half", execute=True)       # asked for a different project


def test_run_self_coord_fires(monkeypatch):
    monkeypatch.setattr(cpw, "read_frontmost_title", lambda: COORD)
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    bounds = iter(["0,0,100,100", "1028,39,1028,1290"])
    monkeypatch.setattr(cpw, "capture_front_bounds", lambda: next(bounds))
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    cpw.run_self("handoff-fanout", "right-half", execute=True)
    assert fired == ["right-half"]


def test_run_self_dryrun_never_fires(monkeypatch, capsys):
    monkeypatch.setattr(cpw, "read_frontmost_title", lambda: COORD)
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    cpw.run_self("handoff-fanout", "right-half", execute=False)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert fired == []
