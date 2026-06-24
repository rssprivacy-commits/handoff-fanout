"""v4.1 single-task heartbeat — template injection + watchdog mode 6.

Root cause (主人 5/29 'API Error 会话裸跑'): v4.1 build_handoff_md emitted a
spawn prompt without any heartbeat instruction, so when the new tab wedged
on 529 / API Error there was nothing for the watchdog to notice. mode 4
only covered fan-out sub-tasks.

This module nails the symmetry shut:

  * ``build_handoff_md`` Step 1 now writes ``queue/<task>.heartbeat`` every 60s
  * ``watchdog.scan_single_task_heartbeats`` (mode 6) marks stale heartbeats
    ``queue/<task>.529-suspected``
  * ``watchdog._mark_single_task_529`` (mode 6 enforcement, added 2026-05-29)
    auto-kills the stuck task's processes via SIGTERM → SIGKILL and notifies
    so the operator doesn't have to manually hunt PIDs.

All three halves are exercised here — drift on any side reopens the gap.
"""

from __future__ import annotations

import os
import re as _re
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from handoff_fanout import templates, watchdog

# ─── A: build_handoff_md heartbeat injection ─────────────────────────────────


def _render_handoff(task: str = "fix-foo", project: str = "demo") -> str:
    return templates.build_handoff_md(
        task=task,
        project=project,
        workspace=Path("/tmp/ws"),
        next_brief="do the thing",
        status="active",
        tests=None,
        baseline={"git_head": "abc123", "last_3_commits": "abc123 do thing\n"},
        roadmap_excerpt="(none)",
        inject_blocks=[],
        handoff_home=Path("/home/x/.claude-handoff"),
        handoff_md_path=Path("/home/x/.claude-handoff/demo/queue/fix-foo.md"),
    )


def test_build_handoff_md_contains_heartbeat_step():
    md = _render_handoff()
    assert "第一步: 启动 heartbeat" in md
    assert "/home/x/.claude-handoff/demo/queue/fix-foo.heartbeat" in md
    assert "sleep 60" in md


def test_build_handoff_md_baseline_renumbered_to_step_two():
    md = _render_handoff()
    assert "第二步: Baseline 验证" in md
    assert md.index("第一步: 启动 heartbeat") < md.index("第二步: Baseline 验证")


def test_build_handoff_md_heartbeat_pid_kill_hint():
    """The kill hint matters — without it, sessions leave background pids around."""
    md = _render_handoff(task="task-x")
    assert "/tmp/heartbeat-task-x.pid" in md
    assert "kill $(cat /tmp/heartbeat-task-x.pid)" in md


def test_build_handoff_md_contains_timeout_caveat():
    """§第一步.5 — closes the 5/29 04:05 codex-stuck-19min follow-up.

    Without a proactive ``timeout`` instruction, a hung codex / ``claude -p``
    call freezes the whole session and starves the heartbeat, so watchdog
    mode 6 only ever reacts after the fact. The caveat tells the session to
    bound long-running CLI calls up front; mode 6 stays the passive backstop.
    """
    md = _render_handoff()
    assert "§第一步.5" in md
    assert "timeout 300 codex exec" in md
    # placed between the heartbeat step and baseline — proactive, before work
    assert (
        md.index("第一步: 启动 heartbeat")
        < md.index("§第一步.5")
        < md.index("第二步: Baseline 验证")
    )


def test_build_handoff_md_contains_retrieval_pull_section():
    """retrieval-pull L1: §0.5 guidance section is present, between §0 and the
    heartbeat step, and names the back-reference flag (warn-mode keystone)."""
    md = _render_handoff()
    assert "§0.5 retrieval-pull" in md
    assert "--predecessor-lesson-backref" in md
    # the dispositions the incoming coordinator must use
    assert "已应用" in md
    assert "已被取代" in md
    assert "不相关" in md
    # warn-mode is stated plainly
    assert "warn-mode" in md
    # ordering: after the §0 audit block, before the heartbeat step
    assert (
        md.index("§0 上任审计")
        < md.index("§0.5 retrieval-pull")
        < md.index("第一步: 启动 heartbeat")
    )


def test_build_handoff_md_section_zero_unchanged():
    """The existing §0 audit block must stay intact (only an additive §0.5)."""
    md = _render_handoff()
    assert "§0 上任审计 — 核对前任 retro evidence" in md
    assert "前任无 retro.evidence.json" in md


