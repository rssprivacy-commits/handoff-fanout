#!/usr/bin/env python3
"""coord-place-window.py — supervisor coordinator helper (cross-project).

Place THIS coordinator chain's VS Code windows WITHIN their desktop using the
already-installed **Rectangle** app — the placement twin of coord-close-windows.py.
Sits beside coord-close-windows.py and reuses its EXACT safety model + shared deps.

WHY RECTANGLE (not our own ``set position/size``): re-implementing tiling math would
reinvent Rectangle (simplicity / skills-first violation). Rectangle exposes a stable,
documented automation API — a URL scheme that acts on the FRONTMOST window:

    open "rectangle://execute-action?name=<slot>"

Verified slot names (stable, owner's Rectangle build has the ``url`` default on by
default — we write NO Rectangle defaults): ``right-half``, ``top-left``, ``bottom-left``.
(We use the URL scheme, NOT keystroke synthesis, because Rectangle registers its
shortcuts as global Carbon hotkeys whose keycodes are unreadable + user-set-dependent.)

OWNER LAYOUT (p71): coordinator → right-half; worker N → top-left if N even else
bottom-left (alternating). WITHIN-desktop only — desktop *assignment* stays with
dharmaxis vscode-spaces (RED LINE #4: this tool only CALLS winlist/goto read-only,
it NEVER modifies vscode-spaces.py / code-router.sh / any other chain's files).

SAFETY (mirrors coord-close-windows.py; placement is LOWER blast-radius than close —
tiling is reversible via Rectangle "Restore" — but we stay strict):
  • DRY-RUN by default; --execute required to actually raise + fire Rectangle.
  • EXACT identity: a target resolves to EXACTLY ONE --project window (by stable Quartz
    window-id OR by structured task-id field == --task); fail-closed on zero/ambiguous.
  • RED LINE #4: never place another chain's window (project field must == --project).
    A 🧭 coordinator window OF THIS CHAIN is placeable (that is the right-half case).
  • Stable WID → fresh UNIQUE title: after the goto we re-read winlist, follow the
    stable WID to its CURRENT title and confirm that title is unique, so the AXRaise
    (EXACT name equality) is unambiguous (§6a TOCTOU root-fix, same as close-windows).
  • --self: place the CURRENTLY-FRONTMOST Code window (a coordinator placing its OWN
    window at startup) — NO identification / NO goto / NO restore. Refuses unless the
    frontmost window is a 🧭 + --project coordinator window (never fires on an unknown
    frontmost window).
  • Fail-closed goto; restore the owner's active desktop afterwards.
  • Rectangle preflight: if Rectangle.app is not running, fail with a clear message
    (never silently no-op).
  • Titles passed to osascript as ARGV (never string-interpolated → AppleScript-
    injection-safe); the slot is whitelisted to VALID_SLOTS so the open(1) URL is safe.
  • HONEST reporting: success is decided by an actual position/size DELTA (bounds before
    vs after), never claimed blind.

Depends on the shared vscode-spaces tools (winlist + vscode-spaces.py goto) + osascript
+ Rectangle.app + macOS ``open``.

Usage:
  # coordinator self-places its own window at startup (frontmost, no goto):
  coord-place-window.py --project handoff-fanout --self --slot right-half --execute

  # place a just-spawned worker (poll up to 45s for its window to appear), worker #0:
  coord-place-window.py --project handoff-fanout --task sw-foo \\
      --role worker --worker-index 0 --wait 45 --execute
  # ^ dry-run by default: prints the plan. Add --execute to actually place.
"""
import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from typing import NoReturn

VSCODE_SPACES = os.path.expanduser("~/Projects/dharmaxis/scripts/vscode-spaces")
WINLIST = os.path.join(VSCODE_SPACES, "winlist")
SPACES_PY = os.path.join(VSCODE_SPACES, "vscode-spaces.py")

COORD_MARKERS = ("🧭", "· sw-coord", "-coord-")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")          # strict ASCII task-id / project slug
# Owner's three layout slots. Whitelisted so the slot can be safely interpolated into the
# rectangle:// URL (Rectangle has more actions; we only need + only allow these three).
VALID_SLOTS = ("right-half", "top-left", "bottom-left")
# Sentinel for a worker placed with NO explicit --worker-index: the concrete left quadrant
# (top-left / bottom-left) is decided LATE — under the placement lock, on the target desktop — by
# COUNTING the windows already in each left quadrant and placing to the side with FEWER (load-balance
# by count instead of stacking; ties → top-left). NEVER a real Rectangle action (∉ VALID_SLOTS);
# always resolved to a VALID_SLOTS member before any fire.
FREE_QUADRANT = "free-left-quadrant"


