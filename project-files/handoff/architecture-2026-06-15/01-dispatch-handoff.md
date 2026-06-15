# 架构地图 — DUMP + RETRO-EVIDENCE GATE + PRECHECK 子系统

> 范围：`handoff-fanout` 的「派会话 dump + 复盘证据闸 + precheck」子系统。
> Git HEAD `5e8d7b2`（read-only 勘察，未改任何源码 / 未跑测试 / 未派子 agent）。
> 承重事实均带 `file:line`，供编排者回读核验。

---

## 1. 运行时角色

这个子系统是 handoff-fanout 的「交接棒生产端 + 复盘门禁」。当一个 Claude Code 会话闭环一个 task 时，它调 `handoff dump` 写出下一棒的 queue 文件（`queue/<task>.md` 人类可读交接棒 + `queue/<task>.uri` launchd WatchPaths 触发器），launchd watcher 看到 `.uri` 落地后在 1 秒内 spawn 一个新 VS Code Claude tab 接续。dump 有两种形态：单任务默认模式（自动接续链 / 中枢 succession / singlepane worker）和 fan-out 批模式（`--open-batch manifest.json` 扇出 ≤3 个 sub-task + 一个 fan-in 汇总 tab，带 N_max/全局活跃上限/file_ownership 物理冲突闸/错峰 spawn 等 v5 安全闸）。

叠在 dump 之上的是 **v5.4 retro-evidence gate**：它把「dump 下一棒前必须先复盘」从口号变成工具层 invariant。闭环会话先跑 `handoff precheck` 把 Phase 0 五项 + Phase 1 五类状态快照成一个自校验哈希的 `precheck/<task>.retro.evidence.json`；下一次 `handoff dump --retro-evidence FILE`（或 `HANDOFF_RETRO_MANDATE=1` 全局拨闸）会校验这份证据（schema/hash/HEAD 新鲜度/phase 状态枚举），产出 §7.1 的七档 exit code（0/1/2/3/4/6）+ stderr 前缀（`OK:`/`ERR-FATAL:`/`ERR-BLOCKED:`/`ERR-LOCKED:`/`ERR-RETRY:`/`ERR-BYPASS:`），并维护一个 attempt 计数器（≤2 后硬拒 BLOCKED）+ 三档锁。证据通过则 dump 额外写 `ack/<task>.old_ready`（审计元数据，供新会话 §0 审前任）。Phase D 还叠了一个 codex 审计闸（G0-G9，`HANDOFF_AUDIT_MANDATE=1`），用独立的 audit attempt 计数器，强制「改了代码必须有通过的 codex 审计块才能下传」。

---

## 2. 核心模块

| 文件 | 一句话责任 | LOC |
|---|---|---|
| `src/handoff_fanout/dump.py` | dump CLI 入口 + 单任务/批/fan-in 三种交接棒产出 + 七档闸调度 + worktree/singlepane 解析 + 孤儿清理 | 2073 |
| `src/handoff_fanout/retro_gate.py` | v5.4 复盘证据闸核心：证据加载/校验、HEAD 新鲜度三档、bypass/overdue 闸、attempt 状态机、BLOCKED.md、re-align、Phase D 审计闸路由 | 1507 |
| `src/handoff_fanout/handoff_precheck.py` | precheck CLI + 证据 builder + 规范哈希（canonical JSON）+ session 指纹/id 解析 + phase status 装配 | 567 |
| `src/handoff_fanout/templates.py` | 三种交接棒 markdown 模板（单任务/sub-task/fan-in）+ BLOCKED.md + worktree banner；§0 审前任 / §-1 复盘 SOP 注入 | 703 |
| `docs/PROTOCOL.md`（对照基准，非本子系统代码） | on-disk 布局 + state machine 规范文档（schema_version 2，停在 v1.0.0） | 289 |

旁系（被引用但不在本 shard 深读范围）：`codex_audit.py`（Phase D G0-G9 闸 + bypass 产出器）、`worktree.py`、`spawn.py`、`config.py`、`atomic.py`、`spawn_nonce.py`、`spawner_focus.py`、`memory_baseline.py`、`install/auto-continue.sh`（launchd watcher + overdue 扫描器）。

