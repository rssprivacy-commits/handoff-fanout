# 派窗统一规范 — Phase 1 完整设计（2026-06-22 / sw-coord-p54 / 待外双脑独立设计+审）

> 承 Phase 0 盘点 `spawn-unification-2026-06-22.md`。owner 立法：唯一规范途径·禁堆零散。
> 本文 = 我（中枢）做的完整技术设计草案，将交外双脑独立设计+审到 codex-GREEN 后定稿成蓝图给 owner 业务级审批。

## 0. 核心不变量（设计第一性约束）
**I1：「派发方忘传/解析不出『派它的中枢在哪』→ worker 落错桌面」从结构上不可能发生。**
- 不可能 = 要么引擎强制解析成功、要么 **fail-loud 拒派/响铃**，绝无「静默落静态表/owner 桌面」。

**I2：所有项目（含未来新项目）派 worker / 派中枢各走唯一规范入口。** 其余路废弃或降为该入口内部实现，调用方不可见、不可绕、不可忘参数。

**I3：隔离/红顶/锁屏/单栏/审计闸 = 引擎内部按项目配置自动决定。** 新项目零配置即合规（缺配置=fail-closed 报错，不靠人记）。

**I4：升级只在唯一路上做。** 禁旁开新路。

## 1. 唯一规范入口（技术决策）

### 1.1 派 worker = 引擎 `handoff spawn` 是唯一 URI 生产者
- 所有派 worker 最终都经**引擎单一 URI 生产代码路径**（`spawn.py:_write_uri` 收口为唯一产出点）。
- `dx-spawn-session.sh` → 瘦 shim，只调 `handoff spawn`（6b 已基本如此）；**删除其 degraded `code -n` 旁路**（A.1 路 4 退路降为「仅人工应急、自动化禁用」并文档化）。
- `dump-handoff.py`(ERP) → 已是 re-exec 到 `handoff_fanout.dump` 的 shim，统一走同一生产者，ERP 特殊只体现在**配置**（inject_blocks/roadmap·已 gate），**无独立 spawn 逻辑**。
- `handoff dump --status active`（接续/succession）、`handoff audit-close --coordinator`（中枢）→ 同一生产者。
- skills（spawn-new-session / spawn-new-coordinator）→ 只调引擎命令，**禁手搓 osascript/code -n**。

### 1.2 派中枢 = `handoff audit-close --coordinator` 唯一入口
- 已是 spawn-new-coordinator skill 主路；`dx-spawn --coordinator` 老路**降为唯一 fallback 且仅当引擎 ack 失败**（最终 Step 3 收编进引擎统一产出）。
- 红顶由 role 单点推导（role=supervisor_succession → 红顶），`--coordinator` 内部置 role，**消除 spawn(role 驱动) vs dump(flag 驱动) 的红顶激活不对称**（盘点 §C 缺陷③）。

## 2. SPAWNER_FOCUS 强制化 + fail-loud（核心修复）

### 2.1 引擎单一生产者**必须**解析 SPAWNER_FOCUS
产 URI 时，引擎据派发会话身份确定性解析「派它的中枢 workspace」：
- worktree 中枢 → cwd Tier-1（cwd 有 `.handoff.code-workspace`）。
- singlepane 中枢 → Tier-2 `--self-task`（singlepane env 不带路径）。
- 收编 `audit-close` 现缺的 Tier-1 尝试（盘点 §F.1 不对称：audit-close 只试 Tier-2）→ **三入口同一解析器 `resolve_spawner_focus_path`，对称无缺口**。

### 2.2 解析不出 = fail-closed（不再静默省略）
- 现状：`spawner_focus is None` → 省略该行 → 静默回落静态表（致命点1）。
- **新**：解析不出 → 引擎**拒产 URI + 明确报错**：「无法解析派你的中枢 workspace——请 `--self-task <你的中枢task>` 或在中枢 cwd 内派」。**派发方一忘=立刻报错，而非静默落错桌面。**
- 罕见合法「确无中枢桌面」（owner 裸终端手派）→ 须显式 `--no-spawner-focus`（响亮 opt-out），**非静默默认**。

