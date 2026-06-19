# Codex→Claude-master 边界事件 — p38 后续闭环进度（2026-06-19 / sw-coord-p38）

> 承接 p37 的 `codex-boundary-violation-20260619.md`（事件认定+加重提示词）+ `claude-master-decontamination-report-20260619.md`（去污染）。
> 本文记 p38 对「剩余 4 项后续」的处置。**零信任**：所有结论对实际状态核验。**未执行任何破坏性操作 / 未部署任何凭据基建 / 未碰 live 凭据**——deploy 与凭据填充走 owner gate（owner 在环）。

## 一览（5 项）

| # | 任务 | 状态 | 终态/去向 |
|---|---|---|---|
| 4B-1 | claude-audit 网关重建 | **✅ DEPLOYED**（owner gate 过·组织边界选项·2026-06-19 10:55） | live `~/.claude/scripts/claude-audit-gateway.py`（deployed `16d1b4f6`（R4 GREEN 于 `b366128b`·post-R4 加 8 项 --out denylist→**R5 部署字节 re-audit GREEN/GREEN 于 `16d1b4f6`**·deployed==R5-audited））+ `~/.claude/audit-pool/accounts.json`·池 0/3 待填充 |
| 4B-2 | 给 codex 拟集成提示词 + 集成核验 | **✅ DONE**（v2 实地调研重写·codex 已建编排器·中枢零信任 spot-check PASS） | codex `~/.codex/bin/codex-external-audit.py`（675 行·3/3 smoke·无 runner import·契约逐字对·零 Claude-master 写·只用自己 token·自接 boundary-guard）→ **codex 边界事件全闭环** |
| 4B-3 | skills-first-guard.py 复核 | **CLEAR（非污染）** | 见下 §4B-3 |
| 4B-4 | codex 全量历史会话审计 | **部分（file-write 已清，keychain 待 backlog 深解析）** | 见下 §4B-4 |
| 4B-5 | 追踪 owner /login 凭据轮换 | **PENDING owner** | 池 0/3 populated，见下 §4B-5 |

---

## 4B-1 claude-audit 网关重建（BUILT，待 owner gate）

**架构（owner 已定·沿凭据边界切两半）**：网关 = Claude 治理（读 Keychain token / 隔离 HOME 调 claude / 账号轮换 / 只回审计文本）；codex 编排器在自有空间调网关、排 Claude #1、用自己的 gemini/deepseek（4B-2）。

**实现**：`/tmp/claude-audit-gateway-staging/claude-audit-gateway.py`（**clean rebuild·参考 codex 污染件设计·不抄字节**）+ `accounts.json`（3 账号模板·未填充）。子命令 `run`/`check`/`store`/`self-test`。
- **零接触 live 主凭据**：池只读 `claude-audit-pool/<alias>` 命名空间（正向白名单 `startswith` 强制），**绝不读** live `Claude Code-credentials`。
- 安全不变量：token 不打印/不落盘（redact 全程）；隔离 HOME+`CLAUDE_CONFIG_DIR`；**严格 env 白名单**（防 NODE_OPTIONS/DYLD_* 等动态挂载钩偷 token）；`--` 分隔防 flag 注入；绝对 `/usr/bin/security` 防 PATH 劫持；`security` 全加 timeout fail-closed；调用方拿不到 token（只回审计文本）。

