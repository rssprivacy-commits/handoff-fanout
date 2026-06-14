# 派窗「定位中枢桌面 → 派出 → 秒回 owner 原桌面」设计稿（MP 式 locate-act-return / env-independent）

> **状态**: ✅ **设计定稿（2026-06-14 sw-coord-p21 / owner「忽视 codex、gemini+deepseek 权威推进」）** → 外双脑双 GREEN 终裁 + ultracode workflow 对抗层 → **可派 worker 实施**（3 refinement 采纳 + 2 个 live return-leg P2 一并修，见 §终裁）。原「待 codex 6-15」已被 owner 裁决取代。
> **缘起**: owner「当我在其他桌面时，中枢派 worker / 接续中枢，能一步定位中枢自己所在桌面、把子窗派在那、然后**马上把我的视图切回我原来工作的桌面**」。owner 指认 **MP（MindPersist）已实现此范式**，要求调研后适配。
> **锚**: 本稿 = 复活 `2026-06-14-direct-jump-focus-resolver-design.md` 的 resolver（p19 实证其根因正确、p18 作废可能过早）**+ 新增 MP 式 return-to-origin 腿**。

## 🎯 北极星（每里程碑回读）
当中枢 W（在桌面 A）派子窗 C（worker / 下一棒）而 owner 在桌面 B 工作时：
1. C **一步原生落在 W 当前桌面 A**（非项目固定桌面、非逐格）；
2. 派完**立即把 owner 视图切回桌面 B**（owner 几乎无感、不被拽走）。
🔴 只核一句：「真派一个窗，子窗落在中枢桌面了吗 + owner 视图回到原桌面了吗？只代码对≠真工作——必须真派窗看（owner 在环）。」

## 📍 当前态（本会话实证 / 全 env-independent / 已 live 验证）
- **M1（项目→桌面 goto 路由）= 全 live**：`code-router.sh` 读 `projects.json` → `vscode-spaces.py goto N`。router.log 近 3 天 87/87 `✅ goto`、零失败。**子窗确定性落到项目映射桌面、视图切过去**——但落的是**项目桌面（如 5）非中枢实际桌面（6）**，且**不回原桌面**。
- **M2（SPAWNER_FOCUS 直跳=落在中枢桌面）= 从没真触发**：router.log `focus-jump` = **0**。断点=`HANDOFF_WINDOW_FOCUS_PATH`（经 `terminal.integrated.env.osx`）**进不到 agent shell**。
- **🔴 p19 直接实证 = 复活旧 resolver 设计的根因**（与作废稿 line 36-41 逐条吻合）：我 singlepane 中枢 agent shell `env`——**有** `CLAUDE_SESSION_ID=2a46e192…` / `VSCODE_PID=5905` / `HANDOFF_HOME` / `PWD`；**无** `HANDOFF_SESSION_ROLE/TASK`（terminal.integrated.env.osx 注入的）/ 无 `HANDOFF_WINDOW_FOCUS_PATH`。→ **作废稿的「通道结论」正确：terminal.integrated.env.osx ❌ 不达 agent；可用通道 = cwd/$CLAUDE_SESSION_ID/$HANDOFF_HOME/磁盘文件**。p18「证伪」基于它**从集成终端手敲 claude 的 squat**（集成终端确继承该 env）=非代表性启动路径，**过度外推**。
- **linchpin 已验证可行（本会话 agent shell 实测）**：① `winlist --spaceinfo` → `active_index=8`（=owner 当前桌面=return 目标）② `winlist --spaces-of-windows` → 我中枢窗（nonce c24d108ef5008ae0）实测**在桌面 6**（=W 桌面=birth 目标）。**两者 agent shell 都能拿、零 env 依赖**。
- **MP 范式**（`mindpersist/src/agent/sender.py`）：记原点(前台 App+桌面+剪贴板)→ wait_for_idle → `Ctrl+数字键`一步跳目标桌面 → 操作 →`finally` 切回原桌面+还原前台。证明 **locate-act-return 范式可行且 env-independent**（键码+前台探测、不碰 env）。⚠️ MP 的 `Ctrl+数字`只到桌面 6（owner 有 14 桌面）→ 借**范式**不借该 primitive，跳转改用 vscode-spaces `goto`（任意桌面+`space_info()` 闭环）。

