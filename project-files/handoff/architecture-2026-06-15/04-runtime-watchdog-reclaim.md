# 运行时 watchdog + 自动接续 + 心跳 + §6c reclaim + GC — 架构图

> shard: 始终在线的运行时子系统。git HEAD `5e8d7b2`（2026-06-15）。READ-ONLY 勘察。
> 承重事实全部带 `file:line`，引用的是当前 HEAD 真实代码。

---

## 1. 运行时角色

这套子系统是 handoff-fanout 的「常驻引擎」——由 launchd 周期驱动，无人值守地把 owner 在某个会话里 dump 出来的交接（handoff）变成一个真实打开、真实收到 prompt、真实自动提交的新 VS Code 窗口；并在派窗过程里把 owner 的视图一步带回原桌面，最后回收死掉的窗口/worktree。

两条 launchd 入口（同一份 `auto-continue.sh` 与 `handoff watchdog` 由 plist 周期触发）：

- **自动接续 loop（`install/auto-continue.sh`）** — 每次 launchd tick 遍历各项目 `queue/`，发现 ready 的 `.uri`（去 lock → caffeinate 防睡 → 可选自动解锁）→ `code -n <workspace>` 开新窗 → 等目标窗 frontmost（带 AXRaise 兜底）→ `open vscode://…` URI 把 prompt 粘进 Claude 输入框 → 合成 Enter 提交。围绕提交一对动作实现 **MP 式「定位-动作-回原点」**：派窗前 `_return_precapture`（auto-continue.sh:1320）快照 owner 桌面 B + 可重激活锚点；提交成功后 `_return_jump_back`（auto-continue.sh:1344）一步把视图重激活回 B。**去程 focus-jump 不在这里**——它发生在 `$CODE_BIN`（= dharmaxis code-router）内部，由 `.uri` 第三行 `SPAWNER_FOCUS=` 透传触发（auto-continue.sh:1422）。

- **watchdog（`src/handoff_fanout/watchdog.py`）** — 每 ~10 min（plist StartInterval / WatchPaths）跑一遍，O_EXCL 锁（watchdog.py:90，stale>30min 自清）下做 6 个 backstop mode + orphan 扫 + v4.1 单任务心跳扫 + §6c reclaim tick：
  - **Mode 1-3**（watchdog.py:157/167/210）— fan-in 批次：完成未触发 / fan-in 心跳僵死 / 超时降级。
  - **Mode 4 + Mode 6**（watchdog.py:186 / 322）— 子任务/单任务心跳僵死 = **529 防御**：心跳 >5min 无更新 → 写 `.529-suspected` + 通知 + 定位并 SIGTERM→5s→SIGKILL 杀掉 wedged heartbeat 进程（watchdog.py:427 `_enforce_kill_stuck_task`，literal heartbeat 路径正则 escape 防误杀同名任务/测试 runner）。
  - **Mode 5**（watchdog.py:768 `scan_orphan_spawns`）— 跨项目扫 `ack/*.spawned` 但 `queue/<task>.md` 消失的孤儿。用 `.singlepane` sidecar 的 `isolation` 字段区分 spawn(有)vs dump(无)：ACTIVE 静默跳过 / STALE 写 `.stale-spawn`（非 BLOCKED.md，不误导 owner 关活窗）/ legacy dump 残留逐字节保留旧 orphan 判定。
  - **§6c reclaim**（watchdog.py:945-954，lazy import `reclaim.tick(cfg)`）— worker worktree 窗口回收的 producer。
  - **心跳/529 守护本体**：心跳由 `heartbeat.py` daemon 在 fan-in tab 内每 60s `touch _fan_in_heartbeat`（heartbeat.py:84），watchdog 是观察方。

- **§6c window reclaim（`src/handoff_fanout/reclaim.py`）** — 哨兵驱动（`ack/<id>.reclaim_requested`）的 worker 窗口/ worktree 回收：record-head 记录被合并的 SHA → coordinator 写 reclaim_requested → watchdog tick 在项目 `.spawn.lock` 下写 `reclaim_pending.json` 授权 → 目标窗扩展自己轮询并自闭（PULL 模型，A-poll 2026-06-12）→ producer 凭 `os.kill(host_pid,0)` ESRCH 确认窗口真死才删 worktree（PID dead-man，reclaim.py:882 `_host_pid_liveness`）。

