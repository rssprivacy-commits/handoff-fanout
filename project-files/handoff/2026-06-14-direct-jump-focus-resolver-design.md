# 派窗直跳「焦点源」重构 — 全面架构方案（锚定实施）

> # ✅ REVERSAL 终裁（2026-06-14 sw-coord-p21 / owner「忽视 codex、gemini+deepseek 权威推进」/ 外双脑双 GREEN + ultracode workflow env-channel lens 印证）
> **根因成立、p18「证伪」过早；本方案 path-resolver/marker 大重构被 MP 简化方案取代（非作废于「根因错」）。**
> - **根因成立（reversal PASS）**：下方「`terminal.integrated.env.osx` 进不到 launchd-spawn 的 singlepane agent 命令 shell」**正确**。p19 + **p21（本会话中枢 agent shell `env` 独立第二次复现：有 HANDOFF_HOME/CLAUDE_SESSION_ID、无 ROLE/TASK/FOCUS）** 闭合证据链。**p18「证伪」过早**——它从 VS Code **集成终端手敲 claude 的 squat**（集成终端确继承该 env）实测，=非代表性启动路径、过度外推到 launchd-spawn 自动化路径。gemini+deepseek 双 GREEN 终裁、workflow env-channel lens 亦判「通道不达 agent shell」印证。
> - **但方案演进（非「根因错」作废）**：本稿 Part A 的 `resolve_spawner_focus` path-resolver + Tier-2 marker **自注册大重构**被 **MP 简化方案取代** = 「发起端自报 Desktop 数字 `SPAWNER_DESKTOP=N`（worktree 彻底砍 resolver）+ singlepane 保留**硬化 marker**（含 VSCODE_PID+nonce+存活校验，因 Electron 多窗共享主 PID、winlist 不暴露 PID → marker 是唯一 env-independent 可靠解）」。详现行设计稿 `2026-06-14-mp-style-locate-dispatch-return-design.md`。
> - **dump 路径 SPAWNER_FOCUS 缺口已修**（djs-dump-path WIP 救活，合 main `cbc0c16`）。
> - **保留下方 p18 原 banner + 原文作调查史**（p18↔p19↔p21 争议演化记录，不删）。
>
> ---
>
> # 🛑🛑 [已被 p21 reversal 订正 / 仅存史] 已作废 / 根因被证伪（2026-06-14 sw-coord-p18 / 外双脑 gemini+deepseek 双 GREEN + 受控 E2E）
> **本方案下方的核心根因（「`terminal.integrated.env.osx` 进不到 agent 命令 shell」）已被直接证据证伪——不要据此实施 marker 自注册大重构。**
> - **证伪**：p18 中枢在自己窗口实测 agent Bash shell `env` **确有** `HANDOFF_SESSION_TASK=sw-coord-p17`（逐窗口唯一值），全局源（launchctl/.zshenv/plist）全空，grep 证唯一写入者就是 `terminal.integrated.env.osx`(worktree.py:525) → **该通道确实到达 agent shell**。FOCUS 缺席只因测试窗都是**旧引擎（8cf06a4，早于 43230f5）**派的、压根没写 FOCUS；p17 自测从旧引擎窗跑=无效测试。新引擎 djs-dump-path 窗的 workspace JSON 已实证有自己的 FOCUS。
> - **受控 E2E（无窗）结论**：dx-spawn/worker 派窗路径**全链已验证可工作**（真实路径过 spawn.py 验证闸 ACCEPT + 单测 2 passed + dx-spawn 条件捕获 + launcher 解析 export；仅差最终视觉跳桌=下次新引擎中枢派 worker 自然可观察）。
> - **真实残留缺口 = 只有 dump 路径未接线 SPAWNER_FOCUS**——而停摆的 `djs-dump-path` worker WIP（`spawner_focus.py` 共享安全闸 + dump.py 3 写入点接线 + spawn.py 复用，读 `HANDOFF_WINDOW_FOCUS_PATH` env）**正是此小缺口的正确修法、且被以假根因错误叫停 → 应救活复核完成，非 marker 重构**。
> - **正确路径**：救活 djs-dump-path WIP（小改 / 共享单点闸）+ 自然视觉确认 dx-spawn + 本设计稿作废。两脑判 marker 自注册=架构反模式。详 `~/.claude-handoff/handoff-fanout/audits/p18-rootcause-refutation.md`(双脑 raw) + open-loops sw-coord-p18 段。
> - **保留本文下方原文仅作调查史**（dump 路径思路部分正确、已被 WIP 吸收）；marker 自注册 / Tier-2 自注册章节**不实施**。
>
> ---
>
> **（以下为原设计稿原文 / 根因已证伪 / 仅存史）**
>
> **状态**: ~~设计稿 / 待外双脑审~~ → **作废（根因证伪，见上）**。
> **缘起**: 已上线的「派窗直跳桌面」(hf `43230f5` + dharmaxis `a1191cb`) 实测未直跳——**真因=测试窗是旧引擎派的无 FOCUS env，非通道坏（见上）**。
> **锚**: 新会话/worker **先读上方作废说明**；下方原文勿据以实施 marker 重构。

