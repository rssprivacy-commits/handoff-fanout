# 架构图：CODEX 审计门禁 + 交付审计 evidence 闸 + 中枢交棒

**子系统**：codex audit gate · delivery-audit machine gate · supervisor succession relay
**仓库**：`/Users/chenmingzhong/Projects/handoff-fanout` · git HEAD `5e8d7b2`（read-only 勘察）

---

## 1. 运行时角色

这个子系统是 handoff-fanout 的「**缺陷不下传**」门禁层，加上中枢→中枢的接力机制。三条相互独立但同源的承重链：

**(A) Codex 审计门禁（dump 时 / "缺陷不下传"）** — 当 `HANDOFF_AUDIT_MANDATE=1` 时，任何 `handoff dump` 前 retro gate 强制跑 G0-G9（`evaluate_audit_gate`）：会话改了代码却没有一份通过的 codex 审计块（`codex_audit`），dump 被打回 RETRY→BLOCKED。审计经四种模式（full / empty_diff / docs_only / bypass）之一兑现；bypass=欠债（强制 follow-up 任务 + 截止期）。`audit-close` 是把 codex-audit 块**折叠进 retro evidence、并在同一把锁下完成 gated dump** 的单一入口。

**(B) 交付审计机器闸（push 时 / "中枢审派出会话交付必加外双脑"机器化）** — 这是另一套、独立于 (A) 的闸：`audit_evidence.py` 提供 `handoff audit-check`，被 `.git/hooks/pre-push`（硬拒）和 `post-merge`（仅 warn + 写 `.audit_pending`）调用。它按 head_sha / patch-id + changed-files 把外双脑 runner 产出的 `*.evidence.json` 匹配到被推区间，RED→拒，唯一放行=owner 亲手 tty 跑 `audit-override`。

**(C) 中枢交棒（succession relay）** — `audit-close --coordinator --status active` 在内部 gated dump 返回 0 之后，签发一次性 succession 授权 token（sidecar hex16 nonce），抑制 dump 自己发窗（`suppress_spawn_artifacts`），改由进程内 `spawn --role supervisor_succession` 消费 token 直接发出继任中枢窗 + 关闭前任窗。这是唯一能合法关前任中枢窗的路径，堵住「中枢零复盘交棒」的 G4 漏洞。

---

## 2. 核心模块

| 文件 | 一句话责任 | LOC |
|---|---|---|
| `src/handoff_fanout/codex_audit.py` | 审计门禁全部：owner_ack_token、bypass 旁路 producer、`build_codex_audit_block`、G0-G9 `evaluate_audit_gate`、`audit-close` CLI、`_succession_relay` | 2683 |
| `src/handoff_fanout/audit_evidence.py` | 交付审计机器闸：`audit-check`（head_sha/patch-id 匹配 evidence.json + verdict 裁决）+ `audit-override`（owner tty 红覆盖） | 495 |
| `src/handoff_fanout/succession_authority.py` | 一次性 succession 授权 token：`issue_token` / `consume_token`（路径含界 + 0600 + 文件名↔payload + project + successor_task + TTL 120s + 单次 unlink） | 220 |
| `src/handoff_fanout/spawn_nonce.py` | 不可猜的 per-spawn 64-bit hex nonce + window.title 装配/校验（焦点漂移 TOCTOU 防护） | 23 |
| `src/handoff_fanout/retro_gate.py`（消费方） | mandate-ON 时调 `codex_audit.evaluate_audit_gate`（`:1458`），把 outcome.klass 映射到退出码协议 | — |
| `src/handoff_fanout/spawn.py`（消费方） | `--role supervisor_succession` 调 `consume_token`（`:603`）发继任窗 | — |
| `install/auto-continue.sh`（消费方） | Phase C 逾期扫描器 `scan_overdue_kind`（`:2157`），对 `audit.override.json` 扫 follow-up 逾期债（`:2245`） | — |
| `install/git-hooks/pre-push` · `post-merge` | 调 `audit-check` 把闸接到 git 推/合并事件 | — |

---

## 3. 工作机制

### 3.1 `audit-close` 如何把 codex-audit 块折叠进 retro evidence（单锁一气呵成）

