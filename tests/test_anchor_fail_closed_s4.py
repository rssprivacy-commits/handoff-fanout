"""spawn-unification Step 4 — fail-closed anchor machinery (warn-mode default, ZERO behavior change).

Today an anchor-resolution MISS on a coordinator dispatch is a SILENT fail-open (omit SPAWNER_FOCUS →
code-router.sh static-map fallback → worker on the wrong desktop). Step 4 turns that into an explicit
fail-CLOSED refuse — but ONLY for a project the owner has explicitly moved to an enforce phase. The
DEFAULT (config enforce lists empty) is warn = byte-identical to Step 1+2.

Coverage maps to design §6 #1-11:
  #1  enforce + coordinator + miss → EXIT_FAIL_CLOSED (spawn + dump; dump leaves NO half-product .uri).
  #2  enforce + coordinator + anchor RESOLVES → normal SPAWNER_FOCUS (zero regression).
  #3  enforce + --origin interactive + front TTY + HANDOFF_UNATTENDED unset + miss → exempt.
  #4  env-inheritance backdoor: --origin interactive + HANDOFF_UNATTENDED set → demote coordinator → block.
  #4b headless backdoor: --origin interactive + NO front TTY (+ forgot HANDOFF_UNATTENDED) → demote → block.
  #4c call-point contract: watchdog / default dispatch never passes interactive → coordinator default.
  #5  system exemption ⟺ config allow-list (+audit-log); not-in-list → demote → block. test ⟺ in-process pytest.
  #6  config fail-safe: enforce-listed project + corrupt config → fail-closed (NEVER silently warn).
  #7  warn (empty lists) + miss → fail-open + log_anchor_miss + byte-identical .uri (disable-fix guard).
      dry_run: full new decision runs but only LOG_BLOCK_INTENT — behavior unchanged.
  #8  per-project three-phase isolation (enforce / dry_run / warn coexist, lists don't bleed).
  #9  AnchorDecision SINGLE parse: resolve_spawner_focus_path called ONCE per dump (no writer re-read).
  #10 error-code / reason separation: anchor-unresolved is distinct, never the Step 6 isolation-unresolved.
  #11 空列表 vs 缺键 config semantics.

Pure filesystem + throwaway git repos; no external services.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


from handoff_fanout import config as _config
from handoff_fanout import dump
from handoff_fanout import spawn
from handoff_fanout import spawner_focus as _sf

PROJECT = "anchor-proj"
TASK = "anchor-task"


# ─── config builders ─────────────────────────────────────────────────────────


def _home(tmp_path: Path, monkeypatch, cfg: dict | None = None, *, write: bool = True) -> Path:
    home = tmp_path / "handoff"
    home.mkdir(exist_ok=True)
    if write:
        (home / "config.json").write_text(json.dumps(cfg or {}), encoding="utf-8")
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    for var in ("HANDOFF_RETRO_MANDATE", "HANDOFF_RETRO_BYPASS", "HANDOFF_AUDIT_MANDATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("HANDOFF_UNATTENDED", raising=False)
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)
    return home


def _cfg(**lists) -> _config.Config:
    """A Config with the Step 4 anchor lists set directly (unit-level, no JSON round-trip)."""
    return _config.Config(
        spawner_anchor_enforce_projects=list(lists.get("enforce", ())),
        spawner_anchor_dry_run_projects=list(lists.get("dry_run", ())),
        spawner_anchor_system_allow=list(lists.get("system_allow", ())),
        config_trusted=lists.get("trusted", True),
    )


# ════════════════════════════════════════════════════════════════════════════
# Unit: _effective_origin trust matrix (design §4.2) — leniency only from
# NON-inheritable sources; env only ADDS strictness.
# ════════════════════════════════════════════════════════════════════════════


def test_effective_origin_coordinator_is_identity():
    assert _sf._effective_origin("coordinator", cfg=_cfg(), project=PROJECT) == "coordinator"


def test_effective_origin_unknown_is_coordinator():
    # any un-provable / typo'd signal → the strictest origin (conservative).
    assert _sf._effective_origin("banana", cfg=_cfg(), project=PROJECT) == "coordinator"
    assert _sf._effective_origin(None, cfg=_cfg(), project=PROJECT) == "coordinator"


def test_effective_origin_interactive_needs_front_tty(monkeypatch):
    # #3: front TTY + HANDOFF_UNATTENDED unset → interactive (exempt-eligible).
    monkeypatch.setattr(_sf, "_front_tty", lambda: True)
    monkeypatch.delenv("HANDOFF_UNATTENDED", raising=False)
    assert _sf._effective_origin("interactive", cfg=_cfg(), project=PROJECT) == "interactive"


def test_effective_origin_interactive_demoted_without_tty(monkeypatch):
    # #4b: a headless chain physically can't be exempt even if it FORGOT HANDOFF_UNATTENDED.
    monkeypatch.setattr(_sf, "_front_tty", lambda: False)
    monkeypatch.delenv("HANDOFF_UNATTENDED", raising=False)
    assert _sf._effective_origin("interactive", cfg=_cfg(), project=PROJECT) == "coordinator"


def test_effective_origin_interactive_demoted_when_unattended_set(monkeypatch):
    # #4: an inherited env can only ADD strictness — HANDOFF_UNATTENDED present (any value) demotes.
    monkeypatch.setattr(_sf, "_front_tty", lambda: True)
    monkeypatch.setenv("HANDOFF_UNATTENDED", "1")
    assert _sf._effective_origin("interactive", cfg=_cfg(), project=PROJECT) == "coordinator"
    monkeypatch.setenv("HANDOFF_UNATTENDED", "")  # even empty-string presence demotes
    assert _sf._effective_origin("interactive", cfg=_cfg(), project=PROJECT) == "coordinator"


def test_effective_origin_system_only_from_config_allowlist():
    # #5: system exemption ⟺ project ∈ config allow-list (NOT a token / env).
    assert _sf._effective_origin("system", cfg=_cfg(system_allow=[PROJECT]), project=PROJECT) == "system"
    assert _sf._effective_origin("system", cfg=_cfg(system_allow=["other"]), project=PROJECT) == "coordinator"
    assert _sf._effective_origin("system", cfg=_cfg(), project=PROJECT) == "coordinator"


def test_effective_origin_test_only_in_process_pytest(monkeypatch):
    # #5: test exemption ⟺ in-process pytest (an inheritable env would violate «env only授 strictness»).
    assert _sf._effective_origin("test", cfg=_cfg(), project=PROJECT) == "test"  # we ARE in pytest
    monkeypatch.setattr(_sf, "_in_process_pytest", lambda: False)
    assert _sf._effective_origin("test", cfg=_cfg(), project=PROJECT) == "coordinator"


# ════════════════════════════════════════════════════════════════════════════
# Unit: _anchor_enforcement (design §4.1) — default-OPEN warn + corrupt-config fail-safe.
# ════════════════════════════════════════════════════════════════════════════


def test_enforcement_default_warn_disable_fix_guard():
    # #7 DISABLE-FIX GUARD: an unlisted project under a trusted config is WARN. If a future change
    # flips the default to block, THIS test fails — proving warn truly = zero behavior change.
    assert _sf._anchor_enforcement(_cfg(), PROJECT) == "warn"


def test_enforcement_enforce_and_dry_run_lists():
    assert _sf._anchor_enforcement(_cfg(enforce=[PROJECT]), PROJECT) == "block"
    assert _sf._anchor_enforcement(_cfg(dry_run=[PROJECT]), PROJECT) == "dry_run"


def test_enforcement_overlap_block_wins():
    # design §4.1 table: a project in BOTH lists → enforce (stricter) wins.
    assert _sf._anchor_enforcement(_cfg(enforce=[PROJECT], dry_run=[PROJECT]), PROJECT) == "block"


def test_enforcement_config_failsafe_corrupt_is_block():
    # #6: a present-but-corrupt config (config_trusted=False) → block, NEVER silently warn.
    assert _sf._anchor_enforcement(_cfg(trusted=False), PROJECT) == "block"
    # ...even for a project absent from the (lost) enforce list — fail-closed > silent fail-open.
    assert _sf._anchor_enforcement(_cfg(trusted=False), "some-other-proj") == "block"


# ════════════════════════════════════════════════════════════════════════════
# Unit: make_anchor_decision — required / miss_reason / system audit-log.
# ════════════════════════════════════════════════════════════════════════════


def test_decision_warn_coordinator_miss_not_required():
    d = _sf.make_anchor_decision(None, cfg=_cfg(), home="/tmp", project=PROJECT,
                                 origin="coordinator", cwd="/tmp")
    assert d.required is False and d.enforcement == "warn"
    assert d.focus_line is None and d.miss_reason == "anchor-unresolved"
    assert d.origin == "coordinator"


def test_decision_enforce_coordinator_miss_required_block():
    d = _sf.make_anchor_decision(None, cfg=_cfg(enforce=[PROJECT]), home="/tmp", project=PROJECT,
                                 origin="coordinator", cwd="/tmp")
    assert d.required is True and d.enforcement == "block" and d.focus_line is None


def test_decision_resolved_anchor_has_focus_line_no_miss():
    d = _sf.make_anchor_decision("/x/y.handoff.code-workspace", cfg=_cfg(enforce=[PROJECT]),
                                 home="/tmp", project=PROJECT, origin="coordinator", cwd="/tmp")
    assert d.focus_line == "SPAWNER_FOCUS=/x/y.handoff.code-workspace\n"
    assert d.miss_reason is None  # a resolved anchor never gates


def test_decision_origin_source_is_log_only():
    # v3: origin_source carries no trust authority; an unknown value collapses to "default".
    d = _sf.make_anchor_decision(None, cfg=_cfg(), home="/tmp", project=PROJECT, cwd="/tmp",
                                 origin_source="trusted-wrapper")
    assert d.origin_source == "default"


def test_decision_system_exemption_writes_audit_log(tmp_path):
    # #5: every system无锚 pass-through is observable (design §2.2 codex R3).
    d = _sf.make_anchor_decision(None, cfg=_cfg(system_allow=[PROJECT]), home=tmp_path,
                                 project=PROJECT, origin="system", cwd="/some/cwd", callsite="spawn")
    assert d.origin == "system" and d.required is False  # exempt, not required
    audit = tmp_path / PROJECT / "spawn-anchor-system-audit.log"
    rec = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert rec["project"] == PROJECT and rec["callsite"] == "spawn" and rec["cwd"] == "/some/cwd"


# ════════════════════════════════════════════════════════════════════════════
# Unit: config parse — 空列表 vs 缺键 semantics (#11).
# ════════════════════════════════════════════════════════════════════════════


def test_config_absent_key_is_warn(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    cfg = _config.load(tmp_path)
    assert cfg.spawner_anchor_enforce_projects == [] and cfg.spawner_anchor_enforce_configured is False
    assert _sf._anchor_enforcement(cfg, PROJECT) == "warn"


def test_config_empty_list_is_warn(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"spawner_anchor_enforce_projects": []}))
    cfg = _config.load(tmp_path)
    assert cfg.spawner_anchor_enforce_projects == [] and cfg.spawner_anchor_enforce_configured is False
    assert _sf._anchor_enforcement(cfg, PROJECT) == "warn"  # explicit empty ≠ enforce-all


def test_config_listed_project_enforced(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"spawner_anchor_enforce_projects": [PROJECT]}))
    cfg = _config.load(tmp_path)
    assert cfg.spawner_anchor_enforce_configured is True
    assert _sf._anchor_enforcement(cfg, PROJECT) == "block"
    assert _sf._anchor_enforcement(cfg, "sibling") == "warn"  # lists don't bleed


def test_config_bare_string_footgun_is_warn(tmp_path):
    # a typo'd bare string must NOT iterate into chars (the mandate-parser footgun).
    (tmp_path / "config.json").write_text(json.dumps({"spawner_anchor_enforce_projects": "hf"}))
    cfg = _config.load(tmp_path)
    assert cfg.spawner_anchor_enforce_projects == [] and cfg.spawner_anchor_enforce_configured is False


def test_config_three_phase_isolation(tmp_path):
    # #8: enforce / dry_run / warn coexist, no bleed.
    (tmp_path / "config.json").write_text(json.dumps({
        "spawner_anchor_enforce_projects": ["hf"],
        "spawner_anchor_dry_run_projects": ["erp-system"],
    }))
    cfg = _config.load(tmp_path)
    assert _sf._anchor_enforcement(cfg, "hf") == "block"
    assert _sf._anchor_enforcement(cfg, "erp-system") == "dry_run"
    assert _sf._anchor_enforcement(cfg, "wilde-hexe") == "warn"


def test_config_corrupt_json_fails_trusted(tmp_path):
    (tmp_path / "config.json").write_text("{not valid json")
    cfg = _config.load(tmp_path)
    assert cfg.config_trusted is False and cfg.unified_spawn_enabled is False


# ════════════════════════════════════════════════════════════════════════════
# Integration — spawn.main (the fresh-spawn producer).
# ════════════════════════════════════════════════════════════════════════════


def _plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    return repo


def _spawn_argv(*, origin: str | None = None, focus: str | None = None,
                project: str = PROJECT, task: str = TASK, workspace: Path) -> list[str]:
    a = ["--project", project, "--task-id", task, "--role", "worker",
         "--isolation", "singlepane", "--workspace", str(workspace), "--prompt", "do it"]
    if origin is not None:
        a += ["--origin", origin]
    if focus is not None:
        a += ["--spawner-focus-path", focus]
    return a


def _uri_exists(home: Path, *, project: str = PROJECT, task: str = TASK) -> bool:
    return (home / project / "queue" / f"{task}.uri").exists()


def test_spawn_warn_miss_fail_open_byte_compat(tmp_path, monkeypatch):
    """#7: warn (empty lists) + coordinator + miss → rc 0, NO SPAWNER_FOCUS, miss logged (Step1 parity).
    DISABLE-FIX GUARD: flipping the default to block makes this assert rc==0 fail."""
    home = _home(tmp_path, monkeypatch, {})  # no anchor lists → warn
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo))  # default --origin coordinator
    assert rc == 0
    uri_text = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert "SPAWNER_FOCUS" not in uri_text  # byte-identical fail-open
    misses = (home / PROJECT / "spawn-anchor-miss.log").read_text().splitlines()
    assert len(misses) == 1 and json.loads(misses[0])["reason"] == "spawn:anchor-unresolved"


def test_spawn_enforce_coordinator_miss_fail_closed(tmp_path, monkeypatch):
    """#1: enforce + coordinator + miss → EXIT_FAIL_CLOSED + NO .uri written."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="coordinator"))
    assert rc == spawn.EXIT_FAIL_CLOSED
    assert not _uri_exists(home)  # no spawn artifact published
    # telemetry still recorded (Step 1 miss log fires before the Step 4 gate).
    assert (home / PROJECT / "spawn-anchor-miss.log").exists()


