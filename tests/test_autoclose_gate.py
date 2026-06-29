"""req3 — THE safety-gate tests (the most important tests in this build).

🔴 SAFETY-CRITICAL: a worker window auto-closes ⟺ ALL 5 predicates hold. These tests
construct synthetic scenarios on a REAL git repo and assert the gate **REFUSES to close**
every unsafe case, and **closes ONLY** the genuinely audited-to-terminal + idle + clean +
worker (non-coordinator) window. Supreme rule: 宁可漏关，绝不误关.

Scenarios (brief §5-C):
  ① forged .audit_discharged with NO git corroboration  → REFUSE
  ② vacuous-ancestor (branch never advanced)            → REFUSE
  ③ in-flight (idle too small / running_tool / unmapped)→ REFUSE
  ④ dirty worktree                                       → REFUSE
  ⑤ coordinator window (role != worker)                 → REFUSE
  ⑥ weak signal only (.worker_reported, no discharge)   → REFUSE
  ✅ genuine GREEN + git-corroborated + idle + clean     → close_ok
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from handoff_fanout import autoclose_gate as gate
from handoff_fanout import config as _config

ISO = "2026-06-26T00:00:00+00:00"
EPOCH = datetime.fromisoformat(ISO).timestamp()  # exact epoch of the transcript timestamp
PROJECT = "demo"
TASK = "wk-1"
NONCE = "184f6d9d2b3830af"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A full synthetic world: origin repo (main = C0 base → C1 merged worker commit),
    a workspace clone, a worker worktree checked out at C1, the spawn sidecars, and a
    discharge signal — all consistent for the HAPPY path. Individual tests then mutate
    ONE thing to drive a refusal.
    """
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    projects_root = tmp_path / "transcripts"  # injected ~/.claude/projects stand-in
    projects_root.mkdir()

    # origin repo: C0 (base) then C1 (the worker's merged commit). origin/main = C1.
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--initial-branch=main")
    _git(origin, "config", "user.email", "t@t.test")
    _git(origin, "config", "user.name", "t")
    _git(origin, "config", "commit.gpgsign", "false")
    (origin / "f.txt").write_text("base\n")
    _git(origin, "add", "f.txt")
    _git(origin, "commit", "-q", "-m", "C0 base")
    c0 = _git(origin, "rev-parse", "HEAD")
    (origin / "f.txt").write_text("worker work\n")
    _git(origin, "add", "f.txt")
    _git(origin, "commit", "-q", "-m", "C1 worker merged")
    c1 = _git(origin, "rev-parse", "HEAD")

    # workspace = clone of origin (has remote origin + refs/remotes/origin/main = C1).
    main_repo = ws_root / PROJECT
    _git(tmp_path, "clone", "--quiet", str(origin), str(main_repo))
    _git(main_repo, "config", "user.email", "t@t.test")
    _git(main_repo, "config", "user.name", "t")
    _git(main_repo, "config", "commit.gpgsign", "false")

    # worker worktree at home/<project>/worktrees/<task>, branch handoff/<task> @ C1.
    cfg = _config.Config(home=home, workspace_root=ws_root)
    wt = cfg.home / PROJECT / "worktrees" / TASK
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(main_repo, "worktree", "add", "-b", f"handoff/{TASK}", str(wt), c1)

    ack = cfg.ack_dir(PROJECT)
    ack.mkdir(parents=True, exist_ok=True)
    queue = cfg.queue_dir(PROJECT)
    queue.mkdir(parents=True, exist_ok=True)

    # spawn-time .worktree base anchor (written at spawn, before the worker ran): base=C0.
    (ack / f"{TASK}.worktree").write_text(
        json.dumps({"base_sha": c0, "branch": f"handoff/{TASK}", "integration_branch": "main"})
    )
    # spawn sidecar: role=worker (predicate 1), spawn_nonce.
    (queue / f"{TASK}.singlepane").write_text(
        json.dumps({"role": "worker", "isolation": "worktree", "spawn_nonce": NONCE})
    )
    # the GREEN discharge signal (worktree_head == merge_sha == C1).
    _write_signal(ack, TASK, verdict="GREEN", merge_sha=c1, worktree_head=c1, nonce=NONCE)
    # this task's OWN recorded closing commit (reclaim._pinned_sha source): pinned == C1.
    # The gate binds merge_sha to THIS (FORGE D fix), not the shared spawn base.
    _write_pinned(ack, TASK, c1)
    # a settled, long-idle transcript (assistant_turn).
    _write_transcript(projects_root, wt, kind="assistant_turn", ts=ISO)

    return {
        "cfg": cfg, "home": home, "projects_root": projects_root, "wt": wt,
        "ack": ack, "queue": queue, "c0": c0, "c1": c1, "origin": origin, "main_repo": main_repo,
    }