`main_audit_close`（`codex_audit.py:2312`）流程：
1. 解析 `--audit-mode`（four modes）+ runs/dispositions/attestation/bypass 文件。
2. `build_codex_audit_block(...)`（`:2548`）按 mode 组装并**自校验** codex_audit 块（`build_codex_audit_block` 在 `:536`，对每种 mode 强制必填字段）。
3. bypass mode：`write_bypass_override(...)`（`:2568`）把欠债 sidecar 落盘（Component B）。
4. `_pc.build_evidence(..., codex_audit=block)`（`:2580`）把块嵌进 retro evidence，写 `precheck/<task>.retro.evidence.json`（`:2588`）。
5. 在调 `dump.main([..., --retro-evidence <out>])`（`:2620`）之前先决定路由（`succession_nonce`，`:2598-2600`）；coordinator+active 且有 predecessor nonce → `suppress_spawn_artifacts=True`。
6. dump 内部 retro_gate **再独立跑一次 G0-G9**（`retro_gate.py:1458`）—— evidence 里的块只是 producer 自报，gate 不信自报，对 live HEAD 重算（`code_repo_head` 绑 live HEAD `:1909-1912`、empty_diff 重算 diff `_gate_empty_diff:1331`、fix 验证重读 last run）。
7. **锁序**：precheck.lock → dump.lock → `<task>.audit.lock`（`retro_gate.py:1455-1457`；`audit-close` 与 dump gate 同序，无死锁）。

### 3.2 pre-push 交付审计闸如何把 evidence.json 匹配到被推区间

`audit-check`（`audit_evidence.py:385 main_check` → `check_range:259`）：
- `range_facts(repo, base, head)`（`:110`）算被推区间的 `base_sha / head_sha / patch_id（git patch-id --stable）/ diff_sha256 / changed_files`。
- 扫 `$HANDOFF_HOME/<project>/audits/*.evidence.json`，每条经 `_matches`（`:153`）两条匹配路径：
  - **head_sha 路径**：`reviewed_head_sha == head_sha` **且** `reviewed_base_sha` 存在且 == base（窄审 head^..head 不得清掉宽推 origin/main..head；缺 base→fail-closed 落到 patch-id 路）。
  - **patch_id 路径**：`reviewed_patch_id == patch_id` **且** changed_files 集合一致（容忍 cherry-pick/rebase）；同 base 还**额外要求** `diff_sha256` 字节级一致（patch-id 忽略空白，但 Python 缩进即语义）；跨 base 保留空白容忍。
- verdict 裁决（全扫后按优先级，fail-closed）：任一匹配 RED 且无合法 owner override → FAIL（即便另有 GREEN，同内容冲突即拒）；MIXED/ERROR 同样 FAIL（只 audit_unavailable bypass 能开门，绝不能清 RED）；`red_overovered` → PASS_OVERRIDE（响亮标注，不改写成 GREEN）；纯 GREEN → PASS。

### 3.3 四种审计模式

| mode | 含义 | gate 入口 | 约束 |
|---|---|---|---|
| `full_codex_audit` | 真跑了 codex 审计、findings 经 disposition 闭环 | `_gate_full`（`:1489`） | G2-G9 全身：findings 哈希绑定、fix 重验、refute 跨会话凭证、owner_override 凭 owner_ack 工件 |
| `empty_diff_attestation` | base..HEAD 无 diff，无须审 | `_gate_empty_diff`（`:1331`） | G0 attested head==HEAD + **机器重算** diff 为空 + diff_hash 匹配；非空→打回 full |
| `docs_only_light_audit` | 只改文档（.md/.rst/.txt） | `_gate_full`（mode 分支） | 机器判 diff 全是 doc 后缀（`DOC_SUFFIXES`），CLAUDE.md/AGENTS.md/GEMINI.md/prompts/ 强制 full（`FORCE_FULL_DOC_BASENAMES:849`）；跨 repo 禁用（`:1897`） |
| `codex_unavailable_bypass` | codex 真宕机、无法审 | `_gate_bypass`（`:1290`） | ≥3 条机器可证 codex 失败（exit + 哈希 stderr `MIN_CODEX_FAILURES=3`）+ 强制 follow_up_audit_task_id；**欠债** |

### 3.4 bypass = 欠债 + follow_up_audit_task_id 机理