---

## 3. 工作机制

### 3.1 `handoff dump` 端到端（单任务 active 路径）

`main()`（`dump.py:1867`）流程：

1. 解析参数 → `--coordinator` × 批/fan-in flag 组合在 `dump.py:1879-1890` 机器拒（warmgap-B MUST-3）。
2. `--cleanup-orphan` 早返回（`dump.py:1892`）。
3. 校验 task-id/project-slug（`dump.py:1903-1910`）。
4. STOP 多层哨兵检查 `any_stop_auto`（`dump.py:1915`；`any_stop_auto` 定义 `dump.py:319`，查 `done`/`STOP_AUTO`/项目级/批级四层）→ 命中则 exit 0 不写。
5. **复盘闸** `_run_retro_gate`（`dump.py:1920`）→ 非 OK 则 `gate_result.emit()` + 返回 exit_code（`dump.py:1921-1923`）。
6. 批/fan-in 分派（`dump.py:1925-1934`）。
7. 项目级 preflight 闸 `run_preflight_gates`（`dump.py:1939-1944`；fail-closed，`dump.py:133`）。
8. worktree 解析 `resolve_spawn_workspace`（`dump.py:1971-1982`，仅 active+非 dry-run+非 suppress）→ 把后继重定向到独立 worktree，或 BLOCKED 退出。
9. `write_active_dump`（`dump.py:2036`）在 singlepane 并发硬闸 `singlepane_worker_guard` 内执行（`dump.py:2029-2035`）。

`write_active_dump`（`dump.py:812`）的关键产出顺序（**所有 sidecar 先写，`.uri` 触发器最后写**，`dump.py:909-916` 注明此排序由 codex+Gemini R2 裁定）：先写 `<task>.md`（crash-atomic `atomic_replace`，`dump.py:871`）→ 若 status=done/blocked 走终态分支（`dump.py:874-900`，写 `.done`/`.BLOCKED.md` + 删 `.uri`/`.heartbeat`）→ active 路径先删任何旧 `BLOCKED.md`（`dump.py:907`，防 launcher 跳过有效 `.uri`）→ 写 `ack/<task>.queued` → 写 `ack/<task>.old_ready`（仅当有 retro evidence，`dump.py:938`）→ 写 `ack/<task>.worktree`（仅 CREATED worktree）→ singlepane sidecar+workspace → 中枢 memory baseline → pbcopy → **最后** `atomic_replace` 写 `.uri`（`dump.py:1037-1039`，附 `SPAWNER_FOCUS=` 行）→ 发通知。

### 3.2 复盘闸如何 enforce「已复盘」

`_run_retro_gate`（`dump.py:247`）决定闸是否跑，三个触发器任一激活：`--retro-evidence FILE` / `HANDOFF_RETRO_BYPASS=1` / `HANDOFF_RETRO_MANDATE=1`（或 `HANDOFF_AUDIT_MANDATE=1`），见 `dump.py:279-301`。豁免路径：

- 批模式（done/blocked/open_batch/fan_in）整体跳过（`dump.py:261-262`）。
- 终态 done/blocked 且**无**显式 evidence → 跳过（`dump.py:276`，理由：终态无后继，retro/audit 都预设有后继）；但若显式传了 `--retro-evidence` 则**继续校验**（不静默跳过）。
- 项目级 mandate roll-out：`mandate_projects` 配置非空时，未列入的兄弟项目走 legacy 路径（`dump.py:293-299`），但 bypass 与显式 evidence 永远到闸。

