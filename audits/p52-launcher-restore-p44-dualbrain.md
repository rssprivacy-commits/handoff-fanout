# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 125s / 用 1 次
- gemini: `ok` / gemini-3-pro-preview(api) / 27s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 125s_


我基于嵌入的 raw diffs 独立判断；没有联网，也没有把摘要结论当事实。

**1. Reconstruction Fidelity**

PASS, with one naming caveat.

DELTA-A is p44 mutex restoration: adds `probe_lock_bounded`, bounded notification wrapper, acquire-before-probe, bounded under-mutex probes, locked-path cleanup/release, and already-unlocked-path early release. It does not touch the P0 focus identity logic.

DELTA-B is the P0 live dispatch package: adds `_title_has_token`, changes task substring matching to whole-token identity, routes retry/wait/raise/discriminator through `_focus_token`, and fail-closes on non-positive identity. It also includes the `HANDOFF_PRESS_NOW` wrapper. If “P0 fix” is defined narrowly as only positive identity, that wrapper is an additional behavioral delta versus HEAD; but in the supplied DELTA-B it is part of the current live dispatch package and is explicitly what makes the pre-URI identity gate mandatory.

**2. Interaction Risk**

No adverse interaction shown.

p44 controls lock-state atomicity and mutex lifetime. P0 controls window identity before URI dispatch. They do not share variables except the broader execution schedule. Restoring p44 can serialize locked-path spawns, but the already-unlocked path still releases the mutex before normal spawn, so P0’s positive identity discriminator remains necessary and unchanged for parallel unlocked spawns.

P0 does not mutate `UNLOCK_LOCK_HELD`, `UNLOCKED_BY_US`, `MAY_NEED_RELOCK`, lock probes, or relock behavior. p44 does not mutate `_focus_token`, `_title_has_token`, snapshot checks, raise/wait logic, or dispatch criteria.

**3. Direction**

Restore p44 and keep the P0 fix.

The orphan revert should not stand based on these diffs. I see no concrete p44 defect being fixed by the revert. The reverted p44 code is specifically addressing a real race: another tick can observe an auto-unlocked desktop outside the mutex and spawn into a window that may be relocked under it, or piggyback without `unlock.enabled`. p44’s acquire-before-probe is the correct control point.

The p44 design also handles the obvious wedge concern: probes under the mutex are bounded, notifications are bounded, stale dead holders are breakable, and already-unlocked normal spawns release the mutex before GUI work.

**4. Residual Fleet Risk**

After reconstruction, live should regain both protections:

- p44 race protection: mutex acquired before authoritative lock probe, bounded lock probes under mutex, locked-path mutex held through unlock→spawn→relock, cleanup release, already-unlocked path released early.
- P0 dispatch protection: URI dispatch only when the front Code window positively carries this spawn’s `_focus_token` as a whole token; markerless, foreign, stale-nonce, and substring windows fail closed.

No new fleet-wedge or mis-dispatch path is introduced by combining these two deltas.

Residual caveat: `HANDOFF_PRESS_NOW=1` still means that after a positive URI dispatch, Enter is pressed without a later front-window check. That is a known live/P0-package residual risk, not a new p44 interaction. If the delivery bar requires post-URI focus proof before Enter, that is a separate RED against press-now policy, not against “restore p44 + keep P0 identity dispatch.”

Verdict: GREEN

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 27s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
Here is the independent audit of the proposed reconstruction (`e98fa1c4`) for the `install/auto-continue.sh` fleet launcher.

### 1. Reconstruction Fidelity
**Yes, the fidelity is absolute.**
*   **DELTA-A** exclusively restores the p44 critical section. It correctly reinstates the bounded wrappers (`probe_lock_bounded`, `notify_async`), moves the `acquire_unlock_lock` call *before* the authoritative `screen_is_locked` probe, and correctly drops the mutex on the already-unlocked path to maintain parallel execution. No extraneous lines were introduced.
*   **DELTA-B** exclusively applies the P0 positive-identity dispatch fix to HEAD. It integrates `_title_has_token` whole-kebab-token matching, upgrades the `wait/raise` target checks, enforces the fail-closed `FOCUS-DISCRIMINATOR`, and preserves the owner's `press-now` mandate. 
The reconstruction is a mechanically clean UNION of the two distinct logic blocks.

### 2. Interaction Risk
**None. The changes are regionally disjoint and functionally symbiotic.**
*   **Execution separation:** The p44 lock-probe and mutex logic (lines 1640–1740) executes and completes its state resolution before the script reaches the P0 post-spawn focus/dispatch phase (lines 1850+). They do not share mutable state.
*   **Symbiosis:** p44's design explicitly releases the global mutex for already-unlocked desktops so that parallel spawns don't bottleneck the fleet. This deliberate concurrency is *exactly* the environment that triggers cross-window mis-dispatches without P0. Therefore, p44 creates the safe concurrency, and P0 securely arbitrates it. Restoring p44 makes P0's positive-identity check mandatory, and P0 ensures p44's parallelism is safe.

### 3. Direction
**Restoring p44 + keeping P0 is the only correct and safe end-state.**
*   The orphan revert in LIVE exhibits the classic signature of a git-staging clobber during parallel coordination sessions, not a deliberate rollback. 
*   Leaving p44 reverted carries unacceptable long-term costs and security risks: it leaves the fleet vulnerable to the unlock race (piggybacking unlocked windows) and unbounded lock probes (a hung backend could hold the mutex and wedge the entire 7-project fleet). 
*   There is zero evidence in the diffs or historical context to justify the orphan revert. It must be discarded.

### 4. Residual Fleet Risk
**Mitigated safely.** 
By deploying this reconstruction, the launcher achieves a robust, fail-closed security posture on both critical axes:
*   **Concurrency/Wedge Risk (p44):** The mutex is secured *before* the probe, completely eliminating the unlock race. The fleet is protected from deadlocks because all under-mutex probes are now rigidly bounded by `probe_lock_bounded`.
*   **Cross-Pollination Risk (P0):** Spawns into foreign or markerless windows are eradicated. The `FOCUS-DISCRIMINATOR` strictly requires positive cryptographic-like identity matching (`_focus_token`), failing closed gracefully (deferring) until the exact intended window is confirmed.

Verdict: GREEN

[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