## 🏗️ 设计 = Part A（复活 resolver / 落在中枢桌面）+ Part B（新增 return-to-origin / 秒回 owner）

### Part A — resolver（解决「子窗落在中枢自己的桌面」/ 复活作废稿 §架构）
完全复用作废稿 `2026-06-14-direct-jump-focus-resolver-design.md` §架构 的 `resolve_spawner_focus(cwd, env, cfg)`：
- **Tier-1 worktree**（ERP/dharmaxis worker_isolation=worktree 中枢）：`realpath(cwd + "/.handoff.code-workspace")` 过验证闸 → 返回。cwd agent 一定能读=可靠。
- **Tier-2 singlepane**（p19/wilde-hexe/sdgf/xunyin/fb 单栏中枢）：`marker = $HANDOFF_HOME/<proj>/focus/<CLAUDE_SESSION_ID>.focus`（SessionStart hook 自注册写入本窗 workspace realpath）→ 过闸返回。
- **Tier-3 fail-open**：None → 不写 SPAWNER_FOCUS → 退既有 goto（=今天行为、零回归）。
- 写进 `.uri` `SPAWNER_FOCUS=` → 看门狗 export → **复用现有 `code-router.sh` focus-jump**（已审 GREEN，不改跳转逻辑本身）。
- 实施分解 / 验证闸单点 / 红线 = 见作废稿 §实施分解 + §红线（逐条仍适用）。**唯一改动 = 把 🛑 作废 banner 的判断交 codex-6-15 重裁**（见审计计划）。

### Part B — return-to-origin 腿（🆕 owner 新需求 / MP 贡献 / 作废稿无此腿）
作废稿只「跳到 W、停在 W」；owner 要「跳到 W 派完、**秒回 B**」。在**做 focus-jump/goto 的那一处**（`code-router.sh` 调 `vscode-spaces.py` 前后 / 或 vscode-spaces 内）加 capture/restore 包壳：
```
origin = winlist --spaceinfo .active_index          # 1. 记 owner 当前桌面 B（派窗前）
focus-jump(SPAWNER_FOCUS) 或 goto(project-desktop)   # 2. 跳到 W 桌面 A（既有逻辑）
code -n <worker workspace>                           # 3. 子窗在 A 出生（既有逻辑）
<确认子窗已在 A 落地>                                  # 4. 🔴 race 关口（见风险①）
goto(origin=B)                                       # 5. 秒回 owner 原桌面 B
```
- 新原语全部已验证可行：`winlist --spaceinfo`（记 B）/ `vscode-spaces.py goto B`（回 B、任意桌面）。
- **开关**：return-to-origin 默认行为待 owner 定（可能不是所有派窗都想回——如 owner 本就想看着子窗出生）。建议 config flag `return_to_origin_after_spawn`（默认 on，符合 owner 主诉）+ 当 origin==target 时跳过（无需回）。

## 🔀 四派发变体
- **V1 dx-spawn 派 worker / V2 引擎 dump 派 worker / V4 audit-close 接棒**：均经引擎 resolver（Part A）+ 看门狗 return 腿（Part B）。
- **V3 dx-spawn --coordinator**：dx-spawn 自身 focus-jump（已有）+ 同加 return 腿（脚本侧 capture/restore）。
- 跨仓协调：引擎 resolver+marker hook 在 hf+cc-global/dharmaxis 两侧（SessionStart hook）；return 腿在 dharmaxis code-router/vscode-spaces。按派会话纪律分 worker 或跨仓单 worker。

## 🚨 风险 / 留给 codex-6-15 的硬问题（spawn/桌面竞争/会话生命周期=codex 最强域）
1. **🔴 race①（最risky / Part B 核心）**：第4步「确认子窗已在 A 落地」——`code -n` 异步开窗，若第5步 `goto B` 早于窗口在 A 渲染完 → 子窗可能落 B 或撕裂。需定**确认机制**（轮询 winlist 直到新窗现身于 A？超时 fail-open 不回？）。MP 靠固定 delay，对 VS Code 冷启动可能不够。
2. **singlepane marker 并发/竞态**（作废稿审计点②③）：同项目并发多 singlepane 窗、`/resume` 同 session-id、marker TTL/stale 误用、SessionStart 时机能否可靠拿 task→workspace（cwd 歧义下 `_scan_singlepane_supervisor` 只给 suspected）。
3. **14 桌面 goto 可靠性**：owner `desktop_total=13~14`，goto 跨远距离桌面（如 8→6→8）的 `space_info()` 闭环在大量桌面下的稳定性/耗时。
4. **owner mid-flight 移动**：capture B 后 owner 手动切到 C，第5步把他拽回 B（旧位）——可接受？或 return 前重读 active？
5. **作废-reversal 本身**：codex 裁「terminal.integrated.env.osx 在 auto-spawn 路径究竟达不达 agent」+「resolver 重构是否其实是对的修法、p18 作废是否过早」。证据包=`audits/p19-env-channel-observation.md` + 本稿 + 作废稿。
6. macOS 前提（goto 不需 Ctrl+数字快捷键、纯 ctrl+arrow 步进，无 MP 那个 6 桌面上限问题——确认 vscode-spaces 现状）。

