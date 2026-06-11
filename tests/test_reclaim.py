"""§6c worker-worktree reclaim (contract v4) — producer + eligibility + CLI tests.

Covers the 12-MUST map in the implementation plan, including P0 #1-#4 from the
contract's mandatory test list (P0 #5 — late close rejected with
``close-command-expired`` — lives extension-side in
``extension/test/handoffReclaim.test.ts``).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from handoff_fanout import config as _config
from handoff_fanout import reclaim, spawn, worktree
from handoff_fanout.spawn_lock import LockHeld, project_spawn_lock

PROJECT = "proj"
TASK = "sw-w1"
NONCE = "0123456789abcdef"
RUN_ID = "00aa11bb22cc33dd"


def _run(args: list[str], cwd: Path) -> str:
    p = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return p.stdout.strip()


def _git(args: list[str], cwd: Path) -> str:
    return _run(["git", *args], cwd)


def _commit_file(repo: Path, name: str, content: str = "x") -> str:
    (repo / name).write_text(content)
    _git(["add", name], repo)
    _git(["commit", "-q", "-m", f"add {name}"], repo)
    return _git(["rev-parse", "HEAD"], repo)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A real origin (bare) + project repo + handoff home. Since the A-poll revision
    (2026-06-12) the producer no longer PUSHES a close URI — it writes the
    ``reclaim_pending`` authorization that the TARGET window's extension polls — so the
    "close authorized" signal is the pending file (``_pending``), not a fired URI. No
    probe by default in callers (tests pass an explicit adapter)."""
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-q", str(origin)], tmp_path)
    repo = ws_root / PROJECT
    repo.mkdir()
    _git(["init", "-q", "--initial-branch=main"], repo)
    _git(["config", "user.email", "t@t.local"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    _commit_file(repo, "README.md", "hello")
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "main"], repo)

    (home / "config.json").write_text(json.dumps({"workspace_root": str(ws_root)}))
    cfg = _config.load()

    claude_root = tmp_path / "claude-projects"
    claude_root.mkdir()
    probe = reclaim.TranscriptProbeAdapter(idle_sec=600.0, projects_root=claude_root)

    return SimpleNamespace(
        home=home,
        cfg=cfg,
        repo=repo,
        origin=origin,
        probe=probe,
        claude_root=claude_root,
        tmp=tmp_path,
    )


def _make_worker(
    env: SimpleNamespace,
    task: str = TASK,
    *,
    nonce: str = NONCE,
    commit: bool = True,
    merge_to_main: bool = True,
    record: str = "head.json",
    wave_id: str | None = None,
) -> Path:
    """Create a worker worktree + sidecar; optionally commit work, merge it to origin
    main (ff), and record the closing head SHA via the requested evidence channel."""
    wt = worktree.worktree_path(env.cfg, PROJECT, task)
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-q", "-b", f"handoff/{task}", str(wt), "origin/main"], env.repo)
    if commit:
        _commit_file(wt, f"{task}.txt", "work")
    if merge_to_main:
        _git(["push", "-q", "origin", "HEAD:main"], wt)
    head = _git(["rev-parse", "HEAD"], wt)
    queue = env.cfg.queue_dir(PROJECT)
    queue.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "workspace": str(wt),
        "role": "worker",
        "close_policy": "keep",
        "spawn_nonce": nonce,
        "isolation": "worktree",
        "predecessor_nonce": None,
    }
    if wave_id is not None:
        sidecar["wave_id"] = wave_id
    (queue / f"{task}.singlepane").write_text(json.dumps(sidecar))
    ack = env.cfg.ack_dir(PROJECT)
    ack.mkdir(parents=True, exist_ok=True)
    if record == "head.json":
        (ack / f"{task}.head.json").write_text(json.dumps({"head_sha": head}))
    elif record == "old_ready":
        (ack / f"{task}.old_ready").write_text(json.dumps({"commit_hash": head}))
    _make_transcript(env, wt, age_sec=3600)  # dead by default (idle > 600s)
    return wt


