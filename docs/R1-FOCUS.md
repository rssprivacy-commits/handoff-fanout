# R1 审计焦点 — Per-Session Worktree Isolation 实施设计

你审计的是**实施设计稿**（`design-per-session-worktree-isolation-2026-06-03.md`，下方给出），不是已写的代码（代码尚未实现）。这是一个 handoff-fanout 自动接续引擎的架构级改造：把"多会话 spawn 进同一 git 工作树"改成"每会话独立 git worktree"，根治多会话裸 `git stash`/`reset --hard` 抢共享树卷走/销毁并发 WIP 的事故。

## 背景事实（引擎现状 / 下方给出 dump.py + config.py + prune.py + launcher 源码）
- `dump.py main()` 算出 `workspace`（cwd 或 --workspace），threaded 进 `queue/<task>.uri`（`WORKSPACE=<path>`）、handoff `.md`（`cd {workspace}`）、`.queued` ack。
- launcher `install/auto-continue.sh` 解析 `.uri` 的 `WORKSPACE=`，`code -r "$WORKSPACE"`（仅当 `[ -d "$WORKSPACE" ]`）激活窗口 + open URI spawn 新 Claude tab。
- 现 serial 接续：每会话 commit 到 `main` + push；launchd 一次只 spawn 一棒。
- 已有 opt-in 范式：v4 autoclose（env/sentinel/per-project）、`mandate_projects`（per-project fail-closed list）。
- 引擎是 editable 装进各项目 venv；deps 纯 stdlib。

## 请重点审这些（按 P0/P1/P2 分级，每条给 文件/章节 + 为什么是问题 + 修复建议）

1. **merge-back 模型（§2.E）是否 sound**：v1 推荐"worktree 会话 closure 时 `git push origin HEAD:<default_branch>` ff-publish 到集成分支，main 工作树退化为被动 ref holder"。
   - 这是否真能让 serial 接续"每棒 build on 前一棒"不破？
   - ff-publish 的并发竞态（两 worktree 同时 push HEAD:main）、non-ff rebase 重试是否考虑周全？
   - 本地 main 工作树 ref 滞后 origin 的后果（owner 在 main 树手动编辑 / 下一棒 fetch 基点）？
   - 有没有更简单且同样 sound 的 merge-back？我列的 3 个 alternative 是否漏了更优解？

2. **base commit 解析（§2.C）**：`fetch origin <default_branch>` → base=`origin/<default_branch>` else 本地 HEAD。default_branch 解析链（symbolic-ref → abbrev-ref → main）有无 footgun（detached HEAD、无 origin、origin/HEAD 未设）？

3. **worktree 生命周期回收（§2.F / §2.B 碰撞）**：terminal `git worktree remove`、orphan `git worktree prune`、retry 同 task 碰撞、dirty/unmerged worktree "保留现场不销毁" —— 有无泄漏 / 误删未合并 WIP / 把本该根治的红线（销毁他人 WIP）又引回来的路径？

4. **graceful degrade（§2.G）+ 默认 OFF**：fallback 回共享树是否真不 brick dump？degrade 路径与现有行为是否 byte-identical？report-only 子模式语义是否清晰？

5. **是否破坏现有不变量**：worktree 化是否破坏现有 dump/spawn/接续/autoclose/mandate gate/retro-evidence/old_ready 写序/preflight gate？workspace 被替换后，`detect_baseline`（在 main 树还是 worktree 跑？）、retro precheck（git HEAD 取自哪）、`.queued`/`old_ready` 的 workspace 字段语义是否一致？

6. **venv 共享 symlink（§2.D）**：`<worktree>/.venv -> <main>/.venv` 的并发 `uv sync` 风险是否被诚实标注？是否有更优解？editable 引擎本身在 worktree 会话下解析是否正常？

7. **诚实边界（§1 OUT）**：batch fan-out 押后、Docker DB 串扰、alembic 迁移链 fork —— 边界声明是否准确，有没有"以为解了其实没解"的过度承诺？

8. **范围/防膨胀**：作为 default-OFF / opt-in / report-only-first 的 v1，scope 是否合理？有没有该纳入的阻断性缺口被错误押后，或不该纳入的扩展？

只报真问题，不凑数。无问题的维度明说"未发现问题"。
