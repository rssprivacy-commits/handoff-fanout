"""audit_evidence — delivery-audit machine gate checker (`handoff audit-check`) +
owner override (`handoff audit-override`).

Closes the gap owner caught 2026-06-11: the「中枢审派出会话交付必加外双脑」iron rule
had no machine enforcement — an unaudited worker delivery could be merged, pushed and
deployed live on coordinator good will alone. The dual-brain runner now writes a
machine-readable ``<out>.evidence.json`` sidecar (the B half, in dharmaxis); this
module is the A half: given a repo + commit range, decide whether matching audit
evidence exists under ``$HANDOFF_HOME/<project>/audits/``.

Match paths (design ruled by codex+gemini dual-brain GREEN, 2026-06-12;
hardened per gate re-review sw-ag-fix1, tightened fail-closed per final-state
re-review sw-ag-fix2 — the evidence-v1 runner always emits the full git-binding
field set, so「missing optional field」is suspect, never legacy):

  1. ``target_head_sha == reviewed_head_sha`` AND ``reviewed_base_sha``
     present and equal to the target range's base (a narrow head^..head
     audit must not clear a wider origin/main..head push range); missing
     or mismatching base falls through to the patch-id path
  2. patch-id equivalence: ``git patch-id --stable`` of the target
     range matches ``reviewed_patch_id`` AND the changed-file sets
     are identical (tolerates cherry-pick / rebase — the audited
     content is equivalent even though SHAs moved). Same-base ranges
     additionally REQUIRE a matching ``diff_sha256`` byte-exact bind
     (patch-id ignores whitespace; Python indentation IS semantics);
     cross-base ranges keep whitespace tolerance by design

Verdict ruling over ALL matched evidence (priority, fail-closed):

  FAIL  — any matched RED without a valid owner override, regardless of
          other matched GREENs (conflicting verdicts on the same content
          = fail-closed; the only door out is the owner red-override);
          likewise any matched MIXED/ERROR conflicts even with a GREEN
          (only door: the audit_unavailable bypass)
  PASS_OVERRIDE — matched RED carrying ``decision=accept_with_red_override``
          plus a valid ``owner_ack`` (only producible by a human running
          ``handoff audit-override`` on a tty — AI sessions have no tty);
          loudly labelled, never rewritten to GREEN
  PASS  — matched GREEN(s) and no RED at all
  PASS_BYPASS — emergency bypass (see below)
  FAIL  — no matching evidence / matched but never completed (MIXED/ERROR)

Fail-closed posture: MIXED / ERROR overall verdicts also FAIL (an unparseable or
half-dead audit is not an audit). The emergency bypass (env + a one-time fully
filled ``audits/bypasses/<ts>.json``) applies ONLY when no matching evidence
exists or the matched audit never completed (ERROR/MIXED) — it can NEVER clear a
matched RED verdict; that path is exclusively the owner red-override (codex MUST:
``audit_unavailable_bypass`` and ``red_override`` are two distinct doors). Bypass
operational details live in the owner runbook (dharmaxis
``project-files/handoff/audit-gate-runbook.md``), deliberately NOT in any
CLAUDE.md/memory/skill hot text (gemini MUST).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from handoff_fanout import config
from handoff_fanout.atomic import atomic_replace

# sha of git's well-known empty tree object — used as the diff base for a
# root/first-push range where no parent commit exists.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# The env var name is intentionally undocumented outside code + the owner runbook.
BYPASS_ENV = "HANDOFF_AUDIT_GATE_BYPASS"
BYPASS_REQUIRED_FIELDS = ("reason", "scope", "attempt_counter", "follow_up_task_id", "expires_at")
PENDING_MARKER = ".audit_pending"

_GUIDANCE = (
    "正路：中枢对该区间跑 dual-brain-runner --evidence-repo <repo> --evidence-range "
    "<base>..<head>，--out 落 $HANDOFF_HOME/<project>/audits/ 下，evidence sidecar "
    "即被本闸识别。运维细节见 audit-gate-runbook.md（dharmaxis project-files/handoff/）。"
)


# ─── git plumbing ────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout
    return out if binary else out.decode("utf-8", "replace").strip()


def derive_project(repo: Path) -> str:
    """Project slug = basename of the MAIN repo dir (worktree-safe via git-common-dir)."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(str(common)).resolve().parent.name