def test_spawn_enforce_coordinator_resolved_no_block(tmp_path, monkeypatch):
    """#2: enforce + coordinator + a RESOLVED anchor (valid --spawner-focus-path) → normal spawn."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    repo = _plain_repo(tmp_path)
    focus = home / "coord" / "singlepane" / "c.handoff.code-workspace"
    focus.parent.mkdir(parents=True)
    focus.write_text("{}")
    rc = spawn.main(_spawn_argv(workspace=repo, origin="coordinator", focus=str(focus)))
    assert rc == 0
    uri = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert f"SPAWNER_FOCUS={os.path.realpath(str(focus))}\n" in uri


def test_spawn_interactive_front_tty_exempt(tmp_path, monkeypatch):
    """#3: --origin interactive + front TTY + HANDOFF_UNATTENDED unset + miss → exempt (rc 0, fail-open)."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    monkeypatch.setattr(_sf, "_front_tty", lambda: True)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="interactive"))
    assert rc == 0 and not (home / PROJECT / "queue" / f"{TASK}.uri").read_text().__contains__("SPAWNER_FOCUS")


def test_spawn_interactive_unattended_env_demoted_block(tmp_path, monkeypatch):
    """#4: --origin interactive + HANDOFF_UNATTENDED set (the automated-chain marker) → demote → block."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    monkeypatch.setattr(_sf, "_front_tty", lambda: True)  # even WITH a TTY...
    monkeypatch.setenv("HANDOFF_UNATTENDED", "1")  # ...the inherited strictness env wins
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="interactive"))
    assert rc == spawn.EXIT_FAIL_CLOSED and not _uri_exists(home)


def test_spawn_interactive_headless_forgot_env_demoted_block(tmp_path, monkeypatch):
    """#4b: --origin interactive + NO front TTY + FORGOT HANDOFF_UNATTENDED → demote → block
    (physical no-TTY = physically can't be exempt — root-cause fix, not "remember the env")."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    monkeypatch.setattr(_sf, "_front_tty", lambda: False)
    monkeypatch.delenv("HANDOFF_UNATTENDED", raising=False)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="interactive"))
    assert rc == spawn.EXIT_FAIL_CLOSED and not _uri_exists(home)


