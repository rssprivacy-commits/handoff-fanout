# mp-locate-return 审计后定稿实施 spec（两程一步 / sw-coord-p22 / 2026-06-14）

> **权威实施 spec**（worker 读这一份即可）。取代散落在 `2026-06-14-mp-style-locate-dispatch-return-design.md` 的 refinement #1/#4 + brief 旧文本。
> **缘起**：sw-coord-p22 用 4 维对抗 workflow 审计自己派给 worker 的 brief，亲读代码坐实 5 条承重 P0/P1（singlepane marker 不存在 / contract 矛盾 / V3 回程缺失 / 测试焊死逐格 / 行号失效）→ 本 spec 全部修对。
> **状态**：✅ **全部定稿可实施**。去程（§1）+ 回程（§2，含 §2.3 保守 fail-open 定稿、捕不准即退 goto 永远安全）+ V3（§3）+ 测试/E2E/卫生（§5-7）齐全。完整 gemini+deepseek 在 **worker 交付物**上复核命中率（gemini agy 通道本轮超时 exit124、且交付物审才是真零信任 gate）。

## 🎯 北极星（每里程碑回读）
派窗时：① 子窗**一步**落中枢桌面（去程 outbound，非逐格）② 派完**一步**回 owner 原桌面（回程 return，非逐格）。**两程都「肉眼只切一次、不见中间桌面逐个闪过」才算达成。** 只代码对≠真工作——真派窗看（owner 在环）。

## 0. 机制底座（macOS 事实 / 亲读 vscode-spaces.py 坐实）
- **一步直达唯一机制 = `focus-jump`**：`code <workspace>` 重激活一个**已开着**的窗 → macOS native「切应用→切空间」一滑直达（[vscode-spaces.py cmd_focus_jump]）。需目标窗在 `storage.json openedWindows`（幽灵窗预检）。
- **逐格 = `goto_desktop`**：`send_ctrl_arrow` Ctrl+方向键一格一格走（[vscode-spaces.py goto_desktop]）。**这是 owner 投诉的「不一步到位」**。
- **winlist 能力**：给「某桌面的 Code 窗（title+window_number+desktop）」；**不暴露 frontmost / ownerPID**。前台 App 探测靠 osascript。
- **fail-open 最高红线**：任何解析/捕获/重激活失败 → 退既有 `goto N`（去程）/ 停当前桌面（回程）。绝不阻断派窗、不撕裂。

---

## 1. 去程（outbound → 中枢桌面）= 自报 workspace【路径】走 focus-jump

发起端 dump/spawn 时 env-independent 解析**自己那个窗的 `.handoff.code-workspace` 路径**，经安全闸 emit `SPAWNER_FOCUS=<PATH>` → 看门狗 code-router 跑既有 focus-jump（一步）。`resolve_spawner_focus_path` 两 tier：

- **Tier-1 WORKTREE**（红顶 worktree 中枢）：`<cwd>/.handoff.code-workspace` → 验证闸 → 返回。✅ **已 live 坐实可行**（mp-locate-return worktree cwd 命中、norm == storage.json openedWindows 一条 → focus-jump 一步）。**不改**。
- **🔴 Tier-2 SINGLEPANE（本次修）**：现调 `_read_singlepane_focus_marker`（读 `<home>/<proj>/focus/<sid>_<nonce>.focus`）——**该 marker 基建全集团零目录、`coord-identity-inject.py` 零写入逻辑 = 永远 None → singlepane 中枢去程永远退逐格 goto**（恰是 owner 单栏窗痛点）。
  - **修 = Tier-2 改调同文件已有的 `derive_singlepane_focus(home, project, task)`**（读**真实存在**的 `<home>/<proj>/singlepane/<task>.handoff.code-workspace` sidecar，p10-p22 都在），**透传 `--self-task`**（发起端自报自己的 task，env-independent；与 djs-jump-return 接棒去程同款、已 live 生效 = p21→p22 focus-jump 7→6 的那条路）。
  - 仍过 `validate_spawner_focus` 单点安全闸（realpath + `.handoff.code-workspace` 后缀 + allowed-root）。
  - **不依赖阶段3 marker hook**——marker 路线作废，singlepane 去程靠 sidecar self-report 即可一步，无需等 marker 基建。
- **会退逐格 goto 的已知 case（brief 必列、E2E 不误判为 bug）**：① 发起端给不出 self-task / sidecar 不存在（bootstrap 窗 dx-spawn 出生、无引擎 sidecar）② dx-coord TMPDIR 协调窗（cwd 非 worktree、无 sidecar）→ 见 §4 V3 ③ 目标窗未登记 storage.json（窗刚开落盘延迟）→ fail-open 不开幽灵窗，正确。

**安全闸单点**：spawn+dump 共用 `validate_spawner_focus`，allowed roots 不放宽。字节级向后兼容：解析 None → 不写 SPAWNER_FOCUS → .uri/行为逐字节同今天。

---