---

## 🎯 目标锚点（每里程碑回读 / 禁漂移）

- **原始目标**：当窗口 W（活动中枢/任一派发方）派出子窗口 C（worker / 下一棒）时，C 要**一步原生直跳**落在 **W 当前所在的桌面**（非项目固定桌面、非逐格滑动）。恢复 owner「瞬间定位」体验。
- **本方案只做**：把「直跳的焦点源」从**坏掉的通道**（`HANDOFF_WINDOW_FOCUS_PATH` via `terminal.integrated.env.osx`，到不了 agent shell）换成 **agent 命令 shell 真正能可靠读到的通道**；并让**所有派窗路径**（`handoff spawn` + `handoff dump` 3 写入点）都写 `SPAWNER_FOCUS`。
- **不做**（防膨胀）：不改 code-router/vscode-spaces 的 focus-jump 跳转逻辑本身（已审 GREEN、对的）；不碰 retro/audit gate；不碰冷窗 Enter 提交友（独立 backlog）；不引入私有 API。
- 🔴 每里程碑只核一句：「派窗口现在能落在派发方的当前桌面了吗？只代码对≠真工作——必须真派一个窗看它跳。」

---

## 🔬 根因（已实地坐实 / 2026-06-14 sw-coord-p17）

直跳链：W 派 C → 派窗命令（在 W 的 **agent 命令 shell** 跑 `dx-spawn`/`handoff spawn`/`handoff dump`）须知道 **W 自己的 `.handoff.code-workspace` 路径** → 写进 C 的 `.uri` 第三行 `SPAWNER_FOCUS=` → 看门狗 export `HANDOFF_SPAWNER_FOCUS` → code-router `code <W 的 workspace>` 原生直跳到 W 的桌面 → C 在此出生。

**断链点**：W 的 workspace 路径靠 `HANDOFF_WINDOW_FOCUS_PATH`（经 `terminal.integrated.env.osx` 注入）传递——**但该 env 只进 VS Code「给人用的集成终端」，进不到 Claude Code agent 跑命令的那个 shell**（Bash 工具 = profile 初始化的独立 shell）。

**实证**（Phase 0）：
- 中枢窗 workspace JSON 里有 `terminal.integrated.env.osx: {ROLE, TASK}`，但 agent shell `env` 里 **ROLE/TASK/FOCUS 全空**。
- agent shell **有**：`CLAUDE_SESSION_ID`、`VSCODE_PID`、`HANDOFF_HOME`、`PWD`（这些走 launchctl/.zshenv/VS Code 进程 env=被 agent shell 继承）。
- memory-guard dry-run log 全 `source=cwd`/`task=-`（同源 no-env，那功能也从没拿到此 env）。

**通道结论**：`terminal.integrated.env.osx` ❌ 不可用作 agent 通道；**可用通道 = cwd(`$PWD`) / `$CLAUDE_SESSION_ID` / `$HANDOFF_HOME` / 磁盘文件**。

**与 memory-guard 同源**：Step3 角色门 env 信号(ROLE/TASK)同样到不了 agent → 一直 cwd 兜底。**本重构若把「身份/焦点」统一改为 agent-shell-可读通道，可顺带为 memory-guard 角色门提供可靠 env 源**（设计兼容点，非本方案必做，留观察）。