def _make_transcript(env: SimpleNamespace, wt: Path, *, age_sec: float) -> Path:
    d = env.claude_root / reclaim.transcript_project_dir_name(wt)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "session.jsonl"
    f.write_text("{}\n")
    old = time.time() - age_sec
    os.utime(f, (old, old))
    return f


def _request(env: SimpleNamespace, request_id: str = TASK, run_id: str = RUN_ID) -> Path:
    ack = env.cfg.ack_dir(PROJECT)
    ack.mkdir(parents=True, exist_ok=True)
    p = ack / f"{request_id}.reclaim_requested"
    p.write_text(json.dumps({"run_id": run_id, "ts": reclaim._now_iso()}))
    return p


def _tick(env: SimpleNamespace) -> int:
    return reclaim.tick(env.cfg, probe=env.probe)


def _failed(env: SimpleNamespace, task: str = TASK) -> dict | None:
    p = reclaim.failed_path(env.cfg, PROJECT, task)
    return json.loads(p.read_text()) if p.exists() else None


def _done(env: SimpleNamespace, task: str = TASK) -> dict | None:
    p = reclaim.done_path(env.cfg, PROJECT, task)
    return json.loads(p.read_text()) if p.exists() else None


def _pending(env: SimpleNamespace, task: str = TASK) -> dict | None:
    p = reclaim.pending_path(env.cfg, PROJECT, task)
    return json.loads(p.read_text()) if p.exists() else None


def _ext_ack(env: SimpleNamespace, task: str = TASK, *, run_id: str, result: str, reason=None):
    payload = {"task": task, "run_id": run_id, "result": result, "ts": reclaim._now_iso()}
    if reason:
        payload["reason"] = reason
    reclaim.ack_file_path(env.cfg, PROJECT, task).write_text(json.dumps(payload))


# ─── C7 enum ─────────────────────────────────────────────────────────────────────


def test_reason_enum_complete_18():
    assert len(reclaim.REASONS) == 18
    assert len(set(reclaim.REASONS)) == 18
    for r in (
        "ref-fetch-failed", "int-branch-missing", "sha-unresolvable", "not-merged",
        "head-drift", "abandon-invalid", "abandon-sha-mismatch",
        "abandon-authority-invalid", "wave-incomplete", "manifest-missing",
        "live-session", "probe-error", "dirty", "nonce-mismatch", "ack-timeout",
        "close-command-expired", "role-reason-rejected", "stale-request",
    ):
        assert r in reclaim.REASONS


# ─── C2: sentinel-triggered only / replay / staleness / consumption ──────────────


def test_requested_sentinel_required_no_sweep(env):
    _make_worker(env)
    assert _tick(env) == 0  # no sentinel → the watchdog NEVER sweeps on its own
    assert _failed(env) is None and _done(env) is None
    assert _pending(env) is None  # no close authorized (no pending written)


def test_full_merged_reclaim_happy_path(env):
    wt = _make_worker(env)
    _request(env)
    _tick(env)

    # Tick N: reclaim_pending written (the poll authorization — A-poll, no push URI),
    # lock HELD (C6 — no sleep, no release). The pending carries the FULL close-param
    # set the extension reconstructs: role/reason (C7 row), nonce (C3 auth), run_id +
    # issued_at + ack_timeout (C3 freshness — reused via effectiveAckTimeoutMs).
    pending = _pending(env)
    assert pending and pending["run_id"] == RUN_ID
    assert pending["role"] == "worker" and pending["reason"] == "reclaim"
    assert pending["nonce"] == NONCE
    assert pending["issued_at"] and pending["ack_timeout"] == 30
    assert (env.home / PROJECT / ".spawn.lock").is_dir()

    # Extension acks done → tick N+1 finalizes: done marker carries the C1 evidence
    # triple, the worktree is reclaimed, the sentinel is consumed, the lock released.
    _ext_ack(env, run_id=RUN_ID, result="done")
    _tick(env)
    done = _done(env)
    assert done and done["run_id"] == RUN_ID
    assert done["pinned_head_sha"] and done["canonical_int_sha"] and done["fetched_at"]
    assert done["worktree_removed"] is True
    assert not wt.exists()
    assert not reclaim.requested_path(env.cfg, PROJECT, TASK).exists()
    assert list(reclaim.processed_dir(env.cfg, PROJECT).iterdir())
    assert not (env.home / PROJECT / ".spawn.lock").exists()
    assert _pending(env) is None