@dataclass
class RangeFacts:
    base_sha: str
    head_sha: str
    patch_id: str
    diff_sha256: str
    changed_files: list[str]


def range_facts(repo: Path, base: str, head: str) -> RangeFacts:
    """Resolve a base..head range into the binding facts the evidence schema carries."""
    base_sha = str(_git(repo, "rev-parse", "--verify", base + "^{object}"))
    head_sha = str(_git(repo, "rev-parse", "--verify", head + "^{object}"))
    diff = _git(repo, "diff", f"{base_sha}..{head_sha}", binary=True)
    assert isinstance(diff, bytes)
    pid = ""
    if diff.strip():
        out = subprocess.run(
            ["git", "-C", str(repo), "patch-id", "--stable"],
            input=diff, check=True, capture_output=True,
        ).stdout.decode("utf-8", "replace").strip()
        pid = out.split()[0] if out else ""
    changed = str(_git(repo, "diff", "--name-only", f"{base_sha}..{head_sha}"))
    return RangeFacts(
        base_sha=base_sha,
        head_sha=head_sha,
        patch_id=pid,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        changed_files=sorted(line for line in changed.splitlines() if line),
    )


# ─── evidence matching ───────────────────────────────────────────────────────


def validate_owner_ack(evidence: dict) -> bool:
    """A valid owner_ack binds reason+ts to the reviewed patch-id via a checksum.

    checksum = sha256("<reviewed_patch_id>|<reason>|<ts>") — same friction model as
    task-closure confirm: the block is only producible via the interactive
    ``handoff audit-override`` (tty-gated), and any tampering with the evidence's
    patch-id, the reason or the timestamp invalidates it.
    """
    ack = evidence.get("owner_ack") or {}
    reason, ts, checksum = ack.get("reason"), ack.get("ts"), ack.get("checksum")
    pid = evidence.get("reviewed_patch_id")
    if not (reason and ts and checksum and pid):
        return False
    expect = hashlib.sha256(f"{pid}|{reason}|{ts}".encode("utf-8")).hexdigest()
    return checksum == expect


def _matches(evidence: dict, facts: RangeFacts) -> str | None:
    """Return the match path ('head_sha' | 'patch_id') or None."""
    if evidence.get("reviewed_head_sha") and evidence["reviewed_head_sha"] == facts.head_sha:
        # head_sha alone is necessary but not sufficient: a narrow audit
        # (head^..head) must not clear a wider push range (origin/main..head).
        # The base must be bound too — present AND equal. Every evidence-v1
        # producer emits reviewed_base_sha alongside reviewed_head_sha, so a
        # missing base is suspect, not legacy: no direct pass-through, fall
        # through to the patch-id path (fail-closed, sw-ag-fix2 MUST-A).
        if evidence.get("reviewed_base_sha") == facts.base_sha:
            return "head_sha"
    pid = evidence.get("reviewed_patch_id")
    if pid and facts.patch_id and pid == facts.patch_id:
        # reviewed_base_sha must be PRESENT before any patch-id tolerance
        # applies: every evidence-v1 producer emits it, so a patch-id record
        # missing the base has no legitimate production path — malformed,
        # fail-closed (sw-ag-fix3). Missing ≠ unequal: a real cherry-pick
        # evidence carries the OLD base (present but unequal), which keeps
        # the cross-base tolerance below intact.
        if not evidence.get("reviewed_base_sha"):
            return None
        reviewed_files = sorted(evidence.get("changed_files") or [])
        if reviewed_files != facts.changed_files:
            return None
        # Same base → bind the diff byte-exactly: diff_sha256 must be present
        # AND equal (patch-id ignores whitespace, but Python indentation IS
        # semantics; evidence-v1 always emits diff_sha256, so a same-base
        # record missing it is suspect — fail-closed, sw-ag-fix2 MUST-B).
        # Cross-base (cherry-pick/rebase) keeps patch-id tolerance by design.
        if evidence.get("reviewed_base_sha") == facts.base_sha and (
            not evidence.get("diff_sha256")
            or evidence["diff_sha256"] != facts.diff_sha256
        ):
            return None
        return "patch_id"
    return None


