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
    # A fabricated merge_sha that is not in the repo at all → no ancestry backing.
    bogus = "f" * 40
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=bogus,
                  worktree_head=bogus, nonce=NONCE)
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
    _write_signal(world["ack"], TASK, verdict="GREEN", merge_sha=c2, worktree_head=c2, nonce=NONCE)
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


# ─── discovery helper ────────────────────────────────────────────────────────


def test_discharged_tasks_lists_signals(world):
    _write_signal(world["ack"], "wk-2", verdict="GREEN", merge_sha=world["c1"],
                  worktree_head=world["c1"], nonce=NONCE)
    assert gate.discharged_tasks(world["cfg"], PROJECT) == [TASK, "wk-2"]


def test_discharged_tasks_empty_when_none(world):
    (world["ack"] / f"{TASK}.audit_discharged").unlink()
    assert gate.discharged_tasks(world["cfg"], PROJECT) == []
