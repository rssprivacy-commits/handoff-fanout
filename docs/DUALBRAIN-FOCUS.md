# 双外脑审计焦点 — Per-Session Worktree Isolation（已 LIVE / erp-system = ON）

审计对象 = handoff-fanout 引擎的 per-session git worktree 隔离特性，**现已 merge 到 main + 对 erp-system 全量 ON（真建 worktree）**。已过 R1-R5 五轮双脑（设计+实施）+ 几个 single-brain codex sanity，但**以下后期改动只过 single-brain，需本轮双脑补**：① report-only 的 per-project `worktree.report` sentinel；② R-ON 真机测揪的 GC 泄漏修（`is_dirty(ignore=...)` 折扣 engine-linked 文件）；③ 把折扣收紧为「只 untracked」。**这是 LIVE feature**，请按「真会跑生产」严度审。

## 关键文件（仓内，main HEAD = c651199）
- `src/handoff_fanout/worktree.py`（全 / 核心：resolve_mode + report sentinel / resolve_integration_branch / create_worktree merge-back gate / is_dirty link-ignore / classify / remove / gc / find_reclaimable）
- `src/handoff_fanout/dump.py`（resolve_spawn_workspace / main() source-spawn 分离 / write_active_dump / _write_old_ready / .worktree sidecar）
- `src/handoff_fanout/templates.py`（worktree banner + merge-back 协议 + `--project/--workspace` 注入）
- `src/handoff_fanout/config.py`（worktree_* 字段）
- `docs/design-per-session-worktree-isolation-2026-06-03.md`（§8 R1 / §9 R2-R4 / §10 runbook / §11 R-ON）
- diff: `git diff 21b9639 c651199 -- src/`

## 请重点审（P0 阻断/数据丢失/破坏现有 relay / P1 / P2）

1. **LIVE ON 安全性 — merge-back gate**：worktree 会话 closure 必须 ff-publish `HEAD:<int>` 到 origin，引擎 dump 时校验 `origin/<int>` 含 source HEAD 否则 BLOCK。生产里：会话没按 banner publish 会怎样？BLOCK 后能恢复吗？有没有让 relay 死锁/丢工作的路径？源 worktree dirty（ERP hook 噪声每棒都有）走 WARN —— 这正确吗，会不会漏报真该 block 的情况？

2. **R-ON GC 泄漏修 + 折扣 untracked（最需补双脑）**：`is_dirty(workspace, ignore)` 只折扣 `??` untracked 且名字 ∈ engine-linked。**红线**：有没有任何路径让真 WIP（tracked 改/未 link 的 untracked）被当 clean → worktree/branch 被 remove/gc 销毁？porcelain 解析（code 取 `line[:2]`、path 取 `line[3:]`、`first` 取 first path component、引号剥离）在重命名/特殊文件名/子路径下稳吗？`_link_names(cfg)` 来源可信吗（config 可被改）？

3. **report sentinel 优先级**：`worktree.report` sentinel 插在 enabled(on) 之后、worktree_projects 之前。有没有让本该 ON 的项目被降级 report，或本该 off 的被误开 report 的路径？全局 vs per-project sentinel 交互？

4. **source/spawn 分离不变量**：old_ready 锚 source、baseline/.uri/.worktree 指 spawn、`--project` 注入 —— 在 ON 下真没串味吗？mandate gate / retro-evidence / preflight 跑在 source 对吗？

5. **并发**：多 erp-system 会话同时 ON dump（不同 task）→ 各自 worktree（OK）；但同 task 并发、`git fetch`/`worktree add`/`worktrees_root mkdir`/`refs/remotes/origin/<int>` tracking 更新的竞争？gc 与活跃会话竞争（heartbeat liveness 判定可靠吗）？

6. **诚实边界**：`.codegraph`/`.gitnexus` 不进 worktree（grep fallback）/ Docker DB 共享 / alembic fork / `refs/stash` repo-global / batch 仍共享树 —— 声明是否准确，有没有「以为解了其实没解」或新引入的过度承诺？

7. **是否破坏现有**：default-OFF 项目（dharmaxis 等）真 byte-identical 吗？现有 dump/spawn/接续/autoclose/mandate/audit gate 有无被 worktree 代码污染？

只报真问题，分 P0/P1/P2，每条 文件:行/章节 + 为什么 + 修复。无问题维度明说。这是生产 LIVE 特性，请严。