## 2. 回程（return → owner 原桌面）= 捕获回跳锚→重激活（一步），fail-open 退 goto

**现状**：`cmd_spawn_precapture` 只输出 `ORIGIN=<桌面号>`/`BEFORE=<窗号集>`；`_spawn_return_logic` 锚定子窗落地后 `goto_desktop(origin)`=**逐格**。北极星②未达成。

**改 = precapture 多捕获「回跳锚」，spawn-return 重激活该锚（一步）**：

### 2.1 precapture 捕获锚（新增 stdout 行 / wire contract）
在现有 `ORIGIN=`/`BEFORE=` 之外**新增**（worker 照此字段名，勿自创）：
- `RETURN_ANCHOR_WS=<realpath>` —— 优先：owner 原桌面前台是 VS Code 窗时，该窗的 `.handoff.code-workspace`/workspace realpath（可被 `code <ws>` 精确一步重激活）。
- `RETURN_ANCHOR_APP=<name>` —— 兜底：前台是非 VS Code App（Safari/Terminal/Finder…）时其 App 名（`osascript tell application "<name>" to activate` 一步切到该 App 桌面）。
- **空（两行都不输出 / 输出空值）** —— 捕获不到可重激活锚 → spawn-return 据此 fail-open 退 `goto N`。

### 2.2 spawn-return 重激活
收到锚：`RETURN_ANCHOR_WS` 非空 → `code <ws>`（一步、精确）；elif `RETURN_ANCHOR_APP` 非空 → `osascript activate`（一步）；else / 重激活失败 → **fail-open `goto_desktop(origin)`**（既有逐格兜底，永远能用）。

### 2.3 保守定稿（fail-open-safe / 中枢裁 / 完整 gemini+deepseek 放 worker 交付物上复核命中率）
> 🔴 关键不变量：**这 4 个歧义全有 fail-open 安全兜底——捕不准就退今天的 `goto`、零回归**。故机制怎么裁都安全；下列保守答案=「捕得准才走一步、稍有不确定即退 goto」。完整双脑放交付物审（gemini agy 通道本轮超时 exit124，且交付物审才是真零信任 gate）。
1. **Code-frontmost 歧义 → 取「precapture 时就地捕获」**：precapture 跑在**派窗之前**（owner 还在 B、前台 Code 窗还没被新子窗污染）→ 就地 osascript 取当前 frontmost 进程；若 == `Code`，取该 frontmost Code 窗 workspace 做 `RETURN_ANCHOR_WS`。**从根上避开「派窗后多 Code 窗」歧义**（捕在污染前）。解析不出唯一 workspace → 不输出 WS、降 APP 或空（退 goto）。
2. **frontmost App 桥 → osascript 取名 + 仅高把握才走**：`System Events` 取 frontmost process name（实测可靠）。非 Code → `RETURN_ANCHOR_APP=<name>`，return `tell application "<name>" to activate`（切到该 App frontmost 窗=通常 B）。多桌面歧义不强求完美；activate 失败/异常 → 退 `goto origin`，**绝不因此切错桌面**。
3. **三态 fail-open（明确 case 表 / worker 照此写测试）**：① owner 空桌面/纯 Finder（无可重激活窗）② 前台窗无标题（winlist 丢弃）③ 无 workspace/app 把握 —— 三态都**不输出锚 → spawn-return 退 `goto_desktop(origin)`**。
4. **focus-steal → return 前重读 active**：重激活前重读 `active_index`：== target(A)（派窗把视图留中枢桌面、owner 未动）→ 重激活锚回 B；== origin(B)（owner ~8s 内已自己回 B）→ 无需动；== 其它桌面 C（owner 已手动去别处工作）→ **放弃重激活、也不 goto**（owner 在 C 工作，绝不拽走）。`code <ws>`/`activate` 改焦点（带 App 到最前）= 可接受（owner 本在该窗工作）。

---

## 3. V3（dx-spawn --coordinator）回程腿 = 当前完全缺失（P1 漏网）

`dx-spawn-session.sh` 去程有 focus-jump（[:351]），**回程 0 代码**（grep spawn-return/precapture = NONE）→ 派完新中枢 owner **永久滞留中枢桌面 A**（连逐格回都没有）。
- **修 = dx-spawn-session.sh --coordinator 路径在 focus-jump + `code -n` 之后加 capture/restore 包壳**：调同款 `vscode-spaces.py spawn-precapture`（派窗前）+ `spawn-return`（开窗后），**同样走 §2 一步重激活、非逐格**。
- ⚠️ 与 hf 看门狗链不共代码（V3 是脚本自调，不经 .uri/code-router）；去程已天然一步（focus-jump @351），只缺回程。

---

## 4. 变体 × 腿 × 机制矩阵（8 格 / 防漏 / brief 必含）