- **gc-singlepane（`src/handoff_fanout/gc_singlepane.py`）** — 卫生 janitor（NON-correctness）：清理 STALE 的 singlepane *coordinator* sidecar 积压（p10–p26 全 active 指同一 cwd → 把共享 identity resolver 弄歧义）。跨 Space winlist/Quartz 活性探针门控、隔离区（quarantine）可逆移动、dry-run 默认。

---

## 2. 核心模块

| 文件 | 责任 | LOC |
|---|---|---|
| `install/auto-continue.sh` | launchd 自动接续 loop：dispatch（precapture→code -n→等 frontmost→AXRaise→open URI→Enter→spawn-return）、unlock-pivot、caffeinate、frontmost/AXRaise 闸、focus 争夺判别器、`run_with_timeout` 通用包装 | 2506 |
| `src/handoff_fanout/watchdog.py` | 周期 watchdog：6 mode backstop（fan-in 1-3 / 529 心跳 4·6 / orphan 5）+ orphan/stale-spawn 分流 + §6c reclaim tick 挂载 | 965 |
| `src/handoff_fanout/reclaim.py` | §6c worker worktree 窗口回收 contract v4：record-head / reclaim_requested 哨兵 / 跨 tick pending 状态机 / PID dead-man / merged 派生 / 19-reason 失败枚举 | 1540 |
| `src/handoff_fanout/heartbeat.py` | fan-in tab 伴生进程：heartbeat daemon（60s touch / 3h 上限 / STOP_AUTO 退出）+ complete + metrics + calibration + status | 335 |
| `src/handoff_fanout/gc_singlepane.py` | STALE singlepane coordinator sidecar 卫生 janitor：跨 Space 活性探针门控、隔离区可逆移动、dry-run 默认 | 295 |

> 注：去程 focus-jump 的实际原语（`spawn-precapture`/`spawn-return`/`focus-jump`/winlist）住在 dharmaxis `scripts/vscode-spaces/vscode-spaces.py`（本 shard 之外的仓），auto-continue.sh 通过 `$(dirname $HANDOFF_CODE_BIN)/vscode-spaces.py` 解析调用（auto-continue.sh:1308）。

---

## 3. 工作机制

### 3.1 watchdog tick 流程（watchdog.py）

1. `main()`（watchdog.py:914）：全局 STOP_AUTO 早退（:915）→ `acquire_lock()` O_EXCL（:90，stale 自清）。
2. 遍历 `glob("*/batches/*/")` 调 `scan_batch`（watchdog.py:927-931）。`scan_batch`（:131）按顺序判 Mode 1（完成未触发 :158 → `dump.trigger_fan_in_if_ready`）/ Mode 2（fan-in 心跳 stale>180s :174）/ Mode 4（子任务心跳 stale>300s :205 → `_mark_529_suspected`）/ Mode 3（超时降级 :214 → `_dump_degraded_fan_in`）。
3. `scan_orphan_spawns()`（:936 → :768）：`ack/*.spawned`、`.md` 缺 + age>300s → `_unified_spawn_state`（:730）分流 ACTIVE(skip)/STALE(`_mark_stale_spawn` :848)/NONE(`_mark_orphan` :823 写 BLOCKED.md)。
4. `scan_single_task_heartbeats()`（:941 → :322）：v4.1 单任务 `queue/<task>.heartbeat` stale>300s → `_mark_single_task_529`（:368）→ `_enforce_kill_stuck_task`（:427）SIGTERM→grace→SIGKILL（`_kill_pid` :577 杀后再探，闭 R1「假 killed」）。
5. `reclaim.tick(cfg)`（:951，lazy import）。
6. `release_lock(fd)`（:106，单出口，finally）。

### 3.2 auto-continue.sh dispatch 序列（cold/singlepane 路径）

