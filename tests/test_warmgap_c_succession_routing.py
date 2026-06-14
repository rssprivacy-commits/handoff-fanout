"""warmgap-C — audit-close 内调 succession spawn（token 闸凭证 → 路由凭证）.

Implementation plan 2026-06-11-warmgap-c-implementation-plan.md (v2, tribrain-folded):

  * §1a ``dump.main(argv, suppress_spawn_artifacts=True)`` keeps the LEDGER half of an
    active dump (.md / .queued / old_ready) and SKIPS the WINDOW-INTENT half (singlepane
    sidecar+workspace / coordinator memory baseline / .uri publish / notification).
    Default ``False`` is byte-identical v0 (§3.1 golden, asserted here by contrast and
    by the whole existing suite).
  * §1b ``audit-close --coordinator --status active`` routes on the CLOSING
    coordinator's own engine sidecar nonce (``queue/<self-task>.singlepane``):
    resolvable → SUCCESSION route (suppressed dump + in-process
    ``spawn --role supervisor_succession`` consuming the just-issued token);
    unresolvable → LEGACY route (full v0 dump publish + loud WARN + idle token).
  * §2 failure semantics: succession-route failures are ERR-FATAL + rc≠0 and NEVER
    fall back to a legacy self-publication; the remedy names whether the token was
    burned (stat, not guesswork — codex MUST#5).
  * §3.8 lock ordering: precheck → dump → audit strictly precede ``.spawn.lock``;
    the前三者 are never (re)acquired while ``.spawn.lock`` is held.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from handoff_fanout import atomic, codex_audit, dump, handoff_precheck, spawn
from handoff_fanout.spawn_lock import project_spawn_lock

PROJECT = "demo-proj"
TASK = "coord-leg-8"  # the SUCCESSOR (audit-close --task)
SELF_TASK = "coord-leg-7"  # the CLOSING coordinator (audit-close --self-task)
PRED_NONCE = "ab12cd34ef567890"


# ─── fixtures / helpers (self-contained, mirroring test_audit_close_coordinator) ──


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_WORKTREE_ISOLATION",
        "HANDOFF_SAFE_COMMIT_LOCK",
        "HANDOFF_SAFE_COMMIT_BYPASS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def notify_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """C′ sandbox + codex MUST#3 双响 probe: record every relay notification."""
    calls: list[tuple] = []
    monkeypatch.setattr(dump, "_notify", lambda *a, **k: calls.append(a))
    return calls


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str = "{}") -> Path:
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text(config)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    return home


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    """A standalone git repo with one commit and NO remote (SHOULD#9: the chain must
    never assume origin exists)."""
    ws = tmp_path / name
    ws.mkdir()
    _run(["git", "init", "--quiet", "--initial-branch=main"], ws)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)
    (ws / "README.md").write_text("base\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    return ws


def _head(ws: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _close_argv(
    ws: Path,
    *,
    task: str = TASK,
    self_task: str | None = SELF_TASK,
    coordinator: bool = True,
    status: str = "active",
) -> list[str]:
    argv = ["--task", task, "--next", "next coordinator leg", "--project", PROJECT]
    argv += ["--workspace", str(ws)]
    argv += ["--audit-mode", "empty_diff_attestation", "--audit-base", _head(ws)]
    argv += ["--status", status]
    if coordinator:
        argv.append("--coordinator")
    if self_task is not None:
        argv += ["--self-task", self_task]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    return argv


def _forge_predecessor_sidecar(home: Path, nonce: str = PRED_NONCE) -> Path:
    """The sidecar the engine wrote when the CLOSING coordinator's window was spawned
    (shape: dump's forced-singlepane coordinator sidecar, e.g. sw-coord-p11)."""
    queue = home / PROJECT / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    sidecar = queue / f"{SELF_TASK}.singlepane"
    sidecar.write_text(
        json.dumps(
            {
                "workspace": str(home / PROJECT / "singlepane" / f"{SELF_TASK}.code-workspace"),
                "role": "worker",
                "close_policy": "keep",
                "spawn_nonce": nonce,
                "predecessor_nonce": None,
                "is_coordinator": True,
            }
        )
    )
    return sidecar


def _forge_predecessor_workspace(home: Path) -> Path:
    """djs-jump-return: the OUT-OF-TREE singlepane ``.handoff.code-workspace`` the engine wrote
    when the CLOSING coordinator's window was spawned (``maybe_write_singlepane_sidecar``:
    ``ws_file = <home>/<proj>/singlepane/<task>.handoff.code-workspace``). Its existence is what
    ``derive_singlepane_focus`` keys on to self-report the predecessor's desktop."""
    ws_file = home / PROJECT / "singlepane" / f"{SELF_TASK}.handoff.code-workspace"
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text("{}")
    return ws_file


def _tokens(home: Path) -> list[Path]:
    d = home / PROJECT / "authority"
    return sorted(d.glob("succession-*.token")) if d.is_dir() else []


def _dump_argv(ws: Path, *, task: str = TASK) -> list[str]:
    return [
        "--task",
        task,
        "--next",
        "next leg",
        "--project",
        PROJECT,
        "--workspace",
        str(ws),
        "--status",
        "active",
        "--coordinator",
    ]


# ─── §3.1 golden: suppress 默认 False 零回归 / True 只砍开窗工序 ────────────────


def test_dump_default_publishes_full_window_intent_golden(tmp_path, monkeypatch, notify_calls):
    """Contrast lock (v0 unchanged): a coordinator active dump WITHOUT suppress writes
    ledger + sidecar + baseline + .uri and sends the notification — exactly today."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    rc = dump.main(_dump_argv(ws))

    assert rc == 0
    queue = home / PROJECT / "queue"
    assert (queue / f"{TASK}.md").exists()
    sidecar = json.loads((queue / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "worker" and sidecar["is_coordinator"] is True
    assert "isolation" not in sidecar, "dump's sidecar shape carries no isolation field"
    assert (queue / f"{TASK}.uri").exists()
    assert (home / PROJECT / "authority" / f"{TASK}.memory-baseline.json").exists()
    assert len(notify_calls) == 1


def test_dump_suppress_keeps_ledger_skips_window_intent(
    tmp_path, monkeypatch, notify_calls, capsys
):
    """§1a: suppress=True keeps the ledger (.md / .queued) and skips EVERY window-intent
    artifact (sidecar / workspace / coordinator baseline / .uri / notification)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    rc = dump.main(_dump_argv(ws), suppress_spawn_artifacts=True)

    assert rc == 0
    queue = home / PROJECT / "queue"
    assert (queue / f"{TASK}.md").exists(), "ledger .md must be written"
    assert (home / PROJECT / "ack" / f"{TASK}.queued").exists(), ".queued breadcrumb kept"
    assert not (queue / f"{TASK}.singlepane").exists(), "no sidecar (spawn publishes it)"
    assert not (queue / f"{TASK}.uri").exists(), "no .uri publish (spawn publishes it)"
    assert not (home / PROJECT / "singlepane").exists(), "no out-of-tree workspace file"
    assert not (home / PROJECT / "authority" / f"{TASK}.memory-baseline.json").exists(), (
        "coordinator baseline is the spawn's job on the succession route"
    )
    assert notify_calls == [], "the suppressed dump must not notify (audit-close does)"
    assert "spawn artifacts suppressed" in capsys.readouterr().out


def test_dump_cli_has_no_suppress_flag():
    """codex MUST#2: the suppress seam must NEVER enter argparse (a public flag would
    be a ready-made ledger-without-window bypass of the spawn-side G4 contract)."""
    parser = dump._build_parser()
    assert not any(
        "suppress" in opt for action in parser._actions for opt in action.option_strings
    )


# ─── §1b.1 routing probe unit matrix ──────────────────────────────────────────


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda home: None, id="sidecar-missing"),
        pytest.param(
            lambda home: _forge_predecessor_sidecar(home).write_text("{corrupt"),
            id="sidecar-corrupt-json",
        ),
        pytest.param(
            lambda home: _forge_predecessor_sidecar(home).write_text(
                json.dumps({"workspace": "x", "role": "worker"})
            ),
            id="nonce-missing",
        ),
        pytest.param(
            lambda home: _forge_predecessor_sidecar(home, nonce="NOT-HEX"),
            id="nonce-not-hex",
        ),
        # fix1 MUST-2: the probe is hex16-exact (watchdog ``is_hex16`` contract) — hex
        # strings of the wrong LENGTH must route legacy, not succession.
        pytest.param(
            lambda home: _forge_predecessor_sidecar(home, nonce="a"),
            id="nonce-hex-too-short",
        ),
        pytest.param(
            lambda home: _forge_predecessor_sidecar(home, nonce="ab12cd34ef567890" * 2 + "ab"),
            id="nonce-hex-too-long-34",
        ),
        pytest.param(
            lambda home: _forge_predecessor_sidecar(home).write_text(json.dumps(["list"])),
            id="payload-not-dict",
        ),
    ],
)
def test_predecessor_nonce_unresolvable_routes_legacy(tmp_path, monkeypatch, prepare):
    home = _home(tmp_path, monkeypatch)
    prepare(home)
    assert codex_audit._predecessor_spawn_nonce(PROJECT, SELF_TASK) is None