def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def die(m) -> NoReturn:
    print(f"ERR: {m}", file=sys.stderr)
    sys.exit(2)


def log(m):
    print(m, flush=True)


# ── machine-wide placement lock (self-contained — NO handoff_fanout import) ──
# This tool runs under a python WITHOUT the editable install, so we mirror the
# spawn_lock.py PATTERN inline rather than importing it.

HANDOFF_HOME = os.path.expanduser(os.environ.get("HANDOFF_HOME", "~/.claude-handoff"))
# A SINGLE machine-wide lock: the contended resource is the GLOBAL active-Space, so the lock is
# NOT per-project. Held via fcntl.flock on an anchor FILE (OS-managed → auto-released on holder
# death/crash, so NO TTL, NO stale-reclaim, NO ABA race that a dir-mtime scheme cannot avoid).
PLACE_LOCK_FILE = os.path.join(HANDOFF_HOME, ".place-window.lock")  # an empty anchor FILE for flock
PLACE_LOCK_WAIT = 20.0   # max seconds to wait for the lock before giving up (fail-safe SKIP)


@contextlib.contextmanager
def placement_lock():
    """Machine-wide mutex around the active-Space critical section, via ``fcntl.flock`` on a lock
    file. Yields True if the exclusive lock was acquired within PLACE_LOCK_WAIT, else False (the
    caller MUST then SKIP placing — never place without the lock, that is the race). The OS releases
    the flock automatically if the holder process dies/crashes, so there is NO TTL, NO stale-reclaim,
    and NO ABA race (a directory-mtime scheme cannot avoid those). Advisory + local-fs only: flock is
    unreliable over NFS, but $HANDOFF_HOME is local APFS. Two opens of the same file get independent
    open-file-descriptions, so concurrent placements (even within one machine, across processes)
    contend correctly."""
    os.makedirs(os.path.dirname(PLACE_LOCK_FILE), exist_ok=True)
    # Defensive: an earlier mkdir-based build (or a manual test) may have left a DIRECTORY at this
    # path; os.open on a dir raises IsADirectoryError. Remove an empty stale dir if present.
    if os.path.isdir(PLACE_LOCK_FILE):
        with contextlib.suppress(OSError):
            os.rmdir(PLACE_LOCK_FILE)
    fd = os.open(PLACE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + PLACE_LOCK_WAIT
    held = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EINTR):
                    raise            # a real lock/FS error — surface it, don't misreport as contention
                if time.monotonic() >= deadline:
                    break            # held by another process past the deadline → caller SKIPs (fail-safe)
                time.sleep(0.5)
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── shared probes (same shape as coord-close-windows.py) ─────────────────────


def probe_windows():
    r = _run([WINLIST, "--spaces-of-windows"])
    if r.returncode != 0:
        die(f"winlist failed (rc={r.returncode}): {r.stderr.strip()}")
    try:
        return json.loads(r.stdout).get("windows", [])
    except ValueError as e:
        die(f"winlist output not JSON: {e}")


def parse_title(title):
    """Extract EXACT (project, task_id, is_coordinator, nonce) from a winlist title.
    Format: '[🧭中枢·]<project> · <task-id> · <role> · <nonce> [<iso>] — <summary>'.
    Unparseable fields come back None (caller treats missing project/task-id as no-match)."""
    is_coord = any(m in title for m in COORD_MARKERS)
    head = title.split(" — ")[0]
    head = head.replace("🧭中枢·", "").strip()
    parts = [p.strip() for p in head.split(" · ")]
    project = parts[0] if len(parts) >= 1 and parts[0] else None
    task_id = parts[1] if len(parts) >= 2 and parts[1] else None
    nonce = None
    if len(parts) >= 4:
        nm = re.match(r"^([0-9a-fA-F]{16})\b", parts[3])
        if nm:
            nonce = nm.group(1)
    return project, task_id, is_coord, nonce


def osascript_current_space_titles():
    r = _run(["osascript", "-e",
              'tell application "System Events" to tell process "Code" to get name of every window'])
    if r.returncode != 0 or not r.stdout.strip():
        return []
    return [t.strip() for t in r.stdout.strip().split(",") if t.strip()]