def _write_signal(ack: Path, task: str, *, verdict, merge_sha, worktree_head, nonce):
    (ack / f"{task}.audit_discharged").write_text(
        json.dumps(
            {
                "schema_version": gate.codex_audit.AUDIT_DISCHARGED_SCHEMA_VERSION,
                "kind": "audit_discharged",
                "task": task,
                "verdict": verdict,
                "merge_sha": merge_sha,
                "worktree_head": worktree_head,
                "nonce": nonce,
                "discharged_at": ISO,
            }
        )
    )


def _write_pinned(ack: Path, task: str, sha: str):
    """This task's OWN recorded closing head — the ``reclaim._pinned_sha`` source the gate
    binds ``merge_sha`` to (``ack/<task>.old_ready`` ``commit_hash``). Distinct from the
    shared spawn base: it is THIS task's delivery, not "any commit between base and main"."""
    (ack / f"{task}.old_ready").write_text(json.dumps({"task": task, "commit_hash": sha}))


def _write_transcript(projects_root: Path, wt: Path, *, kind: str, ts: str):
    munge = gate._reclaim.transcript_project_dir_name(wt)
    tdir = projects_root / munge
    tdir.mkdir(parents=True, exist_ok=True)
    if kind == "assistant_turn":
        content = [{"type": "text", "text": "done"}]
        typ = "assistant"
    elif kind == "running_tool":
        content = [{"type": "tool_use", "name": "Bash"}]
        typ = "assistant"
    elif kind == "blocked_on_question":
        content = [{"type": "tool_use", "name": "AskUserQuestion"}]
        typ = "assistant"
    elif kind == "dangling_tool_result":
        content = [{"type": "tool_result", "content": "out"}]
        typ = "user"
    else:
        raise ValueError(kind)
    (tdir / "t.jsonl").write_text(
        json.dumps({"type": typ, "timestamp": ts, "message": {"content": content}}) + "\n"
    )


def _gate(world, *, now_epoch=EPOCH + 7200):
    return gate.gate_task(
        world["cfg"], PROJECT, TASK,
        now_epoch=now_epoch, projects_root=world["projects_root"],
    )


# ─── ✅ the ONLY closeable case ──────────────────────────────────────────────


def test_happy_path_closes(world):
    d = _gate(world)
    assert d.close_ok is True, f"expected close_ok, got refusal {d.reason}: {d.detail}"
    assert d.reason == gate.CLOSE_OK
    assert d.nonce == NONCE
    assert d.evidence["merge_sha"] == world["c1"]


# ─── ⑥ weak signal only → no-signal ──────────────────────────────────────────


def test_weak_signal_only_refused(world):
    (world["ack"] / f"{TASK}.audit_discharged").unlink()
    (world["ack"] / f"{TASK}.worker_reported").write_text("")  # weak self-report
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "no-signal"


def test_non_green_verdict_refused(world):
    _write_signal(world["ack"], TASK, verdict="RED", merge_sha=world["c1"],
                  worktree_head=world["c1"], nonce=NONCE)
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "verdict-not-green"


# ─── ⑤ coordinator window → not-worker ───────────────────────────────────────


