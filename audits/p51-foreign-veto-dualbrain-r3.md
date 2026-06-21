# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 266s / 用 3 次
- gemini: `ok` / gemini-3-pro-preview(api) / 42s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 266s_


**Finding**

Must-fix in the audited bytes: the R3 behavior is safe, but the explanatory invariant is now false at [install/auto-continue.sh:1933](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1933). It still says a real singlepane pre-settings title `"<task>.handoff (Workspace)"` is matched by the upstream happy-path wait. That is no longer true for nonce-bearing singlepane, because [line 1878](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1878) waits on `$_focus_token`, and [lines 1781-1783](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1781) set that to `SINGLEPANE_NONCE`. So R3 intentionally fail-closes until the nonce-bearing `window.title` renders. The comment must be corrected or it will preserve the exact wrong mental model R3 is trying to remove.

**Behavior Check**

The implementation itself keys the focus path on `$_focus_token` only: retry snapshot/front check, happy wait, raise, re-wait, and discriminator all use the focus token, with no remaining `$TASK` OR fallback in that path. Legacy singlepane without a nonce still falls back to `$TASK`, by design because no per-spawn identity exists.

The bounded retry claim holds: fail-closed increments `queue/$TASK.focus_contended`, defaults `HANDOFF_FOCUS_DEFER_MAX` to `5`, then consumes the URI, writes a failed ack, and notifies. No livelock. If the nonce title never renders, the launcher may rebuild/park windows up to the cap, but it does not dispatch URI or Enter into an unproven window.

`_title_has_token` holds the R2 properties: empty token fails, `task-1` does not match `task-10`, `·` and line boundaries work as delimiters, and probe failure leaves `_d_app` empty so the discriminator fail-closes. I also verified `zsh -n install/auto-continue.sh` passes.

One nuance: with `HANDOFF_PRESS_NOW=1`, the post-URI submit nonce gate is bypassed, so R3 is not merely “moving the same dependency earlier” in the active default path. R3 is the required pre-URI safety gate.

Verdict: RED — fix the stale singlepane happy-path comment/invariant to say nonce-bearing singlepane waits/fail-closes until the nonce title renders.

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 42s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
从生态、长期成本及安全的独立审计视角来看，本次 R3 的变更逻辑严密且击中要害。

**审计意见与重点维度的验证：**

1. **安全与凭证接触面 (Security / Attack Surface) - 核心消除跨域注入风险**
   旧有的 `$TASK` 降级匹配是一个巨大的安全隐患。在 singlepane 模式下，旧窗口（携带旧 nonce 但同属 `$TASK`）的残留会导致新任务的 payload（可能包含环境敏感指令）被错误注入并自动提交（Auto-submit）。R3 彻底移除了 `$TASK` 的 OR 妥协，**全链路（Discriminator、Retry Tick、Wait、Raise）收束于具备全局唯一性的 `$_focus_token` (Nonce)**。这种 Positive Identity（正向身份确权）模式从根本上阻断了跨窗口指令漂移的可能。

2. **长期成本 (Long-term Maintenance Cost) - 拆除脆弱的负向启发式逻辑**
   抛弃了极易在并发/高负载时失效的 `∉ snapshot`（不在快照内即视为新窗口）逻辑，转而使用 `_title_has_token`。基于严格的 kebab-case 边界正则 `(^|[^A-Za-z0-9_-])`，杜绝了 `coord-7` 与 `coord-77` 的子串误判。这种强一致性校验显著降低了并发场景下各种 “幽灵 Bug” 的排查与维护成本。

3. **机会成本与组织阻力 (Opportunity Cost & Adoption)**
   严格依赖 `$_focus_token` 意味着在 singlepane 模式下，必须等待 VS Code 插件加载并完成 `window.title` 的 nonce 注入后才能通过所有检查。这牺牲了几秒钟的 “快速启动体验”（如果在慢速机器上，单次 Tick 会打满 `wait`），但换取了 100% 的确定性。由于外层 `focus_contended` 有 bounded retry 机制（最多 5 次），且失败兜底为 Fail-Closed，这种“宁可等待/挂起也绝不盲目发车”的策略，在自动化流程中带来的信任感远大于几秒钟的性能损耗，不会引入组织采用阻力。

4. **生态依赖与 Lock-in (Dependencies & Lock-in)**
   完全依赖于 POSIX 标准的 `grep -Eq` 和 `sed` 实现，正则表达式没有过度使用任何非常规拓展，无外部生态绑定，轻量且健壮。

**代码级验证确认：**
- `target_window_frontmost` 已被收口至 `_title_has_token`。
- `wait_target_window_frontmost` 和 `raise_task_window` 的参数已全部清理为单参数 `$_focus_token`。
- 空 token 降级保护 `[ -n "$2" ] || return 1` 完备，不会出现空正则匹配全集的漏洞。
- Worktree 下 `$_focus_token == $TASK`，行为无损等价保持。

Verdict: GREEN

[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