- **解析 .uri**（auto-continue.sh:1413-1423）：第 1 行 `WORKSPACE=` / 第 2 行 `URI=` / 第 3 行可选 `SPAWNER_FOCUS=` → `export HANDOFF_SPAWNER_FOCUS`（:1423）；每任务重置 return 状态（:1431）。
- **precapture（去程前）**：`_skip_code_n != 1` 时先调 `_return_precapture`（:1665），**必须在 `$CODE_BIN -n` 之前**——此时 owner 还在 B、frontmost 未被子窗污染。
- **开窗**：`"$CODE_BIN" -n "$OPEN_TARGET"`（:1668）——`$CODE_BIN` = code-router，去程 focus-jump 在它内部按 `HANDOFF_SPAWNER_FOCUS` 发生（A 桌面）。
- **等 frontmost**：`wait_target_window_frontmost`（:1690，真 wall-clock，封顶 3s，poll-first，:414 定义）→ 失败 `raise_task_window` AXRaise 兜底 + re-wait（:1699-1700）。
- **2.4 判别器**（:1722-1730）：仍非 frontmost → 一次 probe，front Code 窗 ∉ pre-open 快照 → 视为「刚开的冷渲染滞后标题」→ dispatch；否则 fail-closed（:1748 `FOCUS-CONTENDED`，bounded retry ≤5 :1749）。
- **open URI**（:1803 `_perf_mark open-uri`，实际 open 在其上方块）→ settle sleep（cold 0.5s :1822 / warm 1.5s :1824）。
- **提交闸**：`screen_is_locked` 必 rc=1（:1834-1835）→ `accessibility_trusted`（:1848）→ frontmost/cold/singlepane 短路（:1854）→ 合成 Enter（cold_submit / singlepane_submit_with_retry，title-nonce + readiness gate，真提交以 transcript 增长为准）。提交成功才 `_RETURN_DISPATCHED=1`（:1895/1902/1937/1943/1968/1981）。
- **spawn-return（回程）**：`[ "$_RETURN_DISPATCHED" = "1" ] && _return_jump_back "$_submit_token"`（auto-continue.sh:2006）——**正向门控**：所有未确认 Enter 路径（re-lock / accessibility 缺 / Enter 被扣 / frontmost-not-Code）都不回跳，owner 永不在 worker 未提交时被拽回。
- 结尾 `sleep 0.5`（:2007）防同 run 连续 spawn 让 owner 晕；`_post_iter_cleanup` + RETURN trap 释放 lock（:2375）。

### 3.3 §6c reclaim 流程（reclaim.py）

record-head（`cli_record_head` :1350，记被合并 worktree 的 SHA）→ coordinator 写 `ack/<id>.reclaim_requested`（JSON `{run_id, ts}`，路径 :192）→ **tick N**（`tick` :1251 → `_process_request`）在项目 `.spawn.lock`（:224）下：transcript-mtime 活性探针 fail-CLOSED（:261，任何读异常 ⇒ 无条件 alive ⇒ 永不关）→ 合法则写 `reclaim_pending.json`（:196，载 role=worker/reason=reclaim/nonce/run_id/issued_at/ack_timeout，:1242 log）→ 续 lock 跨 tick。目标窗扩展轮询自己的 pending、自闭（closeTabs + `workbench.action.closeWindow`），回写 `close_issued` ack（非终态）。**tick N+1**：`_resolve_pending`（:1004）→ `_resolve_close_issued`（:928）读 `host_pid.json` 调 `_host_pid_liveness`（:882）：`os.kill(pid,0)` ESRCH=dead → `_reclaim_worktree_resources` 删 worktree + `_write_done`；alive 且未到 deadline → 续 lock 留 pending 下 tick 再查；PID 复用 fail-closed（false-alive → 等到 deadline → worktree 保留）。终态 `mv → processed/`（:653 防每 tick 重报）。

### 3.4 gc-singlepane 流程（gc_singlepane.py）

