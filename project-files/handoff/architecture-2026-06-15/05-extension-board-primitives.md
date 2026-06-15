# handoff-fanout 架构图 · Shard 05 — VS Code 扩展 + 状态板 + 原语 + 安装器

> 只读架构勘察 · git HEAD `5e8d7b2`（extension/ 最新三笔 §6c 提交 `325bcea`/`92437d2`/`da9b266`）· 2026-06-15
> 范围：`extension/` 的 handoff-helper 扩展、`status_board.py` 会话/状态板、`atomic.py`/`safe_commit.py`/`prune.py`/`memory_baseline.py` 原语、`install/install.sh` 安装器。

---

## 1. 运行时角色

**VS Code 扩展 `dharmaxis.handoff-helper`（已装版本 0.6.0，源码 package.json 0.6.0）** 是冷启动 worktree 窗口的窗口级执行器。它在窗口的扩展宿主里跑，对**本窗口**做三件事，全部 fail-closed：

- **URI 契约**：注册 `vscode://dharmaxis.handoff-helper/<path>?<query>` 处理器（`extension.ts:227-313`）。三条 path：
  - `/singlepane` — 折叠侧边栏成单编辑窗（`closeSidebar`+`closeAuxiliaryBar`）。
  - `/autoclose` — 角色门控的「监管接班关闭前任窗」（succession leg），以及兼容残留的 §6c reclaim URI 接收口。
  - 未知 path 静默忽略。
- **nonce 校验**：nonce = `secrets.token_hex(8)` → 16 位小写 hex（64 位熵）。扩展只做**格式**校验（`isValidNonce`，`handoffClose.ts:69-73`）；窗口自定位用**子串匹配**——本窗口的 `window.title` 是否包含该 nonce（`titleHasNonce`，`handoffClose.ts:266-268`）。一个 vscode:// URI 只落到一个窗口的扩展宿主，宿主只能动**自己**的窗口，所以「错投的 URI 永远关不了错的窗口」是 fail-closed 内禀属性。
- **脏页保护（dirty-tab protection）**：任何关闭路径都先过脏页门。succession 是 skip-dirty（跳过脏 tab 关其余）；§6c reclaim 是**更严**——任一脏 tab 即拒绝整次关闭并写 `dirty` ack（`handoffReclaim.ts:238-252`）。关窗前还会**二次**重扫脏页（gate 6 之后到 closeWindow 之间可能冒出新脏 tab，`decideCloseWindow`，`handoffReclaim.ts:421-430`）。
- **单栏折叠（single-pane fold）**：只对 `.handoff.code-workspace` 结尾的工作区生效（`isHandoffWorktreeWorkspace`，`handoffClose.ts:365-370`），既走 `onStartupFinished`（窗口加载即折叠，`runStartupSinglePane`），也走 `/singlepane` URI（submit 之后兜底）。守卫保证**绝不**碰 owner 正常窗口（多窗红线）。
- **红顶（red-top）协调器窗口**：`🧭中枢·` 前缀的窗口（`COORDINATOR_TITLE_PREFIX`，`handoffReclaim.ts:63`）**永不**是 §6c reclaim 目标——它们走 succession `close_predecessor` leg 退场，不走 reclaim；reclaim 轮询器对它们恒为 inert。
- **§6c reclaim 轮询器（A-poll）**：若本窗口是 worker **worktree** 窗口（标题解析出 worker 身份，`parseWorktreeWorkerIdentity`），每 7s 轮询自己的 `ack/<task>.reclaim_pending.json`（`extension.ts:33,128-193`）。producer 不再 PUSH URI（push 只到一个焦点窗口、跨桌面收不到），改为窗口 PULL 自己 task 的 pending → 窗口定位变成内禀。配套 **PID dead-man（方案 D）**：窗口 activate 时把自己扩展宿主 pid 写进 `ack/<task>.host_pid.json`（`writeHostPid`，`extension.ts:65-89`），producer 关窗后 `os.kill(pid,0)` 确认宿主真死才回收 worktree。

