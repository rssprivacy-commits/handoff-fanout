# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 232s / 用 1 次
- gemini: `ok` / gemini-3-pro-preview(api) / 20s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 232s_


Short answer: the core critical-section idea closes the launcher-vs-launcher race, but I would not GREEN this revision as stated.

1. For participating launcher ticks, yes: acquiring before the authoritative probe and holding through claim/open/Enter/relock makes another tick unable to relock after your unlocked decision. That closes the prior TOCTOU and piggyback path. Manual lock/unlock or non-participating tools remain outside that guarantee.

2. Folding `_RC` is safe for the “another tick already unlocked” case; the top mutex subsumes that. It is not completely free: the old `_RC` was a later sample, after the locked-branch prechecks. With the new single probe, an owner manual-unlock between `_LRC=0` and `HANDOFF_UNLOCK_CMD` can still make the launcher run the unlock/password-injection command on an already-unlocked desktop unless the unlock CLI internally rechecks/no-ops. I would keep a final pre-unlock recheck or require/prove idempotence in the unlock CLI.

3. Release coverage for explicit post-claim paths looks complete in the live file: claim-race cleanup, both focus-contended continues, common iteration cleanup, and EXIT/HUP/INT/TERM traps all route through `_post_iter_cleanup`. I found no `_URC`/`_VRC` references outside the branch that sets them.

4. Serialization itself is acceptable at 20-30s cadence, and releasing before return-jump is not a clean fix because auto-unlocked paths must relock before release. The fleet-wedge claim is the real problem: live [acquire_unlock_lock](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1312) returns busy immediately for a live PID before checking mtime, so the 180s stale-break does not break a live-but-hung holder. The widened section includes GUI helpers with unbounded `osascript` calls. That turns a common unlocked-spawn hang into a global spawn blocker unless an external watchdog kill is guaranteed and documented.

5. No new obvious `set -u` hazard from `_URC`/`_VRC`; they are branch-local. Comments/tests should be updated for removed `_RC`/`lock-unknown-premutex`.

6. Before GREEN: add the two proposed contention tests, plus a live-old-lock test documenting the actual behavior, and either retain a final pre-unlock recheck or prove the unlock CLI rechecks locked state internally.

Verdict: RED

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 20s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
1. **TOCTOU Elimination:** Yes. By pulling the global mutex acquisition to the very top, *before* the authoritative `screen_is_locked` probe, the entire open/submit/relock sequence is strictly serialized across the fleet. No other dispatcher instance can interleave a relock or overlap UI interactions (piggybacking). The lock-state decision is truly stable and uninvalidatable by other automated processes.
2. **Re-probe Folding:** Folding away the re-probe is completely safe and logically sound. In the old design, the first probe was outside the mutex, necessitating a second probe inside it to catch concurrent changes. With the single probe now *inside* the mutex, it inherently captures any external state changes (like manual user unlock or a racing dispatcher that just finished) that occurred while waiting to acquire the mutex. The old `_RC=1` bypass is naturally handled by `_LRC=1`.
3. **Release Coverage:** Complete. All newly added `continue` paths in the gating logic explicitly call `_post_iter_cleanup`, which correctly releases the lock. The existing post-claim logic already safely routed to `_post_iter_cleanup` for the locked branch; widening the mutex hold to the unlocked branch safely reuses this identical cleanup path.
4. **Over-serialization / Wedge Risk:** This strict serialization is highly desirable, not a risk. The macOS UI (focus, keyboard input) is fundamentally a singleton resource. Holding the mutex across the entire `open` → `submit` sequence for *every* spawn prevents concurrent projects from launching their IDEs simultaneously and fighting for input focus. If one project takes 30s for a cold start, other projects deferring with `spawn-busy` is the correct, safe behavior. The 180s `mtime` break mechanism is sufficient to prevent permanent wedges from hard crashes.
5. **Hazards:** No `set -u` hazards introduced. `$_URC` and `$_VRC` are strictly scoped and consumed within the `_LRC=0` block. The state variables (`UNLOCK_LOCK_HELD`, etc.) are safely initialized before the block.

Verdict: GREEN

[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
