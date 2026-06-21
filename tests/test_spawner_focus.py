"""``spawner_focus.validate_spawner_focus`` — the SINGLE security gate shared by ``spawn``
(CLI ``--spawner-focus-path``) and ``dump`` (``$HANDOFF_WINDOW_FOCUS_PATH`` env).

direct-jump-spawn (2026-06-13): the validated value becomes an argument to ``code <file>`` in
``code-router.sh``, so the gate must reject anything that isn't an existing ``.handoff.code-workspace``
under a trusted root — and FAIL-OPEN (return ``None``, never raise) for every reject so a bad UX hint
never blocks a spawn/dump.

``isolated_handoff_home`` (conftest) points ``$HANDOFF_HOME`` at a tmp dir, so ``config.load().home``
— an allowed root — is that tmp dir; every test builds cfg via ``config.load()`` after it ran.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from handoff_fanout import config as _config
from handoff_fanout import spawner_focus


def _valid_ws(home: Path) -> Path:
    """An existing ``.handoff.code-workspace`` under the handoff home (an allowed root)."""
    ws = home / "some-proj" / "singlepane" / "coord-x.handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    return ws


def test_valid_under_home_returns_realpath(isolated_handoff_home):
    ws = _valid_ws(isolated_handoff_home)
    got = spawner_focus.validate_spawner_focus(str(ws), cfg=_config.load())
    assert got == os.path.realpath(str(ws))


def test_valid_under_tmpdir_returns_realpath(isolated_handoff_home, tmp_path):
    """``dx-spawn --coordinator`` writes its out-of-tree WS_FILE under $TMPDIR — also allowed."""
    ws = tmp_path / "coord-tmp.handoff.code-workspace"
    ws.write_text("{}")
    got = spawner_focus.validate_spawner_focus(str(ws), cfg=_config.load())
    assert got == os.path.realpath(str(ws))


@pytest.mark.parametrize("raw", [None, ""])
def test_absent_input_returns_none(isolated_handoff_home, raw):
    assert spawner_focus.validate_spawner_focus(raw, cfg=_config.load()) is None


def test_wrong_suffix_dropped(isolated_handoff_home):
    """A non-``.handoff.code-workspace`` would let the router ``code <arbitrary file>`` — reject."""
    bogus = isolated_handoff_home / "not-a-workspace.txt"
    bogus.write_text("x")
    assert spawner_focus.validate_spawner_focus(str(bogus), cfg=_config.load()) is None


def test_nonexistent_dropped(isolated_handoff_home):
    ghost = isolated_handoff_home / "ghost.handoff.code-workspace"
    assert spawner_focus.validate_spawner_focus(str(ghost), cfg=_config.load()) is None


def test_directory_not_file_dropped(isolated_handoff_home):
    """Right suffix but a directory (not a regular file) → dropped (isfile gate)."""
    d = isolated_handoff_home / "dir.handoff.code-workspace"
    d.mkdir()
    assert spawner_focus.validate_spawner_focus(str(d), cfg=_config.load()) is None


def test_outside_allowed_roots_dropped(isolated_handoff_home):
    """An absolute ``.handoff.code-workspace`` OUTSIDE every allowed root → dropped (root check)."""
    assert (
        spawner_focus.validate_spawner_focus(
            "/etc/forged.handoff.code-workspace", cfg=_config.load()
        )
        is None
    )


# ─── derive_singlepane_focus (djs-jump-return: SELF-REPORT from self-task, no env) ──────


def test_derive_returns_path_when_singlepane_workspace_exists(isolated_handoff_home):
    """The engine wrote ``<home>/<proj>/singlepane/<task>.handoff.code-workspace`` when this
    coordinator spawned — derive reconstructs it from the self-reported task (no env channel)."""
    home = isolated_handoff_home
    ws = home / "demo-proj" / "singlepane" / "coord-leg-7.handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    got = spawner_focus.derive_singlepane_focus(home, "demo-proj", "coord-leg-7")
    assert got == str(ws)
    # and the derived path round-trips through the SAME security gate (single boundary)
    assert spawner_focus.validate_spawner_focus(got, cfg=_config.load()) == os.path.realpath(str(ws))


def test_derive_returns_none_when_workspace_missing(isolated_handoff_home):
    """Bootstrap leg (dx-spawn-launched coordinator, no engine singlepane file) → None →
    caller fail-opens to today's per-project goto, no spurious 'dropped' warning."""
    assert spawner_focus.derive_singlepane_focus(isolated_handoff_home, "demo-proj", "nope") is None