**状态板 `status_board.py`（S5a 切片）** 是给非技术 owner 的**只读**观测+急救面板（`handoff status|sessions|stop|pause|resume|approve|force-sync`）。它读 `~/.claude-handoff/<project>/` 真实 runtime（queue/ack/worktrees/transcript mtime），把每个 task 归一成**业务维度**（`BusinessState`：运行中/卡住需介入/已交付待审/已交付可关/闲置/已完成）。INV-1：控制面零 LLM、纯确定性投影；INV-3：唯一写是 STOP_AUTO sentinel（可逆 touch/unlink）、bindings 文件、经 S3 单写者 API 追加的 event。脑裂规则：监管中枢视图与真实 runtime 冲突时**真实 runtime 赢**。**该模块未接入运行引擎**（`dump`/`worktree`/`audit-close` 从不 import 它，cli.py 懒加载，S5a 红线「只增不改运行路径」）。

**原子/安全提交原语**：
- `atomic.py` — POSIX 原子写（`atomic_create` O_CREAT|O_EXCL、`write_with_fsync`、`atomic_replace` temp+rename 防撕裂读）+ `acquire_dir_lock`（**flock 跨进程互斥**，内核为 fencing 权威，无 staleness 启发、无 owner-nonce 围栏；进程内 re-entrant 用 fd 注册表深度计数防自死锁）。
- `safe_commit.py` — 防 git-index 劫持的 `git commit` 包装（4 层：跨进程锁 + staged-set 期望文件不变量 + pre-commit hook 复验 + post-audit）。
- `prune.py` — 终态 task 的 `.heartbeat`/`.529-suspected`/`.uri` 死 sidecar 清理（默认 dry-run，从不删 .md/.done/.BLOCKED.md 历史）。
- `memory_baseline.py` — G3「真沉淀」机器证明：协调器派发时快照项目 memory `*.md` 的 sha256 基线，relay 时对比，≥1 文件新增/改 = 物理沉淀证据（当前 **WARN-only**，从不阻断 relay）。

**安装器 `install/install.sh`** — 幂等安装：HANDOFF_HOME 树、config.json、per-repo git hooks（pre-commit/post-commit/pre-push/post-merge 软链）、macOS launchd watchdog plist；独立子命令 `--sync-launcher`/`--sync-dump` 把 canonical asset 推到 `~/.local/bin` 运行时副本并记 sha。

---

## 2. 核心模块

| 文件 | 责任 | LOC |
|------|------|-----|
| `extension/src/extension.ts` | 扩展激活胶水：注册 UriHandler、reclaim 轮询器、startup 单栏；ack/host_pid 落字节（temp+rename）；closeWindow 执行 | 331 |
| `extension/src/handoffReclaim.ts` | §6c worker-worktree reclaim 纯决策核（7 gate 链）+ A-poll pull 模型 + `decideCloseWindow` 关窗二次脏检 | 431 |
| `extension/src/handoffClose.ts` | 纯 vscode-free 关闭逻辑：URI 解析、nonce 格式校验、dirty-safe retry-once 关闭原语、succession autoclose、单栏折叠（URI+startup） | 444 |
| `extension/package.json` | 扩展清单 v0.6.0，activationEvents=onUri/onStartupFinished，main=dist/extension.js | 46 |
| `src/handoff_fanout/status_board.py` | S5a 只读状态板/会话视图：runtime 扫描 → BusinessState 归一 + DAG overlay + CLI（status/sessions/stop/pause/resume/approve/force-sync） | 1643 |
| `src/handoff_fanout/atomic.py` | 原子写原语（create/replace/fsync）+ flock 跨进程目录锁（re-entrant、migration fail-closed） | 303 |
| `src/handoff_fanout/safe_commit.py` | 防 index 劫持 git commit 包装（锁 + 期望文件不变量 + post-audit） | 238 |
| `src/handoff_fanout/prune.py` | 终态 task 死 sidecar 清理（heartbeat/529/uri；默认 dry-run） | 111 |
| `src/handoff_fanout/memory_baseline.py` | G3 沉淀证明：memory sha256 基线快照 + WARN-mode 验证 | 267 |
| `install/install.sh` | 幂等安装器：HOME 树/config/git hooks/launchd；--sync-launcher/--sync-dump 运行时副本同步 | 293 |
| `install/git-hooks/post-commit` | 改了 canonical 部署资产即自动 `--sync-*`（带 audit gate，fail-closed，non-fatal） | 79 |