`main()`（:215）→ `_live_code_window_titles()`（:47，winlist 优先 → Quartz 兜底）；`None`=探针不可用 → **FAIL-SAFE 整体不删**，`--execute` 直接 abort（:253-259）。`find_gc_candidates`（:133）：sidecar role=`supervisor_succession`（:43）、task ∉ protect、`live_titles is not None` 且无活窗带该 task（:159 `_task_is_live` 边界匹配防 p1 撞 p12）、`<task>.done` 存在或 mtime age≥retention → 候选。workspace 仅 canonical 路径才打包隔离（:168-189，off-canonical → WARN 只隔 sidecar，禁静默降级）。`_quarantine`（:202）`shutil.move` 进 `_gc_quarantine/<stamp>/` 保相对路径（可逆，非 unlink）。

---

## 4. 数据流 / 状态流（文件 / sidecar）

| 工件 | 路径 | 写者 → 读者 |
|---|---|---|
| fan-in heartbeat | `<home>/<proj>/batches/<bid>/_fan_in_heartbeat` | heartbeat daemon 每 60s touch（heartbeat.py:143）→ watchdog Mode 2（:169） |
| 子任务 heartbeat | `batches/<bid>/<sub>.heartbeat` | sub-task tab → watchdog Mode 4（:193） |
| v4.1 单任务 heartbeat | `<proj>/queue/<task>.heartbeat` | 单任务 spawn-prompt 每 60s → watchdog Mode 6（:344） |
| 529 标记 | `…<task>.529-suspected` | watchdog `_mark_529_suspected`/`_mark_single_task_529`（:235/:374） |
| orphan 阻断 | `<proj>/queue/<task>.BLOCKED.md` | watchdog `_mark_orphan`（:828） |
| stale-spawn note | `<proj>/queue/<task>.stale-spawn` | watchdog `_mark_stale_spawn`（:871，幂等 size-checked） |
| spawn sidecar | `<proj>/queue/<task>.singlepane` | spawn.py(`isolation`∈worktree/singlepane) / dump.py(无 isolation) → Mode 5 分流（:751）+ gc-singlepane（:123） |
| .uri 派发意图 | `<proj>/queue/<task>.uri`（含 `SPAWNER_FOCUS=` 第三行） | dump/watchdog → auto-continue.sh:1413 |
| reclaim 哨兵 | `<proj>/ack/<id>.reclaim_requested`（JSON run_id/ts） | coordinator → reclaim.tick（:1278） |
| reclaim pending | `<proj>/ack/<task>.reclaim_pending.json` | producer tick N（:196）→ 扩展轮询 + producer tick N+1（:1004） |
| host_pid | `<proj>/ack/<task>.host_pid.json`（pid/nonce） | worker activate → `_host_pid_liveness`（:948） |
| reclaim done/failed | `…<task>.reclaim_done` / `.reclaim_failed.json`（19 reason enum） | producer（:212/:216） |
| 项目 spawn 锁 | `<home>/<proj>/.spawn.lock`(+`.reclaim-owner.json`) | spawn-intent producers + reclaim C6（:224/:233） |
| 隔离区 | `<home>/_gc_quarantine/<stamp>/<相对路径>` | gc-singlepane `_quarantine`（:209） |
| router.log | `~/.vscode-spaces/router.log` | code-router 写去程 focus-jump（实证 70 条 hit） |
| auto-continue.log | `~/.claude-handoff/auto-continue.log` | auto-continue.sh `log` + spawn-return（实证 31 条 RETURN-REACTIVATE） |
| watchdog.log | `~/.claude-handoff/watchdog.log` | watchdog stdout（plist）|

---

## 5. 现状三态

