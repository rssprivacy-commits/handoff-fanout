"""direct-jump-spawn (2026-06-13) — the ``dump`` path writes ``SPAWNER_FOCUS`` symmetrically with
``spawn``, at all THREE launcher-visible ``.uri`` producers:

  1. ``write_active_dump``      — the single-task relay (中枢 → worker/succession), = the ERP实证 path;
  2. open-batch fan-out        — every ``--open-batch`` sub-task ``.uri``;
  3. ``trigger_fan_in_if_ready`` — the fan-in ``.uri``.

Each asserts BOTH directions: a valid ``$HANDOFF_WINDOW_FOCUS_PATH`` (a coordinator terminal) yields
the additive third line ``SPAWNER_FOCUS=<realpath>``; an absent env keeps the ``.uri`` byte-identical
to the pre-feature form (no third line) — the fail-open / 向后兼容 contract.

Pure filesystem + a real throwaway git repo; no external services.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import config as _config
from handoff_fanout import dump

PROJECT = "proj"
TASK = "the-task"


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bare_and_clone(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(bare), str(ws)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)
    (ws / "a.py").write_text("a\n")
    (ws / "b.py").write_text("b\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    _run(["git", "push", "-q", "origin", "main"], ws)
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"], cwd=str(ws), capture_output=True
    )
    return ws


def _valid_focus(home: Path) -> Path:
    """An existing ``.handoff.code-workspace`` under the handoff home (an allowed root) standing in
    for the dispatching coordinator window's own focus path."""
    ws = home / "coord-proj" / "singlepane" / "coord.handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    return ws


def _uri_text(queue_dir: Path, task: str) -> str:
    return (queue_dir / f"{task}.uri").read_text()


# ─── point 1: write_active_dump (single-task relay) ──────────────────────────


def _active_dump(home: Path, ws: Path, monkeypatch) -> int:
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    for var in ("HANDOFF_RETRO_MANDATE", "HANDOFF_RETRO_BYPASS", "HANDOFF_AUDIT_MANDATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "off")  # shared tree → still writes the .uri
    return dump.main(
        ["--task", TASK, "--next", "brief", "--project", PROJECT, "--workspace", str(ws),
         "--status", "active"]
    )


def test_active_dump_writes_spawner_focus_when_env_valid(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text("{}")
    ws = _bare_and_clone(tmp_path)
    focus = _valid_focus(home)
    monkeypatch.setenv("HANDOFF_WINDOW_FOCUS_PATH", str(focus))

    assert _active_dump(home, ws, monkeypatch) == 0
    text = _uri_text(home / PROJECT / "queue", TASK)
    assert f"SPAWNER_FOCUS={os.path.realpath(str(focus))}\n" in text
    # additive THIRD line — the first two stay WORKSPACE / URI.
    assert text.splitlines()[2].startswith("SPAWNER_FOCUS=")


def test_active_dump_no_focus_line_when_env_absent(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text("{}")
    ws = _bare_and_clone(tmp_path)
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)

    assert _active_dump(home, ws, monkeypatch) == 0
    text = _uri_text(home / PROJECT / "queue", TASK)
    assert "SPAWNER_FOCUS" not in text  # byte-identical to the pre-feature .uri
    assert text == f"WORKSPACE={ws}\nURI={dump.build_uri(_config.load(), PROJECT, TASK)}\n"


def test_active_dump_anchor_miss_logged_uri_byte_compat(tmp_path, monkeypatch):
    """spawn-unification Step 1: when no anchor resolves, the .uri stays byte-identical (the assert
    above) AND the miss is recorded — one JSON line to <home>/<project>/spawn-anchor-miss.log. The
    telemetry lands in a SEPARATE file, so it never perturbs the launcher-visible .uri."""
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text("{}")
    ws = _bare_and_clone(tmp_path)
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)

    assert _active_dump(home, ws, monkeypatch) == 0
    miss_log = home / PROJECT / "spawn-anchor-miss.log"
    lines = miss_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["project"] == PROJECT
    assert rec["task"] == TASK
    assert rec["reason"] == "dump:anchor-unresolved"
    assert rec["isolation"] == "singlepane"  # shared-tree active dump (worktree_info is None)


def test_active_dump_writes_spawner_focus_from_self_id(tmp_path, monkeypatch):
    """mp-locate-return (sw-coord-p22): env absent, but env-independent self-identification resolves the
    coordinator's OWN workspace → the additive ``SPAWNER_FOCUS=<path>`` line (the worktree/singlepane
    fix where the env channel is empty). The conftest autouse neutralizes the resolver by default; here
    we override it to a validated path, which wins (runs after the fixture)."""
    from handoff_fanout import spawner_focus as _sf
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text("{}")
    ws = _bare_and_clone(tmp_path)
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)  # env empty → exercise self-id
    focus = os.path.realpath(str(_valid_focus(home)))
    monkeypatch.setattr(_sf, "resolve_spawner_focus_path", lambda *a, **k: focus)

    assert _active_dump(home, ws, monkeypatch) == 0
    text = _uri_text(home / PROJECT / "queue", TASK)
    assert f"SPAWNER_FOCUS={focus}\n" in text
    assert text.splitlines()[2] == f"SPAWNER_FOCUS={focus}"  # additive third line