@pytest.mark.parametrize(("project", "task"), [("", "t"), ("p", ""), ("", "")])
def test_derive_returns_none_on_empty_identity(isolated_handoff_home, project, task):
    assert spawner_focus.derive_singlepane_focus(isolated_handoff_home, project, task) is None


# ─── resolve_spawner_focus_path (mp-locate-return / sw-coord-p22: self-report the workspace PATH) ────
# The corrected Part A: env-independently identify the spawner's OWN .handoff.code-workspace (worktree
# from cwd; singlepane from the focus marker), validate through the SAME gate, and emit it as
# SPAWNER_FOCUS so the watchdog runs the EXISTING one-step focus-jump (no SPAWNER_DESKTOP/goto-N, no
# winlist here). Captured BEFORE the conftest autouse neutralizes the public name, so these tests hit
# the real implementation (dump/spawn see the neutralized None — suite hermeticity).
_REAL_RESOLVE = spawner_focus.resolve_spawner_focus_path
# spawn-unification Step 2: the real Tier-3 seam, captured before the conftest autouse pins it to None,
# so the seam's OWN tests (which load a stub resolver from $DX_SESSION_ROLE_PATH) exercise the genuine
# implementation rather than the neutralize stub.
_REAL_DERIVE_SELF = spawner_focus._derive_self_from_session


def _worktree_cwd(home: Path, project: str = "erp-system", task: str = "erp-dev-coord-33") -> Path:
    """A worktree coordinator cwd UNDER the handoff home (= an allowed root) carrying its workspace —
    mirrors the engine layout ``<home>/<project>/worktrees/<task>/.handoff.code-workspace``."""
    cwd = home / project / "worktrees" / task
    cwd.mkdir(parents=True)
    (cwd / ".handoff.code-workspace").write_text("{}")
    return cwd


def test_resolve_focus_worktree_returns_validated_path(isolated_handoff_home):
    cwd = _worktree_cwd(isolated_handoff_home)
    got = _REAL_RESOLVE(cwd, cfg=_config.load())
    assert got == os.path.realpath(str(cwd / ".handoff.code-workspace"))


def test_resolve_focus_none_when_no_workspace_and_no_marker(isolated_handoff_home, tmp_path):
    """A plain cwd (no .handoff.code-workspace) with no marker → None (singlepane / non-worktree)."""
    plain = tmp_path / "plain-repo"
    plain.mkdir()
    assert _REAL_RESOLVE(plain, cfg=_config.load()) is None


# (validation gate — path outside allowed roots / wrong suffix → None — is covered by the
# validate_spawner_focus tests above; resolve_spawner_focus_path delegates to the SAME gate.)


# ─── §1 Tier-2 SINGLEPANE: derive_singlepane_focus from the self-reported task (NO marker hook) ─────
# The corrected Tier-2: a singlepane coordinator (cwd = shared repo root) self-reports its OWN task via
# --self-task; resolve reads the REAL engine sidecar <home>/<proj>/singlepane/<task>.handoff.code-workspace
# (derive_singlepane_focus) → validate → SPAWNER_FOCUS. The earlier marker-hook route is DROPPED.


def _singlepane_ws(home: Path, project: str, task: str) -> Path:
    """The engine's real singlepane workspace sidecar under the home (so derive + validate accept it)."""
    ws = home / project / "singlepane" / f"{task}.handoff.code-workspace"
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.write_text("{}")
    return ws


