# 派窗统一规范 — Step 4 fail-closed 完整设计（2026-06-22 / sw-coord-p55 / 待外双脑独立设计+审到 codex-GREEN）

> 承 Phase 1 蓝图 `spawn-unification-design-2026-06-22.md` §8.3 + §8.6 Step 4。
> 北极星 I1：**「派发方忘传/解析不出『派它的中枢在哪』→ worker 落错桌面」从结构上不可能发生。**
> 本文 = 中枢（sw-coord-p55）做的 Step 4 实现级设计草案。**实施由 worker 做**（W-HOOK enforce ON·中枢不自干源码）。
> **闸**：① Step 3 canary 证 fallback→0 才可翻（fleet-wide·影响 erp）② 翻 enforce = owner 业务级 gate。本设计先就绪、审到 GREEN，待两闸放行再实施。

---

## 0. 这一步到底翻什么（一句话）

引擎解析「派它的中枢 anchor」失败时，今天是 **静默 fail-open**（log_anchor_miss 后省略 `SPAWNER_FOCUS` → code-router.sh 落静态表 → 错桌面）。Step 4 把**该有 anchor 却解析不出**的派发从「静默降级」翻成 **fail-closed 拒派 + 响铃**，并把**合法无 anchor 场景**（owner 裸终端 / cron / bootstrap）从「靠碰巧 fail-open」升级成**显式声明**。翻完，I1 在引擎层结构成立。

## 1. 现状精确锚点（file:line·实证）

| 角色 | 位置 | 现行为（warn-mode / Step 1+2 后） |
|---|---|---|
| spawn 解析+miss | `spawn.py:641-662`（`run_spawn`） | `resolve_spawner_focus_path()` → None → `log_anchor_miss(reason="spawn:anchor-unresolved")` → 继续，省略 `SPAWNER_FOCUS`（fail-open）。**已在 `EXIT_FAIL_CLOSED` 可达上下文**（同函数 :615 已有 fail-closed return）。 |
| dump 解析+miss | `dump.py:812-856`（`_spawner_focus_line`） | 同上 `reason="dump:anchor-unresolved"`，但这是**返回字符串**的 helper（`"SPAWNER_FOCUS=…\n"` 或 `""`），调用点 4 处：`:1090`/`:1320`/`:1472`。**helper 无法返回 exit code** → Step 4 dump 侧须把 miss 决策**上抬**到 dump 命令层。 |
| router 静态回落 | `code-router.sh:21-57`（dharmaxis·run-in-place） | `HANDOFF_SPAWNER_FOCUS` 缺 → 落 `projects.json` goto + 写 `anchor-miss-count.log`（Step 1 telemetry 已 loud+count）。**Step 5 才删此回落**，Step 4 不动 router。 |
| resolver | `spawner_focus.py:233-294`（`resolve_spawner_focus_path`） | Tier1 worktree-cwd / Tier2 `--self-task` / Tier3 definite-session-identity；全 miss → None。**单一安全边界 `validate_spawner_focus` 不变。** |
| 解析的故意缺口 | `spawner_focus.py:181-230`（`_derive_self_from_session`） | singlepane 中枢从 repo 根 = `owner/none` 或 `supervisor/suspected` → Tier3 **拒** → 仍 miss。**这正是 Step 4 要逼显式 `--self-task`（Tier-2）补全的那一类。** |

**配置基建（复用·勿新造）**：`config.py:184-185` `mandate_projects` + `mandate_projects_configured` = per-project gating + 「空列表≠静默全关」的成熟范式 → Step 4 enforce 开关照抄此式。

## 2. 核心机制：origin 建模 + enforce 决策点

> **🔴 v2 修订（post-tribrain-R1 / 三脑 RED 收敛·见 §9 仲裁记录）**：origin **收敛为 4 值**；安全边界从「isatty 探测」改为「**trusted-source 准入**」（三脑一致：isatty 不可作安全边界·可被 pty 伪造·后台误判）。

### 2.1 引擎如何区分「该有 anchor」vs「合法无 anchor」