@pytest.mark.parametrize("role", ["supervisor_succession", "coordinator", "solo", None])
def test_coordinator_or_nonworker_refused(world, role):
    sidecar = {"isolation": "worktree", "spawn_nonce": NONCE}
    if role is not None:
        sidecar["role"] = role
    (world["queue"] / f"{TASK}.singlepane").write_text(json.dumps(sidecar))
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "not-worker"


# ─── ② vacuous-ancestor (branch never advanced) → vacuous-no-advance ─────────


def test_vacuous_ancestor_refused(world):
    # merge_sha == spawn base (C0): an empty/just-spawned branch is vacuously an ancestor.
    c0 = world["c0"]
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=c0, worktree_head=c0, nonce=NONCE)
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "vacuous-no-advance"


# ─── ① forged signal, NO git corroboration ───────────────────────────────────


def test_forged_unmerged_sha_refused(world):
    # A fabricated merge_sha that is not in the repo at all → no ancestry backing. Forge
    # the recorded pinned to MATCH (the stronger attacker who also forges old_ready), so the
    # local pinned-equality check passes and the GIT ancestry half is the defense that bites.
    bogus = "f" * 40
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=bogus,
                  worktree_head=bogus, nonce=NONCE)
    _write_pinned(world["ack"], TASK, bogus)
    d = _gate(world)
    assert d.close_ok is False
    # base is not an ancestor of a non-existent commit → caught in the git half.
    assert d.reason in ("base-not-ancestor-of-merge", "not-merged")


def test_unmerged_branch_commit_refused(world):
    # Worker advanced to C2 (a real commit) but it was NEVER merged to origin/main.
    wt = world["wt"]
    (wt / "f.txt").write_text("unmerged extra\n")
    _git(wt, "add", "f.txt")
    _git(wt, "config", "user.email", "t@t.test")
    _git(wt, "config", "user.name", "t")
    _git(wt, "config", "commit.gpgsign", "false")
    _git(wt, "commit", "-q", "-m", "C2 unmerged")
    c2 = _git(wt, "rev-parse", "HEAD")
    # pinned == merge_sha == C2 (the worker really delivered C2): isolates the GIT
    # not-merged check — C2 is a real advance from base but never reached origin/main.
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=c2, worktree_head=c2, nonce=NONCE)
    _write_pinned(world["ack"], TASK, c2)
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "not-merged"


def test_live_head_drift_refused(world):
    # Signal claims the merged C1, but the live worktree has moved on to C2 → drift forge.
    wt = world["wt"]
    (wt / "f.txt").write_text("moved on\n")
    _git(wt, "add", "f.txt")
    _git(wt, "config", "user.email", "t@t.test")
    _git(wt, "config", "user.name", "t")
    _git(wt, "config", "commit.gpgsign", "false")
    _git(wt, "commit", "-q", "-m", "C2 drift")
    # signal still claims C1 (merged) — head_sha(wt) is now C2 ≠ C1.
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "worktree-live-head-drift"


def test_recorded_head_drift_refused(world):
    # worktree_head in the signal disagrees with merge_sha → local P0-1(iv) drift.
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=world["c1"],
                  worktree_head=world["c0"], nonce=NONCE)
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "head-drift"


# ─── ③ in-flight (idle / kind / unmapped) ────────────────────────────────────


def test_idle_too_small_refused(world):
    # transcript is settled but only 60s idle (< 1800 threshold).
    d = _gate(world, now_epoch=EPOCH + 60)
    assert d.close_ok is False
    assert d.reason == "not-idle-enough"


@pytest.mark.parametrize("kind", ["running_tool", "blocked_on_question", "dangling_tool_result"])
def test_in_flight_kind_refused(world, kind):
    _write_transcript(world["projects_root"], world["wt"], kind=kind, ts=ISO)
    d = _gate(world)  # long idle, but the last conversation kind is NOT settled
    assert d.close_ok is False
    assert d.reason == f"in-flight:{kind}"