def _render_sub_task(sub_task_id: str = "sub-foo", project: str = "demo") -> str:
    return templates.build_sub_task_handoff_md(
        task="parent-task",
        project=project,
        workspace=Path("/tmp/ws"),
        next_brief="do the sub thing",
        batch_id="batch-1",
        sub_task_id=sub_task_id,
        file_ownership=[{"type": "glob", "path": "src/foo/**"}],
        baseline={"git_head": "abc123", "last_3_commits": "abc123 do thing\n"},
        roadmap_excerpt="(none)",
        inject_blocks=[],
        handoff_home=Path("/home/x/.claude-handoff"),
        git_guard_path=Path("/home/x/.claude-handoff/demo/batches/batch-1/git-guard"),
    )


def test_build_sub_task_handoff_md_contains_timeout_caveat():
    """Symmetric §第二步.5 — sub-task sessions run pytest / codex too."""
    md = _render_sub_task()
    assert "§第二步.5" in md
    assert "timeout 300 <cmd>" in md
    assert (
        md.index("第二步: 启动 heartbeat")
        < md.index("§第二步.5")
        < md.index("第三步: Baseline 验证")
    )


# ─── B: watchdog mode 6 scan ────────────────────────────────────────────────


def _stale(path: Path, seconds_ago: float) -> None:
    new_t = time.time() - seconds_ago
    os.utime(path, (new_t, new_t))


def _setup_queue(root: Path, project: str = "demo") -> Path:
    queue = root / project / "queue"
    queue.mkdir(parents=True)
    return queue