def detect_active_desktop(windows):
    """Owner's current desktop = desktop of a window visible on the active Space.
    Matches osascript (current-Space) titles to winlist desktops by EXACT title. None if unknown."""
    cur = set(osascript_current_space_titles())
    for w in windows:
        if w.get("title", "") in cur:
            return w.get("desktop")
    return None


def goto(n):
    """Switch to desktop n. Returns True iff vscode-spaces.py goto reported success."""
    r = _run([sys.executable, SPACES_PY, "goto", str(n)])
    time.sleep(1.5)
    return r.returncode == 0


# ── pure helpers (placement-specific; unit-testable, no side effects) ────────


def slot_for_role(role, worker_index):
    """Map (role, worker_index) → slot per the owner layout. Raises ValueError on bad input."""
    if role == "coord":
        return "right-half"
    if role == "worker":
        if worker_index is None:
            raise ValueError("--role worker requires --worker-index")
        if worker_index < 0:
            raise ValueError("--worker-index must be >= 0")
        return "top-left" if worker_index % 2 == 0 else "bottom-left"
    raise ValueError(f"unknown role {role!r}")


def resolve_slot(slot, role, worker_index):
    """Return the placement slot. Explicit --slot wins; else derive from --role.
    Returns None if neither was given (caller applies a default / errors). Raises ValueError
    on a bad combo so the CLI can surface a clear message."""
    if slot and role:
        raise ValueError("pass either --slot or --role, not both")
    if slot:
        if slot not in VALID_SLOTS:
            raise ValueError(f"--slot must be one of {VALID_SLOTS}")
        return slot
    if role:
        # A worker with NO explicit index defers the concrete left quadrant to placement time
        # (count-based least-loaded selection under the lock). slot_for_role keeps its strict
        # contract for the index-given path (and still raises if called directly without an index).
        if role == "worker" and worker_index is None:
            return FREE_QUADRANT
        return slot_for_role(role, worker_index)
    return None


def find_by_task(windows, project, task_id):
    """Windows whose structured PROJECT field == project AND TASK-ID field == task_id (exact)."""
    out = []
    for w in windows:
        proj, tid, _is_coord, _nonce = parse_title(w.get("title", ""))
        if proj == project and tid == task_id:
            out.append(w)
    return out


def find_by_wid(windows, wid):
    """Windows whose stable Quartz window_number == wid (normally 0 or 1 windows)."""
    return [w for w in windows if w.get("window_number") == wid]


def title_unique(windows, title):
    """True iff exactly one window carries this exact (non-empty) title — so an EXACT-name
    AXRaise is unambiguous. Two AI-titled '.handoff (Workspace)' windows → not unique → False."""
    if not title:
        return False
    return sum(1 for w in windows if w.get("title", "") == title) == 1


def is_self_placeable(title, project):
    """A --self target must be a 🧭 coordinator window OF THIS project (never an unknown
    frontmost window, never another chain)."""
    proj, _tid, is_coord, _nonce = parse_title(title)
    return is_coord and proj == project


def parse_bounds(s):
    """Parse an 'x,y,w,h' bounds string → (x, y, w, h) ints, or None if unparseable
    (osascript returned NOTFOUND / empty / rc!=0)."""
    if not s:
        return None
    try:
        parts = [int(float(x)) for x in s.split(",")]
    except (ValueError, AttributeError):
        return None
    return tuple(parts) if len(parts) == 4 else None


def bounds_changed(before, after):
    """True if the window actually moved/resized, False if identical, None if either bound
    is unknown (so the caller reports 'could not verify' rather than claiming success blind)."""
    b, a = parse_bounds(before), parse_bounds(after)
    if b is None or a is None:
        return None
    return b != a


def quadrant_of(bounds, frame):
    """Classify a window's bounds into 'top-left' / 'bottom-left' (the two worker slots) or None
    (right half / unknown). ``bounds`` + ``frame`` are (x, y, w, h). Center-based + best-effort: a
    window whose CENTER is in the LEFT half of the frame is 'top-left' when its center is above the
    frame's vertical midline, else 'bottom-left'; a center in the right half (or unknown input) ⇒
    None (not occupying a left quadrant). Cheap + deterministic so the slot picker is unit-testable
    without any GUI."""
    if bounds is None or frame is None:
        return None
    bx, by, bw, bh = bounds
    fx, fy, fw, fh = frame
    if fw <= 0 or fh <= 0:
        return None
    cx, cy = bx + bw / 2.0, by + bh / 2.0
    if cx >= fx + fw / 2.0:
        return None
    return "top-left" if cy < fy + fh / 2.0 else "bottom-left"