闸主体 `check_retro_gate`（`retro_gate.py:1226`）：① overdue 闸 `_check_follow_up_overdue`（`retro_gate.py:1251`）→ ② bypass 分支校验 override.json（`retro_gate.py:1260-1283`）→ ③ evidence_path 为 None 时按 mandate 路由（`retro_gate.py:1285-1312`）→ ④ 持 `_ordered_locks`（precheck→dump，`retro_gate.py:1314`）加载证据 `_load_evidence`（`retro_gate.py:419`，校验存在/JSON/schema 版本/evidence_kind/64 位 hash/规范哈希自洽）→ nonce 校验 → mode 校验 → 非 forensic 则 phase 状态枚举校验 `_validate_phase_status`（`retro_gate.py:463`）+ HEAD 新鲜度 `_check_head_freshness`（`retro_gate.py:534`）→ ⑤ 若 audit mandate 开则跑 G0-G9（`retro_gate.py:1446-1482`）→ ⑥ 清 attempt 计数器返回 `_ok()`。

### 3.3 precheck → evidence.json → dump 闸

`handoff precheck`（`handoff_precheck.py:504` main）：校验 task-id/workspace/project → 解析 `--phase0-status`/`--phase1-status`/`--phase-status-file` → CLI 层先查「非 ✅ 必带 reason」`check_reason_required`（`handoff_precheck.py:426`，§7.13 防仪式化打勾）→ 持 `precheck.lock` → `build_evidence`（`handoff_precheck.py:315`）装配 payload：schema_version `5.5.0`（`handoff_precheck.py:35`）、`head_at_precheck`（git HEAD）、`head_at_precheck_timestamp`、`session_commits`（`@{upstream}..HEAD` 快照，供 dump 端 re-align 证明 HEAD 只因兄弟 tab 移动，`handoff_precheck.py:257`）、`phase0`/`phase1`（默认每项 `skip`/`unsupplied`，dump 闸会拒）、`session_id`（`resolve_session_id` `handoff_precheck.py:174`，主键 `CLAUDE_CODE_SESSION_ID` env，缺则 `session_fingerprint` machine-UUID+cwd+entrypoint 哈希）→ `evidence_hash = compute_evidence_hash`（规范 JSON 排序键无空格，排除 hash 字段自身，`handoff_precheck.py:203`）→ `write_evidence`（atomic+fsync）。dump 闸用 `compute_evidence_hash` 重算并比对（`retro_gate.py:454-459`）。

### 3.4 七档 exit code（§7.1）

`retro_gate.py:56-68` 定义：

| exit | 前缀常量 | 含义 | AI 应对 |
|---|---|---|---|
| 0 | `OK` | 闸通过 | 等 launchd spawn |
| 1 | `ERR-FATAL` | tamper（codex 审计篡改类，retry 无用，不动计数器） | 停 |
| 2 | `ERR-BLOCKED` | attempt_n=2 硬拒 / head-stale-fatal / counter-corrupt | 停 retry + 走 BLOCKED 流程 |
| 3 | `ERR-LOCKED` | precheck/dump/attempt/audit 锁竞争 | 让位退出 |
| 4 | `ERR-RETRY` | evidence 缺/hash mismatch/schema 不过/HEAD stale | 修后 re-dump（attempt_n<2） |
| 6 | `ERR-BYPASS` | bypass 字段缺 / follow-up overdue | 补 trail 后 re-dump |

**exit 5 故意不分配**（`retro_gate.py:61` 注释 + §7.1）。FATAL-class subcode 集合 `retro_gate.py:748-757`（hash/schema/nonce mismatch 等不进计数器）。

### 3.5 mandate 状态机