def test_mode6_marks_529_when_heartbeat_stale(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-a.md").write_text("# task")
    hb = queue / "task-a.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    count = watchdog.scan_single_task_heartbeats()
    assert count == 1
    marker = queue / "task-a.529-suspected"
    assert marker.exists()
    body = marker.read_text()
    assert "task-a" in body
    assert "heartbeat stale" in body


def test_mode6_skips_when_heartbeat_fresh(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-b.md").write_text("# task")
    (queue / "task-b.heartbeat").write_text("")  # fresh mtime

    assert watchdog.scan_single_task_heartbeats() == 0
    assert not (queue / "task-b.529-suspected").exists()


def test_mode6_skips_when_md_missing(isolated_handoff_home):
    """No .md means task is already gone — heartbeat is leftover noise."""
    queue = _setup_queue(isolated_handoff_home)
    hb = queue / "ghost.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0
    assert not (queue / "ghost.529-suspected").exists()


def test_mode6_skips_when_done(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-c.md").write_text("# task")
    (queue / "task-c.done").write_text("")
    hb = queue / "task-c.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0


def test_mode6_skips_when_blocked(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-d.md").write_text("# task")
    (queue / "task-d.BLOCKED.md").write_text("blocked")
    hb = queue / "task-d.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0


def test_mode6_idempotent(isolated_handoff_home):
    """Second scan must not re-flag the same task — atomic_create on marker."""
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-e.md").write_text("# task")
    hb = queue / "task-e.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 1
    assert watchdog.scan_single_task_heartbeats() == 0  # marker exists


def test_mode6_cross_project_independent(isolated_handoff_home):
    qa = _setup_queue(isolated_handoff_home, "proj-a")
    qb = _setup_queue(isolated_handoff_home, "proj-b")
    for q in (qa, qb):
        (q / "t.md").write_text("# t")
        hb = q / "t.heartbeat"
        hb.write_text("")
        _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 2
    assert (qa / "t.529-suspected").exists()
    assert (qb / "t.529-suspected").exists()


def test_mode6_skips_special_dirs(isolated_handoff_home):
    """locks/ and _recovery/ are not project directories."""
    for special in ("locks", "_recovery"):
        d = isolated_handoff_home / special / "queue"
        d.mkdir(parents=True)
        (d / "t.md").write_text("# t")
        hb = d / "t.heartbeat"
        hb.write_text("")
        _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0


# ─── C: watchdog mode 6 enforcement (kill + notify) ─────────────────────────
#
# Match criterion now: ``pgrep -fa <re.escape(<queue>/<task>.heartbeat)>``.
# Result type: ``EnforceResult`` (status + killed/still_alive/permission_denied
# /raced_gone tuples). Verifying after SIGKILL.


_REAL_SUBPROCESS_RUN = subprocess.run  # captured before any test monkeypatch


class _FakePgrep:
    """Stand-in for ``subprocess.run(["pgrep", "-fa", <pattern>])``.

    Recognizes ``pgrep`` by basename so ``/usr/bin/pgrep`` works too.
    Non-pgrep calls fall through to the real ``subprocess.run`` captured
    at module import time — this avoids the "monkeypatched run calls
    itself" recursion.
    """

    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        if isinstance(argv, list | tuple) and argv:
            head = str(argv[0])
            if head == "pgrep" or head.endswith("/pgrep"):
                self.calls.append(list(argv))
                return SimpleNamespace(stdout=self.stdout, stderr="", returncode=self.returncode)
        return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)


class _FakeKill:
    """Stand-in for ``os.kill`` that records signals and simulates outcomes.

    Per-PID outcome knobs:

      * ``alive_after_term`` — PIDs that STAY alive after SIGTERM (probe
        returns) so the grace loop expires and SIGKILL is sent.
      * ``exit_after_kill`` — PIDs that flip to "exited" only after
        SIGKILL is delivered (exercises the SIGKILL-succeeds path).
      * ``exit_after_polls`` — PIDs that flip to "exited" after N
        zero-signal probes (exercises the SIGTERM-kills-mid-grace path).
      * ``missing_pids`` — PIDs that raise ``ProcessLookupError`` on
        every signal (dead-PID race).
      * ``permission_pids`` — PIDs that raise ``PermissionError`` on
        every signal (sandbox / setuid).
    """

    def __init__(
        self,
        *,
        alive_after_term: set[int] | None = None,
        exit_after_kill: set[int] | None = None,
        exit_after_polls: dict[int, int] | None = None,
        missing_pids: set[int] | None = None,
        permission_pids: set[int] | None = None,
    ):
        self.alive_after_term = set(alive_after_term or ())
        self.exit_after_kill = set(exit_after_kill or ())
        self.exit_after_polls = dict(exit_after_polls or {})
        self.missing_pids = set(missing_pids or ())
        self.permission_pids = set(permission_pids or ())
        self.signals: list[tuple[int, int]] = []
        self._poll_counts: dict[int, int] = {}
        self._sigkilled: set[int] = set()

    def __call__(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if pid in self.permission_pids:
            raise PermissionError(pid)
        if pid in self.missing_pids:
            raise ProcessLookupError(pid)
        if sig == signal.SIGKILL:
            self._sigkilled.add(pid)
            return
        if sig == signal.SIGTERM:
            return
        # sig == 0: liveness probe
        self._poll_counts[pid] = self._poll_counts.get(pid, 0) + 1
        scheduled = self.exit_after_polls.get(pid)
        if scheduled is not None and self._poll_counts[pid] >= scheduled:
            raise ProcessLookupError(pid)
        if pid in self._sigkilled and pid in self.exit_after_kill:
            raise ProcessLookupError(pid)
        if pid in self.alive_after_term:
            return  # still alive — keep polling
        raise ProcessLookupError(pid)  # default: clean SIGTERM exit


def _setup_stale_task(root: Path, task_id: str, project: str = "demo") -> Path:
    queue = root / project / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / f"{task_id}.md").write_text("# task")
    hb = queue / f"{task_id}.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)
    return queue


def _heartbeat_cmdline(queue: Path, task_id: str, pid: int, extra: str = "") -> str:
    """Realistic pgrep -fa line whose cmdline contains the heartbeat path."""
    hb = queue / f"{task_id}.heartbeat"
    tail = f"  # {extra}" if extra else ""
    return f"{pid} bash -c while true; do touch {hb}; sleep 60; done{tail}\n"


def _patch_time_and_kill(monkeypatch, fake_kill: _FakeKill) -> dict:
    """Deterministic time + kill mocks.

    ``time.sleep(s)`` advances ``time.monotonic`` by exactly ``s`` so the
    grace-period loop terminates after a predictable number of probes
    (~50 with 5s wait / 0.1s poll). Without this the test would hang
    until the real wall-clock elapses, since the production code uses
    real ``time.monotonic`` to bound the loop.
    """
    state: dict = {"t": 0.0}

    def fake_monotonic() -> float:
        return state["t"]

    def fake_sleep(s: float) -> None:
        state["t"] += s

    monkeypatch.setattr(watchdog.os, "kill", fake_kill)
    monkeypatch.setattr(watchdog.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(watchdog.time, "sleep", fake_sleep)
    return state


def _expected_pgrep_pattern(queue: Path, task_id: str) -> str:
    return _re.escape(str(queue / f"{task_id}.heartbeat"))


def test_mode6_enforce_kills_matching_pids(isolated_handoff_home, monkeypatch):
    """pgrep matches → SIGTERM sent → marker records killed PIDs."""
    queue = _setup_stale_task(isolated_handoff_home, "task-kill")
    fake_pgrep = _FakePgrep(
        _heartbeat_cmdline(queue, "task-kill", 12345)
        + _heartbeat_cmdline(queue, "task-kill", 12346, extra="second subshell"),
    )
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()  # all PIDs exit after first SIGTERM
    _patch_time_and_kill(monkeypatch, fake_kill)

    assert watchdog.scan_single_task_heartbeats() == 1
    assert fake_pgrep.calls == [["pgrep", "-fa", _expected_pgrep_pattern(queue, "task-kill")]]
    term_pids = [p for p, s in fake_kill.signals if s == signal.SIGTERM]
    assert sorted(term_pids) == [12345, 12346]
    body = (queue / "task-kill.529-suspected").read_text()
    assert "已 kill 2 进程" in body
    assert "12345" in body and "12346" in body
    assert "SIGKILL escalation" in body


def test_mode6_enforce_escalates_to_sigkill_when_term_ignored(isolated_handoff_home, monkeypatch):
    """Stubborn PID: SIGTERM → grace expires → SIGKILL → verify → killed."""
    queue = _setup_stale_task(isolated_handoff_home, "task-stubborn")
    fake_pgrep = _FakePgrep(_heartbeat_cmdline(queue, "task-stubborn", 9999))
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    # alive throughout grace; SIGKILL actually finishes it (verify-probe ok)
    fake_kill = _FakeKill(alive_after_term={9999}, exit_after_kill={9999})
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    sig_seq = [s for s in fake_kill.signals if s[0] == 9999 and s[1] != 0]
    assert sig_seq == [(9999, signal.SIGTERM), (9999, signal.SIGKILL)]
    body = (queue / "task-stubborn.529-suspected").read_text()
    assert "已 kill 1 进程" in body
    assert "9999" in body


def test_mode6_enforce_records_still_alive_when_sigkill_does_not_take(
    isolated_handoff_home, monkeypatch
):
    """Zombie / D-state: SIGKILL sent, verify probe still finds the PID."""
    queue = _setup_stale_task(isolated_handoff_home, "task-zombie")
    fake_pgrep = _FakePgrep(_heartbeat_cmdline(queue, "task-zombie", 6666))
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill(alive_after_term={6666})  # no exit_after_kill
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    body = (queue / "task-zombie.529-suspected").read_text()
    assert "已 kill" not in body  # didn't actually die
    assert "SIGKILL 后仍存活" in body
    assert "6666" in body
    sent = [sig for pid, sig in fake_kill.signals if pid == 6666 and sig != 0]
    assert signal.SIGTERM in sent
    assert signal.SIGKILL in sent


def test_mode6_enforce_term_alone_when_process_exits_mid_grace(isolated_handoff_home, monkeypatch):
    """SIGTERM lands, process exits on the 3rd alive-probe → no SIGKILL."""
    queue = _setup_stale_task(isolated_handoff_home, "task-graceful")
    fake_pgrep = _FakePgrep(_heartbeat_cmdline(queue, "task-graceful", 7777))
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill(alive_after_term={7777}, exit_after_polls={7777: 3})
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    real_signals = [s for s in fake_kill.signals if s[1] != 0]
    assert real_signals == [(7777, signal.SIGTERM)]


def test_mode6_enforce_skipped_when_stop_auto(isolated_handoff_home, monkeypatch):
    """STOP_AUTO marker → mark, notify, but no kill (operator pause)."""
    queue = _setup_stale_task(isolated_handoff_home, "task-paused", project="paused-proj")
    (isolated_handoff_home / "paused-proj" / "STOP_AUTO").write_text("")
    fake_pgrep = _FakePgrep(_heartbeat_cmdline(queue, "task-paused", 5555))
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    real_signals = [s for s in fake_kill.signals if s[1] != 0]
    assert real_signals == []
    assert fake_pgrep.calls == []  # didn't even run pgrep
    body = (queue / "task-paused.529-suspected").read_text()
    assert "STOP_AUTO" in body
    assert "手动" in body and "rm" in body  # marker explicitly tells op to manual rm


def test_mode6_enforce_no_match_when_no_processes(isolated_handoff_home, monkeypatch):
    """pgrep returns no matches → marker records 'no matching processes'."""
    queue = _setup_stale_task(isolated_handoff_home, "task-ghost")
    fake_pgrep = _FakePgrep("", returncode=1)  # pgrep exit 1 = no matches
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    body = (queue / "task-ghost.529-suspected").read_text()
    assert "未找到匹配 heartbeat 路径" in body
    real_signals = [s for s in fake_kill.signals if s[1] != 0]
    assert real_signals == []


def test_mode6_enforce_skips_self_and_pytest(isolated_handoff_home, monkeypatch):
    """Don't kill the watchdog process itself or any test-runner."""
    queue = _setup_stale_task(isolated_handoff_home, "task-self")
    my_pid = os.getpid()
    hb_cmd = _heartbeat_cmdline(queue, "task-self", 12345)
    fake_pgrep = _FakePgrep(
        f"{my_pid} python -m handoff_fanout.watchdog\n"
        f"88888 pytest -k task-self\n"
        f"99999 /usr/local/bin/pytest -k task-self\n" + hb_cmd + "54321 pgrep -fa task-self\n",
    )
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    killed = [p for p, sig in fake_kill.signals if sig == signal.SIGTERM]
    assert killed == [12345]


def test_mode6_enforce_skips_python_dash_m_pytest(isolated_handoff_home, monkeypatch):
    """``python -m pytest`` / ``python3 -m pytest`` are runners too."""
    queue = _setup_stale_task(isolated_handoff_home, "task-modpy")
    hb_cmd = _heartbeat_cmdline(queue, "task-modpy", 30001)
    fake_pgrep = _FakePgrep(
        "20001 python -m pytest tests/test_v41_heartbeat.py -k task-modpy\n"
        "20002 /opt/homebrew/bin/python3 -m pytest -k task-modpy\n"
        "20003 python3.13 -m pytest\n" + hb_cmd,
    )
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    killed = [p for p, sig in fake_kill.signals if sig == signal.SIGTERM]
    assert killed == [30001]


def test_mode6_enforce_skips_uv_run_pytest(isolated_handoff_home, monkeypatch):
    """``uv run pytest`` / ``uvx pytest`` — uv-wrapped runners."""
    queue = _setup_stale_task(isolated_handoff_home, "task-uv")
    hb_cmd = _heartbeat_cmdline(queue, "task-uv", 40001)
    fake_pgrep = _FakePgrep(
        "21001 uv run pytest -k task-uv\n"
        "21002 uv run --frozen pytest -k task-uv\n"
        "21003 uvx pytest -k task-uv\n" + hb_cmd,
    )
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    killed = [p for p, sig in fake_kill.signals if sig == signal.SIGTERM]
    assert killed == [40001]


def test_mode6_enforce_survives_pgrep_missing(isolated_handoff_home, monkeypatch):
    """Systems without pgrep get a graceful skip — mark/notify still happen."""
    queue = _setup_stale_task(isolated_handoff_home, "task-no-pgrep")

    def boom(argv, *args, **kwargs):
        if isinstance(argv, list | tuple) and argv and str(argv[0]).endswith("pgrep"):
            raise FileNotFoundError("pgrep not on this system")
        return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)

    monkeypatch.setattr(watchdog.subprocess, "run", boom)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    body = (queue / "task-no-pgrep.529-suspected").read_text()
    assert "pgrep 不可用" in body
    real_signals = [s for s in fake_kill.signals if s[1] != 0]
    assert real_signals == []


def test_mode6_enforce_treats_pgrep_rc_gt_1_as_unavailable(isolated_handoff_home, monkeypatch):
    """pgrep rc=2 (syntax) / rc=3 (fatal) → unavailable, don't guess."""
    queue = _setup_stale_task(isolated_handoff_home, "task-rc2")
    fake_pgrep = _FakePgrep("some garbage\n", returncode=2)
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    body = (queue / "task-rc2.529-suspected").read_text()
    assert "pgrep 不可用" in body
    real_signals = [s for s in fake_kill.signals if s[1] != 0]
    assert real_signals == []


def test_mode6_enforce_handles_dead_pid_race(isolated_handoff_home, monkeypatch):
    """Process exits between pgrep and SIGTERM — no crash, no false 'killed'."""
    queue = _setup_stale_task(isolated_handoff_home, "task-race")
    fake_pgrep = _FakePgrep(_heartbeat_cmdline(queue, "task-race", 4242))
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill(missing_pids={4242})  # SIGTERM raises ProcessLookupError
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    real_signals = [s for s in fake_kill.signals if s[1] == signal.SIGTERM]
    assert real_signals == [(4242, signal.SIGTERM)]
    kill_signals = [s for s in fake_kill.signals if s[1] == signal.SIGKILL]
    assert kill_signals == []  # no escalation for a process that's already gone
    body = (queue / "task-race.529-suspected").read_text()
    assert "已自然退出" in body
    assert "4242" in body


def test_mode6_enforce_records_permission_denied(isolated_handoff_home, monkeypatch):
    """PermissionError on initial SIGTERM → recorded as ``permission_denied``."""
    queue = _setup_stale_task(isolated_handoff_home, "task-perm")
    fake_pgrep = _FakePgrep(_heartbeat_cmdline(queue, "task-perm", 8888))
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill(permission_pids={8888})
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    body = (queue / "task-perm.529-suspected").read_text()
    assert "权限拒绝" in body
    assert "8888" in body


def test_mode6_enforce_neutralizes_regex_metacharacters_in_task_id(
    isolated_handoff_home, monkeypatch
):
    """A task id with regex metachars (``.``, ``+``, ``[``) must NOT widen pgrep."""
    task = "weird.task+[case]"
    queue = _setup_stale_task(isolated_handoff_home, task)
    fake_pgrep = _FakePgrep("", returncode=1)
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    assert len(fake_pgrep.calls) == 1
    pattern = fake_pgrep.calls[0][2]
    expected = _re.escape(str(queue / f"{task}.heartbeat"))
    assert pattern == expected
    # Every metachar in the path appears as an escaped literal
    assert r"\." in pattern
    assert r"\+" in pattern
    assert r"\[" in pattern
    assert r"\]" in pattern


def test_mode6_enforce_isolates_same_named_tasks_across_projects(
    isolated_handoff_home, monkeypatch
):
    """A duplicate task id in another project must not be killed.

    pgrep is now scoped to ``<queue_dir>/<task>.heartbeat`` (project-
    unique literal path). The mock returns each project's PID only when
    the pattern matches that project's heartbeat path, proving scoping.
    """
    queue_a = _setup_stale_task(isolated_handoff_home, "shared", project="proj-a")
    queue_b = _setup_stale_task(isolated_handoff_home, "shared", project="proj-b")
    a_pattern = _re.escape(str(queue_a / "shared.heartbeat"))
    b_pattern = _re.escape(str(queue_b / "shared.heartbeat"))

    class _ScopedPgrep:
        def __init__(self):
            self.calls: list[list[str]] = []

        def __call__(self, argv, *args, **kwargs):
            if isinstance(argv, list | tuple) and argv and str(argv[0]).endswith("pgrep"):
                self.calls.append(list(argv))
                pat = argv[2]
                if pat == a_pattern:
                    return SimpleNamespace(
                        stdout=_heartbeat_cmdline(queue_a, "shared", 10001),
                        stderr="",
                        returncode=0,
                    )
                if pat == b_pattern:
                    return SimpleNamespace(
                        stdout=_heartbeat_cmdline(queue_b, "shared", 20002),
                        stderr="",
                        returncode=0,
                    )
                return SimpleNamespace(stdout="", stderr="", returncode=1)
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)

    fake_pgrep = _ScopedPgrep()
    monkeypatch.setattr(watchdog.subprocess, "run", fake_pgrep)
    fake_kill = _FakeKill()
    _patch_time_and_kill(monkeypatch, fake_kill)

    watchdog.scan_single_task_heartbeats()
    killed = [p for p, sig in fake_kill.signals if sig == signal.SIGTERM]
    assert sorted(killed) == [10001, 20002]
    assert (queue_a / "shared.529-suspected").exists()
    assert (queue_b / "shared.529-suspected").exists()
