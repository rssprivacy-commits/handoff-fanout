# mp-locate-return — LIVE E2E 揭露的 P1（uri_match_keys 泛键 → 回程永远退 goto）

**发现者**：sw-coord-p23（中枢）/ 2026-06-14 / owner live 复核「回程仍逐格」触发
**状态**：交付物代码层 GREEN，但 **live 行为层 P1 — 一步回程对常见场景名存实亡**。NOT mergeable for the one-step 回程 claim until fixed。

## 一句话
`_unique_title_workspace`（Fixer dc5bc66 加的唯一性闸）配 `uri_match_keys` 的**泛路径键**（`'handoff'`/`'claude-handoff'`），导致**任何标题含 "handoff" 的前台窗**（= 所有中枢/handoff 风格窗，含 owner 真实回程场景）substring-匹配到**全部 ~10 个 `.claude-handoff/` 下的 workspace** → 永远 ≥2 → 返回 None → 回程退 `RETURN-GOTO-FALLBACK` = **逐格**。owner 看到的就是这个。

## 实证（read-only precapture，零桌面移动）
部署 worktree 版到 live 后跑 `vscode-spaces.py spawn-precapture`：
- frontmost = `Code`，title = `🧭中枢·handoff-fanout · sw-coord-p23 · supervisor_succession · 80f378e0… [singlepane] — 自动接续 handoff-fanout 项目`
- 输出 `ORIGIN=7` + `BEFORE=…`，**无 `RETURN_ANCHOR_WS`、无 `RETURN_ANCHOR_APP`**
- `opened_windows_uris()` = 19 个，其中 ~14 个在 `.claude-handoff/` 下
- `_unique_title_workspace(title, uris)` → **10 matches**（全靠泛键 `'handoff'`/`'claude-handoff'` 命中标题里的 "handoff"）→ `result: None`
- 故 `_capture_return_anchor` 返回 `("","")` → 不输出锚 → 回程必走 goto。

## 根因（精确）
`uri_match_keys(<path>)` 从路径组件派生键，对 `/Users/.../.claude-handoff/<proj>/singlepane/<task>.handoff.code-workspace` 会吐出**泛键 `'handoff'` 和 `'claude-handoff'`**。因为**所有**中枢/worktree workspace 都在 `.claude-handoff/` 下、且**所有**中枢窗标题都含 "handoff"（项目名/路径），→ 泛键让它们互相全匹配 → 唯一性闸**永远**不可满足（对 handoff 风格锚）。
- 对**普通项目窗**（如 frontmost=`rakeforge — file.ts`）可能仍唯一命中 → 一步 OK；但 owner 的真实回程目标常是 handoff/中枢窗 → 必 None → 逐格。

## 为什么 code-review + 双脑 + 测试全漏
- diff 审 + gemini/deepseek 都说「ambiguous→None→goto 是正确 fail-open」——**孤立看对**，但**没人拿真实 `opened_windows_uris`（满是 .claude-handoff 路径）跑一遍**。
- 测试 conftest `neutralize_spawner_self_report` + 构造的少量 uri，从不暴露「泛键让全集合互撞」。
- **= falsify-claims 教训铁证**：行为断言地面真相是「跑」不是「读」；读 diff 说「唯一性闸对」= 假设，跑真数据 = None。§6 owner-eye / read-only precapture 闸正是为此存在。

## 修法候选（交下个 Fixer，需 owner/中枢裁）
1. **`uri_match_keys` 不吐泛键**：剔除 `'handoff'`/`'claude-handoff'`（及其它 >N 路径共享的通用组件），只留可区分的项目名/task-id/basename。⚠️ 影响 save/restore 共享调用方——需回归（best-match 路径不能退化）。
2. **锚匹配换更强信号**：回程锚不用标题 substring，改用**窗口对应的精确 workspace**（如 window→workspace 直接映射 / task-id token 精确匹配），绕开泛键歧义。
3. 评估：方案 2 更干净（不动共享 `uri_match_keys`/save-restore），但需查 winlist 是否能拿到窗口的精确 ws。

## live 状态
- **已回滚**到部署前 known-good：`vscode-spaces.py` sha `9a070cff…`、`auto-continue.sh` sha `de22b836…`（OLD goto 版，`_spawn_return_logic(origin,before,max_wait)`，RETURN-REACTIVATE-WS=0）。备份在 `~/.claude-handoff/handoff-fanout/_mplr_e2e_backup/`。
- worktree 分支码未动（dharmaxis `dc5bc66` / hf `8f6ba4c`），未合并。
- 部署顺序教训复用：dharmaxis 原语先、hf 编排后；新 `spawn-return` 去掉 `--max-wait`、加 `--anchor-ws/-app/-token` → 半部署态会 argparse 报错 → 必须两文件背靠背部署。

## 交付物其余状态（这些是真 GREEN）
- 去程 SPAWNER_FOCUS Tier-1/2 + `--self-task` 透传（Fixer dc5bc66）：引擎层正确、fail-open、测试覆盖（hf 1645 passed / dharmaxis 20 passed）。
- 双脑（gemini+deepseek）对 Fixer diff 均 GREEN（但同样没跑真数据，故漏了本 P1）。
- runbook 已更新（MEMORY.md「派 worker」+ skill-cross-project-spawn：singlepane 中枢传 `--self-task`）。
- 原 7 RED（gemini/deepseek 首轮）：title-race P0 三路证伪、osascript exit0 兜底、V3 precapture 是设计意图——均已消化。**本 P1 是 live 新发现，不在那 7 条内。**