把「这次派发是否应当锚定中枢桌面」从**隐式**（今天靠 anchor 在不在）变成**显式 origin**：

```
origin ∈ { coordinator | interactive | system | test }
   coordinator  —— 默认。中枢派 worker/中枢（自动化链）→ 必须解析出 anchor，否则 fail-closed。
   interactive  —— 人在裸终端手派（合并旧 manual+owner·语义同=人工 TTY 干预）→ 允许无 anchor（落当前桌面）。
   system       —— 受信无头入口（合并旧 cron+bootstrap·定时/冷启动第一个中枢）→ 允许无 anchor·但须 trusted-source 准入。
   test         —— 仅测试环境（防测试用 manual/owner 污染生产 origin 语义·codex 提）。
```

- **默认 = `coordinator`**：自动化派发链（dx-spawn / `handoff dump --status active` / `audit-close` / skills）默认 origin=coordinator → **miss = fail-closed**。安全默认：忘声明 = 当中枢派发 = 强制有 anchor。
- **合法无 anchor 必须显式 + trusted-source**：见 §2.2。

### 2.2 🔴 origin 信任模型（v3 定稿·结构性消除 env 后门 / R2 三脑「最大新缺口」根治）

**v2 残留（R2 三脑收敛）**：v2 用 `HANDOFF_SYSTEM_ORIGIN_TOKEN` + 受信注入 `HANDOFF_SPAWN_SOURCE` 作 trusted-source → **自身成新后门**：① **env 继承泄漏**（gemini 最锐）——受信 wrapper 注入豁免-env 后拉起常驻子进程 → env **静默向下继承** → 该树下任何普通 CLI 误获免检 → fail-closed 从内部击穿；② token 静态可复制/可自传。

**v3 根治（中枢仲裁·simplicity-first·threat=单用户状态污染非攻击·见 §9）= 删除可伪造凭据，让「豁免」结构上不可继承：**

> **铁律：leniency（允许无锚）只能来自【不可继承】的来源；env 只能加 strictness，永不授豁免。** → 继承一个 env 永远只会让派发**更严**（更趋 coordinator 强制锚），绝不可能授予豁免 → env 继承后门**结构上不存在**（强于「消费后 pop」补丁）。

**4 条裁决规则（取代 v2 token/trusted-env）**：
1. **origin 仅来自 per-invocation CLI flag `--origin`**（默认 `coordinator`）。**无 env 自报 origin**（删 `HANDOFF_SPAWN_SOURCE` 信任·删 `origin_source` 枚举的信任用途·只留 cli|default 两态作日志）。CLI flag 每次调用显式给、不继承。
2. **`coordinator`（默认）= 强制锚**：miss → fail-closed（enforce 阶段）。**任何不满足下列豁免条件的派发都当 coordinator。**
3. **豁免（允许无锚）的【唯一来源】=【不可继承】信号**：
   - `--origin system` 无锚 ⟺ **项目 ∈ config `spawner_anchor_system_allow`**（config 文件·**不随进程继承**·cron/bootstrap headless 项目显式登记）。无登记 → 当 coordinator 拒。**无 token。**🔴 **每次 system 无锚放行必 audit-log**（`{ts, project, callsite, cwd}`·codex R3·过宽可观测）；**未来收紧**（dry_run 若显示某项目 system 用得过宽）= 缩到 `(project, entrypoint/callsite)` 二元白名单（本步留 project 级·记为可选 tightening）。
   - `--origin interactive` 无锚 ⟺ **真前台 TTY**（`sys.stdin.isatty()` AND `sys.stdout.isatty()`·**必要非充分门**·codex R3 RED + gemini/deepseek 一致建议）**AND** `HANDOFF_UNATTENDED` 未设。**TTY 在此非「安全边界」而是「必要前置」**——脱离 TTY 的无头链**即便忘设** `HANDOFF_UNATTENDED` 也**拿不到** interactive 豁免（根治 §7-2a「忘设 env」脆弱点：物理无 TTY = 物理不能豁免）。真人裸终端传 `--origin interactive` 即放行（误判代价=窗落当前桌面·人在场无害）。
   - `--origin test` ⟺ **in-process pytest 探测**（`sys.modules` 含 pytest / conftest 注入·**非**可继承生产 env）。🔴 **禁用可被生产继承的 env（如 `HANDOFF_TEST_MODE`）授 leniency**（codex R3·否则违「env 只授 strictness」）。