def test_spawn_system_allowlisted_exempt_with_audit(tmp_path, monkeypatch):
    """#5: --origin system + project ∈ allow-list + miss → exempt (rc 0) + audit-log line."""
    home = _home(tmp_path, monkeypatch, {
        "spawner_anchor_enforce_projects": [PROJECT],
        "spawner_anchor_system_allow": [PROJECT],
    })
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="system"))
    assert rc == 0
    audit = home / PROJECT / "spawn-anchor-system-audit.log"
    assert audit.exists() and json.loads(audit.read_text().splitlines()[0])["callsite"] == "spawn"


def test_spawn_system_not_allowlisted_demoted_block(tmp_path, monkeypatch):
    """#5: --origin system + project NOT in allow-list → demote coordinator → block (no token escape)."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})  # no system_allow
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="system"))
    assert rc == spawn.EXIT_FAIL_CLOSED and not _uri_exists(home)


def test_spawn_dry_run_logs_block_intent_no_block(tmp_path, monkeypatch):
    """#7 dry_run: project in dry_run + coordinator + miss → NOT blocked (rc 0) + LOG_BLOCK_INTENT."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_dry_run_projects": [PROJECT]})
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="coordinator"))
    assert rc == 0  # shadow phase never blocks
    intent = home / PROJECT / "spawn-anchor-block-intent.log"
    rec = json.loads(intent.read_text().splitlines()[0])
    assert rec["would_block"] is True and rec["enforcement"] == "dry_run"