def test_replay_same_run_id_skipped(env):
    _make_worker(env)
    _request(env)
    _tick(env)
    _ext_ack(env, run_id=RUN_ID, result="done")
    _tick(env)
    assert _done(env)
    _request(env, run_id=RUN_ID)  # replayed sentinel, SAME run_id
    _tick(env)
    assert _pending(env) is None  # no second authorization written (replay guard)
    assert not reclaim.requested_path(env.cfg, PROJECT, TASK).exists()  # consumed


def test_stale_sentinel_24h_invalidated(env):
    _make_worker(env)
    sent = _request(env)
    old = time.time() - reclaim.STALE_REQUEST_SECONDS - 60
    os.utime(sent, (old, old))
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "stale-request"
    assert not sent.exists()  # consumed → never re-alerts
    assert _pending(env) is None  # no close authorized (no pending written)


def test_malformed_sentinel_consumed_as_stale(env):
    _make_worker(env)
    p = env.cfg.ack_dir(PROJECT) / f"{TASK}.reclaim_requested"
    p.write_text("not json")
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "stale-request"
    assert not p.exists()


# ─── C1: merged-path gates (incl. P0 #1-#3) ──────────────────────────────────────


def test_p0_1_stale_tracking_ref_not_misjudged(env):
    """P0 #1: a forged/stale local remote-tracking ref claiming the worker head is
    merged must NOT pass — the in-critical-section explicit-refspec fetch resets the
    ref to the TRUE remote state, so ancestry says not-merged."""
    wt = _make_worker(env, merge_to_main=False)  # real origin/main does NOT have the work
    head = _git(["rev-parse", "HEAD"], wt)
    # forge the tracking ref in the main repo to point AT the worker head
    _git(["update-ref", "refs/remotes/origin/main", head], env.repo)
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "not-merged"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_p0_2_forged_old_ready_sha_head_drift(env):
    """P0 #2: a worker self-reporting a MERGED sha in old_ready while its worktree
    HEAD actually differs (hiding commits) → head-drift fail-closed."""
    wt = _make_worker(env, record="none")
    merged_sha = _git(["rev-parse", "refs/remotes/origin/main"], env.repo)
    ack = env.cfg.ack_dir(PROJECT)
    (ack / f"{TASK}.old_ready").write_text(json.dumps({"commit_hash": merged_sha}))
    _commit_file(wt, "sneaky.txt", "post-ready commit")  # HEAD moves past the report
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "head-drift"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_p0_3_head_moved_after_record_head_drift(env):
    """P0 #3: head recorded at ready, then the worktree gains another commit →
    the recorded SHA no longer equals the actual HEAD → head-drift."""
    wt = _make_worker(env)  # records head.json at the merged head
    _commit_file(wt, "late.txt", "after ready")
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "head-drift"


def test_missing_recorded_sha_fail_closed(env):
    _make_worker(env, record="none")
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "sha-unresolvable"


def test_old_ready_wins_over_head_json(env):
    """Pinned-SHA source order: old_ready.commit_hash > head.json."""
    wt = _make_worker(env)  # head.json = true head
    ack = env.cfg.ack_dir(PROJECT)
    (ack / f"{TASK}.old_ready").write_text(json.dumps({"commit_hash": "f" * 40}))
    _request(env)
    _tick(env)
    failed = _failed(env)
    # the bogus old_ready value wins the source order and is then caught by the
    # head-drift cross-check — proving precedence AND the forgery gate together.
    assert failed and failed["reason"] == "head-drift"
    assert wt.exists()


