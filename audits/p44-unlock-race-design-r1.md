# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 222s / 用 1 次
- gemini: `ok` / gemini-3-pro-preview(api) / 63s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 222s_


I verified the live file. The minimum design is not fully sound.

**Findings**

1. The guard is advisory, not a critical section. A run can pass `unlock_lock_active` while unlocked, then the screen can lock, another tick can acquire the unlock mutex, auto-unlock, and later re-lock while the first run is still in its open/Enter path. That recreates the relock-under-submit race and lets a non-`unlock.enabled` project use someone else’s later auto-unlock.

2. A holder releasing between `_LRC=1` and the new guard is mostly fail-closed for cold/warm because the later screen check can defer as `re-locked-before-submit`, but singlepane skips that screen check, so it can still degrade into an opened/failed/manual state rather than clean defer.

3. `unlock_lock_active` mostly mirrors `acquire_unlock_lock`, but not exactly: it treats `pid == $$` as inactive while `acquire_unlock_lock` would treat it as busy. That is benign at the proposed insertion point because `UNLOCK_LOCK_HELD=1` short-circuits, but the “EXACTLY” claim is overstated.

4. Both helpers have the same stale-live-PID weakness: if a stale lock’s PID is reused by an unrelated long-lived process, or a holder is wedged but still alive, the fleet can defer forever. This patch newly applies that wedge to normal unlocked spawns too.

5. The bare `continue` is correct at the proposed insertion point: no caffeinate, no held lock, no unlock/relock state, and no return-jump state has been acquired yet.

6. I do not see another real task spawn/open/Enter path bypassing the insertion point in the main loop. The later autoclose helper opens a VS Code URI outside this path, but it is not a spawn/Enter flow.

**Answer**

Defer-if-mutex-held closes the already-active-holder case, including the original “second tick sees unlocked while first tick holds the unlock mutex” race. It does not close the broader correctness condition “no auto-unlocker can appear and relock during another GUI spawn.” For that, the launcher needs a real global GUI/unlock critical section, acquired before the authoritative lock-state decision and held through open/submit/cleanup, or an equivalent separate exclusion that auto-unlock honors.

Verdict: RED

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 63s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
1. **Does this fully close BOTH defects?**
   No. 存在一个经典的 TOCTOU（Time-of-Check to Time-of-Use）残余竞争窗口。假设 Run 2 在循环顶部检测到屏幕已解锁（因为 Run 1 已经解锁并持有锁）。Run 2 于是跳过 LOCKED 分支，来到新的 guard 插入点。如果在这极短的间隙内，Run 1 刚好完成任务，执行了 `do_relock`（将屏幕重新锁定）并释放了 `GLOBAL_UNLOCK_LOCK`，那么 Run 2 执行 `unlock_lock_active` 时会得到 "false"（锁未激活）。于是 Run 2 越过防线，执行 atomic claim 并尝试 spawn，但此时屏幕**已经被 Run 1 重新锁定**，导致直接在锁屏界面触发击键/拉起，发生 mis-fire。这未能完全闭环可靠性目标（徒增长期的隐性维护成本）。

2. **Is `unlock_lock_active`'s held/stale boundary truly identical to `acquire_unlock_lock`'s?**
   Yes. 边界逻辑（180秒超时、PID 存活检测）完全镜像了 `acquire_unlock_lock`，且通过 `[ "$pid" = "$$" ]` 正确排除了自身。没有发现 pid 复用、mtime 获取或 `set -u` 导致的崩溃陷阱。从生态与长期成本视角来看，将复杂且底层的锁状态嗅探逻辑 copy-paste 两遍会增加技术债务，但单纯从本次逻辑正确性上是无误的。

3. **Is the bare `continue` (no `_post_iter_cleanup`) correct at the insertion point?**
   Yes. 在 fall-through 路径上，四个核心状态变量（`UNLOCKED_BY_US`, `MAY_NEED_RELOCK`, `CAFF_PID`, `UNLOCK_LOCK_HELD`）在每次迭代顶部已经被强制置零，也没有涉及敏感凭证的临时接触。因此直接调用 `continue` 相当于 no-op 的安全跳过，不会产生句柄泄漏或锁残留（安全与凭证接触面无负面影响）。

4. **Is "defer-if-mutex-held" the right scope?**
   是的，这是一个合理的权衡。如果选择方案 a（全局串行化所有的 spawn），会造成巨大的**机会成本**——在无需解锁（用户常态使用）的环境中强行排队，严重降低多项目的并行吞吐量，且可能引发组织内对于无端性能损耗的采用阻力。"defer-if-mutex-held" 将临界区限定在需要干预锁屏的高危操作上。不过也需明确，放弃全局临界区意味着不同项目的纯 unlocked spawn 之间仍可能发生 UI 焦点的争抢（生态固疾）。

5. **Any regression to existing behaviors? Could a crashed/stale holder wedge the fleet?**
   No. 由于保留了与原获取逻辑完全相同的 mtime（180秒）失效兜底机制，崩溃或挂起的解锁进程不会永久卡死 fleet。系统具有自愈能力，不会引发需要人工界入清理 `.unlock.lock` 目录的严重事故。

6. **Anything else fleet-critical that must change before this is GREEN.**
   必须消除 Q1 提到的 TOCTOU 漏洞。在 guard 越过之后（即确认没有其他进程正在持有解锁锁）、且执行 atomic claim 之前，必须**重新校验一次屏幕锁定状态**（再调一次 `screen_is_locked`）。如果在跳过锁等待后发现屏幕变成了锁定状态，说明发生了并发 relock，此时应当放弃或 defer，而非盲目将载荷击发到已被锁定的屏幕上。

Verdict: RED

[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