bypass 通过后并非「免审」而是「**赊账**」：`audit-close` 自动写 `ack/<task>.audit.override.json`（`write_bypass_override:284`），内含 `follow_up_audit_task_id` + `follow_up_deadline`（=created_at + `BYPASS_FOLLOW_UP_DEADLINE_DAYS=1` 天）。两个消费方强制还债：
- **Phase C 逾期扫描器**（`auto-continue.sh:2245` `scan_overdue_kind ... audit.override.json follow_up_audit_task_id`）每次调用都扫，过期未还→写 `<task>.audit_overdue.txt` 逾期 marker。
- **dump 下一棒**：`forced_follow_up_task`（`codex_audit.py:381`）让下一棒的首个 task 必须是 bypass 的 follow_up（不能在未审代码上继续路线图）。

### 3.5 owner_ack_token（绑定校验，非加密诚实层）

`compute_owner_ack_token = sha256(task | finding_hash | nonce | approved_at)`（`:160`）。**明确不是密钥**（`:140-146` trust-model 注释）：以 owner 身份运行的 AI 能写任意自洽文件。它防三件事：(a) 把一个 finding 的批准复用到**另一个** finding（finding_hash 绑定）、(b) 永不过期的批准（7 天 TTL，`OWNER_ACK_TTL_DAYS=7`）、(c) 无痕批准（audit jsonl trail + AskUserQuestion 按钮摩擦）。G7（`:1732-1799`）校验 disposition token == on-disk ack token == 重算值，且 expires_at 必须 == approved_at + TTL（防只改 expires_at 续命）。交付闸侧同构：`validate_owner_ack`（`audit_evidence.py:136`）= `sha256(reviewed_patch_id | reason | ts)`，只能由 tty-gated `audit-override` 产出。

### 3.6 succession relay（sidecar hex16 nonce → 抑制 dump + 进程内 spawn）

`_succession_relay`（`codex_audit.py:2164`）：
1. gated dump 返回 0 后才 `issue_token`（`:2195`）—— live token = 一份新鲜 retro-gated 中枢 close 的机器证据。
2. `_predecessor_spawn_nonce`（`:2138`）读 `queue/<self_task>.singlepane` 的 `spawn_nonce`（hex16）做路由探针；缺/坏→None→走 legacy 全发布路（bootstrap leg）。
3. `spawn.run_spawn(role=supervisor_succession, succession_token=..., predecessor_nonce=...)`（`:2234`）；spawn 侧 `consume_token`（`spawn.py:603`，`expected_task=task`）路径含界+0600+文件名↔payload+project+TTL 校验，单次 unlink（并发竞争恰一个赢）。
4. 成功后写 `queue/<predecessor_task>.done`（`:2278`）让共享身份解析器跳过前任陈旧 sidecar；发 ONE 通知（`:2304`，抑制的 dump 没发，绝不双响）。
5. **绝不**失败回退 legacy 自发布（禁静默降级 `:2184`）；手动逃生口 = `dx-spawn-session.sh --coordinator`。

---

## 4. 数据流 / 状态流（所有读写的 sidecar / 文件）

`$HANDOFF_HOME` = `$HOME/.claude-handoff`（`config.home_dir()`）。

**Codex 审计门禁（链 A）：**
- `$HANDOFF_HOME/<project>/audit/<task>/<run>/codex-findings.json` + `.manifest`（findings 工件 + 哈希 sidecar；`audit_run_dir:118`、`findings_path:122`、`manifest_path:126`）— audit-run 写，gate 读重验。
- `$HANDOFF_HOME/<project>/audit/<task>/dispositions.json`（`dispositions_path:130`）— audit-disposition 追加（持锁）。
- `$HANDOFF_HOME/<project>/ack/<task>.owner_ack.<short>.json`（`owner_ack_path:171`）— owner override 工件，G7 读。
- `$HANDOFF_HOME/<project>/ack/<task>.audit.override.json`（`bypass_override_path:277`）— **bypass 欠债 sidecar**，Phase C 扫描器读。
- `$HANDOFF_HOME/<project>/ack/<task>.audit.retry_audit.jsonl`（`_audit_trail_path:178`）— 审计事件 jsonl（owner-ack-written / bypass-override-written），Phase C 也追加。
- `precheck/<task>.retro.evidence.json`（`:2588`）— 折叠 codex_audit 块的 retro evidence；retro gate 读。
- `queue/<task>.singlepane`（`_predecessor_spawn_nonce:2151`）— 引擎 spawn 时写的 sidecar，带 `spawn_nonce`。