---

## 3. 工作机制

### 3.1 扩展 URI 处理器（handleUri，`extension.ts:227-313`）

请求按 path 分流：

1. **`/singlepane`**（`isSinglePanePath`）→ `handleSinglePane`（`handoffClose.ts:372-401`）：缺 task 拒 → 工作区非 `.handoff.code-workspace` 拒（`wrong-window`）→ 顺序跑 `closeSidebar`+`closeAuxiliaryBar`（显式 close 非 toggle，幂等不会重开）。

2. **`/autoclose` 带 `reason` 参数**（§6c 矩阵，`extension.ts:258-283`）：先查 `reclaimHandled` 去重 → `handleReclaim` 跑完整决策核 → succession 行 `delegate-legacy` 落到下面的 §6 legacy 关闭；其余记入 handled。**A-poll 后此 leg 在生产中只被 succession 委托走到**（reclaim 改 pull）。

3. **`/autoclose` 无 reason**（succession，`extension.ts:288-309`）→ `handleAutoclose`（`handoffClose.ts:274-310`）：role=worker → 永不关（`worker-keep`）；role≠supervisor_succession → fail-closed（`unknown-role`）；`predecessor_nonce` 缺/非法 → fail-closed（`missing-predecessor-nonce`，从不猜）；本窗口 title 不含 predecessor_nonce → fail-closed（`predecessor-not-here`）；确认是前任窗 → dirty-safe retry-once 关闭。

### 3.2 §6c reclaim 七 gate 链（`handleReclaim`，`handoffReclaim.ts:156-275`）

拒绝先于任何副作用（含关闭，C7）：① project/task slug 校验（非法 slug 永不进文件路径，仅 log，`:164-169`）→ ② role×reason 白名单矩阵（succession+close_predecessor → delegate；worker+reclaim → 本路；其余 → `role-reason-rejected` ack，`:172-188`）→ ③ nonce 必 hex16（nonce 即 auth token，`:191-201`）→ ④ run_id/issued_at/ack_timeout freshness（畸形或超 `issued_at+ack_timeout` 窗口 → `close-command-expired`，防 producer 释放锁后陈旧 URI 杀掉新 spawn 占的窗口，`:205-224`）→ ⑤ 窗口本地自定位（title 不含 nonce → `not-this-window` 静默，`:230-236`）→ ⑥ 脏页门（任一脏 tab → 拒整次关 + `dirty` ack，`:239-252`）→ ⑦ dirty-safe retry-once 关 tab → `close_issued` ack **意图**（非终态 done）。

**关窗 split（方案 D）**：`closeTabs` 只关编辑 tab（VS Code 无关窗 tab API）。胶水 `finishCloseWindow`（`extension.ts:97-117`）在 close_issued 上**先**写 durable ack（temp+rename，因为 closeWindow 杀宿主、ack 必须先落盘）**再** `workbench.action.closeWindow`；二次脏检冒新脏 tab → `abandon-dirty`：不写 ack/不关窗、丢出 handled 待重试，producer 的 PID dead-man 超时 fail-closed（worktree 保留）。

### 3.3 A-poll pull 模型（`pollReclaimOnce`/`startReclaimPoller`，`handoffReclaim.ts:382-398` + `extension.ts:128-193`）

根因：`open vscode://…` 只投递到一个焦点窗口，跨桌面 worker 收不到 → producer 总 `ack-timeout`。修法 push→pull：producer 写 `reclaim_pending.json`，本窗口轮询**自己** task 的 pending，重建 close params 跑**不变的** `handleReclaim`。`busy` 守卫防重叠 tick；`reclaimHandled` 在 poll 与 legacy URI 间共享，一个 run 恰好关一次。四不变量（nonce 自定位/freshness/fail-closed/dirty 门）全因复用同一决策核而保留。

### 3.4 原子写契约（`atomic.py`）

