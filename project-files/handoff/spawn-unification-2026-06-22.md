# 派窗统一规范 — Phase 0 盘点 + Phase 1 设计方向（2026-06-22 / sw-coord-p54）

> owner 立法（2026-06-22）：所有项目（含未来新项目）中枢派窗必须走**统一、规范的唯一途径**；升级在原有基础上做统一升级；**严禁再堆零散**。先全面盘点 + 统一设计，owner 审过再开工。
> 触发事件：rf-ex-research / sf-techlib-fix 两个 worker 被派到 owner 自己的桌面5（去程错落）。

## A. Phase 0 盘点 — 现状到底有多少条路（4 并行 agent 实读 + 主脑坐实）

### A.1 「派 worker」5 条路
| # | 入口 | 用在哪 | 隔离 |
|---|---|---|---|
| 1 | `dx-spawn-session.sh --project --brief --task-id` → registry `worker_isolation` → `handoff spawn` | fateforge/dharmaxis/handoff-fanout（干净单窗） | 按 registry |
| 2 | `handoff dump --project <p> --status active` + watchdog | wilde-hexe/sdgf/xunyin/styleforge/mindpersist/stageforge/rakeforge | singlepane |
| 3 | `dump-handoff.py` + launchd watchdog | **erp 专属** | 多窗共享 |
| 4 | `code -n <path>` + osascript 原子标题校验 | watchdog 挂时退路 | — |
| 5 | 终端 `claude '<prompt>'` | erp 多窗雷区/锁屏 | CLI 单栏 |

### A.2 「派新中枢」4 条路
| # | 入口 |
|---|---|
| 1 | `handoff dump --coordinator` |
| 2 | `dx-spawn-session.sh --coordinator`（老路·红顶） |
| 3 | `handoff spawn --role supervisor_succession` |
| 4 | `handoff audit-close --coordinator`（带审计闸·spawn-new-coordinator skill 现走这条） |

### A.3 正交复杂度叠加
- **隔离模式**：worktree vs singlepane（每项目 registry 配；config 还有遗留 `singlepane_projects` 列表 vs 空的 `worker_isolation` dict，两套并存）。
- **mandate**：只 `["erp-system","rakeforge"]` 走审计闸，其余 legacy。
- **去程两套路由模型**（见下，根因）。

## B. 根因 — 去程为什么千差万别（坐实 file:line）

去程（worker 落哪个桌面）在 `code-router.sh:21-53` 有**两套互斥模型**：

1. **新模型·focus-jump（对）**：URI 文件第 3 行 `SPAWNER_FOCUS=<派它的中枢 workspace 绝对路径>` 存在 → `vscode-spaces.py focus-jump` **跳到那个中枢窗口当前所在的桌面**（跟着中枢实际位置走，中枢在哪 worker 就生在哪）。
2. **老模型·静态号表（错）**：`SPAWNER_FOCUS` 为空 → 回落 `~/.vscode-spaces/projects.json` 的**静态项目→桌面号**映射 → `goto N`。

**致命点 1 — `SPAWNER_FOCUS` 在每一个生产者里都是「可选/缺省省略」**：
- `spawn.py:126` `_write_uri()`：`spawner_focus is None` 就不写这行；
- `dump.py:1069` / `1351` / `1438`：`None` → 省略；
- → 派发方一忘传 `--spawner-focus-path` / `--self-task`，或引擎 Tier-1/2 解析失败 → URI 没有这行 → **静默回落老模型**。

**致命点 2 — 老模型的静态表是错的前提**：`projects.json` 假设「每个项目固定住一个桌面」，但**中枢会移动**（一个中枢可以在任意桌面）。且表本身残缺：
```
rakeforge→5  handoff-fanout→7  erp→3  xunyin→4  wilde-hexe→8 ...
（stageforge 根本不在表里！）
```

**2026-06-22 02:15 事故复现（坐实）**：
- rf-ex-research：URI 无 `SPAWNER_FOCUS` → 老模型 → `projects.json[rakeforge]=5` → `goto 5` → 落桌面5（= owner origin·撞 owner 桌面）。
- sf-techlib-fix：URI 无 `SPAWNER_FOCUS` → 老模型 → **projects.json 无 stageforge** → 无 goto → 落「当前桌面」（rf 刚 goto 完的 5）。
- xunyin-crosslingual-fix：URI **有** `SPAWNER_FOCUS=xunyin-coord-67.handoff.code-workspace` → 新模型 focus-jump 5→6 → 落 xunyin-coord-67 真实桌面（对）。

→ **同一个 watchdog run，3 个 worker，2 套路由模型，2 个落错**。这就是「有的项目中枢正常、有的不正常」的机器级证据。

## C. 结构性病根（一句话）

**「worker 该落哪个中枢桌面」这个关键信息（SPAWNER_FOCUS）是可选的、缺了就静默回落到一张会过时的静态号表。** 可选 → 总会被某条路忘掉；静默回落 → 错了不报警。**只要这两点还在，就一定会有 worker 落错桌面。**

## D. Phase 1 统一设计方向（待外双脑独立设计 + owner 审）

核心不变量（对齐 owner 立法）：**「忘传参数 → 落错桌面」从结构上不可能发生。**

1. **唯一入口**：所有项目（含未来新项目）派 worker / 派中枢各走**一条**规范入口（其余降级为这条入口的内部实现，调用方不可见、不可绕）。候选：统一收敛到 `handoff spawn`（worker）/ `handoff audit-close --coordinator`（中枢），`dx-spawn` / `dump-handoff.py` / `code -n` / 终端 全部废弃或降为内部实现。
2. **SPAWNER_FOCUS 强制解析、fail-loud**：引擎在产 URI 时**必须**解析出「派它的中枢是谁、在哪」；解析不出 → **告警 + 拒派 / 显式降级**，**绝不静默回落静态号表落 owner 桌面**。去程只保留 focus-jump 一套模型，废弃 projects.json 静态表（或仅作显式声明的最后兜底 + 响铃）。
3. **隔离/红顶/锁屏/单栏 = 这条入口内部按项目配置自动决定**，调用方永远不需要知道、也不可能忘传。
4. **新项目零配置即合规**：新建项目接入派窗只需在 registry 声明 isolation，其余自动；缺配置 = fail-closed 报错，不靠人记。
5. **升级只在这条路上做**：任何未来增强（新隔离模式/新桌面机制）改这一条路的内部实现，不再旁开新路。

## E. 下一步
1. 本盘点文档 = Phase 0 交付（owner 可读）。
2. Phase 1：据 D 出**详细统一设计**（迁移路线：哪条留唯一正路 / 哪些废弃 / 特殊需求怎么收进 / 不破坏 live 运营的灰度）→ **外双脑独立设计 + 审到 codex-GREEN**（高爆炸半径·跨所有链）。
3. 设计蓝图交 owner 审批 → 才开工实现（实现派 worker·W-HOOK enforce 已 ON·中枢不自干）。

## 关联
- 事故链：rf/sf 去程错落（本文 B 节）。
- 立法：owner 2026-06-22「派窗统一规范·禁堆零散」。
- 现有半成品收敛信号：config `worker_isolation` dict 已定义但空（`config.py`）、`singlepane_projects` 遗留列表并存——统一时合一。