def test_resolve_focus_tier2_singlepane_via_self_task(isolated_handoff_home):
    """cwd is NOT a worktree → Tier-2: derive_singlepane_focus(home, project, self_task) → validated PATH."""
    home = isolated_handoff_home
    ws = _singlepane_ws(home, "sdgf-runner", "sdgf-sup-13")
    plain = home / "sdgf-runner"  # singlepane cwd = shared repo root (no in-tree .handoff.code-workspace)
    plain.mkdir(parents=True, exist_ok=True)
    got = _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="sdgf-runner",
                        self_task="sdgf-sup-13")
    assert got == os.path.realpath(str(ws))


def test_resolve_focus_tier2_none_when_no_sidecar(isolated_handoff_home):
    """self_task given but the engine sidecar doesn't exist (bootstrap leg) → None (fail-open)."""
    home = isolated_handoff_home
    plain = home / "sdgf-runner"
    plain.mkdir(parents=True, exist_ok=True)
    assert _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="sdgf-runner",
                         self_task="nope-task") is None


def test_resolve_focus_tier2_skipped_without_self_task(isolated_handoff_home):
    """No self_task (worker / non-self-reporting caller) → Tier-2 never consulted → None."""
    home = isolated_handoff_home
    _singlepane_ws(home, "p", "co")  # sidecar exists, but no self_task to point at it
    plain = home / "p"
    assert _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="p") is None


# ─── Tier-1 project binding (sw-spawn-unify-s1fix) ──────────────────────────────────────────────
# validate_spawner_focus only proves "an existing .handoff.code-workspace under a TRUSTED root" — and
# EVERY project's worktrees live under that SAME root — so without this binding, a spawn/succession run
# from project B's worktree cwd while dispatching FOR project A would mis-resolve B's workspace (worker
# born on the wrong project's coordinator desktop). Tightening-only: same-project cwd passes unchanged;
# only a cross-project cwd is dropped (→ Tier-2 / None).


def test_resolve_tier1_dropped_when_cwd_belongs_to_other_project(isolated_handoff_home):
    """Cross-project mis-grab blocked: cwd is project-B's worktree (a valid .handoff.code-workspace
    under an allowed root) but we resolve FOR project-A → Tier-1 is dropped (None, NOT B's workspace)."""
    home = isolated_handoff_home
    cwd_b = _worktree_cwd(home, "project-b", "b-coord-1")
    got = _REAL_RESOLVE(cwd_b, cfg=_config.load(), home=home, project="project-a")
    assert got is None  # B's Tier-1 workspace must NOT be returned for project-a


def test_resolve_tier1_other_project_falls_through_to_tier2(isolated_handoff_home):
    """The cross-project Tier-1 drop still lets the correct project-A Tier-2 sidecar resolve: the cwd
    workspace is B's (dropped), the self-reported project-A singlepane sidecar wins."""
    home = isolated_handoff_home
    cwd_b = _worktree_cwd(home, "project-b", "b-coord-2")
    a_sidecar = _singlepane_ws(home, "project-a", "a-coord-9")
    got = _REAL_RESOLVE(
        cwd_b, cfg=_config.load(), home=home, project="project-a", self_task="a-coord-9"
    )
    assert got == os.path.realpath(str(a_sidecar))  # Tier-2 (project-a) — never B's Tier-1


def test_resolve_tier1_same_project_cwd_passes_unchanged(isolated_handoff_home):
    """Tightening-only invariant: the normal flow — a same-project worktree coordinator dispatching a
    same-project worker — resolves its OWN cwd workspace via Tier-1 exactly as before (binding passes)."""
    home = isolated_handoff_home
    cwd_a = _worktree_cwd(home, "project-a", "a-coord-1")
    got = _REAL_RESOLVE(cwd_a, cfg=_config.load(), home=home, project="project-a")
    assert got == os.path.realpath(str(cwd_a / ".handoff.code-workspace"))