**交付审计闸（链 B）：**
- `$HANDOFF_HOME/<project>/audits/*.evidence.json`（`_iter_evidence:200`）— 外双脑 runner 产出，`audit-check` 读匹配。
- `$HANDOFF_HOME/<project>/audits/bypasses/*.json`（`_try_bypass:215`）— 一次性、按 range、全字段填充的应急 bypass 留痕；消费后写 `used_at`/`used_range`（`:248-250`）。
- `$HANDOFF_HOME/<project>/audits/.audit_pending`（`PENDING_MARKER`，`write_pending:354`）— post-merge FAIL 时写、PASS 时清。

**Succession（链 C）：**
- `$HANDOFF_HOME/<project>/authority/succession-<task>.<nonce>.token`（`succession_authority.py:107`，0600 O_EXCL）— issue 写、consume unlink。
- `$HANDOFF_HOME/<project>/authority/succession-audit.log`（`AUDIT_LOG_NAME:45`）— ISSUED/CONSUMED/REJECTED/SWEPT-EXPIRED forensics（best-effort，非闸）。
- `queue/<predecessor_task>.done`（`:2278`）— 标前任终态。

---

## 5. 现状三态

| 能力 | 态 | 一句话 |
|---|---|---|
| **HANDOFF_AUDIT_MANDATE 门禁真实强制** | ✅ | mandate-ON 时 retro_gate 在 dump 成功前的最后一闸真调 `evaluate_audit_gate`（`retro_gate.py:1446-1481`），不 OK 即 `_handle_audit_failure` 打回 — 改码无通过审计块的 dump 被 RETRY→BLOCKED，**确实在拦**（templates.py 标 mandate flipped 2026-05-30）。 |
| **bypass 欠债 Phase C 逾期扫描器** | ✅ | 扫描器 LIVE 且**有 producer**：`audit-close --audit-mode codex_unavailable_bypass` 调 `write_bypass_override`（`:2568`）真写 `audit.override.json`，`auto-continue.sh:2245` 每次调用扫它 — 非休眠。 |
| **交付审计机器闸（pre-push / audit-check）** | ✅ | pre-push hook（`:49`）硬拒、post-merge（`:26`）warn+pending、`check_range` 按 head_sha/patch-id+diff_sha256 匹配，fail-closed 裁决全实现。 |
| **succession 一次性 token 端到端** | ✅ | issue（`:2195`）→ consume（`spawn.py:603`）→ unlink 单次竞争 → predecessor `.done`，全链接通。 |
| **owner_ack / audit-override 防伪** | 🟡 | tty 闸 + checksum 绑定 + finding/expiry 绑定都在，但**按设计仅诚实层非加密**（owner 身份的 AI 可伪造自洽 token；真防护=owner 持私钥，design §6 deferred）。 |
| **auto-continue.sh 对 codex bypass 的注释** | 🟡 | 注释（`:2113-2116`）仍写「producer deferred until Phase D，无 audit.override.json，codex kind dormant-but-ready」— **与代码相左**（producer 已 wired）：注释陈旧，扫描逻辑本身正确。 |
| **codex-audit-gate spec 草案文档** | ❌ | `project-files/handoff/codex-audit-gate-spec-draft.md` 在 HEAD `5e8d7b2` **不存在**（git 无历史）— 全部源码引为 SOT 的 spec 文档缺失。 |

**关键回答（任务点名）**：
1. **mandate 门禁真在强制吗？** 是。`audit_mandate_enabled` 为真时（env `HANDOFF_AUDIT_MANDATE=1` + 可选 `mandate_projects` 项目白名单，`dump.py:281`/`:295`），retro gate 真调 G0-G9 并据 outcome 拦 dump。非装饰。
2. **bypass 逾期扫描器休眠还是 LIVE？有 producer 吗？** **LIVE，且有 producer**。`write_bypass_override`（`:284`）被 `main_audit_close`（`:2568`）在 bypass mode 真调，落 `audit.override.json`；`auto-continue.sh` 每次调用扫该文件（`:2245`）。注释声称的「dormant-but-ready / 无文件存在」是陈旧描述，不是真实状态。

---

## 6. 🔴 半实现陷阱

