# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 246s / 用 1 次
- gemini: `ok` / gemini-3-pro-preview(api) / 64s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 246s_


**Findings**

P0: retry path still has a substring fail-open and bypasses the new veto. On a `.focus_contended` retry, [install/auto-continue.sh](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1822) uses `_wins_contain` substring matching, and [line 1826](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1826) uses shell glob substring matching. For `demo-coord-7`, an already-front `demo-coord-77 ... [worktree]` window is treated as “target already frontmost”, so the code skips `code -n`, skips `wait_target_window_frontmost`, skips the discriminator entirely, then opens the URI into the foreign window at [line 2004](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:2004). With the stated live press-now default, that can also auto-submit. This must use whole-token matching too.

P0: markerless foreign windows still slip through the 2.4 discriminator. `_foreign` only fires when the front title already contains `[worktree]` or `[singlepane]` at [line 1923](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1923). A concurrent foreign window opened after this task’s snapshot but before its custom title renders has no marker, so `_foreign=0`; if its title is not exactly in `_snap_wins`, the dispatch condition at [line 1932](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1932) still sends the URI there. Test #16 actually preserves this unsafe class for generic markerless titles at [tests/test_focus_drift.py](/Users/chenmingzhong/Projects/handoff-fanout/tests/test_focus_drift.py:706). With title-only evidence, generic markerless singlepane cannot be proven yours; fail-closed is the safe direction.

The populated-title invariant is sound: generated worktree/singlepane titles bind project, task, role, nonce, and marker in one `window.title`; coordinator prefix only wraps it. `_title_has_token` is also basically correct for ASCII task ids/nonces and `·` boundaries.

I do not agree that the legacy `HANDOFF_PRESS_NOW=0` path is harmless, only that it is not the primary default-path blocker. Its AppleScript `contains` checks remain substring-based, so it should be hardened before treating the fallback as a safety rail.

Verdict: RED — retry substring bypass and markerless-foreign discriminator slip remain must-fix P0 holes.

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 64s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
Verdict: RED (Fail-OPEN hole on lagging/unpopulated foreign windows)

**Technical Hole (The Race Condition):**
The `_foreign` veto only successfully blocks *fully populated* foreign windows. If the concurrent sibling's window is also lagging and hasn't rendered the `[worktree]` or `[singlepane]` marker yet, its title will be a generic string (e.g., its folder basename like `fateforge` or `Visual Studio Code`). 
In this state:
1. The `case *"[worktree]"*` check falls through, leaving `_foreign=0`.
2. Because this sibling window was opened *after* our pre-open snapshot, its temporary title is naturally `∉ _snap_wins`.
The ∉-snapshot fallback will immediately misidentify this unpopulated sibling window as our own lagging window, dispatching the prompt into the wrong session. The fix fails completely against slow-to-render concurrent spawns.

**Audit Focus Insights (Architecture & Costs):**
* **Ecosystem Lock-in & Long-term Costs:** Using `osascript` to scrape GUI window titles for critical concurrency control tightly couples the system to undocumented macOS UI timing and VS Code's internal rendering lifecycle. This guarantees an endless tail of timing Heisenbugs.
* **Opportunity Cost & Adoption Friction:** Engineering cycles are being burned playing whack-a-mole with shell-script race conditions. A resilient fleet dispatcher should abandon UI-scraping for prompt delivery and instead rely on robust IPC (e.g., routing URIs via a dedicated VS Code extension, a local socket, or a state file), which would categorically eliminate window-focus collisions.

[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