# ─── log_anchor_miss (spawn-unification Step 1 / sw-spawn-unify-step1) ───────────────────────────
# The telemetry that turns the silent "no SPAWNER_FOCUS → static-map fallback → wrong desktop" event
# into a countable JSON line. STRICTLY ADDITIVE + NON-BLOCKING (a telemetry write never breaks a
# spawn/dump). The producers (spawn/dump/audit-close) call it the moment anchor resolution yields None.
import json as _json  # noqa: E402


def test_log_anchor_miss_writes_one_json_line_with_all_fields(tmp_path):
    spawner_focus.log_anchor_miss(
        home=tmp_path,
        project="demo-proj",
        task="wk-7",
        cwd="/some/cwd",
        isolation="singlepane",
        reason="spawn:anchor-unresolved",
    )
    log = tmp_path / "demo-proj" / "spawn-anchor-miss.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert set(rec) == {"ts", "task", "project", "cwd", "isolation", "reason"}
    assert rec["task"] == "wk-7"
    assert rec["project"] == "demo-proj"
    assert rec["cwd"] == "/some/cwd"
    assert rec["isolation"] == "singlepane"
    assert rec["reason"] == "spawn:anchor-unresolved"
    assert rec["ts"]  # ISO-8601 UTC timestamp present


def test_log_anchor_miss_appends_across_calls(tmp_path):
    for i in range(3):
        spawner_focus.log_anchor_miss(
            home=tmp_path, project="p", task=f"t{i}", cwd="/c", isolation=None,
            reason="dump:anchor-unresolved",
        )
    log = tmp_path / "p" / "spawn-anchor-miss.log"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 3


def test_log_anchor_miss_tolerates_none_task_and_isolation(tmp_path):
    """A dump producer may not know the worker task / isolation — None must serialize cleanly."""
    spawner_focus.log_anchor_miss(
        home=tmp_path, project="p", task=None, cwd="/c", isolation=None, reason="dump:anchor-unresolved",
    )
    rec = _json.loads((tmp_path / "p" / "spawn-anchor-miss.log").read_text().splitlines()[0])
    assert rec["task"] is None and rec["isolation"] is None


def test_log_anchor_miss_is_non_blocking_on_unwritable_home(tmp_path):
    """Contract: a telemetry write must NEVER raise — an unwritable home is swallowed (fail-open)."""
    bogus_home = tmp_path / "afile"
    bogus_home.write_text("not a dir")  # makedirs(<afile>/p) will fail → swallowed
    # must not raise
    spawner_focus.log_anchor_miss(
        home=bogus_home, project="p", task="t", cwd="/c", isolation="x", reason="r",
    )


# ─── Tier 3 SESSION IDENTITY (spawn-unification Step 2 / sw-su-step2) ────────────────────────────────
# When no explicit anchor, no same-project worktree cwd (Tier-1), and no --self-task (Tier-2) resolved,
# the engine recovers the DISPATCHING coordinator's OWN (project, task) from the shared session-role
# resolver (``_derive_self_from_session``) and rebuilds ITS singlepane workspace. This closes the rf/sf
# wrong-desktop root cause for every produce path uniformly (a coordinator that forgot --self-task still
# emits SPAWNER_FOCUS). The conftest autouse pins the seam to None for hermeticity; these tests re-set it
# to a controlled identity (Tier-3 RESOLVE tests) or exercise the real seam via $DX_SESSION_ROLE_PATH.


def test_resolve_focus_tier3_session_identity(isolated_handoff_home, monkeypatch):
    """Tier-1 (cwd) + Tier-2 (no self_task) both miss → Tier-3 derives the coordinator's (project, task)
    from the session resolver and resolves ITS singlepane sidecar workspace."""
    home = isolated_handoff_home
    coord_ws = home / "rakeforge" / "singlepane" / "rf-coord-3.handoff.code-workspace"
    coord_ws.parent.mkdir(parents=True)
    coord_ws.write_text("{}")
    monkeypatch.setattr(
        spawner_focus, "_derive_self_from_session", lambda cwd: ("rakeforge", "rf-coord-3")
    )
    plain = home / "shared-repo-root"  # repo-root cwd: no workspace, not a worktree → Tier-1 miss
    plain.mkdir()
    got = _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="rakeforge")
    assert got == os.path.realpath(str(coord_ws))