def test_unmapped_no_transcript_dir_refused(world):
    # No transcript dir at all (AI-titled / unmapped) → never read as idle-enough.
    import shutil
    munge = gate._reclaim.transcript_project_dir_name(world["wt"])
    shutil.rmtree(world["projects_root"] / munge)
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "transcript-dir-missing"


def test_idle_unknown_no_timestamp_refused(world):
    # A settled assistant entry with NO timestamp → idle -1 → fail-safe.
    munge = gate._reclaim.transcript_project_dir_name(world["wt"])
    tdir = world["projects_root"] / munge
    (tdir / "t.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n"
    )
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "idle-unknown"


# ─── ④ dirty worktree ────────────────────────────────────────────────────────


def test_dirty_worktree_refused(world):
    (world["wt"] / "f.txt").write_text("uncommitted edit\n")  # tracked file modified
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "dirty"


def test_dirty_untracked_file_refused(world):
    (world["wt"] / "scratch.txt").write_text("untracked junk\n")
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "dirty"


# ─── base anchor / sidecar / schema fail-safes ───────────────────────────────


def test_missing_base_anchor_refused(world):
    (world["ack"] / f"{TASK}.worktree").unlink()
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "base-anchor-missing"


def test_missing_sidecar_refused(world):
    (world["queue"] / f"{TASK}.singlepane").unlink()
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "sidecar-missing"


def test_unknown_schema_refused(world):
    sig = json.loads((world["ack"] / f"{TASK}.audit_discharged").read_text())
    sig["schema_version"] = "9.9"
    (world["ack"] / f"{TASK}.audit_discharged").write_text(json.dumps(sig))
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "signal-schema-unknown"


def test_nonce_mismatch_refused(world):
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=world["c1"],
                  worktree_head=world["c1"], nonce="deadbeefdeadbeef")
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "nonce-mismatch"


# ─── worktree-gone backlog (janitor) corroboration ───────────────────────────


def test_worktree_gone_with_reclaim_done_closes(world):
    # Janitor backlog: the §6c reclaim already merged + removed the worktree (reclaim_done
    # present). The signal + ancestry still prove terminal → closeable.
    wt = world["wt"]
    _git(world["main_repo"], "worktree", "remove", "--force", str(wt))
    (world["ack"] / f"{TASK}.reclaim_done").write_text(json.dumps({"task": TASK}))
    d = _gate(world)
    assert d.close_ok is True, f"{d.reason}: {d.detail}"


def test_worktree_gone_without_reclaim_done_refused(world):
    wt = world["wt"]
    _git(world["main_repo"], "worktree", "remove", "--force", str(wt))
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "worktree-gone-no-reclaim-proof"


# ─── pinned parity (FORGE D / FORGE E) — the req3-fixer P0 ───────────────────
# The discharge signal's merge_sha must be bound to THIS task's OWN recorded delivery
# (reclaim._pinned_sha), NOT merely "any commit between the shared spawn base and main".


def test_forge_d_other_task_merged_commit_refused(world):
    """FORGE D: a worker that did NO work points merge_sha at ANOTHER task's already-merged
    commit Q (on origin/main, descends from the shared spawn base). The OLD base-only lower
    bound PASSES Q (it descends from base AND is merged); the pinned binding REFUSES because
    Q is not THIS task's recorded delivery (old_ready commit_hash stays C1)."""
    origin, wt = world["origin"], world["wt"]
    # Q: another task's merged commit, advanced on origin/main beyond C1 (descends from base).
    (origin / "g.txt").write_text("another task's merged work\n")
    _git(origin, "add", "g.txt")
    _git(origin, "commit", "-q", "-m", "Q another task merged")
    q = _git(origin, "rev-parse", "HEAD")
    assert q != world["c1"]
    # The worker resets its OWN worktree HEAD to Q (brief §1 FORGE D) so the live-head-drift
    # check PASSES — on the pre-fix base-only gate this closes (Q descends from base + merged
    # + live HEAD == merge_sha); ONLY the pinned binding catches that Q is not this task's
    # delivery. this task's pinned delivery stays C1 (old_ready commit_hash unchanged).
    _git(wt, "fetch", "origin")
    _git(wt, "reset", "--hard", q)
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=q, worktree_head=q, nonce=NONCE)
    d = _gate(world)
    assert d.close_ok is False, "FORGE D must be refused, got close_ok"
    assert d.reason == "merge-sha-not-pinned", f"got {d.reason}: {d.detail}"
    assert world["c1"][:8] in d.detail  # detail names this task's real pinned commit