`atomic_create`（O_CREAT|O_EXCL，race-safe 返 bool）、`write_with_fsync`（O_TRUNC 就地写，**有撕裂读窗口**——并发读可能见截断）、`atomic_replace`（same-dir temp + fsync + os.replace，读者只见全旧或全新，hash 校验产物必用，`:63-94`）。`acquire_dir_lock`（`:145-214`）= flock 跨进程独占锁：内核在持锁进程死时自动释放（无 staleness 启发、无陈旧清理 TOCTOU）；re-entrant 同进程同路径用 fd 注册表深度计数复用（防 flock 自阻塞）；遇 legacy mkdir lockdir 抛 `LockMigrationError` fail-closed（不 auto-rmdir，避免重新引入 TOCTOU）；活着但 hung 的持有者**永不**强破（防 split-brain，交给 watchdog/超时）。

### 3.5 safe-commit（`safe_commit.py`）

`-m MSG -- file1 file2`：校验文件存在/被跟踪 → 取 `_default_lock_path()`（默认 `$HANDOFF_HOME/git-commit.lockdir`）下 `acquire_dir_lock` → `_commit_under_lock`：逐文件 `git add -A -- f` → 比对 `index_after - expected - index_before`，有 unexpected 且非 bypass → 中止（exit 1）→ 写 `HANDOFF_EXPECTED_FILES` 临时文件传给 pre-commit hook → `git commit --only -- files` → `_post_audit`：`git show --stat HEAD` 落地集是否 ⊆ expected，超出 → exit 2。`core.quotepath=false` 防 CJK 文件名 octal 转义假阳。bypass：`HANDOFF_SAFE_COMMIT_BYPASS=1`。

### 3.6 install.sh 流程

`set -euo pipefail`，定位 install/ asset（curl-pipe 模式自 clone 到 temp）。① 建 HANDOFF_HOME 树 ② config.json（缺才写，从模板）③ **git hooks**：`_link_hook` 软链 pre-commit/post-commit/pre-push/post-merge 到 `install/git-hooks/*`（备份既有非 handoff hook），尊重 `core.hooksPath` ④ **launchd**：sed 模板填 `@@HANDOFF_BIN@@`/`@@HANDOFF_HOME@@` → `com.handoff-fanout.watchdog.plist`，变了才 reload ⑤ **🔴 VS Code 扩展：UNINSTALL 迁移**——见 §6 半实现陷阱。`--sync-launcher`/`--sync-dump` 独立子命令：cp canonical → `~/.local/bin` + 记 sha + byte-for-byte 校验。**install.sh 从不安装扩展 vsix**——它只**卸载**扩展。

---

## 4. 数据流 / 状态流

**URI 参数**（`parseQuery`，`handoffClose.ts:50-63`）：`task_id`/`nonce`/`project`/`role`/`predecessor_nonce`/`reason`/`run_id`/`issued_at`/`ack_timeout`。

**§6c reclaim 文件流**（全在 `~/.claude-handoff/<project>/ack/`）：
- producer 写 `<task>.reclaim_pending.json`（授权信号，携带 role/reason/nonce/run_id/issued_at/ack_timeout）。
- 窗口 activate 写 `<task>.host_pid.json`（pid+nonce+project+task+ts；PID dead-man token）。**实查：磁盘上有多个真实 host_pid.json**（fateforge/dharmaxis/handoff-fanout 等），证明 reclaim/PID 机制 live 且被实跑过。
- 窗口决策后写 `<task>.reclaim_ack.json`（`relPath=<project>/ack/<task>.reclaim_ack.json`，result=close_issued|failed + reason；`ackIntent`，`handoffReclaim.ts:121-137`）。
- producer 读 close_issued ack + `os.kill(host_pid,0)` ESRCH 才删 worktree（终态 done 由 producer 拥有）。

**succession autoclose 流**（`auto-continue.sh try_autoclose`，`:2332+`）：opt-in 门（`autoclose_enabled_for_project`，`:2314-2320`）→ project spawn lock → 读 `queue/<task>.singlepane` sidecar 的 role/predecessor_nonce → fire `vscode://…/autoclose?...&role=supervisor_succession&predecessor_nonce=…`（`:2478`）。每条失败路径落 `ack/<task>.autoclose_failed.txt`（reason enum）防 loop；worker/无 sidecar/BLOCKED 是静默 SKIP **不**落 marker。`<task>.autoclose_done` 幂等 sentinel。

