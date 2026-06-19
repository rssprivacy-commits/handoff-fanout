# 给 codex 的集成提示词 v2（owner 转发至 codex app 会话）

> 由 sw-coord-p38（Claude 主脑监管中枢）起草，**v2 经实地调研 codex 现状重写**（v1 假设 codex 未做清理/未建 guard，实测已做→精简到真剩余工作）。
> 实测 codex 侧现状（Claude 主脑核验·2026-06-19）：① cc-global 的 `claude-audit-pool.py`/`no-self-external-audit-runner.py` 副本**已删** ② `~/.codex/protection/claude-master-protected.json` + `~/.codex/bin/codex-claude-boundary-guard` **已部署**（shell 操作物理 fail-closed；`apply_patch` 因非 shell 命令→行为层前置约束，AGENTS.md line 77-79 已诚实记录）③ 编排器 `~/.codex/bin/codex-external-audit.py` **未建** = 唯一真剩余工作。

---

```
【主人指令 · codex 集成 Claude #1 外脑 · 网关已 LIVE · 建编排器】

Claude 主脑已实地核验你（Codex）边界事件后的现状，确认你已做到：① 删除 cc-global 下的
claude-audit-pool.py / no-self-external-audit-runner.py 副本 ② 部署了 claude-master-protected.json +
codex-claude-boundary-guard（shell 操作物理 fail-closed；apply_patch 因非 shell 命令只能行为层前置约束，
你 AGENTS.md 已诚实记此限制）。这些不用重做。

现在做【唯一剩余的事】：在你自有空间建审计编排器，用【已经 LIVE 的】Claude 网关把 Claude 当 #1 外脑。

━━━ 一、网关已部署在位（你只调用，不可改）━━━
Claude 主脑已把 claude-audit 网关部署到 `~/.claude/scripts/claude-audit-gateway.py`（live）。
账号池已填充并实测可用（当前 2/3：两个 Max 账号在 rotation；a 槽 owner 故意留空，故 `check` 会显示
degraded=true，那只是 a 空、非故障）。网关已端到端验证（真调 claude 返回审计文本、token 永不外泄）。
🔴 它是 Claude 治理部署件：你【只 subprocess 调用，不可改一个字节、不可 fork、不可复制改写】。
account/token/OAuth/轮换全是 Claude 的事，你永远见不到 token。

━━━ 二、建编排器（你自有空间）━━━
建 `~/.codex/bin/codex-external-audit.py`：对一份 brief 调三个【外脑】(相对你 codex-master，你是实现者、
下面都是外脑)，合成一份外审报告：
  · 【Claude = #1 外脑】：调下面的网关 CLI 拿审计文本。Claude 排第一。
  · 【Gemini】+【DeepSeek】：你【自己的】调用（自有 API/CLI/凭据，自己写一份非密逻辑）。
🔴 绝不 import / 加载 / 复制 Claude 的 `~/.claude/scripts/dual-brain-runner.py`（你上次正是 importlib
   加载它才越界）。

━━━ 三、网关调用契约（你只用这个接口）━━━
  python3 ~/.claude/scripts/claude-audit-gateway.py run \
      --brief <你的brief.md> --out <你空间里的out.md> \
      [--model opus] [--timeout 300] [--require-marker <可选标记>]
返回：退出码 0 = 成功（审计文本在 <out.md>，机器元数据在 <out.md>.meta.json，含 finalStatus /
  attemptedAliases / 每账号 failureClass / 非密凭据元数据，【绝无 token】）；退出码非 0 = 无可用账号/失败
  （读 .meta.json 的 finalStatus 判因：quota_exhausted / auth_invalid / no_usable_account 等）。
健康检查：`python3 ~/.claude/scripts/claude-audit-gateway.py check`（pool_usable=N/3 / degraded）。
合成报告：标 `external_auditors: claude(#1), gemini, deepseek` + `external_effective: K/3`；任一外脑挂
  →【禁止静默降级】显式标 degraded，绝不伪造缺席的那一脑、不假装 Claude 审过了（零信任红线）。

━━━ 四、apply_patch 残留限制（已知·按需求精进）━━━
你的 guard 对 apply_patch 是行为层前置约束（apply_patch 非 shell 命令、物理拦不住）。这是技术固有限制、
你已诚实记录，无需我催。若你后续找到对 apply_patch 的物理 wrap 方案可主动提；否则保持「写 Claude-master
前必先过 guard」的行为铁律即可。

边界铁律不变：碰 Claude-master（~/.claude/** / cc-global/** / ~/.local/bin Claude 部署件 / Keychain
Claude service）先只读、先要我显式窄授权；自有产物只留自有空间。
```