def test_canonical_remote_config_wins_over_origin(env):
    """C1 gemini M1: an explicit ``canonical_remote`` beats origin. The work IS on
    origin/main but NOT on the canonical upstream → not-merged."""
    upstream = env.tmp / "upstream.git"
    _run(["git", "init", "--bare", "-q", str(upstream)], env.tmp)
    _git(["remote", "add", "upstream", str(upstream)], env.repo)
    _git(["push", "-q", "upstream", "main"], env.repo)  # upstream has only the base
    (env.home / "config.json").write_text(
        json.dumps({"workspace_root": str(env.repo.parent), "canonical_remote": "upstream"})
    )
    env.cfg = _config.load()
    _make_worker(env)  # merges to ORIGIN main only
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "not-merged"


def test_canonical_remote_unresolvable_fail_closed(env):
    (env.home / "config.json").write_text(
        json.dumps({"workspace_root": str(env.repo.parent), "canonical_remote": "nosuch"})
    )
    env.cfg = _config.load()
    _make_worker(env)
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "ref-fetch-failed"
    assert "canonical remote" in failed["detail"]
    assert _pending(env) is None  # no close authorized (no pending written)


def test_fetch_failure_nonterminal_with_backoff(env):
    """Offline: fetch fails → visible non-terminal failed record + exponential
    backoff; the sentinel STAYS (retry later), and the backoff gate suppresses the
    next tick's fetch storm."""
    _make_worker(env)
    _git(["remote", "set-url", "origin", str(env.tmp / "gone.git")], env.repo)
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "ref-fetch-failed"
    assert failed["terminal"] is False
    assert reclaim.requested_path(env.cfg, PROJECT, TASK).exists()  # not consumed
    assert reclaim._backoff_active(env.cfg, PROJECT)
    assert _pending(env) is None  # fetch failed before any authorization
    _tick(env)  # within the backoff window → no new fetch attempt / no authorization
    assert _pending(env) is None


# ─── C4: abandoned path (incl. P0 #4) ────────────────────────────────────────────


def test_p0_4_forged_worktree_abandon_marker_rejected(env):
    """P0 #4: an in-worktree ``.handoff-abandoned.json`` with NO control-plane record
    (the worker forging its own eligibility) → abandon-authority-invalid."""
    wt = _make_worker(env, merge_to_main=False)
    head = _git(["rev-parse", "HEAD"], wt)
    (wt / ".handoff-abandoned.json").write_text(
        json.dumps({"task": TASK, "reason": "forged", "branch_head_sha": head})
    )
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "abandon-authority-invalid"
    assert _pending(env) is None  # no close authorized (no pending written)
    assert wt.exists()


def test_abandon_cli_grants_eligibility_and_reclaim_force_removes(env):
    wt = _make_worker(env, merge_to_main=False)  # unmerged work — only abandon frees it
    rc = reclaim.cli_abandon([TASK, "--project", PROJECT, "--reason", "replaced-by sw-w2"])
    assert rc == 0
    record = json.loads((reclaim.abandoned_dir(env.cfg, PROJECT) / f"{TASK}.json").read_text())
    assert record["branch_head_sha"] == _git(["rev-parse", "HEAD"], wt)
    assert record["reason"] == "replaced-by sw-w2"
    assert (wt / ".handoff-abandoned.json").exists()  # audit copy
    _request(env)
    _tick(env)
    assert _pending(env) is not None  # eligible via the abandoned path → authorized
    _ext_ack(env, run_id=RUN_ID, result="done")
    _tick(env)
    done = _done(env)
    assert done and done["worktree_removed"] is True
    assert done["removed_head_sha"] == record["branch_head_sha"]  # 复核进 ack (C4)
    assert not wt.exists()


def test_abandon_cli_is_immutable_and_validates(env):
    _make_worker(env, merge_to_main=False)
    assert reclaim.cli_abandon([TASK, "--project", PROJECT, "--reason", "r1"]) == 0
    assert reclaim.cli_abandon([TASK, "--project", PROJECT, "--reason", "r2"]) == 2
    assert reclaim.cli_abandon(["no-such-task", "--project", PROJECT, "--reason", "x"]) == 2