---

## 🏗️ 架构：`resolve_spawner_focus()` 焦点解析器（引擎自解析 / 分层 / fail-open）

核心：**引擎（spawn.py/dump.py）自己从可靠通道解析 W 的焦点路径**，不再依赖 dx-spawn 透传那个坏 env。

```
resolve_spawner_focus(cwd, env, cfg) -> validated_realpath | None
  Tier-1 worktree（覆盖 ERP/dharmaxis worker_isolation=worktree 中枢=主诉）：
    cand = realpath(cwd + "/.handoff.code-workspace")
    cand 存在 + 过验证闸 → 返回 cand
  Tier-2 singlepane（覆盖 p17/wilde-hexe/sdgf/xunyin/fb 单栏中枢）：
    sid = env.get("CLAUDE_SESSION_ID")
    marker = $HANDOFF_HOME/<project>/focus/<sid>.focus   # 自注册写入(见下)
    marker 存在 + 内容过验证闸 → 返回其内容
  Tier-3 fail-open：
    返回 None → 不写 SPAWNER_FOCUS → code-router 走既有 goto（=今天行为、零回归）
```

**验证闸（复用 spawn.py 现有 / 抽共享 helper 单点）**：`isabs` + `realpath` + `.handoff.code-workspace` 后缀 + `isfile` + 限定 allowed roots（`cfg.home`/`~/.claude-handoff`/tmp）。**安全闸单点**，spawn.py 与 dump.py 共用同一份。

### Tier-2 singlepane 自注册（关键新增）
singlepane 窗 cwd=仓库根=共享，cwd 推不出 workspace → 需窗口**自注册** focus marker：
- **谁写**：window 开张时的 **SessionStart hook**（扩展现有 Step3 `dx_session_role.py` 身份感知 hook——它已在 SessionStart 跑、已做 singlepane sidecar 身份匹配）。
- **写什么**：`$HANDOFF_HOME/<project>/focus/<CLAUDE_SESSION_ID>.focus` = 本窗 workspace realpath。
- **怎么拿本窗 workspace 路径**：hook 复用 Step3 已有身份解析（singlepane sidecar 匹配 / 首条消息 🆔<task> 解析）得 task → 构造 `$HANDOFF_HOME/<project>/singlepane/<task>.handoff.code-workspace`。worktree 窗 hook 走 cwd 推导即可（也可一并写 marker 做统一，但 Tier-1 已覆盖、非必须）。
- **生命周期**：marker 按 CLAUDE_SESSION_ID 命名 → 会话结束/换会话自然失效；加 TTL 清理（巡检顺手）防堆积。`/resume` 同 session-id → marker 仍有效（韧性）。

> **设计取舍记录**：① 否决「engine 在 spawn 窗时预写 marker」——engine 在 spawn 时不知未来 session-id，singlepane 无法预写。② 否决「靠 agent 记住自己路径 + 显式传 --spawner-focus-path」——跨 compaction 脆弱。③ 选「自注册 marker（session-id 键）+ cwd 推导（worktree）」——不靠 agent 记忆、用 shell 真有的稳定键。

---

## 🔧 实施分解（worker 任务 / 外科手术）

1. **抽共享验证 helper**（reuse 安全闸单点）：spawn.py `run_spawn` 的 spawner-focus 验证 → 抽 `validate_spawner_focus(raw, *, cfg) -> str|None`（共享模块或 spawn.py 导出）。spawn.py 改调用、**行为字节级不变**（既有 spawn 测试全绿=回归红线）。
2. **`resolve_spawner_focus(cwd, env, cfg)`**：实现 Tier-1/2/3（上）。
3. **spawn.py 接入**：`run_spawn` 改为 `spawner_focus = resolve_spawner_focus(os.getcwd(), os.environ, cfg)`（替代「dx-spawn 透传 --spawner-focus-path 读坏 env」）。`--spawner-focus-path` arg 可保留作显式覆盖（测试/特殊），但默认走 resolver。
4. **dump.py 接入**：3 个 .uri 写入点（`write_active_dump` L998 / sub-task fan-out L1274 / fan-in L1358）→ 都 `resolve_spawner_focus` + 追加 `SPAWNER_FOCUS=`（缺省/None 时字节级不变=向后兼容）。
5. **singlepane 自注册 hook**：扩展 Step3 `dx_session_role.py`（或新 SessionStart hook）写 `focus/<sid>.focus` marker（仅 singlepane 必需；worktree 可选）。**注意**：hook 改动在 cc-global/dharmaxis 侧，跨仓——需与 hf 引擎改动协调（分两 worker 或一 worker 跨仓，按派会话纪律）。
6. **dx-spawn-session.sh 收尾**：删/废它读 `$HANDOFF_WINDOW_FOCUS_PATH` 透传那段（已坏、引擎自解析后冗余）；coord 同步路径同理。
7. **`terminal.integrated.env.osx` 的 HANDOFF_WINDOW_FOCUS_PATH 注入**：保留无害（人用终端可读）或移除（agent 不用）——worker 评估，倾向保留+注释「非 agent 通道」。