def test_resolve_focus_tier3_uses_coordinator_project_not_target(isolated_handoff_home, monkeypatch):
    """Tier-3 rebuilds the workspace under the COORDINATOR's OWN project (from the resolver), NOT the
    target ``project`` — so a cross-project dispatch (coordinator in handoff-fanout dispatching FOR
    rakeforge) still resolves the coordinator's real workspace."""
    home = isolated_handoff_home
    coord_ws = home / "handoff-fanout" / "singlepane" / "sw-coord-9.handoff.code-workspace"
    coord_ws.parent.mkdir(parents=True)
    coord_ws.write_text("{}")
    monkeypatch.setattr(
        spawner_focus, "_derive_self_from_session", lambda cwd: ("handoff-fanout", "sw-coord-9")
    )
    plain = home / "shared-repo-root"
    plain.mkdir()
    got = _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="rakeforge")  # target ≠ coord proj
    assert got == os.path.realpath(str(coord_ws))


def test_resolve_focus_tier3_none_when_no_coord_sidecar(isolated_handoff_home, monkeypatch):
    """Re-grounding floor: a resolver identity whose singlepane workspace does NOT exist under cfg.home
    → None (a stale/foreign identity never produces a mis-jump)."""
    home = isolated_handoff_home
    monkeypatch.setattr(
        spawner_focus, "_derive_self_from_session", lambda cwd: ("ghost-proj", "ghost-coord")
    )
    plain = home / "shared-repo-root"
    plain.mkdir()
    assert _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="ghost-proj") is None


def test_resolve_focus_tier2_explicit_wins_over_tier3(isolated_handoff_home, monkeypatch):
    """Precedence: an explicit ``--self-task`` (Tier-2) resolves BEFORE Tier-3 is consulted — the seam
    is wired to a DIFFERENT identity to prove precedence, not coincidence."""
    home = isolated_handoff_home
    tier2_ws = home / "demo" / "singlepane" / "explicit-coord.handoff.code-workspace"
    tier2_ws.parent.mkdir(parents=True)
    tier2_ws.write_text("{}")
    other_ws = home / "other" / "singlepane" / "other-coord.handoff.code-workspace"
    other_ws.parent.mkdir(parents=True)
    other_ws.write_text("{}")
    monkeypatch.setattr(
        spawner_focus, "_derive_self_from_session", lambda cwd: ("other", "other-coord")
    )
    plain = home / "shared-repo-root"
    plain.mkdir()
    got = _REAL_RESOLVE(
        plain, cfg=_config.load(), home=home, project="demo", self_task="explicit-coord"
    )
    assert got == os.path.realpath(str(tier2_ws))  # Tier-2 explicit, never Tier-3's other-coord


# ─── Tier-3 CONFIDENCE GATE end-to-end (sw-su-s2fix) ─────────────────────────────────────────────────
# These drive the REAL ``_derive_self_from_session`` seam (restored over the conftest neutralization) via
# a ``$DX_SESSION_ROLE_PATH`` stub resolver, through the REAL ``resolve_spawner_focus_path``, with the
# coordinator's singlepane sidecar PRESENT ON DISK — so the ONLY thing that decides whether Tier-3 emits
# the workspace is the ``confidence == "definite"`` gate (not a monkeypatched lambda). Pins Tier-3's three
# states: ① definite supervisor → fires; ② suspected singlepane-sidecar → REJECTED though the sidecar
# exists (adversarial: drop the gate → ② resolves the workspace → test fails); ③ owner/worker → None.


def _write_role_stub(tmp_path: Path, role_dict: dict) -> Path:
    """Write a one-function ``dx_session_role`` stand-in returning ``role_dict`` (no machine dependency)."""
    stub = tmp_path / "stub_role_resolver.py"
    stub.write_text(f"def resolve_session_role(cwd, env=None):\n    return {role_dict!r}\n")
    return stub