- **HANDOFF_RETRO_MANDATE**：env 读于 `dump.py:280`。当前阶段 Phase 4c **已拨**（据 MEMORY + 模板 `templates.py:162/313`：三路径 `.zshenv`+`launchctl setenv`+`auto-continue.plist`）。无 evidence 调 dump → exit 4 ERR-RETRY（`retro_gate.py:1294`）。
- **HANDOFF_AUDIT_MANDATE**：env 读于 `dump.py:281`。Phase D 已拨。开则 evidence-None 走独立 audit 计数器（`retro_gate.py:1293-1312`），有 evidence 则跑 G0-G9（`retro_gate.py:1446`）。
- **HANDOFF_RETRO_BYPASS**：紧急 P0 例外（`dump.py:279`），须 `ack/<task>.retro.override.json` 含 `follow_up_retro_task_id`+ISO-8601 `follow_up_deadline`，否则 exit 6（`retro_gate.py:614` `_validate_override`）。
- **attempt 状态机**（§7.2）：`ATTEMPT_MAX=2`（`retro_gate.py:91`），soft-retry 失败 bump 计数器（`_handle_validation_failure` `retro_gate.py:764`），n≥2 写 BLOCKED.md 返回 `retro-attempt-exhausted`（`retro_gate.py:854-882`）。审计计数器 isolated（`<task>.audit.attempt_n.txt`，`retro_gate.py:184`），两个失败预算独立。
- **1-B dump-time re-align**（`retro_gate.py:1118` `_attempt_realign`）：HEAD 因兄弟 commit 漂移时，在已持的 dump.lock 内 CAS-guarded 重绑证据到新 HEAD，不 bump attempt。安全条件全满足才做（session_commits 快照在、git 健康、工作树干净、所有自己的 commit 仍是新 HEAD 祖先，拒 ABA reset）。

---

## 4. 数据流 / 状态流

**输入**：CLI flag（`--task/--next/--status/--retro-evidence/--nonce/--coordinator/--self-task/--open-batch/...`）、env（`HANDOFF_HOME/HANDOFF_RETRO_MANDATE/HANDOFF_AUDIT_MANDATE/HANDOFF_RETRO_BYPASS/CLAUDE_CODE_SESSION_ID/HANDOFF_WINDOW_FOCUS_PATH/PYTEST_CURRENT_TEST/HANDOFF_NO_PBCOPY`）、`$HANDOFF_HOME/config.json`（shared）+ `$HANDOFF_HOME/<project>/handoff.config.json`（per-project head_freshness/follow_up，`retro_gate.py:176`）、git 工作区状态。

**处理**：STOP 闸 → 复盘闸（证据校验/HEAD 新鲜度/re-align/审计 G0-G9）→ preflight 闸 → worktree/singlepane 解析 → 模板渲染。

**输出 / sidecar / 锁全清单**（路径以 `$HANDOFF_HOME/<project>/` 为根，除全局哨兵）：

