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
_EXISTING = [p for p in _CANDIDATES if p.exists()]
if not _EXISTING:
    pytest.skip("coord-place-window.py not deployed/staged on this machine", allow_module_level=True)


def _pick_path():
    """Deterministically prefer the LOCK-CAPABLE build: the first candidate whose SOURCE defines
    ``placement_lock`` (the final flock build). This way the suite always exercises the lock build
    when one is present, regardless of which copy (deployed vs staging) `next-existing` would pick.
    Fall back to the first existing candidate if none define it (an older pre-lock build)."""
    for p in _EXISTING:
        try:
            if "def placement_lock" in p.read_text():
                return p
        except OSError:
            continue
    return _EXISTING[0]


CPW_PATH = _pick_path()


def _load():
    spec = importlib.util.spec_from_file_location("coord_place_window", CPW_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cpw = _load()

# Holder for the per-test isolated lock path (set by the `_isolate_placement_lock` autouse fixture).
# A 1-element list so `_load_staging` (module function) can read the CURRENT value by reference and
# redirect freshly-loaded staging modules to the same tmp path.
_ACTIVE_LOCKFILE: "list[str | None]" = [None]


@pytest.fixture(autouse=True)
def _isolate_placement_lock(tmp_path, monkeypatch):
    """Never touch the real machine-wide ~/.claude-handoff/.place-window.lock during tests:
    redirect every loaded module's PLACE_LOCK_FILE to a per-test tmp file (so the real flock —
    and any concurrent live placement — is never contended) + keep the wait short. `cpw` is the
    module-level build (deployed copy now carries the real flock lock); staging modules loaded
    per-test via `_load_staging` pick up the same tmp path through `_ACTIVE_LOCKFILE`."""
    lockfile = str(tmp_path / "place-window.lock")
    _ACTIVE_LOCKFILE[0] = lockfile
    for _mod in {id(m): m for m in (cpw,) if m is not None}.values():
        if hasattr(_mod, "PLACE_LOCK_FILE"):
            monkeypatch.setattr(_mod, "PLACE_LOCK_FILE", lockfile, raising=False)
        if hasattr(_mod, "PLACE_LOCK_WAIT"):
            monkeypatch.setattr(_mod, "PLACE_LOCK_WAIT", 1.0, raising=False)
    yield
    _ACTIVE_LOCKFILE[0] = None


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


def test_frontmost_is(monkeypatch):
    # frontmost_is delegates to frontmost_window_title and demands EXACT equality.
    monkeypatch.setattr(cpw, "frontmost_window_title", lambda: COORD)
    assert cpw.frontmost_is(COORD) is True             # matching title → True
    assert cpw.frontmost_is(WORKER) is False           # different title → False
    monkeypatch.setattr(cpw, "frontmost_window_title", lambda: None)
    assert cpw.frontmost_is(COORD) is False            # None (another app frontmost) → never True


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


# EVERY module-level AppleScript constant (attr ending in _OSA) is enumerated DYNAMICALLY so
# any new actuator (e.g. FRONTMOST_OSA) is auto-covered without editing a hardcoded list.
_OSA_CONSTS = sorted(n for n in dir(cpw) if n.endswith("_OSA") and isinstance(getattr(cpw, n), str))


def test_osa_consts_enumerated():
    # Guard: the dynamic scan actually found the actuators (a typo'd suffix would silently skip).
    assert "FRONTMOST_OSA" in _OSA_CONSTS
    assert "RAISE_OSA" in _OSA_CONSTS


@pytest.mark.parametrize("const_name", _OSA_CONSTS)
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
    monkeypatch.setattr(cpw, "frontmost_is", lambda t: True)   # raise made the target the global frontmost
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


def test_run_place_active_unknown_fail_closed(monkeypatch):
    # Target is on a DIFFERENT desktop but the owner's active desktop is UNKNOWN (None) →
    # a cross-desktop goto could never be restored → fail-closed BEFORE any goto/fire.
    win = _win("handoff-fanout · sw-foo · worker · " + "f" * 16 + " [worktree] — x", 100, desktop=7)
    monkeypatch.setattr(cpw, "probe_windows", lambda: [win])
    monkeypatch.setattr(cpw, "detect_active_desktop", lambda w: None)   # active desktop undetectable
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    gotos = []
    monkeypatch.setattr(cpw, "goto", lambda n: (gotos.append(n), True)[1])
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    with pytest.raises(SystemExit):
        cpw.run_place("handoff-fanout", None, 100, "top-left", 0.0, execute=True)
    assert gotos == []   # never switched Space (can't restore it)
    assert fired == []   # fail-closed: nothing fired


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
    monkeypatch.setattr(cpw, "raise_window", lambda t: None)
    monkeypatch.setattr(cpw, "frontmost_is", lambda t: True)   # raise made it the global frontmost
    bounds = iter(["0,0,100,100", "1028,39,1028,1290"])
    monkeypatch.setattr(cpw, "capture_front_bounds", lambda: next(bounds))
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    cpw.run_self("handoff-fanout", "right-half", execute=True)
    assert fired == ["right-half"]


def test_run_self_fail_closed_when_not_frontmost_after_raise(monkeypatch):
    # The validated 🧭 window is correct, but another app/window holds focus after raise →
    # fail-closed, never fire Rectangle on the wrong (global frontmost) window.
    monkeypatch.setattr(cpw, "read_frontmost_title", lambda: COORD)   # valid coord title
    monkeypatch.setattr(cpw, "rectangle_running", lambda: True)
    monkeypatch.setattr(cpw, "raise_window", lambda t: None)
    monkeypatch.setattr(cpw, "frontmost_is", lambda t: False)         # raise did NOT win focus
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    monkeypatch.setattr(cpw.time, "sleep", lambda *a: None)
    with pytest.raises(SystemExit):
        cpw.run_self("handoff-fanout", "right-half", execute=True)
    assert fired == []   # fail-closed: never fired on the wrong frontmost window


def test_run_self_dryrun_never_fires(monkeypatch, capsys):
    monkeypatch.setattr(cpw, "read_frontmost_title", lambda: COORD)
    fired = []
    monkeypatch.setattr(cpw, "fire_rectangle", lambda s: fired.append(s))
    cpw.run_self("handoff-fanout", "right-half", execute=False)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert fired == []


# ── fleet placement lock (machine-wide active-Space mutex) ───────────────────
# IMPORTANT: the lock lives in the STAGING copy first; the deployed supervisor-monitor/
# copy is the OLD version until the coordinator re-deploys. _CANDIDATES above PREFERS the
# deployed copy, so `cpw` may be the pre-lock build. These tests therefore load the STAGING
# file EXPLICITLY (the SOT for the new code), independent of which copy `cpw` resolved to.
# Once the coordinator deploy-audits the staging file → supervisor-monitor/, both copies
# carry the lock and these tests would pass against either; until then they pin the new code.

_STAGING_CPW = Path.home() / ".claude-handoff" / "staging-place" / "coord-place-window.py"


def _load_staging():
    spec = importlib.util.spec_from_file_location("coord_place_window_staging", _STAGING_CPW)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # A staging module is loaded FRESH inside each test (after the autouse isolation fixture has
    # run), so it starts with the REAL PLACE_LOCK_FILE. Redirect it to the per-test isolated path
    # set by `_isolate_placement_lock` so a freshly-loaded `spw` never touches the real lock either.
    if _ACTIVE_LOCKFILE[0] is not None and hasattr(mod, "PLACE_LOCK_FILE"):
        setattr(mod, "PLACE_LOCK_FILE", _ACTIVE_LOCKFILE[0])
        if hasattr(mod, "PLACE_LOCK_WAIT"):
            setattr(mod, "PLACE_LOCK_WAIT", 1.0)
    return mod


_staging_skip = pytest.mark.skipif(
    not _STAGING_CPW.exists()
    or "placement_lock" not in _STAGING_CPW.read_text(),
    reason="staging coord-place-window.py with placement_lock not present",
)


@_staging_skip
def test_placement_lock_acquires_and_releases(tmp_path, monkeypatch):
    # flock-based: an anchor FILE persists; acquire/release is the flock state, not the file's
    # existence. Re-acquiring after the first `with` exits proves the lock was truly released.
    spw = _load_staging()
    lockfile = tmp_path / ".place-window.lock"
    monkeypatch.setattr(spw, "PLACE_LOCK_FILE", str(lockfile))
    with spw.placement_lock() as got:
        assert got is True
        assert lockfile.is_file()       # anchor file created
    assert lockfile.is_file()           # anchor file PERSISTS (not unlinked — that would break mutex)
    with spw.placement_lock() as got2:  # a second acquire succeeds → the first truly released
        assert got2 is True


@_staging_skip
def test_placement_lock_contention_skips(tmp_path, monkeypatch):
    # Hold the SAME anchor file's flock via an independent fd (a separate open-file-description),
    # so placement_lock contends and must fail-safe SKIP (yield False) within the short wait.
    import fcntl as _fcntl
    import os as _os
    spw = _load_staging()
    lockfile = tmp_path / ".place-window.lock"
    monkeypatch.setattr(spw, "PLACE_LOCK_FILE", str(lockfile))
    monkeypatch.setattr(spw, "PLACE_LOCK_WAIT", 0.5)    # tiny wait → fast fail-safe
    monkeypatch.setattr(spw.time, "sleep", lambda *a: None)
    # Contend on the EXACT path the code-under-test uses (read it back from the module, never a
    # hardcoded ~/.claude-handoff path) so the holder fd and placement_lock share the same file.
    held_path = spw.PLACE_LOCK_FILE
    holder_fd = _os.open(held_path, _os.O_CREAT | _os.O_RDWR, 0o644)
    _fcntl.flock(holder_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)   # we hold it exclusively
    try:
        with spw.placement_lock() as got:
            assert got is False         # contention → could not acquire → caller must SKIP
    finally:
        _fcntl.flock(holder_fd, _fcntl.LOCK_UN)
        _os.close(holder_fd)
    # After we release, placement_lock can acquire cleanly (proves it never stole/broke our lock).
    with spw.placement_lock() as got2:
        assert got2 is True


@_staging_skip
def test_placement_lock_recovers_leftover_directory(tmp_path, monkeypatch):
    # Defensive path: an earlier mkdir-based build may have left a DIRECTORY at the lock path;
    # the flock lock removes an empty stale dir then opens the anchor file and acquires.
    spw = _load_staging()
    lockfile = tmp_path / ".place-window.lock"
    monkeypatch.setattr(spw, "PLACE_LOCK_FILE", str(lockfile))
    lockfile.mkdir()                    # leftover directory from the old scheme
    assert lockfile.is_dir()
    with spw.placement_lock() as got:
        assert got is True              # recovered: dir removed, file anchor opened, flock held
        assert lockfile.is_file()


@_staging_skip
def test_run_place_skips_when_lock_unavailable(monkeypatch):
    # placement_lock yields False (could not acquire) → run_place SKIPs (SystemExit) and NEVER
    # fires Rectangle — that is the whole point: never place without the lock (= the race).
    spw = _load_staging()
    import contextlib as _cl
    win = _win("handoff-fanout · sw-foo · worker · " + "a" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(spw, "probe_windows", lambda: [win])
    monkeypatch.setattr(spw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(spw, "rectangle_running", lambda: True)
    fired = []
    monkeypatch.setattr(spw, "fire_rectangle", lambda s: fired.append(s))
    gotos = []
    monkeypatch.setattr(spw, "goto", lambda n: (gotos.append(n), True)[1])

    @_cl.contextmanager
    def _no_lock():
        yield False                     # never acquired
    monkeypatch.setattr(spw, "placement_lock", _no_lock)
    monkeypatch.setattr(spw.time, "sleep", lambda *a: None)
    with pytest.raises(SystemExit):
        spw.run_place("handoff-fanout", "sw-foo", None, "top-left", 0.0, execute=True)
    assert fired == []                  # skipped → no race
    assert gotos == []                  # never switched Space


@_staging_skip
def test_run_place_fires_when_lock_held(monkeypatch):
    # Happy path on the STAGING build: lock acquired (yields True) → full goto/raise/fire/restore.
    spw = _load_staging()
    import contextlib as _cl
    win = _win("handoff-fanout · sw-foo · worker · " + "c" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(spw, "probe_windows", lambda: [win])
    monkeypatch.setattr(spw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(spw, "rectangle_running", lambda: True)
    gotos = []
    monkeypatch.setattr(spw, "goto", lambda n: (gotos.append(n), True)[1])
    raised = []
    monkeypatch.setattr(spw, "raise_window", lambda t: raised.append(t))
    monkeypatch.setattr(spw, "frontmost_is", lambda t: True)
    fired = []
    monkeypatch.setattr(spw, "fire_rectangle", lambda s: fired.append(s))
    bounds = iter(["0,0,100,100", "1028,39,1028,1290"])
    monkeypatch.setattr(spw, "capture_bounds_by_title", lambda t: next(bounds))

    @_cl.contextmanager
    def _held_lock():
        yield True                      # acquired
    monkeypatch.setattr(spw, "placement_lock", _held_lock)
    monkeypatch.setattr(spw.time, "sleep", lambda *a: None)
    spw.run_place("handoff-fanout", "sw-foo", None, "top-left", 0.0, execute=True)
    assert 5 in gotos                   # switched to the target's desktop (inside the lock)
    assert gotos[-1] == 9               # restored the owner's active desktop LAST
    assert raised and raised[0] == win["title"]
    assert fired == ["top-left"]


@_staging_skip
def test_run_self_skips_when_lock_unavailable(monkeypatch):
    # --self now serializes too: the frontmost window + Rectangle's "act on frontmost" target are
    # GLOBAL shared state, so a concurrent placement could steal focus between validate and fire.
    # placement_lock yields False → run_self SKIPs (SystemExit) and NEVER fires Rectangle.
    spw = _load_staging()
    import contextlib as _cl
    monkeypatch.setattr(spw, "read_frontmost_title", lambda: COORD)   # a valid 🧭 handoff-fanout coord
    monkeypatch.setattr(spw, "rectangle_running", lambda: True)
    monkeypatch.setattr(spw, "raise_window", lambda t: None)
    monkeypatch.setattr(spw, "frontmost_is", lambda t: True)
    fired = []
    monkeypatch.setattr(spw, "fire_rectangle", lambda s: fired.append(s))

    @_cl.contextmanager
    def _no_lock():
        yield False                     # never acquired
    monkeypatch.setattr(spw, "placement_lock", _no_lock)
    monkeypatch.setattr(spw.time, "sleep", lambda *a: None)
    with pytest.raises(SystemExit):
        spw.run_self("handoff-fanout", "right-half", execute=True)
    assert fired == []                  # skipped → no wrong-window fire


@_staging_skip
def test_run_self_fires_when_lock_held(monkeypatch):
    # Happy path on the STAGING build: lock acquired (yields True) → re-validate under lock →
    # raise → frontmost_is → fire.
    spw = _load_staging()
    import contextlib as _cl
    monkeypatch.setattr(spw, "read_frontmost_title", lambda: COORD)   # valid 🧭 handoff-fanout coord
    monkeypatch.setattr(spw, "rectangle_running", lambda: True)
    raised = []
    monkeypatch.setattr(spw, "raise_window", lambda t: raised.append(t))
    monkeypatch.setattr(spw, "frontmost_is", lambda t: True)
    bounds = iter(["0,0,100,100", "1028,39,1028,1290"])
    monkeypatch.setattr(spw, "capture_front_bounds", lambda: next(bounds))
    fired = []
    monkeypatch.setattr(spw, "fire_rectangle", lambda s: fired.append(s))

    @_cl.contextmanager
    def _held_lock():
        yield True                      # acquired
    monkeypatch.setattr(spw, "placement_lock", _held_lock)
    monkeypatch.setattr(spw.time, "sleep", lambda *a: None)
    spw.run_self("handoff-fanout", "right-half", execute=True)
    assert raised and raised[0] == COORD
    assert fired == ["right-half"]


# ── worker free-quadrant auto-alternation (sw-place-at-spawn) ─────────────────
# `--role worker` WITHOUT `--worker-index` picks the FIRST FREE of {top-left, bottom-left} on the
# target desktop, so auto-spawned workers fill/alternate instead of stacking. The feature lives in
# the STAGING copy first (the deployed supervisor-monitor/ copy is the pre-feature build until the
# coordinator deploy-audits it); these tests therefore load the STAGING file EXPLICITLY (the SOT
# for the new code), pinned by a skip that requires the new symbol — exactly like the lock tests.

_staging_freequad_skip = pytest.mark.skipif(
    not _STAGING_CPW.exists()
    or "def free_worker_slot" not in _STAGING_CPW.read_text(),
    reason="staging coord-place-window.py with worker free-quadrant selection not present",
)


@_staging_freequad_skip
def test_resolve_slot_worker_no_index_defers_to_free_quadrant():
    # resolve_slot defers a worker-without-index to the FREE_QUADRANT sentinel (decided at place
    # time), WHILE slot_for_role keeps its strict contract (still raises if called directly).
    spw = _load_staging()
    assert spw.resolve_slot(None, "worker", None) == spw.FREE_QUADRANT
    assert spw.FREE_QUADRANT not in spw.VALID_SLOTS          # never a real Rectangle action
    assert spw.resolve_slot(None, "worker", 0) == "top-left"   # index path EXACTLY unchanged
    assert spw.resolve_slot(None, "worker", 1) == "bottom-left"
    with pytest.raises(ValueError):
        spw.slot_for_role("worker", None)                    # strict contract preserved


@_staging_freequad_skip
def test_free_worker_slot_rule():
    spw = _load_staging()
    assert spw.free_worker_slot(set()) == "top-left"                    # both free → top-left
    assert spw.free_worker_slot({"bottom-left"}) == "top-left"          # only bottom occ → top-left
    assert spw.free_worker_slot({"top-left"}) == "bottom-left"          # top occ → bottom-left
    assert spw.free_worker_slot({"top-left", "bottom-left"}) == "top-left"  # both occ → fallback TL
    # accepts any iterable; extraneous members are ignored
    assert spw.free_worker_slot(["top-left", "right-half"]) == "bottom-left"


@_staging_freequad_skip
def test_quadrant_of_classification():
    spw = _load_staging()
    F = (0, 0, 2056, 1329)
    assert spw.quadrant_of((0, 39, 1028, 645), F) == "top-left"       # left half, upper
    assert spw.quadrant_of((0, 684, 1028, 645), F) == "bottom-left"   # left half, lower
    assert spw.quadrant_of((1028, 39, 1028, 1290), F) is None         # right half → not a left quad
    assert spw.quadrant_of(None, F) is None                          # unreadable bounds
    assert spw.quadrant_of((0, 0, 100, 100), None) is None           # unknown frame
    assert spw.quadrant_of((0, 0, 100, 100), (0, 0, 0, 0)) is None   # degenerate frame


@_staging_freequad_skip
def test_screen_visible_frame_parses_and_guards(monkeypatch):
    spw = _load_staging()

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    monkeypatch.setattr(spw, "_run", lambda *a, **k: _R(0, "0, 0, 2056, 1329"))
    assert spw.screen_visible_frame() == (0, 0, 2056, 1329)          # left,top,right,bottom → x,y,w,h
    monkeypatch.setattr(spw, "_run", lambda *a, **k: _R(1, ""))
    assert spw.screen_visible_frame() is None                       # osascript failed
    monkeypatch.setattr(spw, "_run", lambda *a, **k: _R(0, "0,0,0,0"))
    assert spw.screen_visible_frame() is None                       # degenerate rect
    monkeypatch.setattr(spw, "_run", lambda *a, **k: _R(0, "garbage"))
    assert spw.screen_visible_frame() is None                       # unparseable


@_staging_freequad_skip
def test_probe_left_quadrant_occupancy_classifies_excludes_and_filters(monkeypatch):
    # The probe: only the target desktop, excluding the target window itself; right-half windows
    # don't count; other-desktop windows are ignored.
    spw = _load_staging()
    monkeypatch.setattr(spw, "screen_visible_frame", lambda: (0, 0, 2056, 1329))
    wins = [
        _win("target", 100, desktop=5),    # the target itself → excluded by wid
        _win("worker-A", 101, desktop=5),  # top-left tile → occupies top-left
        _win("coord-R", 102, desktop=5),   # right-half → NOT a left quadrant
        _win("worker-B", 103, desktop=9),  # other desktop → filtered out
    ]
    bounds = {"worker-A": "0,39,1028,645", "coord-R": "1028,39,1028,1290"}
    monkeypatch.setattr(spw, "capture_bounds_by_title", lambda t: bounds.get(t))
    assert spw.probe_left_quadrant_occupancy(wins, 5, 100) == {"top-left"}


@_staging_freequad_skip
def test_probe_left_quadrant_occupancy_no_frame_is_empty(monkeypatch):
    # No screen frame (best-effort failure) ⇒ occupancy unknown ⇒ empty (→ default top-left).
    spw = _load_staging()
    monkeypatch.setattr(spw, "screen_visible_frame", lambda: None)
    assert spw.probe_left_quadrant_occupancy([_win("x", 1, desktop=5)], 5, 99) == set()


def _free_quad_run_place(monkeypatch, occupied):
    """Drive run_place on the STAGING build with slot=FREE_QUADRANT and a MOCKED occupancy probe
    (returning ``occupied``); return the list of slots actually fired."""
    import contextlib as _cl
    spw = _load_staging()
    win = _win("handoff-fanout · sw-foo · worker · " + "a" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(spw, "probe_windows", lambda: [win])
    monkeypatch.setattr(spw, "detect_active_desktop", lambda w: 9)
    monkeypatch.setattr(spw, "rectangle_running", lambda: True)
    monkeypatch.setattr(spw, "goto", lambda n: True)
    monkeypatch.setattr(spw, "raise_window", lambda t: None)
    monkeypatch.setattr(spw, "frontmost_is", lambda t: True)
    bounds = iter(["0,0,100,100", "0,39,1028,645"])
    monkeypatch.setattr(spw, "capture_bounds_by_title", lambda t: next(bounds))
    # the occupancy probe is MOCKED (the GUI screen/bounds osascript is never touched here)
    monkeypatch.setattr(spw, "probe_left_quadrant_occupancy", lambda w, d, x: set(occupied))
    fired = []
    monkeypatch.setattr(spw, "fire_rectangle", lambda s: fired.append(s))

    @_cl.contextmanager
    def _held_lock():
        yield True
    monkeypatch.setattr(spw, "placement_lock", _held_lock)
    monkeypatch.setattr(spw.time, "sleep", lambda *a: None)
    spw.run_place("handoff-fanout", "sw-foo", None, spw.FREE_QUADRANT, 0.0, execute=True)
    return fired


@_staging_freequad_skip
def test_run_place_free_quadrant_both_free_fires_top_left(monkeypatch):
    assert _free_quad_run_place(monkeypatch, set()) == ["top-left"]


@_staging_freequad_skip
def test_run_place_free_quadrant_top_left_occupied_fires_bottom_left(monkeypatch):
    assert _free_quad_run_place(monkeypatch, {"top-left"}) == ["bottom-left"]


@_staging_freequad_skip
def test_run_place_free_quadrant_both_occupied_fires_top_left(monkeypatch):
    # best-effort fallback when both left quadrants are already occupied
    assert _free_quad_run_place(monkeypatch, {"top-left", "bottom-left"}) == ["top-left"]


@_staging_freequad_skip
def test_run_place_free_quadrant_dryrun_never_fires(monkeypatch, capsys):
    # A FREE_QUADRANT dry-run resolves NOTHING + fires NOTHING (and never calls the occupancy probe).
    spw = _load_staging()
    win = _win("handoff-fanout · sw-foo · worker · " + "b" * 16 + " [worktree] — x", 100, desktop=5)
    monkeypatch.setattr(spw, "probe_windows", lambda: [win])
    monkeypatch.setattr(spw, "detect_active_desktop", lambda w: 9)
    probed = []
    monkeypatch.setattr(spw, "probe_left_quadrant_occupancy",
                        lambda w, d, x: (probed.append(1), set())[1])
    fired = []
    monkeypatch.setattr(spw, "fire_rectangle", lambda s: fired.append(s))
    spw.run_place("handoff-fanout", "sw-foo", None, spw.FREE_QUADRANT, 0.0, execute=False)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "free-left-quadrant" in out   # preview names the auto behavior
    assert fired == [] and probed == []  # dry-run touches nothing
