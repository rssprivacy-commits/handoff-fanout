"""v4 path-D autoclose watcher tests — Phase 4 role-gated supervisor succession.

Restored from commit ``21dad1b`` (the A-series was dropped with the feature on
2026-05-31) and ADAPTED to the spawn-window-unify Task 4.1 contract:

* The watcher no longer closes "every non-dirty tab"; it emits a role-gated URI
  ``?role=supervisor_succession&predecessor_nonce=<hex>`` consumed by the
  extension's precise self-targeting ``handleAutoclose`` (c2ac814). ``role`` +
  ``predecessor_nonce`` are read from the Phase 2 ``queue/<task>.singlepane`` JSON
  sidecar. A ``role=worker`` spawn (the common case) closes NOTHING.
* ``old_ready.schema_version`` is now ``5.5.0`` (== ``EVIDENCE_SCHEMA_VERSION``),
  not the original ``v5.4.1``.
* The close critical section holds the PROJECT spawn lock
  (``<project>/.spawn.lock``) — same dir + 120s TTL as
  ``handoff_fanout.spawn_lock.project_spawn_lock`` — instead of a per-task lock.

Every test shells out to ``install/auto-continue.sh`` with ``HANDOFF_SKIP_SPAWN=1``
and a tmpdir ``HANDOFF_ROOT`` so only the autoclose segment runs. External commands
(``open``, ``osascript``) are stubbed to record their argv to a sink file.

The follow-up overdue (V-series) + ``old_ready`` writer (D3) cases live in
``test_overdue_and_old_ready.py`` (kept when the A-series was removed).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from handoff_fanout import handoff_precheck

# ─── locate the script under test ───────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install" / "auto-continue.sh"

PROJECT = "demo"
TASK = "demo-task"

# Two distinct, well-formed 16-hex spawn nonces (secrets.token_hex(8) shape).
NEW_NONCE = "a1b2c3d4e5f60718"  # the successor window's spawn_nonce → URI `nonce`
PRED_NONCE = "0011223344556677"  # the predecessor window's spawn_nonce → URI `predecessor_nonce`


# ─── helpers ────────────────────────────────────────────────────────────────


def _write_stub(path: Path, sink: Path, *, exit_code: int = 0) -> None:
    """Create a stub command that appends its argv to ``sink``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sink}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_evidence(
    home: Path, task: str = TASK, project: str = PROJECT, *, nonce: str | None = None
) -> Path:
    """Generate a valid retro evidence file via the real precheck builder."""
    payload = handoff_precheck.build_evidence(
        task_id=task,
        project=project,
        workspace=Path("/tmp"),  # cwd contents irrelevant for the gate
        nonce=nonce,
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    )
    out = home / project / "precheck" / f"{task}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def _write_old_ready(
    home: Path,
    task: str,
    evidence_file: Path,
    *,
    project: str = PROJECT,
    nonce: str = "demo-nonce",
    schema_version: str = handoff_precheck.EVIDENCE_SCHEMA_VERSION,  # "5.5.0"
    override_hash: str | None = None,
    rel_path: str | None = None,
) -> Path:
    ack_dir = home / project / "ack"
    ack_dir.mkdir(parents=True, exist_ok=True)
    out = ack_dir / f"{task}.old_ready"
    file_hash = override_hash or hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    if rel_path is None:
        rel_path = str(evidence_file.relative_to(home / project))
    payload = {
        "schema_version": schema_version,
        "task_id": task,
        "nonce": nonce,
        "session_id": "fp-test-session",
        "session_id_kind": "fallback-fingerprint",
        "commit_hash": "abc1234",
        "push_completed_at": "2026-05-29T10:00:00+00:00",
        "tests_passed": True,
        "memory_updated": True,
        "dump_success": True,
        "retro_evidence_hash": file_hash,
        "retro_evidence_path": rel_path,
        "retro_evidence_path_absolute": str(evidence_file),
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _write_singlepane_sidecar(
    home: Path,
    task: str,
    *,
    project: str = PROJECT,
    role: str = "supervisor_succession",
    predecessor_nonce: str | None = PRED_NONCE,
    spawn_nonce: str = NEW_NONCE,
    close_policy: str = "keep",
) -> Path:
    """Write the Phase 2 ``queue/<task>.singlepane`` JSON sidecar EXACTLY as
    ``dump.maybe_write_singlepane_sidecar`` does (compact one-line JSON), so the
    watcher's ``json_get`` is exercised against the real production shape."""
    queue = home / project / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    sidecar = queue / f"{task}.singlepane"
    ws = home / project / "singlepane" / f"{task}.handoff.code-workspace"
    sidecar.write_text(
        json.dumps(
            {
                "workspace": str(ws),
                "role": role,
                "close_policy": close_policy,
                "spawn_nonce": spawn_nonce,
                "predecessor_nonce": predecessor_nonce,
            }
        ),
        encoding="utf-8",
    )
    return sidecar


def _touch_submitted(home: Path, task: str, project: str = PROJECT) -> Path:
    ack = home / project / "ack"
    ack.mkdir(parents=True, exist_ok=True)
    f = ack / f"{task}.submitted"
    f.write_text("2026-05-29 10:00:00\nstubbed submit\n")
    return f


def _full_succession(home: Path, *, task: str = TASK) -> None:
    """The complete happy-path on-disk state for a supervisor-succession autoclose:
    valid retro evidence + old_ready + a submitted successor + a succession sidecar."""
    evidence = _make_evidence(home, task)
    _write_old_ready(home, task, evidence)
    _write_singlepane_sidecar(home, task)
    _touch_submitted(home, task)


def _stubbed_env(home: Path, tmp_path: Path, *, autoclose: bool = True) -> dict[str, str]:
    """Build the env that points every external dependency at a stub."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    open_sink = tmp_path / "open.log"
    osascript_sink = tmp_path / "osascript.log"
    open_stub = stub_dir / "open"
    osascript_stub = stub_dir / "osascript"
    _write_stub(open_stub, open_sink)
    _write_stub(osascript_stub, osascript_sink)
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(open_stub),
            "HANDOFF_OSASCRIPT_CMD": str(osascript_stub),
            "HANDOFF_SKIP_SPAWN": "1",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "1" if autoclose else "0",
        },
    )
    # Hide the test runner's own HANDOFF_HOME so the script doesn't see it.
    env.pop("HANDOFF_HOME", None)
    env["_OPEN_SINK"] = str(open_sink)
    env["_OSASCRIPT_SINK"] = str(osascript_sink)
    return env


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def _open_log(env: dict[str, str]) -> str:
    sink = Path(env["_OPEN_SINK"])
    return sink.read_text() if sink.exists() else ""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Tmp ``~/.claude-handoff`` for one test."""
    root = tmp_path / "claude-handoff"
    root.mkdir()
    (root / PROJECT).mkdir()
    (root / PROJECT / "queue").mkdir()
    (root / PROJECT / "ack").mkdir()
    (root / PROJECT / "precheck").mkdir()
    return root


@pytest.fixture
def stubbed_env(home: Path, tmp_path: Path) -> dict[str, str]:
    return _stubbed_env(home, tmp_path)


# ─── A-01 happy path — full succession state triggers the role-gated URI ─────


def test_A01_full_succession_triggers_autoclose(home, stubbed_env):
    _full_succession(home)

    proc = _run_script(stubbed_env)
    assert proc.returncode == 0, proc.stderr

    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    ack_listing = list((home / PROJECT / "ack").iterdir())
    assert done.exists(), f"expected autoclose_done; ack: {ack_listing}"
    assert not failed.exists()
    log = _open_log(stubbed_env)
    assert "vscode://dharmaxis.handoff-helper/autoclose" in log
    assert f"task_id={TASK}" in log
    assert "role=supervisor_succession" in log
    assert f"predecessor_nonce={PRED_NONCE}" in log
    assert f"nonce={NEW_NONCE}" in log  # successor's own spawn_nonce → URI `nonce`


# ─── A-02 the URI carries both nonces in the canonical contract order ────────


def test_A02_uri_carries_role_and_both_nonces(home, stubbed_env):
    """A regression in the role / predecessor_nonce / nonce wiring must fail loudly."""
    _full_succession(home)
    _run_script(stubbed_env)
    log = _open_log(stubbed_env)
    assert (
        f"task_id={TASK}&nonce={NEW_NONCE}&project={PROJECT}"
        f"&role=supervisor_succession&predecessor_nonce={PRED_NONCE}" in log
    )


# ─── role gate: worker spawn closes NOTHING (no marker, no URI) ──────────────


def test_role_worker_never_closes(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _write_singlepane_sidecar(home, TASK, role="worker", predecessor_nonce=None)
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()  # NOT a failure — workers keep
    assert "task_id" not in _open_log(stubbed_env)


# ─── role gate: no singlepane sidecar at all → fail-closed silent skip ───────


def test_no_sidecar_skips_silently(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _touch_submitted(home, TASK)
    # No sidecar written — role unknowable → never close, but not a failure either.

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()
    assert "task_id" not in _open_log(stubbed_env)


# ─── sentinel fail-closed (Task 5.2 / design §7): corrupt sidecar JSON → no close ─


def test_corrupt_sidecar_json_fails_closed_no_close(home, stubbed_env):
    """The 损坏 branch for the autoclose read point: a ``queue/<task>.singlepane`` sidecar
    that EXISTS but holds CORRUPT JSON (truncated / disk garbage) → ``json_get`` can't
    extract a ``role`` → role != supervisor_succession → fail-closed SILENT skip. The pane
    keeps; no helper URI fires; no done/failed marker (a valid sidecar may land later)."""
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _touch_submitted(home, TASK)
    # Corrupt sidecar: not parseable JSON, no readable "role" field.
    (home / PROJECT / "queue" / f"{TASK}.singlepane").write_text(
        '{"workspace":"/x","ro\x00\x00 TRUNCATED-CORRUPT', encoding="utf-8"
    )

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert not (ack / f"{TASK}.autoclose_done").exists()  # never closed
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()  # silent skip, not a failure
    assert "task_id" not in _open_log(stubbed_env)  # no URI emitted


def test_corrupt_sidecar_role_present_but_garbage_value_no_close(home, stubbed_env):
    """Even when the corrupt sidecar happens to contain a ``role`` token, a garbage value
    (not exactly ``supervisor_succession``) must fail closed — never coerced into a close."""
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _touch_submitted(home, TASK)
    (home / PROJECT / "queue" / f"{TASK}.singlepane").write_text(
        '{"role": "supervisor_succ\x00GARBAGE', encoding="utf-8"
    )

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert "task_id" not in _open_log(stubbed_env)


# ─── role gate: succession with a malformed predecessor_nonce → reject ───────


def test_bad_predecessor_nonce_rejects(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _write_singlepane_sidecar(home, TASK, predecessor_nonce="not-a-valid-hex")
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    assert failed.exists()
    assert "predecessor_nonce_invalid" in failed.read_text()
    assert not done.exists()
    assert "task_id" not in _open_log(stubbed_env)


# ─── A-03 spawned (submitted) not present → watcher skips silently ───────────


def test_A03_no_submitted_marker_no_autoclose(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _write_singlepane_sidecar(home, TASK)
    # Deliberately omit the .submitted ack — the spawn never confirmed.

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()
    assert "task_id" not in _open_log(stubbed_env)


# ─── A-04..A-06 pre-existing failure markers short-circuit (no re-dispatch) ──


@pytest.mark.parametrize(
    "reason", ["open_uri_failed", "retro_evidence_invalid", "schema_version_unknown"]
)
def test_A04_A05_A06_failed_marker_short_circuits(home, stubbed_env, reason):
    _full_succession(home)
    pre_existing = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    pre_existing.write_text(f"task_id: {TASK}\nreason: {reason}\n")

    _run_script(stubbed_env)
    assert "task_id" not in _open_log(stubbed_env)  # never re-dispatched
    assert not (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()
    assert reason in pre_existing.read_text()  # marker untouched


# ─── A-07 project spawn lock — concurrent runs only emit one autoclose ───────


def test_A07_spawn_lock_serializes_two_runs(home, tmp_path, stubbed_env):
    _full_succession(home)

    # Slow the `open` stub so the lock is genuinely held across the URI fire.
    open_stub = Path(stubbed_env["HANDOFF_OPEN_CMD"])
    open_stub.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_OPEN_SINK"\nsleep 0.5\nexit 0\n',
    )
    open_stub.chmod(0o755)

    proc_a = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)], env=stubbed_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(0.05)  # ensure proc_a acquires the lock first
    proc_b = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)], env=stubbed_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    proc_a.wait(timeout=20)
    proc_b.wait(timeout=20)

    assert (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()
    # Exactly one URI dispatched even though two scripts raced.
    log = _open_log(stubbed_env)
    assert log.count("task_id=") == 1, log


# ─── R2 lock-order TOCTOU — sidecar read + evidence gate + URI emit are ONE
#     atomic critical section under the spawn lock (Task 4.1 fix1) ──────────────


def test_autoclose_sidecar_rewrite_during_critical_section(home, tmp_path, stubbed_env):
    """R2 lock-order regression: a concurrent spawn-intent rewriting the sidecar must
    NOT let the producer emit a torn / stale ``predecessor_nonce``.

    The fix acquires the PROJECT spawn lock BEFORE reading the sidecar / old_ready /
    evidence, so the role read, the retro-evidence gate, and the URI emit are a single
    atomic critical section. We prove the gate executes UNDER the lock by stubbing the
    sha256 helper: ``sha256_file`` is called by the evidence gate, which sits BETWEEN the
    sidecar read and the URI emit. The stub (1) records whether ``.spawn.lock`` is held at
    call time and (2) injects a racing rewrite of the sidecar's ``predecessor_nonce``
    (P1 → P2), then (3) emits the REAL digest so the evidence gate still passes.

    Pre-fix the gate ran BEFORE the lock was taken → the probe would read ``free`` and the
    racing rewrite would slip into the read→emit window. After the fix the probe reads
    ``held`` and the emitted URI carries the lock-consistent value snapshotted at the top of
    the critical section (P1) — never the value the mid-section rewrite tried to smuggle in.
    """
    _full_succession(home)  # sidecar predecessor_nonce = PRED_NONCE (P1)

    lock_dir = home / PROJECT / ".spawn.lock"
    sidecar = home / PROJECT / "queue" / f"{TASK}.singlepane"
    probe = tmp_path / "lock_probe.txt"
    rewrote = tmp_path / "rewrote.flag"
    p2_nonce = "8899aabbccddeeff"  # the racing rewrite's (different, well-formed) nonce
    # The racing rewrite the stub drops in mid-critical-section — a fresh succession
    # sidecar carrying P2 instead of P1. Written via a file (no shell quoting of JSON).
    rewritten = tmp_path / "rewritten_sidecar.json"
    rewritten.write_text(
        json.dumps(
            {
                "workspace": str(home / PROJECT / "singlepane" / f"{TASK}.handoff.code-workspace"),
                "role": "supervisor_succession",
                "close_policy": "keep",
                "spawn_nonce": NEW_NONCE,
                "predecessor_nonce": p2_nonce,
            }
        )
    )

    # A `shasum` stub. The evidence gate calls it as `shasum -a 256 <file>` (sha256_file
    # gates on basename == "shasum"); the early drift self-check calls it bare as
    # `shasum <path>` — delegate those unchanged so the script behaves normally.
    shasum_stub = tmp_path / "stubs" / "shasum"
    shasum_stub.parent.mkdir(parents=True, exist_ok=True)
    shasum_stub.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-a" ]; then\n'
        f'    if [ -d "{lock_dir}" ]; then echo held > "{probe}"; else echo free > "{probe}"; fi\n'
        f'    if [ ! -f "{rewrote}" ]; then cp "{rewritten}" "{sidecar}"; : > "{rewrote}"; fi\n'
        '    /usr/bin/shasum -a 256 "$3"\n'
        "else\n"
        '    /usr/bin/shasum "$@"\n'
        "fi\n",
        encoding="utf-8",
    )
    shasum_stub.chmod(0o755)
    stubbed_env["HANDOFF_SHA256_CMD"] = str(shasum_stub)

    proc = _run_script(stubbed_env)
    assert proc.returncode == 0, proc.stderr

    # 1) The evidence gate (and therefore the sidecar read just above it) executed UNDER
    #    the spawn lock. Pre-fix this ran before the lock was acquired → probe == "free".
    assert probe.read_text().strip() == "held", (
        "evidence gate ran OUTSIDE the spawn lock — read→emit is not a single critical "
        "section (R2 lock-order TOCTOU not fixed)"
    )

    # 2) The racing rewrite really fired mid-critical-section (guard the test itself).
    assert rewrote.exists(), "stub barrier never ran — test would be vacuous"
    assert json.loads(sidecar.read_text())["predecessor_nonce"] == p2_nonce

    # 3) Exactly one URI dispatched, carrying the lock-consistent value (P1) — never the
    #    value the mid-section rewrite smuggled in (P2). No torn / stale emit.
    log = _open_log(stubbed_env)
    assert log.count("task_id=") == 1, log
    assert f"predecessor_nonce={PRED_NONCE}" in log
    assert f"predecessor_nonce={p2_nonce}" not in log

    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert done.exists()
    assert not failed.exists()
    assert f"predecessor_nonce: {PRED_NONCE}" in done.read_text()


# ─── A-08 stale project spawn lock self-clean (TTL 120s) ─────────────────────


def test_A08_stale_spawn_lock_is_recycled(home, stubbed_env):
    _full_succession(home)
    # Stamp a stale .spawn.lock (10 minutes old) — older than the 120s TTL.
    stale = home / PROJECT / ".spawn.lock"
    stale.mkdir()
    old = time.time() - 600
    os.utime(stale, (old, old))

    _run_script(stubbed_env)
    assert (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()


# ─── A-09 retro_evidence_hash tampered → reject ──────────────────────────────


def test_A09_evidence_hash_mismatch_rejects(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence, override_hash="0" * 64)
    _write_singlepane_sidecar(home, TASK)
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert failed.exists()
    assert "retro_evidence_invalid" in failed.read_text()
    assert not (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()
    assert "task_id" not in _open_log(stubbed_env)


# ─── A-10 missing retro_evidence file → reject ───────────────────────────────


def test_A10_missing_evidence_file_rejects(home, stubbed_env):
    fake = home / PROJECT / "precheck" / "missing.retro.evidence.json"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("{}")  # write so we can compute a hash
    _write_old_ready(home, TASK, fake)
    fake.unlink()  # delete after old_ready so both resolved paths point at nothing
    _write_singlepane_sidecar(home, TASK)
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert failed.exists()
    assert "missing_retro_evidence" in failed.read_text()


# ─── A-11 BLOCKED.md present → watcher skip (no URI, no marker) ───────────────


def test_A11_BLOCKED_md_skips_autoclose(home, stubbed_env):
    _full_succession(home)
    blocked = home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    blocked.write_text("# BLOCKED — manual hold\n")

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert "task_id" not in _open_log(stubbed_env)
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()


# ─── A-12 unknown schema_version → reject ────────────────────────────────────


def test_A12_unknown_schema_version_rejects(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence, schema_version="v9.9.9-future")
    _write_singlepane_sidecar(home, TASK)
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert failed.exists()
    assert "schema_version_unknown" in failed.read_text()


# ─── A-13..A-15 pending-intent gate (design §6 临界区①) — an unconsumed
#     queue/<other>.uri is an in-flight spawn intent dispatched by the OLD
#     coordinator; the close must be withheld until it is consumed ─────────────


def test_A13_pending_worker_intent_withholds_close(home, stubbed_env):
    """An unconsumed worker .uri (watchdog has not yet mv'ed it → launched/) must
    withhold the succession's autoclose: no URI, no done/failed marker — SKIP, not
    failure (same semantics as the lock-contention skip)."""
    _full_succession(home)
    (home / PROJECT / "queue" / "infl-worker.uri").write_text("URI=vscode://file/tmp/infl-worker\n")

    _run_script(stubbed_env)
    ack = home / PROJECT / "ack"
    assert "task_id" not in _open_log(stubbed_env)
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()  # skip, not a failure


def test_A14_intent_consumed_next_tick_fires(home, stubbed_env):
    """Once the in-flight intent is consumed (mv → launched/, exactly what the
    watchdog spawn segment does), the next tick fires the close normally."""
    _full_succession(home)
    inflight = home / PROJECT / "queue" / "infl-worker.uri"
    inflight.write_text("URI=vscode://file/tmp/infl-worker\n")

    _run_script(stubbed_env)
    assert "task_id" not in _open_log(stubbed_env)  # tick 1: withheld

    launched = home / PROJECT / "launched"
    launched.mkdir(exist_ok=True)
    inflight.rename(launched / "infl-worker-consumed.txt")

    _run_script(stubbed_env)  # tick 2: intent consumed → close proceeds
    assert (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()
    assert _open_log(stubbed_env).count("task_id=") == 1


def test_A15_own_residual_uri_does_not_deadlock(home, stubbed_env):
    """The succession's OWN residual queue/<task>.uri (e.g. the spawn segment has
    not consumed it yet when the autoclose segment runs) must NOT withhold its own
    close — gating on it would deadlock the very succession it belongs to."""
    _full_succession(home)
    (home / PROJECT / "queue" / f"{TASK}.uri").write_text("URI=vscode://file/tmp/own\n")

    _run_script(stubbed_env)
    assert (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()
    assert _open_log(stubbed_env).count("task_id=") == 1


# ─── default-OFF guard — no env, no sentinel → segment short-circuits ─────────


def test_autoclose_disabled_by_default_no_helper_call(home, tmp_path):
    env = _stubbed_env(home, tmp_path, autoclose=False)
    _full_succession(home)
    _run_script(env)
    ack = home / PROJECT / "ack"
    assert not (ack / f"{TASK}.autoclose_done").exists()
    assert not (ack / f"{TASK}.autoclose_failed.txt").exists()
    assert "task_id" not in _open_log(env)


# ─── opt-in via per-project sentinel file (no env) ───────────────────────────


def test_autoclose_enabled_via_project_sentinel(home, tmp_path):
    env = _stubbed_env(home, tmp_path, autoclose=False)  # env OFF
    (home / PROJECT / "autoclose.enabled").write_text("")  # but project sentinel ON
    _full_succession(home)
    _run_script(env)
    assert (home / PROJECT / "ack" / f"{TASK}.autoclose_done").exists()
    assert "role=supervisor_succession" in _open_log(env)