def test_predecessor_nonce_resolves_from_engine_sidecar(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _forge_predecessor_sidecar(home)
    assert codex_audit._predecessor_spawn_nonce(PROJECT, SELF_TASK) == PRED_NONCE


def test_predecessor_nonce_none_self_task_routes_legacy(tmp_path, monkeypatch):
    """The bootstrap leg: a dx-spawn-launched / first-generation coordinator passes no
    --self-task — the probe must short-circuit to the legacy route."""
    _home(tmp_path, monkeypatch)
    assert codex_audit._predecessor_spawn_nonce(PROJECT, None) is None


# ─── §3.2 succession 路由 e2e ─────────────────────────────────────────────────


def test_succession_route_e2e(tmp_path, monkeypatch, notify_calls, capsys):
    """The C-终局 happy path: forged engine predecessor sidecar → audit-close routes
    succession — ledger by the suppressed dump, window intent by the in-process
    ``spawn --role supervisor_succession``, token issued AND consumed in one process."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0
    queue = home / PROJECT / "queue"
    # ledger half (suppressed dump)
    assert (queue / f"{TASK}.md").exists()
    assert (home / PROJECT / "ack" / f"{TASK}.old_ready").exists(), "retro proof persisted"
    # window-intent half (spawn) — sidecar is SPAWN-shaped, not dump-shaped
    sidecar = json.loads((queue / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "supervisor_succession"
    assert sidecar["isolation"] == "singlepane", "spawn-written sidecar (suppress proven)"
    assert sidecar["close_policy"] == "close_predecessor"
    assert sidecar["predecessor_nonce"] == PRED_NONCE
    assert re.fullmatch(r"[0-9a-f]+", sidecar["spawn_nonce"])
    assert "is_coordinator" not in sidecar, "spawn sidecar proves 中枢 via role (plan §1c)"
    # .uri: WORKSPACE is the REAL repo (singlepane), prompt 同文 encode_short_prompt + 🆔
    uri_text = (queue / f"{TASK}.uri").read_text()
    assert f"WORKSPACE={ws}" in uri_text
    expected_prompt = (
        f"🆔{TASK} 自动接续 / project=`{PROJECT}` / task=`{TASK}` — "
        f"open `{home}/{PROJECT}/queue/{TASK}.md` "
        f"and continue per the baseline + reading list."
    )
    assert urllib.parse.quote(expected_prompt, safe="") in uri_text
    # token: issued AND consumed inside this very process (routing credential)
    assert _tokens(home) == [], "the routing token must be consumed, never idle"
    log = (home / PROJECT / "authority" / "succession-audit.log").read_text()
    assert "ISSUED" in log and "CONSUMED" in log
    # G3 baseline for the NEW coordinator leg — written by the spawn (suppressed dump
    # skipped its own; see test_dump_suppress_keeps_ledger_skips_window_intent)
    assert (home / PROJECT / "authority" / f"{TASK}.memory-baseline.json").exists()
    # exactly ONE relay notification, sent by audit-close after the spawn (MUST#3)
    assert len(notify_calls) == 1
    out = capsys.readouterr()
    assert "succession-spawned" in out.out
    assert "spawn artifacts suppressed" in out.out
    assert "legacy-relay" not in out.err


def test_succession_marks_predecessor_done(tmp_path, monkeypatch, notify_calls, capsys):
    """focusjump-fix S2: a successful succession spawn marks the DIRECT predecessor
    (``--self-task``) terminal by writing ``queue/<predecessor>.done`` so the SHARED identity
    resolver skips its now-stale ``.singlepane`` sidecar (the L2 ambiguity root cause). The
    marker carries diagnostic JSON; it is the predecessor's task, NOT the successor's."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0
    queue = home / PROJECT / "queue"
    done = queue / f"{SELF_TASK}.done"  # the PREDECESSOR (closing coordinator), not TASK
    assert done.exists(), "the direct predecessor must be marked .done after a succession spawn"
    payload = json.loads(done.read_text())
    assert payload["done_by"] == "succession_relay"
    assert payload["successor_task"] == TASK
    # the predecessor's stale sidecar still exists (S2 only writes .done; workspace GC is S4) but
    # the resolver now skips it because of the .done marker — prove the co-located skip key exists.
    assert (queue / f"{SELF_TASK}.singlepane").exists(), "S2 does not delete the sidecar (that is S4 GC)"
    assert "succession-predecessor-done" in capsys.readouterr().out


def test_succession_predecessor_done_write_failure_is_fail_open(
    tmp_path, monkeypatch, notify_calls, capsys
):
    """focusjump-fix S2 fail-open red line: a ``.done`` write failure NEVER fails the
    succession —去程/清理是 best-effort hygiene, the relay still spawns the successor (rc=0)
    and emits a loud WARN (禁止静默降级)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    real_write_text = Path.write_text

    def _boom_on_predecessor_done(self, *args, **kwargs):
        if self.name == f"{SELF_TASK}.done":
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _boom_on_predecessor_done)
    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0, "a .done write failure must NOT fail the succession (fail-open)"
    # the successor window intent still published (the relay ran to completion)
    assert (home / PROJECT / "queue" / f"{TASK}.singlepane").exists()
    assert len(notify_calls) == 1
    err = capsys.readouterr().err
    assert "WARN succession-predecessor-done-failed" in err, "禁止静默降级: must warn visibly"


def test_succession_self_report_writes_spawner_focus_to_uri(tmp_path, monkeypatch, notify_calls):
    """djs-jump-return Part A: when the PREDECESSOR coordinator's singlepane workspace file
    exists, the succession spawn writes ``SPAWNER_FOCUS=<predecessor workspace>`` into the
    successor .uri — DERIVED from ``--self-task`` (no env channel). The watchdog/code-router
    focus-jumps the new coordinator window to the PREDECESSOR's desktop (not project-mapped)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)
    pred_ws = _forge_predecessor_workspace(home)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0
    uri_text = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    # the SPAWNER_FOCUS line points at the predecessor's OWN workspace (realpath-normalized
    # by the shared validate_spawner_focus gate the produce site re-runs)
    import os as _os

    assert f"SPAWNER_FOCUS={_os.path.realpath(str(pred_ws))}\n" in uri_text
    # still the SUCCESSOR's real-repo workspace + succession sidecar (no regression)
    assert f"WORKSPACE={ws}" in uri_text
    sidecar = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "supervisor_succession"


def test_succession_no_predecessor_workspace_omits_spawner_focus_byte_compat(
    tmp_path, monkeypatch, notify_calls
):
    """字节级向后兼容红线: a succession whose predecessor has NO singlepane workspace file
    (bootstrap leg / file gone) derives None → the .uri carries NO SPAWNER_FOCUS line, exactly
    like before this feature. Fail-open to today's per-project goto, zero regression."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)
    # NOTE: deliberately do NOT forge the predecessor workspace file

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0
    uri_text = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert "SPAWNER_FOCUS=" not in uri_text, "no workspace → no jump line (byte-compat)"
    assert f"WORKSPACE={ws}" in uri_text


def test_succession_route_worktree_mode_on_never_creates_worktree(
    tmp_path, monkeypatch, notify_calls, capsys
):
    """fix1 MUST-1: a MODE_ON project (config ``worktree_projects``) closing via the
    succession route must NOT resolve a successor worktree — the succession spawn is
    invariantly a singlepane window on the SOURCE tree (warmgap design Q3), so a
    worktree here would split the ledger from the window and orphan the worktree.
    Reachable paths: a singlepane→worktree config migration, or the healthy leg after
    a degraded-worktree leg."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    # Step1 A-收口 MUST 3 fail-closes a MODE_ON coordinator close on a no-remote repo
    # BEFORE routing — real MODE_ON projects have an origin, so stub one (name-only
    # check; nothing fetches it on the succession route since no worktree is made).
    _run(["git", "remote", "add", "origin", str(tmp_path / "origin-stub")], ws)
    _forge_predecessor_sidecar(home)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0
    assert not (home / PROJECT / "worktrees").exists(), "no successor worktree created"
    assert not (home / PROJECT / "ack" / f"{TASK}.worktree").exists(), "no .worktree ack"
    # the ledger .md points at the SOURCE tree (no worktree banner / path anywhere)
    md_text = (home / PROJECT / "queue" / f"{TASK}.md").read_text()
    assert f"cd {ws}" in md_text
    assert "worktrees" not in md_text
    # window intent: spawn-shaped singlepane sidecar + .uri on the source tree
    sidecar = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "supervisor_succession"
    assert sidecar["isolation"] == "singlepane"
    assert f"WORKSPACE={ws}" in (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    out = capsys.readouterr()
    assert "succession-spawned" in out.out
    assert "legacy-relay" not in out.err


# ─── §3.3 legacy 路由 e2e（自举腿） ────────────────────────────────────────────


def test_legacy_route_e2e_no_predecessor_sidecar(tmp_path, monkeypatch, notify_calls, capsys):
    """Bootstrap leg: --self-task given but NO engine sidecar exists → the FULL v0 dump
    publication (dump-shaped sidecar role=worker + .uri by dump) + loud WARN + the token
    issued and left idle (today's D1-ruled behavior). role=worker keeps the watchdog's
    autoclose on its silent-skip path → no ``autoclose_failed`` debris is ever staged."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0
    queue = home / PROJECT / "queue"
    sidecar = json.loads((queue / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "worker", "watchdog autoclose role gate → silent skip"
    assert sidecar["close_policy"] == "keep"
    assert sidecar["is_coordinator"] is True
    assert "isolation" not in sidecar, "dump-shaped sidecar = the v0 publication ran"
    assert (queue / f"{TASK}.uri").exists(), ".uri published by dump (v0)"
    assert (home / PROJECT / "ack" / f"{TASK}.old_ready").exists()
    # token: issued, NOT consumed (gate credential, idle-expires — D1 expected-idle)
    assert len(_tokens(home)) == 1
    log = (home / PROJECT / "authority" / "succession-audit.log").read_text()
    assert "ISSUED" in log and "CONSUMED" not in log
    # codex SHOULD#4: nothing on this path may stage autoclose-failure debris
    ack = home / PROJECT / "ack"
    assert not list(ack.glob("*autoclose_failed*"))
    assert len(notify_calls) == 1, "exactly one notification (dump's own)"
    out = capsys.readouterr()
    assert "WARN legacy-relay" in out.err
    assert SELF_TASK in out.err, "the WARN must name the nonce-less predecessor"
    assert "succession-authority-issued" in out.out


def test_legacy_route_without_self_task(tmp_path, monkeypatch, notify_calls, capsys):
    """No --self-task at all (first-generation coordinator): same legacy publication."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    rc = codex_audit.main_audit_close(_close_argv(ws, self_task=None))

    assert rc == 0
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert len(_tokens(home)) == 1
    assert "WARN legacy-relay" in capsys.readouterr().err


# ─── §3.4 fail-closed 矩阵（§2 失败语义） ─────────────────────────────────────


def test_succession_issue_token_oserror_is_fatal(tmp_path, monkeypatch, notify_calls, capsys):
    """§2 row 2: issue_token OSError on the SUCCESSION route → ERR-FATAL + rc≠0 (v0 was
    WARN — only honest because dump had already published; here NOTHING window-side is
    published, so failing closed is finally truthful). Ledger artifacts stay (harmless)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    def _boom(**kwargs):
        raise OSError("disk says no")

    monkeypatch.setattr(codex_audit._authority, "issue_token", _boom)
    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 1
    queue = home / PROJECT / "queue"
    assert (queue / f"{TASK}.md").exists(), "ledger may stay (no window intent rides it)"
    assert not (queue / f"{TASK}.uri").exists(), "NO window intent may be published"
    assert not (queue / f"{TASK}.singlepane").exists()
    assert notify_calls == [], "a failed relay must not notify"
    err = capsys.readouterr().err
    assert "ERR-FATAL succession-authority-unissued" in err
    assert "re-run audit-close" in err, "the remedy must be named"


def test_succession_spawn_rejected_preconsume_token_unconsumed(
    tmp_path, monkeypatch, notify_calls, capsys
):
    """§2 row 3 — REAL pre-consume rejection: ``unified_spawn_enabled: false`` (the
    one-key rollback switch) rejects in ``run_spawn`` BEFORE ``consume_token`` runs
    (spawn.py: config check precedes the consume). Remedy says NOT consumed (stat,
    codex MUST#5); rc≠0; no window intent; NEVER a silent legacy fallback."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"unified_spawn_enabled": False}))
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 1
    [token] = _tokens(home)
    assert token.exists(), "the rejected spawn must not have burned the token"
    queue = home / PROJECT / "queue"
    assert not (queue / f"{TASK}.uri").exists()
    assert not (queue / f"{TASK}.singlepane").exists()
    assert notify_calls == []
    err = capsys.readouterr().err
    assert "ERR-FATAL succession-spawn-failed" in err
    assert "NOT consumed" in err
    assert "dx-spawn-session.sh --coordinator" in err, "escape hatch must be named"


def test_succession_spawn_lock_rejected_token_burned_truthfully_reported(
    tmp_path, monkeypatch, notify_calls, capsys
):
    """§2 lock-rejection sub-case — REALITY CHECK (plan §2 row 3 falsified by code):
    ``run_spawn`` consumes the token BEFORE ``_spawn_singlepane`` takes the project
    ``.spawn.lock`` (consume → lock order; spawn.py is a zero-change red line this
    slice), so a lock rejection BURNS the token. What this locks: the codex-MUST#5
    discrimination must report that truthfully (stat says gone → "BURNED" + re-run
    remedy), never claim the token survived."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    with project_spawn_lock(PROJECT, root=home):  # a concurrent spawn in flight
        rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 1
    assert _tokens(home) == [], "consume precedes the lock → the token IS burned"
    queue = home / PROJECT / "queue"
    assert not (queue / f"{TASK}.uri").exists()
    assert not (queue / f"{TASK}.singlepane").exists()
    assert notify_calls == []
    err = capsys.readouterr().err
    assert "ERR-FATAL succession-spawn-failed" in err
    assert "BURNED" in err, "stat-based remedy must report the burn truthfully"
    assert "re-run audit-close" in err


def test_succession_spawn_produce_failure_token_burned_then_rerun_succeeds(
    tmp_path, monkeypatch, notify_calls, capsys
):
    """§2 row 4 + the idempotent re-run chain: the spawn consumes the token, then its
    produce step fails (rolled back) → remedy says token BURNED → a FRESH audit-close
    re-issues and the whole relay succeeds (ledger overwrite is harmless)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    real_produce = spawn._produce_singlepane

    def _produce_boom(**kwargs):
        return spawn.EXIT_FAIL_CLOSED  # post-consume produce failure (rolled back)

    monkeypatch.setattr(spawn, "_produce_singlepane", _produce_boom)
    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 1
    assert _tokens(home) == [], "the token was consumed (burned) before the failure"
    queue = home / PROJECT / "queue"
    assert not (queue / f"{TASK}.uri").exists()
    err = capsys.readouterr().err
    assert "ERR-FATAL succession-spawn-failed" in err and "BURNED" in err
    assert "re-run audit-close" in err

    # ── the named remedy actually works: re-run audit-close end to end ──
    monkeypatch.setattr(spawn, "_produce_singlepane", real_produce)
    notify_calls.clear()
    rc2 = codex_audit.main_audit_close(_close_argv(ws))

    assert rc2 == 0
    sidecar = json.loads((queue / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "supervisor_succession"
    assert (queue / f"{TASK}.uri").exists()
    assert _tokens(home) == [], "fresh token re-issued and consumed"
    assert len(notify_calls) == 1
    assert "succession-spawned" in capsys.readouterr().out


def test_terminal_close_never_routes_succession(tmp_path, monkeypatch, notify_calls):
    """A --status done close has no successor: even WITH a resolvable predecessor nonce
    the route decision must not fire (no token, no spawn, terminal artifacts only)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    rc = codex_audit.main_audit_close(_close_argv(ws, status="done"))

    assert rc == 0
    assert (home / PROJECT / "queue" / f"{TASK}.done").exists()
    assert _tokens(home) == []
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_non_coordinator_close_ignores_predecessor_sidecar(tmp_path, monkeypatch):
    """Routing is scoped to --coordinator: a plain close next to a forged sidecar stays
    the v0 non-coordinator dump (no token, no succession spawn)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    rc = codex_audit.main_audit_close(_close_argv(ws, coordinator=False, self_task=None))

    assert rc == 0
    assert _tokens(home) == []
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists(), "the v0 dump published"
    # not a singlepane-opted project + not a coordinator → no sidecar at all (v0)
    assert not (home / PROJECT / "queue" / f"{TASK}.singlepane").exists()


# ─── §3.8 锁序（precheck → dump → audit → .spawn；持 .spawn 禁取前三者） ────────


def test_lock_ordering_succession_chain(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    _forge_predecessor_sidecar(home)

    events: list[str] = []
    real_acquire = atomic.acquire_dir_lock
    real_spawn_lock = spawn.project_spawn_lock

    import contextlib

    @contextlib.contextmanager
    def traced_acquire(path, **kwargs):
        events.append(f"acquire:{Path(path).name}")
        with real_acquire(path, **kwargs):
            yield

    @contextlib.contextmanager
    def traced_spawn_lock(project, **kwargs):
        events.append("spawn-lock:enter")
        with real_spawn_lock(project, **kwargs):
            yield
        events.append("spawn-lock:exit")

    monkeypatch.setattr(atomic, "acquire_dir_lock", traced_acquire)
    monkeypatch.setattr(codex_audit.atomic, "acquire_dir_lock", traced_acquire)
    monkeypatch.setattr(spawn, "project_spawn_lock", traced_spawn_lock)

    rc = codex_audit.main_audit_close(_close_argv(ws))
    assert rc == 0

    def first(name: str) -> int:
        return next(i for i, e in enumerate(events) if name in e)

    i_pre, i_dump, i_audit = first("precheck.lock"), first("dump.lock"), first(".audit.lock")
    i_spawn = first("spawn-lock:enter")
    assert i_pre < i_dump < i_audit < i_spawn, events
    # 持 .spawn.lock 时禁取 precheck/dump/audit（§1b.3 锁序契约）
    held = False
    for e in events:
        if e == "spawn-lock:enter":
            held = True
        elif e == "spawn-lock:exit":
            held = False
        elif held and e.startswith("acquire:"):
            pytest.fail(f"acquired {e} while holding .spawn.lock: {events}")