def test_abandon_sha_mismatch_fail_closed(env):
    wt = _make_worker(env, merge_to_main=False)
    assert reclaim.cli_abandon([TASK, "--project", PROJECT, "--reason", "stale"]) == 0
    _commit_file(wt, "post-abandon.txt", "moved")  # HEAD moves after the ruling
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "abandon-sha-mismatch"


def test_abandon_copy_conflict_fail_closed(env):
    wt = _make_worker(env, merge_to_main=False)
    assert reclaim.cli_abandon([TASK, "--project", PROJECT, "--reason", "ok"]) == 0
    copy = json.loads((wt / ".handoff-abandoned.json").read_text())
    copy["branch_head_sha"] = "0" * 40  # tampered audit copy
    (wt / ".handoff-abandoned.json").write_text(json.dumps(copy))
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "abandon-authority-invalid"


def test_abandon_sha_never_feeds_merged_path(env):
    """Dual-path SHA separation (C1 MUST#2): a valid abandon of UNMERGED work must
    reclaim via the abandoned path — the done marker must not claim a merged check."""
    _make_worker(env, merge_to_main=False)
    reclaim.cli_abandon([TASK, "--project", PROJECT, "--reason", "discard"])
    _request(env)
    _tick(env)
    pending = _pending(env)
    assert pending and pending["path"] == "abandoned"
    assert "canonical_int_sha" not in pending["evidence"]  # no merged-path evidence


# ─── identity gates ──────────────────────────────────────────────────────────────


def test_sidecar_missing_fail_closed(env):
    """A request id with its sidecar gone is indistinguishable from a wave id with no
    manifest — both mean 'member set unknown' → C5 fail-closed (manifest-missing).
    C3's per-member 'sidecar 丢失 → fail-closed' reads as nonce-mismatch only when
    membership is otherwise established (the wave-manifest case, covered below)."""
    _make_worker(env)
    (env.cfg.queue_dir(PROJECT) / f"{TASK}.singlepane").unlink()
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "manifest-missing"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_wave_member_sidecar_missing_nonce_mismatch(env):
    """C3: an established wave MEMBER whose sidecar vanished → nonce unobtainable →
    nonce-mismatch fail-closed (membership itself is still known via the manifest)."""
    _make_worker(env, "sw-a", nonce="aaaaaaaaaaaaaaaa")
    assert (
        reclaim.cli_wave_freeze(["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a"])
        == 0
    )
    (env.cfg.queue_dir(PROJECT) / "sw-a.singlepane").unlink()
    _request(env, request_id="wave-1")
    _tick(env)
    failed = _failed(env, "sw-a")
    assert failed and failed["reason"] == "nonce-mismatch"


def test_sidecar_malformed_nonce_rejected(env):
    _make_worker(env, nonce="not-hex!!")
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "nonce-mismatch"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_non_worker_or_non_worktree_matrix_rejected(env):
    _make_worker(env)
    sc = env.cfg.queue_dir(PROJECT) / f"{TASK}.singlepane"
    data = json.loads(sc.read_text())
    data["isolation"] = "singlepane"
    sc.write_text(json.dumps(data))
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "role-reason-rejected"


# ─── dirty + probe gates ─────────────────────────────────────────────────────────


def test_dirty_gate_covers_untracked(env):
    wt = _make_worker(env)
    (wt / "untracked-wip.txt").write_text("not committed")
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "dirty"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_probe_live_session_blocks_close(env):
    wt = _make_worker(env)
    _make_transcript(env, wt, age_sec=10)  # fresh → live
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "live-session"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_probe_missing_dir_probe_error_no_close(env):
    """C6 gemini MUST: transcript dir missing / unreadable / ANY anomaly →
    unconditionally treated alive → never close (distinct probe-error reason)."""
    wt = _make_worker(env)
    d = env.claude_root / reclaim.transcript_project_dir_name(wt)
    for f in d.glob("*"):
        f.unlink()
    d.rmdir()
    _request(env)
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "probe-error"
    assert _pending(env) is None  # no close authorized (no pending written)