**nonce 文件**：nonce 不单独落文件——它编码进 `window.title`（worktree.inject_vscode_workspace 写 `<project> · <task> · <role> · <nonce> [worktree]…`）+ sidecar JSON 的 spawn_nonce/predecessor_nonce + URI query。扩展靠 title 子串匹配自定位。

**launcher 部署路径**：canonical `install/auto-continue.sh` → `~/.local/bin/auto-continue.sh`（com.dharmaxis.auto-continue 跑）；`install/dump-handoff.py` → `~/.local/bin/dump-handoff.py`（v5.4 引擎 re-exec shim）。sha 记 `~/.claude-handoff/.auto-continue.canonical.sha` / `.dump-handoff.canonical.sha`（启动 drift guard 读）。

**状态板数据源**：`~/.claude-handoff/<project>/queue/<task>.{md,uri,heartbeat,done,529-suspected}` + `.BLOCKED.md` + `ack/<task>.{spawned,submitted,worker_reported,failed,old_ready}` + `worktrees/<task>/` + worker transcript JSONL mtime → `BusinessState`。

---

## 5. 现状三态

- ✅ **§6c reclaim + PID dead-man（方案 D）live**：扩展 0.6.0 已装=源码一致；磁盘有真实 host_pid.json；A-poll pull 模型在 extension.ts/handoffReclaim.ts 接线完整。
- ✅ **单栏折叠 live**：onStartupFinished + /singlepane URI 双路，`.handoff.code-workspace` 守卫；config `singlepane_projects` 开关。
- ✅ **nonce 自定位（窗口本地）live**：但是**子串校验**而非 per-window 内容比对（见下）。
- ✅ **atomic/flock/safe-commit 原语 live**：flock 锁、atomic_replace、防劫持 commit 全在运行路径。
- 🟡 **状态板 status_board.py 半接入**：CLI 子命令可用，但**未接入运行引擎**（设计如此，S5a 只增不改红线）——它是只读观测层，不参与 dispatch。
- 🟡 **succession autoclose 默认 OFF**：opt-in 三路（`HANDOFF_AUTOCLOSE_ENABLED=1` env / `~/.claude-handoff/autoclose.enabled` / per-project sentinel），**实查全部未拨**（env unset、无任何 sentinel 文件）。producer 在 auto-continue.sh，扩展接收 leg 存在但生产中只被 succession 委托走。
- 🟡 **memory_baseline G3 验证 WARN-only**：从不阻断 relay（observe-then-enforce，A.5），硬 enforce 待 owner 拍板。
- ❌ **install.sh 不安装扩展**：它只**卸载** `dharmaxis.handoff-helper`（见 §6）；扩展靠手动 `code --install-extension` 装。

**已装 vs 源码**：扩展已装 **0.6.0**，源码 package.json **0.6.0** — **一致，无版本 skew**。
**autoclose opt-in**：**OFF（默认未拨）** — env unset + 无 sentinel。
**nonce per-window 校验**：是窗口本地自定位（每个扩展宿主只查自己 title），但用的是 **coarse 子串匹配**（`titleHasNonce` = `title.includes(nonce)`），非 F2 级 ack 内容比对——16-hex 熵让子串假阳天文级低，但严格说不是「per-window nonce 内容核对 ack/<task>.submitted」（`handoffClose.ts:65-68` 注释明确 D-1 只校验 FORMAT，匹配 ack 内容是 D-2，out of scope）。

---

## 6. 🔴 半实现陷阱

**陷阱 1（最严重）— install.sh 与 live 扩展意图直接矛盾**：
- **现象**：`install/install.sh:252-267` 步骤 5 标题写「handoff-helper VS Code extension — REMOVED (autoclose feature dropped)」，正文称扩展「obsolete」「autoclose feature was dropped — owner ruling 2026-05-31」「existed ONLY to drive autoclose」，并**主动 `code --uninstall-extension dharmaxis.handoff-helper`**。但 live 扩展是 0.6.0、含完整 singlepane + §6c reclaim + PID dead-man，磁盘有真实 host_pid.json 证明它 live 在用。uninstall 路径（`:154-161`）同样卸它。
- **后果**：任何人跑一次 `install/install.sh`（不带 `--no-extension`）就会**卸掉正在运行的 0.6.0 扩展**，导致冷启动窗口不再单栏折叠、§6c worktree 回收彻底失效（worktree 永不被 reclaim）、succession 关窗失效。这是「注释/迁移逻辑滞后于实现 ~2 周」的活坑——install.sh 的 step 5 还停在 5/31「autoclose 被砍」的世界观，而 6/06 起单栏折叠 + 6/12 起 §6c reclaim 都重新建在这个扩展上。
- **正解**：把 step 5 从「卸载迁移」改回「安装/升级扩展到 0.6.0」（`code --install-extension extension/handoff-helper.vsix`），并删掉 uninstall 路径里对 handoff-helper 的无条件卸载（或仅在版本 < 0.6.0 时升级）。在修之前，**任何人不得跑 install.sh 的扩展步骤**——必须 `--no-extension`。

