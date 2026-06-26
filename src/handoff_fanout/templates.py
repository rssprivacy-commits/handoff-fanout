"""Markdown templates rendered into handoff queue files.

Three top-level builders, one per file kind:
  - ``build_handoff_md`` — the standard single-task baton handed to the next session.
  - ``build_sub_task_handoff_md`` — given to each fan-out worker tab (role=sub-task).
  - ``build_fan_in_handoff_md`` — given to the tab that consolidates a finished batch.

Project-specific blocks (e.g. accounting redlines, in-house legislation) are
not hardcoded; instead, the caller supplies them via ``inject_blocks`` from
``handoff_fanout.config.Config``.

Templates are plain f-strings (no Jinja dependency) so the wheel stays
zero-dep.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


def _join_inject_blocks(blocks: Iterable[str]) -> str:
    return "\n\n".join(b.rstrip() for b in blocks if b and b.strip())


def _format_baseline_block(baseline: dict) -> str:
    """Render the baseline header — only fields actually present are shown."""
    lines = [
        f"**HEAD**: `{baseline.get('git_head', '(unknown)')}` ({baseline.get('branch', 'main')})",
    ]
    for k, v in baseline.items():
        if k in {"git_head", "branch", "last_3_commits"}:
            continue
        if v in (None, "", "(N/A)"):
            continue
        lines.append(f"**{k}**: `{v}`")
    return "\n".join(lines)


def _worktree_banner(worktree_info: dict | None, project: str, workspace: Path) -> str:
    """Markdown banner shown to the successor when worktree isolation is in play.

    For a CREATED worktree it states the isolation + the merge-back closure protocol
    (commit to the task branch, push it, then ff-publish to the integration branch);
    for DEGRADE it warns the session it is on the shared tree (local main may lag
    origin). Empty string when isolation is off / no worktree info.
    """
    if not worktree_info:
        return ""
    status = worktree_info.get("status")
    if status == "created":
        branch = worktree_info.get("branch")
        intb = worktree_info.get("integration_branch")
        warns = worktree_info.get("warnings") or []
        warn_block = ""
        if warns:
            warn_block = "\n> ⚠️ **前任 dump 警告**:\n" + "".join(f">   - {w}\n" for w in warns)
        return f"""
## 🌿 隔离 worktree (per-session git worktree isolation)
{warn_block}

> 本会话在**独立 git worktree** 工作：你的 `reset --hard` / 工作树改动 / pytest **只动这棵树**，
> 不会卷走其它会话或主树的 WIP（事故根治点）。⚠️ **但 `refs/stash` 是 repo 全局的** —— `git
> stash list` / `pop` 跨 worktree 共享，别用 `git stash` 当隔离手段（优先 commit，不要 stash/pop）。
> **目录 basename = task-id ≠ project** —— 所有 `handoff` 命令必须显式带 `--project {project}`（见 §-1，已注入）。

- **worktree**: `{workspace}`
- **branch**: `{branch}`（从 `origin/{intb}` 切出）
- **integration branch**: `{intb}`

### 闭环合并协议 (merge-back / 不可省)
1. 在本 worktree 正常 `commit` 到分支 `{branch}` + `git push origin {branch}`（保留分支）。
2. **闭环 dump 前**把工作 ff-publish 到集成分支：`git push origin HEAD:{intb}`。
3. 然后才跑 §-1 的 `handoff precheck` + `handoff dump`（引擎会校验 `origin/{intb}` 已含本会话
   HEAD；未 publish → dump 被 BLOCK，提示先 push）。
4. 终态后本 worktree 由 `handoff prune` / `handoff worktree gc` 安全回收（脏/未合并则保留现场）。
"""
    if status == "degraded":
        reason = worktree_info.get("reason") or "unknown"
        return f"""
## ⚠️ worktree 隔离已降级 (degraded → 共享主树)

> 本会话请求了 worktree 隔离但**降级回共享主树**（原因：{reason}）。你在主树工作，
> 若本项目曾用 worktree relay，本地 `{worktree_info.get("integration_branch") or "main"}`
> 可能滞后 origin —— 必要时先 `git pull --ff-only`。注意与其它会话的共享树并发风险。
"""
    if status == "report":
        return f"""
## 🌿 worktree 隔离 report-only（未创建 / 仅演示）

> report-only 模式：本会话仍在共享主树。计划命令：`{worktree_info.get("planned_cmd") or ""}`。
"""
    return ""


def build_handoff_md(
    *,
    task: str,
    project: str,
    workspace: Path,
    next_brief: str,
    status: str,
    tests: str | None,
    baseline: dict,
    roadmap_excerpt: str,
    inject_blocks: Iterable[str],
    handoff_home: Path,
    handoff_md_path: Path,
    worktree_info: dict | None = None,
    self_task_args: str = "",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    baseline_block = _format_baseline_block(baseline)
    wt_banner = _worktree_banner(worktree_info, project, workspace)
    # R1-X1: under worktree isolation the cwd basename is the task-id, NOT the
    # project, so every generated handoff command MUST carry an explicit
    # ``--project``/``--workspace`` or it writes evidence/queue/ack under a project
    # named after the task. Empty for the non-worktree path (byte-identical legacy).
    wt_args = (
        f" --project {project} --workspace {workspace}"
        if worktree_info and worktree_info.get("status") == "created"
        else ""
    )

    test_section = ""
    if tests:
        test_section = f"""