def least_loaded_slot(n_tl, n_bl):
    """Load-balance by COUNT (not binary free/full): place to the LEFT quadrant holding FEWER windows.
    ``n_tl`` / ``n_bl`` are the current window COUNTS in the top-left / bottom-left quadrants on the
    target desktop. Ties (incl. both-empty) → 'top-left'. Always returns a VALID_SLOTS member, so
    auto-spawned workers SPREAD evenly instead of dead-ending to top-left once both quadrants hold a
    window (the binary free/full bug)."""
    if n_tl == n_bl:
        return "top-left"
    return "top-left" if n_tl < n_bl else "bottom-left"


# ── osascript actuators (NOT unit-testable; osacompile-verified in the test suite) ──
# Each is a module-level constant so the regression test can run the real `osacompile` on
# it (the close-windows bug that bit p70 was an AppleScript that never compiled, slipping
# through string-match-only tests → fail-safe held but the feature never worked). Proper
# nested tell/end-tell blocks (NOT the one-liner `tell ... to tell ...` form that opened a
# single block but closed two). Titles arrive as ARGV → never interpolated → injection-safe.

RAISE_OSA = r'''
on run argv
  tell application "System Events"
    tell process "Code"
      set frontmost to true
      set theTitle to (item 1 of argv)
      repeat with w in (every window)
        if (name of w) is equal to theTitle then
          perform action "AXRaise" of w
        end if
      end repeat
    end tell
  end tell
end run
'''

BOUNDS_BY_TITLE_OSA = r'''
on run argv
  tell application "System Events"
    tell process "Code"
      set theTitle to (item 1 of argv)
      repeat with w in (every window)
        if (name of w) is equal to theTitle then
          set p to position of w
          set s to size of w
          return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
        end if
      end repeat
      return "NOTFOUND"
    end tell
  end tell
end run
'''

SELF_TITLE_OSA = r'''
tell application "System Events"
  tell process "Code"
    return name of window 1
  end tell
end tell
'''

FRONTMOST_OSA = r'''
tell application "System Events"
  set frontApp to ""
  try
    set frontApp to name of first application process whose frontmost is true
  end try
  if frontApp is not "Code" then return "NOTFRONT:" & frontApp
  tell process "Code"
    return name of window 1
  end tell
end tell
'''

BOUNDS_FRONT_OSA = r'''
tell application "System Events"
  tell process "Code"
    set w to window 1
    set p to position of w
    set s to size of w
    return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
  end tell
end tell
'''

# Main-display frame as "left, top, right, bottom" (Finder; read-only, no Accessibility prompt).
# Used ONLY by the best-effort free-quadrant occupancy probe as the DEGRADE fallback (single display,
# or Quartz/target-bounds unavailable); the multi-display path pins to the target's display via
# all_display_frames(). Any failure ⇒ caller treats the desktop as empty (→ default top-left), never
# a hard error. Multi-display: main display only (best-effort) — see probe_left_quadrant_occupancy.
SCREEN_FRAME_OSA = r'''
tell application "Finder"
  get bounds of window of desktop
end tell
'''


def rectangle_running():
    """True iff Rectangle.app is running (so the URL scheme has a receiver)."""
    return _run(["pgrep", "-x", "Rectangle"]).returncode == 0


def read_frontmost_title():
    """Name of the frontmost Code window (for --self), or None if osascript failed."""
    r = _run(["osascript", "-e", SELF_TITLE_OSA])
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def frontmost_window_title():
    """Title of the frontmost Code window IFF Code is the frontmost APPLICATION; else None
    (None when another app is globally frontmost — so callers fail-closed instead of firing
    Rectangle on the wrong app's window)."""
    r = _run(["osascript", "-e", FRONTMOST_OSA])
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out or out.startswith("NOTFRONT"):
        return None
    return out


def frontmost_is(title):
    """True iff Code is the frontmost app AND its frontmost window's title EXACTLY == title."""
    return frontmost_window_title() == title


def capture_bounds_by_title(title):
    """'x,y,w,h' of the window whose name EXACTLY EQUALS title, or None. Title is ARGV."""
    r = _run(["osascript", "-e", BOUNDS_BY_TITLE_OSA, title])
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out if out and out != "NOTFOUND" else None