def test_resolve_focus_tier3_real_seam_accepts_definite_supervisor(
    isolated_handoff_home, monkeypatch, tmp_path
):
    """① DEFINITE supervisor (worktree-marker / env identity) with a real sidecar → Tier-3 fires and
    resolves the coordinator's workspace, running the genuine confidence-gated seam end-to-end."""
    home = isolated_handoff_home
    coord_ws = _singlepane_ws(home, "erp-system", "erp-coord-7")
    monkeypatch.setattr(spawner_focus, "_derive_self_from_session", _REAL_DERIVE_SELF)  # un-neutralize
    monkeypatch.setenv(
        "DX_SESSION_ROLE_PATH",
        str(_write_role_stub(tmp_path, {
            "role": "supervisor", "confidence": "definite", "source": "marker",
            "task": "erp-coord-7", "project": "erp-system",
        })),
    )
    plain = home / "shared-repo-root"
    plain.mkdir()
    got = _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="erp-system")
    assert got == os.path.realpath(str(coord_ws))


def test_resolve_focus_tier3_real_seam_rejects_suspected_singlepane_identity(
    isolated_handoff_home, monkeypatch, tmp_path
):
    """② ADVERSARIAL GUARD: the coordinator's REAL singlepane sidecar EXISTS, so if the resolver's
    ``suspected`` singlepane-sidecar identity were honored Tier-3 WOULD resolve that workspace. The
    confidence gate is the ONLY thing returning None here — remove ``confidence == "definite"`` from
    ``_derive_self_from_session`` and this assertion flips to the workspace path (true guard, not a
    mock-tautology). This is exactly the real-but-wrong anchor codex flagged."""
    home = isolated_handoff_home
    _singlepane_ws(home, "erp-system", "erp-coord-7")  # sidecar present → would resolve if accepted
    monkeypatch.setattr(spawner_focus, "_derive_self_from_session", _REAL_DERIVE_SELF)
    monkeypatch.setenv(
        "DX_SESSION_ROLE_PATH",
        str(_write_role_stub(tmp_path, {
            "role": "supervisor", "confidence": "suspected", "source": "singlepane-sidecar",
            "task": "erp-coord-7", "project": "erp-system",
        })),
    )
    plain = home / "shared-repo-root"
    plain.mkdir()
    assert _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="erp-system") is None


@pytest.mark.parametrize(
    "role_dict",
    [
        {"role": "owner", "confidence": "definite", "source": "none", "task": None, "project": None},
        {"role": "worker", "confidence": "suspected", "source": "cwd",
         "task": "wk-3", "project": "erp-system"},
    ],
)
def test_resolve_focus_tier3_real_seam_none_for_owner_or_worker(
    isolated_handoff_home, monkeypatch, tmp_path, role_dict
):
    """③ owner / worker identity → Tier-3 never fires (None) even with a same-name sidecar on disk —
    only a supervisor identity is ever a Tier-3 candidate."""
    home = isolated_handoff_home
    _singlepane_ws(home, "erp-system", "wk-3")  # exists; a worker/owner identity still must not anchor
    monkeypatch.setattr(spawner_focus, "_derive_self_from_session", _REAL_DERIVE_SELF)
    monkeypatch.setenv("DX_SESSION_ROLE_PATH", str(_write_role_stub(tmp_path, role_dict)))
    plain = home / "shared-repo-root"
    plain.mkdir()
    assert _REAL_RESOLVE(plain, cfg=_config.load(), home=home, project="erp-system") is None


# ─── _derive_self_from_session (the Tier-3 seam itself) ──────────────────────────────────────────────


def test_derive_self_from_session_returns_definite_supervisor_identity(monkeypatch, tmp_path):
    """The seam loads the shared resolver from ``$DX_SESSION_ROLE_PATH`` (a stub here — no machine
    dependency) and returns (project, task) for a ``definite`` supervisor carrying both. ``definite`` is
    the worktree-marker / worktree-sidecar / env path — the only confidence Tier-3 trusts (sw-su-s2fix)."""
    stub = tmp_path / "stub_role.py"
    stub.write_text(
        "def resolve_session_role(cwd, env=None):\n"
        "    return {'role': 'supervisor', 'task': 'c-7', 'project': 'p',\n"
        "            'confidence': 'definite', 'source': 'marker'}\n"
    )
    monkeypatch.setenv("DX_SESSION_ROLE_PATH", str(stub))
    assert _REAL_DERIVE_SELF("/anywhere") == ("p", "c-7")