### 测试基线
```bash
cd {workspace}
pytest {tests} 2>&1 | tail -10
# 期望全 PASS / fail 即停 + dump --status blocked
```
"""

    inject_section = _join_inject_blocks(inject_blocks)
    if inject_section:
        inject_section = "\n" + inject_section + "\n"

    return f"""\
# Handoff — project `{project}` / task `{task}`

**生成**: {now}
**Project**: `{project}` ({workspace})
{baseline_block}
**Status**: `{status}`
{wt_banner}
## §0 上任审计 — 核对前任 retro evidence (v5.4 / 不可跳过)

> **触发**: 本会话被 launchd 由前任 dump 触发开张。第一步**不是**写代码，是审计前任是否真的复盘了。
> Source of truth: `docs/PROTOCOL.md` Part II §13 (v5.4 retro-evidence gate)。

```bash
TASK="{task}"; PROJ="{project}"
EVID="$HOME/.claude-handoff/$PROJ/precheck/$TASK.retro.evidence.json"
OLDREADY="$HOME/.claude-handoff/$PROJ/ack/$TASK.old_ready"
if [ -f "$EVID" ]; then
    jq '{{head, mode, phase0, phase1, next_brief, evidence_hash}}' "$EVID" 2>/dev/null || cat "$EVID"
    [ -f "$OLDREADY" ] && jq '{{session_id_kind, retro_evidence_hash, commit_hash, codex_audit_hash, codex_audit_mode, next_session_forced_task, code_repo, code_repo_head}}' "$OLDREADY"
    echo "✅ 前任 retro evidence 在位 — 可推进 task"
else
    echo "⛔ 前任无 retro.evidence.json — HANDOFF_RETRO_MANDATE=1 已拨 (Phase 4c, 2026-05-29)。"
    echo "   不要代签、不要自己补跑 precheck 假装前任复盘过 (spec §2.3 Q9)。"
    echo "   立即写 queue/$TASK.BLOCKED.md 注明 '前任缺 retro evidence' 并通知主人裁决；"
    echo "   仅在主人显式授权后用 'handoff precheck --mode forensic_retro' 补登记。"
fi

# Phase C — codex 审计 bypass 强制 follow-up (spec §1.3)。前任若 codex 审计走
# bypass (codex 不可用), 它欠一次审计 → 下一棒 (=本会话) 的首任务**必须**是前任
# 记录的 follow-up task, 不得继续 roadmap (否则 = 在未审代码上接着干)。
FORCED=$([ -f "$OLDREADY" ] && jq -r '.next_session_forced_task // empty' "$OLDREADY" 2>/dev/null)
if [ -n "$FORCED" ] && [ "$FORCED" != "$TASK" ]; then
    echo "⛔ 前任 codex 审计 bypass — 强制 follow-up = '$FORCED', 但本会话 task = '$TASK'。"
    echo "   bypass = 欠一次审计, 下一棒先还债 (spec §1.3)。不要继续 roadmap。"
    echo "   立即写 queue/$TASK.BLOCKED.md 注明 '跳过 codex audit follow-up' 并通知主人裁决。"
fi
```

**新会话不代签** (v5.4 spec §2.3 Q9): 缺 evidence 时**不要**自己跑 `handoff precheck` 假装补做 — Phase 0/1 是老会话对自身工作的闭环声明，新会话无法证明。若主人明确授权 forensic retro，用 `handoff precheck --mode forensic_retro` 显式标记。

**Phase D — codex 审计门禁状态** (mandate ON / flipped 2026-05-30): old_ready 的 `codex_audit_hash` / `codex_audit_mode` / `next_session_forced_task` 为审计门禁元数据 (spec §6 Phase D)。`HANDOFF_AUDIT_MANDATE=1` **已拨** (三路径: `.zshenv` + `launchctl setenv` + `auto-continue.plist EnvironmentVariables`) → 上面的 forced-follow-up 检查现为**工具层硬拒** (非会话自律): 前任 codex 审计走 bypass 时下一棒接了别的 task = §0 拦下。详 `docs/PROTOCOL.md` Part II §14 (codex 审计闸 / bypass / forced follow-up)。

## §0.5 retrieval-pull — 调出前任 lesson + 强制回引 (enforce 闸 / 学习闭环 keystone)

> **触发**: 中枢接棒开张 — §0 验完前任**真复盘了**之后，本步把"前任的坑"真正**调出来用上**。
> **为什么**: 闸只卡"存了没"、从不卡"下一棒读没读、用没用"(Goodhart)。让沉淀对**接棒人承重** = 经验真累积、不反复踩坑。这是学习闭环的 keystone。

