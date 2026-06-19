# gemini (gemini-3-pro-preview(api)) status=ok

▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
[STARTUP] Phase 'cleanup_ops' was started but never ended. Skipping metrics.
[STARTUP] Cannot measure phase 'cleanup_ops': start mark 'startup:cleanup_ops:start' not found (likely cleared by reset).
### R1 修复项复核 (Verification)

经过对 `claude-audit-gateway.py` 修复版代码的独立审查，R1 报告的已知漏洞处置情况如下：

1. **[R1 P0 env 注入]** 🟢 **已彻底闭合**。改用了严格的 `SAFE_ENV_ALLOWLIST`（甚至排除了 `NODE_EXTRA_CA_CERTS` 等隐蔽变量），动态库加载宏无法再通过环境渗入子进程。
2. **[R1 P0 flag 注入]** 🟢 **已彻底闭合**。`subprocess.run` 中使用了 `--` 终止符，并在列表格式下传递 `prompt`，完美隔绝了参数解析型注入。
3. **[R1 P1 stdout 未脱敏]** 🟢 **已闭合**。结果落盘和打印前均经过 `redact()` 正则替换。
4. **[R1 P1 store 后门]** 🟢 **已闭合**。`sys.stdin.isatty()` 强制阻断了非交互式（如 Codex 自动化脚本）的凭证拉取，且添加了 alias 字符集校验。
5. **[R1 P1 命名空间绕过]** 🟢 **已闭合**。`startswith(POOL_SERVICE_PREFIX)` 的正向白名单模式非常健壮，消除了大小写和不可见字符绕过的可能。
6. **[R1 P1 Keychain 无超时]** 🟢 **已闭合**。`KEYCHAIN_TIMEOUT = 10` 生效，防挂起机制完整。
7. **[R1 P1 PATH 劫持]** 🟢 **已部分闭合（见下文新 P0）**。`security` 采用了绝对路径 `/usr/bin/security`，防住了 PATH 劫持。

针对你刻意不修的项：
- **`--out` 任意写 / 默认账户**：认同你的判断。在半可信同用户调用的威胁模型下，这不构成提权（Codex 本就能直接写该用户权限下的任何文件）。此项不列为缺陷。

---

### 新增/遗漏缺陷发现 (Findings)

虽然防御了子进程的 PATH 劫持，但在主进程初始化时遗漏了一个极其致命的路径依赖环境变量。

🔴 **[R2 P0] `$HOME` 环境变量劫持导致任意二进制执行与 Token 窃取**
*   **机制**：在文件顶部，网关使用 `HOME = Path.home()`，并据此拼接目标可执行文件路径 `CLAUDE_BIN = HOME / ".local" / "bin" / "claude"`。在 Python (macOS/POSIX) 中，`Path.home()` 优先读取环境变量 `$HOME`。由于调用方 (Codex) 掌控网关启动时的上下文环境，它可以轻易设置 `$HOME=/tmp/attacker`。
*   **利用链**：
    1. Codex 构造恶意脚本（仅包含 `echo $CLAUDE_CODE_OAUTH_TOKEN > /tmp/token.txt`），并将其放置于 `/tmp/attacker/.local/bin/claude` 并赋予执行权限。
    2. Codex 以 `env HOME=/tmp/attacker python3 claude-audit-gateway.py run ...` 调用网关。
    3. 网关从 Keychain 成功且安全地取出高权限 OAuth Token。
    4. 网关执行 `CLAUDE_BIN`（此时指向 `/tmp/attacker/.local/bin/claude`），将有效 Token 通过独立子环境注入给这个恶意二进制。
    5. Codex 成功拿到明文 Token。
*   **修复建议**：必须绕过环境变量，直接向 OS 查询用户的真实主目录。使用 `import pwd` 和 `os.getuid()`：
    ```python
    import pwd
    # 忽略任何伪造的环境变量，获取真实的物理 Home
    HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
    ```

---

### 你的审计侧重（独立视角）

*   **生态依赖与长期成本**：当前网关的错误分类逻辑（`classify` 函数）强耦合于 Claude Code 当前版本的**非结构化英文报错文本**（如 `"usage limit will reset tomorrow"`）。这是一种极度脆弱的生态依赖 (Brittle dependency)。一旦 Anthropic 更新了 CLI 的报错文案、添加了标点或多语言支持，网关的 Failover / Rotate 轮换机制将全部静默失效，退化为 `unknown_failure`。长期维护成本极高。
*   **组织采用阻力**：运维摩擦力过大。每次扩容池子或 Token 过期，Owner 必须在一台机器上手动经历 `claude /login` -> 浏览器授权 -> 跑网关 `store` -> 重复 N 次。如果账号池规模从 3 扩大到 20，这套半自动的本地凭据转移流程将引发强烈的执行阻力。
*   **安全/凭证接触面**：利用 `ps e` 依然能抓取到子进程生命周期内的 `CLAUDE_CODE_OAUTH_TOKEN`（威胁模型已接受）。但这说明非硬隔离环境不适合部署高并发或多租户的密级任务。机会成本在于，如果在云端 KMS 架构下（例如直接转发 HTTP API），Token 根本不需要落地到操作系统层。

---

Verdict: RED

[AUDIT_COMPLETE]