4. **自动链路硬约束 + 调用点契约（codex R3）**：watchdog/queue/dx-spawn **必设** `HANDOFF_UNATTENDED=1`（strictness）+ 默认 `origin=coordinator`·**禁传 `--origin interactive`**；`system` 只能从登记 callsite 出现。**契约测试钉死**（§6）。**保守默认**：任何信号测不准 → coordinator 强制锚（最严）。

**closes**：R2 env 继承/token 后门（无可伪造豁免凭据）+ R3-2a「忘设 env」（interactive 须物理 TTY·无头链结构上拿不到豁免）。豁免三来源全不可继承且默认 deny：config 白名单[不继承]·CLI flag[不继承]+物理 TTY[不可伪造继承]·in-process test[不可继承]。

### 2.3 enforce 决策点（spawn 侧）

`spawn.py:654` 现 `if spawner_focus is None: log_anchor_miss(...)`，翻成：

```python
if spawner_focus is None:
    log_anchor_miss(...)                      # 仍记（telemetry 不丢）
    if _anchor_enforced(cfg, project) and origin == "coordinator":
        _err("无法解析派你的中枢 workspace（anchor unresolved）。"
             "singlepane 中枢请加 --self-task <你的中枢task>；"
             "worktree 中枢请在中枢 cwd 内派；"
             "若确无中枢桌面（手动/owner/cron/bootstrap）请显式 --origin <kind>。")
        return EXIT_FAIL_CLOSED
    # 否则（未 enforce / 合法非-coordinator origin）→ 既有 fail-open omit 不变
```

`_anchor_enforced(cfg, project)` = 照 `mandate_projects` 式：per-project 名单 + configured 标志（reversibility + 灰度）。

### 2.4 enforce 决策点（dump 侧）— `AnchorDecision` 单对象（v2 定稿·三脑一致 方案 A）

`_spawner_focus_line()` 是字符串 helper，不能 return exit code。**三脑一致裁定方案 A（preflight 预检）·废方案 B**（B 4 调用点各判·易漏一处=fail-open 漏洞·TOCTOU）。**定稿 = 单一 `AnchorDecision` 对象**（codex 提·gemini/deepseek 附议）：

```python
@dataclass(frozen=True)
class AnchorDecision:
    focus_line: str | None      # 已验证的 "SPAWNER_FOCUS=<path>\n" 或 None（单次解析结果·缓存）
    required: bool              # origin==coordinator AND enforce(project) → True
    origin: str                 # coordinator|interactive|system|test
    origin_source: str          # cli|env|config|trusted-wrapper|derived（信任裁决看这个）
    enforcement: str            # warn | dry_run | block（§4.1 三阶段）
    miss_reason: str | None     # anchor-unresolved 等（与 isolation-miss 区分·§4.2）
```

- **dump 命令入口算一次** `AnchorDecision`（唯一解析点·调 `resolve_spawner_focus_path` 一次·**把验证后的 path 缓存进对象**），后续 **全部 4 个 writer**（`:1090`/`:1320`/`:1472` fan-in + 任何产 .uri 路径）**只消费此对象**、**禁二次读 cwd/cfg/env 重解析**（剥夺 helper 自解析权·消 TOCTOU）。
- `required and focus_line is None and enforcement=="block"` → 在写**任何产物之前** `return EXIT_FAIL_CLOSED`（原子·无半产物 .uri）。
- **fan-in `:1472` 不例外**（三脑一致）：只要它能产 .uri 触发新窗 = 属 I1 行为面。
- **dump 侧 origin 来源**：`handoff dump`/`audit-close` 加 `--origin`（默认 coordinator）+ 引擎注入 `origin_source`；watchdog succession 链默认 coordinator + `HANDOFF_UNATTENDED=1`。

