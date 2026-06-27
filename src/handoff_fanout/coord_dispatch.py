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
* **Earned parallel, fail-closed.** Parallel is the *optimization exception a
  coordinator must EARN by proving "safe to parallelize"*, never an unchecked
  default. The gate is fail-closed: a pair is ``SAFE-PARALLEL`` only when it is
  *provably* disjoint; any doubt → ``MUST-SERIAL``. ``--execute`` then dispatches
  ONLY the provably-disjoint subset (see "concurrent wave" below) — it never fans
  out a pair it could not prove safe.
* **Concurrent wave + partition (the live default).** ``--execute`` does NOT
  serial-loop the batch (the disease this command cures: coordinators that always
  dispatch one-at-a-time). It partitions the batch into a **wave** — a maximum set
  of tasks that are *pairwise* ``SAFE-PARALLEL`` — and fans the wave out
  CONCURRENTLY (one thread per ``dx-spawn-session.sh`` call, failure-isolated).
  Tasks left out of the wave are **deferred** (reported, NOT dispatched) for the
  coordinator to send in a LATER wave. This is the only safe handling of a
  conflicting task — see "serial dispatch ≠ serial execution" below.
* **serial dispatch ≠ serial execution.** Dispatch is fire-and-forget intent
  production; every dispatched worker then runs ~concurrently regardless of the
  order we dispatched them. So serial-dispatching two conflicting workers does
  NOT serialize their execution — they would still collide. The ONLY safe action
  for a conflicting task is to keep it OUT of the current concurrent wave; "serial"
  therefore means "a later wave" (a separate coordinator action after the current
  wave's workers merge), never "serial-dispatch now". The set actually dispatched
  in one ``--execute`` is one wave and is ALWAYS pairwise-disjoint.
* **Resource-bounded width (N ≤ loadavg).** The concurrent wave width is capped by
  the machine's load headroom (``cpu_count − loadavg``, ≥1) or an explicit
  ``--max-width``. Tasks beyond the cap are load-deferred (kept for a later wave),
  so a saturated box degrades to width 1 (serial) instead of piling on workers.
* **Machine-judge declared fields ONLY.** The conflict verdict reads the task's
  *declared* schema fields (``predicted_files`` / ``repo_branch`` / ``will_push``
  / ``worktree_isolation`` / ``shared_writes`` / ``credential_scopes`` /
  ``runtime_targets``). It runs NO heuristic, NO AST parse, NO LLM guess — the
  brief is explicit that those are forbidden (they would turn an auditable gate
  into an opaque one). The soft dimensions (logical independence, resource
  bounds) are surfaced for the human/owner to eyeball in the dry-run table.
* **dry-run by default.** It prints the batch plan + the full pairwise conflict
  table + the concurrent wave plan + each generated brief; it writes no ``.uri``
  and spawns nothing. ``--execute`` is the only path that actually dispatches.
* **Failure isolation (sound because the wave is proven-disjoint).** Each spawn in
  the wave is isolated: one ``dx-spawn`` failing neither aborts nor corrupts its
  peers — they were proven file/resource-disjoint, so a failed spawn is an
  operational hiccup for that one task only. ``--execute`` attempts every wave
  task and reports per-task success/failure (exit 2 if any spawn failed).
* **Reuse, never re-implement spawn.** The actual spawn is a thin adapter that
  shells out to the existing ``dx-spawn-session.sh`` (``$DX_SPAWN_SH``); this
  module never re-implements the spawn engine, never touches the watchdog /
  launcher, and never alters the owner's worker-dispatch two-stage confirm.
"""

from __future__ import annotations

import argparse
import concurrent.futures
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

# Sentinel for ``ConflictProfile.isolation`` when the registry routing is IN FORCE but
# could not yield a clean ``worktree``/``singlepane`` mode (an explicit
# ``DX_PROJECT_REGISTRY`` that is missing/unreadable/corrupt, or a project found in the
# registry with a missing/illegal ``worker_isolation``). It is NOT a routable mode — it
# means "fail-closed: treat a same-project pair as MUST-SERIAL" (宁可多串行别漏). Distinct
# from a real mode so the conflict table can say *why* (unresolved registry) rather than
# mislabel it "singlepane".
ISOLATION_UNKNOWN = "registry-unresolved"

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
    case_insensitive: bool = False  # project FS folds case (macOS APFS) → fold path keys
    files_concrete: set[str] = field(default_factory=set)  # abs paths (literals + expanded globs)
    glob_patterns: set[str] = field(default_factory=set)  # abs glob patterns (for fnmatch vs literals)
    files_indeterminate: bool = False
    file_notes: list[str] = field(default_factory=list)  # why indeterminate (for the table)
    repo_branch: str | None = None
    will_push: bool | None = None
    worktree_isolation: bool | None = None
    shared: dict[str, set[str] | None] = field(default_factory=dict)  # dim → set, or None=missing/unknown
    field_issues: list[str] = field(default_factory=list)
    # EFFECTIVE worker isolation mode the actuator routes on. PRIMARY source = the
    # project-registry.json that ``dx-spawn-session.sh`` actually reads (NOT handoff
    # config — the live config's global ``worker_isolation={"default":"worktree"}`` would
    # mask every project's true mode; the actuator ignores config). config / the declared
    # ``worktree_isolation`` field are only a fallback when the registry is silent for this
    # project. ``"singlepane"`` → the project may hold only ONE active worker (spawn-side
    # hard REJECT of a concurrent 2nd); ``"worktree"`` → concurrent same-project workers
    # queue safely on the shared spawn lock; ``ISOLATION_UNKNOWN`` → registry in force but
    # unresolved (fail-closed → serialize); ``None`` → unresolvable (no registry/config,
    # declared field missing → already a field_issue). Drives the same-project axis.
    isolation: str | None = None
    # realpath of the project's git common-dir (``git rev-parse --git-common-dir``), or
    # ``None`` when the project is genuinely NOT a git repo (no shared object store to
    # race). A probe that could not RUN (git missing / unexpected error) instead adds a
    # ``field_issue`` (fail-closed). Drives the cross-slug shared-object-store axis.
    git_common_dir: str | None = None


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


def _anchor_glob(project_root: str, entry: str) -> str:
    """Anchor a GLOB entry WITHOUT letting ``realpath`` dissolve its wildcard
    segments — the root-cause fix for the "realpath erases the glob" class.

    ``_anchor`` realpaths the WHOLE joined path. For a glob that is a latent bug: a
    symlink whose own NAME contains a metachar — a file literally named ``l*.py``,
    or a directory ``l*`` / ``?x`` / ``[a]`` — is a real on-disk entry, so realpath
    RESOLVES it and ERASES the wildcard, turning the pattern into a concrete path
    that no longer ``fnmatch``-matches a colliding sibling → false ``SAFE-PARALLEL``.

    The fix realpaths ONLY the static prefix — the entry segments BEFORE the first
    one carrying a glob metachar — and keeps every segment from the first wildcard
    onward LITERAL. realpath therefore never touches a wildcard segment, so a
    metachar-named symlink in ANY segment (directory OR filename) can never rewrite
    the pattern. The static prefix is still fully canonicalized (a static symlink
    dir ``link``→``src`` resolves exactly as ``_anchor`` did), so precision is
    unchanged. The wildcard boundary is located within the DECLARED entry only — the
    already-realpath'd ``project_root`` is trusted and never re-splits the prefix,
    so a metachar that happens to live in the root path can't shift the split.

    One last layer: the realpath'd static prefix is itself a CONCRETE name, but a
    symlink target's real name (or a project-root path) may legitimately CONTAIN a
    glob metachar — a real on-disk dir literally named ``real[ab]``. Spliced raw into
    the returned pattern, that ``[ab]`` is read as a CHARACTER CLASS by both
    ``glob.glob`` and ``fnmatch`` downstream, so the pattern mis-expands onto a decoy
    sibling (``reala/``) and FAILS to fnmatch the true ``real[ab]/`` file → false
    ``SAFE-PARALLEL``. ``glob.escape`` wraps each metachar in the prefix as a literal
    (``[``→``[[]``); both engines then treat the escaped prefix as the exact literal
    name while the DECLARED wildcard tail stays a live pattern. A metachar-free prefix
    is left byte-identical, so precision is unchanged.

    Declarations use POSIX ``/``; a Windows ``\\`` is normalized before splitting."""
    segs = entry.replace("\\", "/").split("/")
    g = next((i for i, s in enumerate(segs) if _has_glob(s)), len(segs))
    static = "/".join(segs[:g])               # entry's static prefix (no wildcard)
    tail = segs[g:]                           # from the first wildcard segment: LITERAL
    if os.path.isabs(entry):
        base = static or os.sep
    else:
        base = os.path.join(project_root, static) if static else project_root
    anchored_static = os.path.realpath(base)  # realpath the static prefix ONLY
    # Escape the (concrete) static prefix so a metachar in its REAL name stays a
    # literal in the pattern; the wildcard tail is the only live glob. A tail-less
    # path is compared as a string/samefile, never as a pattern → must NOT be escaped.
    return os.path.join(glob.escape(anchored_static), *tail) if tail else anchored_static


def _fs_case_insensitive(path: str) -> bool:
    """Best-effort probe: does the filesystem holding ``path`` treat names
    case-INSENSITIVELY (macOS APFS/HFS+ default, Windows) vs case-sensitively
    (typical Linux/ext4 CI)? This drives whether file-path comparison case-folds —
    on a case-insensitive FS ``src/Foo.py`` and ``src/foo.py`` are the SAME on-disk
    file, so distinct spellings must NOT read as file-disjoint (a false
    ``SAFE-PARALLEL``). ``os.path.realpath`` does NOT fold case — and for a
    predicted *new* file (not yet on disk) it returns the declared case verbatim —
    so the gate cannot lean on realpath here and must probe the FS itself.

    Probes by toggling the case of an existing path component and checking whether
    the toggled name still resolves to the SAME inode. Fails closed to ``True``
    (treat as insensitive → fold → may over-serialize, but never misses a real
    same-file collision) whenever the probe is inconclusive."""
    probe = path
    # climb to the nearest existing ancestor (a predicted new file won't exist)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return True  # no existing ancestor to probe → fail closed
        probe = parent
    cur = probe
    while True:
        head, tail = os.path.split(cur)
        swapped = tail.swapcase()
        if swapped != tail:  # this component has alphabetic case to toggle
            cand = os.path.join(head, swapped)
            try:
                if os.path.lexists(cand) and os.path.samefile(cand, cur):
                    return True   # toggled-case name = same file → case-insensitive
                return False      # toggled-case absent / different inode → sensitive
            except OSError:
                return True       # stat error → fail closed
        if head == cur:           # reached the root with nothing to toggle
            return True           # can't probe → fail closed
        cur = head


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


def _strip_branch_ref_prefixes(branch: str) -> str:
    """Reduce a branch ref spelling to its bare local-branch name.

    Handles the local-branch namespace (``refs/heads/`` / ``heads/``) and the
    remote-tracking namespace (``refs/remotes/<remote>/`` / ``remotes/<remote>/``):
    the namespace prefix AND the remote-name component are dropped, while a slashed
    branch *under* that remote is kept whole (``refs/remotes/origin/feature/x`` →
    ``feature/x``). A bare ``<remote>/<branch>`` with NO namespace prefix (e.g.
    ``origin/main``) is deliberately NOT stripped — it is indistinguishable from a
    legitimately-slashed local branch (``feature/main``), and blindly collapsing it
    would falsely merge distinct branches; it stays a documented boundary like the
    symbolic refs ``HEAD`` / ``@`` (left verbatim, never guessed).

    Returns the bare branch — possibly ``""`` when the spelling carries no branch
    at all (``refs/heads/`` / ``refs/remotes/origin``); the caller fails closed on
    an empty branch (an untrustworthy declaration)."""
    branch = branch.strip()
    for prefix in ("refs/remotes/", "remotes/"):
        if branch.startswith(prefix):
            # drop the namespace prefix AND the leading remote-name component
            _, _, after = branch[len(prefix):].partition("/")
            return after.strip()
    for prefix in ("refs/heads/", "heads/"):
        if branch.startswith(prefix):
            return branch[len(prefix):].strip()
    return branch


def _normalize_repo_branch(rb: str) -> tuple[str, str]:
    """Canonicalize a self-reported ``repo_branch`` and expose its branch part.

    Returns ``(canonical, branch)`` where *canonical* is the comparable
    ``<repo>@<branch>`` (or bare ``<branch>``) string and *branch* is the bare
    branch component. Ref-prefix and remote-tracking aliases of the *same* branch
    map to one canonical — e.g. ``proj@main`` ≡ ``proj@refs/heads/main`` ≡
    ``proj@heads/main`` ≡ ``proj@refs/remotes/origin/main`` ≡
    ``proj@remotes/origin/main`` — so an aliased spelling can't slip past the
    same-repo+branch-push rule (string mismatch → false ``SAFE-PARALLEL`` → two
    pushes racing the same branch). An empty *branch* signals a malformed
    declaration (``repo@`` / ``refs/heads/`` / ``@``) the caller must fail-closed on.

    The convention is ``<repo>@<branch>``; we split on the LAST ``@`` (a repo
    identifier such as ``git@host:org/repo`` may itself contain ``@``, but a branch
    ref does not), normalize only the branch component, and rejoin. Git refs are
    case-SENSITIVE, so the branch is never case-folded."""
    rb = rb.strip()
    repo, sep, branch = rb.rpartition("@")
    if not sep:  # no "@": the whole value is the branch
        repo, branch = "", rb
    branch = _strip_branch_ref_prefixes(branch)
    canonical = f"{repo}@{branch}" if sep else branch
    return canonical, branch


def _load_config() -> _config.Config:
    """Load the live handoff config, never raising — the gate must stay up even if the
    config is absent/degenerate (``_config.load`` already fails closed to defaults).
    Returns an empty default ``Config`` on any unexpected error (isolation then falls
    back to the per-task declared ``worktree_isolation`` field)."""
    try:
        return _config.load(_config.home_dir())
    except Exception:  # never let a config read crash the conflict gate
        return _config.Config()


def _registry_path() -> tuple[Path | None, bool]:
    """``(path, explicit)`` for the project-registry.json the ACTUATOR (dx-spawn) reads.

    Mirrors ``dx-spawn-session.sh``: honour ``$DX_PROJECT_REGISTRY`` first, else derive
    ``$(dirname SCRIPT_DIR)/project-registry.json`` from the spawn-engine location
    (``SCRIPT_DIR`` = the dir holding ``dx-spawn-session.sh`` = ``$DX_SPAWN_SH``'s parent).

    ``explicit`` = whether ``$DX_PROJECT_REGISTRY`` was set. It is ``True`` only for an
    operator-pinned registry — that path is treated STRICTLY (a miss = fail-closed). A
    DERIVED path (``explicit=False``) is best-effort: a project the derived registry does
    not list falls back to config rather than serializing every unrelated project (the
    test suite runs with ``$DX_SPAWN_SH`` pointing at the live engine, so the derived
    registry is the live one and would otherwise serialize every tmp_path fixture)."""
    env = os.environ.get("DX_PROJECT_REGISTRY", "")
    if env.strip():
        return Path(os.path.expanduser(env.strip())), True
    dx = _resolve_dx_spawn()
    if dx is None:
        return None, False
    # dx-spawn: SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"; REGISTRY="$(dirname "$SCRIPT_DIR")/...".
    # abspath (NOT realpath) matches ``cd dirname && pwd`` (it makes the path absolute
    # without resolving a symlinked script file — dx-spawn doesn't resolve $0's symlink).
    script_dir = Path(os.path.abspath(os.path.expanduser(str(dx)))).parent
    return script_dir.parent / "project-registry.json", False


def _registry_isolation(project_root: str) -> str | None:
    """Worker-isolation mode the ACTUATOR routes ``project_root`` on, read from the
    project-registry.json — dx-spawn's TRUE source (NOT handoff config).

    Byte-mirrors ``dx-spawn-session.sh:157-204``: load ``projects`` (dict) → realpath-match
    each entry's ``paths.root`` against ``project_root`` → take its ``worker_isolation``.

    Returns:
      ``"worktree"`` / ``"singlepane"`` — the registry's explicit, valid mode (the actuator
        will route on exactly this);
      ``ISOLATION_UNKNOWN`` — the registry could not yield a clean mode and the project may
        be singlepane → fail-closed (serialize). Fires when: the registry path is NOT
        locatable at all (no ``$DX_PROJECT_REGISTRY`` AND ``$DX_SPAWN_SH`` unset → can't even
        derive a path — the COMMON 中枢-shell case, p74 bug-1 三修); an EXPLICIT
        ``$DX_PROJECT_REGISTRY`` that is missing/unreadable/corrupt; a registry (explicit or
        derived) that is readable-but-corrupt; a malformed ``projects`` map; a project found
        with a missing/illegal mode; or a project absent from an EXPLICIT registry (drift);
      ``None`` — registry routing is locatable but simply silent for THIS project: a DERIVED
        registry path that doesn't exist on disk, or that exists but does not list this
        project. Only here does the caller fall back to config (don't over-serialize an
        unrelated project the live derived registry happens not to route)."""
    reg, explicit = _registry_path()
    if reg is None:
        # Registry path is NOT locatable AT ALL (no ``$DX_PROJECT_REGISTRY`` AND
        # ``$DX_SPAWN_SH`` unset → we can't even derive a path to read). The actuator's
        # true isolation source is therefore unverifiable — the project MAY be singlepane.
        # Fail-CLOSED (``ISOLATION_UNKNOWN``), NOT ``None``: returning ``None`` here would
        # fall back to handoff config whose global ``{"default":"worktree"}`` masks every
        # project's true mode → a singlepane project mislabeled SAFE-PARALLEL in the
        # dry-run preview table (p74 bug-1 三修). 中枢 live shell defaults to DX_SPAWN_SH
        # UNSET, so this is the COMMON path, not an edge case.
        return ISOLATION_UNKNOWN
    try:
        with open(reg, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # An explicit pinned registry that's missing is a misconfig → fail-closed; a derived
        # one that doesn't exist just means registry routing isn't wired here → config fallback.
        return ISOLATION_UNKNOWN if explicit else None
    except (OSError, ValueError):
        # readable-but-corrupt / unreadable: the actuator would fail to parse it too → fail-closed.
        return ISOLATION_UNKNOWN
    if not isinstance(data, dict):
        # readable but not a JSON object (top-level list/scalar/null) = corrupt
        # registry → fail-closed (same class as the OSError/ValueError branch).
        return ISOLATION_UNKNOWN
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return ISOLATION_UNKNOWN
    target = os.path.realpath(project_root)
    for entry in projects.values():
        if not isinstance(entry, dict):
            continue
        paths = entry.get("paths")
        root = paths.get("root") if isinstance(paths, dict) else None
        if not isinstance(root, str) or not root:
            continue
        if os.path.realpath(os.path.expanduser(root)) == target:
            iso = entry.get("worker_isolation")
            if iso in ("worktree", "singlepane"):
                return iso
            return ISOLATION_UNKNOWN  # found but mode missing/illegal → fail-closed (mirrors dx-spawn)
    # project not listed: drift in an explicit registry → fail-closed; a derived registry
    # that doesn't route this project → config fallback (don't over-serialize the unrelated).
    return ISOLATION_UNKNOWN if explicit else None


def _effective_isolation(
    cfg: _config.Config, project_root: str, declared_worktree_isolation: bool | None
) -> str | None:
    """The EFFECTIVE worker-isolation mode the actuator would route ``project_root`` on.

    PRIMARY source = the project-registry.json the actuator (dx-spawn) actually reads
    (``_registry_isolation``) — NOT handoff config. The live config carries a global
    ``worker_isolation={"default":"worktree"}`` that masks every project's true mode, and
    dx-spawn ignores config entirely, so resolving from config would read a registry-
    ``singlepane`` project as ``worktree`` → false SAFE-PARALLEL → the actuator rejects the
    concurrent 2nd worker → the whole wave exits 2 (p74 bug 1).

    Resolution:
      1. registry yields a concrete mode (``worktree``/``singlepane``) → use it (the actuator
         will route on exactly this);
      2. registry unresolved / not locatable (``ISOLATION_UNKNOWN``) → return it so the same-
         project axis serializes (宁可多串行别漏). NOTE: a registry that can't even be located
         (DX_SPAWN_SH unset + no DX_PROJECT_REGISTRY) lands HERE now (p74 bug-1 三修), NOT in
         the config-fallback branch below — an unverifiable isolation must never silently
         resolve to config's masking ``worktree`` default;
      3. registry locatable but silent for this project (``None``) → legacy fallback to
         ``config.resolve_isolation(slug)``, then the declared ``worktree_isolation`` field
         (preserves pre-registry behavior for projects a DERIVED registry doesn't route):
         ``True`` → ``"worktree"``, ``False`` → ``"singlepane"``, missing → ``None`` (the
         missing field already taints the pair via ``field_issues``)."""
    reg = _registry_isolation(project_root)
    if reg is not None:
        return reg  # "worktree" / "singlepane" / ISOLATION_UNKNOWN
    mode = cfg.resolve_isolation(os.path.basename(project_root))
    if mode is not None:
        return mode
    if declared_worktree_isolation is True:
        return "worktree"
    if declared_worktree_isolation is False:
        return "singlepane"
    return None


def _resolve_git_common_dir(project_root: str) -> tuple[str | None, str | None]:
    """``(common_dir, issue)`` for a project's git object store.

    ``common_dir`` = realpath of ``git -C <root> rev-parse --git-common-dir`` when the
    root is inside a git repo, else ``None``. Two projects that share ONE object store
    (a linked git-worktree / two checkouts of one bare repo / a symlinked source tree)
    resolve to the SAME ``common_dir`` even with different slugs — the signal the slug-
    keyed spawn lock is blind to.

    ``issue`` is a fail-closed field-issue string ONLY when the probe could not RUN
    (git binary missing / unexpected error / timeout). A clean "not a git repository"
    (non-zero exit) is NOT an issue: a non-git directory has no object store to race →
    ``(None, None)``. The subprocess is hard-bounded (``timeout``) so a hung git can
    never wedge the gate."""
    try:
        proc = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # could not even run the probe (git missing / timeout / …) → fail closed
        return None, f"git common-dir probe could not run ({e}) — fail-closed serialize"
    if proc.returncode != 0:
        # "not a git repository" (or a repo-level error): no resolvable shared object
        # store on this axis → no taint (a genuine non-git project shares nothing).
        return None, None
    out = proc.stdout.strip()
    if not out:
        return None, None
    # git prints the path relative to <root> (we ran with -C <root>), or absolute.
    raw = out if os.path.isabs(out) else os.path.join(project_root, out)
    return os.path.realpath(raw), None


def build_conflict_profile(task: Task, *, cfg: _config.Config | None = None) -> ConflictProfile:
    """Extract the declared-field conflict surface for one task. Side-effect-free
    except read-only filesystem glob expansion + a read-only ``git rev-parse`` probe
    (shared-object-store detection) + a read-only config read (isolation resolution).
    ``cfg`` is loaded once per batch by ``analyze_batch`` and threaded in; a direct
    caller may omit it (loaded on demand)."""
    if cfg is None:
        cfg = _load_config()
    project_root = os.path.realpath(task.project)
    prof = ConflictProfile(task_id=task.task_id, project_root=project_root)
    prof.case_insensitive = _fs_case_insensitive(project_root)

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
            if entry.lower() == UNKNOWN_TOKEN:
                # A literal "unknown" sentinel means the worker can't name its
                # files — that is an indeterminate file set (fail-closed →
                # MUST-SERIAL), NOT a concrete file literally named "unknown"
                # (which would wrongly read as disjoint from real files).
                prof.files_indeterminate = True
                prof.file_notes.append('predicted_files entry "unknown" → file set unknown')
                continue
            # A malformed entry (embedded NUL, undecodable path, …) makes the
            # ``os.path.realpath`` calls below raise — fail-closed for THIS entry
            # (taint → MUST-SERIAL), never crash. Same class as an unexpandable glob.
            try:
                # A trailing separator declares a *directory* — capture it before
                # _anchor (realpath strips it).
                declared_dir = entry.endswith("/") or entry.endswith(os.sep)
                # Anchor globs with a glob-AWARE canonicalizer: realpath ONLY the static
                # prefix before the first wildcard segment, never the wildcard segments
                # themselves. A whole-path realpath would dissolve a symlink whose own
                # NAME is a metachar (``l*`` / ``l*.py``), ERASING the pattern into a
                # concrete file → false SAFE-PARALLEL (codex R3/R4/R5). Exact-file entries
                # still get full realpath (resolves symlink aliases to one canonical path).
                is_glob = _has_glob(entry)
                anchored = _anchor_glob(project_root, entry) if is_glob else _anchor(project_root, entry)
                if is_glob:
                    prof.glob_patterns.add(anchored)
                    # A wildcard in a NON-FINAL (directory) segment makes the glob's
                    # future expansion unbounded — it can match a symlink dir or a
                    # not-yet-created subdir at runtime that no static expansion (nor the
                    # leaf-realpath check below, which sees only TODAY's matches) can
                    # predict (the codex case ``l*`` matching a plain dir today but a
                    # symlink dir tomorrow). Fail closed → indeterminate.
                    #
                    # Key this check on the RAW DECLARED string, NOT on ``anchored``:
                    # ``_anchor``'s realpath can dissolve a symlink whose own NAME contains
                    # a metachar — a literal dir named ``l*`` → ``src`` collapses to ``src``,
                    # ERASING the ``*`` that should have tripped this guard → false
                    # SAFE-PARALLEL (two workers then clobber a runtime-shared file). The
                    # raw string is a pure-syntactic anchor the filesystem can't rewrite,
                    # so it's immune to every symlink/alias trick. Precision is unchanged:
                    # a guarded directory segment (``src``, static-symlink ``link``) carries
                    # no metachar in raw OR anchored form, so keying on raw adds zero false
                    # serialization — it only plugs the metachar-named-symlink hole.
                    # Declarations use ``/`` (POSIX); normalize ``\`` too before splitting.
                    raw_segs = entry.replace("\\", "/").split("/")
                    if any(_has_glob(seg) for seg in raw_segs[:-1]):
                        prof.files_indeterminate = True
                        prof.file_notes.append(
                            f"glob has a directory-segment wildcard — future expansion "
                            f"unbounded (may cross symlinks / new subdirs): {entry}"
                        )
                        continue
                    matches = glob.glob(anchored, recursive=True)
                    if matches:
                        # canonicalize each match too: glob traverses INTO symlinks and
                        # yields them unresolved, so two globs reaching one real file
                        # would otherwise look disjoint.
                        prof.files_concrete.update(os.path.realpath(m) for m in matches)
                        # A glob whose SYMLINK lives in a glob *segment* (``l*/*.py``,
                        # not a static prefix) can't be statically resolved by _anchor,
                        # so the stored pattern keeps the unresolved ``l*`` — a future
                        # file created under the symlinked real dir WOULD collide at
                        # runtime but the unresolved pattern won't ``fnmatch`` it
                        # (false SAFE). Detect it: a match whose realpath differs from
                        # its literal (normpath) form crossed a symlink the pattern
                        # can't predict → fail-closed indeterminate.
                        if any(os.path.realpath(m) != os.path.normpath(m) for m in matches):
                            prof.files_indeterminate = True
                            prof.file_notes.append(
                                f"glob expands through a symlink — future matches "
                                f"unpredictable: {entry}"
                            )
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
            except (OSError, ValueError) as e:
                # malformed path entry (embedded NUL / undecodable / OS-rejected) →
                # can't anchor it, so can't prove it disjoint from anything → taint
                # (fail-closed → MUST-SERIAL), no crash.
                prof.files_indeterminate = True
                prof.field_issues.append(f"predicted_files entry unprocessable ({entry!r}): {e}")
                prof.file_notes.append(f"unprocessable predicted_files entry ({entry!r}): {e}")
                continue

    # ── repo / branch / push / isolation ──
    rb = task.raw.get("repo_branch")
    if not isinstance(rb, str) or not rb.strip() or rb.strip().lower() == UNKNOWN_TOKEN:
        prof.field_issues.append("repo_branch missing / unknown")
    else:
        canonical, branch = _normalize_repo_branch(rb)
        if not branch:
            # A spelling that normalizes to an EMPTY branch (``repo@`` /
            # ``refs/heads/`` / ``@``) is an untrustworthy declaration → taint.
            prof.field_issues.append(
                f"repo_branch malformed (empty branch after normalize): {rb!r}"
            )
        else:
            prof.repo_branch = canonical

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

    # ── effective worker isolation (singlepane-blind axis) ──
    # PRIMARY source = the project-registry.json the actuator (dx-spawn) reads — its TRUE
    # routing source (NOT handoff config, whose default-worktree masks real singlepane
    # projects). config / the declared worktree_isolation field are only the registry-silent
    # fallback. Pass the full project_root so the registry realpath-match (on paths.root) works.
    prof.isolation = _effective_isolation(cfg, project_root, prof.worktree_isolation)

    # ── shared git object-store (cross-slug-collision axis) ──
    # A probe that could not RUN taints the task (fail-closed); a clean non-git dir does not.
    prof.git_common_dir, git_issue = _resolve_git_common_dir(project_root)
    if git_issue:
        prof.field_issues.append(git_issue)

    return prof


# ─── pairwise conflict analysis ────────────────────────────────────────────────


def _case_key(path: str, fold: bool) -> str:
    """Case-canonical comparison key for a path: casefolded on a case-insensitive
    filesystem (so ``src/Foo.py`` and ``src/foo.py`` — the SAME file on APFS —
    compare equal), verbatim on a case-sensitive one (where they are distinct
    files and folding would over-serialize)."""
    return path.casefold() if fold else path


def _glob_literal_prefix(pattern: str) -> str:
    """The fixed leading run of ``pattern`` — chars before the FIRST glob metachar (``* ? [``).
    Any string the pattern matches MUST start with this."""
    i = 0
    for ch in pattern:
        if ch in _GLOB_META:
            break
        i += 1
    return pattern[:i]


def _glob_literal_suffix(pattern: str) -> str:
    """The fixed trailing run of ``pattern`` — chars after the LAST glob metachar (``* ? [ ]``).
    Any string the pattern matches MUST end with this. ``]`` is treated as a boundary too (a
    character class closes there); this only ever SHORTENS the fixed suffix → strictly fail-safe."""
    j = len(pattern)
    for ch in reversed(pattern):
        if ch in _GLOB_META or ch == "]":
            break
        j -= 1
    return pattern[j:]


def _globs_may_intersect(g1: str, g2: str, fold: bool) -> bool:
    """Can two ANCHORED globs both match some common path? FAIL-CLOSED: return True (MAY overlap)
    unless they can be cheaply PROVEN disjoint. This closes the glob-vs-glob fail-open (p51 finding ①
    / sw-coord-p53): ``src/foo*.py`` and ``src/*_new.py`` currently expand to disjoint concrete sets
    but BOTH can match a future ``src/foo_new.py`` — the old ``_files_overlap`` never compared two
    globs, so it emitted SAFE-PARALLEL for a pair that can collide.

    Both inputs carry a wildcard ONLY in the final segment (a directory-segment wildcard already
    routed the profile to ``files_indeterminate`` upstream), so disjointness reduces to:
      1. different (literal, realpath-canonical) directory → provably disjoint (no dir wildcard to
         bridge them);
      2. same directory → the two final-segment patterns can share a match unless their FIXED prefixes
         are incompatible (neither a prefix of the other) or their FIXED suffixes are incompatible
         (neither a suffix of the other). Anything not provably disjoint by those fixed anchors →
         True (we do NOT attempt to prove disjointness through the wildcard interior — fail-closed)."""
    d1, b1 = os.path.split(g1)
    d2, b2 = os.path.split(g2)
    # Defense-in-depth (codex p53 audit caveat): this proof ASSUMES the only wildcard is in the FINAL
    # segment — a directory-segment wildcard is routed to ``files_indeterminate`` upstream and never
    # reaches here. Should that invariant ever drift, a literal directory comparison could falsely read
    # two dir-globs as disjoint → fail-open. Fail CLOSED: a surviving metachar in the directory part
    # cannot be proven disjoint → treat as overlap.
    if _has_glob(d1) or _has_glob(d2):
        return True
    if _case_key(d1, fold) != _case_key(d2, fold):
        return False  # distinct literal directories → no common file
    if fold:
        b1, b2 = b1.casefold(), b2.casefold()
    p1, p2 = _glob_literal_prefix(b1), _glob_literal_prefix(b2)
    if not (p1.startswith(p2) or p2.startswith(p1)):
        return False  # incompatible fixed prefixes → no common basename
    s1, s2 = _glob_literal_suffix(b1), _glob_literal_suffix(b2)
    if not (s1.endswith(s2) or s2.endswith(s1)):
        return False  # incompatible fixed suffixes → no common basename
    return True  # fail-closed: cannot prove disjoint → treat as overlap


def _files_overlap(a: ConflictProfile, b: ConflictProfile) -> tuple[bool, list[str]]:
    """Return (overlap, reasons). Overlap is conservative/fail-closed: an
    indeterminate file set on EITHER side counts as overlap (cannot prove
    disjoint). Otherwise concrete∩concrete, plus a glob pattern on one side that
    ``fnmatch``-matches a concrete path on the other (catches "my glob will match
    your new file").

    Path comparison is case-folded when EITHER side lives on a case-insensitive
    filesystem (fail-closed: a case-only spelling difference there reaches the same
    on-disk file — ``realpath`` doesn't fold case, and a predicted new file keeps
    its declared case, so two casings would otherwise read as file-disjoint)."""
    reasons: list[str] = []
    if a.files_indeterminate or b.files_indeterminate:
        notes = a.file_notes + b.file_notes
        reasons.append(
            "file set indeterminate → cannot prove disjoint"
            + (f" [{'; '.join(notes)}]" if notes else "")
        )
        return True, reasons

    fold = a.case_insensitive or b.case_insensitive
    # case-canonical key → an original spelling (keeps the reasons human-readable)
    a_concrete = {_case_key(p, fold): p for p in a.files_concrete}
    b_concrete = {_case_key(p, fold): p for p in b.files_concrete}

    common = a_concrete.keys() & b_concrete.keys()
    if common:
        label = "predicted_files overlap" + (" (case-insensitive FS)" if fold else "")
        reasons.append(f"{label}: {sorted(a_concrete[k] for k in common)}")

    for patt in a.glob_patterns:
        pk = _case_key(patt, fold)
        hits = sorted(orig for k, orig in b_concrete.items() if fnmatch.fnmatch(k, pk))
        if hits:
            reasons.append(f"{a.task_id} glob {patt!r} matches {b.task_id} files {hits}")
    for patt in b.glob_patterns:
        pk = _case_key(patt, fold)
        hits = sorted(orig for k, orig in a_concrete.items() if fnmatch.fnmatch(k, pk))
        if hits:
            reasons.append(f"{b.task_id} glob {patt!r} matches {a.task_id} files {hits}")

    # glob ∩ glob (sw-coord-p53 fail-open fix / p51 finding ①): two globs that cannot be PROVEN
    # disjoint both lay claim to a future file → fail-closed overlap. The old gate compared
    # glob-vs-concrete ONLY, so a pair like ``src/foo*.py`` vs ``src/*_new.py`` (concrete expansions
    # disjoint TODAY, yet both match a future ``src/foo_new.py``) slipped through as SAFE-PARALLEL.
    for pa in sorted(a.glob_patterns):
        for pb in sorted(b.glob_patterns):
            if _globs_may_intersect(pa, pb, fold):
                reasons.append(
                    f"{a.task_id} glob {pa!r} may overlap {b.task_id} glob {pb!r} "
                    f"(cannot prove disjoint → fail-closed)"
                )

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

    slug_a, slug_b = os.path.basename(a.project_root), os.path.basename(b.project_root)

    # same singlepane project (bug 1 — singlepane-blind axis). A singlepane project may
    # hold only ONE active worker: spawn-side ``_active_singlepane_worker`` hard-REJECTs a
    # concurrent 2nd (design §5.4), so co-dispatching two same-(slug)project singlepane
    # tasks makes the actuator reject one → the whole wave exits 2. The gate must instead
    # judge them MUST-SERIAL so ``compute_wave`` DEFERS the 2nd to a later wave. Keyed on
    # SLUG (the actuator's queue/lock are slug-keyed); worktree projects are exempt
    # (worktree isolation + the wait=120 spawn-lock queue serialize them safely). The
    # isolation values come from the registry (dx-spawn's real source); an unresolved
    # registry (``ISOLATION_UNKNOWN``) is treated as possibly-singlepane → fail-closed.
    if slug_a == slug_b:
        sp = a.isolation == "singlepane" or b.isolation == "singlepane"
        unk = a.isolation == ISOLATION_UNKNOWN or b.isolation == ISOLATION_UNKNOWN
        if sp:
            reasons.append(
                f"same singlepane project {slug_a!r} — only one active worker allowed at a "
                "time (actuator hard-rejects a concurrent 2nd) → serialize"
            )
        elif unk:
            reasons.append(
                f"same project {slug_a!r} with unresolved registry isolation "
                "(registry missing/corrupt/illegal mode) — fail-closed, may be singlepane "
                "→ serialize"
            )

    # cross-slug shared git object store (bug 2). Two DIFFERENT slugs (→ two distinct
    # spawn lockdirs ``home/<slug>/.spawn.lock`` → NO serialization) that share ONE git
    # common-dir (a linked worktree / two checkouts of one bare repo / a symlinked source
    # tree) would run concurrent ``git fetch`` / ``worktree add`` against the SAME object
    # store and race index.lock / packed-refs — exactly what the worktree spawn lock is
    # meant to prevent but can't here because the lock is slug-keyed. The per-project
    # realpath file axis can't see it (declared files anchor under each project root).
    if (
        a.git_common_dir is not None
        and b.git_common_dir is not None
        and a.git_common_dir == b.git_common_dir
        and slug_a != slug_b
    ):
        reasons.append(
            f"shared git object store ({a.git_common_dir}) across distinct projects "
            f"{slug_a!r}/{slug_b!r} — concurrent git writes race (the slug-keyed spawn "
            "lock can't serialize them) → serialize"
        )

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


def analyze_batch(tasks: list[Task], *, cfg: _config.Config | None = None) -> BatchAnalysis:
    # Load the config ONCE for the whole batch (isolation resolution) and thread it into
    # every profile — avoids a per-task config read. A caller may inject ``cfg`` (tests).
    if cfg is None:
        cfg = _load_config()
    profiles = [build_conflict_profile(t, cfg=cfg) for t in tasks]
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


# ─── concurrent wave planning (partition + resource bound) ──────────────────────

# Above this many tasks, the exact maximum-independent-set search (2^N subsets) is
# skipped for a deterministic greedy maximal set. A coordinator fanning out >16
# workers in a single wave is not a real scenario (load headroom caps it far lower),
# so the exact path always runs in practice; the greedy fallback is a runaway guard.
_MIS_EXACT_MAX = 16

# Conservative wave width when the load probe is unavailable (no os.getloadavg on
# the platform / probe error). Small on purpose — fail toward fewer concurrent
# spawns, never toward piling workers onto an unknown box.
_WIDTH_FALLBACK = 4


@dataclass
class WavePlan:
    """How a batch is partitioned for one ``--execute`` invocation.

    ``wave`` — the task_ids dispatched CONCURRENTLY this invocation. ALWAYS
    pairwise ``SAFE-PARALLEL`` (a subset of a maximum independent set in the
    conflict graph) AND ``len(wave) ≤ max_width`` (resource bound). Never empty
    for a non-empty batch.

    ``conflict_deferred`` — task_ids left out because they conflict with a wave
    member (or with each other); the coordinator dispatches them in a LATER wave
    after the current wave merges. ``load_deferred`` — task_ids that ARE wave-
    eligible (disjoint) but exceeded the load/width cap; dispatch them next wave
    when headroom frees up. Both deferred lists are in declared order."""

    wave: list[str]
    conflict_deferred: list[str]
    load_deferred: list[str]


def _conflict_adjacency(analysis: BatchAnalysis) -> tuple[list[str], list[list[bool]]]:
    """Build the undirected conflict graph: nodes = tasks (declared order), an edge
    iff the pair is ``MUST-SERIAL`` (cannot run concurrently). Returns
    ``(ids, adj)`` where ``adj[i][j]`` is True when tasks i,j conflict."""
    ids = [p.task_id for p in analysis.profiles]
    idx = {tid: i for i, tid in enumerate(ids)}
    n = len(ids)
    adj = [[False] * n for _ in range(n)]
    for pv in analysis.pairs:
        if pv.verdict == MUST_SERIAL:
            i, j = idx[pv.a], idx[pv.b]
            adj[i][j] = adj[j][i] = True
    return ids, adj


def _is_independent(sel: tuple[int, ...], adj: list[list[bool]]) -> bool:
    for x in range(len(sel)):
        for y in range(x + 1, len(sel)):
            if adj[sel[x]][sel[y]]:
                return False
    return True


def _max_independent_set(adj: list[list[bool]]) -> list[int]:
    """The largest set of pairwise-non-conflicting task indices (a maximum
    independent set in the conflict graph). Ties broken toward the lexicographically
    smallest index tuple → prefers earlier-declared tasks, fully deterministic.

    A node that conflicts with EVERY other (e.g. a task with incomplete/unknown
    declared fields → ``analyze_pair`` taints all its pairs) has edges to all, so it
    can only sit in a size-1 set; the maximum set therefore naturally EXCLUDES it in
    favour of the larger clean group — the under-declared task is the one deferred,
    not the good batch. Exact over all 2^N subsets for N ≤ ``_MIS_EXACT_MAX``;
    deterministic greedy (declared order) above it (a runaway guard, never hit in
    practice). Always returns ≥1 index for a non-empty graph."""
    n = len(adj)
    if n == 0:
        return []
    if n > _MIS_EXACT_MAX:
        # greedy maximal set in declared order (guard path only)
        chosen: list[int] = []
        for i in range(n):
            if all(not adj[i][c] for c in chosen):
                chosen.append(i)
        return chosen
    best: tuple[int, ...] | None = None
    for mask in range(1, 1 << n):
        sel = tuple(i for i in range(n) if mask >> i & 1)
        if not _is_independent(sel, adj):
            continue
        if best is None or len(sel) > len(best) or (len(sel) == len(best) and sel < best):
            best = sel
    return list(best) if best is not None else [0]


def compute_wave(analysis: BatchAnalysis, *, max_width: int) -> WavePlan:
    """Partition the batch into the concurrent wave + the two deferred buckets.

    1. Maximum independent set of the conflict graph = the largest pairwise-disjoint
       group (everything NOT in it is ``conflict_deferred``).
    2. Cap the wave to ``max_width`` (resource bound, N ≤ loadavg). The independent
       set's first ``max_width`` tasks (declared order) form the wave; the rest are
       ``load_deferred``. A prefix of an independent set is still independent, so the
       capped wave stays provably pairwise-disjoint."""
    ids, adj = _conflict_adjacency(analysis)
    if not ids:
        return WavePlan(wave=[], conflict_deferred=[], load_deferred=[])
    width = max(1, max_width)
    mis = _max_independent_set(adj)            # indices, ascending = declared order
    mis_set = set(mis)
    kept = mis[:width]
    load_idx = mis[width:]
    conflict_idx = [i for i in range(len(ids)) if i not in mis_set]
    return WavePlan(
        wave=[ids[i] for i in kept],
        conflict_deferred=[ids[i] for i in conflict_idx],
        load_deferred=[ids[i] for i in load_idx],
    )


def _load_headroom() -> int:
    """Best-effort concurrent-wave width ceiling from system load: ``cpu_count −
    1-min loadavg``, floored at 1 (always dispatch at least one). A busy box yields a
    small headroom → fewer concurrent spawns; a saturated box → width 1 (serial),
    satisfying owner law §六 "资源有界". Falls back to ``_WIDTH_FALLBACK`` whenever
    the load/cpu probe is unavailable (fail toward fewer, never more)."""
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):  # no getloadavg on this platform
        return _WIDTH_FALLBACK
    cpu = os.cpu_count() or _WIDTH_FALLBACK
    return max(1, int(cpu - load1))


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


def dispatch_wave(
    wave_tasks: list[Task], *, dx_spawn: Path, home: Path, width: int
) -> dict[str, tuple[bool, str]]:
    """Fan the wave out CONCURRENTLY, one thread per ``dispatch_one`` call, and
    return ``{task_id: (ok, msg)}`` for EVERY wave task. Concurrency is bounded by
    ``width`` (already ≤ the load/width cap from ``compute_wave``).

    Failure-isolated: a task whose spawn fails — or unexpectedly raises — is recorded
    as ``(False, msg)`` and never aborts its peers (sound because the wave is proven
    pairwise-disjoint, so a failed spawn cannot corrupt another's files/state). The
    spawn itself (``subprocess.run``) is I/O-bound and releases the GIL, so a thread
    pool is the right primitive (no re-implementation of the spawn engine)."""
    results: dict[str, tuple[bool, str]] = {}
    if not wave_tasks:
        return results
    max_workers = max(1, min(width, len(wave_tasks)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(dispatch_one, t, dx_spawn=dx_spawn, home=home): t for t in wave_tasks
        }
        for fut in concurrent.futures.as_completed(futs):
            t = futs[fut]
            try:
                results[t.task_id] = fut.result()
            except Exception as e:  # dispatch_one already catches its own; this is belt-and-braces
                results[t.task_id] = (False, f"dispatch raised for {t.task_id}: {e!r}")
    return results


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
        f"  🔴 NOT FULLY PARALLEL — {n} 个任务对判 MUST-SERIAL（见上表原因）。\n"
        "  → --execute 仍会并发派【可证明 disjoint 的最大子集】(wave)，冲突的 defer 到下一波。"
    )


def _render_wave_plan(plan: WavePlan, *, width: int, width_src: str) -> str:
    """Render the concurrent wave partition — exactly what ``--execute`` will do."""
    lines = ["═══ 并发波次计划（concurrent wave plan） ═══"]
    lines.append(f"  并发宽度上界 N≤{width}  （来源：{width_src}）")
    if plan.wave:
        lines.append(
            f"  ✅ 本波并发派 (wave, {len(plan.wave)} 个 — 两两 disjoint): {plan.wave}"
        )
    else:
        lines.append("  ✅ 本波并发派 (wave): (空 — 无可派任务)")
    if plan.load_deferred:
        lines.append(
            f"  ⏳ 负载 deferred ({len(plan.load_deferred)} 个 — 宽度上界已满，下一波派): "
            f"{plan.load_deferred}"
        )
    if plan.conflict_deferred:
        lines.append(
            f"  ⏭️  冲突 deferred ({len(plan.conflict_deferred)} 个 — 与本波/彼此冲突，"
            f"待本波合并后再派，见冲突表): {plan.conflict_deferred}"
        )
    if not plan.load_deferred and not plan.conflict_deferred:
        lines.append("  （无 deferred — 全批一波并发派）")
    return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────────────────────────────


def run(tasks_json: Path, *, execute: bool, max_width: int | None = None) -> int:
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

    # Catch-all backstop (mirrors the actuator dx-spawn's bash FATAL wrapper): the
    # per-task fail-closes in build_conflict_profile (FIX 1/2) are the primary graceful
    # path; this is the last-resort net so NO analysis crash ever escapes as a raw
    # traceback — refuse to dispatch (fail-closed) instead.
    try:
        analysis = analyze_batch(tasks)
    except Exception as e:
        _err(f"conflict analysis failed (fail-closed, refusing to dispatch): {e}")
        return EXIT_FAIL

    # resolve the concurrent-wave width ceiling (explicit --max-width wins; else the
    # live load headroom). Computed up front so the dry-run preview shows EXACTLY
    # what --execute would do this moment.
    if max_width is not None:
        width, width_src = max(1, max_width), "--max-width"
    else:
        width, width_src = _load_headroom(), "loadavg headroom (cpu−load1, ≥1)"
    plan = compute_wave(analysis, max_width=width)
    by_id = {t.task_id: t for t in tasks}

    # dry-run / preview output (always printed — the gate's visual record)
    print(_render_plan(tasks))
    print()
    print(_render_conflict_table(analysis))
    print()
    print(_render_verdict(analysis))
    print()
    print(_render_wave_plan(plan, width=width, width_src=width_src))
    print()
    print("═══ 每任务 brief 预览（brief preview） ═══")
    for t in tasks:
        print(f"\n───── 🆔{t.task_id} ─────")
        print(build_brief(t))

    if not execute:
        print(
            "\n[coord-dispatch] dry-run（默认）—— 未写 .uri、未真派。确认无误后加 --execute 真派"
            "（将并发派上面 wave 列出的任务，deferred 的留待下一波）。"
        )
        return EXIT_OK

    # ── --execute: real CONCURRENT dispatch of the proven-disjoint wave ──
    dx_spawn = _resolve_dx_spawn()
    if dx_spawn is None:
        _err(
            "DX_SPAWN_SH 未设置或不是文件 —— 无法定位派发引擎 dx-spawn-session.sh。"
            "export DX_SPAWN_SH=<dharmaxis>/scripts/dx-spawn-session.sh 后重试。"
        )
        return EXIT_FAIL

    home = _config.home_dir()
    wave_tasks = [by_id[tid] for tid in plan.wave]
    print(
        f"\n[coord-dispatch] 并发派 wave（{len(wave_tasks)} 个，宽度上界 N≤{width}）"
        f"经 {dx_spawn} …"
    )
    results = dispatch_wave(wave_tasks, dx_spawn=dx_spawn, home=home, width=width)

    ok_ids = [tid for tid in plan.wave if results.get(tid, (False, ""))[0]]
    failed_ids = [tid for tid in plan.wave if not results.get(tid, (False, ""))[0]]
    for tid in plan.wave:
        ok, msg = results.get(tid, (False, "(no result)"))
        if ok:
            tail = msg.splitlines()[-1] if msg.splitlines() else ""
            print(f"  ✅ 已派 🆔{tid}" + (f"  {tail}" if tail else ""))
        else:
            print(f"  ❌ 派失败 🆔{tid}: {msg.splitlines()[0] if msg.splitlines() else msg}")

    if plan.load_deferred:
        print(
            f"  ⏳ 负载 deferred（未派，宽度上界已满，下一波派）：{plan.load_deferred}"
        )
    if plan.conflict_deferred:
        print(
            f"  ⏭️  冲突 deferred（未派，与本波/彼此冲突，待本波合并后再派）："
            f"{plan.conflict_deferred}"
        )

    if failed_ids:
        _err(
            f"{len(failed_ids)} 个 worker intent 派发失败：{failed_ids}（已隔离，未影响其余 "
            f"{ok_ids or '(none)'}）。请人工核查失败原因后重派失败项。"
        )
        return EXIT_FAIL
    print(
        f"[coord-dispatch] ✅ 本波 {len(ok_ids)} 个 worker intent 已并发产出：{ok_ids}"
        + (
            f"；下一波待派：{plan.conflict_deferred + plan.load_deferred}"
            if (plan.conflict_deferred or plan.load_deferred)
            else "（全批一波派完）"
        )
    )
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff coord-dispatch",
        description=(
            "Low-friction coordinator fan-out with a HARD machine-judged "
            "concurrency-conflict gate. Default dry-run (prints batch plan + "
            "conflict table + concurrent wave plan + brief previews, spawns "
            "nothing); --execute CONCURRENTLY fans out the proven-disjoint wave "
            "(load-/width-capped, failure-isolated) and defers the rest to a later wave."
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
        help="actually dispatch (default: dry-run). Concurrently fans out the proven-"
        "disjoint wave; conflicting / over-width tasks are deferred to a later wave.",
    )
    p.add_argument(
        "--max-width",
        type=int,
        default=None,
        metavar="N",
        help="explicit concurrent-wave width ceiling (overrides the loadavg headroom). "
        "N≤0 is clamped to 1. Use to force serial (--max-width 1) or a known-idle box's full width.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run(
        Path(args.tasks_json).expanduser(), execute=args.execute, max_width=args.max_width
    )


if __name__ == "__main__":
    sys.exit(main())