def test_spawn_config_failsafe_corrupt_blocks(tmp_path, monkeypatch):
    """#6: a corrupt config → config_trusted False → block (never silently warn an enforce-able dispatch).
    NB: a corrupt config also sets unified_spawn_enabled False, which spawn already refuses on — this
    asserts the dispatch is REFUSED (the safe direction), the point of the fail-safe."""
    home = _home(tmp_path, monkeypatch, write=False)
    (home / "config.json").write_text("{corrupt")
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_spawn_argv(workspace=repo, origin="coordinator"))
    assert rc == spawn.EXIT_FAIL_CLOSED and not _uri_exists(home)


# ════════════════════════════════════════════════════════════════════════════
# Integration — dump.main (the relay producer).
# ════════════════════════════════════════════════════════════════════════════


def _git_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True)
    return ws


def _dump_active(home: Path, ws: Path, monkeypatch, *, origin: str | None = None) -> int:
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "off")
    argv = ["--task", TASK, "--next", "brief", "--project", PROJECT,
            "--workspace", str(ws), "--status", "active"]
    if origin is not None:
        argv += ["--origin", origin]
    return dump.main(argv)


def test_dump_warn_miss_fail_open_byte_compat(tmp_path, monkeypatch):
    """#7: warn + miss → rc 0, .uri byte-identical (no SPAWNER_FOCUS), miss logged. DISABLE-FIX guard."""
    home = _home(tmp_path, monkeypatch, {})
    ws = _git_ws(tmp_path)
    assert _dump_active(home, ws, monkeypatch) == 0
    uri = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert uri == f"WORKSPACE={ws}\nURI={dump.build_uri(_config.load(), PROJECT, TASK)}\n"
    misses = (home / PROJECT / "spawn-anchor-miss.log").read_text().splitlines()
    assert len(misses) == 1 and json.loads(misses[0])["reason"] == "dump:anchor-unresolved"