### 2.3 去程只留 focus-jump 一套模型，废静态表
- `code-router.sh` 删除 `projects.json` 静态桌面回落（致命点2：假设项目住固定桌面=错·中枢会动·stageforge 都不在表）。
- 迁移期过渡：保留静态表但**每次回落响亮 WARN + 计数**（把静默→显式），Step 4 删除。

## 3. 配置合一（消除盘点 §A.3 双套）
- 隔离唯一来源 = registry `worker_isolation[project]`（worktree|singlepane）。
- **废 `singlepane_projects` 遗留列表**，全迁入 `worker_isolation` dict；缺配置 = fail-closed 报错（非猜默认）。
- mandate（审计闸）= 引擎内部据 `mandate_projects` 自动套，调用方无感。

## 4. 新项目零配置合规
新建项目接入派窗：registry 声明 `worker_isolation` 一项即可，其余（红顶/锁屏/单栏/去程/审计）引擎自动。缺 `worker_isolation` → spawn 时 fail-closed：「项目 X 未声明 worker_isolation」。

## 5. 迁移路线（不破 live·可逆·canary 每步）
| Step | 动作 | 风险闸 |
|---|---|---|
| 1 | 引擎单一生产者**恒解析** SPAWNER_FOCUS；解析不出 → 响亮 WARN + 暂仍写静态回落（静默→显式·零破坏）。canary 计 fallback 次数。 | 纯增告警·可逆 |
| 2 | 翻 解析不出 = fail-closed（`--no-spawner-focus` 显式 opt-out）。待 Step 1 canary 证 fallback→~0（派发方都已带参）。 | 行为变·owner gate·canary |
| 3 | dx-spawn/dump-handoff/skills 全收编到单一生产者·删各自 spawn 旁路。 | 逐路迁·每路 E2E |
| 4 | 删 code-router.sh 静态表回落。 | 待 Step2 稳 |
| 5 | config `singlepane_projects`→`worker_isolation` 合一·删遗留列表。 | 配置迁·golden 校验 |
每步：外双脑审 + canary + 可逆回滚命令。

## 6. 闭环定义（本任务"完成"标准·对齐 #1 元原则）
1. 上述 Step1-5 全实施（非 P0 部分）。2. 回归测试覆盖（含 fail-loud 路径·对称解析·配置合一）。3. 外双脑审到 codex-GREEN（含独立设计阶段）。4. 真实派窗 E2E：rf/sf 类无 SPAWNER_FOCUS 场景现 fail-loud 而非落错桌面 + 正常派落对中枢桌面。5. deploy-audited（run-in-place）+ pre-push（引擎）+ canary behavior-verified + memory 沉淀。

## 7. 待外双脑独立审的关键决策点（让外脑也独立出方案）
- Q1：唯一入口收口到 `handoff spawn` 是否最优，还是新建一个更高层 `handoff dispatch` 统一 worker+coordinator？
- Q2：解析不出 = fail-closed 是否过激（会不会卡住合法边缘场景）？vs 响亮降级。
- Q3：废静态表 projects.json 有无我没想到的合法用途（非中枢派窗场景）？
- Q4：迁移 5 步顺序/灰度是否安全·有无更小爆炸半径的切法。
- Q5：配置合一时 ERP（多窗无隔离）怎么进 `worker_isolation` 模型（它既非 worktree 也非 singlepane·是第三态 multiwindow？）。

## 8. v2 定稿蓝图（外双脑独立设计+审后 / tribrain 3/3 GREEN / `audits/p54-spawn-unify-design.md`）

三脑独立设计收敛 + codex 实地仲裁，纳入下列修正后定稿（草案方向全对，强化 6 点）：