### 2.5 单次解析 + 内存缓存（TOCTOU 加固·codex/deepseek）

`AnchorDecision.focus_line` = **一次解析 + 验证后缓存**；spawn 侧（`:642` 已一次解析存 `spawner_focus` 复用）与 dump 侧（入口算 `AnchorDecision`）均**禁产物路径二次独立解析/重读文件**。这同时满足三脑的 TOCTOU 关切（解析后 anchor 文件被删/改也写已验证的缓存值；router 侧本就 fail-open 兜底·Step 4 不动 router）。

## 3. 北极星闭合证明（I1 结构论证）

翻完后，对**任一 coordinator-origin 派发**：anchor 解析成功 → 落中枢桌面（既有）；anchor 解析失败 → **拒派 + 响铃**（不再静默落静态表）。故「忘传 → 错桌面」replaced by「忘传 → 立刻拒派报错」，**错桌面从引擎层结构上不可能**（I1 ✓·引擎侧）。

**诚实边界（必 surface 给 owner·非隐瞒·三脑一致「禁 overclaim 全链 I1」）**：
1. **router 静态表仍在**（Step 5 删）。Step 4 后，引擎不再产无-anchor 的 coordinator .uri，故 router 静态回落**理论上不再被 coordinator 流量触发**；但**非引擎产的 .uri**（dx-spawn `unified_spawn_enabled=false` 的 legacy DEGRADED 旁路·`dx-spawn-session.sh:354-363`·**非默认+须 owner 手动确认**；及 `--coordinator` dx-spawn fallback `:446`）仍可绕过引擎。**完整 I1 需 Step 3 收编旁路 + Step 5 删 router 回落**。
2. **🔴 Step 4 交付口径硬约束（三脑 RED·防 overclaim）= 仅「引擎层 I1」**：PR/交付文案严格限定「引擎产 .uri 的 anchor-unresolved fail-closed 已闭合」，**禁宣称全链 I1 已闭**。**验收项**：当 `unified_spawn_enabled=false` 时，测试或 log **必须显式标注「legacy bypass not covered」**（防把引擎层闭环误盖章成系统级）。legacy 收编 + router 静态删除 = **后续阻断项**（非 Step 4 可盖章完成）。
3. **multiwindow（ERP）**：无隔离多窗仍须走 anchor（蓝图 §8.5「multiwindow 仍须走 spawn anchor·否则多窗放大错桌面」）→ Step 4 enforce 对 erp 生效 = fleet 冲击点（§5）。

## 4. 配置 / API / 可逆

### 4.1 新配置（config.json·照 mandate_projects 式）— 三阶段 enforcement（v2·三脑一致补 dry_run）

```jsonc
{
  // 三阶段·每 project 独立。缺键 vs 空列表语义（照 mandate_projects_configured·见下表）。
  "spawner_anchor_dry_run_projects": [],        // 阶段2：跑全套裁决但只记 LOG_BLOCK_INTENT·不阻断
  "spawner_anchor_enforce_projects": [],        // 阶段3：真 fail-closed。e.g. ["handoff-fanout"] 先单项目
  "spawner_anchor_system_allow": [],            // §2.2 唯一 system-无锚豁免来源（config·不可继承·无 token）
  "spawner_anchor_enforce_configured": false    // 解析期算出·防「空列表」误判（照 mandate_projects_configured）
}
```

**🔴 缺键 vs 空列表精确语义（codex R2 必修·照 mandate_projects 既有式）**：

| 状态 | dry_run/enforce 名单 | 该 project 阶段 |
|---|---|---|
| 缺键（config 无此键） | — | **warn**（Step 4 前·安全默认·不静默全开 enforce） |
| 空列表 `[]` | 无任何项目 | 全 project = warn（显式空≠全开） |
| `["hf"]` | hf 在 enforce | hf=enforce·其余=warn |
| 同名同时在 dry_run+enforce | 重叠 | **enforce 优先**（取更严·codex R2） |

