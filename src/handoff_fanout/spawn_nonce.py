"""Unguessable per-spawn nonce — bound into the VS Code window.title so the
watchdog can ATOMICALLY prove the front window is the exact window we launched
(kills focus-drift TOCTOU). task_id may repeat across spawns; the nonce never does."""

from __future__ import annotations

import secrets

SEP = " · "


def new_nonce() -> str:
    """64-bit hex, cryptographically random (secrets, not random — never guessable)."""
    return secrets.token_hex(8)


def title_for(*, project: str, task_id: str, role: str, nonce: str) -> str:
    return SEP.join((project, task_id, role, nonce))


def nonce_in_title(title: str, nonce: str) -> bool:
    """True iff ``title`` contains the exact nonce token (exact, not prefix)."""
    return nonce in title.split(SEP)