### 8.1 唯一内部 producer（Q1 定稿）
- **唯一内部生产管线** `LaunchIntent → SpawnPlan → URI → router placement`（3 脑一致的承重不变量）。
- 外部表面：`handoff spawn --kind worker|coordinator`（参数化·2/3 主张·gemini「God command」顾虑用 **per-kind 严格校验**化解）；worker/coordinator/succession 全是同一 producer 的不同 intent，**禁独立 URI 生成逻辑**。

### 8.2 SPAWNER_FOCUS 升级为完整 anchor（codex 硬改进·采纳）
- 不只是 mandatory **path**，升级为 mandatory **anchor** = `workspace_realpath + coordinator/task id + 引擎生成的 exact title/window token`。
- 理由：纯路径在多窗口/同 repo 多中枢时歧义（focus-jump 认窗靠 title token）；anchor 三元组才唯一定位。

### 8.3 fail-closed + 显式 origin 建模（Q2 定稿·3 脑一致 fail-closed）
- coordinator-anchored dispatch 解析不出 anchor → **fail-closed**（loud-degrade 在 fleet 级=变相静默失败·窗仍落 owner 桌面）。
- 合法非锚场景**显式建模**（非「降级」）：`origin=manual|owner|cron|bootstrap` + `placement=current|none|ask`。即调用方**显式声明**「这不是中枢锚定派发」，而非 anchor 失败后偷偷降级。
- `--no-spawner-focus` **严格限制**（deepseek）：仅非工作/手动；生产调用链 `allow_unattended_spawn=False` 禁用，防后门复活静态表。

### 8.4 静态表 projects.json 降为 bootstrap-only（Q3 定稿·调和分歧）
- **绝不被 router 在缺 anchor 时自动吃**（3 脑一致）。
- 保留唯一合法用途 = **冷启动**（gemini 实证场景：派**第一个**中枢时无任何中枢窗可 focus-jump）→ 仅 `--bootstrap`/`origin=bootstrap` 显式调用时用作绝对定位·改名隔离（如 `bootstrap-desktops.json`）·普通链路彻底禁用。

### 8.5 配置合一·枚举·安全默认（Q5 + codex 矛盾修正）
- `worker_isolation` 改**枚举**：`worktree | singlepane | multiwindow`（ERP 等遗留非隔离·`multiwindow`=标准 OS 原生并发·引擎旁路隔离逻辑·但**仍须走 spawn anchor**·否则多窗放大错桌面）。
- 「缺配置 fail-closed」与「新项目零配置」矛盾 → **配置文件/schema 缺=fail-closed**；**项目 entry 缺=走 `worker_isolation.default`**（必须存在的全局安全默认·新项目零配置即合规·缺声明走 default 非报错）。
- 废 `singlepane_projects` 遗留列表·全迁入枚举 dict。

### 8.6 迁移顺序倒置（Q4 定稿·3 脑一致纠我草案 Step2/3 反了）
| Step | 动作 | 模式 |
|---|---|---|
| 1 | 引擎中央 resolver/producer + telemetry：所有旧路径仍跑·缺 anchor **必 WARN+count** | warn·零破坏 |
| 2 | **先**把 wrappers/ERP dump/skills/audit-close **全收编中央 producer** | warn·流量收敛 |
| 3 | canary 观察·确认 fallback **归零** | 验证闸 |
| 4 | **再**翻 coordinator-anchored 缺/歧义 anchor = **fail-closed** | 行为变·owner gate |
| 5 | 静态表从 auto-router 删除（仅留显式 bootstrap） | — |
| 6 | config `singlepane_projects`→`worker_isolation` 枚举合一·跑三类 canary（singlepane/worktree/multiwindow） | golden 校验 |
- **关键**：先收口流量再翻硬闸（否则旧路径未收口就 fail-closed=大面积阻塞·gemini「人为宕机」警告）。

