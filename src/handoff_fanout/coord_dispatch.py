"""``handoff coord-dispatch`` — low-friction coordinator fan-out with a HARD
machine-judged concurrency-conflict gate.

Why this exists (大白话)
------------------------
A supervisor-coordinator dispatches workers by hand-writing a brief and shelling
out to ``dx-spawn-session.sh`` — enough friction that it would rather do the work
itself, and enough rope to fan out concurrent workers that collide. This command
removes the friction (it drafts each worker brief from a declared schema) AND
welds the owner's standing law — *prove a batch can run in parallel before you
fan it out* (feedback-supervisor-center-duty §六) — into a deterministic gate.

Design posture (owner law §六)
------------------------------
* **Default serial.** Parallel is the *optimization exception a coordinator must
  EARN by proving "safe to parallelize"*, never the default. So the gate is
  fail-closed: a batch is declared ``SAFE-PARALLEL`` only when *every* pair is
  provably disjoint; any doubt → ``MUST-SERIAL``.
* **Machine-judge declared fields ONLY.** The conflict verdict reads the task's
  *declared* schema fields (``predicted_files`` / ``repo_branch`` / ``will_push``
  / ``worktree_isolation`` / ``shared_writes`` / ``credential_scopes`` /
  ``runtime_targets``). It runs NO heuristic, NO AST parse, NO LLM guess — the
  brief is explicit that those are forbidden (they would turn an auditable gate
  into an opaque one). The soft dimensions (logical independence, resource
  bounds) are surfaced for the human/owner to eyeball in the dry-run table.
* **dry-run by default.** It prints the batch plan + the full pairwise conflict
  table + each generated brief; it writes no ``.uri`` and spawns nothing.
  ``--execute`` is the only path that actually dispatches, and it REFUSES a batch
  that is not ``SAFE-PARALLEL`` (the gate would be theatre otherwise).
* **Reuse, never re-implement spawn.** The actual spawn is a thin adapter that
  shells out to the existing ``dx-spawn-session.sh`` (``$DX_SPAWN_SH``); this
  module never re-implements the spawn engine, never touches the watchdog /
  launcher, and never alters the owner's worker-dispatch two-stage confirm.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from handoff_fanout import config as _config

# Kebab-case identity — same slug contract the engine uses (spawn._SLUG_RE).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

# Sentinel string values a declared list-or-"none" field may carry.
NONE_TOKEN = "none"
UNKNOWN_TOKEN = "unknown"

# Glob metacharacters — an entry containing any is a pattern, not a literal path.
_GLOB_META = ("*", "?", "[")

EXIT_OK = 0
EXIT_FAIL = 2  # fail-closed: invalid input / refused unsafe --execute / dispatch failure

# Verdicts.
SAFE_PARALLEL = "SAFE-PARALLEL"
MUST_SERIAL = "MUST-SERIAL"

# The three opaque-token "shared resource" dimensions, compared by set-intersection.
_SHARED_DIMS = ("shared_writes", "credential_scopes", "runtime_targets")


def _err(msg: str) -> None:
    print(f"❌ [coord-dispatch] {msg}", file=sys.stderr)


# ─── schema parsing ────────────────────────────────────────────────────────────


@dataclass
class Task:
    """One declared dispatch task (verbatim from the tasks-json)."""

    task_id: str
    project: str
    purpose_plain: str
    brief_points: list[str]
    raw: dict  # the original object, for accessing conflict fields with explicit presence


def _load_tasks(path: Path) -> list[dict]:
    """Parse the tasks-json. Accepts a top-level list or ``{"tasks": [...]}``.

    Raises ``ValueError`` with a human message on any structural problem (the
    caller maps that to a fail-closed exit 2)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ValueError(f"tasks-json not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"tasks-json is not valid JSON ({path}): {e}") from e
    except OSError as e:
        raise ValueError(f"tasks-json unreadable ({path}): {e}") from e

    if isinstance(data, dict):
        data = data.get("tasks")
    if not isinstance(data, list) or not data:
        raise ValueError(
            "tasks-json must be a non-empty JSON list of task objects "
            '(or {"tasks": [ ... ]})'
        )
    for i, t in enumerate(data):
        if not isinstance(t, dict):
            raise ValueError(f"task #{i} is not a JSON object: {t!r}")
    return data


def _parse_identity(raw: dict, index: int) -> Task:
    """Validate the identity + brief-required fields, the ones with NO fail-closed
    soft path: a task that can't be identified, located, or whose plain-language
    purpose is absent cannot be dispatched at all (vs. the conflict fields, whose
    absence is a *soft* MUST-SERIAL verdict). Raises ``ValueError`` on any miss."""
    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not _SLUG_RE.match(task_id) or len(task_id) > 60:
        raise ValueError(
            f"task #{index}: task_id must be kebab-case (a-z 0-9 -, ≤60): {task_id!r}"
        )
    project = raw.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError(f"task {task_id!r}: project (full path) is required")
    # req1 + 3.4: purpose_plain is the plain-language anchor. Missing → REFUSE (the
    # whole point is that the owner, who can't read code, gets a human sentence).
    purpose = raw.get("purpose_plain")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError(
            f"task {task_id!r}: purpose_plain (大白话目的) is REQUIRED — refusing to "
            "generate a brief without it (req1)"
        )
    bp = raw.get("brief_points", [])
    if bp is None:
        bp = []
    if not isinstance(bp, list) or not all(isinstance(x, str) for x in bp):
        raise ValueError(f"task {task_id!r}: brief_points must be a list of strings if present")
    return Task(
        task_id=task_id,
        project=project.strip(),
        purpose_plain=purpose.strip(),
        brief_points=[x for x in bp if x.strip()],
        raw=raw,
    )


# ─── conflict profile (declared-field extraction) ──────────────────────────────


@dataclass
class ConflictProfile:
    """The machine-judgeable conflict surface extracted from a task's declared
    fields. ``field_issues`` (any missing / "unknown" relevant field) taints EVERY
    pair the task is in (fail-closed). ``files_indeterminate`` means the file set
    cannot be enumerated (unexpandable glob / missing predicted_files) → it can't
    be proven file-disjoint from anything."""

    task_id: str
    project_root: str  # realpath of the task's project dir (anchors file paths)
    files_concrete: set[str] = field(default_factory=set)  # abs paths (literals + expanded globs)
    glob_patterns: set[str] = field(default_factory=set)  # abs glob patterns (for fnmatch vs literals)
    files_indeterminate: bool = False
    file_notes: list[str] = field(default_factory=list)  # why indeterminate (for the table)
    repo_branch: str | None = None
    will_push: bool | None = None
    worktree_isolation: bool | None = None
    shared: dict[str, set[str] | None] = field(default_factory=dict)  # dim → set, or None=missing/unknown
    field_issues: list[str] = field(default_factory=list)


def _has_glob(entry: str) -> bool:
    return any(meta in entry for meta in _GLOB_META)


def _anchor(project_root: str, entry: str) -> str:
    """Anchor a predicted-files entry to a *canonical* absolute path.

    Absolute entries are taken as-is; relative entries join the project root.
    The result is ``os.path.realpath``-canonicalized so that two spellings that
    reach the SAME real file — e.g. ``link/foo.py`` and ``src/foo.py`` when
    ``link`` is a symlink to ``src`` — collapse to ONE absolute path *before*
    disjointness is judged. Without this, the distinct strings would falsely read
    as file-disjoint → a false ``SAFE-PARALLEL`` → two workers clobbering one file.

    ``realpath`` resolves every symlink in the existing path prefix; a not-yet-
    created trailing component (a new file declared in advance) is kept literally,
    so a planned file need not exist on disk. Anchoring at the realpath of each
    task's project root still keeps two projects' identical relative paths on
    distinct absolute paths — cross-project tasks stay provably file-disjoint."""
    raw = entry if os.path.isabs(entry) else os.path.join(project_root, entry)
    return os.path.realpath(raw)


def _parse_shared_dim(raw: dict, key: str) -> set[str] | None:
    """A list-or-"none" declared field → a set of tokens, or ``None`` when the
    field is missing / "unknown" / malformed (fail-closed: caller treats None as a
    field issue → MUST-SERIAL)."""
    if key not in raw:
        return None
    val = raw[key]
    if isinstance(val, str):
        if val.strip().lower() == NONE_TOKEN:
            return set()  # explicitly shares nothing on this dimension
        return None  # any other bare string (incl. "unknown") → fail-closed
    if isinstance(val, list):
        if any((not isinstance(x, str)) or x.strip().lower() == UNKNOWN_TOKEN for x in val):
            return None
        return {x.strip() for x in val if x.strip()}
    return None


def _parse_bool_field(raw: dict, key: str) -> bool | None:
    """A declared bool field → its value, or ``None`` when missing / "unknown" /
    non-bool (fail-closed)."""
    if key not in raw:
        return None
    val = raw[key]
    if isinstance(val, bool):
        return val
    return None  # "unknown", strings, numbers → can't trust → fail-closed


# git ref-prefix spellings of a *local* branch — all denote the same branch.
_BRANCH_REF_PREFIXES = ("refs/heads/", "heads/")


def _normalize_repo_branch(rb: str) -> str:
    """Canonicalize a self-reported ``repo_branch`` so ref-prefix aliases of the
    *same* branch compare equal — e.g. ``proj@main`` ≡ ``proj@refs/heads/main`` ≡
    ``proj@heads/main``. Without this, an aliased spelling slips past the
    same-repo+branch-push rule (string mismatch → false ``SAFE-PARALLEL`` → two
    pushes racing the same branch).

    The convention is ``<repo>@<branch>``; we split on the LAST ``@`` (a repo
    identifier such as ``git@host:org/repo`` may itself contain ``@``, but a
    branch ref does not), normalize only the branch component's ref prefix, and
    rejoin. A bare value with no ``@`` is treated as the branch."""
    rb = rb.strip()
    repo, sep, branch = rb.rpartition("@")
    if not sep:  # no "@": the whole value is the branch
        repo, branch = "", rb
    for prefix in _BRANCH_REF_PREFIXES:
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            break
    return f"{repo}@{branch}" if sep else branch


def build_conflict_profile(task: Task) -> ConflictProfile:
    """Extract the declared-field conflict surface for one task. Pure (no spawn,
    no mutation) except read-only filesystem glob expansion against the project."""
    project_root = os.path.realpath(task.project)
    prof = ConflictProfile(task_id=task.task_id, project_root=project_root)

    if not os.path.isdir(project_root):
        prof.field_issues.append(f"project dir does not exist: {task.project}")

    # ── predicted_files (required) ──
    pf = task.raw.get("predicted_files")
    if not isinstance(pf, list) or not pf or not all(isinstance(x, str) and x.strip() for x in pf):
        prof.field_issues.append("predicted_files missing / empty / not a list of paths")
        prof.files_indeterminate = True
        prof.file_notes.append("no usable predicted_files → file set unknown")
    else:
        for entry in pf:
            entry = entry.strip()
            # A trailing separator declares a *directory* — capture it before
            # _anchor (realpath strips it).
            declared_dir = entry.endswith("/") or entry.endswith(os.sep)
            # _anchor realpath-canonicalizes (resolves symlinks in the prefix), so
            # the static prefix of a glob is canonicalized for free (the glob
            # metachar tail is a non-existent component → kept literal).
            anchored = _anchor(project_root, entry)
            if _has_glob(entry):
                prof.glob_patterns.add(anchored)
                matches = glob.glob(anchored, recursive=True)
                if matches:
                    # canonicalize each match too: glob traverses INTO symlinks and
                    # yields them unresolved, so two globs reaching one real file
                    # would otherwise look disjoint.
                    prof.files_concrete.update(os.path.realpath(m) for m in matches)
                else:
                    # "无法展开的 glob 不得当空集" — a glob that matches nothing on disk
                    # (incl. one whose prefix dir is missing / an unresolvable symlink)
                    # cannot be proven to touch any specific (or no) file → indeterminate.
                    prof.files_indeterminate = True
                    prof.file_notes.append(f"unexpandable glob (0 matches): {entry}")
            elif os.path.isdir(anchored):
                # A directory entry claims the WHOLE subtree, including files not
                # created yet — a recursive listing of today's files can't prove
                # what it will touch tomorrow → fail-closed indeterminate.
                prof.files_indeterminate = True
                prof.file_notes.append(f"directory entry (claims whole subtree): {entry}")
            elif declared_dir:
                # A trailing-slash entry that doesn't exist is a directory claim we
                # can't even enumerate → fail-closed.
                prof.files_indeterminate = True
                prof.file_notes.append(f"non-existent directory entry: {entry}")
            else:
                # literal file — a new file need not exist yet; keep it as a
                # realpath-canonical concrete path.
                prof.files_concrete.add(anchored)

    # ── repo / branch / push / isolation ──
    rb = task.raw.get("repo_branch")
    if not isinstance(rb, str) or not rb.strip() or rb.strip().lower() == UNKNOWN_TOKEN:
        prof.field_issues.append("repo_branch missing / unknown")
    else:
        prof.repo_branch = _normalize_repo_branch(rb)

    prof.will_push = _parse_bool_field(task.raw, "will_push")
    if prof.will_push is None:
        prof.field_issues.append("will_push missing / unknown / non-bool")

    prof.worktree_isolation = _parse_bool_field(task.raw, "worktree_isolation")
    if prof.worktree_isolation is None:
        prof.field_issues.append("worktree_isolation missing / unknown / non-bool")

    # ── shared write / credential / runtime dims ──
    for dim in _SHARED_DIMS:
        parsed = _parse_shared_dim(task.raw, dim)
        prof.shared[dim] = parsed
        if parsed is None:
            prof.field_issues.append(f"{dim} missing / unknown / malformed (need a list or \"none\")")

    return prof


# ─── pairwise conflict analysis ────────────────────────────────────────────────


def _files_overlap(a: ConflictProfile, b: ConflictProfile) -> tuple[bool, list[str]]:
    """Return (overlap, reasons). Overlap is conservative/fail-closed: an
    indeterminate file set on EITHER side counts as overlap (cannot prove
    disjoint). Otherwise concrete∩concrete, plus a glob pattern on one side that
    ``fnmatch``-matches a concrete path on the other (catches "my glob will match
    your new file")."""
    reasons: list[str] = []
    if a.files_indeterminate or b.files_indeterminate:
        notes = a.file_notes + b.file_notes
        reasons.append(
            "file set indeterminate → cannot prove disjoint"
            + (f" [{'; '.join(notes)}]" if notes else "")
        )
        return True, reasons

    inter = a.files_concrete & b.files_concrete
    if inter:
        reasons.append(f"predicted_files overlap: {sorted(inter)}")

    for patt in a.glob_patterns:
        hits = sorted(p for p in b.files_concrete if fnmatch.fnmatch(p, patt))
        if hits:
            reasons.append(f"{a.task_id} glob {patt!r} matches {b.task_id} files {hits}")
    for patt in b.glob_patterns:
        hits = sorted(p for p in a.files_concrete if fnmatch.fnmatch(p, patt))
        if hits:
            reasons.append(f"{b.task_id} glob {patt!r} matches {a.task_id} files {hits}")

    return (bool(reasons), reasons)


def analyze_pair(a: ConflictProfile, b: ConflictProfile) -> tuple[str, list[str]]:
    """Verdict for one unordered pair. ``MUST-SERIAL`` if any reason fires, else
    ``SAFE-PARALLEL``. Reasons accumulate (a pair may collide on several axes)."""
    reasons: list[str] = []

    # fail-closed: any missing/unknown relevant field on EITHER task taints the pair.
    for prof in (a, b):
        if prof.field_issues:
            reasons.append(
                f"fail-closed: {prof.task_id} has incomplete/unknown fields "
                f"({'; '.join(prof.field_issues)})"
            )

    # file set overlap (hard judge ②).
    overlap, freasons = _files_overlap(a, b)
    if overlap:
        reasons.extend(freasons)

    # same repo+branch push without full worktree isolation (judge ③ — git push).
    if (
        a.repo_branch is not None
        and b.repo_branch is not None
        and a.repo_branch == b.repo_branch
        and (a.will_push or b.will_push)
        and (a.worktree_isolation is False or b.worktree_isolation is False)
    ):
        reasons.append(
            f"same repo+branch push without full worktree isolation: {a.repo_branch}"
        )

    # shared write / credential / runtime intersection (judge ③ — shared state).
    for dim in _SHARED_DIMS:
        sa, sb = a.shared.get(dim), b.shared.get(dim)
        if sa is not None and sb is not None:  # both declared (None handled by field_issues)
            inter = sa & sb
            if inter:
                reasons.append(f"shared {dim}: {sorted(inter)}")

    return (MUST_SERIAL if reasons else SAFE_PARALLEL, reasons)


@dataclass
class PairVerdict:
    a: str
    b: str
    verdict: str
    reasons: list[str]


@dataclass
class BatchAnalysis:
    profiles: list[ConflictProfile]
    pairs: list[PairVerdict]

    @property
    def parallel_safe(self) -> bool:
        """A batch is parallel-safe only if EVERY pair is SAFE-PARALLEL. A single
        task (no pairs) is trivially safe."""
        return all(p.verdict == SAFE_PARALLEL for p in self.pairs)


def analyze_batch(tasks: list[Task]) -> BatchAnalysis:
    profiles = [build_conflict_profile(t) for t in tasks]
    pairs: list[PairVerdict] = []
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            verdict, reasons = analyze_pair(profiles[i], profiles[j])
            pairs.append(
                PairVerdict(
                    a=profiles[i].task_id, b=profiles[j].task_id, verdict=verdict, reasons=reasons
                )
            )
    return BatchAnalysis(profiles=profiles, pairs=pairs)


# ─── brief skeleton generation ─────────────────────────────────────────────────


def _project_slug(project: str) -> str:
    """Engine slug for a project = basename of its path (matches dx-spawn / the
    ack sentinel layout ``$HANDOFF_HOME/<slug>/ack/<task>.worker_reported``)."""
    return os.path.basename(os.path.realpath(project))


def build_brief(task: Task) -> str:
    """Generate the worker brief skeleton (req1 + hard boundaries welded in).

    Always contains, in order: ① the plain-language purpose ② the open-line echo
    instruction ③ the predicted_files HARD operating boundary ④ the §6b worker
    red-lines ⑤ the worker_reported sentinel line. ``purpose_plain`` is required
    by the caller (``_parse_identity``) so it is always present here."""
    slug = _project_slug(task.project)
    pf = task.raw.get("predicted_files")
    have_files = isinstance(pf, list) and pf and all(isinstance(x, str) and x.strip() for x in pf)

    lines: list[str] = []
    lines.append(f"🆔{task.task_id} — {slug} worker")
    lines.append("")
    lines.append("## 0. 任务目的（开张先用人话回显这句）")
    lines.append(task.purpose_plain)
    lines.append("")
    lines.append(
        f"开张第一句必须回显：🆔{task.task_id} ＋ 任务目的：{task.purpose_plain}"
    )
    lines.append("")

    if task.brief_points:
        lines.append("## 1. 任务要点")
        for pt in task.brief_points:
            lines.append(f"- {pt}")
        lines.append("")

    lines.append("## 2. 🔴 硬性操作边界（predicted_files — 禁改预测外文件）")
    lines.append(
        "本任务【只允许】改动下列预测文件集。改动任何【预测外】文件 = 越界 —— 会让中枢的"
        "前置并发冲突分析失效（别的并发 worker 是基于你这份文件集判定的不会撞车）："
    )
    if have_files and isinstance(pf, list):
        for entry in pf:
            lines.append(f"- {entry.strip()}")
    else:
        lines.append(
            "- ⚠️ 本任务未声明 predicted_files —— 中枢没给你文件边界。动手前必须先向中枢"
            "澄清允许改动的文件范围，禁凭感觉扩面。"
        )
    lines.append(
        "如执行中发现【必须】改预测外文件 → 停手、在报告里说明原因，交中枢重新评估并发安全，"
        "禁擅自扩面（防 worker 漂移使前置冲突分析形同虚设）。"
    )
    lines.append("")

    lines.append("## 3. 🔴 worker 红线（§6b 信任根 / 不可破）")
    lines.append("① 禁自派（禁 spawn / 派下一棒会话）。")
    lines.append("② 禁写共享 MEMORY.md / open-loops.md —— 只在报告里抛发现，由中枢沉淀。")
    lines.append(
        "③ 禁自我 discharge —— GREEN 由中枢零信任复审（亲跑 + 外双脑）后裁决，worker 不得自宣通过。"
    )
    lines.append(
        f"④ 干完 touch 哨兵后静默等中枢：\n"
        f"   touch ~/.claude-handoff/{slug}/ack/{task.task_id}.worker_reported"
    )
    lines.append("")
    lines.append("## 4. 完成定义")
    lines.append(
        "按上面「任务目的」彻底闭环交付（业界规范完整版，非部分上线）；过程中发现的对称缺口/"
        "预存 bug 一次到位修完，范围边界外的扩展须报中枢裁决，禁默默标“以后做”。"
    )
    lines.append("")
    return "\n".join(lines)


# ─── thin spawn adapter (reuse dx-spawn-session.sh — never re-implement spawn) ──


def _resolve_dx_spawn() -> Path | None:
    """Locate the existing spawn engine via ``$DX_SPAWN_SH`` (the cross-project
    worker-dispatch script). Returns None when unset / not a file (caller fails
    closed). This module NEVER re-implements spawn — it only adapts to this."""
    p = os.environ.get("DX_SPAWN_SH", "")
    if p and Path(p).is_file():
        return Path(p)
    return None


def _brief_dir(home: Path) -> Path:
    return home / "_dispatch_briefs"


def dispatch_one(task: Task, *, dx_spawn: Path, home: Path) -> tuple[bool, str]:
    """Write the brief to a persistent file (the spawned session reads it by path
    minutes later) and shell out to the existing spawn engine. Returns (ok, msg).
    No retries, no background — one synchronous spawn-intent producer call."""
    bdir = _brief_dir(home)
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        brief_path = bdir / f"{task.task_id}.md"
        brief_path.write_text(build_brief(task), encoding="utf-8")
    except OSError as e:
        return False, f"could not write brief file for {task.task_id}: {e}"

    cmd = [
        str(dx_spawn),
        "--project",
        task.project,
        "--brief",
        str(brief_path),
        "--task-id",
        task.task_id,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"dispatch invocation failed for {task.task_id}: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, f"dx-spawn rc={proc.returncode} for {task.task_id}:\n{out.strip()}"
    return True, out.strip()


# ─── rendering ─────────────────────────────────────────────────────────────────


def _render_plan(tasks: list[Task]) -> str:
    lines = ["═══ 批次计划（batch plan） ═══"]
    for t in tasks:
        lines.append(f"  🆔{t.task_id}  [{_project_slug(t.project)}]  — {t.purpose_plain}")
    return "\n".join(lines)


def _render_conflict_table(analysis: BatchAnalysis) -> str:
    lines = ["═══ 冲突分析表（pairwise conflict analysis） ═══"]
    if not analysis.pairs:
        lines.append("  (单任务 / single task — 无任务对，trivially parallel-safe)")
        return "\n".join(lines)
    for p in analysis.pairs:
        mark = "✅" if p.verdict == SAFE_PARALLEL else "🔴"
        lines.append(f"  {mark} {p.a} ↔ {p.b}: {p.verdict}")
        for r in p.reasons:
            lines.append(f"        · {r}")
    return "\n".join(lines)


def _render_verdict(analysis: BatchAnalysis) -> str:
    if analysis.parallel_safe:
        return (
            "═══ 批次裁定 ═══\n"
            "  ✅ SAFE-PARALLEL — 全部任务对显式 disjoint，可并发派（仍受 owner 二段确认约束）。"
        )
    n = sum(1 for p in analysis.pairs if p.verdict == MUST_SERIAL)
    return (
        "═══ 批次裁定 ═══\n"
        f"  🔴 NOT PARALLEL-SAFE — {n} 个任务对判 MUST-SERIAL（见上表原因）。\n"
        "  → 建议：串行逐个派 / 拆小到无冲突子批 / 交 owner 决策。--execute 将拒绝并发本批。"
    )


# ─── CLI ───────────────────────────────────────────────────────────────────────


def run(tasks_json: Path, *, execute: bool) -> int:
    # parse + validate identity/brief-required fields (hard, fail-closed)
    try:
        raw_tasks = _load_tasks(tasks_json)
        tasks = [_parse_identity(rt, i) for i, rt in enumerate(raw_tasks)]
    except ValueError as e:
        _err(str(e))
        return EXIT_FAIL

    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        _err(f"duplicate task_id(s) in batch: {dupes}")
        return EXIT_FAIL

    analysis = analyze_batch(tasks)

    # dry-run / preview output (always printed — the gate's visual record)
    print(_render_plan(tasks))
    print()
    print(_render_conflict_table(analysis))
    print()
    print(_render_verdict(analysis))
    print()
    print("═══ 每任务 brief 预览（brief preview） ═══")
    for t in tasks:
        print(f"\n───── 🆔{t.task_id} ─────")
        print(build_brief(t))

    if not execute:
        print(
            "\n[coord-dispatch] dry-run（默认）—— 未写 .uri、未真派。确认无误后加 --execute 真派。"
        )
        return EXIT_OK

    # ── --execute: real dispatch ──
    if not analysis.parallel_safe:
        _err(
            "REFUSED: 本批非 SAFE-PARALLEL（见冲突表）。并发派被 fail-closed 拒绝 —— "
            "请串行逐个派、拆小到无冲突子批、或交 owner 决策。"
        )
        return EXIT_FAIL

    dx_spawn = _resolve_dx_spawn()
    if dx_spawn is None:
        _err(
            "DX_SPAWN_SH 未设置或不是文件 —— 无法定位派发引擎 dx-spawn-session.sh。"
            "export DX_SPAWN_SH=<dharmaxis>/scripts/dx-spawn-session.sh 后重试。"
        )
        return EXIT_FAIL

    home = _config.home_dir()
    print(f"\n[coord-dispatch] SAFE-PARALLEL — 经 {dx_spawn} 逐个派 {len(tasks)} 个 worker intent…")
    dispatched: list[str] = []
    for t in tasks:
        ok, msg = dispatch_one(t, dx_spawn=dx_spawn, home=home)
        if not ok:
            _err(msg)
            _err(
                f"dispatch 在 {t.task_id} 处失败 —— 已派 {dispatched or '(none)'}；停止本批"
                "（fail-closed，不在失败后继续派）。请人工核查后处理剩余任务。"
            )
            return EXIT_FAIL
        dispatched.append(t.task_id)
        print(f"  ✅ 已派 🆔{t.task_id}")
        if msg:
            print(f"     {msg.splitlines()[-1] if msg.splitlines() else ''}")
    print(f"[coord-dispatch] ✅ 全部 {len(dispatched)} 个 worker intent 已产出：{dispatched}")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff coord-dispatch",
        description=(
            "Low-friction coordinator fan-out with a HARD machine-judged "
            "concurrency-conflict gate. Default dry-run (prints batch plan + "
            "conflict table + brief previews, spawns nothing); --execute fans out "
            "via dx-spawn-session.sh ONLY when the batch is SAFE-PARALLEL."
        ),
    )
    p.add_argument(
        "--tasks-json",
        required=True,
        help="path to the batch tasks JSON (a list of task objects, or {\"tasks\": [...]})",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="actually dispatch (default: dry-run). Refused unless the batch is SAFE-PARALLEL.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run(Path(args.tasks_json).expanduser(), execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
