# Runbook — `~/.claude-handoff` 状态备份与恢复

> 闭合架构缺口 GAP-ANALYSIS §F#2 / B1：运行时状态零应用级备份。
> 工具：[`install/backup-handoff-state.sh`](../install/backup-handoff-state.sh)。

## 1. 单点故障是什么（the gap）

`~/.claude-handoff` 是整个派窗/接续/审计系统的运行时状态目录（所有链共用）。它当前：

- **无 `.git`、无 export/restore、无应用级备份**——只有 Time-Machine-*Included*（即"没被排除在 TM 之外"），这掩盖了一个事实：**没有恢复故事**。一旦没有最新 TM 快照而丢失该目录，在飞的交接、审计证据、一次性 succession token 全部不可恢复。
- 体积约 **26GB**，但其中 **~99% 是可重建的**：

| 内容 | 体积量级 | 可重建吗 |
|------|----------|----------|
| `*/worktrees` | ~26 GB | ✅ 可，从各 repo `git worktree add` 重建 |
| `*_venv` / `_iterm_engine_venv` | ~13 MB | ✅ 可，`pip install -e` 重装 |
| `*.log`（auto-continue / watchdog） | ~13 MB | ✅ 追加日志，非状态 |
| **`queue/` `ack/` `audits/` `precheck/` `singlepane/` `authority/` `locks/` `launched/` `config.json` + sidecar/token** | **~50–80 MB** | ❌ **不可重建——这才是要保护的** |

**关键不可重建状态**（合计仅 ~50–80MB）= 在飞 `queue/*.uri`、审计 evidence（`audits/*.evidence.json`）、succession sidecar、ack 哨兵、锁、配置。把这 ~50MB 单独打包，就把"丢一个目录=灾难"降成"复制一个小 tar"。

## 2. 备份（导出关键小 state）

```bash
# 默认导出到 ~/.claude-handoff-backups（在 ~/.claude-handoff 之外，避免自吞历史归档）
install/backup-handoff-state.sh

# 或指定目标目录（例如挂载的外部卷 / 同步盘）
install/backup-handoff-state.sh /Volumes/Backup/handoff
```

- 产物：`claude-handoff-state-<host>-<YYYYMMDDThhmmss>.tar.gz`（实测 26GB → ~72MB）。
- 自动**排除** `*/worktrees`、`*_venv`、`*.log`、`tmp.*`、`*.sock`（可重建的 bulk）。
- **只读源目录**，从不改 live 状态——任何链运行中跑都安全。
- 自带两道自检：① 体积超 `HANDOFF_BACKUP_MAXMB`（默认 500MB）则告警（说明 bulk 漏进来了）② 归档里没有 `queue/ack/audits/config.json` 则告警。
- 保留最近 `HANDOFF_BACKUP_KEEP`（默认 12）份，更旧的自动 prune。
- 归档目录 `chmod 700`、归档文件 `chmod 600`（运行态，owner-only）。

### 何时跑（手动·非自动 / cadence 自定）

刻意不上 cron/launchd（避免过度工程 + 备份本身静默失败比没备份更坑）。建议触发点：

- **高危操作前**：`gc-singlepane --execute`、批量 GC、迁移、改共享原语。
- **定期**：按你接受的丢失窗口（如每天一次 / 每会话交棒前）。
- **里程碑后**：审计闸 GREEN 合 main 之后（审计 evidence 是不可重建的）。

> ⚠️ 凭据边界（诚实标注）：归档里是**运营态**——`queue/` `audits/` `config.json` `authority/`
> `singlepane/` + sidecar。已知的敏感凭据**不在**其中（解锁密码在 Keychain
> `mindpersist-login-password`，从不落盘；succession token 是 0600/120s 临时态、过期即废）。
> 但本脚本**不是密钥清洗器**——它不扫描/不剥离内容，只保护归档权限（600/700 owner-only）。
> 因此：把归档当本机运营状态存，**别推到公开/共享位置**；若放同步盘，按敏感数据对待。

## 3. 恢复（restore 故事）

整个目录丢失后：

```bash
# 1) 先恢复不可重建的小 state（解到 $HOME，归档内路径含 .claude-handoff/ 前缀）
tar -xzf claude-handoff-state-<host>-<ts>.tar.gz -C "$HOME"

# 2) worktrees 不在归档里——按需从各 repo 重建（仅恢复正在用的那条链即可）
#    handoff-fanout 引擎会在下次 spawn 时按需 create_worktree；或手动：
#    cd <repo> && git worktree add ~/.claude-handoff/<project>/worktrees/<task> <branch>

# 3) venv 按需重装：
#    python -m venv ~/.claude-handoff/_iterm_engine_venv && pip install -e <...>
```

恢复后 `queue/.uri`、`audits/*.evidence.json`、succession sidecar、`config.json` 即回到归档时刻；in-flight 的交接可继续，审计闸的 evidence 可被 `audit-check` 重新匹配。

**已验证**（2026-06-15 / sw-coord-p29）：解档后 `config.json` 与 live 字节一致、`queue/*.md` 完整还原；归档排除 worktrees（0 泄漏）、venv（0 泄漏）；26GB→72MB。

## 4. 范围与非目标

- ✅ **做**：把不可重建的小 state 变成一个 ~50–80MB 的可复制归档 + 文档化单点故障 + 真实恢复故事。
- ❌ **不做**：24GB 全量自动镜像、cron 排程、增量/去重、跨机同步框架——那是过度工程；最小安全版就是"文档化单点 + 简单手动 export"。
- 🔭 **未来若需**（非现在）：把 `~/.claude-handoff-backups` 纳入显式异地备份；或给 `prune` 扩到陈旧 ack/sidecar（叠 GAP §F#8 / B2）以从源头压小 state。