> 📌 **Step 1 范围红线 — audit-close succession 在 Step 1 保持 Tier-2-only（DEFERRED 修正 / sw-spawn-unify-s1fix 2026-06-22）**
> Step 1 = 纯观测（telemetry/WARN+count），**真零行为变化**。把 **audit-close succession 收编到中央 Tier-1-first `resolve_spawner_focus_path`**（即 §2.1 / GAP §F.1「audit-close 现缺的 Tier-1 尝试」对称化）属于 **Step 2 流量收敛**，是**真行为变化**、**非 Step 1**：从任一带 `.handoff.code-workspace` 的 worktree cwd 跑 audit-close 且 predecessor sidecar 也存在时，Tier-1-first 解析会选 **cwd workspace**，旧 Tier-2-only 选 **predecessor sidecar** → 产出的 `.uri` `SPAWNER_FOCUS=` 行变 → worker 落不同桌面。
> 因此 `codex_audit.py:_succession_relay` 在 Step 1 **保持直接 `derive_singlepane_focus(home, project, predecessor_task)` 作显式解析**，产出的 `.uri` **字节等价 pre-unification HEAD**。
> 🔴 **精确合同（re-audit codex 仲裁澄清 / 非「字面 Tier-2-only end-to-end」）**：Step 1 的合同是 **「与 HEAD 字节等价」**，**不是**「succession 一定 omit/Tier-2-only」。HEAD 既有行为 = `_succession_relay` 把 `derive` 结果（含 None）传给 `run_spawn`，而 `run_spawn:if spawner_focus is None` **本来就有** Tier-1 cwd fallback（`spawn.py:~641`·pre-existing·本次未改、只在其后加 telemetry）→ 故 **derive 返 None（无 predecessor sidecar）且 cwd 是带 workspace 的 worktree 时，succession 在 HEAD 与 Step 1 都会 emit Tier-1 `SPAWNER_FOCUS`**（一致·非本次引入）。**「succession-miss 是否该 fail-loud / 不落 Tier-1」= §F.1 / Step 2 的真行为变化**，连同对称化一起做、自带等价审 + canary。Step 1 用真 guard 测试钉死「succession-miss-from-worktree-cwd == HEAD」（非 mock-resolver）。
> 配套硬化（Step 1 内合法·只收紧）：`resolve_spawner_focus_path` 的 **Tier-1 候选加 project 绑定**（cwd workspace 须属目标 project 的 `worktrees_root`，否则 drop）——治「从 B 项目 worktree cwd 跑 A 项目 spawn/succession 被 Tier-1 误抢 B workspace」的跨项目误解析；正常同项目派发无变化。

## 9. 实施中新发现（一次到位·纳入 unification 范围）

### 9.1 🔴 task-id 18 字符截断 → branch/worktree 撞名（实证 2026-06-22）
派 `sw-spawn-unify-s1fix`(Fixer-1) 与 `sw-spawn-unify-s1fix2`(Fixer-2) **都截断成 `sw-spawn-unify-s1f`**（引擎 18 字符 slug 上限）→ 同 branch `handoff/sw-spawn-unify-s1f` + 同 worktree 目录 → 撞名。本次 brief 路径对（功能没坏·两 Fixer 都在共享 ste worktree 干活），但**若两个真并发 worker 撞名→branch/worktree/ack/sentinel 全共享→灾难**（错窗注入 / ack 互相覆盖 / 收养闸认错）。
- **根因**：task-id 截断**无碰撞检测**（截断后不校验唯一性）。这是「派窗脆弱」病的又一现形（与 SPAWNER_FOCUS 可选同类=可选/隐式失败）。
- **纳入 unification（唯一 producer 内）**：引擎产 slug 时**截断后查唯一性**（撞 → fail-closed 报错「task-id 截断后与现有 <slug> 撞名·请改短/改名」或自动加 hash 后缀去歧义）。归 §1.1 唯一 producer 的内部强制·调用方无感。

## 关联
Phase 0 盘点 `spawn-unification-2026-06-22.md` / 外审 `audits/p54-spawn-unify-design.md`(tribrain 3/3 GREEN) / owner 立法 2026-06-22 / [[thorough-execution]] / [[feedback-no-partial-offload-to-nontech-owner]]。