def test_derive_self_from_session_rejects_suspected_supervisor(monkeypatch, tmp_path):
    """CONFIDENCE GATE (sw-su-s2fix): a ``supervisor`` resolved with ``confidence='suspected'`` —
    the singlepane-sidecar identity whose cwd (shared repo root) is indistinguishable from the owner's
    everyday session — is REJECTED (→ None) even though it carries BOTH task and project. Honoring it
    would let Tier-3 anchor the worker to a real-but-WRONG desktop. ADVERSARIAL SELF-PROOF: drop the
    ``confidence == "definite"`` clause in ``_derive_self_from_session`` and this returns ('p','c-7') →
    this test fails → it is a genuine guard, not a tautology."""
    stub = tmp_path / "stub_role_suspected.py"
    stub.write_text(
        "def resolve_session_role(cwd, env=None):\n"
        "    return {'role': 'supervisor', 'task': 'c-7', 'project': 'p',\n"
        "            'confidence': 'suspected', 'source': 'singlepane-sidecar'}\n"
    )
    monkeypatch.setenv("DX_SESSION_ROLE_PATH", str(stub))
    assert _REAL_DERIVE_SELF("/anywhere") is None


@pytest.mark.parametrize(
    "role_dict",
    [
        {"role": "worker", "task": "w-1", "project": "p", "confidence": "suspected"},
        {"role": "owner", "task": None, "project": None, "confidence": "definite"},
        # ambiguous scan → resolver gives no task
        {"role": "supervisor", "task": None, "project": "p", "confidence": "definite"},
        {"role": "supervisor", "task": "c-7", "project": None, "confidence": "definite"},
        {"role": "contradiction", "task": "c-7", "project": "p", "confidence": "definite"},
        # confidence gate (sw-su-s2fix): a supervisor WITH task+project but NOT definite is rejected
        {"role": "supervisor", "task": "c-7", "project": "p", "confidence": "suspected"},
        # defensive: a malformed identity missing the confidence key entirely is also rejected
        {"role": "supervisor", "task": "c-7", "project": "p"},
    ],
)
def test_derive_self_from_session_none_for_non_definite_or_incomplete(monkeypatch, tmp_path, role_dict):
    """Only a ``definite`` ``supervisor`` with BOTH task and project is honored — worker / owner /
    ambiguous (no task) / contradiction / ``suspected`` supervisor / missing-confidence → None (never a
    guessed identity, never a real-but-wrong anchor from a weak-evidence identity)."""
    stub = tmp_path / "stub_role.py"
    stub.write_text(f"def resolve_session_role(cwd, env=None):\n    return {role_dict!r}\n")
    monkeypatch.setenv("DX_SESSION_ROLE_PATH", str(stub))
    assert _REAL_DERIVE_SELF("/anywhere") is None


def test_derive_self_from_session_fail_open_when_resolver_absent(monkeypatch, tmp_path):
    """Resolver file absent → None (FAIL-OPEN), never raises."""
    monkeypatch.setenv("DX_SESSION_ROLE_PATH", str(tmp_path / "does-not-exist.py"))
    assert _REAL_DERIVE_SELF("/anywhere") is None


def test_derive_self_from_session_fail_open_when_resolver_raises(monkeypatch, tmp_path):
    """A resolver that raises → None (FAIL-OPEN) — a broken identity source never breaks a spawn."""
    stub = tmp_path / "stub_boom.py"
    stub.write_text("def resolve_session_role(cwd, env=None):\n    raise RuntimeError('boom')\n")
    monkeypatch.setenv("DX_SESSION_ROLE_PATH", str(stub))
    assert _REAL_DERIVE_SELF("/anywhere") is None