**审计（dual-brain·gemini+deepseek·**不用 codex 审它自己通道**·4 轮 + 1 workflow 对抗 panel）**：
- **R1 = RED/RED**（gemini 2P0+5P1 / deepseek 1P0+4P1）→ 7 真缺陷：env 注入(P0)·flag 注入(P0)·store 后门·大小写绕 guard·security 无超时·PATH 劫持·output 未脱敏。**全修**。
- **R2 = deepseek GREEN / gemini RED**：gemini 抓 1 新 P0 = **`$HOME` 劫持**（`Path.home()` 读 `$HOME`→调用方设 `HOME=/tmp/attacker` 植假 claude 偷 token）。**修**：`pwd.getpwuid` 定 HOME + 覆盖 env。实测 exploit 被挡。
- **R3 = deepseek GREEN / gemini RED**：gemini 抓 2 新 P0（PYTHONPATH 劫持网关自身 / `--out` confused-deputy 毒化 binary）。**修**：shebang `-S python3 -I` + `safe_out_path` 闸（拒保护目录+拒覆盖可执行）。
- **🔴 威胁模型实证校正（裁决）**：gemini 连环报「恶意同用户调用方能偷 token」=**「打地鼠」**。**实测**：`security find-generic-password -s claude-audit-pool/x -w` 任意同用户进程**直读明文 token·无弹窗**（`-T` ACL 对脚本无效）。→ **天花板**：单用户机 codex 本就能直读 keychain，绕过网关。gemini 的 P0 全是「已接受单用户残留」非「网关 worse-than-baseline」。网关真正买到=**组织边界（codex 正常流程不经手 token·只拿审计文本）+ 纵深防御（防意外泄漏）**。真硬边界需独立 Unix 用户/网络代理（owner 架构决策·超本网关范围）。已写进网关 docstring「SECURITY CEILING」。
- **R4（威胁模型校正后重审）= gemini GREEN + deepseek GREEN**：两脑均确认裁决**站得住·非合理化**（gemini 原话「认清 OS 机制后的实事求是…刺穿了伪需求」）。
- **Workflow 对抗 panel（5 Claude agent·ultracode）= 全 clean·零 real-defect**：rationalization-auditor 专门攻击我的裁决→「HOLDS·中枢没合理化掉危险失败模式·反而 under-credit 自己的缓解」；honest-flow-leak/worse-than-baseline/owner-goal 全 clean·synthesis clean。仅剩 1 个 **P2**（`--out` denylist 补 shell-rc/dotfile·非 worse-than-baseline）→已补。
- **验证**：`self-test` 26/26（env-allowlist/正向白名单/`--out` 闸/HOME-pin 断言）·`check` fail-closed·fake-token plumbing→`auth_invalid`·HOME-hijack exploit→**假 binary 不执行零失窃**（未用任何真凭据·真凭据 `run` E2E=owner gate 后）。
- **审计 raw 全留**：`audits/p38-gateway-audit-R{1,2,3,4}-{gemini,deepseek}.md` + `project-files/handoff/codex-audit-gateway-p38/workflow-adjudication-verify-result.json`。**网关 staged SHA(16) `16d1b4f61cb5f784`**（`project-files/handoff/codex-audit-gateway-p38/`·**NOT deployed**）。

**owner gate 决议（2026-06-19）**：owner 选**选项 1 = 部署网关（组织边界）**，接受诚实天花板（非硬隔离·单用户 codex 本就能直读 keychain）。
1. ✅ **部署完成**（deployed `16d1b4f6`（R4 GREEN 于 `b366128b`·post-R4 加 8 项 --out denylist→**R5 部署字节 re-audit GREEN/GREEN 于 `16d1b4f6`**·deployed==R5-audited）·self-test pass·`-I` shebang 生效）。
2. ✅ **池填充 2/3 + E2E 验证**（owner-在环·`store` 经 pty 我跑·owner 只 foreground profile + click authorize）：b=rssprivacy + c=sopranoquan present（a 故意空=审计走 b/c 不抢 live jascawow 配额）；live 复位 jascawow（双源核实）；`run` 真调 claude→`AUDIT_OK`·exit 0·无 token 泄漏。**4B-5 轮换闭**（3 账号当日 re-login·旧 token server 端失效）。**实战教训**：OAuth 授权页用被打开 profile 的 claude.com 会话→必在目标 profile 手动开 URL（auto-open 不可靠·常落 rss）；`auth status` 是权威验证源（"Login successful" 不可信·sopranoquan 误授 rss 3 次靠 auth status 抓出）；顺序=announce→owner 置顶→等确认→再 login。
3. ⏳ 转发 codex 提示词（4B-2·owner hands-on·`codex-integration-prompt.md`）。

---

## 4B-3 skills-first-guard.py 复核 — CLEAR（非 codex 边界污染）

