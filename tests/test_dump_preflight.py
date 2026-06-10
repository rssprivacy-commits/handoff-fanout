"""Generic project-scoped ``dump_preflight_commands`` gate (2C).

A project may configure a list of preflight commands that ``handoff dump`` runs
as a HARD pre-req before producing the closure artifact. Any non-zero exit (or
a command that cannot run) FAILS CLOSED and blocks the dump. The engine is
progress-agnostic: it only executes whatever the project configured.

Motivating consumer: ERP's ``progress_pending.py --gate`` (progress-site sync
anti-drift step 2). Other projects with no such config are entirely unaffected.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from handoff_fanout import dump

TASK = "preflight-test-task"
PROJECT = "ptest"


def _git_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    return ws


def _home_with_config(tmp_path: Path, preflight: list | None) -> Path:
    home = tmp_path / "handoff"
    home.mkdir()
    cfg: dict = {}
    if preflight is not None:
        cfg["dump_preflight_commands"] = preflight
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return home


def _run_dump(home: Path, ws: Path, monkeypatch, status: str = "active",
              extra: list[str] | None = None) -> int:
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    argv = ["--task", TASK, "--next", "n", "--project", PROJECT,
            "--workspace", str(ws), "--status", status]
    return dump.main(argv + (extra or []))


def _queue_md(home: Path) -> Path:
    return home / PROJECT / "queue" / f"{TASK}.md"


# ── pass / fail / fail-closed ────────────────────────────────────────────────
def test_preflight_pass_allows_dump(tmp_path, monkeypatch):
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "ok-gate", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(0)"]},
    ])
    rc = _run_dump(home, ws, monkeypatch)
    assert rc == 0
    assert _queue_md(home).exists()                     # dump 真的产出了


def test_preflight_fail_blocks_dump(tmp_path, monkeypatch):
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "block-gate", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(1)"]},
    ])
    rc = _run_dump(home, ws, monkeypatch)
    assert rc != 0                                      # 被 fail-closed 阻断
    assert not _queue_md(home).exists()                 # 无产出（没绕过闸）


def test_preflight_missing_program_fails_closed(tmp_path, monkeypatch):
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "ghost", "command": ["this-program-does-not-exist-xyz", "--gate"]},
    ])
    rc = _run_dump(home, ws, monkeypatch)
    assert rc != 0                                      # 默认 on_error=block：跑不起来也 fail-closed
    assert not _queue_md(home).exists()


def test_preflight_on_error_warn_does_not_brick_when_command_missing(tmp_path, monkeypatch):
    # on_error=warn：命令跑不起来（基础设施故障，非闸的判定）→ LOUD 警告但放行
    # （提醒类闸不该因解释器路径坏了就 brick 掉所有 closure / I8 fail-open 类）。
    # 注意：命令真能跑且 exit 非 0（闸的真判定）仍 block —— 见下个用例。
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "ghost", "command": ["this-program-does-not-exist-xyz", "--gate"],
         "on_error": "warn"},
    ])
    rc = _run_dump(home, ws, monkeypatch)
    assert rc == 0                                      # 不 brick
    assert _queue_md(home).exists()


def test_preflight_on_error_warn_still_blocks_on_nonzero_verdict(tmp_path, monkeypatch):
    # on_error=warn 只放过"跑不起来"；命令真跑了且 exit 非 0（闸判定不通过）仍 block。
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "real-verdict", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(1)"],
         "on_error": "warn"},
    ])
    rc = _run_dump(home, ws, monkeypatch)
    assert rc != 0                                      # 真判定不通过 → 仍 block
    assert not _queue_md(home).exists()


# ── zero-impact for projects without the config ──────────────────────────────
def test_no_preflight_config_is_legacy_unaffected(tmp_path, monkeypatch):
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, None)            # 无 dump_preflight_commands 键
    rc = _run_dump(home, ws, monkeypatch)
    assert rc == 0
    assert _queue_md(home).exists()


# ── status scoping ───────────────────────────────────────────────────────────
def test_preflight_scoped_status_skips_other_status(tmp_path, monkeypatch):
    # gate 只对 status=blocked 生效；本 dump 是 active → 闸不该跑（命令会失败但 dump 通过）
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "blocked-only", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(1)"],
         "statuses": ["blocked"]},
    ])
    rc = _run_dump(home, ws, monkeypatch, status="active")
    assert rc == 0
    assert _queue_md(home).exists()


# ── project scoping (config.json is SHARED across projects under one HANDOFF_HOME) ──
def test_preflight_project_scoped_skips_other_project(tmp_path, monkeypatch):
    # gate 限定 projects=["erp-system"]；本 dump 是 project=ptest → 闸不该跑
    # （否则共享 config.json 会让 ERP 的 gate 污染 dharmaxis/rakeforge 等 dump）
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "erp-only", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(1)"],
         "projects": ["erp-system"]},
    ])
    rc = _run_dump(home, ws, monkeypatch)               # project=ptest（见 _run_dump）
    assert rc == 0
    assert _queue_md(home).exists()


def test_preflight_project_scoped_runs_for_matching_project(tmp_path, monkeypatch):
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "ptest-only", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(1)"],
         "projects": ["ptest"]},
    ])
    rc = _run_dump(home, ws, monkeypatch)               # project=ptest → 匹配 → 阻断
    assert rc != 0
    assert not _queue_md(home).exists()


# ── dry-run preview is not blocked ───────────────────────────────────────────
def test_preflight_dry_run_not_blocked(tmp_path, monkeypatch):
    ws = _git_ws(tmp_path)
    home = _home_with_config(tmp_path, [
        {"name": "block-gate", "command": ["/usr/bin/python3", "-c", "import sys; sys.exit(1)"]},
    ])
    rc = _run_dump(home, ws, monkeypatch, extra=["--dry-run"])
    assert rc == 0                                      # 预览不被闸挡


# ── config parsing ───────────────────────────────────────────────────────────
def test_config_parses_preflight_specs(tmp_path, monkeypatch):
    from handoff_fanout import config as _config
    home = _home_with_config(tmp_path, [
        {"name": "g", "command": ["echo", "hi"], "timeout": 12, "statuses": ["active"]},
    ])
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    cfg = _config.load(home)
    assert len(cfg.dump_preflight_commands) == 1
    spec = cfg.dump_preflight_commands[0]
    assert spec.command == ["echo", "hi"]
    assert spec.timeout == 12
    assert "active" in spec.statuses


def test_config_absent_preflight_is_empty(tmp_path, monkeypatch):
    from handoff_fanout import config as _config
    home = _home_with_config(tmp_path, None)
    cfg = _config.load(home)
    assert cfg.dump_preflight_commands == []