| 变体 | 去程 outbound | 回程 return |
|---|---|---|
| V1 引擎 dump 派 worker | focus-jump via SPAWNER_FOCUS（worktree✅ / singlepane 需 §1 Tier-2 修） | §2 重激活（现逐格，待修） |
| V2 spawn 派 worker | 同上 | §2 重激活（现逐格，待修） |
| V3 dx-spawn --coordinator | dx-spawn 脚本自调 focus-jump @351 ✅（不经引擎） | §3 **当前缺失**，须脚本侧新增（一步） |
| V4 audit-close 接棒 | spawn self-identify（§1，singlepane 走 Tier-2 修） | §2 重激活（现逐格，待修） |

> ⚠️ 设计稿 §四 V1/V2 编号与本矩阵相反且未区分「dx-spawn 派 worker（经引擎链）vs dx-spawn --coordinator（老路）」——以**本矩阵**为准。

---

## 5. 测试要求（🔴 旧测试焊死逐格、必须改）
- **现 `test_return_leg.py` monkeypatch `goto_desktop` 断言走 goto** = 把逐格焊死，worker 改一步会让它变红 → 必改：① 回程腿单测断言走**重激活锚路径**（mock 锚捕获 → 断言调 `code <ws>` / `activate`，**NOT** `goto_desktop`）② **保留**一条「锚空 → fail-open goto」的 fallback 断言（把旧 `assert goto==[origin]` 改成 fallback-only 语义）。
- 去程单测：worktree Tier-1 + **singlepane Tier-2 走 derive_singlepane_focus**（mock sidecar 存在 → 返回 path；不存在 → None fail-open）+ 字节兼容（无 self-task/sidecar → 逐字节同今天）。
- 全套件绿（基线 1634/3/0；shim `python` + `DX_SPAWN_SH` + `PYTHONPATH=src`）。

## 6. E2E 验收（🔴 机检判据 / 区分一步 vs 逐格）
- **去程**：router.log 必出 `focus-jump OK`（**非 `goto`**）。
- **回程**：spawn-return log 必打印走**重激活锚分支**（`code <ws>`/`activate`），**非 `goto_desktop` fallback** —— 给两分支**不同 log 前缀**（现逐格/一步 log 文案都含「回 origin 桌面N」无法区分，必须加前缀标记）。
- **owner 在环肉眼**：盯桌面切换——A→B 只切一次（一步）vs 看到中间桌面逐个闪过（逐格）。**两程都一步才 GREEN。**
- 分档：worktree 去程可立即验；singlepane 去程验 Tier-2 修后；回程验 §2 实施后。

## 7. 卫生 / 红线（治审计揪出的误导）
- **🔴 行号已失效，用符号锚 grep 定位**（restructure 漂移 + 混了 dharmaxis main vs worktree 两版本行号）：`_return_jump_back` / `_RETURN_DISPATCHED` gate / `_spawn_return_logic` / `cmd_spawn_precapture` / `resolve_spawner_focus_path` / `derive_singlepane_focus`。**勿信任何 brief/spec 里的行号。**
- **🔴 return-leg placement 已定 = hf `auto-continue.sh`**（在 prompt-inject+Enter 之后；precapture 在 `$CODE_BIN -n` 之前）。设计稿 §Part B 写的 `code-router.sh` placement **已作废**（46331b5 placement-bug fix）——**别往 code-router.sh 加回程包壳**（会重引入已修 bug）。dharmaxis 侧只改 `vscode-spaces.py` 的 precapture/return 原语。
- **跨仓隔离**：hf 改在 hf worktree；dharmaxis 改在 dharmaxis 隔离 worktree（`_xrepo-wt/dharmaxis-mp-locate-return`，非 owner main checkout）；owner 脏 cc-global 树绝不碰。
- **install/ 部署陷阱**：改 `install/auto-continue.sh` → commit 前 `export HANDOFF_INSTALL_SH=/nonexistent`、commit 后验 launcher sha `cbb39ff9` 未变。`src/*.py` 不触发。
- **§6b**：worker 禁自合/自审/自派/写共享 memory/push origin。报告含：hf 分支+worktree 绝对路径、dharmaxis 分支+worktree 绝对路径、各自 HEAD sha（供中枢零信任拉取审）。
- **报告**：`touch ~/.claude-handoff/handoff-fanout/ack/mp-locate-return.worker_reported` 后静默等中枢复审。

## 8. 实施顺序（阶段）
1. **去程 Tier-2 修**（spawner_focus.py Tier-2 → derive_singlepane_focus + dump/spawn 透传 self-task）+ 单测。【不阻塞、可独立验】
2. **回程一步**（precapture 捕锚 + spawn-return 重激活 + fail-open，§2）—— **待 §2.3 双脑审定机制后实施** + 改 test_return_leg.py（§5）。
3. **V3 回程**（dx-spawn-session.sh --coordinator 脚本侧 §3）。
4. P2-live-1（return-arming，worker 已提交 4b13237 ✅）/ P2-live-2（anchor identity，worker 已做 ✅）保留。
5. 全套件 + E2E（§6）。