def test_probe_disabled_config_skips_probe(env):
    (env.home / "config.json").write_text(
        json.dumps({"workspace_root": str(env.repo.parent), "reclaim_probe_disabled": True})
    )
    env.cfg = _config.load()
    wt = _make_worker(env)
    _make_transcript(env, wt, age_sec=10)  # would be LIVE — but the owner disabled it
    _request(env)
    _tick(env)
    assert _pending(env) is not None  # proceeded (owner accepted the risk explicitly)


# ─── C6: cross-tick state machine + lock discipline ──────────────────────────────


def test_lock_held_across_pending_ticks_and_renewed(env):
    _make_worker(env)
    _request(env)
    _tick(env)
    assert _pending(env)
    # The project spawn lock is HELD → any concurrent spawn intent is excluded.
    with pytest.raises(LockHeld):
        with project_spawn_lock(PROJECT, root=env.cfg.home):
            pass
    # A tick before the deadline keeps holding (renewal, not release).
    _tick(env)
    assert _pending(env)
    assert (env.home / PROJECT / ".spawn.lock").is_dir()


def test_no_sleep_inside_tick(env, monkeypatch):
    """C6 gemini MUST: the watchdog tick must NEVER synchronously sleep — waiting is
    a state transition, not a blocking call."""

    def _no_sleep(_secs):  # pragma: no cover - failure path
        raise AssertionError("tick called time.sleep — forbidden by C6")

    monkeypatch.setattr(time, "sleep", _no_sleep)
    _make_worker(env)
    _request(env)
    _tick(env)  # fire + pending
    _tick(env)  # renew while waiting
    assert _pending(env)


def test_ack_timeout_releases_lock_and_marks(env):
    _make_worker(env)
    _request(env)
    _tick(env)
    pf = reclaim.pending_path(env.cfg, PROJECT, TASK)
    pending = json.loads(pf.read_text())
    pending["deadline_epoch"] = time.time() - 1
    pf.write_text(json.dumps(pending))
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "ack-timeout"
    assert not (env.home / PROJECT / ".spawn.lock").exists()  # released
    assert _pending(env) is None
    assert not reclaim.requested_path(env.cfg, PROJECT, TASK).exists()  # consumed


def test_lock_lost_pending_treated_stale(env):
    """A crashed/TTL-reaped hold: the pending state is cleaned as stale and the
    (possibly rival-owned) lock is NOT touched."""
    _make_worker(env)
    _request(env)
    _tick(env)
    (env.home / PROJECT / ".spawn.lock").rmdir()  # simulate TTL break by a rival
    reclaim._owner_path(env.cfg, PROJECT).unlink()
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "ack-timeout"
    assert "lock lost" in failed["detail"]
    assert _pending(env) is None


def test_extension_failed_ack_recorded_with_enum_reason(env):
    _make_worker(env)
    _request(env)
    _tick(env)
    _ext_ack(env, run_id=RUN_ID, result="failed", reason="dirty")
    _tick(env)
    failed = _failed(env)
    assert failed and failed["reason"] == "dirty"
    assert not (env.home / PROJECT / ".spawn.lock").exists()


def test_stale_ack_with_wrong_run_id_ignored(env):
    """An ack from an OLDER run must not satisfy the current pending state."""
    _make_worker(env)
    _request(env)
    _tick(env)
    _ext_ack(env, run_id="9999999999999999", result="done")
    _tick(env)
    assert _done(env) is None  # not consumed by the wrong-run ack
    assert _pending(env)  # still waiting (within deadline)


# ─── C5: wave manifest ───────────────────────────────────────────────────────────


def test_wave_freeze_oexcl_no_overwrite(env):
    _make_worker(env, "sw-a", nonce="aaaaaaaaaaaaaaaa")
    _make_worker(env, "sw-b", nonce="bbbbbbbbbbbbbbbb")
    rc = reclaim.cli_wave_freeze(
        ["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a,sw-b"]
    )
    assert rc == 0
    members = reclaim.load_manifest(env.cfg, PROJECT, "wave-1")
    assert [m["task_id"] for m in members] == ["sw-a", "sw-b"]
    assert members[0]["spawn_nonce"] == "aaaaaaaaaaaaaaaa"
    # immutable: a second freeze (even with different members) is refused
    rc2 = reclaim.cli_wave_freeze(
        ["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a"]
    )
    assert rc2 == 2
    assert len(reclaim.load_manifest(env.cfg, PROJECT, "wave-1")) == 2