def test_forge_e_reclaim_done_over_unmerged_pinned_refused(world):
    """FORGE E: worker advances to an UNMERGED commit C2, removes its worktree, and forges
    ack/<task>.reclaim_done to skip the live-head drift check. The reclaim_done marker is NOT
    terminal proof alone — the gate RE-VERIFIES the pinned commit is merged (runs before the
    worktree-existence branch) and refuses an unmerged commit (mirror reclaim never trusting
    a marker)."""
    wt = world["wt"]
    (wt / "f.txt").write_text("unmerged work\n")
    _git(wt, "add", "f.txt")
    _git(wt, "config", "user.email", "t@t.test")
    _git(wt, "config", "user.name", "t")
    _git(wt, "config", "commit.gpgsign", "false")
    _git(wt, "commit", "-q", "-m", "C2 unmerged")
    c2 = _git(wt, "rev-parse", "HEAD")
    # pinned == merge_sha == C2 (passes the local pinned-equality binding), but C2 was NEVER
    # merged to origin/main.
    _write_pinned(world["ack"], TASK, c2)
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=c2, worktree_head=c2, nonce=NONCE)
    # remove the worktree + FORGE the §6c terminal marker.
    _git(world["main_repo"], "worktree", "remove", "--force", str(wt))
    (world["ack"] / f"{TASK}.reclaim_done").write_text(json.dumps({"task": TASK}))
    d = _gate(world)
    assert d.close_ok is False, "FORGE E must be refused — reclaim_done must not bypass merged"
    assert d.reason == "not-merged", f"got {d.reason}: {d.detail}"


def test_pinned_unresolvable_refused(world):
    """No recorded closing head (no old_ready, no head.json) → REFUSE rather than fall back
    to the shared spawn base (the FORGE D root). A discharge for a window with no independent
    delivery record can never be trusted."""
    (world["ack"] / f"{TASK}.old_ready").unlink()
    hj = world["ack"] / f"{TASK}.head.json"
    if hj.exists():
        hj.unlink()
    d = _gate(world)
    assert d.close_ok is False
    assert d.reason == "pinned-unresolvable", f"got {d.reason}: {d.detail}"


def test_pinned_from_head_json_fallback_closes(world):
    """reclaim parity: _pinned_sha falls back to ack/<task>.head.json head_sha when
    old_ready is absent — still binds merge_sha to THIS task's delivery → close_ok."""
    (world["ack"] / f"{TASK}.old_ready").unlink()
    (world["ack"] / f"{TASK}.head.json").write_text(json.dumps({"head_sha": world["c1"]}))
    d = _gate(world)
    assert d.close_ok is True, f"{d.reason}: {d.detail}"
    assert d.evidence["pinned_sha"] == world["c1"]


def test_pinned_equals_merge_sha_true_positive_closes(world):
    """True-positive parity (the genuine case the binding must NOT block): pinned ==
    merge_sha == merged C1, live HEAD == merge_sha, idle + clean + worker → close_ok, and
    the evidence records the bound pinned commit."""
    d = _gate(world)
    assert d.close_ok is True, f"{d.reason}: {d.detail}"
    assert d.evidence["merge_sha"] == world["c1"]
    assert d.evidence["pinned_sha"] == world["c1"]


# ─── discovery helper ────────────────────────────────────────────────────────


