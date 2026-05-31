# ⚠️ OBSOLETE — autoclose removed (2026-05-31)

The autoclose tab-closing feature this extension drove was removed: VS Code
tabs are the human-audit record and must not auto-close. This extension is no
longer installed by `install/install.sh` (which now uninstalls it). Kept only
for history.

---

# DHARMAXIS Handoff Helper

Auto-closes a stale Claude Code tab after the handoff workflow spawns a fresh
tab for the next task. The extension does nothing on its own — it only reacts
to a URI invoked by the handoff-fanout launchd watcher
(`install/auto-continue.sh`).

## How it works

The watcher opens a single canonical URI:

```
vscode://dharmaxis.handoff-helper/autoclose?task_id=<id>&nonce=<hex>&project=<slug>
```

On receipt the extension:

1. validates the `nonce` format (16 lowercase hex chars, `secrets.token_hex(8)`);
2. closes every **non-dirty** tab in the activated window (dirty tabs are never
   touched — unsaved work is the master's, not ours);
3. retries once after 500 ms if VS Code's `close()` returns `false`.

## Opt-in

Autoclose is **off by default**. It is enabled on the watcher side via
`HANDOFF_AUTOCLOSE_ENABLED=1` or an `autoclose.enabled` sentinel file — see the
handoff-fanout docs. Installing this extension alone does not start closing tabs.

## Status — do NOT install

This extension is **obsolete** (autoclose removed, 2026-05-31) and intentionally
has **no install instructions**. `install/install.sh` no longer builds or installs
it — it now *uninstalls* any previously-installed copy (`code --uninstall-extension
dharmaxis.handoff-helper`; pass `--no-extension` to skip that migration). The
source is retained for history only; re-installing it would resurrect the
tab-closing behaviour that the 2026-05-31 owner ruling removed (VS Code tabs are
the human-audit record and must not auto-close).