def test_wave_freeze_requires_member_sidecars(env):
    _make_worker(env, "sw-a", nonce="aaaaaaaaaaaaaaaa")
    rc = reclaim.cli_wave_freeze(
        ["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a,sw-ghost"]
    )
    assert rc == 2
    assert not (reclaim.waves_dir(env.cfg, PROJECT) / "wave-1.manifest.json").exists()


def test_manifest_missing_whole_wave_fail_closed(env):
    """A request id with neither a manifest nor a member sidecar = member set
    unknown → the WHOLE wave fails closed (manifest-missing)."""
    _request(env, request_id="wave-ghost")
    _tick(env)
    failed = _failed(env, "wave-ghost")
    assert failed and failed["reason"] == "manifest-missing"
    assert not reclaim.requested_path(env.cfg, PROJECT, "wave-ghost").exists()


def test_corrupt_manifest_whole_wave_fail_closed(env):
    wdir = reclaim.waves_dir(env.cfg, PROJECT)
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "wave-1.manifest.json").write_text("{broken")
    _request(env, request_id="wave-1")
    _tick(env)
    failed = _failed(env, "wave-1")
    assert failed and failed["reason"] == "manifest-missing"


def test_wave_per_member_close_and_late_add_ignored(env):
    _make_worker(env, "sw-a", nonce="aaaaaaaaaaaaaaaa", wave_id="wave-1")
    _make_worker(env, "sw-b", nonce="bbbbbbbbbbbbbbbb", wave_id="wave-1")
    assert (
        reclaim.cli_wave_freeze(
            ["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a,sw-b"]
        )
        == 0
    )
    # a LATE worker claiming the same wave after the freeze — ignored + audited
    _make_worker(env, "sw-late", nonce="cccccccccccccccc", wave_id="wave-1")

    _request(env, request_id="wave-1")
    _tick(env)  # member 1 fired
    assert _pending(env, "sw-a")
    late = json.loads(
        (env.cfg.ack_dir(PROJECT) / "wave-1.reclaim_lateadd.json").read_text()
    )
    assert late["attempted_late_add"] == ["sw-late"]
    _ext_ack(env, "sw-a", run_id=RUN_ID, result="done")
    _tick(env)  # member 1 done → lock released
    assert _done(env, "sw-a")
    _tick(env)  # member 2 fired
    assert _pending(env, "sw-b")
    _ext_ack(env, "sw-b", run_id=RUN_ID, result="done")
    _tick(env)  # member 2 done → wave finalized next entry
    _tick(env)
    wave_done = _done(env, "wave-1")
    assert wave_done and wave_done["members"] == {"sw-a": "done", "sw-b": "done"}
    assert not reclaim.requested_path(env.cfg, PROJECT, "wave-1").exists()
    assert _done(env, "sw-late") is None  # the late add was NEVER closed


def test_wave_incomplete_summary_on_member_failure(env):
    _make_worker(env, "sw-a", nonce="aaaaaaaaaaaaaaaa")
    _make_worker(env, "sw-b", nonce="bbbbbbbbbbbbbbbb", merge_to_main=False)  # not merged
    assert (
        reclaim.cli_wave_freeze(
            ["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a,sw-b"]
        )
        == 0
    )
    _request(env, request_id="wave-1")
    _tick(env)
    _ext_ack(env, "sw-a", run_id=RUN_ID, result="done")
    _tick(env)  # sw-a done
    _tick(env)  # sw-b evaluated → not-merged (terminal)
    failed_b = _failed(env, "sw-b")
    assert failed_b and failed_b["reason"] == "not-merged"
    wave_failed = _failed(env, "wave-1")
    assert wave_failed and wave_failed["reason"] == "wave-incomplete"
    detail = json.loads(wave_failed["detail"])
    assert detail == {"sw-a": "done", "sw-b": "not-merged"}
    assert not reclaim.requested_path(env.cfg, PROJECT, "wave-1").exists()


