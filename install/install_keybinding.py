#!/usr/bin/env python3
"""Idempotently install the handoff-fanout SINGLE-PANE keybinding into the user's VS Code
``keybindings.json``.

Binds ``cmd+ctrl+alt+<key>`` (key default ``9`` from ``HANDOFF_SIDEBAR_CLOSE_KEY``) to
``runCommands[workbench.action.closeSidebar, workbench.action.closeAuxiliaryBar]`` so
``auto-continue.sh`` can collapse a cold worktree window to a single editor pane via ONE guarded
keystroke BEFORE the URI — eliminating the empty-Claude-sidebar focus competitor that made ~40% of
cold auto-submits miss (root-cause fix 2026-06-06, dual-brain codex+Gemini + owner ruling).

DELIBERATELY NOT the tombstoned hack (``keybindings-claude-focus.json``): NO ``claude-vscode.focus``
chord, and IDEMPOTENT *close* commands (never the stateful ``Cmd+B`` / ``Cmd+Alt+B`` toggles).

SAFETY (hardened after the implementation audit — codex+Gemini both flagged the naive textual merge):
- **Never corrupts keybindings.json.** The file is parsed (comment-aware) and must validate as a JSON
  array or it is left UNTOUCHED (fail-soft). The insertion point is found by a JSONC-aware scanner that
  ignores ``]`` inside strings/comments and handles empty arrays + trailing commas; the merged result is
  re-validated before it is written. User bindings AND comments are preserved (textual insert).
- **Atomic + backed up.** Writes go through a temp file + ``os.replace``; the original is copied to a
  ``.handoff-bak`` sidecar first.
- **Fail-safe runtime coupling.** On success a SENTINEL is written
  (``$HANDOFF_ROOT/.singlepane-keybinding.installed``). ``auto-continue.sh`` fires the close-sidebars
  chord ONLY when that sentinel exists → if install is skipped/fails, or our chord key is already bound
  to a DIFFERENT command (we refuse to override), the runtime simply never sends the chord and the
  readiness-gate still guards the submit.

Env: ``HANDOFF_SIDEBAR_CLOSE_KEY`` (chord key), ``HANDOFF_KEYBINDINGS_FILE`` (target path, tests),
``HANDOFF_SINGLEPANE_SENTINEL`` / ``HANDOFF_ROOT`` (sentinel path, tests).

Exit codes: 0 = installed or already present (sentinel written) · 1 = fail-soft, file untouched
(not a JSON array / unparseable / merge failed validation) · 2 = chord key already bound to another
command, refused to override (no sentinel → runtime chord stays disabled).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

MARKER = "handoff-fanout-singlepane"
CLOSE_COMMANDS = ["workbench.action.closeSidebar", "workbench.action.closeAuxiliaryBar"]


def _key() -> str:
    k = os.environ.get("HANDOFF_SIDEBAR_CLOSE_KEY", "9")
    # single alnum only (the keybinding string + the osascript keystroke must agree); lowercase to match
    # VS Code's keybinding casing. Anything else → default.
    return k.lower() if (len(k) == 1 and k.isalnum()) else "9"


def _chord(key: str) -> str:
    return f"cmd+ctrl+alt+{key}"


def binding_text(key: str | None = None) -> str:
    key = key or _key()
    return (
        "  {\n"
        f"    // {MARKER} — collapse side bars before the cold-spawn URI (auto-continue.sh\n"
        "    // close_sidebars_if_front_window_contains). Re-key ⇒ set HANDOFF_SIDEBAR_CLOSE_KEY + re-run --sync-keybinding.\n"
        f'    "key": "{_chord(key)}",\n'
        '    "command": "runCommands",\n'
        f'    "args": {{ "commands": {json.dumps(CLOSE_COMMANDS)} }}\n'
        "  }"
    )


def keybindings_path() -> pathlib.Path:
    override = os.environ.get("HANDOFF_KEYBINDINGS_FILE")
    if override:
        return pathlib.Path(override)
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "keybindings.json"
    return home / ".config" / "Code" / "User" / "keybindings.json"


def sentinel_path() -> pathlib.Path:
    override = os.environ.get("HANDOFF_SINGLEPANE_SENTINEL")
    if override:
        return pathlib.Path(override)
    root = os.environ.get("HANDOFF_ROOT", str(pathlib.Path.home() / ".claude-handoff"))
    return pathlib.Path(root) / ".singlepane-keybinding.installed"


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` line and ``/* */`` block comments, respecting string literals → parseable JSON."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")  # replace the block comment with a space so adjacent tokens can't merge (1/*x*/2 → "1 2")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_trailing_commas(s: str) -> str:
    """Remove trailing commas (``,`` immediately before ``]`` / ``}``), string-aware, so VS Code's
    JSONC (which allows them) validates under the strict ``json`` module. Operates on already
    comment-stripped text."""
    out: list[str] = []
    i, n = 0, len(s)
    in_str = esc = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == ",":
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            if j < n and s[j] in "]}":
                i += 1  # drop the trailing comma
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_array(text: str):
    """Parse JSONC (comments + trailing commas tolerated) and return the list, or None if it is not a
    valid JSON array. Used for fail-soft validation, idempotency, and conflict detection."""
    try:
        data = json.loads(_strip_trailing_commas(_strip_jsonc(text)) or "[]")
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _scan_root_array_close(text: str):
    """Find the ROOT JSON array's closing ``]`` (where bracket depth returns to 0), ignoring ``]`` inside
    strings and ``//`` / ``/* */`` comments. Returns ``(close_idx, last_sig_char, last_sig_pos)`` where
    last_sig is the last significant char before that ``]`` (``[`` = empty array, ``,`` = trailing comma,
    else = an element) and last_sig_pos is its index. ``(None, None, None)`` if no balanced root array."""
    i, n = 0, len(text)
    in_str = esc = in_line = in_block = False
    depth = 0
    started = False
    last_sig: str | None = None
    last_sig_pos = -1
    while i < n:
        c = text[i]
        if in_line:
            if c == "\n":
                in_line = False
            i += 1
        elif in_block:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                in_block = False
                i += 2
            else:
                i += 1
        elif in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
                last_sig, last_sig_pos = '"', i
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            in_line = True
            i += 2
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            in_block = True
            i += 2
        elif c == '"':
            in_str = True
            i += 1
        elif c.isspace():
            i += 1
        elif c == "[":
            depth += 1
            started = True
            last_sig, last_sig_pos = "[", i
            i += 1
        elif c == "]":
            depth -= 1
            if started and depth == 0:
                return i, last_sig, last_sig_pos
            last_sig, last_sig_pos = "]", i
            i += 1
        else:
            last_sig, last_sig_pos = c, i
            i += 1
    return None, None, None


def _atomic_write(path: pathlib.Path, content: str) -> None:
    tmp = path.with_name(path.name + ".handoff-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX


def _backup(path: pathlib.Path, original: str) -> None:
    try:
        path.with_name(path.name + ".handoff-bak").write_text(original, encoding="utf-8")
    except OSError:
        pass  # best-effort


def _write_sentinel(key: str) -> None:
    # Absence of the sentinel disables the runtime chord (fail-safe), so a failed write just degrades.
    try:
        sp = sentinel_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        tmp = sp.with_name(sp.name + ".tmp")
        tmp.write_text(key + "\n", encoding="utf-8")
        os.replace(tmp, sp)  # atomic (R3 codex: no partial/corrupt sentinel)
    except OSError:
        pass


def _remove_sentinel() -> None:
    # Called on EVERY non-success path so a stale sentinel from a prior install can NEVER keep the runtime
    # chord enabled after a conflict/failure (R3 codex P1: a stale sentinel re-opened the fail-safe).
    try:
        sentinel_path().unlink()
    except OSError:
        pass  # already absent / unreadable → nothing to disable


def _is_our_binding(b: object, chord: str) -> bool:
    """True iff ``b`` is EXACTLY our unconditional single-pane binding — same key, ``runCommands`` with our
    exact close commands, and NO ``when`` clause (a conditional/partial match is NOT 'already installed')."""
    return (
        isinstance(b, dict)
        and b.get("key") == chord
        and b.get("command") == "runCommands"
        and isinstance(b.get("args"), dict)
        and b["args"].get("commands") == CLOSE_COMMANDS
        and "when" not in b
    )


def _validates_as_array_with(text: str, chord: str) -> bool:
    data = _parse_array(text)
    return data is not None and any(isinstance(b, dict) and b.get("key") == chord for b in data)


def _install_impl(path: pathlib.Path | None = None) -> int:
    path = path or keybindings_path()
    key = _key()
    chord = _chord(key)
    binding = binding_text(key)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        _atomic_write(path, "[\n" + binding + "\n]\n")
        _write_sentinel(key)
        print(f"✓ single-pane keybinding installed (new file): {path}")
        return 0

    text = path.read_text(encoding="utf-8")

    # 1) validate it is a JSON array (comments + trailing commas tolerated) — else leave it UNTOUCHED
    #    (never corrupt a non-array / unparseable file)
    parsed = _parse_array(text)
    if parsed is None:
        print(f"⚠ {path} is not a valid JSONC array — left untouched (close-chord stays a no-op; readiness-gate guards).",
              file=sys.stderr)
        return 1

    # 2) idempotency (content-based): our EXACT unconditional binding already present?
    if any(_is_our_binding(b, chord) for b in parsed):
        _write_sentinel(key)  # keep the runtime gate truthful even if the binding pre-existed
        print(f"✓ single-pane keybinding already present: {path}")
        return 0

    # 3) conflict: our chord already bound to ANY other (non-unbind) binding → refuse to override. Covers a
    #    foreign `runCommands` with different commands and a conditional (`when`) match (R3 codex P2).
    for b in parsed:
        if not isinstance(b, dict) or b.get("key") != chord or _is_our_binding(b, chord):
            continue
        if str(b.get("command") or "").startswith("-"):
            continue  # a "-command" unbind directive removes a default → not a real conflict
        print(f"⚠ {chord} is already bound to '{b.get('command')}' in {path} — NOT overriding. "
              f"Set HANDOFF_SIDEBAR_CLOSE_KEY to a free key + re-run --sync-keybinding. The single-pane "
              f"close-chord stays DISABLED (readiness-gate still guards the submit).", file=sys.stderr)
        return 2

    # 4) locate the ROOT array ']' (JSONC-aware) and insert our binding, preserving comments + user bindings
    close_idx, last_sig, last_sig_pos = _scan_root_array_close(text)
    if close_idx is None or last_sig_pos is None or last_sig_pos < 0:
        print(f"⚠ could not locate the root array ']' in {path} — left untouched.", file=sys.stderr)
        return 1
    sep = "" if last_sig in ("[", ",") else ","  # empty array / trailing comma → no extra comma
    new_text = text[: last_sig_pos + 1] + sep + "\n" + binding + "\n" + text[last_sig_pos + 1:]
    if not new_text.endswith("\n"):
        new_text += "\n"

    # 5) final safety net: refuse to write unless the merged result still validates as an array with our chord
    if not _validates_as_array_with(new_text, chord):
        print(f"⚠ refused to write — merged result did not validate as JSONC. {path} left untouched.",
              file=sys.stderr)
        return 1

    _backup(path, text)
    _atomic_write(path, new_text)
    _write_sentinel(key)
    print(f"✓ single-pane keybinding installed (merged, user bindings + comments preserved): {path}")
    return 0


def install(path: pathlib.Path | None = None) -> int:
    """Public entry. Runs the install, and on ANY non-success result REMOVES the sentinel so a stale one
    from a prior install can never keep the runtime close-chord enabled after a conflict/failure (R3 codex
    P1 fail-safe). Success paths write the sentinel inside ``_install_impl``."""
    rc = _install_impl(path)
    if rc != 0:
        _remove_sentinel()
    return rc


if __name__ == "__main__":
    sys.exit(install())
