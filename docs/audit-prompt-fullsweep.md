# Full-sweep codex audit prompt — handoff-fanout

> Gate 0 of `runbook-unlock-pivot-rollout.md`. Run on branch `feat/vscode-unlock`
> (= main + unlock-pivot; the riskiest new surface). For main-only: `git checkout
> main` and drop Pass 2's unlock section.
>
> codex is **read-only, does not modify files**, model gpt-5.x high. Because it's
> ~8140 LOC python + ~1012 LOC bash, run as **5 subsystem passes** (broad one-shot
> would miss depth). Each pass = the COMMON HEAD + that pass's section. Wrap each
> `codex exec` in `timeout 600`. After each pass, summarise findings into a
> P0/P1/P2 table + mark 需修/可豁免/误报 for owner ruling — **do not auto-edit code**.

## How to run (per pass)
```bash
cd ~/Projects/handoff-fanout && git checkout feat/vscode-unlock
timeout 600 codex exec --sandbox read-only --skip-git-repo-check "<COMMON HEAD>

<PASS N SECTION>"
```

---

## COMMON HEAD (prepend to every pass)

```text
你是资深代码审计师，对 handoff-fanout 做只读全面审计（不改任何文件）。先实地读源码再下结论，每条 finding 必须带 file:line 证据；宁可少报不可误报，无法证实的写「存疑」不写「P0」。

业务背景与红线：
- handoff-fanout 是 macOS 上驱动 AI 编码会话「自动接续」的编排引擎，无人值守地推进一个金融 ERP 系统的开发（事件源记账、¥500K 单笔上限）。它的价值 = 一串闸门保证「无人审的自主工作不失控」：retro 强制复盘门(HANDOFF_RETRO_MANDATE) + Phase A-D codex 审计门(HANDOFF_AUDIT_MANDATE)：会话闭环必须先复盘+审计才能 dump 下一棒；git 劫持防御(safe-commit + pre-commit/pre-push hook)：多 tab 共享 .git/index 不串；自动接续 launcher(auto-continue.sh)：路由会话产物到新 VS Code tab，新增「锁屏→自动解锁(注入登录密码)→可见 GUI→重锁」路径；防孤儿/防死锁/防 529 的 watchdog + heartbeat。
- 红线：①任何闸门都不能被绕过或伪造（不能跳过 retro/codex 审计就 dump；BLOCKED 不能 spoof）；②launcher 绝不静默死锁、绝不盲跑、绝不把机器留在解锁态；③自动解锁会注入 Mac 登录密码，爆炸半径大，必须 per-project opt-in + 失败保护；④账务相关绝不被工具层绕过。

审计维度（每条 finding 标维度）：正确性/并发竞态、安全、闸门完整性、可靠性/故障模式、数据与状态完整性。
输出：按 P0/P1/P2 分组，每条 = 问题 + 证据(file:line/符号) + 触发场景 + 建议修法 + 一句严重度理由。最后列「本次未覆盖/需另 pass 的点」。
跨仓依赖：解锁路径调用 ~/Projects/mindpersist 的 src/agent/unlock_cli.py（退出码 0成功/1解锁失败/2配置错），审 launcher 解锁段时一并核对该契约是否被正确消费。
```

## PASS 1 — 闸门完整性（最高优先）
```text
读 src/handoff_fanout/retro_gate.py(1507) + codex_audit.py(2207) + handoff_precheck.py(567) + dump.py(1136) 的 _run_retro_gate / _write_old_ready / 审计块折叠。重点：
- 会话能否在「没真复盘 / 没真过 codex 审计」时仍成功 dump？枚举所有 bypass 路径（env、缺 evidence、hash 不校验、schema 协商漏洞、HANDOFF_*_MANDATE 未设时的 legacy 路径）。
- evidence_hash / codex_audit_hash / owner_ack_token 防篡改强度；有无「机械填表也能过」的 ceremonial pass。
- HEAD freshness 有无零并发也触发的假阳性（commit 后做长时间 memory+audit → head_matches∧¬commit_fresh），或反过来「过期 evidence 仍被接受」。
- next_session_forced_task（bypass 欠债）能否被下一棒静默绕过。
- schema 版本耦合 EVIDENCE↔OLD_READY↔消费方，bump 一个是否牵连误拒。
```

## PASS 2 — 自动接续 launcher + 新解锁路径（最高危新面）
```text
读 install/auto-continue.sh(771) 全文 + mindpersist/src/agent/unlock_cli.py、idle.py(_attempt_unlock)。重点：
- 解锁路径(screen_is_locked / unlock_enabled_for_project / .unlock.lock 互斥 / run_with_timeout / unlock_fail_bump 冷却 / do_relock / _post_iter_cleanup)：
  - 互斥锁是否真持有跨 unlock→claim→submit→relock，所有 continue/break 路径都释放不泄漏？caffeinate 子进程在所有退出路径都被 kill？
  - 任一退出路径会不会「已解锁但没重锁」把 Mac 留在解锁态？relock 失败的 halt 是否真生效(break 2 覆盖嵌套循环)？
  - 冷却：rc=2 是否真永久转人工？rc=1 阈值后会不会永久卡死或永不冷却？时间比较符号/类型坑。
  - per-project opt-in 是否真没全局 default-on 后门？lock 探针 key-absent=unlocked 判定在真 ioreg 上对吗？UNKNOWN→fail-closed 所有分支一致？
- 通用：set -u 下未定义变量；$HANDOFF_UNLOCK_CMD/$HANDOFF_CAFFEINATE_CMD/$HANDOFF_RELOCK_CMD 未加引号 word-split 是有意 argv 拆分还是注入/空格路径 bug；osascript/open 参数注入；原子认领(mv .uri→launched)竞态。
```

## PASS 3 — 并发/竞态/锁（跨 tab + 跨 launchd tick）
```text
读 safe_commit.py(237) + git_guard/git + dump.py(fan-out/fan-in 状态机) + watchdog.py(820) + heartbeat.py(335)。重点：
- 多 tab 共享 .git/index 的 hijack 防御是否真闭合(safe-commit flock + pre-commit 段5 HANDOFF_EXPECTED_FILES + commit --only)；有无 TOCTOU。
- fan-out/fan-in：sub-task 文件归属交集校验、last-one-out 触发、孤儿回收、心跳「死 vs 老」。
- 所有 mkdir-锁/flock 的 stale 回收、PID 复用、并发首写者竞争(noclobber)。
- watchdog mode 6 (529) 误杀本会话风险。
```

## PASS 4 — 原子性/数据与状态完整性
```text
读 atomic.py(302) + 全仓 write_with_fsync vs atomic_replace 用法。重点：
- 每个「会被并发读 / kill 中途」的产物(.md/.uri/manifest/evidence/old_ready/.done/.BLOCKED.md)是否用了正确原语(crash-原子 replace vs 仅 durable 的 O_TRUNC)；列出仍用 write_with_fsync 但需要 atomic_replace 的点。
- 部分写、目录 fsync、temp 名是否撞 launcher 的 *.uri/*.md glob。
- 状态机非法迁移(done/blocked/active)、重复消费、幂等。
```

## PASS 5 — install/部署 + 残留面
```text
读 install/install.sh(241) + launchd plist + extension/(已标 OBSOLETE)。重点：
- 卸 autoclose 是否干净(无 dangling 调用 / 无误删 overdue+old_ready 承重件)；extension 卸载迁移幂等。
- launchd plist 的 env(mandate 三路径)、WatchPaths/QueueDirectories 语义、curl-pipe 安装路径。
- 全仓 grep 残留死代码 / 过期文档 / 安全提示泄露。
```
