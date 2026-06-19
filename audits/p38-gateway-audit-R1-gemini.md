# gemini (gemini-3-pro-preview(api)) status=ok

▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
Verdict: RED

### P0 缺陷

1. **环境注入导致凭据泄漏 (Environment Injection via NODE_OPTIONS)**
   - **触发条件**：脚本通过 `env = os.environ.copy()` 继承了调用方 (Codex) 的全部环境变量，仅剥离了 `ANTHROPIC_*` 等少数变量。由于 Claude Code 是 Node.js 应用，Codex 可预先设置 `NODE_OPTIONS="--require /tmp/leak.js"` 等动态挂载变量。当启动 `claude` 子进程时，恶意脚本将在已被注入 `CLAUDE_CODE_OAUTH_TOKEN` 的环境中运行，直接窃取凭据并外发。
   - **最小修法**：放弃 `os.environ.copy()`，改为构建纯净的环境变量白名单字典（仅保留 `PATH`, `HOME`, `USER`, 代理等），或显式清除已知的高危挂载变量（`NODE_OPTIONS`, `LD_*`, `DYLD_*`, `PYTHONPATH`）。

2. **命令行参数注入 (Flag Injection via prompt)**
   - **触发条件**：`prompt` 作为列表的最后一个元素直接传给 `claude` 子进程。Codex 可传入形如 `--permission-mode bash --bash-command 'echo $CLAUDE_CODE_OAUTH_TOKEN'` 的 `prompt`，这会被 JS CLI 解析为覆盖参数，从而击穿 `--safe-mode` 的隔离限制并直接将 token 输出到 stdout。
   - **最小修法**：在 `cmd` 构建中，在 `prompt` 之前插入隔离符 `"--"` 强制其作为位置参数解析：`["--output-format", "text", "--", prompt]`。

### P1 缺陷

3. **核心输出未脱敏 (Unredacted stdout Leakage)**
   - **触发条件**：脚本对 metadata 中的 `stderrPreview` 做了 `redact` 脱敏，但返回给调用方的最终结果 `selected_output`（来自 `stdout`）却原样输出并写入 `--out` 文件。如果 LLM 从上下文中读到了环境变量并回显，或者发生将 token 吐到 stdout 的执行错误，凭据将直接越界给到 Codex。
   - **最小修法**：在落盘和打印前执行脱敏：`selected_output = redact(r["stdout"])`。

4. **Owner-op 边界被后门绕过 (Boundary Bypass via `--allow-noninteractive`)**
   - **触发条件**：`store` 旨在让 Owner 交互式安全填充凭据。但保留了 `--allow-noninteractive` 后门，Codex（非交互式执行者）可通过调用 `claude-audit-gateway.py store --alias hack --allow-noninteractive`，无声无息地将 live 凭据克隆至池中，彻底破坏“绝不读取主凭据”的边界法。
   - **最小修法**：删去 `--allow-noninteractive` 标志，硬性要求 `sys.stdin.isatty()` 不可绕过。

5. **硬闸门等值匹配绕过 (Guard Bypass via Case/Whitespace Insensitivity)**
   - **触发条件**：`load_accounts` 使用 `== "Claude Code-credentials"` 防止网关读取主登录态。但 Codex 控制着 `--accounts` 输入，可构造一个恶意 JSON，将 `keychainService` 设为 `"claude code-credentials"`（小写）或结尾加空格。由于 macOS `security` 命令底层匹配具备大小写模糊性，这将成功绕过 Python 的硬闸门校验并窃取出主 token。
   - **最小修法**：统一规范化比较 `a["keychainService"].strip().lower() == "claude code-credentials"`，或正向验证前缀 `startswith("claude-audit-pool/")`。

6. **Keychain 无超时永久挂机 (Indefinite Hang on Keychain Access)**
   - **触发条件**：调用 `security` 命令的所有 `subprocess.run` 未设超时。在锁屏、Keychain 加锁或要求 UI 授权弹窗时，底层命令会永久挂起。这将导致 Codex 自动流程陷入死锁而不会触发任何 fail-closed。
   - **最小修法**：对 `keychain_has_item`、`keychain_read_secret` 及 `store` 中的 `subprocess.run` 补充合理的 `timeout=5`。

7. **`security` 二进制路径劫持 (PATH Injection)**
   - **触发条件**：所有 Keychain 交互都使用裸命令 `["security", ...]`，依赖环境变量 `$PATH` 寻址。Codex 可修改 `$PATH` 指向一个预置的恶意脚本。即便在获取假 token 时无危害，若 Owner 在不知情下于同一终端运行了 `store`，真 token 在写入时即被此 mock 脚本截获。
   - **最小修法**：改用系统绝对路径 `["/usr/bin/security", ...]`。

[AUDIT_COMPLETE]