def test_dump_enforce_coordinator_miss_fail_closed_no_half_product(tmp_path, monkeypatch):
    """#1: enforce + coordinator + miss → EXIT_FAIL_CLOSED + NO half-product (.uri AND .md absent)."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    ws = _git_ws(tmp_path)
    rc = _dump_active(home, ws, monkeypatch, origin="coordinator")
    assert rc == dump._EXIT_FAIL_CLOSED
    queue = home / PROJECT / "queue"
    assert not (queue / f"{TASK}.uri").exists()  # no .uri trigger
    assert not (queue / f"{TASK}.md").exists()   # gated BEFORE any artifact (atomic)


def test_dump_enforce_resolved_no_block(tmp_path, monkeypatch):
    """#2: enforce + a valid $HANDOFF_WINDOW_FOCUS_PATH (anchor resolves) → normal .uri, no block."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    ws = _git_ws(tmp_path)
    focus = home / "coord" / "singlepane" / "c.handoff.code-workspace"
    focus.parent.mkdir(parents=True)
    focus.write_text("{}")
    monkeypatch.setenv("HANDOFF_WINDOW_FOCUS_PATH", str(focus))
    assert _dump_active(home, ws, monkeypatch, origin="coordinator") == 0
    uri = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert f"SPAWNER_FOCUS={os.path.realpath(str(focus))}\n" in uri


