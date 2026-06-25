"""req3 C — autoclose-audited-workers.py driver: thin glue over the safety gate.

The driver itself is in ``~/.claude-handoff/supervisor-monitor/`` (non-git). ALL the
dangerous judgement is in ``autoclose_gate`` (tested in test_autoclose_gate.py); these
tests cover the GLUE: opt-in / kill-switch gating, the dry-run-by-default safety, WID
binding (incl. the AI-titled fail-safe), and that only gate-cleared tasks reach the close
tool. The gate, window detection, and the close tool are all mocked — nothing real closes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from handoff_fanout import autoclose_gate as gate
from handoff_fanout import config as _config

DRV_PATH = Path.home() / ".claude-handoff" / "supervisor-monitor" / "autoclose-audited-workers.py"
if not DRV_PATH.exists():
    pytest.skip("autoclose-audited-workers.py not deployed", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("autoclose_audited_workers", DRV_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drv = _load()

PROJECT = "demo"
NONCE = "184f6d9d2b3830af"
WORKER_TITLE = f"demo · wk-1 · worker · {NONCE} [worktree] — doing stuff"
COORD_TITLE = "🧭中枢·demo · demo-coord · supervisor_succession · aaaaaaaaaaaaaaaa [singlepane] — x"
AI_TITLED = "审计交接义务工作文件 — .handoff (Workspace)"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    (home / PROJECT).mkdir(parents=True)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_WORKER_AUTOCLOSE_ENABLED", raising=False)
    return _config.Config(home=home)


# ─── opt-in switch (DEFAULT-OFF) ─────────────────────────────────────────────


def test_opt_in_default_off(cfg):
    assert drv.opt_in_enabled(cfg, PROJECT) is False


def test_opt_in_env(cfg, monkeypatch):
    monkeypatch.setenv("HANDOFF_WORKER_AUTOCLOSE_ENABLED", "1")
    assert drv.opt_in_enabled(cfg, PROJECT) is True


def test_opt_in_fleet_sentinel(cfg):
    (cfg.home / "worker-autoclose.enabled").write_text("")
    assert drv.opt_in_enabled(cfg, PROJECT) is True


def test_opt_in_per_project_sentinel(cfg):
    (cfg.home / PROJECT / "worker-autoclose.enabled").write_text("")
    assert drv.opt_in_enabled(cfg, PROJECT) is True


def test_opt_in_is_separate_from_v4_coordinator_switch(cfg):
    # the v4 coordinator-autoclose switch must NOT enable worker-autoclose.
    (cfg.home / "autoclose.enabled").write_text("")
    assert drv.opt_in_enabled(cfg, PROJECT) is False


# ─── kill-switch ─────────────────────────────────────────────────────────────


def test_kill_switch_fleet(cfg):
    (cfg.home / ".worker-autoclose-off").write_text("")
    assert drv.kill_switch_active(cfg, PROJECT) is True


def test_kill_switch_per_project(cfg):
    (cfg.home / PROJECT / ".worker-autoclose-off").write_text("")
    assert drv.kill_switch_active(cfg, PROJECT) is True


def test_kill_switch_absent(cfg):
    assert drv.kill_switch_active(cfg, PROJECT) is False


# ─── WID binding (the AI-titled fail-safe is the safety crux) ────────────────


def _win(title, wid):
    return {"title": title, "window_number": wid, "desktop": 1}


def test_resolve_wid_unique_structured(cfg):
    windows = [_win(WORKER_TITLE, 100), _win("other · x · worker · bbbbbbbbbbbbbbbb [worktree] — y", 101)]
    assert drv.resolve_wid(windows, PROJECT, "wk-1", NONCE) == 100


def test_resolve_wid_ai_titled_unbindable(cfg):
    # A fully AI-retitled window has no recoverable identity → None (never auto-closed).
    windows = [_win(AI_TITLED, 100)]
    assert drv.resolve_wid(windows, PROJECT, "wk-1", NONCE) is None


def test_resolve_wid_nonce_not_unique(cfg):
    # nonce appears in two windows (e.g. one's summary echoes it) → can't safely bind.
    windows = [_win(WORKER_TITLE, 100), _win(f"noise {NONCE} echo", 101)]
    assert drv.resolve_wid(windows, PROJECT, "wk-1", NONCE) is None


def test_resolve_wid_never_binds_coordinator(cfg):
    windows = [_win(COORD_TITLE, 100)]
    assert drv.resolve_wid(windows, PROJECT, "demo-coord", "aaaaaaaaaaaaaaaa") is None


def test_resolve_wid_none_nonce(cfg):
    windows = [_win(WORKER_TITLE, 100)]
    assert drv.resolve_wid(windows, PROJECT, "wk-1", None) is None


# ─── run(): only gate-cleared + bindable tasks reach the close tool ──────────


def _decision(close_ok, reason="ok", nonce=NONCE, evidence=None):
    return gate.GateDecision(close_ok=close_ok, reason=reason, nonce=nonce, evidence=evidence or {})


def test_run_cleared_task_invokes_close_tool(cfg, monkeypatch):
    monkeypatch.setattr(drv._gate, "gate_task", lambda *a, **k: _decision(True, evidence={"merge_sha": "a" * 40}))
    calls = []
    monkeypatch.setattr(drv, "invoke_close_tool", lambda project, wids, execute: (calls.append((project, wids, execute)), 0)[1])
    windows = [_win(WORKER_TITLE, 100)]
    summary = drv.run(cfg, PROJECT, ["wk-1"], execute=True, idle_threshold=1800, windows=windows)
    assert summary["cleared"] == ["wk-1"]
    assert summary["wids"] == [100]
    assert calls == [(PROJECT, [100], True)]


def test_run_refused_task_never_closes(cfg, monkeypatch):
    monkeypatch.setattr(drv._gate, "gate_task", lambda *a, **k: _decision(False, reason="not-merged"))
    calls = []
    monkeypatch.setattr(drv, "invoke_close_tool", lambda *a, **k: calls.append(a) or 0)
    summary = drv.run(cfg, PROJECT, ["wk-1"], execute=True, idle_threshold=1800, windows=[_win(WORKER_TITLE, 100)])
    assert summary["cleared"] == []
    assert ("wk-1", "not-merged") in summary["refused"]
    assert calls == []  # the close tool is NEVER invoked when nothing cleared


def test_run_unbindable_cleared_task_not_closed(cfg, monkeypatch):
    # Gate clears it, but the window is AI-titled (no WID binding) → left for manual close.
    monkeypatch.setattr(drv._gate, "gate_task", lambda *a, **k: _decision(True))
    calls = []
    monkeypatch.setattr(drv, "invoke_close_tool", lambda *a, **k: calls.append(a) or 0)
    summary = drv.run(cfg, PROJECT, ["wk-1"], execute=True, idle_threshold=1800, windows=[_win(AI_TITLED, 100)])
    assert summary["unbindable"] == ["wk-1"]
    assert summary["cleared"] == []
    assert calls == []


def test_run_writes_durable_log(cfg, monkeypatch):
    monkeypatch.setattr(drv._gate, "gate_task", lambda *a, **k: _decision(False, reason="dirty"))
    monkeypatch.setattr(drv, "invoke_close_tool", lambda *a, **k: 0)
    drv.run(cfg, PROJECT, ["wk-1"], execute=False, idle_threshold=1800, windows=[])
    logp = drv.log_path(cfg, PROJECT)
    assert logp.exists()
    assert "dirty" in logp.read_text()


def test_run_dry_run_passes_execute_false(cfg, monkeypatch):
    monkeypatch.setattr(drv._gate, "gate_task", lambda *a, **k: _decision(True))
    seen = {}
    monkeypatch.setattr(drv, "invoke_close_tool", lambda project, wids, execute: seen.update(execute=execute) or 0)
    drv.run(cfg, PROJECT, ["wk-1"], execute=False, idle_threshold=1800, windows=[_win(WORKER_TITLE, 100)])
    assert seen["execute"] is False


# ─── main(): kill-switch + opt-in gating ─────────────────────────────────────


def test_main_kill_switch_short_circuits(cfg, monkeypatch):
    (cfg.home / ".worker-autoclose-off").write_text("")
    called = {"run": 0}
    monkeypatch.setattr(drv, "run", lambda *a, **k: called.update(run=1) or {})
    rc = drv.main(["--project", PROJECT, "--task", "wk-1", "--execute"])
    assert rc == 0
    assert called["run"] == 0  # nothing happened, not even a dry-run


def test_main_execute_forced_dry_run_when_disabled(cfg, monkeypatch):
    seen = {}
    monkeypatch.setattr(drv, "run", lambda *a, **k: seen.update(execute=k["execute"]) or {"cleared": [], "refused": [], "unbindable": []})
    rc = drv.main(["--project", PROJECT, "--task", "wk-1", "--execute"])
    assert rc == 0
    assert seen["execute"] is False  # opt-in OFF → --execute downgraded to dry-run


def test_main_execute_honored_when_enabled(cfg, monkeypatch):
    (cfg.home / PROJECT / "worker-autoclose.enabled").write_text("")
    seen = {}
    monkeypatch.setattr(drv, "run", lambda *a, **k: seen.update(execute=k["execute"]) or {"cleared": [], "refused": [], "unbindable": []})
    rc = drv.main(["--project", PROJECT, "--task", "wk-1", "--execute"])
    assert rc == 0
    assert seen["execute"] is True


def test_main_sweep_gathers_discharged(cfg, monkeypatch):
    monkeypatch.setattr(drv._gate, "discharged_tasks", lambda c, p: ["wk-1", "wk-2"])
    seen = {}
    monkeypatch.setattr(drv, "run", lambda c, p, tasks, **k: seen.update(tasks=tasks) or {"cleared": [], "refused": [], "unbindable": []})
    rc = drv.main(["--project", PROJECT, "--sweep"])
    assert rc == 0
    assert seen["tasks"] == ["wk-1", "wk-2"]