- ✅ **watchdog 6-mode backstop + 529 防御**：mode 1-6 全活，`_enforce_kill_stuck_task` 杀 wedged 进程上线（watchdog.py:427）。watchdog.log 活跃（mtime 06-15 05:37）。
- ✅ **自动接续 dispatch + Enter 提交**：cold/singlepane/warm 三路径活，title-nonce + transcript-增长 readiness gate（auto-continue.sh:1854+）。
- ✅ **去程 focus-jump（A 桌面派窗）LIVE / 已观测**：`~/.vscode-spaces/router.log` 70 条 `focus-jump ✅ 直跳 N→M` hit，最新 06-15 05:19（sw-coord-p26 / ff-coord-4 / erp-dev-coord-36）——**不是 never-fired**。
- ✅ **回程 spawn-return（一步回原点）LIVE / 已观测**：`auto-continue.log` 31 条 `RETURN-REACTIVATE-WS 一步重激活`（最新 ff-coord-5 / xunyin-coord-15），含 focus-steal 保护的 `RETURN-ABANDON`（owner 漂移则不回跳）。`spawn-return` 恒 exit 0 契约坐实（vscode-spaces.py:878-893）。
- 🟡 **§6c reclaim — 哨兵驱动，当前 IDLE（非 dormant-死）**：producer = watchdog tick 已接线（watchdog.py:951），代码全活（PID dead-man / pending 状态机 / 19-reason enum）。但 **`tick` 是哨兵触发**——无 `reclaim_requested` 哨兵的 tick 是 no-op（reclaim.py:1278-1280）。实地核查：`~/.claude-handoff` 下当前**零** `reclaim_requested`/`reclaim_pending`/`reclaim_done` 文件。即 reclaim 链路活、就绪、但当下无在飞回收（要 coordinator 主动 `reclaim-request` + 扩展侧轮询配合才走完）。这是「就绪待触发」而非「写了跑不通」。
- 🟡 **gc-singlepane — 刚合入，按需手动跑**：代码活、winlist 探针可用（binary 06-11 已编译），但是 `handoff gc-singlepane` CLI 默认 dry-run，无 launchd 自动周期挂载（设计即「one-shot + ongoing 手动 hygiene」，非 always-on）。
- ✅ **heartbeat daemon + metrics/calibration**：fan-in tab 内 60s touch + 3h 上限 + STOP_AUTO 退出（heartbeat.py:118-146），活。

---

## 6. 🔴 半实现陷阱

1. **§6c reclaim 的「端到端」依赖扩展侧轮询配合 — producer 单边活 ≠ 全链路在跑（🟡 非陷阱但易 overclaim）**
   - 现象：watchdog 已挂 `reclaim.tick`、producer 状态机 + PID dead-man 全活，但回收要走完必须：① coordinator 真发 `reclaim_requested` ② 目标窗扩展真轮询自己的 `reclaim_pending` 并自闭回写 `close_issued`/`host_pid`。当前盘上零哨兵 = 没有任何在飞回收可证「真触发真关窗」此刻在发生。
   - 后果：若误把「producer 侧单测 + tick 接线」当「§6c LIVE 端到端」会重蹈 p15 教训（扩展侧 ≠ E2E）。
   - 正解：§6c 标 **🟡 就绪-IDLE**；声称 LIVE 必须有一次真 coordinator→扩展→PID-ESRCH→worktree 删的端到端证据（盘上 `reclaim_done` + worktree 消失），非仅 tick 接线。

2. **gc-singlepane 无自动周期 — 积压只在有人手动跑时才清（🟡）**
   - 现象：sidecar 积压（p10-p26）是 identity resolver 歧义的根因，gc janitor 是排空手段，但它不在任何 launchd 间隔里，靠人记得 `handoff gc-singlepane --execute`。
   - 后果：不跑则 resolver 持续歧义、dx-spawn auto-derive(S3) 不工作——「写了但不自动跑」=半在线。
   - 正解：作为 hygiene 设计如此（低风险、可逆、避免误删活窗的 always-on 风险），但应在 runbook/月度蒸馏里登记「定期手动 GC」或评估加 watchdog 末尾 dry-run-only 探测告警，不静默放任积压。

3. **去程/回程的真实原语跨仓（dharmaxis vscode-spaces.py）— 本仓只有编排壳**
   - 现象：auto-continue.sh 的 `_return_precapture`/`_return_jump_back` 只是壳，真活全在 `$(dirname $HANDOFF_CODE_BIN)/vscode-spaces.py`（auto-continue.sh:1308）。`$CODE_BIN` 不指向 router（如裸 `code`）→ `_return_spaces_py` return 1 → 回程整体静默 disarm（fail-open，无返回腿）。
   - 后果：非陷阱（设计 fail-open），但运维上「回程没生效」可能只是 `HANDOFF_CODE_BIN` 未指 router，而非 bug——排查须先验 `$HANDOFF_CODE_BIN`。
   - 正解：已是 fail-open 正确姿态；登记为「回程激活前提 = `HANDOFF_CODE_BIN` 指 code-router」。