def test_dump_single_parse_resolves_once(tmp_path, monkeypatch):
    """#9: AnchorDecision is resolved ONCE per dump — the 4 writers consume it, no writer re-reads
    cwd/env/cfg (TOCTOU). Counts resolve_spawner_focus_path calls across a whole active dump."""
    home = _home(tmp_path, monkeypatch, {})
    ws = _git_ws(tmp_path)
    calls: list[int] = []

    def counting(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setattr(_sf, "resolve_spawner_focus_path", counting)  # wins over conftest neutralize
    assert _dump_active(home, ws, monkeypatch) == 0
    assert len(calls) == 1  # exactly one resolution for the whole dump


def test_dump_dry_run_logs_block_intent_no_block(tmp_path, monkeypatch):
    """#7 dry_run: dump dry_run project + miss → not blocked (rc 0, byte-identical) + LOG_BLOCK_INTENT."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_dry_run_projects": [PROJECT]})
    ws = _git_ws(tmp_path)
    assert _dump_active(home, ws, monkeypatch, origin="coordinator") == 0
    uri = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert "SPAWNER_FOCUS" not in uri  # behavior unchanged in shadow phase
    intent = home / PROJECT / "spawn-anchor-block-intent.log"
    assert json.loads(intent.read_text().splitlines()[0])["enforcement"] == "dry_run"


def test_dump_config_failsafe_corrupt_blocks_active(tmp_path, monkeypatch):
    """#6: corrupt config → config_trusted False → an active coordinator dump with a miss fails closed."""
    home = _home(tmp_path, monkeypatch, write=False)
    (home / "config.json").write_text("{corrupt")
    ws = _git_ws(tmp_path)
    rc = _dump_active(home, ws, monkeypatch, origin="coordinator")
    assert rc == dump._EXIT_FAIL_CLOSED
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def _bare_and_clone(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(bare), str(ws)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=ws, check=True, capture_output=True)
    (ws / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "remote", "set-head", "origin", "main"], cwd=ws, capture_output=True)
    return ws