**陷阱 1 — auto-continue.sh 的「codex kind dormant」陈旧注释（误导，非真休眠）**
- **现象**：`install/auto-continue.sh:2113-2116` 注释断言 codex-audit override producer 「deferred to before Phase D，until it lands no `*.audit.override.json` files exist, so the codex kind is dormant-but-ready and a strict no-op」。
- **后果**：读注释的人会以为 bypass 欠债不被追讨（虚假的「门禁未生效」印象），而代码里 producer（`codex_audit.py:2568`）早已 wired、扫描器（`auto-continue.sh:2245`）真在跑。一个新中枢若信注释，可能误判 bypass 是免费午餐、不补 follow-up，实则会被逾期 marker + 下一棒 forced-task 拦。
- **正解**：删/订正该注释为「producer LIVE（codex_audit.write_bypass_override），codex kind 与 retro kind 同等活跃」。

**陷阱 2 — owner_ack_token / audit-override checksum 可被同身份 AI 伪造（设计已知、非缺陷但易被误读为加密保证）**
- **现象**：`compute_owner_ack_token`（`:160`）与 `validate_owner_ack`（`audit_evidence.py:136`）都是裸 sha256(明文拼接)，无密钥。以 owner 身份运行的 AI 能算出自洽 token + 写 on-disk ack 工件，绕过 G7 / 绕过 RED-override 摩擦。
- **后果**：若把「checksum 校验通过」当成「owner 真的批准过」的密码学证明，则信任根被高估。真实防护只是「防复用 + 防永久 + 留痕 + tty 摩擦」，挡 drift 不挡蓄意伪造者。
- **正解**：保持现状但措辞零信任（代码注释 `:140-146` 已诚实标注）；真要密码学保证需 owner 持私钥签名（design §6 deferred）。架构总览须把这条显式 surface，不能让「✅ 有 owner override 闸」掩盖「非加密」。

**陷阱 3 — codex-audit-gate spec 草案文档不存在（全模块引为 SOT 的文档缺失）**
- **现象**：`codex_audit.py:21`、`audit_evidence.py` 引导注释、`templates.py` 多处都把 `project-files/handoff/codex-audit-gate-spec-draft.md` 当 source-of-truth 引用，但该文件在 HEAD 不存在（`git log` 零历史）。
- **后果**：spec §1.3 / §2.2 / §5 / §6 / §7.3 等被代码注释反复引用的条款无法核对；doc-vs-code 漂移无法仲裁；新会话想读 spec 会扑空。
- **正解**：要么补回 spec 草案（让注释引用兑现），要么把注释里的 `spec §X` 引用改为指向真实存在的 design/legislation 文档（或内联进 docstring）。

**陷阱 4 — `mandate_projects` 白名单 fail-closed 但「未列项目走 legacy 无闸路」需警觉**
- **现象**：`dump.py:295-300` 当 `mandate_projects_configured` 为真且 `project not in mandate_projects` 时，对无 evidence dump 返回 None（走 legacy 路、不上闸）。config.py 注释（examples/config.json:45）声称退化形状（空/typo/全非法）一律 fail-closed 全局强制。
- **后果**：闸是否对某项目生效，取决于一个共享 `config.json` 的列表。若该列表被误填成非空但漏掉某活跃项目，该项目的 mandate 会**静默失效**（且不是退化形状、不触发 fail-closed）。`HANDOFF_RETRO_BYPASS` / 显式 `--retro-evidence` 总能绕过白名单上闸（`:289-294` 注释），属设计内但需知晓。
- **正解**：非代码 bug，但架构上「闸生效范围 = 一个外部可编辑列表」是承重事实，须在总览显式登记；建议运维对 `mandate_projects` 漏项做周期核对。

---

## 7. 承重事实 file:line 清单