---

## 🚦 红线 / 不变量

1. **fail-open（最高）**：focus 解析任何失败/缺失 → 省略 SPAWNER_FOCUS、派窗照常、退既有 goto。绝不阻断派窗。
2. **字节级向后兼容**：无 valid focus 时 .uri/workspace 与改前逐字节一致（既有测试全绿）。
3. **安全闸单点**：验证闸只一份、spawn+dump 共用。allowed roots 不放宽。
4. **🔴 禁碰 install/**（post-commit 部署陷阱）——本方案在 `src/`（editable=合即 live）。动 install/ 须先 `export HANDOFF_INSTALL_SH=/nonexistent`、commit 后验 live sha 未变。
5. **§6b**：worker 禁自合/自审/自派/写共享 memory。
6. **不引私有 API / 不碰 focus-jump 跳转逻辑（已审对）/ 不碰 retro·audit gate**。

---

## ✅ 验证（🔴 验证层级=用户体验层级 / 本次惨痛教训）

**本功能首次「上线」失败的根因 = 全员只审「代码在 env 存在时对不对」、没验「env 在 agent 真实执行环境有没有」。** 故本方案验收**必须到真实派窗层**：
- **Tier-1 worktree E2E**：真从一个 worktree 中枢派 worker → 看 code-router.log 出现 `focus-jump OK`（非 goto）+ 子窗落在中枢桌面（owner 观察 / active_index 实证）。
- **Tier-2 singlepane E2E**：真从一个 singlepane 中枢（如下一棒中枢窗）派 worker → 同上。
- **marker 自注册验**：开窗后 `focus/<sid>.focus` 真被写 + 内容=本窗 workspace。
- 单测：resolver 三 tier + 验证闸 + 3 dump 写入点 + 字节兼容；全量引擎套件绿。
- ⚠️ **不接受**「单测过=完成」——必须真派窗看 focus-jump 真触发（owner 在环观察）。

---

## 🔍 审计计划

- **设计审**（本文）：外双脑（**codex 6-15 恢复后必审**——AX/IPC/会话生命周期/时序是 codex 最强域、本类改动它缺席=关键盲区；gemini+deepseek 先审不阻塞）。重点质疑：① cwd 推导对所有 worktree 窗都成立吗（有无 cwd≠worktree 的边界）② session-id marker 跨 /resume·compaction·并发同项目多窗 的正确性与竞态 ③ marker TTL/清理与 stale marker 误用 ④ 自注册 hook 在 SessionStart 时机能否可靠拿到 task/workspace。
- **实施审**：worker 交付后中枢独立外双脑 + 亲跑测试 + **真派窗 E2E**。

---

## 📌 当前状态联动

- 上一版 djs-dump-path worker（只改 dump 写 SPAWNER_FOCUS、读坏 env）= **方向错已停**（`.done`+`.stopped.txt`）。其「dump 写 SPAWNER_FOCUS」思路被本方案吸收（但读 resolver 非空 env）。
- live 现状：直跳 fail-open、退 goto、零回归（没弄坏东西、只是没工作）。本方案不急、非阻断业务。
- codex 6-15 补审债叠加本设计审（连同 sw-sp-rc6-codex-reaudit + 并发立法 + direct-jump-spawn 两侧）。