1. **调出前任 lesson** (按本任务域 trigger-keyword 匹配)：grep 中枢 memory 目录的 `lesson-*<本链前缀>*` / 前任 task-id，读其 `## 当前有效摘要` 段。
```bash
LESSON_DIR="$HOME/.claude/projects/-Users-chenmingzhong-Projects-{project}/memory"
ls "$LESSON_DIR"/lesson-*.md 2>/dev/null | tail -3   # 或按链前缀 grep
```
2. **为每条调出的 lesson 产一条结构化回引**：`前任课 X → 已应用 (applied) / 已被取代 (superseded, 附新课名) / 不相关 (not_relevant, 因 Y)`。
3. **在你自己首次交棒时把回引一并提交** (折进同一份 retro evidence)：
```bash
handoff audit-close ... \
  --predecessor-lesson-backref <lesson>=applied \
  --predecessor-lesson-backref <lesson-old>=superseded:<lesson-new> \
  --predecessor-lesson-backref <lesson-x>=not_relevant:<原因>
# 或一次性给 JSON 数组: --predecessor-lesson-backref-file <backref.json>
```
4. **真无新课时的诚实出口** (component 5)：本棒确属例行、确无可沉淀的新课 → 用 `--lesson-disposition no_novel_lesson_attested:<理由>` 显式声明（**禁**拿它当偷懒旁路——它要求一句诚实理由）。
5. **这是 enforce 硬闸 (B1 / 项目在 `retrieval_pull_enforce_projects` 内时生效)**：中枢 active 交棒**缺回引 (`--predecessor-lesson-backref`) 且缺 `no_novel_lesson_attested` 声明 → dump 被拒 (`ERR-RETRY`)**、不产任何 artifact——读了前任的课、记下回引（或诚实声明无新课）再 re-dump。**为什么硬闸**：闸只卡"存了没"、从不卡"下一棒读没读用没用"(Goodhart)，让沉淀对**接棒人承重**=经验真累积、不反复踩坑，这是学习闭环 keystone；它是唯一难作弊的学习信号(独立消费者给前任沉淀打分)、别把它当 checkbox。default-OFF；一键回滚 `touch $HANDOFF_HOME/{project}/.retrieval-pull-enforce-off`。

## §0.6 closeout obligations — 交棒前记录复盘义务向量 (warn-mode / 第三 status-vector)

> **触发**: 中枢接棒后，一路干到**自己交棒**那一刻，记录这一棒的"复盘义务覆盖" —— 把软文字规则⑬「交棒前先复盘」机器化成一个 scope-by-delivery 的向量。
> **为什么**: 软规则只靠自律、抓不到"做了 80% 报成 100%"。这个向量把"复盘义务"拆成 6 条按交付范围裁剪的 key，每条要么 ✅（有 artifact 背书）要么 `skip:<理由>`（本棒 N/A，必带为何不适用）——让"有没有真复盘"可机器读。它是软规则⑬的可校验影子（"复盘义务的机器化版"）。

1. **6 个 key + scope-by-delivery 含义**（按本棒**实际交付**裁剪，不适用就 `skip:<理由>`）：
   - `sedimentation_always` — **每棒必做**（lesson + retro evidence），应 ✅。
   - `audit` — 仅**有代码改动**时适用。
   - `doc_mapping` — 仅 **instructions / architecture / config** 改动时适用。
   - `release` — 仅有**用户可见交付**时适用。
   - `sync_pipeline` — 仅 **artifacts** 改动时适用。
   - `postmortem` — 仅**本棒有事故 / 回归**时适用。
2. **怎么用**（在你自己交棒的 `audit-close` 上加 flag，折进同一份 retro evidence）：
```bash
handoff audit-close ... \\
  --closeout-status sedimentation_always=✅ \\
  --closeout-status audit=✅ \\
  --closeout-status release=skip:no user-visible change this hop
```
3. **warn-mode 非阻断**：缺向量**永不 block** —— 仅当项目在 `closeout_obligations_warn_projects` 内（DEFAULT-OFF）才出一条 advisory 提醒补 `--closeout-status`，handoff 照常推进。一键静默：`touch $HANDOFF_HOME/{project}/.closeout-obligations-warn-off`。

## §0.7 parked-backlog scan — 开张扫 owner backlog, 防衰减失忆 (anti-decay / 软规则机器化)

> **触发**: 中枢接棒开张 — §0.5/§0.6 之后, 本步强制扫一遍 owner 的 parked backlog, 防 owner-req 衰减出当前摘要、靠 owner 记起才浮回。
> **为什么**: "每棒开张必扫 backlog" 是软规则、靠自律, 实证仍漏 (中枢声称"无活了", owner 敲打"查你账本不是还有一堆")。把"扫 backlog"前移成开张硬动作。

1. **扫 open-loops backlog 块**: 读中枢 memory `open-loops.md` 的「当前有效摘要」+「排期 BACKLOG」段, 逐项核当前状态 (别凭记忆)。
```bash
OL="$HOME/.claude/projects/-Users-chenmingzhong-Projects-{project}/memory/open-loops.md"
grep -nE "BACKLOG|DONE|dx territory|owner-gated|parked|待 owner|RESURFACED" "$OL" | head -25
```
2. **逐项判定**: 每个 parked / owner-gated 项 → 仍有效 (still valid·本棒可推) / 已闭环 (done) / 已交别链 (dx-handed)。**禁**让 owner-gated 项静默滑出摘要。
3. **诚实出口**: 本棒确无自主切片时 → **禁报 "nothing left / 没活了"**; 须显式列 owner-gated 清单交 owner (它们是"此刻无自主切片"非"不存在")。让 owner 一眼看全 backlog、不靠记忆浮回。

## §0.8 window placement — 开张自摆 + 派 worker 自动入位 (Rectangle within-desktop / 软规则机器化)