def test_manifest_nonce_mismatch_member_rejected(env):
    _make_worker(env, "sw-a", nonce="aaaaaaaaaaaaaaaa")
    assert (
        reclaim.cli_wave_freeze(["--project", PROJECT, "--wave-id", "wave-1", "--members", "sw-a"])
        == 0
    )
    sc = env.cfg.queue_dir(PROJECT) / "sw-a.singlepane"
    data = json.loads(sc.read_text())
    data["spawn_nonce"] = "dddddddddddddddd"  # re-spawned after the freeze
    sc.write_text(json.dumps(data))
    _request(env, request_id="wave-1")
    _tick(env)
    failed = _failed(env, "sw-a")
    assert failed and failed["reason"] == "nonce-mismatch"


# ─── CLI: record-head / reclaim-request / reclaim-report ─────────────────────────


def test_record_head_cli(env):
    wt = _make_worker(env, record="none")
    assert reclaim.cli_record_head([TASK, "--project", PROJECT]) == 0
    rec = json.loads((env.cfg.ack_dir(PROJECT) / f"{TASK}.head.json").read_text())
    assert rec["head_sha"] == _git(["rev-parse", "HEAD"], wt)


def test_reclaim_request_cli_generates_run_id(env):
    _make_worker(env)
    assert reclaim.cli_reclaim_request([TASK, "--project", PROJECT]) == 0
    data = json.loads(reclaim.requested_path(env.cfg, PROJECT, TASK).read_text())
    assert reclaim._HEX16_RE.match(data["run_id"])
    assert reclaim.cli_reclaim_request([TASK, "--project", PROJECT]) == 2  # already pending


def test_reclaim_report_read_only(env, capsys):
    _make_worker(env, merge_to_main=False)
    rc = reclaim.cli_reclaim_report(["--project", PROJECT])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{PROJECT}/{TASK}" in out
    assert "no reclaim_requested sentinel" in out
    assert _pending(env) is None  # READ-ONLY: never authorizes a close
    assert not (env.home / PROJECT / ".spawn.lock").exists()


# ─── spawn --wave-id (additive sidecar field) ────────────────────────────────────


def test_spawn_sidecar_wave_id_additive(tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir()
    base = dict(
        workspace="/x",
        role="worker",
        close_policy="keep",
        spawn_nonce=NONCE,
        isolation="worktree",
        predecessor_nonce=None,
    )
    spawn._write_sidecar(queue, "t1", **base)
    spawn._write_sidecar(queue, "t2", **base, wave_id="wave-9")
    d1 = json.loads((queue / "t1.singlepane").read_text())
    d2 = json.loads((queue / "t2.singlepane").read_text())
    assert "wave_id" not in d1  # non-wave sidecar byte-shape unchanged
    assert d2["wave_id"] == "wave-9"
    assert {k: v for k, v in d2.items() if k != "wave_id"} == d1


def test_spawn_wave_id_validation():
    assert (
        spawn.run_spawn(
            project="p1",
            task="t1",
            role="worker",
            isolation="worktree",
            prompt="x",
            wave_id="BAD SLUG",
        )
        == spawn.EXIT_FAIL_CLOSED
    )
    assert (
        spawn.run_spawn(
            project="p1",
            task="t1",
            role="supervisor_succession",
            isolation="singlepane",
            prompt="x",
            wave_id="wave-1",
            predecessor_nonce=NONCE,
            succession_token="/nonexistent",
        )
        == spawn.EXIT_FAIL_CLOSED  # a wave is a WORKER batch — succession rejected
    )


# ─── watchdog wiring ─────────────────────────────────────────────────────────────


def test_watchdog_main_runs_reclaim_tick(env, monkeypatch, capsys):
    from handoff_fanout import watchdog

    monkeypatch.setattr(watchdog, "_notify", lambda *a, **k: None)
    rc = watchdog.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "reclaim-active" in out
