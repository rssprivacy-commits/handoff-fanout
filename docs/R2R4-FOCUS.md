# R2–R4 审计焦点 — Per-Session Worktree Isolation 实施物

审计对象 = **已实现的代码**（git diff main，下方/仓内给出）。这是 handoff-fanout 自动接续引擎的架构改造：每个 spawn 会话在独立 git worktree 工作（根治多会话裸 `git stash`/`reset --hard` 抢共享树）。**默认 OFF / opt-in / report-only 子模式**。R1 设计审计已闭环（codex+Gemini 收敛，见 design §8）；本轮审实施物。

## 新增/改动文件
- `src/handoff_fanout/worktree.py`（新）：resolve_mode / resolve_integration_branch / create_worktree（merge-back gate）/ classify / remove / gc / list。
- `src/handoff_fanout/config.py`：worktree_* 字段 + `_parse_worktree`。
- `src/handoff_fanout/dump.py`：`resolve_spawn_workspace` + `main()` source/spawn 分离 + `write_active_dump`(source_workspace/old_head/worktree_info) + `_write_old_ready`(commit_hash) + `.worktree` sidecar。
- `src/handoff_fanout/templates.py`：`_worktree_banner` + `wt_args`(--project/--workspace 注入)。
- `src/handoff_fanout/cli.py`：`worktree` 子命令。

## R2 — 审实施物（实现层 bug / 边界 / 单点逻辑）
1. `create_worktree` 的 merge-back gate：`is_ancestor(source_head, origin/INT)` 判定 + fetch 时序 + 退出语义（BLOCK→dump 返回 2）是否正确无洞？fetch 失败/超时分支？
2. collision 分类（dirty / clean+published / clean+unpublished / 仅 stale branch ref）四象限处理是否完备、有无误删未合并 WIP 的路径？`safe_to_recreate` + remove 的 `--force` 用法是否安全？
3. `resolve_integration_branch` 链（config→origin/HEAD→remote show→origin/main|master→local main|master→None）有无 footgun（detached HEAD、origin/HEAD 未设、symbolic-ref 输出解析、handoff/* 排除）？
4. `dump.main` 集成时序：source/spawn 分离后，retro gate/preflight 跑 source、baseline 跑 spawn、old_ready 锚 source HEAD（old_head 预捕获）—— 有无遗漏的旧不变量（.queued/.uri/notify/pbcopy/老 schema）？
5. `.worktree` sidecar 写序（在 .uri publish 之前）+ block 路径（`_write_worktree_block` 删 .uri + 写 BLOCKED.md）是否与现有 atomic 写序/old_ready 写序一致？

## R3 — 审业务目标（真能达成 + 不破坏现有）
6. 真能让 **N 会话并发不互咬**吗？两会话各自 worktree → `git stash`/`reset --hard`/pytest 互不影响 —— 实现是否真兑现？有没有残留共享态（.git/index、worktrees_root 并发 mkdir、同 task 并发 dump）？
7. 是否**破坏现有**：default OFF 路径是否 byte-identical（off 时 `resolve_spawn_workspace` 早返回，不跑任何 git）？现有 dump/spawn/接续/autoclose/mandate gate/retro-evidence/old_ready/preflight 是否原样？batch fan-out 仍走共享树（v1 OUT）是否真未被波及？
8. merge-back 协议改变闭环语义（会话须 ff-publish 到集成分支）：本地 main 工作树滞后 origin 的后果、owner 手动 tab、下一棒 base 解析是否自洽？

## R4 — 审并发/数据（泄漏回收 / 解析 / 边界）
9. worktree 泄漏回收：`gc`/`remove_worktree` 的 fail-safe（脏/未 publish 保留现场）有无被绕过的路径？sidecar 与实际 worktree 状态不一致（手动删 worktree / 手动删 sidecar）时？
10. venv 解析：`.venv` symlink 对 editable-self-install 项目会让 worktree 跑主树代码（R1-X3 已标 caveat）—— caveat 是否准确、ERP 是否真不受影响（ERP 自身非 editable 装）？`.env`/`.claude` symlink 有无副作用（绝对/相对、跨盘）？
11. 诚实边界：Docker DB 串扰 + alembic 迁移链 fork 仍未解（worktree 只解 git 树层）—— 实现/文档是否如实声明，有无"以为解了其实没解"的过度承诺？
12. 并发 dump-time：同一 source 并发两次 dump 同 task（worktree add 竞争 / branch 已存在 / worktrees_root mkdir）；fetch 改 origin tracking ref 的并发；这些是否 fail-safe？

只报真问题，按 P0(阻断/数据错误)/P1(重要)/P2(建议) 分级，每条含 文件:行号 + 为什么 + 修复。无问题维度明说。
