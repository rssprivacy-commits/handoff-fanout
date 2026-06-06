"""Tests for ``install/install_keybinding.py`` — the idempotent, preservative merge of the SINGLE-PANE
VS Code keybinding (cmd+ctrl+alt+9 → runCommands[closeSidebar, closeAuxiliaryBar]) into the user's
keybindings.json. The keybinding lets auto-continue.sh collapse a cold worktree window to one editor
pane before the URI (root-cause cold-submit fix, 2026-06-06). It must NEVER add the tombstoned
``claude-vscode.focus`` chord, never duplicate, never clobber the user's bindings/comments, and never
corrupt a malformed file.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = REPO_ROOT / "install" / "install_keybinding.py"


def _load():
    spec = importlib.util.spec_from_file_location("install_keybinding", MOD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """CRITICAL: redirect the success-sentinel to tmp so install() never writes to the REAL
    ~/.claude-handoff, and start each test from a clean HANDOFF_SIDEBAR_CLOSE_KEY (default 9)."""
    monkeypatch.setenv("HANDOFF_SINGLEPANE_SENTINEL", str(tmp_path / "sentinel"))
    monkeypatch.delenv("HANDOFF_SIDEBAR_CLOSE_KEY", raising=False)


def _sentinel_written(key: str = "9") -> bool:
    sp = mod.sentinel_path()
    return sp.exists() and sp.read_text().strip() == key


def _parse_jsonc(text: str):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # /* block */
    text = re.sub(r"//[^\n]*", "", text)  # // line
    return json.loads(text)


def _runcommands(data):
    return [b for b in data if b.get("command") == "runCommands"]


def test_merge_preserves_existing_and_adds(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text(
        '[\n  {\n    "key": "cmd+n",\n    "command": "claude-vscode.editor.open"\n  }\n]\n',
        encoding="utf-8",
    )
    assert mod.install(kb) == 0
    data = _parse_jsonc(kb.read_text())
    assert len(data) == 2, "existing binding preserved + ours appended"
    assert any(b.get("command") == "claude-vscode.editor.open" for b in data), "user binding kept verbatim"
    rc = _runcommands(data)
    assert len(rc) == 1
    assert rc[0]["key"] == "cmd+ctrl+alt+9"
    assert rc[0]["args"]["commands"] == [
        "workbench.action.closeSidebar",
        "workbench.action.closeAuxiliaryBar",
    ]
    assert "claude-vscode.focus" not in kb.read_text(), "must NEVER add the tombstoned focus chord"


def test_idempotent_no_duplicate(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text("[]\n", encoding="utf-8")
    assert mod.install(kb) == 0
    first = kb.read_text()
    assert mod.install(kb) == 0  # second run = no-op
    assert kb.read_text() == first, "idempotent: second run leaves the file byte-identical"
    assert len(_runcommands(_parse_jsonc(kb.read_text()))) == 1, "no duplicate binding"


def test_creates_missing_file(tmp_path):
    kb = tmp_path / "sub" / "deep" / "keybindings.json"
    assert mod.install(kb) == 0
    assert kb.exists()
    data = _parse_jsonc(kb.read_text())
    assert len(data) == 1 and _runcommands(data)


def test_empty_array_variants(tmp_path):
    for body in ("[]\n", "[\n]\n", "[ ]"):
        kb = tmp_path / f"kb_{abs(hash(body))}.json"
        kb.write_text(body, encoding="utf-8")
        assert mod.install(kb) == 0
        data = _parse_jsonc(kb.read_text())
        assert len(data) == 1, f"empty-array variant {body!r} → exactly one binding"


def test_malformed_no_array_left_untouched(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text("not json, no bracket", encoding="utf-8")
    assert mod.install(kb) == 1, "fail-soft on a non-array file"
    assert kb.read_text() == "not json, no bracket", "must NOT clobber a malformed file"


def test_preserves_user_comments(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text(
        '// my custom keybindings\n[\n  // an inline comment\n  { "key": "cmd+k", "command": "x" }\n]\n',
        encoding="utf-8",
    )
    assert mod.install(kb) == 0
    txt = kb.read_text()
    assert "// my custom keybindings" in txt, "user header comment preserved"
    assert "// an inline comment" in txt, "user inline comment preserved"
    assert len(_parse_jsonc(txt)) == 2


def test_custom_key_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_SIDEBAR_CLOSE_KEY", "7")
    kb = tmp_path / "keybindings.json"
    kb.write_text("[]\n", encoding="utf-8")
    assert mod.install(kb) == 0
    assert _runcommands(_parse_jsonc(kb.read_text()))[0]["key"] == "cmd+ctrl+alt+7"


def test_invalid_key_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_SIDEBAR_CLOSE_KEY", "not-a-single-char")
    kb = tmp_path / "keybindings.json"
    kb.write_text("[]\n", encoding="utf-8")
    assert mod.install(kb) == 0
    assert _runcommands(_parse_jsonc(kb.read_text()))[0]["key"] == "cmd+ctrl+alt+9", "invalid key → default 9"


def test_keybindings_path_resolves(monkeypatch):
    monkeypatch.delenv("HANDOFF_KEYBINDINGS_FILE", raising=False)
    p = mod.keybindings_path()
    assert p.name == "keybindings.json"
    assert "Code" in str(p) and "User" in str(p)


def test_keybindings_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "kb.json"
    monkeypatch.setenv("HANDOFF_KEYBINDINGS_FILE", str(target))
    assert mod.keybindings_path() == target


# ----------------------------------------------------- audit-hardening: JSONC robustness + fail-safe (2026-06-06)


def test_sentinel_written_on_success(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text("[]\n", encoding="utf-8")
    assert mod.install(kb) == 0
    assert _sentinel_written("9"), "a successful install must write the runtime fail-safe sentinel with the key"


def test_trailing_comma_does_not_produce_double_comma(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text('[\n  { "key": "cmd+k", "command": "x" },\n]\n', encoding="utf-8")  # JSONC trailing comma
    assert mod.install(kb) == 0
    txt = kb.read_text()
    assert ",," not in re.sub(r"\s", "", txt), "must not create a double comma after an existing trailing comma"
    assert len(_parse_jsonc(txt)) == 2


def test_trailing_comment_containing_bracket_not_fooled(tmp_path):
    """The naive rfind(']') would insert after a trailing comment's ']' (outside the array) → corruption.
    The JSONC-aware scanner must find the REAL root ']' and keep the result valid."""
    kb = tmp_path / "keybindings.json"
    kb.write_text(
        '[\n  { "key": "cmd+k", "command": "x" }\n]\n// old binds [removed]\n', encoding="utf-8"
    )
    assert mod.install(kb) == 0
    data = _parse_jsonc(kb.read_text())  # must still parse
    assert len(data) == 2 and _runcommands(data), "valid merge despite a trailing comment containing ']'"
    assert "// old binds [removed]" in kb.read_text(), "trailing comment preserved"


def test_element_value_containing_bracket_not_fooled(tmp_path):
    """A binding whose VALUE contains ']' (e.g. a key literal) must not mislead the scanner."""
    kb = tmp_path / "keybindings.json"
    kb.write_text('[\n  { "key": "ctrl+]", "command": "x" }\n]\n', encoding="utf-8")
    assert mod.install(kb) == 0
    data = _parse_jsonc(kb.read_text())
    assert len(data) == 2
    assert any(b.get("key") == "ctrl+]" for b in data), "the ']'-in-value binding is preserved intact"


def test_block_comment_only_array(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text("[\n  /* nothing yet */\n]\n", encoding="utf-8")
    assert mod.install(kb) == 0
    assert len(_parse_jsonc(kb.read_text())) == 1, "empty array with a block comment → one binding, valid"


def test_conflict_same_key_different_command_refused(tmp_path):
    """If cmd+ctrl+alt+9 is ALREADY bound to a different command, we must NOT override it and must NOT write the
    sentinel (so the runtime never fires a chord that would trigger someone else's command). Exit 2, file untouched."""
    kb = tmp_path / "keybindings.json"
    original = '[\n  { "key": "cmd+ctrl+alt+9", "command": "someExtension.doThing" }\n]\n'
    kb.write_text(original, encoding="utf-8")
    assert mod.install(kb) == 2, "conflict → refuse (exit 2)"
    assert kb.read_text() == original, "the conflicting file must be left byte-identical (no override)"
    assert not mod.sentinel_path().exists(), "no sentinel on conflict → runtime chord stays disabled (fail-safe)"


def test_idempotent_content_based_ignores_marker_drift(tmp_path):
    """Idempotency is content-based (key + commands), so even if the marker comment was edited/removed the
    exact binding is detected and NOT duplicated."""
    kb = tmp_path / "keybindings.json"
    kb.write_text(
        '[\n  {\n    "key": "cmd+ctrl+alt+9",\n    "command": "runCommands",\n'
        '    "args": { "commands": ["workbench.action.closeSidebar", "workbench.action.closeAuxiliaryBar"] }\n'
        "  }\n]\n",
        encoding="utf-8",
    )  # our binding present but WITHOUT the marker comment
    assert mod.install(kb) == 0
    assert len(_runcommands(_parse_jsonc(kb.read_text()))) == 1, "no duplicate even when the marker is absent"


def test_atomic_write_makes_backup(tmp_path):
    kb = tmp_path / "keybindings.json"
    kb.write_text('[\n  { "key": "cmd+k", "command": "x" }\n]\n', encoding="utf-8")
    assert mod.install(kb) == 0
    bak = kb.with_name(kb.name + ".handoff-bak")
    assert bak.exists(), "the original must be backed up before the merge write"
    assert _parse_jsonc(bak.read_text()) == [{"key": "cmd+k", "command": "x"}], "the backup holds the pre-merge content"


def test_no_sentinel_when_left_untouched(tmp_path):
    """A fail-soft (non-array) path must NOT write a sentinel — the runtime must stay disabled."""
    kb = tmp_path / "keybindings.json"
    kb.write_text("garbage no array", encoding="utf-8")
    assert mod.install(kb) == 1
    assert not mod.sentinel_path().exists(), "no sentinel when the file was left untouched"


# ------------------------------------ R3 audit fixes: stale-sentinel removal / conflict breadth / block-comment merge


def test_conflict_removes_stale_sentinel(tmp_path):
    """R3 codex P1: a conflict must REMOVE any pre-existing sentinel so the runtime chord cannot stay enabled
    after our binding is no longer the one bound to the chord."""
    sp = mod.sentinel_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("9\n", encoding="utf-8")  # a stale sentinel from a prior successful install
    kb = tmp_path / "keybindings.json"
    kb.write_text('[\n  { "key": "cmd+ctrl+alt+9", "command": "someExtension.doThing" }\n]\n', encoding="utf-8")
    assert mod.install(kb) == 2
    assert not sp.exists(), "conflict must remove the stale sentinel (runtime chord disabled, fail-safe)"


def test_failsoft_removes_stale_sentinel(tmp_path):
    """R3 codex P1: a fail-soft (unparseable / non-array) path must also remove a stale sentinel."""
    sp = mod.sentinel_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("9\n", encoding="utf-8")
    kb = tmp_path / "keybindings.json"
    kb.write_text("not an array at all", encoding="utf-8")
    assert mod.install(kb) == 1
    assert not sp.exists(), "fail-soft must remove the stale sentinel"


def test_conflict_foreign_runcommands_same_key_refused(tmp_path):
    """R3 codex P2: a foreign `runCommands` bound to OUR chord (different commands) is still a conflict —
    must refuse (exit 2), not silently add a second binding on the same key."""
    kb = tmp_path / "keybindings.json"
    original = ('[\n  { "key": "cmd+ctrl+alt+9", "command": "runCommands",\n'
               '    "args": { "commands": ["workbench.action.toggleZenMode"] } }\n]\n')
    kb.write_text(original, encoding="utf-8")
    assert mod.install(kb) == 2, "foreign runCommands on our chord → conflict"
    assert kb.read_text() == original, "left untouched"


def test_conditional_when_binding_is_conflict_not_idempotent(tmp_path):
    """A same-key runCommands with our exact commands BUT a `when` clause is conditional → it is NOT 'already
    installed' (it would only fire in that context); treat it as a conflict rather than adding a 2nd binding."""
    kb = tmp_path / "keybindings.json"
    original = ('[\n  { "key": "cmd+ctrl+alt+9", "command": "runCommands", "when": "editorFocus",\n'
               '    "args": { "commands": ["workbench.action.closeSidebar", "workbench.action.closeAuxiliaryBar"] } }\n]\n')
    kb.write_text(original, encoding="utf-8")
    assert mod.install(kb) == 2, "a conditional (when) match is not idempotent → conflict"


def test_unbind_directive_on_same_key_not_a_conflict(tmp_path):
    """A negative `-command` unbind directive on our chord removes a default — it is NOT a real binding, so it
    must NOT block our install."""
    kb = tmp_path / "keybindings.json"
    kb.write_text('[\n  { "key": "cmd+ctrl+alt+9", "command": "-someDefault" }\n]\n', encoding="utf-8")
    assert mod.install(kb) == 0, "an unbind directive is not a conflict → our binding installs"
    data = _parse_jsonc(kb.read_text())
    assert len(data) == 2 and any(b.get("command") == "runCommands" for b in data)


def test_block_comment_token_merge_fails_soft(tmp_path):
    """R3 codex P2: a block comment must not let two tokens merge into a spuriously-valid value
    (`[1/*x*/2]` must NOT become `[12]`). Such a malformed file fails soft (untouched)."""
    kb = tmp_path / "keybindings.json"
    original = "[ 1/*x*/2 ]\n"
    kb.write_text(original, encoding="utf-8")
    assert mod.install(kb) == 1, "block-comment-separated tokens must not merge into valid JSON"
    assert kb.read_text() == original, "malformed file left untouched"