def test_discharged_tasks_lists_signals(world):
    _write_signal(world["ack"], "wk-2", verdict="GREEN", merge_sha=world["c1"],
                  worktree_head=world["c1"], nonce=NONCE)
    assert gate.discharged_tasks(world["cfg"], PROJECT) == [TASK, "wk-2"]


def test_discharged_tasks_empty_when_none(world):
    (world["ack"] / f"{TASK}.audit_discharged").unlink()
    assert gate.discharged_tasks(world["cfg"], PROJECT) == []


# ═══ git-terminal path (signal-FREE reconciler) ══════════════════════════════
# gate_task_git_terminal seeds merge_sha from the task's OWN pinned delivery record
# (reclaim._pinned_sha) instead of reading ack/<task>.audit_discharged. It reuses the SAME
# _evaluate_idle + _corroborate_git, so the SAME 5 fail-safe predicates apply. The happy
# world fixture is already terminal (pinned == worktree HEAD == origin/main C1), so it
# closes WITHOUT a discharge signal; each test mutates ONE thing to drive a refusal.


def _commit_in_worktree(wt: Path, content: str, msg: str) -> str:
    """Advance the worker worktree by one real commit (matches the existing fixtures'
    inline pattern) and return the new HEAD sha."""
    (wt / "f.txt").write_text(content)
    _git(wt, "add", "f.txt")
    _git(wt, "config", "user.email", "t@t.test")
    _git(wt, "config", "user.name", "t")
    _git(wt, "config", "commit.gpgsign", "false")
    _git(wt, "commit", "-q", "-m", msg)
    return _git(wt, "rev-parse", "HEAD")


def _gate_gt(world, *, now_epoch=EPOCH + 7200):
    return gate.gate_task_git_terminal(
        world["cfg"], PROJECT, TASK,
        now_epoch=now_epoch, projects_root=world["projects_root"],
    )


# ─── ✅ closeable via git-terminal, with AND without a discharge signal ───────


def test_git_terminal_happy_path_closes(world):
    d = _gate_gt(world)
    assert d.close_ok is True, f"expected close_ok, got refusal {d.reason}: {d.detail}"
    assert d.reason == gate.CLOSE_OK
    assert d.nonce == NONCE
    assert d.evidence["merge_sha"] == world["c1"]
    assert d.evidence["pinned_sha"] == world["c1"]


def test_git_terminal_closes_without_any_signal(world):
    # The crux: NO audit_discharged signal exists at all, yet the already-merged terminal
    # worker still closes — auto-close now self-triggers without a coordinator hand-run.
    (world["ack"] / f"{TASK}.audit_discharged").unlink()
    d = _gate_gt(world)
    assert d.close_ok is True, f"git-terminal must close without a signal, got {d.reason}: {d.detail}"
    assert d.evidence["merge_sha"] == world["c1"]


# ─── pinned (the merge_sha source) fail-safes ────────────────────────────────


def test_git_terminal_pinned_unresolvable_refused(world):
    # No recorded closing head (no old_ready, no head.json) → REFUSE (never fall back to base).
    (world["ack"] / f"{TASK}.old_ready").unlink()
    hj = world["ack"] / f"{TASK}.head.json"
    if hj.exists():
        hj.unlink()
    d = _gate_gt(world)
    assert d.close_ok is False
    assert d.reason == "pinned-unresolvable", f"got {d.reason}: {d.detail}"


def test_git_terminal_pinned_from_head_json_closes(world):
    # reclaim parity: pinned falls back to head.json head_sha when old_ready is absent.
    (world["ack"] / f"{TASK}.old_ready").unlink()
    (world["ack"] / f"{TASK}.head.json").write_text(json.dumps({"head_sha": world["c1"]}))
    d = _gate_gt(world)
    assert d.close_ok is True, f"{d.reason}: {d.detail}"
    assert d.evidence["pinned_sha"] == world["c1"]


def test_git_terminal_vacuous_refused(world):
    # pinned == spawn base (C0): the branch never advanced (vacuous ancestor) → REFUSE.
    _write_pinned(world["ack"], TASK, world["c0"])
    d = _gate_gt(world)
    assert d.close_ok is False
    assert d.reason == "vacuous-no-advance", f"got {d.reason}: {d.detail}"


