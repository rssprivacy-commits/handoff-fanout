"""One-time succession-authority tokens — Step1 G4 收口 (tribrain MUST#1).

``handoff spawn --role supervisor_succession`` is the only spawn path that closes a
predecessor (coordinator) window. Left as a bare public CLI it is a legal bypass of the
v5.4 retro mandate (root cause G4: a coordinator could relay/交棒 with zero retrospective).
This module is the executable internal-vs-manual discriminator the design demands:

  * ``handoff audit-close --coordinator --status active`` calls :func:`issue_token` ONLY
    AFTER its inner retro-gated dump returned 0 — so a live token is machine proof of a
    fresh retro-gated coordinator close.
  * ``handoff spawn --role supervisor_succession --succession-token <path>`` calls
    :func:`consume_token`: path containment + 0600 permission + filename↔payload identity
    + project match + TTL — then UNLINKS the file (one-time; the unlink races concurrent
    consumers and exactly one wins). Missing/expired/mismatched/replayed → fail closed.
  * every issue / consume / reject appends a line to
    ``$HANDOFF_HOME/<project>/authority/succession-audit.log`` (auditable).

PATH/permission-based by design (§6b red line): never session-name prefixes, never
content sniffing. Threat model is the same as the rest of the retro mandate
(``HANDOFF_RETRO_BYPASS`` exists): it stops DRIFT — a session habitually skipping the
retro gate — not a determined local user, who owns this machine and could forge any
file. A NORMAL manual CLI invocation cannot hold a live token (0600, ≤120s, one-time),
which is exactly the G4 guarantee.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout import spawn_nonce as _spawn_nonce

TOKEN_TTL_SECONDS = 120
# Small allowance for clock skew between issuer and consumer processes (same host in
# practice, so anything beyond this means a tampered/garbage issued_at — reject).
_FUTURE_SKEW_TOLERANCE_SECONDS = 30

_TOKEN_NAME_RE = re.compile(
    r"^succession-(?P<task>[a-z0-9][a-z0-9-]*[a-z0-9])\.(?P<nonce>[0-9a-f]+)\.token$"
)

AUDIT_LOG_NAME = "succession-audit.log"


def _now() -> datetime:
    return datetime.now(UTC)


def authority_dir(home: Path, project: str) -> Path:
    return home / project / "authority"


def _audit_log(home: Path, project: str, event: str, detail: str) -> None:
    """Append one audit line. Best-effort: the log is forensics, never a gate —
    an unwritable log must not block an otherwise valid issue/consume."""
    try:
        d = authority_dir(home, project)
        d.mkdir(parents=True, exist_ok=True)
        with (d / AUDIT_LOG_NAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{_now().isoformat(timespec='seconds')} {event} {detail}\n")
    except OSError:
        pass


def _sweep_expired(home: Path, project: str) -> None:
    """Best-effort removal of expired token files so the dir can't accumulate stale
    authority. Never touches a still-live token; failures are ignored (a leftover
    expired file is harmless — consume_token rejects it by TTL anyway)."""
    d = authority_dir(home, project)
    if not d.is_dir():
        return
    for p in d.glob("succession-*.token"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            issued = datetime.fromisoformat(str(payload.get("issued_at", "")))
            age = (_now() - issued).total_seconds()
        except (OSError, ValueError):
            age = TOKEN_TTL_SECONDS + 1  # unreadable/garbage → treat as expired
        if age > TOKEN_TTL_SECONDS:
            try:
                p.unlink(missing_ok=True)
                _audit_log(home, project, "SWEPT-EXPIRED", f"token={p.name}")
            except OSError:
                pass


def issue_token(*, home: Path, project: str, task: str) -> Path:
    """Issue a fresh one-time succession authority for ``project``.

    ``task`` is the SUCCESSOR task id (Step2 契约 C.1 语义厘清 / 修 F4 歧义):
    ``audit-close --task`` is the next coordinator leg under v5.4 dump semantics, and
    that is exactly what callers pass here. The consumer binds on project + nonce +
    TTL **+ this successor task** (``consume_token``'s required ``expected_task``) —
    the token authorizes ONE designated succession, never "any succession in this
    project for 120s".

    The file is created 0600 + ``O_EXCL`` (the nonce is cryptographically unique, so a
    name collision means something is forging — fail loudly rather than overwrite).
    """
    _sweep_expired(home, project)
    d = authority_dir(home, project)
    d.mkdir(parents=True, exist_ok=True)
    nonce = _spawn_nonce.new_nonce()
    path = d / f"succession-{task}.{nonce}.token"
    payload = {
        "schema": 1,
        "project": project,
        "task": task,
        "nonce": nonce,
        "issued_at": _now().isoformat(timespec="seconds"),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except OSError:
        # Never leave a half-written authority file behind.
        path.unlink(missing_ok=True)
        raise
    _audit_log(home, project, "ISSUED", f"token={path.name} successor_task={task}")
    return path


def consume_token(
    token_path: Path, *, home: Path, project: str, expected_task: str
) -> tuple[bool, str]:
    """Validate + CONSUME (unlink) a succession token. Returns ``(ok, reason)``.

    ``expected_task`` is REQUIRED (Step2 契约 C.2 / SHOULD#5 接口纪律): the consumer's
    own successor task id (``spawn --role supervisor_succession --task X`` passes X).
    The token payload's ``task`` (bound by the issuer to the designated successor) must
    match it — a mismatch is REJECTED *without consuming* (a wrong-task spawn must not
    burn the designated successor's authority). This collapses the pre-Step2 "project-
    level 120s universal key" to a designated-successor key. Missing/empty
    ``expected_task`` is rejected loudly (never silently un-bound).

    Consumption happens on the validated file via ``unlink`` — when two spawns race the
    same token, exactly one unlink succeeds and the loser is rejected (one-time). The
    token is consumed even though the spawn may still fail afterwards: the conservative
    direction (a failed spawn needs a fresh ``audit-close`` re-issue) is preferred over
    a reusable authority.
    """

    def _reject(reason: str) -> tuple[bool, str]:
        _audit_log(home, project, "REJECTED", f"token={token_path.name} reason={reason}")
        return False, reason

    if not expected_task:
        return _reject(
            "expected_task missing/empty — the consumer must bind its own successor "
            "task id (spawn passes its --task); an un-bound consume is not allowed"
        )

    try:
        resolved = token_path.resolve()
    except OSError as e:
        return _reject(f"unresolvable path ({e})")
    expected_dir = authority_dir(home, project).resolve()
    if resolved.parent != expected_dir:
        return _reject(f"not under the {project!r} authority dir {expected_dir}")

    m = _TOKEN_NAME_RE.match(resolved.name)
    if not m:
        return _reject("filename is not succession-<task>.<nonce>.token")

    try:
        st = resolved.lstat()
    except FileNotFoundError:
        return _reject("missing (never issued, expired-swept, or already consumed)")
    except OSError as e:
        return _reject(f"unreadable ({e})")
    if not os.path.isfile(resolved) or os.path.islink(resolved):
        return _reject("not a regular file")
    if st.st_mode & 0o077:
        return _reject("permissions are not owner-only (must be 0600)")

    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _reject(f"unparseable payload ({e})")
    if not isinstance(payload, dict):
        return _reject("payload is not a JSON object")
    if payload.get("project") != project:
        return _reject(f"issued for project {payload.get('project')!r}, not {project!r}")
    if payload.get("task") != m.group("task") or payload.get("nonce") != m.group("nonce"):
        return _reject("filename/payload identity mismatch (tampered?)")
    if payload.get("task") != expected_task:
        # Step2 C.2: the authority designates ONE successor; a different task may not
        # ride it. Reject WITHOUT unlinking — the designated successor can still spawn.
        return _reject(
            f"task-mismatch: token is bound to successor_task={payload.get('task')!r}, "
            f"this spawn's task is {expected_task!r}"
        )

    try:
        issued = datetime.fromisoformat(str(payload.get("issued_at", "")))
        if issued.tzinfo is None:
            return _reject("issued_at lacks a timezone")
    except ValueError:
        return _reject("issued_at missing or unparseable")
    age = (_now() - issued).total_seconds()
    if age > TOKEN_TTL_SECONDS:
        return _reject(f"expired ({age:.0f}s old, TTL {TOKEN_TTL_SECONDS}s)")
    if age < -_FUTURE_SKEW_TOLERANCE_SECONDS:
        return _reject("issued_at is in the future")

    # ── consume: the unlink IS the one-time gate (exactly one racer wins) ──
    try:
        resolved.unlink()
    except FileNotFoundError:
        return _reject("consumed by a concurrent spawn")
    except OSError as e:
        # Fail CLOSED: if we cannot guarantee one-time-ness, we must not authorize.
        return _reject(f"could not consume ({e})")

    _audit_log(home, project, "CONSUMED", f"token={resolved.name}")
    return True, ""