- **🔴 三阶段灰度（三脑一致：2h warn 数据不足撑生产 fail-closed·须 dry_run 缓冲）**：
  1. **warn**（现状·Step1+2）：被动记 `log_anchor_miss`，不跑新裁决逻辑。
  2. **dry_run / shadow**（新增）：跑**完整新裁决**（origin + §2.2 豁免判定 + preflight + miss_reason），命中本会 block 的场景**只记 `LOG_BLOCK_INTENT`、不改行为**。**与 warn 的区别 = dry_run 真跑新裁决逻辑**。**须 ≥24-48h + 覆盖真实 worker 派发 + erp 样本**（实证：本棒 canary 已抓 rakeforge singlepane-dump miss·正是 dry_run 该 surface 的 would-block）。
  3. **enforce / block**：dry_run 确认 would-block=0 后翻真 block。
- **顺序**：每项目 warn → dry_run(≥24h·0 would-block) → enforce。先 `hf`→ +erp（erp 须先单独 dry_run 证 0）→ `["*"]`。**每进一阶 = 一次 owner gate**。
- **🔴 config fail-safe（codex R2 必修·禁静默 fail-open）**：config 读取/解析/schema 失败 → `config_trusted=False`。
  > **🔴 实现裁决（impl-audit codex RED #2·中枢显式接受·2026-06-22）**：原设计字面「**仅已 enroll enforce 的 project** fail-closed·未配置不受影响」**在 config 损坏时不可实现**——config 不可读时引擎**无从得知谁曾 enroll**（enforce 名单已读不出）。两条出路（codex 给）：(a) 显式接受「损坏 config → **全 project** coordinator-anchor-miss fail-closed」为新设计决策；(b) 维护 last-known-good enrollment 快照。**中枢采 (a)**（gemini+deepseek 一致：损坏 config 退 warn=已 enroll 的关键 project 被静默穿透=致命·「Security>Uptime」最高优先·且损坏是 loud 故障[`_fail_closed_config` 打印告警]强制运维介入）。**代价诚实记**：config 短暂损坏期间·从未 enroll 的 project 的 anchor-miss 也会 block（而非 warn）——可接受（损坏=异常态·fail-closed 比 fail-open 安全）。**未来可选收紧**=last-known-good enrollment 快照（enforce-prep backlog）。**注**：仅 config 真损坏触发；warn-mode 正常运行 `config_trusted=True`·零影响（landing byte-identical 不受此条影响）。
- **一秒回滚**：移名单 / 清空 → 立即回上一阶（零部署·config 热读）。

### 4.2 裁决矩阵 + CLI + 区分错误码（v3·删 token·codex R2 allow-matrix）

**裁决矩阵**（`required` = 须有锚·miss→按阶段 warn/dry_run-log/block；输入全非继承-可伪造）：

| `--origin`(cli) | 前台 TTY | `HANDOFF_UNATTENDED` | project∈`system_allow` | → 裁决 |
|---|---|---|---|---|
| coordinator(默认) | 任意 | 任意 | 任意 | **required**（强制锚） |
| interactive | **是** | 未设 | — | 豁免（无锚放行） |
| interactive | **否** | 任意 | — | **降 coordinator → required**（无头链物理拿不到·根治忘设-env） |
| interactive | 是 | =1 | — | **降 coordinator → required**（继承 env 只会更严） |
| system | — | 任意 | 是 | 豁免（无锚·**audit-log**） |
| system | — | 任意 | 否 | **降 coordinator → required**（无 config 登记不豁免） |
| test | — | （in-process pytest） | — | 豁免（仅测试·非 env） |
| 任意/未知 | 测不准 | 测不准 | — | **保守 → required** |

- `handoff spawn`/`dump`/`audit-close` 加 `--origin {coordinator|interactive|system|test}`（默认 coordinator·**per-invocation·不继承**）。**删** `HANDOFF_SYSTEM_ORIGIN_TOKEN`/`HANDOFF_SPAWN_SOURCE` 信任（§2.2 v3）。
- dx-spawn/watchdog 自动链路**显式设** `HANDOFF_UNATTENDED=1`（strictness）+ 默认 `origin=coordinator`。
- **错误码/miss-reason 分离**（防 Step6 isolation-miss 混淆）：anchor enforce 失败 = `reason="anchor-unresolved"` + 独立 err code；Step6 `worker_isolation_for()→None` = `reason="isolation-unresolved"` + 另一 err code。error message 必区分。