def capture_front_bounds():
    """'x,y,w,h' of the frontmost Code window (for --self), or None."""
    r = _run(["osascript", "-e", BOUNDS_FRONT_OSA])
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def screen_visible_frame():
    """(x, y, w, h) of the MAIN display, parsed from Finder's 'left, top, right, bottom' desktop
    bounds — or None on any failure (a Finder automation denial / unparseable output / degenerate
    rect). Best-effort: the free-quadrant probe treats None as 'occupancy unknown → empty'."""
    r = _run(["osascript", "-e", SCREEN_FRAME_OSA])
    if r.returncode != 0:
        return None
    try:
        left, top, right, bottom = [int(float(x)) for x in (r.stdout or "").strip().split(",")]
    except (ValueError, AttributeError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


def all_display_frames():
    """Every active display's bounds as (x, y, w, h) in the SAME top-left-origin global space as the
    AX/Finder window bounds — Quartz ``CGDisplayBounds`` is y-DOWN global coords, matching (NSScreen
    is y-UP, which would misclassify quadrants, so it is deliberately NOT used). Empty list on ANY
    failure (pyobjc/Quartz absent at runtime, API error) ⇒ the caller degrades to the main-display
    best-effort frame. Read-only, no Accessibility prompt."""
    try:
        import Quartz  # pyobjc; lazy so the module imports even where Quartz is unavailable
        # (pyobjc dynamic attrs — Pyright has no stubs; verified live: CGGetActiveDisplayList +
        #  CGDisplayBounds return y-down global rects matching the AX bounds.)
        err, ids, cnt = Quartz.CGGetActiveDisplayList(16, None, None)  # type: ignore[attr-defined]
        if err or not cnt:
            return []
        frames = []
        for d in list(ids)[:cnt]:
            b = Quartz.CGDisplayBounds(d)  # type: ignore[attr-defined]
            w, h = int(b.size.width), int(b.size.height)
            if w > 0 and h > 0:
                frames.append((int(b.origin.x), int(b.origin.y), w, h))
        return frames
    except Exception:
        return []


def frame_containing(point, frames):
    """The (x,y,w,h) frame whose rect contains ``point`` (px,py), or None. Half-open on the far edges
    so two adjacent displays never both claim a shared border pixel."""
    if point is None:
        return None
    px, py = point
    for (fx, fy, fw, fh) in frames:
        if fx <= px < fx + fw and fy <= py < fy + fh:
            return (fx, fy, fw, fh)
    return None


def probe_left_quadrant_occupancy(windows, target_desktop, exclude_wid):
    """Per-quadrant COUNT (a ``Counter``) of windows in {top-left, bottom-left} on the TARGET
    desktop+display — the input to the count-based ``least_loaded_slot`` load-balancer. Best-
    effort: we are ON the target desktop (post-goto), so each non-target window's bounds (via the
    EXISTING osascript bounds reader) classify into a left quadrant. Excludes the target window
    itself (``exclude_wid``) so a worker never blocks its own slot; a missing frame / unreadable
    bounds just skips that window (→ may fall back to top-left), never a hard failure.

    Multi-display aware: with ≥2 active displays the occupancy frame is pinned to the display that
    CONTAINS the target window (so a worker on a secondary display alternates against ITS screen's
    quadrants, not the main screen's — the bug Finder's main-display-only ``window of desktop`` frame
    caused), and only windows on that same display are counted. With a single display, or when Quartz
    / the target's bounds are unavailable, behavior is unchanged: the main-display best-effort frame
    classifies every same-desktop window. Worst case under degrade = same-quadrant stacking, never a
    wrong window / wrong desktop."""
    frames = all_display_frames()
    restrict = None    # when set (multi-display), only windows centered in this frame are counted
    frame = None
    if len(frames) >= 2:
        # find the target window's display via its bounds' center (top-left-origin, matching Quartz)
        target_title = next((w.get("title", "") for w in windows
                             if w.get("window_number") == exclude_wid), "")
        tb = parse_bounds(capture_bounds_by_title(target_title)) if target_title else None
        if tb is not None:
            frame = frame_containing((tb[0] + tb[2] // 2, tb[1] + tb[3] // 2), frames)
            if frame is not None:
                restrict = frame
    if frame is None:
        # single display / Quartz unavailable / target display unresolved →
        # multi-display: best-effort (main-display frame).
        frame = screen_visible_frame()
    if frame is None:
        return Counter()
    counts: Counter = Counter()
    for w in windows:
        if w.get("window_number") == exclude_wid:
            continue
        if target_desktop is not None and w.get("desktop") != target_desktop:
            continue
        title = w.get("title", "")
        if not title:
            continue
        bounds = parse_bounds(capture_bounds_by_title(title))
        if bounds is None:
            continue
        if restrict is not None:   # multi-display: skip windows not on the target's display
            if frame_containing((bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2),
                                [restrict]) is None:
                continue
        q = quadrant_of(bounds, frame)
        if q:
            counts[q] += 1
    return counts


def raise_window(title):
    """Bring the window whose name EXACTLY EQUALS title to the front so Rectangle acts on it."""
    r = _run(["osascript", "-e", RAISE_OSA, title])
    if r.returncode != 0:
        log(f"  ⚠️ AXRaise osascript rc={r.returncode}: {r.stderr.strip()[:160]}")


def fire_rectangle(slot):
    """Fire the Rectangle action on the frontmost window. slot ∈ VALID_SLOTS (URL-safe)."""
    return _run(["open", f"rectangle://execute-action?name={slot}"])


def _report_delta(before, after, slot):
    """Print an honest bounds-delta verdict (changed / unchanged / unverifiable)."""
    changed = bounds_changed(before, after)
    log(f"  bounds before: {before or '(unreadable)'}")
    log(f"  bounds after : {after or '(unreadable)'}")
    if changed is True:
        log(f"  ✅ window moved → slot '{slot}' applied (position/size changed).")
    elif changed is False:
        log(f"  ⚠️ bounds UNCHANGED — Rectangle action '{slot}' had no effect "
            "(already tiled there? wrong frontmost? Rectangle url action disabled?).")
    else:
        log(f"  ⚠️ could NOT verify the move (bounds unreadable) — not claiming success.")
    return changed


# ── flows ────────────────────────────────────────────────────────────────────


def poll_resolve(project, task_id, wid, wait):
    """Resolve to EXACTLY ONE target window. Polls winlist up to ``wait`` seconds (a
    just-spawned worker window appears asynchronously). Returns (window, windows, None) on
    success, else (None, windows, reason). >1 match is ambiguous and fails immediately
    (waiting won't disambiguate); 0 matches retries until the deadline."""
    deadline = time.monotonic() + max(0.0, wait)
    windows = []
    while True:
        windows = probe_windows()
        matches = find_by_wid(windows, wid) if wid is not None else \
            find_by_task(windows, project, task_id)
        if len(matches) == 1:
            return matches[0], windows, None
        if len(matches) > 1:
            return None, windows, f"ambiguous — {len(matches)} windows match (won't guess)"
        if time.monotonic() >= deadline:
            ident = f"wid {wid}" if wid is not None else f"task '{task_id}'"
            return None, windows, f"no window matches {ident} (project={project})"
        time.sleep(1.5)


def run_self(project, slot, execute):
    """Place the frontmost Code window (coordinator self-placement)."""
    title = read_frontmost_title()
    if title is None:
        die("could not read the frontmost Code window title (osascript failed)")
    log(f"=== coord-place-window (--self) | project={project} | slot={slot} ===")
    log(f"frontmost window: {title[:72]}")
    if not is_self_placeable(title, project):
        die(f"--self REFUSED: frontmost window is not a 🧭 {project} coordinator window "
            f"(got {title[:60]!r}). Never firing on an unknown frontmost window.")
    log(f"PLAN: place THIS (frontmost 🧭 {project}) window → '{slot}'")
    if not execute:
        log("DRY-RUN (nothing moved). Re-run with --execute to place.")
        return
    if not rectangle_running():
        die("Rectangle.app is not running — start it; refusing to silently no-op.")
    # The frontmost window + Rectangle's "act on frontmost" target are GLOBAL shared state, so even
    # though --self never goto's, it must serialize with every other execute-mode placement (a
    # concurrent run_self/run_place could steal focus between our frontmost check and the fire).
    with placement_lock() as got:
        if not got:
            die("another window-placement holds the machine-wide placement lock "
                "(concurrent placement) — skipped to avoid a wrong-window fire; re-run if needed.")
        # Re-read + re-validate the frontmost UNDER the lock (no concurrent op can change it now).
        title = read_frontmost_title()
        if title is None or not is_self_placeable(title, project):
            shown = title[:60] if title is not None else None   # None-safe (no subscript on None)
            die(f"--self REFUSED under lock: frontmost is no longer a 🧭 {project} coordinator "
                f"window (got {shown!r}). fail-closed.")
        raise_window(title)            # bring the validated 🧭 window to the ABSOLUTE front
        time.sleep(0.4)
        if not frontmost_is(title):    # fail-closed: another app/window is frontmost → do NOT fire
            die("after raise, the validated window is NOT the global frontmost (another app or "
                "window holds focus) — refusing to fire Rectangle on the wrong window.")
        before = capture_front_bounds()
        fire_rectangle(slot)
        time.sleep(0.9)
        after = capture_front_bounds()
        _report_delta(before, after, slot)


def run_place(project, task_id, wid, slot, wait, execute):
    """Locate the target --project window and tile it to ``slot`` (mirror close-windows flow).

    The active-Space critical section (fresh detect_active_desktop → goto → raise → fire →
    restore) runs under the machine-wide ``placement_lock``: the owner's active desktop is a
    single GLOBAL OS state, so two concurrent placements (different projects' coordinators)
    would race on it — one's goto lands mid-flight of the other and the "restore to active"
    reads a transient wrong desktop, stranding the owner on the wrong Space."""
    win, windows, err = poll_resolve(project, task_id, wid, wait)
    if err or win is None:
        die(f"cannot resolve target: {err}")
    target_wid = win.get("window_number")
    target_desktop = win.get("desktop")
    title0 = win.get("title", "")
    proj0 = parse_title(title0)[0]

    log(f"=== coord-place-window | project={project} ===")
    log(f"target: wid {target_wid} | desktop {target_desktop} | slot {slot}")
    log(f"        {title0[:72]}")

    # RED LINE #4: never place a window of another chain.
    if proj0 != project:
        die(f"target project field={proj0!r} != --project {project!r} — RED LINE #4: "
            "refusing to place another chain's (or an unstructured) window.")

    if not execute:
        # Best-effort PREVIEW only — dry-run never enters the lock, so the active read here is
        # advisory (the real EXECUTE path re-detects fresh inside the lock).
        active_preview = detect_active_desktop(windows)
        slot_disp = ("free-left-quadrant (auto: least-loaded of top-left/bottom-left by count, "
                     "decided under lock)" if slot == FREE_QUADRANT else f"'{slot}'")
        log(f"PLAN: goto desktop {target_desktop} (if != active {active_preview} [preview]) → "
            f"AXRaise → rectangle {slot_disp} → restore that desktop.")
        log("DRY-RUN (nothing moved). Re-run with --execute to place.")
        return

    if not rectangle_running():
        die("Rectangle.app is not running — start it; refusing to silently no-op.")

    # ── machine-wide critical section: serialize the goto-bearing block across the whole
    #    machine so concurrent multi-project placements can't race on the GLOBAL active Space. ──
    with placement_lock() as got:
        if not got:
            die("another window-placement holds the machine-wide desktop-switch lock "
                "(concurrent multi-project placement) — skipped to avoid a goto race; re-run if needed.")

        # Re-probe FRESH inside the lock so this reads the owner's TRUE active desktop, not a
        # concurrent placement's transient mid-goto state.
        active = detect_active_desktop(probe_windows())
        log(f"--- active desktop={active} (detected inside lock) ---")
        # Re-resolve the target's CURRENT desktop inside the lock (it may have migrated during the
        # wait); fall back to the pre-lock value if the fresh probe can't find it (the §6a re-probe
        # below + frontmost_is still fail-close any mismatch).
        _fm = find_by_wid(probe_windows(), target_wid)
        cur_target_desktop = _fm[0].get("desktop") if len(_fm) == 1 else target_desktop

        did_goto = False

        def _restore_and_die(msg) -> NoReturn:
            if did_goto and active is not None:
                goto(active)
            die(msg)

        # Fail-closed Space switch (only when the target is on a different, known desktop).
        if cur_target_desktop is not None and cur_target_desktop != active:
            if active is None:
                die("active desktop is unknown — refusing a cross-desktop goto (could not restore "
                    "the owner's Space afterward). fail-closed.")
            if not goto(cur_target_desktop):
                die(f"goto desktop {cur_target_desktop} FAILED → refusing to fire on the wrong Space "
                    "(fail-closed).")
            did_goto = True

        # §6a TOCTOU root-fix: re-read winlist FRESH post-goto, follow the STABLE wid to its
        # CURRENT title, and require that title to be UNIQUE before the EXACT-name AXRaise.
        fresh = probe_windows()
        fresh_match = find_by_wid(fresh, target_wid)
        if len(fresh_match) != 1:
            _restore_and_die(f"target wid {target_wid} vanished/duplicated post-goto "
                             f"(found {len(fresh_match)}) — fail-closed, nothing fired.")
        title = fresh_match[0].get("title", "")
        if not title_unique(fresh, title):
            _restore_and_die(f"target title is empty or NOT unique post-goto "
                             f"({title[:46]!r}) — fail-closed, nothing fired.")
        if parse_title(title)[0] != project:
            _restore_and_die(f"target title's project changed to {parse_title(title)[0]!r} "
                             "post-goto — fail-closed (RED LINE #4).")

        # FREE_QUADRANT (worker, no --worker-index): pick the concrete left quadrant NOW — under the
        # lock, on the target desktop, BEFORE we move the target — from the current per-quadrant window
        # COUNTS (place to the side with FEWER), so auto-spawned workers LOAD-BALANCE instead of
        # stacking. Probe excludes the target itself.
        place_slot = slot
        if slot == FREE_QUADRANT:
            counts = probe_left_quadrant_occupancy(fresh, cur_target_desktop, target_wid)
            n_tl, n_bl = counts.get("top-left", 0), counts.get("bottom-left", 0)
            place_slot = least_loaded_slot(n_tl, n_bl)
            log(f"--- free-quadrant: left-counts top-left={n_tl} bottom-left={n_bl} → slot "
                f"'{place_slot}' (least-loaded) ---")

        raise_window(title)
        time.sleep(0.4)
        if not frontmost_is(title):
            _restore_and_die("target is NOT the global frontmost after AXRaise (focus steal / "
                             "AXRaise no-op) — fail-closed, nothing fired.")
        before = capture_bounds_by_title(title)
        fire_rectangle(place_slot)
        time.sleep(0.9)
        after = capture_bounds_by_title(title)
        _report_delta(before, after, place_slot)

        if did_goto:
            if active is not None and goto(active):
                log(f"--- restored active desktop {active} ---")
            elif active is not None:
                log(f"⚠️ could not restore desktop {active} — you may be on the target's Space now.")
            else:
                log("⚠️ active desktop was undetectable up front — not auto-restored (check your Space).")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="project slug (EXACT title field match)")
    ap.add_argument("--wid", help="stable Quartz window-id of the target (mutually excl. with --task)")
    ap.add_argument("--task", help="resolve the unique --project window whose task-id field == this")
    ap.add_argument("--slot", choices=VALID_SLOTS, help="explicit Rectangle slot")
    ap.add_argument("--role", choices=("coord", "worker"),
                    help="derive slot: coord→right-half; worker→top-left(even)/bottom-left(odd), "
                         "or least-loaded left quadrant when --worker-index is omitted")
    ap.add_argument("--worker-index", type=int, default=None,
                    help="worker index (with --role worker): even→top-left, odd→bottom-left. "
                         "OMIT → auto-pick the LEAST-LOADED (fewer windows) left quadrant on the "
                         "target desktop")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="poll winlist up to N secs for the target to appear (just-spawned worker)")
    ap.add_argument("--self", dest="self_place", action="store_true",
                    help="place the CURRENTLY-FRONTMOST Code window (coordinator self-placement)")
    ap.add_argument("--execute", action="store_true", help="actually place (default = dry-run)")
    args = ap.parse_args()

    if not _ID_RE.match(args.project):
        die(f"--project must be ASCII [A-Za-z0-9_-]: {args.project!r}")

    try:
        slot = resolve_slot(args.slot, args.role, args.worker_index)
    except ValueError as e:
        die(str(e))

    # ── --self path: frontmost window, no identity/goto/restore ──
    if args.self_place:
        if args.wid or args.task:
            die("--self is mutually exclusive with --wid / --task (self == frontmost window)")
        if slot is None:
            slot = "right-half"   # self == coordinator → default right-half
        return run_self(args.project, slot, args.execute)

    # ── locate-and-place path ──
    if not os.path.exists(WINLIST):
        die(f"winlist not found at {WINLIST} (needs the dharmaxis vscode-spaces tools)")
    if bool(args.wid) == bool(args.task):
        die("pass exactly one of --wid / --task (non-self placement needs an identity)")
    if slot is None:
        die("no slot resolved — pass --slot or --role")

    wid = None
    if args.wid:
        if not args.wid.isdigit():
            die(f"--wid must be a positive integer Quartz window-id: {args.wid!r}")
        wid = int(args.wid)
    task_id = None
    if args.task:
        if not _ID_RE.match(args.task):
            die(f"--task must be ASCII [A-Za-z0-9_-]: {args.task!r}")
        task_id = args.task

    return run_place(args.project, task_id, wid, slot, args.wait, args.execute)


if __name__ == "__main__":
    main()