# ─── abandoned work MUST be refused (ancestry is the safety proof) ────────────


def test_git_terminal_abandoned_not_merged_refused(world):
    # The worker advanced to a REAL commit C2 that was NEVER merged to origin/main (the
    # "abandoned worker window" case). pinned == C2 → ancestry proves it is NOT merged → REFUSE.
    c2 = _commit_in_worktree(world["wt"], "abandoned unmerged work\n", "C2 unmerged")
    _write_pinned(world["ack"], TASK, c2)
    d = _gate_gt(world)
    assert d.close_ok is False, "abandoned (unmerged) work must NEVER auto-close"
    assert d.reason == "not-merged", f"got {d.reason}: {d.detail}"


def test_git_terminal_live_head_drift_refused(world):
    # pinned claims the merged C1 but the live worktree has moved on to an unmerged C2.
    _commit_in_worktree(world["wt"], "moved on after audit\n", "C2 drift")
    d = _gate_gt(world)  # pinned (old_ready) still C1
    assert d.close_ok is False
    assert d.reason == "worktree-live-head-drift", f"got {d.reason}: {d.detail}"


def test_git_terminal_dirty_refused(world):
    (world["wt"] / "f.txt").write_text("uncommitted edit\n")  # tracked file modified
    d = _gate_gt(world)
    assert d.close_ok is False
    assert d.reason == "dirty", f"got {d.reason}: {d.detail}"


def test_git_terminal_dirty_untracked_refused(world):
    (world["wt"] / "scratch.txt").write_text("untracked junk\n")
    d = _gate_gt(world)
    assert d.close_ok is False
    assert d.reason == "dirty", f"got {d.reason}: {d.detail}"


# ─── identity / in-flight fail-safes (shared with the signal path) ────────────


@pytest.mark.parametrize("role", ["supervisor_succession", "coordinator", "solo", None])
def test_git_terminal_non_worker_refused(world, role):
    sidecar = {"isolation": "worktree", "spawn_nonce": NONCE}
    if role is not None:
        sidecar["role"] = role
    (world["queue"] / f"{TASK}.singlepane").write_text(json.dumps(sidecar))
    d = _gate_gt(world)
    assert d.close_ok is False
    assert d.reason == "not-worker", f"got {d.reason}: {d.detail}"


def test_git_terminal_missing_sidecar_refused(world):
    (world["queue"] / f"{TASK}.singlepane").unlink()
    d = _gate_gt(world)
    assert d.close_ok is False
    assert d.reason == "sidecar-missing"


def test_git_terminal_idle_too_small_refused(world):
    d = _gate_gt(world, now_epoch=EPOCH + 60)  # settled but only 60s idle (< 1800)
    assert d.close_ok is False
    assert d.reason == "not-idle-enough"


@pytest.mark.parametrize("kind", ["running_tool", "blocked_on_question", "dangling_tool_result"])
def test_git_terminal_in_flight_refused(world, kind):
    _write_transcript(world["projects_root"], world["wt"], kind=kind, ts=ISO)
    d = _gate_gt(world)  # long idle, but the last conversation kind is NOT settled
    assert d.close_ok is False
    assert d.reason == f"in-flight:{kind}"


# ─── reconcile_open_worker_windows (PURE: injected windows + parse_title) ─────


def _fake_parse_title_map(mapping):
    """Build a parse_title stub: title → (project, task_id, is_coord, nonce)."""
    def _parse(title):
        return mapping.get(title, (None, None, False, None))
    return _parse


