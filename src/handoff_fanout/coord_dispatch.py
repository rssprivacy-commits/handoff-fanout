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


def build_conflict_profile(task: Task) -> ConflictProfile:
    """Extract the declared-field conflict surface for one task. Pure (no spawn,
    no mutation) except read-only filesystem glob expansion against the project."""
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