---

## 7. 🔴 KNOWN P1 确认：回程 helper 无 wall-clock 超时

**结论：CONFIRMED（已证实，且根因比 brief 描述更尖锐）。**

`_return_precapture`（auto-continue.sh:1320）和 `_return_jump_back`（auto-continue.sh:1344）都**同步**调 `/usr/bin/python3 "$_RETURN_PY" …`，**没有任何 `run_with_timeout` 包装**：

- `_return_precapture` → `_pre=$(/usr/bin/python3 "$_RETURN_PY" spawn-precapture 2>>"$LOG")`（**auto-continue.sh:1326**，裸命令替换，无 timeout）。
- `_return_jump_back` → `/usr/bin/python3 "$_RETURN_PY" spawn-return …`（**auto-continue.sh:1351**，仅尾随 `|| true`，无 timeout）。

对照：仓内**确有** `run_with_timeout` 通用包装（**auto-continue.sh:1079-1096**：后台 `"$@" &` + `kill -0` 轮询 + `pkill -TERM -P` 收孙 + SIGTERM→1s→SIGKILL，超时 return 124），且 lock-probe 路径已用它（auto-continue.sh:1034 `run_with_timeout "${HANDOFF_LOCKCHECK_TIMEOUT:-15}" $qcmd`）。回程两函数**未享用**这层保护。

**为什么一次 hang 冻结整个 iteration（爆炸半径）**：

- 这两个 python 进程**没有自身的整体 wall-clock 上限**。vscode-spaces.py 内部的 `subprocess.run(..., timeout=…)` 只覆盖单个子调用（winlist 10s / code 15-30s 等），**不覆盖** `ensure_winlist()` 里的 **`swiftc` 编译（vscode-spaces.py:82，无 timeout）**——首次运行 / winlist.swift 更新 / NFS 抖动 / swiftc 卡住时这一步可无限阻塞；也不覆盖 `code <ws>` 之外可能 wedge 的 macOS GUI 调用。任一卡住，整个 `python3` 进程挂死。
- 该进程挂在自动接续 loop 的**同步主路径**上（dispatch iteration 内）。loop 末尾用 RETURN trap 释放每任务 `lock`（auto-continue.sh:2375 `rmdir "$lock"`），并在 `_post_iter_cleanup` 里停 caffeinate / 可能 re-lock。`_return_jump_back` 卡死 → iteration 永不返回 → **lock mutex 不释放、caffeinate 不停、后续 spawn 全阻塞**——正是 brief 描述的级联。
- 缓解项（已有，但不充分）：`spawn-return` 自身恒 exit 0（vscode-spaces.py:878-893 fail-open），且内部子调用多带 `timeout=`。**但**「恒 exit 0」只在进程**能跑到退出**时成立——若卡在无 timeout 的 `swiftc` 或某个无 timeout 的 GUI syscall，进程根本到不了 `sys.exit(0)`，bash 层又没有外层闸 → 真实悬挂仍可发生。当前 winlist binary 已编译（`~/Projects/dharmaxis/scripts/vscode-spaces/winlist` 存在，06-11），happy path 不触发 swiftc；但这只是「当前侥幸」，非结构保证。

**正解（对齐「禁静默降级 + fail-open 红线」）**：用既有 `run_with_timeout` 包两处调用，例如
`_pre=$(run_with_timeout "${HANDOFF_RETURN_TIMEOUT:-20}" /usr/bin/python3 "$_RETURN_PY" spawn-precapture 2>>"$LOG")` 及对 `spawn-return` 同样处理；超时（rc=124）按 fail-open 处理（precapture 保持 disarm；jump-back 跳过回程、记 WARN）。这把「回程是 best-effort、任何失败都不阻塞 spawn」的设计意图从「依赖被调进程自律」升级为「编排层强制兜底」。次级：给 vscode-spaces.py 的 `swiftc` 编译加 timeout（防首次/重编译挂死）。