**陷阱 2 — vsix 产物版本错配 / 命名不一致**：
- **现象**：`extension/` 里有两个 vsix：`handoff-helper-0.4.0.vsix`（Jun 10，旧）+ `handoff-helper.vsix`（Jun 12，**无版本号**，package.json 已是 0.6.0）。`npm run package` 产 `handoff-helper.vsix`（无版本后缀，`package.json:32`）。
- **后果**：手动装扩展时若 `code --install-extension handoff-helper-0.4.0.vsix` 会装到**旧 0.4.0**（无 §6c reclaim/PID dead-man，无 A-poll）——0.4.0 的 worker 窗口收不到 reclaim、worktree 不回收。无文件名版本号让人无法一眼分辨 `handoff-helper.vsix` 是哪版。
- **正解**：删 stale `handoff-helper-0.4.0.vsix`；package 脚本改输出带版本号文件名（`handoff-helper-0.6.0.vsix`）；安装文档/脚本固定指向 canonical vsix。

**陷阱 3 — autoclose opt-in 永未拨（按设计休眠，但要明示）**：
- **现象**：succession autoclose 全套 producer（auto-continue.sh）+ 扩展接收 leg 都在，但三个 opt-in 开关**全部未拨**（env unset、无 sentinel）。`handleHandoffClose`（`handoffClose.ts:129-148`）整个函数注释自承「no longer wired to a production URI … Safe to prune」=已死的 D-1 粗粒度关闭原语，仅作测试面留存。
- **后果**：旧中枢窗口不自动关（owner 手关）——这是**有意**的默认安全姿态（误关丢 context），非缺陷；但 readers 会误以为「装了扩展窗口就会自动收」。
- **正解**：现状正确（默认 OFF 是立法选择）；只需文档明示「autoclose 默认休眠、需 opt-in」，并清理 `handleHandoffClose` 死原语（或保留注释说明它是测试面）。

**陷阱 4 — nonce 校验是 coarse 子串、非 ack 内容比对**：
- **现象**：扩展自定位用 `title.includes(nonce)`（`handoffClose.ts:266-268`），非把 URI nonce 与 `ack/<task>.submitted` 内容核对（D-2 的事，未做）。
- **后果**：实践上 64 位熵 + 窗口本地隔离让风险天文级低；但若两个窗口标题碰巧都含同一 nonce 子串（实际不会发生），理论上可错关。属可接受的已知简化，非活跃 bug。
- **正解**：现状可接受；若要 F2 级强度，需把 nonce 比对升级到 ack-content 校验（需 ack/config plumbing，明确 out of scope）。

---

## 7. 承重事实 file:line 清单