1. mandate-ON 真调 gate：`retro_gate.py:1458` `audit_outcome = codex_audit.evaluate_audit_gate(payload, workspace, project, task)`，包在 `if audit_mandate_enabled:`（`:1446`）。
2. gate 失败即拦 dump：`retro_gate.py:1472-1481` `if not audit_outcome.ok: return _handle_audit_failure(...)`。
3. bypass producer 真被调：`codex_audit.py:2566-2568` `if args.audit_mode == _pc.AUDIT_MODE_BYPASS and bypass is not None: write_bypass_override(...)`。
4. bypass 欠债 sidecar 路径：`codex_audit.py:281` `ack/<task>.audit.override.json`。
5. Phase C 扫描器扫该 sidecar：`auto-continue.sh:2245` `scan_overdue_kind "$proj_dir" "audit.override.json" "follow_up_audit_task_id" "audit_overdue.txt" ... "codex-audit" "1"`。
6. bypass ≥3 失败门槛（producer 与 gate 统一）：`codex_audit.py:831` `MIN_CODEX_FAILURES = 3`，`:836` `BYPASS_MIN_FAILURES = MIN_CODEX_FAILURES`。
7. bypass 强制 follow-up + 1 天截止：`codex_audit.py:824` `BYPASS_FOLLOW_UP_DEADLINE_DAYS = 1`；`:328` `deadline = _add_days_iso(created_at, ...)`。
8. owner_ack_token = 非加密绑定 checksum：`codex_audit.py:167` `canonical = f"{task}\n{finding_hash}\n{nonce}\n{approved_at}"`，`:140-146` 明示 NOT cryptography。
9. G7 owner override 三方一致校验：`codex_audit.py:1773` `if not (recomputed == ack.get("owner_ack_token") == token):`。
10. owner_ack expiry 防续命：`codex_audit.py:1799` `if exp_dt != approved_dt + timedelta(days=OWNER_ACK_TTL_DAYS):`。
11. 交付闸 head_sha 匹配须绑 base：`audit_evidence.py:162` `if evidence.get("reviewed_base_sha") == facts.base_sha: return "head_sha"`（缺 base 不直通）。
12. 交付闸同 base 须 diff_sha256 字节绑定：`audit_evidence.py:182-186`（patch-id 忽略空白，Python 缩进即语义）。
13. 交付闸 RED fail-closed、唯一门 owner override：`audit_evidence.py:297-303` `if red_plain: return CheckResult(False, "FAIL", ...)`。
14. owner override 仅 tty 可产：`audit_evidence.py:441-446` `if not (sys.stdin.isatty() and sys.stdout.isatty()): ... return 1`。
15. succession token 一次性 unlink 即门：`succession_authority.py:210-217` `resolved.unlink()` + 失败 fail-closed。
16. consume 须绑 successor task：`succession_authority.py:190-196` `if payload.get("task") != expected_task:` 拒（不 unlink）。
17. token TTL 120s：`succession_authority.py:36` `TOKEN_TTL_SECONDS = 120`。
18. succession 仅 gated dump 返回 0 后签 token：`codex_audit.py:2625` `if rc == 0 and args.coordinator and args.status == "active":` → `_succession_relay`（`:2634`）。
19. succession 绝不静默降级回 legacy：`codex_audit.py:2245-2265`（spawn 失败按 token 是否 burned 给两种 ERR-FATAL，均不回退）。
20. spawn nonce 用 secrets 不可猜：`spawn_nonce.py:14` `return secrets.token_hex(8)`。
21. mandate 项目白名单退化 fail-closed（全局强制）：`install/examples/config.json:45`（注释 SOT）+ `dump.py:295-300` 路由。

---

## 8. 与 spec 出入

- **spec 文档本体缺失**：`project-files/handoff/codex-audit-gate-spec-draft.md` 在 HEAD `5e8d7b2` 不存在（`git log` 无历史）。所有引用它的注释（`codex_audit.py:21`「Spec source of truth ... v0.2」、`audit_evidence.py` 导言、`templates.py:181/338`）都是**悬空引用**，无法核对。这是最大的一处 doc-vs-code 漂移：代码自称遵循一份不在仓里的 spec。
- **auto-continue.sh 的 Phase 阶段描述滞后于代码**：注释（`:2113-2116`）写 codex bypass producer「deferred to before Phase D，until it lands no `*.audit.override.json` files exist」，但 producer（`codex_audit.py:2568`）+ mandate（templates.py 标 flipped 2026-05-30 Phase D ON）均已落地。注释停在 Phase A/B「mandate OFF」叙事（`codex_audit.py:7-9` docstring 亦写「With the audit mandate OFF (the only state Phase A ships in)」），与 templates.py「Phase D / mandate ON」相矛盾 —— **同仓内两处对 Phase 状态自相矛盾**。
- **行为本身无出入**：能定位到的实际 gate 逻辑（G0-G9、四模式、bypass 欠债、succession token、交付闸匹配/裁决）彼此自洽且按注释承诺执行；漂移集中在「文档/注释滞后」而非「代码偏离设计」。