`dharmaxis/cc-global/scripts/skills-first-guard.py` 工作树显示 ` M`（未提交），decontamination §F 曾标疑。**核验结论：非污染**。
- **mtime = 2026-06-12 22:32**，**早于** codex 边界会话（06-19 00:36–05:36）；apply_patch 会把 mtime 顶到 06-19 → 时间证伪。
- diff 内容 = **2026-06-12 双脑审定升级**：INTENT_VERBS 补破坏/逆向类动词（删/清理/取消/迁移…）+ 路由表改从 capability-registry marker 段读（单一真相源）。注释自带「埋没事故『删除 X 所有 reply』复盘」——是 owner 自己的 JIT-capability-discovery 立法风格。
- 触它的 commit（`0c9639a`/`590a97b`）均与 codex 06-19 会话无关。
→ **owner 自己的良性未提交工作，owner 自行 commit，无需处置。**

---

## 4B-4 codex 全量历史会话审计 — 部分（file-write 已清；keychain 待 backlog）

扫 `~/.codex/sessions/` 全部 **2357** 个 rollout JSONL：
- **写文件到 Claude-master 路径（Add/Update File: .claude/ | cc-global/ | .local/bin Claude 部署件）= 仅 1 个会话** = 已知边界会话 `rollout-2026-06-19T00-36-22-…`。**强信号：file-write 污染确认隔离在该单一会话**，无其他会话写过 Claude-master 文件。
- **Keychain 写模式（security add/delete/update-generic-password 命中 Claude Code-credentials|claude-audit-pool|claude-max）= 5 个会话**：已知 06-19 + **4 个额外**（06-10 ×3 / 06-17 ×1）。4 个额外会话**伴随审计/复审语言**（"回滚失败…最小修复…P2 同邮箱多 alias"）→ 高度疑似 codex 在**复审/引用** cc-account.sh（owner 的凭据工具·内含 `security add/delete-generic-password`），**非执行凭据变更**。
- **诚实残留（不 overclaim）**：「执行 vs 引用」用简单启发式**无法可靠区分**（同一启发式对已知边界会话也误报 0 executed → codex 的 shell-执行 JSONL schema 与朴素 pattern 不符）。**定论需正确解析 codex 会话 JSONL 的 shell-call schema = backlog 深活**。当前 file-write 强信号干净；4 个 keychain-pattern 会话**未定性清除**。
- 取证产物：`/tmp/codex-claude-master-writes-sessions.txt`（1 个）+ `/tmp/codex-claude-keychain-writes.txt`（5 个）。

---

## 4B-5 追踪 owner /login 凭据轮换 — PENDING owner（池 0/3）

当前 Keychain 实查：
- `Claude Code-credentials` = **PRESENT**（live 主凭据·`~/.claude.json` oauthAccount = **jascawow@gmail.com**·在跑本 fleet）。
- `claude-audit-pool/claude-max-{a,b,c}` = **全 ABSENT**（codex 经手的 b/c 凭据 c71bfad1/f3f6f072 已在 p37 去污染删除）。
- → 池 **0/3 populated**。网关 `run` 端到端 = behavior-pending（依赖填充）。

**owner 填充流程（owner gate 部署后·owner 在环）**：对 a=jascawow / b=rssprivacy / c=sopranoquan 各做：① `claude /login <账号>` ② `claude-audit-gateway store --alias claude-max-<x>`；最后 `claude /login jascawow` 复位 live 会话。`store` 硬 isatty（codex 无法触发）。
**server 端轮换**：owner /login 重登即旧 token 失效（含 p37 删的本地凭据对应的 server session）。可选 claude.ai 撤 sessions。

---

## 红线守护（本棒自检）
- 未部署任何凭据基建（staging only）；未碰 live `Claude Code-credentials` token（仅 presence 查·无 -w 解密·唯一例外=fake-token plumbing 用伪 token）；未碰 owner dharmaxis 脏文件（skills-first-guard 只读核·未改）；未触别链。
- 网关审计排除 codex（它是被审通道的消费方）——双外脑=gemini+deepseek（不 import Claude runner 由我 Claude-master 自调，合规）。