读/写：
- `queue/<task>.md` — 交接棒（写，`atomic_replace`，`dump.py:871`）
- `queue/<task>.uri` — launchd 触发器（写最后，含 `WORKSPACE=/URI=/SPAWNER_FOCUS=`，`dump.py:1037`；终态删，`dump.py:876/895`）
- `queue/<task>.done` / `queue/<task>.BLOCKED.md` — 终态标记（写，`dump.py:875/886`；BLOCKED 也由 retro_gate `_write_blocked_md` `retro_gate.py:364` 写）
- `queue/<task>.heartbeat` — 终态删（`dump.py:880/897`）
- `queue/<task>.singlepane` — singlepane JSON sidecar（**必须单行紧凑 JSON**，bash awk json_get 读，`dump.py:591-612`）
- `<project>/singlepane/<task>.handoff.code-workspace` — 出树 workspace 文件（`dump.py:544`，红顶/nonce/title）
- `ack/<task>.queued` — dump 已跑待 spawn 面包屑（`dump.py:926`）
- `ack/<task>.old_ready` — 审计元数据（retro_evidence_hash/codex_audit_hash/next_session_forced_task/code_repo，`_write_old_ready` `dump.py:1047`，schema 版本 = EVIDENCE_SCHEMA_VERSION `dump.py:50`）
- `ack/<task>.worktree` — worktree 回收元数据（`dump.py:971`）
- `ack/<task>.singlepane_busy.txt` — singlepane 拒绝面包屑（`dump.py:691`）
- `ack/<task>.spawned`/`.submitted`/`.failed` — launcher ACK（dump 不写，由 watcher 写；`find_orphans` 读 `dump.py:1521`）
- `ack/<task>.retro.attempt_n.txt` — 复盘 attempt 计数器（`retro_gate.py:181`）
- `ack/<task>.audit.attempt_n.txt` — 隔离的审计计数器（`retro_gate.py:188`）
- `ack/<task>.retro.retry_audit.jsonl` — 每 task retry 审计流（`retro_gate.py:192/261`）
- `ack/<task>.retro.warnings.txt` — HEAD 新鲜度警告 sink（`retro_gate.py:195/409`）
- `ack/<task>.retro.override.json` — bypass override（读，`retro_gate.py:199/614`）
- `ack/<task>.audit.override.json` — 审计 bypass override（codex_audit.py 写）
- `ack/<task>.retro_overdue.txt` / `ack/<task>.audit_overdue.txt` — overdue 标记（auto-continue.sh 写，闸读 `retro_gate.py:673`）
- `precheck/<task>.retro.evidence.json` — 复盘证据（precheck 写 `handoff_precheck.py:537`，dump 闸读）
- `locks/precheck.lock` / `locks/dump.lock` — §7.3 有序锁（`retro_gate.py:687`）
- `locks/<task>.retro.attempt.lock` / `locks/<task>.audit.attempt.lock` — 短命计数器锁（`retro_gate.py:724`）
- `locks/<task>.audit.lock` — Phase D 审计评估锁（`retro_gate.py:1455`）
- `.spawn.lock`（project 级，`spawn_lock.project_spawn_lock`）— worktree create + singlepane 并发闸共享（`dump.py:1706/729`）
- `batches/<batch>/manifest.json`+`<sub>.env`+`<sub>.done`/`.blocked`/`.heartbeat`+`fan-in.env`+特殊标记（`_fanin_triggered`/`_corrupted` 等，`dump.py:54-62`/`handle_open_batch` `dump.py:1201`）
- `launched/<task>-<ts>.txt`（launcher 写，orphan 清理读 `dump.py:1529`）
- `_recovery/orphans-<ts>.json`（cleanup 留档，`dump.py:1601`）
- 全局：`$HANDOFF_HOME/done`、`$HANDOFF_HOME/STOP_AUTO`、`$HANDOFF_HOME/<project>/STOP_AUTO`、`batches/<batch>/STOP`（`any_stop_auto` `dump.py:319`）
- macOS 副作用：pbcopy 剪贴板（`_maybe_pbcopy` `dump.py:1160`，pytest/HANDOFF_NO_PBCOPY 跳过）+ osascript 通知（`_notify` `dump.py:1189`）

---

## 5. 现状三态