### 4.3 可逆矩阵
| 回滚级别 | 命令 | 效果 |
|---|---|---|
| 单项目 | config 删该 project 名 | 该 project 回 warn |
| 全 fleet | `spawner_anchor_enforce_projects: []` | 全回 warn（行为=Step 3 前） |
| 代码级 | revert worker commit | 回 Step 2 |

## 5. 爆炸半径 / fleet 冲击（owner 必知·诚实定性）

- **影响面**：所有走引擎产 .uri 的 coordinator 派发 = hf / erp / fateforge / styleforge / rakeforge / stageforge / wilde-hexe / sdgf 等**全链**（当配 `["*"]`）。
- **erp = multiwindow**：erp 红顶中枢派 worker 若未带 anchor → Step 4 后**拒派**（今天静默落静态表）。**这是行为变**：erp 链若有「裸 dump 不带 self-task」的既有习惯 → 翻 enforce 当天会 fail-closed。**故先 Step 3 canary 证 erp 流量 fallback→0**（含 erp worker 派发样本）才可把 erp 加进 enforce 名单。
- **缓解**：灰度名单（先 hf 单项目·零 erp 风险）+ 每批 owner gate + 一秒 config 回滚 + canary 续盯。

## 6. 测试计划（worker 实施时·钉死不变量）

1. **enforce ON + origin=coordinator + anchor miss → `EXIT_FAIL_CLOSED`**（spawn + dump 各一·dump 验**无半产物**：queue/.uri 未写）。
2. **enforce ON + origin=coordinator + anchor 解析成功 → 正常产 SPAWNER_FOCUS**（Tier1/2/3 各一·零回归）。
3. **enforce + `--origin interactive` + 前台 TTY + `HANDOFF_UNATTENDED` 未设 + miss → 放行**（合法人工无锚·§4.2 矩阵）。
4. **🔴 后门防线（env 继承不授豁免）**：`--origin interactive` **+ `HANDOFF_UNATTENDED=1`**（=自动链路·含继承）+ miss → **降 coordinator → 拒**。
4b. **🔴 无头链防线（v4·TTY 必要门·根治 §7-2a）**：`--origin interactive` **+ 无前台 TTY**（mock isatty=False）**+ 忘设** `HANDOFF_UNATTENDED` + miss → **仍降 coordinator → 拒**（物理无 TTY = 物理不能豁免·不押注「记得设 env」）。
4c. **调用点契约（codex R3）**：grep/契约测试钉死 watchdog/queue/dx-spawn 派发**从不**传 `--origin interactive`·默认 coordinator·设 `HANDOFF_UNATTENDED=1`；`system` 只从登记 callsite。
5. **system 豁免仅来自 config**：`--origin system` + project∈`spawner_anchor_system_allow` + miss → 放行(+audit-log)；project **不在白名单** + miss → **降 coordinator → 拒**（无 token）。`test` 豁免仅 in-process pytest 探测·非 `HANDOFF_TEST_MODE` 可继承 env。
6. **config fail-safe**：project 在 enforce 名单 + config 读取/解析失败 → **fail-closed 拒派**（非静默回 warn·§4.1 codex R2 必修）。
7. **warn-mode（缺名单）+ miss → 既有 fail-open + log_anchor_miss**（Step1+2 字节等价·**disable-fix→该测 FAIL** 守卫）。
7. **dry_run 阶段**：project 在 `dry_run_projects` + coordinator + miss → **不阻断 + 记 `LOG_BLOCK_INTENT`**（跑全新规则·行为不变）。
8. **per-project 三阶段隔离**：hf=enforce / erp=dry_run / 其余=warn 三态并存正确（名单互不串）。
9. **`AnchorDecision` 单解析**：一次解析后 4 个 writer（含 fan-in `:1472`）消费同对象·无二次重读（§2.5 TOCTOU）。
10. **错误码分离**：anchor-unresolved vs isolation-unresolved 两 reason/err code 不混（§4.2）。
11. **空列表 vs 缺键语义**（照 mandate_projects 既有测式·三阶段各一）。