def test_reconcile_open_worker_windows_filters_and_gates(world):
    windows = [
        {"title": "worker-win", "window_number": 100, "desktop": 1},
        {"title": "coord-win", "window_number": 101, "desktop": 1},
        {"title": "ai-titled", "window_number": 102, "desktop": 1},
        {"title": "other-proj-win", "window_number": 103, "desktop": 1},
    ]
    parse = _fake_parse_title_map({
        "worker-win": (PROJECT, TASK, False, NONCE),               # this project's worker → gate
        "coord-win": (PROJECT, "demo-coord", True, "a" * 16),      # coordinator → skip
        "ai-titled": (None, None, False, None),                    # unparseable → skip (manual)
        "other-proj-win": ("other", "wk-9", False, "b" * 16),      # different project → skip
    })
    decisions = gate.reconcile_open_worker_windows(
        world["cfg"], PROJECT, windows, parse,
        now_epoch=EPOCH + 7200, projects_root=world["projects_root"],
    )
    assert len(decisions) == 1, [d.reason for d in decisions]
    d = decisions[0]
    assert d.close_ok is True
    assert d.task == TASK  # annotated for the driver's WID binding
    assert d.nonce == NONCE


def test_reconcile_excludes_refused_windows(world):
    # The window parses to this project's worker, but the gate REFUSES (dirty) → not collected.
    (world["wt"] / "f.txt").write_text("uncommitted\n")
    windows = [{"title": "worker-win", "window_number": 100, "desktop": 1}]
    parse = _fake_parse_title_map({"worker-win": (PROJECT, TASK, False, NONCE)})
    decisions = gate.reconcile_open_worker_windows(
        world["cfg"], PROJECT, windows, parse,
        now_epoch=EPOCH + 7200, projects_root=world["projects_root"],
    )
    assert decisions == []


def test_reconcile_gates_each_task_once(world):
    # Two windows that both parse to the same task → gated once, one decision (no duplicates).
    windows = [
        {"title": "w-a", "window_number": 100, "desktop": 1},
        {"title": "w-b", "window_number": 101, "desktop": 1},
    ]
    parse = _fake_parse_title_map({
        "w-a": (PROJECT, TASK, False, NONCE),
        "w-b": (PROJECT, TASK, False, NONCE),
    })
    decisions = gate.reconcile_open_worker_windows(
        world["cfg"], PROJECT, windows, parse,
        now_epoch=EPOCH + 7200, projects_root=world["projects_root"],
    )
    assert len(decisions) == 1


# ─── reconcile_enabled (DEFAULT-OFF; decoupled from worker-autoclose.enabled) ─


def test_reconcile_enabled_default_off(world, monkeypatch):
    monkeypatch.delenv("HANDOFF_WORKER_AUTOCLOSE_RECONCILE", raising=False)
    assert gate.reconcile_enabled(world["cfg"], PROJECT) is False


def test_reconcile_enabled_env(world, monkeypatch):
    monkeypatch.setenv("HANDOFF_WORKER_AUTOCLOSE_RECONCILE", "1")
    assert gate.reconcile_enabled(world["cfg"], PROJECT) is True


def test_reconcile_enabled_fleet_sentinel(world, monkeypatch):
    monkeypatch.delenv("HANDOFF_WORKER_AUTOCLOSE_RECONCILE", raising=False)
    (world["home"] / "worker-autoclose-reconcile.enabled").write_text("")
    assert gate.reconcile_enabled(world["cfg"], PROJECT) is True


def test_reconcile_enabled_per_project_sentinel(world, monkeypatch):
    monkeypatch.delenv("HANDOFF_WORKER_AUTOCLOSE_RECONCILE", raising=False)
    (world["home"] / PROJECT / "worker-autoclose-reconcile.enabled").write_text("")
    assert gate.reconcile_enabled(world["cfg"], PROJECT) is True


def test_reconcile_enabled_separate_from_signal_switch(world, monkeypatch):
    # the signal-path worker-autoclose.enabled switch must NOT enable the reconciler.
    monkeypatch.delenv("HANDOFF_WORKER_AUTOCLOSE_RECONCILE", raising=False)
    (world["home"] / "worker-autoclose.enabled").write_text("")
    assert gate.reconcile_enabled(world["cfg"], PROJECT) is False