| 能力 | 三态 | 真实情况 |
|---|---|---|
| 单任务 dump（md+uri+sidecar 排序产出） | 有 ✅ | `write_active_dump` 完整，sidecar-先-uri-后排序由双脑裁定并 test-locked。 |
| 七档 exit code + stderr 前缀 | 有 ✅ | `retro_gate.py:56-130` GateResult.emit 完整，exit 5 故意空缺。 |
| 复盘证据校验（schema/hash/枚举/reason） | 有 ✅ | `_load_evidence` + `_validate_phase_status` 完整；canonical hash 双向自洽。 |
| HEAD 新鲜度三档 + 1-B re-align | 有 ✅ | `_check_head_freshness` + `_attempt_realign`，CAS-guarded，拒 ABA。 |
| attempt 状态机（retro + 隔离 audit 双计数器） | 有 ✅ | `ATTEMPT_MAX=2`，corrupt quarantine + BLOCKED.md，两预算独立。 |
| Phase D codex 审计闸 G0-G9 | 有 ✅ | `HANDOFF_AUDIT_MANDATE` 已拨，audit_mandate 时 `evaluate_audit_gate` 跑（codex_audit.py，本 shard 未深读其内部）。 |
| bypass + override.json 校验 | 有 ✅ | `_validate_override` 强制 follow_up_task（charset 限 `[a-z0-9-]` 防路径穿越 `retro_gate.py:637`）+ ISO deadline。 |
| overdue 扫描器（retro + audit 两 kind） | 有 ✅ | auto-continue.sh:2241-2246 每周期对每项目 `scan_overdue_overrides`，两 kind 都扫；闸 `_check_follow_up_overdue` 两 pattern 都读。 |
| bypass-override 产出器（audit kind） | 有 ✅ | `codex_audit.write_bypass_override` 在 audit-close（codex_audit.py:2568）自动写 `ack/<task>.audit.override.json`。**注意 retro_gate.py:672 注释说它「dormant/deferred」是过时注释**（见 §6）。 |
| fan-out 批 + fan-in（N_max/全局上限/Gate A/错峰） | 有 ✅ | `handle_open_batch` 完整含 batch_dir-vanished 守卫 `assert_batch_alive`。 |
| worktree 隔离（active 路径） | 有 ✅ | `resolve_spawn_workspace` 含 p21 spawn-lock 对称修复，fail-closed 不静默降级。 |
| singlepane 并发硬闸（design §5.4） | 有 ✅ | `singlepane_worker_guard` 持锁拒第二 worker，写 owner-readable busy ack。 |
| 中枢红顶 / SPAWNER_FOCUS focus-jump | 有 ✅ | coordinator 强制 singlepane（MUST-1）+ 红顶；SPAWNER_FOCUS env-independent 自识别 fail-open。 |
| 孤儿检测 + 清理 | 有 ✅ | `find_orphans`/`handle_cleanup_orphan`，--apply 删残留 + _recovery 留档。 |
| `--kill-spawned` 物理关 tab | 半成品 🟡 | 只打印「请手动关」+ 发通知，**不真关 tab**（`dump.py:1615-1625`，IDE tab 标题不带 task_id 做不到）。 |
| `docs/PROTOCOL.md` 反映本子系统现状 | 无 ❌ | 文档停在 schema_version 2 / v1.0.0（2026-05-29），**整个 v5.4 retro/precheck/audit 闸链零文档**（见 §8）。 |
| `--no-dedupe` flag | 半成品 🟡 | 已废弃 no-op，仅为向后兼容接受并忽略（`dump.py:1845`），非真功能。 |

---

## 6. 🔴 半实现陷阱

### 陷阱 1 — `retro_gate.py:670-672` 过时注释把已上线的 audit-overdue 链说成「dormant/deferred」
- **现象**：`_check_follow_up_overdue` 注释写「The audit kind stays dormant until the bypass-override producer lands (deferred, spec §7.3)」（`retro_gate.py:672`）。但实查：产出器 `codex_audit.write_bypass_override` 已在 audit-close（codex_audit.py:2568）live 调用，扫描器 auto-continue.sh:2245-2246 每周期已扫 `audit.override.json`/写 `audit_overdue.txt`，闸 `retro_gate.py:673` 已读 `*.audit_overdue.txt`。整条链已闭环。
- **后果**：注释误导未来维护者以为 audit-overdue 是死代码可删/不必测。**代码行为正确**（闸真会因 audit overdue 返回 exit 6），只是文字落后于实现 = 反向半实现（代码已活、注释说没活）。
- **正解**：把 `retro_gate.py:670-672` 注释更新为「producer 已 land（codex_audit.write_bypass_override，audit-close 自动写），两 kind 均 live」。

### 陷阱 2 — `--kill-spawned` flag 存在但不真关 tab
- **现象**：`dump.py:1820-1824` 注册 `--kill-spawned`「notify user to close tabs」，但实现 `dump.py:1615-1625` 只 print 任务列表 + 发一条 macOS 通知，**从不物理关闭任何 VS Code tab**（注释自承「IDE tab title doesn't carry task_id; manual close needed」）。
- **后果**：flag 名字（kill）暗示自动关，实际要 owner 手动逐个关 → 虚假就绪感；孤儿 tab 仍空转吃资源直到人工介入。
- **正解**：要么改名/改 help 为 `--notify-stale-tabs`（诚实），要么接 v4 autoclose watcher 的 URI 契约真关（该机制存在于 auto-continue.sh，但默认 OFF/opt-in，与本 flag 未打通）。