1. 扩展已装版本 0.6.0 == 源码 `extension/package.json:5`（`"version": "0.6.0"`）— 实查 `code --list-extensions --show-versions` = `dharmaxis.handoff-helper@0.6.0`。
2. URI 契约 path 集：`/autoclose`（`handoffClose.ts:44` AUTOCLOSE_PATH）+ `/singlepane`（`handoffClose.ts:324` SINGLEPANE_PATH）。
3. nonce 格式 = 16 位小写 hex（`handoffClose.ts:69` `NONCE_RE = /^[0-9a-f]{16}$/`）。
4. 窗口自定位 = title 子串匹配（`handoffClose.ts:266-268` `titleHasNonce` → `title.includes(nonce)`）。
5. §6c reclaim 七 gate 链先于副作用（`handoffReclaim.ts:156-275`；脏页门 `:239-252`，freshness `:205-224`，自定位 `:230-236`）。
6. 关窗二次脏检（race after gate 6）（`handoffReclaim.ts:421-430` `decideCloseWindow`）。
7. PID dead-man token 写 `ack/<task>.host_pid.json`（`extension.ts:65-89` `writeHostPid`，payload 含 pid/nonce/project/task/ts）。
8. A-poll 间隔 7s（`extension.ts:33` `RECLAIM_POLL_INTERVAL_MS = 7_000`）。
9. 红顶协调器 `🧭中枢·` 前缀永不是 reclaim 目标（`handoffReclaim.ts:63` + `:321`）。
10. 单栏折叠只对 `.handoff.code-workspace`（`handoffClose.ts:365-370` `isHandoffWorktreeWorkspace`）。
11. flock 跨进程锁、内核为 fencing 权威、无 staleness（`atomic.py:145-214` `acquire_dir_lock`；migration fail-closed `:228-233`）。
12. `atomic_replace` temp+rename 防撕裂读（`atomic.py:63-94`）；`write_with_fsync` 有撕裂窗口（`atomic.py:47-60` docstring）。
13. safe-commit 4 层防御 + post-audit exit 2（`safe_commit.py:1-34` docstring，`:210-233` `_post_audit`）。
14. **install.sh step 5 卸载扩展、称其 obsolete**（`install/install.sh:252-267`，正文 `:253-256`）— 与 live 0.6.0 矛盾。
15. install.sh hooks 装 pre-commit/post-commit/pre-push/post-merge（`install/install.sh:212-218`）。
16. post-commit deploy 陷阱：改 `install/auto-continue.sh` 或 `install/dump-handoff.py` 即自动 `--sync-*` 到 live launcher（`install/git-hooks/post-commit:43-44,75-76`），带 audit gate fail-closed（`:42-62`）。
17. autoclose opt-in 三路、默认 OFF（`install/auto-continue.sh:2314-2320` `autoclose_enabled_for_project`）— 实查 env unset + 无 sentinel。
18. autoclose 失败落 `ack/<task>.autoclose_failed.txt`（`install/auto-continue.sh:2340,2401-2403`）。
19. memory_baseline G3 WARN-only，从不阻断 relay（`memory_baseline.py:17-19,232-266`）。
20. status_board 未接入运行引擎（`status_board.py:55-58` docstring「not wired into the running handoff engine … S5a 红线 只增不改运行路径」）。
21. 两个 vsix：`handoff-helper-0.4.0.vsix`（旧）+ `handoff-helper.vsix`（无版本号）— 实查 `ls extension/*.vsix`。

---

## 8. DEPLOYMENT note — post-commit 部署陷阱

**已读 `install/git-hooks/post-commit` 确认**：该 hook（per-repo 软链自 install.sh `:213`）在 commit 后检查 `git diff-tree --root --no-commit-id --name-only -r HEAD`，**若 commit 触及 `install/auto-continue.sh` 或 `install/dump-handoff.py`，自动 fire 对应 `bash install.sh --sync-launcher` / `--sync-dump`**（`post-commit:64-76`），把刚提交的版本推到 live launcher 运行时副本（`~/.local/bin/auto-continue.sh` / `dump-handoff.py`）。

陷阱含义：
- **在 worktree 内 commit 这两个 asset 会自动改 live launcher**——改 watchdog/install 的 worker commit 前必 `export HANDOFF_INSTALL_SH=/nonexistent`（hook `:26-27` `[ -f "$installer" ]` 假 → exit 0 跳过），commit 后验 live sha 未变。`src/*.py` 不匹配 → 不触发。
- **2026-06-12 audit deploy gate**：自动部署现在**还**要求匹配的 dual-brain audit evidence（`handoff audit-check`，`post-commit:42-62`）——无 evidence → 响铃 WARN + 跳过自动 sync（fail-closed，但仍 exit 0，post-commit 从不 fail commit）。人工补救 `install.sh --sync-*` 不走 gate（保留最后 live 一步在人手上，对齐 C′ 红线）。
- **non-fatal 契约**：sync 失败只 WARN 给手动补救命令，绝不 fail 已发生的 commit（`post-commit:13-18,67-71`）。