## 🚦 红线 / 不变量（继承作废稿 + 新增）
1. **fail-open 最高**：resolver 任何失败→退 goto；return 腿任何失败→停在当前桌面（不阻断派窗、不撕裂）。
2. **字节级向后兼容**：无 valid focus + return off 时 .uri/行为逐字节同今天（既有测试全绿）。
3. **安全闸单点**（spawn+dump 共用 `validate_spawner_focus`，allowed roots 不放宽）。
4. **🔴 禁碰 install/**（post-commit 部署陷阱）；本设计引擎部分在 src/（editable=合即 live）；改 install/ 须 `export HANDOFF_INSTALL_SH=/nonexistent`+验 live sha。
5. **§6b**：worker 禁自合/自审/自派/写共享 memory。
6. **不碰 retro/audit gate；不引私有 API；不改 focus-jump 跳转逻辑本身（已审对，只加 capture/restore 包壳）**。

## ✅ 验证（🔴 验证层级=用户体验层级 / 本功能上次「上线」就栽在只审代码不验体验）
- **Tier-1 worktree E2E + Tier-2 singlepane E2E**：真派窗 → router.log 出 `focus-jump OK`（非 goto）+ 子窗落在中枢桌面（winlist 实证）+ **owner 视图回到原桌面**（active_index 实证 + owner 在环肉眼）。
- marker 自注册验：开窗后 `focus/<sid>.focus` 真被写+内容=本窗 workspace。
- 单测：resolver 三 tier + 验证闸 + 3 dump 写入点 + return 腿（capture/restore + origin==target skip + fail-open）+ 字节兼容；全量引擎套件绿。
- ⚠️ 不接受「单测过=完成」——必须真派窗看子窗落中枢桌面 + 视图秒回（owner 在环）。

## 🔍 审计计划
- **设计审 = codex-6-15 authoritative**（item #3 已含「direct-jump 两侧实际态 + 作废是否过早」，本稿并入）：codex 裁 race①确认机制 / singlepane marker 竞态 / 作废-reversal / 跨桌面 goto。gemini+deepseek 先初审不阻塞（refine before codex）。
- **实施审**：worker 交付后中枢独立外双脑 + 亲跑测试 + **真派窗 E2E**（机器审计闸 LIVE：合 hf main 需 audit evidence）。
- **codex-6-15 批次**（恢复 03:52 本地）：本设计审 + sw-sp-rc6-reaudit + 并发立法安全审 + direct-jump 两侧实际态 + djs-dump-codex-reaudit（5 笔，本稿是第 5/与 #3 合并）。

## ✅ 终裁 + refinement（2026-06-14 sw-coord-p21 / owner「忽视 codex、gemini+deepseek 权威」/ 外双脑双 GREEN + ultracode workflow 对抗层）

> ⛔🔴 **[sw-coord-p22 纠偏 2026-06-14 / owner 实证「切到中枢桌面不是一步到位」+「立即纠偏」/ 中枢亲读 vscode-spaces 源码坐实]**：下方 **refinement #1「自报数字→`goto N`」+ 终裁 verdict ② 已作废**。根因=`goto N` 是**逐格**（`send_ctrl_arrow` Ctrl+方向键一格格走，[vscode-spaces.py:151-188]），违反北极星「非逐格、一步原生」。**正解=发起端自报 workspace【路径】走既有 `focus-jump`（`code <workspace>` 重激活已开窗→macOS 一滑直达=真·一步，[vscode-spaces.py:566-620]）**，详 refinement #1 内联纠偏。

**终裁 verdict**：gemini=**GREEN** / deepseek=**GREEN** / **可定稿派 worker**。两脑**终裁共识**：① 作废-reversal=**PASS**（p19+**p21 独立第二次复现** env 通道不达 launchd-spawn singlepane agent shell；p18 集成终端 squat 过度外推）② ~~Part A 简化（发起端自报 `SPAWNER_DESKTOP=N`）=PASS~~ **[作废→见 p22 纠偏：自报数字走 goto 是逐格，改自报路径走 focus-jump]** ③ **singlepane 自识别硬核两脑收敛解**：VSCODE_PID 无法经 winlist 定位窗（Electron 多窗共享主 PID）→ **保留扩展 marker** 是唯一 env-independent 可靠解（SessionStart hook 写、含 VSCODE_PID+nonce+存活校验）④ race① 锚点+超时机制=PASS。evidence=`/tmp/p21-audit/out-directjump-mp.md(.evidence.json)`（初审 raw `/tmp/p19-mp-design-review-out.md` 存史）。

> **🔴 ultracode workflow 对抗层抓出 2 个 LIVE return-leg P2（djs-jump 已合 `6b7c94c`，外双脑漏、中枢亲读源码坐实）——worker 实施 MP 时一并修（与本设计 race① refinement 同源）**：
> - **P2-live-1（return-on-failure）**：`auto-continue.sh:1984` 的 `[ "$_RETURN_DEFERRED" = "1" ] || _return_jump_back` 只在 screen-relock（1834）排除 return；accessibility-fail（1835）/frontmost-not-Code（1975）/各 submit-withheld 子case（cold rc=5 / singlepane rc=6/1/* / warm focus-drift）= Enter 没成功按却仍触发 return → owner 被切回 B、隐藏了需手动 Enter 的 worker。修法=return 只在 submit **成功**（ack `submitted`）才 arm（加 `_RETURN_DISPATCHED` 标记，仅成功路径置位），非「!deferred」。低当前影响（return-leg 仅 SPAWNER_FOCUS 罕见 arm）但真缺口。
> - **P2-live-2（anchor presence-not-identity）**：`vscode-spaces.py:_spawn_return_logic`（689）只判「target 桌面有 ∉before 的窗」=**存在非身份**；empty `--before`（precapture 输出畸形时）退化为「target 有任意窗即返回」。应升级为 gemini refinement 的 `Window_ID ∉ before && APP==Code && (workspace/nonce 匹配) && Space==target`。fail-open 不崩，低影响。

**3 条 refinement 终裁采纳（owner-authoritative，无须再等 codex）**：

1. **🔴🔴 [sw-coord-p22 已纠偏] 简化 Part A = 「发起端自报 workspace【路径】走 focus-jump（一步直达）」——不是自报数字走 goto（逐格）**：
   - **根因（证据级 / 中枢亲读源码）**：`vscode-spaces.py` 两套切桌面机制——`focus-jump`（`code <workspace>` 重激活已开窗 → macOS native「切应用→切空间」一滑直达 = **真·一步**，[code-router.sh:21-28] + [vscode-spaces.py:566-620]）vs `goto N`（`_press_until_moved`→`send_ctrl_arrow` Ctrl+方向键**一格一格走**，[vscode-spaces.py:151-188]）。owner 实证「切到中枢桌面不是一步到位」= 逐格 goto 暴露。**故「自报数字→goto N」本身就违反北极星「非逐格、一步原生」**。
   - **正解**：发起端 `resolve_spawner_*` 两 tier（worktree=`<cwd>/.handoff.code-workspace`；singlepane=marker→`marker_ws`）**本就先算出 workspace PATH 再 `_desktop_of` 转成数字**——直接**返回 PATH、经验证闸 emit `SPAWNER_FOCUS=<PATH>`**，复用既有 focus-jump（一步直达），**不引 `SPAWNER_DESKTOP`、不调 `goto N`、dharmaxis code-router 零改**。focus-jump 预检要求「目标窗开着」——worktree/singlepane 发起端的「自己那个窗」本就开着 → 成立。
   - **复用 worker 已写工作**：env-independent 自识别（cwd/marker→winlist）100% 留用，只把输出从「数字」换「路径」、删 `_desktop_of` 转换 + `SPAWNER_DESKTOP` emission。「为得数字仍要 path-resolution」证明原「省 resolver」是假前提——focus-jump 反而更简单。
   - ~~[原 #1，作废]：写 `SPAWNER_DESKTOP=<N>`（数字）→ 看门狗 `goto N`，砍掉 focus-jump 依赖。~~（错因：goto N 逐格、非一步。）
2. **🔴 race① 锚点（两脑一致硬修）**：snapshot spawn 前 window 列表 → `code -n` → 轮询直到出现**不在旧列表的新 window-ID 且其 Space 明确=A** → 才 `goto B`；3-5s fail-open 超时（超时放弃回跳、不连锁）。仅「发现有新窗」不够、必须确认新窗 ID 在 A。
3. **marker 硬化（若 singlepane 保留 marker）**：marker 名/内容含 `VSCODE_PID`（gemini）+ 读时校验该 PID 窗进程存活、否则 stale→fail-open；并发隔离 marker 名追加 nonce `<sid>_<nonce>.focus`（deepseek）；session-id 唯一性。

> **codex-6-15 焦点**：① singlepane 自识别硬核（自报数字仍需识别本窗——VSCODE_PID 能否经某通道映射到窗？winlist 不暴露 PID）② race① 锚点是否够稳（VS Code 冷启动 >2s 撕裂）③ 简化 vs marker 的最终取舍。

4. **🔴🔴 [sw-coord-p22 新增 / owner 实证「回程也要一步到位」/ ⏳ 待 gemini+deepseek 双脑审] Part B 回程同样必须一步直达，非逐格**：
   - **现状（亲读坐实）**：`cmd_spawn_precapture` 只捕获 `ORIGIN=<桌面号>`+`BEFORE=<窗号集>`；`_spawn_return_logic` 锚定子窗落地后用 **`goto_desktop(origin)`=逐格** 回跳（[vscode-spaces.py:659-694]）。live log `spawn-return: …→回 origin 桌面N` 即逐格。**违反北极星②「几乎无感、不被拽走」**（逐格走多格 = 很有感）。
   - **难点（与去程不对称）**：去程目标=中枢自己那个已知窗（focus-jump 现成）；**回程目标=owner 原桌面，系统只记了号码、没记可重激活的窗**——macOS 无「瞬移到桌面 N」脚本接口，一步直达唯一靠「重激活一个在该桌面的窗/App」。
   - **提案机制（MP「记原点前台→finally 还原」范式）**：`spawn-precapture` **多捕获一个回跳锚**——owner B 桌面的前台窗/App 身份（优先 VS Code 窗→`code <ws>` 一步；非 VS Code→通用 App 激活 `tell application "X" to activate`；前台 App 跨多桌面→须捕获具体窗 window-id）。`spawn-return` 改为**重激活该锚=一步直达**，**fail-open 退回既有 `goto N`**（锚无效/捕获失败/非 VS Code 无通用激活把握时）。
   - **已知取舍**：一步回程重激活会把 owner 原 App **带到最前**（现 goto 只切桌面不改焦点）——owner 本在那工作，通常正是所需；双脑审需裁此 UX + 多桌面同 App 的窗歧义 + focus-steal 安全。
   - **scope**：归 `mp-locate-return` 同棒（回程腿正是阶段2 改处）；**先双脑审定机制再让 worker 实施**，E2E 硬验收=去程+回程**两程肉眼不逐格**。

## 📌 与现状联动
- live 直跳 fail-open 退 goto、零回归（没弄坏、只是没工作）。本设计不阻断业务。
- ✅ 作废稿 `2026-06-14-direct-jump-focus-resolver-design.md` 的 banner **已 p21 订正**（reversal 终裁 PASS=根因对/p18 过早/方案被 MP 取代；顶部加 ✅ 结论、原 p18 🛑 banner 保留作争议史）。
- **race① 超时值仲裁（gemini 10s vs deepseek 3-5s 分歧）→ 取 configurable + 慷慨默认 ~8-10s**：超时仅 fail-open 牺牲「回 B」体验（worker 安全留 A），过等无害、过早撕裂有害 → 偏保守，做成 `HANDOFF_RETURN_MAX_WAIT` 可调（现 live default 2.0s 对 MP 全链 cold-start 偏激进，worker 实施时调大）。
- capability-registry 待补登 MP「locate-act-return 桌面自动化」对外能力（JIT 发现：owner 指认时 registry 未录）。