### 陷阱 3 — mandate 是 env 驱动、本子系统代码内无默认拨闸；「已拨」状态完全靠会话外 env 三路径
- **现象**：`HANDOFF_RETRO_MANDATE`/`HANDOFF_AUDIT_MANDATE` 只在 `dump.py:280-281` 读 env，代码默认全是 legacy（无 evidence → exit 0 放行，`retro_gate.py:1287`）。「Phase 4c/D 已拨」只存在于 `~/.zshenv`+`launchctl setenv`+`auto-continue.plist`（模板 `templates.py:313` 描述，非代码保证）。
- **后果**：若任一 env 路径漂移/未注入（例如某个不经 launchd 的 shell、CI、或 env 被清），dump 会**静默走 legacy 放行**——复盘闸形同虚设却无任何告警。这正是「UI 上有、运行时为空」的虚假就绪：闸的存在 ≠ 闸在跑。验证层级（env 真注入）≠ 代码层级。
- **正解**：本子系统无法在代码内根治（env 是设计契约）；缺口在「mandate 生效性」缺运行时自证。可加一个开张自检（新会话 §0 探针确认 `HANDOFF_RETRO_MANDATE` 在 agent 命令 shell 真可见），否则「闸已拨」是断言非证据。

### 陷阱 4 — `old_ready` 写失败只 print 警告、不阻断已发布的 dump
- **现象**：`dump.py:948-957`，retro evidence 通过但 `_write_old_ready` 返回 None（证据在闸与此处之间消失/不可读）时，仅 print 一条 `⚠️` 不返回非零。
- **后果**：`.uri` 此时尚未发布（old_ready 在 §7.6 排序里先于 uri），但代码继续往下走到发布 `.uri` → spawn 的新会话 §0 审前任时找不到 `old_ready` → 无法验证前任 = 复盘链在这一棒断开却无人知。
- **正解**：属设计取舍（不让「已发布的 dump」回滚），但「loud print」在自动接续里没人看 → 实质静默。可改为这种情况下也不发布 `.uri`（fail-closed），与 worktree 闸的 BLOCKED 对称。

### 陷阱 5 — `docs/PROTOCOL.md` 是空壳级过时（详见 §8，归类为「文档半实现」）
- **现象**：协议文档自称权威 on-disk 规范，却完全没有 retro/precheck/evidence/mandate/七档 exit/old_ready/locks 任何内容。
- **后果**：任何「conforming producer/consumer」按文档实现都会缺整个 v5.4 闸层 = 文档承诺的「可替换实现」对当前系统不成立。
- **正解**：补 §11 retro-evidence gate + §12 codex-audit gate 章节，或在文首标注「v5.4+ 闸协议见 spec draft，本文档仅覆盖 schema_version 2 基础层」。

---

## 7. 承重事实 file:line 清单（供回读核验）

- 七档 exit code 常量（含 exit 5 故意空缺）：`retro_gate.py:56-61`
- stderr 前缀常量：`retro_gate.py:63-68`
- `GateResult.emit` 写 stderr 前缀行：`retro_gate.py:121-130`
- 闸三触发器 + legacy 跳过判断：`dump.py:279-301`
- 终态 done/blocked 无 evidence 豁免（有 evidence 则继续校验）：`dump.py:276`
- `check_retro_gate` 公共入口：`retro_gate.py:1226`
- evidence 加载 + 自哈希校验：`retro_gate.py:419-460`
- canonical JSON 哈希（排序键/无空格/排除 hash 字段）：`handoff_precheck.py:189-211`
- phase 状态枚举 + 非 ✅ 必带 reason 校验：`retro_gate.py:463-501`
- HEAD 新鲜度三档（match+drift / drift-tolerance / stale action）：`retro_gate.py:534-608`
- 1-B re-align（CAS-guarded、拒 ABA、不 bump attempt）：`retro_gate.py:1118-1220`
- attempt 状态机 ATTEMPT_MAX=2 + BLOCKED.md：`retro_gate.py:91`、`retro_gate.py:854-882`
- 隔离 audit 计数器路径 + 失败路由：`retro_gate.py:184-188`、`retro_gate.py:924-1072`
- bypass override.json 校验（follow_up_task charset `[a-z0-9-]` 防穿越 + ISO deadline）：`retro_gate.py:614-654`
- overdue 闸读两 kind（retro + audit）：`retro_gate.py:657-680`
- §7.3 有序锁 precheck→dump：`retro_gate.py:686-712`
- `write_active_dump` sidecar-先-uri-后排序契约：`dump.py:909-916`、`dump.py:1037-1039`
- `_write_old_ready` 审计元数据（含 codex_audit_hash / next_session_forced_task / code_repo）：`dump.py:1047-1157`
- old_ready 写失败仅 print 不阻断：`dump.py:948-957`
- precheck evidence builder（schema 5.5.0 / session_commits / phase 默认 skip）：`handoff_precheck.py:315-372`
- session id 解析（CLAUDE_CODE_SESSION_ID 主键 + 指纹 fallback）：`handoff_precheck.py:154-183`
- SUPPORTED schema 版本集（5.5.0 + v5.4.1 迁移窗）：`handoff_precheck.py:35-40`
- `--coordinator` × 批 flag 机器拒：`dump.py:1879-1890`
- singlepane 并发硬闸：`dump.py:708-737`、`dump.py:2024-2057`
- worktree 解析 + p21 spawn-lock 对称修复 + fail-closed：`dump.py:1669-1764`
- `--kill-spawned` 不真关 tab：`dump.py:1615-1625`
- fan-out N_max/全局上限/Gate A/错峰：`dump.py:1222-1241`、`dump.py:1294-1296`
- §0 审前任模板（含 forced-follow-up 校验）：`templates.py:148-181`