def test_dump_enforce_block_creates_no_orphan_worktree(tmp_path, monkeypatch):
    """#1 atomicity: under worktree isolation, the gate fires BEFORE worktree creation, so an
    enforce + coordinator + miss block leaves NO orphan worktree (the gate is placed before
    resolve_spawn_workspace, not after it)."""
    home = _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    ws = _bare_and_clone(tmp_path)
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "on")  # would create a worktree if not blocked
    rc = dump.main(["--task", TASK, "--next", "brief", "--project", PROJECT,
                    "--workspace", str(ws), "--status", "active", "--origin", "coordinator"])
    assert rc == dump._EXIT_FAIL_CLOSED
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert not (home / PROJECT / "worktrees").exists()  # no orphan worktree (gated pre-creation)


def test_dump_terminal_status_not_gated(tmp_path, monkeypatch):
    """A terminal (done) dump unlinks the .uri (no spawn) → the anchor gate must NOT fire even under
    enforce (no wrong-desktop risk; only the spawning 'active' status is gated)."""
    _home(tmp_path, monkeypatch, {"spawner_anchor_enforce_projects": [PROJECT]})
    ws = _git_ws(tmp_path)
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "off")
    rc = dump.main(["--task", TASK, "--next", "done", "--project", PROJECT,
                    "--workspace", str(ws), "--status", "done", "--origin", "coordinator"])
    assert rc == 0  # terminal closure is never blocked by the anchor gate


# ════════════════════════════════════════════════════════════════════════════
# #10 error-code / reason separation — anchor-unresolved is its own reason.
# ════════════════════════════════════════════════════════════════════════════


def test_reason_anchor_unresolved_is_distinct():
    # design §4.2: the Step 4 miss reason must never collide with Step 6's isolation-unresolved.
    assert _sf.MISS_REASON_ANCHOR == "anchor-unresolved"
    assert _sf.MISS_REASON_ANCHOR != "isolation-unresolved"
    d = _sf.make_anchor_decision(None, cfg=_cfg(enforce=[PROJECT]), home="/tmp", project=PROJECT,
                                 origin="coordinator", cwd="/tmp")
    assert d.miss_reason == "anchor-unresolved"


# ════════════════════════════════════════════════════════════════════════════
# #4c call-point contract — the default dispatch / a wrapper that omits --origin
# resolves to coordinator (never the lenient interactive).
# ════════════════════════════════════════════════════════════════════════════


def test_default_dispatch_origin_is_coordinator(tmp_path, monkeypatch):
    """#4c: a dump that omits --origin (the watchdog / queue / dx-spawn convention) is coordinator —
    the STRICTEST origin — so an automated chain can never accidentally inherit the interactive exemption."""
    _home(tmp_path, monkeypatch, {})
    d = dump._dump_anchor_decision(_config.load(), PROJECT)  # no origin kwarg = default
    assert d.origin == "coordinator"


def test_spawn_default_argv_origin_is_coordinator():
    ns = spawn._build_parser().parse_args(
        ["--project", "p", "--task-id", "t", "--isolation", "singlepane", "--prompt", "x"]
    )
    assert ns.origin == "coordinator"
