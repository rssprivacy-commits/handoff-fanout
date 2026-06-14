🆔sw-coord-p24 — handoff-fanout 链中枢接棒（mp-locate-return：live E2E 揭露 P1，待修+重审+合并）

开张第一句回显 🆔sw-coord-p24。你是 handoff-fanout 监管链中枢。先 Read 监管立法 `~/.claude/projects/-Users-chenmingzhong/memory/feedback-supervisor-center-duty.md` + falsify 方法论 `~/.claude/projects/-Users-chenmingzhong/memory/feedback-falsify-claims-evidence-hierarchy.md`。

## 前任（p23）做了什么 / 你接什么
p23 零信任复审 `mp-locate-return`（派窗两程一步：去程一步落中枢桌面 + 回程一步切回 owner 原桌面）+ Fixer `mp-lr-dispatch-fix`。

**结论：去程链路 GREEN，回程 live 行为层有 P1（一步回程对常见场景名存实亡）。NOT mergeable for 回程 one-step claim until fixed。**

🔴 **必读**：`project-files/handoff/2026-06-14-mp-locate-return-LIVE-E2E-P1.md`（完整实证 + 根因 + 3 个修法候选）。一句话：`_unique_title_workspace` 配 `uri_match_keys` 泛键 `'handoff'`/`'claude-handoff'` → 任何标题含 "handoff" 的前台窗（= 所有中枢窗 + owner 真实回程目标）匹配全部 ~10 个 `.claude-handoff/` workspace → 永远 ≥2 → None → 回程退 goto = 逐格。owner live 复核「回程仍逐格」即此。code-review+双脑+测试全漏（没拿真实 opened_windows_uris 跑）。

## 当前 live / 分支状态（已实地核）
- live 已**回滚**到部署前 known-good（hf main `0ad1d00` 配套的旧 goto 回程）：`~/Projects/dharmaxis/.../vscode-spaces.py` sha `9a070cff…`、`~/.local/bin/auto-continue.sh` sha `de22b836…`。备份 `~/.claude-handoff/handoff-fanout/_mplr_e2e_backup/`。**无未合并 drift 留 live。**
- 分支码未动、未合并：dharmaxis worktree `dc5bc66`（`~/.claude-handoff/_xrepo-wt/dharmaxis-mp-locate-return`）、hf worktree `8f6ba4c`（`~/.claude-handoff/handoff-fanout/worktrees/mp-locate-return`）。
- 测试：hf 1645 passed/3 skip/0 fail、dharmaxis 20 passed（都未覆盖本 P1）。
- runbook 已更新（MEMORY.md「派 worker」+ skill-cross-project-spawn：singlepane 中枢传 `--self-task`）。
- 去程 SPAWNER_FOCUS Tier-1/2 + `--self-task` 透传 = 真 GREEN（引擎正确、fail-open）。

## 你的下一步（建议，owner 裁）
1. **派 Fixer 修 P1**：选修法（候选 2「锚匹配换精确 window→workspace 信号，不动共享 uri_match_keys」最干净，但需查 winlist 能否拿窗口精确 ws；候选 1「剔泛键」要回归 save/restore）。先双脑审修法方向再派。Fixer 在既有两 worktree 续 WIP（§6b 红线：禁自合/自审/自派/写共享 memory/push）。
2. **修完零信任复审**：铁线是**拿真实 opened_windows_uris 跑 read-only precapture**（`vscode-spaces.py spawn-precapture` 必须真吐 `RETURN_ANCHOR_WS`），别只读 diff/信双脑/信测试（本 P1 就是这样漏的）。
3. **部署 + owner-eye E2E**：dharmaxis 原语先→hf 编排后背靠背（新 spawn-return 无 `--max-wait`、有 `--anchor-ws/-app/-token`，半部署态 argparse 报错）。owner 盯真实派窗回程一步（日志 RETURN-REACTIVATE-WS=一步 / RETURN-GOTO-FALLBACK=逐格）。
4. GREEN 才合并（hf+dharmaxis 分支过 audit 机器闸）→ GC carrier worktree `mp-lr-dispatch-fix` → 写 p23/p24 lesson。

## §6b 红线（派 Fixer 必含）
worker 只报告抛坑、禁自合/自审/自派/写共享 MEMORY.md|open-loops|skill/push origin；干完 `touch ~/.claude-handoff/handoff-fanout/ack/<task>.worker_reported` 静默等审。owner 脏 cc-global 树绝不碰。codex 恢复前不碰（外脑只用 gemini+deepseek）。