---

## 8. 与 docs/PROTOCOL.md 的出入

`docs/PROTOCOL.md`（289 行，2026-05-29，schema_version 2 / v1.0.0）严重落后于本子系统当前实现。grep 全文 0 命中 retro/precheck/evidence/mandate/exit/old_ready/overdue/attempt（已实测确认）。具体 gap：

1. **整个 v5.4 retro-evidence 闸层零文档**：七档 exit code、stderr 前缀、attempt 状态机、HEAD 新鲜度、re-align、bypass/override、overdue 扫描——全无。文档 §5 state machine 只画了 active/done/blocked 三态，没有「闸拒绝 → ERR-RETRY/BLOCKED」这一整层。
2. **`precheck/` 目录缺失**：文档 §1 root 布局列了 queue/batches/ack/launched，**没有 `precheck/`、`locks/`**（实际承载 evidence.json + 所有锁）。
3. **`ack/` 内容不全**：文档 §1 只列 `.spawned/.submitted/.failed`，实际还有 `.old_ready/.queued/.worktree/.singlepane_busy.txt/.retro.attempt_n.txt/.audit.attempt_n.txt/.retro.retry_audit.jsonl/.retro.warnings.txt/.retro.override.json/.audit.override.json/.retro_overdue.txt/.audit_overdue.txt`。
4. **`.uri` 格式过时**：文档 §3.2 说是「两行」`WORKSPACE=/URI=`，实际是三行（多了 `SPAWNER_FOCUS=`，`dump.py:1038`）。
5. **worktree 隔离 / singlepane / 中枢红顶 / coordinator succession 全无**：文档无任何 per-session worktree、singlepane 并发闸、`--coordinator` 概念。
6. **watchdog 模式不全**：文档 §7 列 4 模式（last-one-out/stale-heartbeat/fan-in-stalled/orphan-sub-task），与 MEMORY 提及的 mode 5/6（singlepane spawn 鉴别、529-suspected）不一致——文档版本更早。
7. **schema_version 仍写 2**：retro evidence 自己的 `EVIDENCE_SCHEMA_VERSION` 是 `5.5.0`（`handoff_precheck.py:35`），文档完全未提这套独立版本号。

**结论**：PROTOCOL.md 仅可信用于「fan-out 批 + file_ownership Gate A + 原子性保证 + ACK 协议」这部分基础层（这些仍与代码一致）；**v5.4+ 的复盘/审计闸链以代码 + spec draft（`project-files/handoff/v5.4-retro-mandate-draft.md` / `codex-audit-gate-spec-draft.md`）为权威**，文档需补章节或加显式「过时范围」标注。