## 7. 决策点 — v2 已据三脑独立审定稿（原 Q1-Q7 见 `audits/p55-step4-design-dualbrain.md`）

| Q | 三脑收敛 | 本设计定稿 |
|---|---|---|
| Q1 origin 枚举 | 三脑一致：5 值过多·`manual`+`owner` 合并·`cron`+`bootstrap` 合并 | 收敛 4 值 `coordinator/interactive/system/test`（§2.1） |
| Q2 dump 方案 | 三脑一致 A·废 B·4 点全 enforce·单对象 | `AnchorDecision` 单解析对象（§2.4） |
| Q3 交互探测 | 三脑一致：isatty 不可作安全边界·可伪造 | 改 **trusted-source 准入**·isatty 仅辅助（§2.2） |
| Q4 灰度 | 三脑一致：2h 不足·须 dry_run 缓冲 24-48h | 三阶段 warn→dry_run→enforce（§4.1） |
| Q5 legacy 收编 | codex+gemini：**不并进 Step4**·honest-scope+验收项；deepseek：并进 | **采 codex+gemini**（§9 仲裁）·Step4=引擎层·legacy=后续阻断项+「not covered」验收项（§3.2） |
| Q6 与 Step6 交互 | 三脑一致：anchor 先于 isolation·错误码须分离 | err code 分离（§4.2） |
| Q7 最大风险 | codex/deepseek：origin 信任伪造（=Q3 同根）；gemini：中央 config 瓶颈 | 信任模型采纳（§2.2）；中央 config **不下放**（§9 仲裁） |

## 8. 闭环定义（Step 4「完成」标准）

1. 上述机制全实施（spawn+dump `AnchorDecision` fail-closed·4 值 origin·**v3 不可继承豁免**·三阶段 enforce 配置+fail-safe·裁决矩阵·分离错误码）。2. §6 测试全过（含 env-继承后门防线 #4·config-fail-safe #6·dry_run·disable-fix→FAIL 守卫）。3. 外双脑审到 **codex-GREEN**（v3 待 R3 re-audit）。4. **Step 3 canary 证 fallback→0**（含 erp/worker 样本·**本棒已抓 rakeforge miss=未达·须先 surface+修**）+ **erp 单独 dry_run 阶段证 would-block=0** = 翻 enforce 前置硬闸。5. **owner 业务级 gate** 批准每进一阶（warn→dry_run→enforce·先 hf）。6. deploy（引擎 pre-push·dx-spawn/router run-in-place deploy-audited）+ canary behavior-verified + memory 沉淀。**交付口径硬限「引擎层 I1」（§3.2·禁 overclaim 全链）。**

## 9. 三脑审仲裁记录（zero-trust·中枢自算·分歧站 codex/实证）

**R1（v1·三脑均 RED·`audits/p55-step4-design-dualbrain.md`）**——收敛缺陷**全采纳**（origin 信任模型须 trusted-source 非 isatty / AnchorDecision 单对象 / dry_run 三阶段缓冲 / 分离错误码 / 禁 overclaim 全链 I1）→ v2。**两处分歧中枢仲裁**：
- **Q5 legacy 收编时机**（deepseek「并进 Step4」vs codex+gemini「不并·honest-scope」）：**采 codex+gemini**。中枢实地核 `dx-spawn-session.sh:354-363`——legacy worker 旁路 = **非默认**（`unified_spawn_enabled=false`）**+ DEGRADED + owner 全手动确认**（exit 11·禁自动注入/提交）；默认 worker 路已 fail-closed（`:344`「绝不静默回退老路」）。并进 Step4 = 爆炸半径膨胀（动 dx-spawn run-in-place）。**改为 honest-scope（§3.2）+ legacy 收编列后续阻断项**——deepseek 的 I1 关切由「禁 overclaim + 列阻断项」满足，非靠塞进本步。
- **Q7 中央 config 下放各项目 marker**（gemini 独有·1/3）：**不采纳**。理由=本系统**单用户单机单 config**（`config.json` 已是 `mandate_projects`/`worker_isolation` 的 SOT）；下放 per-project marker 与既有架构不一致 + 增复杂度（撞 simplicity-first）；gemini 的「跨部门合并冲突/组织瓶颈」前提在单用户场景不成立。**honest 记录分歧**（非隐瞒）。

