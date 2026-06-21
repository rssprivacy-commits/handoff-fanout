# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 189s / 用 1 次
- gemini: `ok` / gemini-3-pro-preview(api) / 43s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 189s_


**Finding**

RED: the new identity check is still fail-open for same-task, wrong-nonce windows.

In [install/auto-continue.sh](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1829), [line 1831](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1831), and [line 1935](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1935), the code accepts `focus_token OR TASK`. For singlepane with `SINGLEPANE_NONCE` present, `$TASK` is not a per-spawn identity. A stale/sibling window titled like:

`project · task · worker · oldnonce [singlepane]`

whole-token-matches `$TASK` but not the current nonce. It can pass the retry fast path as `_front_verified=1`, skipping `code -n`, waits, and the discriminator. The happy path also only checks `$TASK` in [target_window_frontmost](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:417). With live `HANDOFF_PRESS_NOW=1`, that becomes URI dispatch plus Enter×3 into the stale/same-task window.

This is not hypothetical against the code’s own model: the submit-path comments say nonce is what proves “THIS is the window we launched” and that stale/sibling windows may carry the task but not the nonce ([install/auto-continue.sh](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:2140)).

Other checks look sound: empty probe output does not become positive identity; `_title_has_token` closes the superstring bug; newline-joined snapshots do not create blob-spanning matches for normal task/nonce tokens; tokenless titles fail closed and are bounded by the focus counter.

Must-fix: when `SINGLEPANE_NONCE` exists, dispatch/fast-path identity must require the nonce, not `nonce OR task`. If early singlepane titles need pre-settings matching, put the nonce into the workspace/display basename or fail closed until the nonce-bearing title renders.

Verdict: RED — must fix singlepane same-task wrong-nonce false-positive dispatch.

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 43s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
**审计视角：生态依赖、长期成本、安全接触面与系统隔离**

1. **安全与凭证接触面 (Security & Credential Surface) —— 极大的防御提升**
   之前的“基于快照排除（∉ snapshot）”是一种典型的**启发式状态推断**，在并发调度时极易发生状态穿透。当系统将高权限指令（如携带凭证或敏感代码的 prompt）自动提交（Auto-submit）到错误的 VS Code 窗口（如 `erp-dev` 串线到 `fateforge`）时，这不仅仅是功能 Bug，更是跨租户/跨上下文的严重信息泄漏（P0级安全事故）。本次改用**基于 Token 的正向身份断言（Positive-identity）**，将默认策略从“无法证明是别人，就当成自己”扭转为“无法证明是自己，则立刻阻断（Fail-closed）”。这种“无信任则拒绝”的零信任边界，从根本上消除了跨窗口执行任意指令的安全风险。

2. **长期维护成本与系统隔离 (Long-term Costs & Brittleness)**
   抛弃前置快照 (`$_snap_wins`) 参与最终的派发决策，是极佳的架构减负。快照机制存在天然的 TOCTOU (Time-of-check to time-of-use) 竞态缺陷。依赖高频、异构并发的 OS GUI 状态来维护一个“一致性快照”是不切实际的。正向身份核验解耦了并发任务之间的状态纠缠，每个派发链路仅需关注“当前窗口是否携带我的 Token”，使得系统具备了更好的无状态性（Statelessness），极大降低了后续排查幽灵 Bug（Heisenbugs）的运维成本。

3. **正则表达式与边界健壮性 (Regex & Boundary Assertions)**
   `_title_has_token` 的实现是严密的：
   - **空值防御**：`[ -n "$2" ] || return 1` 确保了如果 Token 尚未生成或意外为空，永远安全阻断，不会匹配任意窗口。
   - **边界界定**：使用 `(^|[^A-Za-z0-9_-])` 作为词边界，完美涵盖了 Singlepane 场景下 `.handoff (Workspace)` 中的 `.`，以及传统场景下的空格和路径分隔符。
   - **转义安全**：`sed 's/[^A-Za-z0-9_-]/\\&/g'` 将所有非标准字符转义，防御了潜在的正则注入。
   - **Blob 遍历**：对于 `$_snap_wins` 这样换行符拼接的文本，`grep` 按行处理或将换行符视为 `[^A-Za-z0-9_-]` 处理都完全满足 per-window 边界，不会发生跨行合并的误判。

4. **生态依赖与锁定 (Ecological Lock-in & Adoption Friction)**
   将身份核验的核心逻辑从晦涩、极度平台绑定的 AppleScript 中剥离，下沉到标准的 POSIX bash 管道（`grep -E`, `sed`）。这不仅避免了 AppleScript 在处理字符串边界和 Unicode 时的怪异行为，还极大降低了未来向 Linux (Wayland/X11) 或 Windows 生态迁移的阻力（只需替换 `probe_code_windows` 的外壳，即可复用纯文本的身份核验逻辑）。保留在 AppleScript 中的仅剩击键提交，作为无状态动作，其锁定风险已降至最低。

5. **对 Livelock 攻击面的回应**
   是否存在自己真实的窗口永远无法命中全词匹配的情况？VS Code 的窗口标题在生命周期最早期，其本质就是 Worktree 的目录名（即 Task ID）或 Workspace 的文件名（包含 Task ID）。只要窗口存在，其基础骨架必然含有该 Token。就算遇到极端自定义配置或插件覆盖标题，回退机制是超时并要求人工 `Enter`（Bounded Retry）。在“多按一次回车”与“自动向生产环境窗口注入错误指令”之间，前者的摩擦成本在组织层面是绝对可以接受的（Fail-closed is cheap, fail-open is catastrophic）。

Verdict: GREEN
[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