> **触发**: 中枢接棒开张 (self-place) + 每派出一个 worker 之后 (place worker) — 用已装的 **Rectangle** 把窗口在**本桌面内**摆位, owner 一眼看全队形。
> **为什么**: owner p71 立法「中枢窗 → 右半 / worker 窗 → 左上·左下交替」。手动摆窗靠自律必漂; 把"摆位"前移成开张 + 派后的硬动作。桌面*分配*仍归 dharmaxis vscode-spaces (RED LINE #4 — 本工具只读消费 winlist/goto); 这里只在桌面内平铺、用 Rectangle URL scheme (非自写 set position/size)、可逆 (Rectangle "Restore")。

1. **开张自摆右半** (onboarding 之后, 一次; `--self` = 当前 frontmost 窗, 无 goto/无 restore):
```bash
~/.claude-handoff/supervisor-monitor/coord-place-window.py --project {project} --self --slot right-half --execute
```
2. **每派一个 worker 后自动入位** (worker 窗异步出现 → `--wait` 轮询; 计数 n 从 0 起、本棒每派一个 +1, 偶数→top-left 奇数→bottom-left):
```bash
~/.claude-handoff/supervisor-monitor/coord-place-window.py --project {project} --task <worker-task> --role worker --worker-index <n> --wait 45 --execute
```
3. **说明**: DRY-RUN 默认 (去掉 `--execute` 只打印 plan、不动窗); 身份 fail-closed (stable WID → 唯一 title, 非本链窗 HARD REFUSE); Rectangle 没起会显式报错不静默 no-op; 成功与否按 bounds delta 诚实判定。

## 第一步: 启动 heartbeat (v5.1+ / 529 风暴防御 / v4.1 单 task 模式)

> **触发**: 主人 5/29 'API Error 会话裸跑' 根因 — v4.1 单 task spawn 后若卡死 / 529 overloaded 没人接手。
> 本步骤让新会话每 60s touch heartbeat 文件，watchdog mode 6 在 >5min 失活时写 `.529-suspected` + 通知主人。
> 与 sub-task 模式 (build_sub_task_handoff_md 第二步) 对称。

```bash
( while true; do
    touch {handoff_home}/{project}/queue/{task}.heartbeat
    sleep 60
  done ) &
echo $! > /tmp/heartbeat-{task}.pid
# 闭环前 kill: kill $(cat /tmp/heartbeat-{task}.pid) 2>/dev/null
```

## §第一步.5: 长跑 CLI 调用必须 wrap `timeout` (529 风暴防御延伸 / v5.4)

> **触发**: 5/29 04:05 实战 — codex audit CLI 调用 stuck 19 min，整个会话冻结、
> heartbeat 随之失活，watchdog mode 6 只能事后补救 (被动 kill 卡死进程)。
> 根因: codex / `claude -p` / 任意外部 CLI 无超时上限时，单次 hang 拖垮整会话。

**铁律**: 本会话调用任何可能长跑的外部 CLI (codex / `claude -p` / `gh` / 大文件 `curl`
/ 全量 `pytest` 等) **必须** wrap `timeout` (默认 300s，按预估调整，上限 600s):

```bash
timeout 300 codex exec "..." || echo "⚠️ codex 超时/失败 (exit $?) — 不阻塞，记录后继续"
```

- ✅ 超时即退出，不让单次 hang 冻结会话 / 拖垮 heartbeat (主动预防)
- ✅ 与 watchdog mode 6 (被动 kill) 互补，不替代
- ⚠️ 自递归防御: codex audit 期间 mode 6 可能误杀本会话进程，必要时先
  `touch {handoff_home}/{project}/STOP_AUTO`，audit 完 `rm`

## 第二步: Baseline 验证 (新会话开局必跑)

```bash
cd {workspace}
git log --oneline -1                          # 应 = {baseline.get("git_head", "(unknown)")}
git status -sb                                 # 工作区干净
```

近 3 commits:
```
{baseline.get("last_3_commits", "(unavailable)")}
```
{test_section}

## 当前进度 (roadmap 摘要)

{roadmap_excerpt}

## 下一步任务 (task: `{task}`)

{next_brief}

## 必读 (按顺序 / 项目相关)

1. 本文件 (`{handoff_md_path}`)
2. 项目 CLAUDE.md / AGENTS.md (`{workspace}/CLAUDE.md`)
3. 当前 task 涉及的源文件
{inject_section}
## STOP 控制 (主人秒级介入)

```bash
touch {handoff_home}/STOP_AUTO                       # 全局暂停所有项目
touch {handoff_home}/{project}/STOP_AUTO              # 仅暂停本项目
touch {handoff_home}/done                            # 全局永久停所有
touch {handoff_home}/{project}/queue/{task}.done      # 仅停本 task
```

中文快捷指令 (任何会话有效):
- `暂停` / `pause` → touch {handoff_home}/STOP_AUTO
- `放行` / `resume` → rm STOP_AUTO
- `永久停` / `done` → touch {handoff_home}/done
- `状态` / `status` → ls {handoff_home}/*/queue/

## AI 自检 (闭环前)

4 触发场景任一命中 → 必沉淀全局 skill:
1. 写 > 30 行复用脚本
2. 跨项目共享库
3. 文档提取 / 邮箱 / OCR / 银行
4. 绕守护 / 解锁

## §-1 老会话自检 + 启动指令 (dump 前必跑 / v5.4)

按主人 5/27 立法自主推进 task `{task}` (project `{project}`). 本 task 闭环后, **dump 下一个 task 之前**必须先跑 retro precheck — Phase 闭环 SOP 工具层 invariant (Phase 4a `handoff precheck` 工具 ✅, Phase 4b prompt 注入 ✅, Phase 4c mandate flipped 2026-05-29 ✅)。

```bash
cd {workspace}
# 1) 生成 retro evidence (Phase 0 五项 + Phase 1 五类显式声明 / status enum 见 §7.13)
handoff precheck \\
    --task <next-task-id>{wt_args}{self_task_args} \\
    --phase0-status memory=✅ --phase0-status tests=✅ \\
    --phase0-status audit=✅ --phase0-status commit=✅ \\
    --phase0-status code_review=✅ \\
    --phase1-status codex=✅ --phase1-status claude_md=✅ \\
    --phase1-status l2_memory=✅ --phase1-status tests=✅ \\
    --phase1-status prs=✅
# → 写 ~/.claude-handoff/{project}/precheck/<next-task-id>.retro.evidence.json

# 2) dump 时传 --retro-evidence; HANDOFF_RETRO_MANDATE=1 强制激活 gate
handoff dump \\
    --task <next-task-id>{wt_args}{self_task_args} \\
    --next "<next-task-brief>" \\
    --status active \\
    --tests "<test-files>" \\
    --retro-evidence ~/.claude-handoff/{project}/precheck/<next-task-id>.retro.evidence.json
# project + workspace 自动从 cwd 推断（worktree 隔离下 cwd basename = task-id ≠ project，
# 故上面已显式注入 --project/--workspace）
```

**status enum** (Phase 4a 实施 / `handoff_fanout.handoff_precheck`):
- `✅` — 本 task 实际改动 / `⚠️` — warning 不阻塞 / `❌` — 漏做 (gate 拒) / `skip` — 显式跳过 (须 reason)
- **phase0 keys**: `memory / tests / audit / commit / code_review`
- **phase1 keys**: `codex / claude_md / l2_memory / tests / prs`
- spec §7.13 旧 enum (`updated/...`) Phase 4c 取 runtime 协调闭环 — runtime 为权威

**紧急 P0 bypass** (§7.1 / §7.9): `HANDOFF_RETRO_BYPASS=1` 启用 — 须有 `ack/<task>.retro.override.json` 含 `follow_up_retro_task_id` + ISO-8601 `follow_up_deadline`，否则 exit 6 `ERR-BYPASS`。

**exit code 速查** (§7.1 — AI 按 subcode 决定 retry / stop / BLOCKED):

| exit / 前缀 | 含义 | AI 应对 |
|---|---|---|
| 0 / `OK:` | gate 通过 | 等 launchd spawn 下个 tab |
| 2 / `ERR-BLOCKED:` | attempt_n=2 硬拒 / head-stale-fatal | 停止 retry + 走 BLOCKED 流程 |
| 3 / `ERR-LOCKED:` | precheck/dump/attempt 锁竞争 | 让位退出 (并行 tab 在 dump) |
| 4 / `ERR-RETRY:` | evidence 缺 / hash mismatch / schema 不通过 | 修后 re-dump 一次 (attempt_n < 2) |
| 6 / `ERR-BYPASS:` | bypass 字段缺 / follow-up overdue | 补 trail 字段后 re-dump |

**当前阶段** (Phase 4c flipped 2026-05-29 / 已拨): `HANDOFF_RETRO_MANDATE=1` 在 `~/.zshenv` + `launchctl setenv` + `auto-continue.plist EnvironmentVariables` 三路径全量生效。不带 `--retro-evidence` 的 active dump → **mandate_projects 列内项目** exit 4 `ERR-RETRY` 拒绝；**未列入项目 (如 handoff-fanout)** 走 legacy exit 0 (dump-时强制改由 pre-push 闸 + 显式 `--retro-evidence` 承担，详 docs/PROTOCOL.md §13.3)。紧急 P0 走 `HANDOFF_RETRO_BYPASS=1` + `ack/<task>.retro.override.json` (含 `follow_up_retro_task_id` + ISO-8601 `follow_up_deadline`)。

### §-1.5 codex 审计门禁 — audit-close 流程 (Phase D / mandate ON / spec §6)

本 task 若**改了代码** (非纯文档), 闭环时应跑 codex 审计并用 `handoff audit-close` 替代裸 `handoff dump` —— 它在**单进程持锁内**把 codex 审计块 (机器可校验) 折进 retro evidence 再 dump, HEAD 不会在审计与 dump 之间漂移 (spec §1.6 / R2-P0-6)。

```bash
cd {workspace}
# 1) 跑 codex 审计 → 登记机器产物 (按 spec §3 / Phase B 已能记录)
handoff audit-run --task <next-task-id>{wt_args}{self_task_args} --run-index 1 ...   # 写 codex-findings.json + sidecar manifest
handoff audit-disposition --task <next-task-id>{wt_args}{self_task_args} ...        # 每个 P0/P1 一条 disposition

# 2) audit-close: full 模式 (有代码改动) — 把审计块折进 evidence + dump 一气呵成
handoff audit-close \\
    --task <next-task-id>{wt_args}{self_task_args} --next "<brief>" --status active \\
    --audit-mode full_codex_audit --run-record '<run-record-json>' \\
    --phase0-status memory=✅ ... --phase1-status codex=✅ ... \\
    --closeout-status sedimentation_always=✅ --closeout-status audit=✅
```

**4 模式** (gate 在 Phase D 机器裁定 / Phase C 仅记录): `full_codex_audit` (有代码改动) / `empty_diff_attestation` (diff 为空) / `docs_only_light_audit` (纯文档, prompts/CLAUDE.md/schema/SQL 不算) / `codex_unavailable_bypass` (codex 不可用)。

**bypass = 欠债** (spec §1.3): codex 真不可用时走 `--audit-mode codex_unavailable_bypass --bypass-file <f>` (含 `codex_failure_attempts` 机器失败证明 ≥3 次 + `follow_up_audit_task_id`)。**下一棒 `--task` 必须 = `follow_up_audit_task_id`** —— dump 会把它写进 `old_ready.next_session_forced_task`, 新会话 §0 校验; 接了别的 task = §0 拦下 (mandate on 后硬拒)。audit-close bypass 模式**自动写** `ack/<task>.audit.override.json` (含 `follow_up_deadline` = now+1天), 无需主人点击 (codex 宕机是机器事实 / 主人裁决 #2); Phase C overdue scanner 到期未见重审 evidence → 下次 dump exit 6 拦。

**owner_override (豁免 finding / 需主人单按钮)**: AI 遇到一个它主张"豁免不修"的 P0/P1 → 调 AskUserQuestion 单按钮「确认豁免」(与 5/28 单按钮二段确认立法一致) → 主人点击后 AI 调 `write_owner_ack(...)` 写 `ack/<task>.owner_ack.<finding_hash_short>.json` (绑定 finding_hash + nonce + `owner_ack_token` 校验和 + 7天 `expires_at`) + 在 disposition 写 `disposition=owner_override` 带同一 `owner_ack_token`。gate G7 读 on-disk ack 校验: 存在 + schema 支持 + ack.finding_hash==该 finding + 重算 `sha256(task|finding_hash|nonce|approved_at)` 三方一致 + 未过期, 任一不满足 = BLOCKED。**诚实声明**: `owner_ack_token` = **防篡改 + 摩擦**绑定校验和, **非加密**; 防的是一次批准被静默复用到别的 finding / 无限期有效 / 无痕批准, **不防**恶意 AI 以主人身份伪造批准 (单用户场景接受 / 多用户上线再引入主人持密钥的 HMAC / 设计 §1 + §6)。

**当前阶段** (Phase D / mandate ON / flipped 2026-05-30): `HANDOFF_AUDIT_MANDATE=1` **已拨** (三路径全量生效)。dump gate 对 **mandate_projects 列内项目**（或任何用显式 `--retro-evidence`/`audit-close` 的 dump）强制 G0-G9 — 改了代码却无通过的 codex 审计块 → dump 被 RETRY→BLOCKED ("缺陷不下传")；**未列入项目 (如 handoff-fanout)** 的裸 no-evidence dump 走 legacy (`dump.py:293`)，G0-G9 强制改由 pre-push 闸 + 显式 evidence 承担 (详 §13.3)。本 task 若改代码用 `handoff audit-close` 替代裸 `handoff dump`。详 `docs/PROTOCOL.md` Part II §14 (codex 审计闸 / G0-G9 / owner_ack_token)。✅ Phase D 前置两项已补: `owner_ack_token` 绑定校验 (非加密 / G7) + bypass sidecar producer。

成功 → launchd / cron WatchPaths 1 秒内 spawn 新 Claude tab.

## 并发会话提示

Queue 模式: 本会话只负责自己的 task `{task}`. 同项目内其他 task 由各自会话推进.
跨项目: 主人可能并行推其他项目, 本会话仅在 `{project}` 内活动.
"""


def build_sub_task_handoff_md(
    *,
    task: str,
    project: str,
    workspace: Path,
    next_brief: str,
    batch_id: str,
    sub_task_id: str,
    file_ownership: list[dict],
    baseline: dict,
    roadmap_excerpt: str,
    inject_blocks: Iterable[str],
    handoff_home: Path,
    git_guard_path: Path,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ownership_md = "\n".join(f"- type=`{o['type']}` path=`{o['path']}`" for o in file_ownership)
    role_env_path = handoff_home / project / "batches" / batch_id / f"{sub_task_id}.env"
    batch_dir_disp = handoff_home / project / "batches" / batch_id
    inject_section = _join_inject_blocks(inject_blocks)
    if inject_section:
        inject_section = "\n" + inject_section + "\n"

    return f"""\
# Handoff v5 SUB-TASK — `{project}` / `{task}`

**生成**: {now} | **HEAD**: `{baseline.get("git_head", "(unknown)")}` | **batch**: `{batch_id}` | **sub-task**: `{sub_task_id}`

## 🛡 第零步: 孤儿自检 (v5.2 / 必跑 / 缺则立即 BLOCKED)

**触发原因**: 历史 case 发现 spawn 完成后 batch_dir 被外部 rm, sub-task tab
在没有 env / manifest 的壳中跑成孤儿. 本步骤是孤儿自我识别 + 优雅退出.
**任何 Bash 操作之前必须先跑这个 block**:

```bash
ORPHAN_FAIL=""
[ -d "{batch_dir_disp}" ] || ORPHAN_FAIL="batch_dir 不存在: {batch_dir_disp}"
[ -z "$ORPHAN_FAIL" ] && [ -f "{role_env_path}" ] || ORPHAN_FAIL="${{ORPHAN_FAIL:-env 文件不存在: {role_env_path}}}"
[ -z "$ORPHAN_FAIL" ] && [ -d "{git_guard_path}" ] || ORPHAN_FAIL="${{ORPHAN_FAIL:-git wrapper 目录不存在: {git_guard_path}}}"
[ -z "$ORPHAN_FAIL" ] && [ -f "{batch_dir_disp}/manifest.json" ] || ORPHAN_FAIL="${{ORPHAN_FAIL:-manifest.json 不存在}}"

if [ -n "$ORPHAN_FAIL" ]; then
    echo "❌ 孤儿自检失败: $ORPHAN_FAIL"
    mkdir -p {handoff_home}/{project}/queue
    cat > {handoff_home}/{project}/queue/{sub_task_id}.BLOCKED.md <<EOF
# BLOCKED — orphan sub-task ({sub_task_id})

Reason: $ORPHAN_FAIL
Detected at: $(date -Iseconds)
Batch: {batch_id}
Workspace: {workspace}

## 主人恢复路径
1. handoff dump --cleanup-orphan 列孤儿
2. 确认无误后 --cleanup-orphan --apply 清残留
3. 手动关闭本 VS Code Claude tab (task: {sub_task_id})
EOF
    exit 1
fi
echo "✅ 孤儿自检通过 (batch_dir + env + git_guard + manifest 全在)"
```

## ⚠️ 第一步: 角色环境 (v5 hard rule)

**本 tab 是 sub-task 角色, 禁 git commit/push/rebase/reset/cherry-pick/tag/revert.**

每次 Bash 调用必须先 source 角色环境 (强制):

```bash
source {role_env_path}
```

这会设置:
- `HANDOFF_ROLE=sub-task`
- `HANDOFF_BATCH_ID={batch_id}`
- `HANDOFF_SUB_TASK_ID={sub_task_id}`
- `PATH={git_guard_path}:$PATH` (git wrapper 接管)

git wrapper + pre-commit hook 双保险: sub-task 角色调 commit 会被物理拦截.

## 第二步: 启动 heartbeat (v5.1 / 529 风暴防御)

```bash
( while true; do
    touch {handoff_home}/{project}/batches/{batch_id}/{sub_task_id}.heartbeat
    sleep 60
  done ) &
echo $! > /tmp/heartbeat-{sub_task_id}.pid
# 闭环前 kill: kill $(cat /tmp/heartbeat-{sub_task_id}.pid)
```

## §第二步.5: 长跑 CLI 调用必须 wrap `timeout` (529 风暴防御延伸 / v5.4)

> **触发**: 5/29 04:05 实战 — codex audit CLI stuck 19 min，会话冻结、heartbeat 失活。
> 根因: codex / `claude -p` / 任意外部 CLI 无超时上限时，单次 hang 拖垮整会话。

**铁律**: 本 sub-task 调用任何可能长跑的外部 CLI (codex / `claude -p` / 全量 `pytest`
/ 大文件 `curl` 等) **必须** wrap `timeout` (默认 300s，按预估调整，上限 600s):

```bash
timeout 300 <cmd> "..." || echo "⚠️ 超时/失败 (exit $?) — 不阻塞，记录后继续"
```

- ✅ 超时即退出，不让单次 hang 冻结会话 / 拖垮 heartbeat (主动预防)
- ✅ 与 watchdog mode 6 (被动 kill) 互补，不替代

## 第三步: Baseline 验证

```bash
cd {workspace}
source {role_env_path}
git log --oneline -1                          # 应 = {baseline.get("git_head", "(unknown)")}
git status -sb                                 # 工作区干净
```

## 文件领域 (file_ownership 严格不可越界)

{ownership_md}

**越界行为**: fan-in tab 会 grep working tree diff 抓到越界文件 + dump BLOCKED. 你的工作会被回滚.

## 当前进度 (roadmap 摘要)

{roadmap_excerpt}

## 你的任务 (sub-task `{sub_task_id}`)

{next_brief}

## 闭环规范

**只在 file_ownership 范围内改文件**. 不要 git commit (会被 wrapper 拦). 完成后:

```bash
cd {workspace}
source {role_env_path}
handoff dump \\
    --task {sub_task_id}-done \\
    --next "(by fan-in tab)" \\
    --batch-id {batch_id} \\
    --batch-done
```

失败 → `--batch-blocked` + `--blocked-reason "<reason>"`.

## STOP 控制

```bash
touch {handoff_home}/STOP_AUTO                                    # 全局暂停
touch {handoff_home}/{project}/STOP_AUTO                            # 项目暂停
touch {handoff_home}/{project}/batches/{batch_id}/STOP              # batch 暂停
```
{inject_section}"""


def build_fan_in_handoff_md(
    *,
    project: str,
    workspace: Path,
    batch_id: str,
    manifest: dict,
    done_files: set[str],
    blocked_files: set[str],
    baseline: dict,
    inject_blocks: Iterable[str],
    handoff_home: Path,
    degraded: bool = False,
    missing: set[str] | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fan_in_task = manifest["fan_in_task"]
    next_after = manifest.get("next_after_fanin", "(待定)")
    role_env_path = handoff_home / project / "batches" / batch_id / "fan-in.env"
    batch_dir = handoff_home / project / "batches" / batch_id

    sub_tasks_md = "\n".join(
        f"- `{st['id']}`: {st['brief']} "
        f"({'✅ done' if st['id'] in done_files else '❌ blocked' if st['id'] in blocked_files else '⚠️ missing'})"
        for st in manifest["sub_tasks"]
    )

    degraded_section = ""
    if degraded:
        missing_str = ", ".join(sorted(missing or [])) or "(none)"
        degraded_section = f"""

## ⚠️ DEGRADED MODE (watchdog 超时降级)

`missing_sub_tasks` (无 .done 无 .blocked): {missing_str}

按 v5 §7.2 详细化:
1. `git status --porcelain=v1` 快照半成品
2. 按 file_ownership 归属分类: accept / orphan / unknown
3. 如 orphan 或 unknown 非空 → dump --status blocked + 三选一让主人裁决:
   - A. 采纳 (orphan 视同 missing sub-task 完成)
   - B. 回滚 (git checkout -- + rm)
   - C. 移 recovery (mv 到 .recovery/{batch_id}/)
"""

    inject_section = _join_inject_blocks(inject_blocks)
    if inject_section:
        inject_section = "\n" + inject_section + "\n"

    return f"""\
# Handoff v5 FAN-IN — `{project}` / `{fan_in_task}`

**生成**: {now} | **HEAD**: `{baseline.get("git_head", "(unknown)")}` | **batch**: `{batch_id}`

## 你的角色: FAN-IN tab

汇总所有 sub-task 结果 + 统一 commit + 跑回归测试 + 推进下一个 task.

## 第一步: 启动状态机 (v5 §4.6)

```bash
cd {workspace}
source {role_env_path}    # 设置 HANDOFF_ROLE=fan-in (git wrapper 放行 commit)

# atomic_create _fan_in_started + 开启心跳后台 (60s touch _fan_in_heartbeat)
handoff heartbeat heartbeat {batch_dir} &
HEARTBEAT_PID=$!
echo "💓 heartbeat daemon pid=$HEARTBEAT_PID"
```

崩溃恢复: watchdog 3 min 检测心跳失活, 自动重 dump 让你重启 (幂等).

## Batch 信息

- batch_id: `{batch_id}`
- 拆分依据: {manifest.get("split_rationale", "(N/A)")}
- Amdahl 估算: {manifest.get("amdahl_estimate", {}).get("estimated_speedup", "N/A")}x

## Sub-task 状态

{sub_tasks_md}
{degraded_section}

## 必做 7 步 (v5 §6.2)

### Step 2: working tree 全量审计

```bash
cd {workspace}
source {role_env_path}
git diff --name-only HEAD > /tmp/modified.txt
git diff --cached --name-only > /tmp/staged.txt
git ls-files --others --exclude-standard > /tmp/untracked.txt
cat /tmp/modified.txt /tmp/staged.txt /tmp/untracked.txt | sort -u > /tmp/all_changes.txt
wc -l /tmp/all_changes.txt
```

### Step 3: file_ownership 守纪律

```bash
python3 -c "
import json, sys
from pathlib import Path
from handoff_fanout.dump import expand_ownership
mf = json.load(open('{batch_dir}/manifest.json'))
ws = Path('{workspace}')
all_owned = set()
for st in mf['sub_tasks']:
    for spec in st['file_ownership']:
        all_owned |= expand_ownership(spec, ws)
all_changes = set(open('/tmp/all_changes.txt').read().splitlines())
out_of_scope = all_changes - all_owned
print(f'all_changes: {{len(all_changes)}} / owned: {{len(all_owned)}}')
if out_of_scope:
    print('❌ 越界:', out_of_scope)
    sys.exit(1)
print('✅ file_ownership pass')
"
```

### Step 4: 验证无擅自 commit

```bash
git log --oneline HEAD@{{1}}..HEAD
# 期待: 空 (sub-task 被 git wrapper 拦截了)
```

### Step 5: 统一 git add + commit (仅 file_ownership 范围)

```bash
xargs git add < /tmp/owned_changes.txt
git commit -m "feat({batch_id}): 汇总 {len(done_files)}/{len(manifest["sub_tasks"])} sub-task"
```

### Step 6: 跑全量回归测试

```bash
pytest tests/ 2>&1 | tail -30
```

### Step 7: 状态机收尾 + 记录 metrics

```bash
handoff heartbeat complete {batch_dir} \\
    --actual-minutes <wall-time-min> \\
    --amdahl-actual <实际 speedup> \\
    --summary "<batch 汇总>"
```

### Step 8: dump 下一个 task

```bash
cd {workspace}
handoff dump \\
    --task {next_after} \\
    --next "<brief>" \\
    --status active
```
{inject_section}"""


def build_blocked_md(*, project: str, task: str, head: str, reason: str) -> str:
    return (
        f"# BLOCKED — project `{project}` / task `{task}`\n\n"
        f"Generated: {datetime.now()}\n"
        f"HEAD: {head}\n\n"
        f"## Reason\n{reason or '(unspecified)'}\n"
    )


def build_orphan_blocked_md(
    *,
    project: str,
    task_id: str,
    age_seconds: float,
    grace_seconds: float,
    handoff_home: Path,
    workspace_root: Path,
    now_iso: str,
) -> str:
    return (
        f"# BLOCKED — orphan sub-task `{task_id}` (watchdog mode 5)\n\n"
        f"Detected at: {now_iso}\n"
        f"Spawned age: {age_seconds:.0f}s (> {grace_seconds:.0f}s grace)\n"
        f"Project: {project}\n\n"
        f"## 判定依据\n"
        f"`ack/{task_id}.spawned` 文件存在 (launchd 已派会话), 但:\n"
        f"- `queue/{task_id}.md` 不存在 (task 文件被消费/清掉)\n"
        f"- `queue/{task_id}.done` 不存在 (未闭环)\n"
        f"- `queue/{task_id}.BLOCKED.md` 不存在 (无 BLOCKED 记录)\n\n"
        f"= **孤儿 tab**: 任务调度环境仍在跑, 但没任务可做.\n\n"
        f"## 可能原因\n"
        f"1. batch_dir 被外部 rm (cleanup 未级联清 spawned tab)\n"
        f"2. STOP_AUTO 之前/期间清了 queue/*.md\n"
        f"3. dump spawn 中途崩, 已 spawn 的 tab 留下成孤儿\n\n"
        f"## 主人恢复路径\n"
        f"1. 查 `{handoff_home}/{project}/launched/{task_id}-*.txt` 确认是哪次 spawn\n"
        f"2. 跑 `handoff dump --cleanup-orphan` 列所有孤儿\n"
        f"3. 确认后 `--cleanup-orphan --apply` 清残留\n"
        f"4. 手动关闭 IDE 中 task=`{task_id}` 的 Claude tab\n"
    )