# ─── points 2 & 3: open-batch fan-out + fan-in ───────────────────────────────


def _init_git(ws: Path) -> None:
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "a.py").write_text("a\n")
    (ws / "b.py").write_text("b\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)


def _manifest() -> dict:
    return {
        "schema_version": dump.SCHEMA_VERSION,
        "batch_id": "test-batch",
        "fan_in_task": "test-fanin",
        "sub_tasks": [
            {"id": "sub-a", "brief": "do a",
             "file_ownership": [{"type": "exact", "path": "a.py"}], "depends_on": []},
            {"id": "sub-b", "brief": "do b",
             "file_ownership": [{"type": "exact", "path": "b.py"}], "depends_on": []},
        ],
    }


@pytest.fixture(autouse=True)
def _no_stagger(monkeypatch):
    """The 30s inter-spawn stagger would make the batch test crawl; collapse it."""
    monkeypatch.setattr(dump, "STAGGER_SPAWN_SECONDS", 0)


def test_open_batch_subtasks_write_spawner_focus_when_env_valid(
    tmp_path, isolated_handoff_home, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name
    focus = _valid_focus(isolated_handoff_home)
    monkeypatch.setenv("HANDOFF_WINDOW_FOCUS_PATH", str(focus))

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    assert dump.main(
        ["--open-batch", str(manifest_path), "--project", project, "--workspace", str(ws)]
    ) == 0

    queue_dir = isolated_handoff_home / project / "queue"
    line = f"SPAWNER_FOCUS={os.path.realpath(str(focus))}\n"
    assert line in _uri_text(queue_dir, "sub-a")
    assert line in _uri_text(queue_dir, "sub-b")


def test_open_batch_no_focus_line_when_env_absent(
    tmp_path, isolated_handoff_home, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    assert dump.main(
        ["--open-batch", str(manifest_path), "--project", project, "--workspace", str(ws)]
    ) == 0

    queue_dir = isolated_handoff_home / project / "queue"
    uri = dump.build_uri(_config.load(), project, "sub-a")
    assert _uri_text(queue_dir, "sub-a") == f"WORKSPACE={ws}\nURI={uri}\n"  # byte-identical
    assert "SPAWNER_FOCUS" not in _uri_text(queue_dir, "sub-b")


def test_fan_in_writes_spawner_focus_when_env_valid(
    tmp_path, isolated_handoff_home, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name
    focus = _valid_focus(isolated_handoff_home)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    # Open the batch with NO focus env so the sub-task .uris (not under test here) are unaffected.
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)
    assert dump.main(
        ["--open-batch", str(manifest_path), "--project", project, "--workspace", str(ws)]
    ) == 0

    cfg = _config.load()
    queue_dir = cfg.queue_dir(project)
    batch_dir = dump.handoff_root() / project / "batches" / "test-batch"
    (batch_dir / "sub-a.done").write_text("done\n")
    (batch_dir / "sub-b.done").write_text("done\n")

    # The fan-in fires from the coordinator terminal → SPAWNER_FOCUS lands on the fan-in .uri.
    monkeypatch.setenv("HANDOFF_WINDOW_FOCUS_PATH", str(focus))
    assert dump.trigger_fan_in_if_ready(project, ws, "test-batch", queue_dir, cfg=cfg) is True

    text = _uri_text(queue_dir, "test-fanin")
    assert f"SPAWNER_FOCUS={os.path.realpath(str(focus))}\n" in text


def test_fan_in_no_focus_line_when_env_absent(
    tmp_path, isolated_handoff_home, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)
    assert dump.main(
        ["--open-batch", str(manifest_path), "--project", project, "--workspace", str(ws)]
    ) == 0

    cfg = _config.load()
    queue_dir = cfg.queue_dir(project)
    batch_dir = dump.handoff_root() / project / "batches" / "test-batch"
    (batch_dir / "sub-a.done").write_text("done\n")
    (batch_dir / "sub-b.done").write_text("done\n")

    assert dump.trigger_fan_in_if_ready(project, ws, "test-batch", queue_dir, cfg=cfg) is True
    uri = dump.build_uri(cfg, project, "test-fanin")
    assert _uri_text(queue_dir, "test-fanin") == f"WORKSPACE={ws}\nURI={uri}\n"  # byte-identical