---

## 8. 承重事实 file:line 清单

1. `run_with_timeout` 通用包装存在（后台+SIGTERM→1s→SIGKILL，超时 return 124）— `install/auto-continue.sh:1079-1096`。
2. `_return_precapture` 裸同步 python 无 timeout — `install/auto-continue.sh:1326`。
3. `_return_jump_back` 裸同步 python 仅 `|| true` 无 timeout — `install/auto-continue.sh:1351`。
4. 回程正向门控（仅真提交 `_RETURN_DISPATCHED=1` 才回跳）— `install/auto-continue.sh:2006`。
5. precapture 必须先于 `$CODE_BIN -n` 去程开窗 — `install/auto-continue.sh:1665,1668`。
6. `SPAWNER_FOCUS=` 从 .uri 第三行透传 `export HANDOFF_SPAWNER_FOCUS` — `install/auto-continue.sh:1422-1423`。
7. `_return_spaces_py` 用 `${HANDOFF_CODE_BIN:-}` 守 set-u，CODE_BIN 非 router → return 1 fail-open 无回程 — `install/auto-continue.sh:1306-1310`。
8. lock-probe 路径已用 `run_with_timeout`（对照证明回程未用）— `install/auto-continue.sh:1034`。
9. watchdog O_EXCL 锁，stale>1800s 自清 — `src/handoff_fanout/watchdog.py:90-103`。
10. Mode 6 杀 wedged 进程 literal heartbeat 路径正则 escape — `src/handoff_fanout/watchdog.py:489-503`。
11. `_kill_pid` SIGTERM→5s grace→SIGKILL 杀后再探（闭「假 killed」）— `src/handoff_fanout/watchdog.py:577-622`。
12. Mode 5 用 sidecar `isolation` 区分 spawn/dump，ACTIVE skip / STALE 写 `.stale-spawn` — `src/handoff_fanout/watchdog.py:730-765,809-816`。
13. §6c reclaim tick 挂在 watchdog `main()`（lazy import）— `src/handoff_fanout/watchdog.py:945-954`。
14. heartbeat daemon 60s touch + 3h 上限 + STOP_AUTO 退出 — `src/handoff_fanout/heartbeat.py:118-146`。
15. reclaim `tick` 哨兵驱动、无哨兵即 no-op — `src/handoff_fanout/reclaim.py:1251-1285`。
16. PID dead-man `_host_pid_liveness`：ESRCH=dead / EPERM=alive / 其他=unknown fail-closed — `src/handoff_fanout/reclaim.py:882-912`。
17. close_issued 仲裁：host PID 物理离场才删 worktree — `src/handoff_fanout/reclaim.py:928,948-955`。
18. transcript 活性探针 fail-CLOSED（读异常 ⇒ 无条件 alive ⇒ 永不关）— `src/handoff_fanout/reclaim.py:261-287`。
19. gc-singlepane 探针不可用 → FAIL-SAFE 整体不删、`--execute` abort — `src/handoff_fanout/gc_singlepane.py:246-259`。
20. gc-singlepane workspace 仅 canonical 才隔离，off-canonical → WARN 只隔 sidecar — `src/handoff_fanout/gc_singlepane.py:168-189`。
21. `_quarantine` `shutil.move`（可逆，非 unlink）保相对路径 — `src/handoff_fanout/gc_singlepane.py:202-212`。
22. （跨仓）`ensure_winlist()` `swiftc` 编译无 timeout = 回程 hang 真实根因点 — `dharmaxis/scripts/vscode-spaces/vscode-spaces.py:82`。
23. （跨仓）`cmd_spawn_return` 恒 `sys.exit(0)` fail-open 契约 — `dharmaxis/scripts/vscode-spaces/vscode-spaces.py:878-893`。

> 活态实证（READ-ONLY 验证，非代码）：router.log 70 条 `focus-jump ✅ 直跳`（最新 06-15 05:19）；auto-continue.log 31 条 `RETURN-REACTIVATE-WS 一步`（最新 ff-coord-5）；`~/.claude-handoff` 下当前 0 个 reclaim 哨兵 → §6c IDLE。