@dataclass
class CheckResult:
    ok: bool
    status: str  # PASS | PASS_OVERRIDE | PASS_BYPASS | FAIL
    reason: str
    evidence_path: str | None = None
    lines: list[str] = field(default_factory=list)


def _iter_evidence(audits_dir: Path):
    files = sorted(
        audits_dir.glob("*.evidence.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield f, data


def _try_bypass(audits_dir: Path, facts: RangeFacts, env: dict) -> CheckResult | None:
    """One-time, fully-filled, range-scoped emergency bypass. None = not in play."""
    if env.get(BYPASS_ENV) != "1":
        return None
    bdir = audits_dir / "bypasses"
    # Design MUST: one-time, range-scoped. Only the full base..head form is
    # accepted — a bare head sha would authorize any base, i.e. too wide.
    scope = f"{facts.base_sha}..{facts.head_sha}"
    candidates = sorted(bdir.glob("*.json")) if bdir.is_dir() else []
    for f in candidates:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("scope") != scope:
            continue
        # A scope-matching record is THE record for this range — any defect rejects
        # outright (no silent fall-through to a different file).
        missing = [k for k in BYPASS_REQUIRED_FIELDS if not data.get(k)]
        if missing:
            return CheckResult(False, "FAIL", f"bypass 留痕缺字段 {missing}（{f.name}）— 拒")
        if data.get("used_at"):
            return CheckResult(False, "FAIL", f"bypass 已使用过（一次性按 range 生效；{f.name}）— 拒")
        try:
            from datetime import datetime, timezone

            expires = datetime.fromisoformat(str(data["expires_at"]))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                return CheckResult(False, "FAIL", f"bypass 已过期（expires_at={data['expires_at']}）— 拒")
        except ValueError:
            return CheckResult(False, "FAIL", f"bypass expires_at 不是合法 ISO-8601（{f.name}）— 拒")
        data["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        data["used_range"] = f"{facts.base_sha}..{facts.head_sha}"
        atomic_replace(f, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return CheckResult(
            True, "PASS_BYPASS",
            f"⚠️⚠️ AUDIT-GATE BYPASS（audit_unavailable 路径 / 一次性已消耗：{f.name}）",
            evidence_path=str(f),
        )
    return CheckResult(False, "FAIL", "bypass env 已设但无 scope 匹配本区间的合法留痕文件 — 拒")


def check_range(
    repo: Path,
    base: str,
    head: str,
    audits_dir: Path,
    *,
    env: dict | None = None,
) -> CheckResult:
    env = dict(os.environ) if env is None else env
    facts = range_facts(repo, base, head)

    matched: list[tuple[Path, dict, str]] = []
    if audits_dir.is_dir():
        for f, data in _iter_evidence(audits_dir):
            path_kind = _matches(data, facts)
            if path_kind:
                matched.append((f, data, path_kind))

    # Full scan, then priority ruling (never first-match): a RED without a
    # valid owner override FAILs no matter how many GREENs also match — two
    # audits of the same content with conflicting verdicts is a conflict, and
    # fail-closed means the conflict rejects. Order/mtime plays no part.
    greens: list[tuple[Path, dict, str]] = []
    red_overridden: list[tuple[Path, dict, str]] = []
    red_plain: list[tuple[Path, dict, str]] = []
    incomplete: list[tuple[Path, dict, str]] = []  # MIXED / ERROR / anything else
    for f, data, path_kind in matched:
        verdict = data.get("overall_verdict")
        if verdict == "GREEN":
            greens.append((f, data, path_kind))
        elif verdict == "RED":
            if data.get("decision") == "accept_with_red_override" and validate_owner_ack(data):
                red_overridden.append((f, data, path_kind))
            else:
                red_plain.append((f, data, path_kind))
        else:
            incomplete.append((f, data, path_kind))

    if red_plain:
        return CheckResult(
            False, "FAIL",
            f"匹配到 RED verdict 的审计 evidence 且无合法 owner red-override（{red_plain[0][0].name}）— 拒"
            + ("（同区间另有 GREEN evidence——同内容两次审出冲突 verdict，fail-closed 拒）" if greens else "")
            + "。唯一放行路径：owner 亲手在 tty 跑 `handoff audit-override`（AI 会话禁代跑）。",
        )
    if incomplete:
        # P2 (sw-ag-fix2): a matched-but-never-completed audit (MIXED/ERROR)
        # is a conflict even when a GREEN also matches — same「冲突即拒」
        # semantics as the RED priority above. The only door out here is the
        # audit_unavailable bypass (never available for RED).
        bypass = _try_bypass(audits_dir, facts, env)
        if bypass is not None:
            return bypass
        f, data, _ = incomplete[0]
        return CheckResult(
            False, "FAIL",
            f"匹配到的审计 evidence verdict={data.get('overall_verdict')}（非 GREEN）— "
            f"fail-closed 拒（{f.name}）"
            + ("（同区间另有 GREEN evidence——同内容审出冲突 verdict，fail-closed 拒）" if greens else "")
            + "。审计未完成/不可解析不等于审过。" + _GUIDANCE,
        )
    if red_overridden:
        f = red_overridden[0][0]
        return CheckResult(
            True, "PASS_OVERRIDE",
            f"🟥➡️ PASS-with-override：{f.name} verdict=RED，owner red-override 放行"
            "（这不是 GREEN——RED 结论保留在案，放行属 owner 例外批准）",
            evidence_path=str(f),
        )
    if greens:
        f, _, path_kind = greens[0]
        return CheckResult(
            True, "PASS",
            f"✅ audit evidence 匹配（{path_kind}）：{f.name} verdict=GREEN",
            evidence_path=str(f),
        )

    bypass = _try_bypass(audits_dir, facts, env)
    if bypass is not None:
        return bypass

    return CheckResult(
        False, "FAIL",
        f"无匹配本区间的审计 evidence（target_head={facts.head_sha[:12]} "
        f"patch_id={facts.patch_id[:12] if facts.patch_id else '(empty-diff)'}）— 拒。" + _GUIDANCE,
    )


# ─── pending marker (post-merge warn path) ──────────────────────────────────


def _pending_marker(audits_dir: Path) -> Path:
    return audits_dir / PENDING_MARKER


def write_pending(audits_dir: Path, base: str, head: str, reason: str) -> None:
    audits_dir.mkdir(parents=True, exist_ok=True)
    atomic_replace(
        _pending_marker(audits_dir),
        json.dumps(
            {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "range": f"{base}..{head}", "reason": reason},
            ensure_ascii=False,
        )
        + "\n",
    )


def clear_pending(audits_dir: Path) -> None:
    try:
        _pending_marker(audits_dir).unlink()
    except FileNotFoundError:
        pass


# ─── CLI: handoff audit-check ────────────────────────────────────────────────


def _split_range(spec: str) -> tuple[str, str]:
    if "..." in spec or ".." not in spec:
        raise ValueError(f"range 必须是 <base>..<head> 形式: {spec!r}")
    base, head = spec.split("..", 1)
    if not base or not head:
        raise ValueError(f"range 必须是 <base>..<head> 形式: {spec!r}")
    return base, head


def main_check(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="handoff audit-check",
        description="Delivery-audit machine gate: does matching dual-brain evidence "
        "exist for this repo+range? exit 0 pass / 1 fail / 2 usage-or-infra error.",
    )
    ap.add_argument("--repo", required=True, help="git repo (worktree ok) the range lives in")
    ap.add_argument("--range", required=True, dest="range_spec", metavar="BASE..HEAD")
    ap.add_argument("--project", default=None, help="project slug (default: derived from repo)")
    ap.add_argument("--audits-dir", default=None, help="override audits dir (default: $HANDOFF_HOME/<project>/audits)")
    ap.add_argument(
        "--pending-marker-on-fail", action="store_true",
        help="on FAIL also write audits/.audit_pending (post-merge warn path); PASS clears it",
    )
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser()
    try:
        base, head = _split_range(args.range_spec)
        project = args.project or derive_project(repo)
        audits_dir = (
            Path(args.audits_dir).expanduser()
            if args.audits_dir
            else config.home_dir() / project / "audits"
        )
        result = check_range(repo, base, head, audits_dir)
    except (ValueError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"audit-check: 用法/git 错误: {exc} {detail}", file=sys.stderr)
        return 2

    stream = sys.stdout if result.ok else sys.stderr
    print(f"audit-check[{result.status}] {result.reason}", file=stream)
    if result.ok:
        clear_pending(audits_dir)
        return 0
    if args.pending_marker_on_fail:
        write_pending(audits_dir, base, head, result.reason)
    return 1


# ─── CLI: handoff audit-override (owner-only, tty-gated) ─────────────────────


def main_override(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="handoff audit-override",
        description="OWNER-ONLY: accept a RED-verdict audit evidence with an explicit, "
        "checksummed override record. Requires an interactive tty — AI sessions cannot run this.",
    )
    ap.add_argument("--evidence", required=True, help="path to the *.evidence.json to override")
    ap.add_argument("--reason", required=True, help="why the RED verdict is being accepted")
    args = ap.parse_args(argv)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "audit-override: 需要交互式 tty（owner 亲手跑）。AI 会话/管道环境禁代跑 — 拒。",
            file=sys.stderr,
        )
        return 1

    path = Path(args.evidence).expanduser()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"audit-override: evidence 不可读: {exc}", file=sys.stderr)
        return 1
    if evidence.get("overall_verdict") != "RED":
        print(
            f"audit-override: overall_verdict={evidence.get('overall_verdict')} ≠ RED — "
            "没有可 override 的 RED 结论。",
            file=sys.stderr,
        )
        return 1
    pid = evidence.get("reviewed_patch_id")
    if not pid:
        print(
            "audit-override: evidence 无 reviewed_patch_id（非代码绑定审计）— override 只支持代码审计 evidence。",
            file=sys.stderr,
        )
        return 1

    print(f"evidence: {path}")
    print(f"reviewed_head_sha: {evidence.get('reviewed_head_sha')}")
    print(f"reviewed_patch_id: {pid}")
    print(f"reason: {args.reason}")
    print("将写入 decision=accept_with_red_override + owner_ack（RED 结论保留在案，放行=例外批准）。")
    try:
        answer = input("输入 OVERRIDE 确认（其他任意输入取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "OVERRIDE":
        print("audit-override: 未确认，已取消。", file=sys.stderr)
        return 1

    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    evidence["decision"] = "accept_with_red_override"
    evidence["owner_ack"] = {
        "reason": args.reason,
        "ts": ts,
        "checksum": hashlib.sha256(f"{pid}|{args.reason}|{ts}".encode("utf-8")).hexdigest(),
    }
    atomic_replace(path, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(f"✅ owner red-override 已写入 {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_check())