**R2（v2·三脑均 RED·`audits/p55-step4-design-dualbrain-r2.md`·degraded=false codex 61s/gemini 31s/deepseek 22s）**：**B1-B5 三脑全判闭合**（B1 信任模型 codex「部分闭」其余「闭」·B2-B5 三脑「闭」）；**Q5/Q7 仲裁三脑皆认同**。唯一残留 = **v2 自引入的 trusted-source 凭据（`HANDOFF_SYSTEM_ORIGIN_TOKEN` + 受信注入 `HANDOFF_SPAWN_SOURCE`）协议未定 = 新后门**——三脑收敛于 **env 继承泄漏**（gemini 最锐：注入豁免-env→常驻子进程静默继承→普通 CLI 误获免检→fail-closed 内部击穿）+ token 静态可伪造。
- **中枢 v3 仲裁（simplicity-first·threat=单用户状态污染非攻击）**：**采 gemini「env 继承」诊断为根问题，但拒 deepseek 的「非对称签名/HSM/KMS」过度工程**（单用户无对抗攻击者·密码学 token 是 comprehensive-architecture 反射）。**根治 = 删可伪造凭据**：leniency 只来自【不可继承】来源（config 白名单 + per-invocation CLI flag）·env 只授 strictness（`HANDOFF_UNATTENDED` 设了只会更严）→ **env 继承结构上不能授豁免**（强于 gemini 的「消费后 pop」补丁·因压根无豁免-env 可继承）。codex「config fail-safe / allow-matrix / 缺键-空列表语义」全采纳（§4.1/§4.2）。→ v3（§2.2/§4 重写）。

**R3（v3·gemini+deepseek GREEN / codex RED·`audits/p55-step4-design-dualbrain-r3.md`·codex 116s 深审）**：三脑一致确认 **R2 残留（env 继承+token 伪造）结构根治**（gemini「完美自洽 fail-safe」·deepseek「无残留」）。**分歧 = interactive 豁免的「忘设 strictness」残口（=我 brief §7-2a 自捅的刀）**：codex RED 要求 interactive 须**正向人机信号（前台 TTY）非仅 env 缺席**；**gemini+deepseek 虽判 GREEN 但都独立建议同一 TTY 门**（gemini「结合 isatty 探测·脱离 TTY 即剥夺 interactive 豁免·防御纵深无懈可击」·deepseek「(a) 点最严重·需流程管控」）。
- **中枢 R3 仲裁（站 codex·反 consensus-laundering）**：**采 codex RED + 一致 TTY 建议为必修**——**禁**拿「2/3 GREEN」给 codex 的有效有据 RED 背书（三脑实为收敛于同一 TTY 门·codex 称必修、另两脑称强烈建议·实质一致）。v4 改 **interactive 豁免 ⟺ `--origin interactive` AND 物理前台 TTY（必要非充分门）AND `HANDOFF_UNATTENDED` 未设**——TTY 在此是「必要前置」非 R1 否决的「唯一安全边界」（它只会 restrict·无头链物理拿不到豁免）→ **结构性根治「忘设 env」**（不再押注自动链路纪律）。并采 codex：system 每次 audit-log + callsite 契约测试 + test 豁免改 in-process pytest 探测（非可继承 env）。→ v4（§2.2/§4.2/§6）待 **R4 验证**。

## 关联
蓝图 `spawn-unification-design-2026-06-22.md` §8.3/§8.6 / Phase 0 `spawn-unification-2026-06-22.md` / [[feedback-no-partial-offload-to-nontech-owner]] / [[thorough-execution]] / owner 北极星立法 2026-06-22。
