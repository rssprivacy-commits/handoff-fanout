# 安全审计简报 R4 — `claude-audit-gateway.py`（威胁模型已实证校正）

第四轮。**重要校正**：前几轮我的简报把网关目标写成「Codex 即使想要也拿不到 token」——这在单用户机上**实证为假**，构成我的 overclaim。本轮请按**校正后的真实威胁模型**审计。末行 `Verdict: GREEN|RED`。

## 🔴 实证确立的安全天花板（务必据此评判·勿按旧的「绝对隔离」标准）
**实测命令**：`security add-generic-password -s claude-audit-pool/x -w <tok>` 存，然后**任意同用户进程**跑 `security find-generic-password -s claude-audit-pool/x -w` → **直接返回明文 token·无弹窗·无网关**。`-T` ACL 对脚本无效（受信 app=解释器，调用方也有）。
**结论**：单用户机上，Codex（同 Unix 用户·有 shell）**本来就能直接读到池凭据**，绕过网关。所以：
- 网关**不可能**、也**不声称**让「恶意 Codex 拿不到 token」——那是单用户做不到的。
- 「恶意同用户调用方能通过 X 偷 token」**不是**网关缺陷，除非 X 让偷取**比直接读 keychain 更易/更持久/影响他人**（如毒化 `~/.local/bin/claude` 让未来 legit 运行泄漏=真升级，已加 `--out` 闸）。
- 网关真正买到的：① **组织边界**——Codex 编排器代码在**正常流程**不经手 token，正常流程只拿回审计**文本**；② **纵深防御**——防**意外/非恶意**泄漏（clean-env 白名单 / `-I` 解释器隔离 / `--out` confused-deputy 闸 / output+preview redaction / isatty-gated store / HOME pin / security 超时+绝对路径 / 正向命名空间白名单）。
- 真·硬边界需**不同架构**（给 Codex 独立 Unix 用户 + 逐项 Keychain ACL，或网络代理让 token 不落在 Codex 的 OS）——owner 架构决策，不在本网关范围。

## 本轮审计问题（请只答这些·按校正标准）
1. **正常流程泄漏**：在**非恶意**正常使用下，token 会不会泄漏给调用方/落盘/进日志/meta/异常栈？（纵深防御的真问题）
2. **worse-than-baseline**：有没有任何路径让偷取**比「直接读 keychain」更易/更持久/会害到他人**？（给定天花板·这才是真 P0）`--out` 毒化 binary 已加闸（拒写保护目录+拒覆盖可执行），还有别的吗？
3. **纵深防御正确性**：上面列的硬化措施实现对不对？有没有写错导致**正常流程**就漏？
4. **我的裁决是否在合理化**：我据「Codex 能直接读 keychain」把 gemini 之前的 HOME/PYTHONPATH/--out P0 归为「已接受的单用户残留」——这个裁决**站得住吗**？还是我在**合理化掉真缺陷**？（请对抗我的推理）

## 不要再报的（除非满足 worse-than-baseline）
- 「恶意同用户调用方设 NODE_OPTIONS/PYTHONPATH/$HOME/直接读 keychain 偷 token」——已知天花板·非缺陷。
- `--out` 任意写非保护目录 / `--from-account` 默认用户名 / ps e 可见 env token / classify 依赖英文文案 / `--brief` 无大小限——已知接受残留。

末行 `Verdict: GREEN`（给定校正威胁模型·无正常流程泄漏·无 worse-than-baseline·裁决站得住）或 `Verdict: RED`（逐条列·须满足 worse-than-baseline 或正常流程泄漏或裁决错误）。

---

## 当前完整源码

```python
#!/usr/bin/env -S python3 -I
"""
claude-audit-gateway.py — Claude-governed external-audit gateway.

PURPOSE
  Lets a *Codex-master* orchestrator use Claude as its #1 external brain WITHOUT
  ever handling a Claude credential. Codex calls `... run --brief <f> --out <f>`;
  this gateway reads a per-account Claude Code OAuth token from the macOS Keychain,
  runs `claude --print` under an isolated HOME, rotates across the account pool when
  a failure class warrants it, and returns ONLY audit text. The token never crosses
  the process boundary back to the caller, is never printed, and is never written.

GOVERNANCE / BOUNDARY (2026-06-19 codex->Claude-master boundary law)
  - This file is Claude-master territory. Codex EXECUTES it; Codex must not modify it.
  - Account management / credential rotation / OAuth = Claude-governed only.
  - The gateway NEVER reads the live `Claude Code-credentials` service (that is the
    running fleet's primary login). The pool lives in a dedicated Keychain namespace
    `claude-audit-pool/<alias>`, populated by the owner-operated `store` subcommand.
  - cc-account (retired 2026-06-14) is NEVER revived. No code here touches it.

SECURITY INVARIANTS
  1. Token never printed to stdout/stderr and never written to any file (redacted
     everywhere; meta JSON carries only non-secret credential metadata).
  2. Keychain is read-only in `run`/`check`. Only `store` writes, and only the owner
     invokes it (interactive, after a deliberate `/login`).
  3. Each `claude` invocation runs under a fresh isolated HOME + CLAUDE_CONFIG_DIR, so
     it can never read or mutate the live login state.
  4. Conflicting auth env (ANTHROPIC_*, CLAUDE_CODE_USE_*, inherited
     CLAUDE_CODE_OAUTH_TOKEN) is stripped before injecting the account token; proxy
     env (HTTP(S)_PROXY/NO_PROXY) is preserved so the audit call can reach Anthropic.
  5. The real binary `~/.local/bin/claude` is invoked explicitly — NOT the `claude`
     shell function / claude-rc.py wrapper — and WITHOUT --dangerously-skip-permissions.
  6. fail-closed: missing / expired / malformed token => that account fails and the
     pool rotates; if no account yields a clean marked output, exit non-zero.

SECURITY CEILING (honest — read before trusting this as a hard boundary)
  This is an ORGANIZATIONAL boundary + defense-in-depth, NOT an airtight sandbox.
  EMPIRICALLY VERIFIED (2026-06-19): a same-user process can read the pool credential
  directly — `security find-generic-password -s claude-audit-pool/<alias> -w` returns the
  token with no prompt. So a *malicious* same-user caller (incl. Codex, which has a shell)
  can already obtain the token WITHOUT this gateway. macOS `-T` ACLs do not close this for a
  script (the trusted app would be the interpreter, which the caller also runs).
  What this gateway therefore DOES buy:
    - Codex's orchestrator code does not HANDLE the token in normal operation; the token is
      never returned to the caller in the honest flow (it gets audit TEXT only).
    - Defense-in-depth against ACCIDENTAL leakage (clean-env allowlist, `-I` interpreter
      isolation, `--out` confused-deputy guard, output/preview redaction, isatty-gated store).
  What it CANNOT buy single-user: prevention of a deliberately malicious same-user caller.
  A true token boundary needs a different architecture (a separate Unix user for Codex with
  per-item Keychain ACLs, or a network-mediated proxy where the token never lands on Codex's
  OS). That is an owner architectural decision, out of scope for this gateway.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

# The caller (a Codex orchestrator) controls our startup env, so $HOME is attacker-controlled.
# If we trusted it, CLAUDE_BIN / DEFAULT_ACCOUNTS / the `security` login-keychain lookup would
# all resolve under a path the caller planted (e.g. a fake ~/.local/bin/claude that exfiltrates
# the injected token). Pin HOME from the OS pwd database (real uid) and overwrite the env so
# every `security` subprocess we spawn reads the REAL login keychain. The child `claude` call
# still gets its own isolated HOME, set explicitly in invoke_claude().
_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
os.environ["HOME"] = _REAL_HOME
HOME = Path(_REAL_HOME)
DEFAULT_ACCOUNTS = HOME / ".claude" / "audit-pool" / "accounts.json"
CLAUDE_BIN = HOME / ".local" / "bin" / "claude"  # real binary, not the shell function
SECURITY_BIN = "/usr/bin/security"               # absolute: defeats $PATH hijack by the caller
KEYCHAIN_TIMEOUT = 10                             # never hang on a locked/UI-prompting Keychain
POOL_SERVICE_PREFIX = "claude-audit-pool/"        # pool credentials live ONLY here

# The caller (a Codex orchestrator) controls our environment, so we must NOT inherit it.
# We build the child env from a strict allowlist instead of copy-and-strip: a blocklist can
# miss a dynamic-load hook (NODE_OPTIONS / DYLD_* / LD_* / PYTHONPATH ...) that would let the
# caller run code inside the token-bearing `claude` subprocess and exfiltrate the OAuth token.
SAFE_ENV_ALLOWLIST = (
    "PATH", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "LANG", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)

# Anything that looks like a key/token, so it can never leak through previews or output.
SECRET_VALUE_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9._-]+|sk-[A-Za-z0-9._-]{16,}|claude-[A-Za-z0-9._-]{16,}|[A-Za-z0-9._-]{60,})")

# Failure classes after which it is worth trying the next account in the pool.
ROTATABLE_FAILURES = {
    "quota_exhausted",
    "quota_exhausted_spend_limit",
    "rate_limited",
    "auth_invalid",
    "provider_transient",
    "timeout_or_hung",
    "output_integrity_failure",
    "network_or_proxy",
    "unknown_failure",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact(text: str | None) -> str:
    return SECRET_VALUE_RE.sub("[REDACTED]", text or "")


def die(msg: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


# Directories whose contents are trusted/executed later; the gateway must never be tricked
# (confused deputy) into writing audit output over a binary/config there via --out.
_OUT_DENY_PREFIXES = tuple(str(HOME / d) for d in (
    ".local", ".claude", ".codex", "Library", ".ssh", ".config", ".zshenv", ".zshrc",
)) + ("/usr", "/bin", "/sbin", "/etc", "/Library", "/System")


def safe_out_path(raw: str) -> Path:
    """Resolve --out and refuse confused-deputy writes: no sensitive dir, and never overwrite
    an existing executable (which would let a caller poison a trusted binary like ~/.local/bin/claude)."""
    p = Path(raw).expanduser().resolve()
    s = str(p)
    if any(s == d or s.startswith(d + os.sep) for d in _OUT_DENY_PREFIXES):
        die(f"--out refused: {p} is under a protected directory", 2)
    if p.exists() and (p.is_dir() or (p.stat().st_mode & 0o111)):
        die(f"--out refused: {p} exists and is a directory or executable (no overwrite)", 2)
    return p


# --------------------------------------------------------------------------- config


def load_accounts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        die(f"accounts config not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"invalid accounts JSON {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        die(f"accounts config must be an object with an 'accounts' list: {path}")
    accounts = [a for a in data["accounts"] if isinstance(a, dict)]
    for a in accounts:
        if "alias" not in a or "keychainService" not in a:
            die(f"each account needs 'alias' and 'keychainService': {a!r}")
        # Positive allowlist (robust vs. case/whitespace bypass of a negative guard): a pool
        # account may ONLY read from the dedicated namespace, never the live primary login.
        if not str(a["keychainService"]).startswith(POOL_SERVICE_PREFIX):
            die(f"pool keychainService must start with '{POOL_SERVICE_PREFIX}' "
                f"(got {a['keychainService']!r}) — never the live 'Claude Code-credentials'")
    return accounts


def enabled_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in accounts if a.get("enabled", True)]


# ------------------------------------------------------------------------- keychain


def keychain_has_item(service: str, account: str | None) -> bool:
    cmd = [SECURITY_BIN, "find-generic-password", "-s", service]
    if account:
        cmd[2:2] = ["-a", account]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=KEYCHAIN_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def keychain_read_secret(service: str, account: str | None) -> str | None:
    cmd = [SECURITY_BIN, "find-generic-password", "-s", service, "-w"]
    if account:
        cmd[2:2] = ["-a", account]
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=KEYCHAIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0:
        return None
    return (p.stdout or "").strip() or None


def extract_oauth_token(raw: str) -> tuple[str | None, dict[str, Any]]:
    """Return (token, non-secret metadata). Supports plain token or the Claude Code
    credential JSON ({"claudeAiOauth": {...}}). Never returns the token in metadata."""
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, {"credentialFormat": "json_invalid"}
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            return None, {"credentialFormat": "json_missing_claudeAiOauth"}
        token = oauth.get("accessToken")
        expires_at = oauth.get("expiresAt")
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        meta = {
            "credentialFormat": "claude-code-credentials-json",
            "subscriptionType": oauth.get("subscriptionType"),
            "rateLimitTier": oauth.get("rateLimitTier"),
            "expiresAt": expires_at,
            "expired": bool(isinstance(expires_at, int) and expires_at <= now_ms),
        }
        return (token if isinstance(token, str) and token else None), meta
    return raw, {"credentialFormat": "plain"}


# ----------------------------------------------------------------- classification


def classify(returncode: int | None, stdout: str, stderr: str,
             timed_out: bool, marker: str | None) -> str:
    if timed_out:
        return "timeout_or_hung"
    if returncode == 0 and stdout.strip():
        if marker and marker not in stdout:
            return "output_integrity_failure"
        return "ok"
    text = f"{stdout}\n{stderr}".lower()
    if re.search(r"unknown option|invalid choice|matches no known tool|unrecognized arguments", text):
        return "invocation_bug"
    if re.search(r"invalid api key|invalid token|not logged in|unauthorized|authentication|auth error|\b401\b|\b403\b", text):
        return "auth_invalid"
    if re.search(r"monthly spend limit|spend limit|credit limit|billing limit", text):
        return "quota_exhausted_spend_limit"
    if re.search(r"rate limit|too many requests|retry after|\b429\b", text):
        return "rate_limited"
    if re.search(r"usage limit|quota|limit reached|subscription limit|limit will reset", text):
        return "quota_exhausted"
    if re.search(r"overloaded|temporarily unavailable|service unavailable|capacity|\b529\b|\b500\b|\b502\b|\b503\b|\b504\b", text):
        return "provider_transient"
    if re.search(r"dns|network unreachable|proxy|tls|certificate|econn|enotfound|could not resolve|connection refused", text):
        return "network_or_proxy"
    if returncode == 0 and not stdout.strip():
        return "output_integrity_failure"
    return "unknown_failure"


# --------------------------------------------------------------------- invocation


def invoke_claude(token: str, prompt: str, model: str, timeout: int,
                  marker: str | None, alias: str) -> dict[str, Any]:
    # Build the child env from a strict allowlist — do NOT inherit the caller's env, which
    # could carry a dynamic-load hook (NODE_OPTIONS / DYLD_* / LD_* / PYTHONPATH) to run code
    # inside this token-bearing subprocess. Only safe, named keys pass through.
    env = {k: os.environ[k] for k in SAFE_ENV_ALLOWLIST if k in os.environ}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    safe_alias = re.sub(r"[^A-Za-z0-9_.-]+", "_", alias)
    with tempfile.TemporaryDirectory(prefix=f"claude-audit-{safe_alias}.") as isolated_home:
        os.chmod(isolated_home, 0o700)  # belt-and-suspenders (mkdtemp is already 0o700)
        env["HOME"] = isolated_home
        env["CLAUDE_CONFIG_DIR"] = str(Path(isolated_home) / ".claude")
        cmd = [
            str(CLAUDE_BIN),
            "--print",
            "--no-session-persistence",
            "--safe-mode",
            "--permission-mode", "plan",   # read-only; no edits/bash even if a tool is requested
            "--allowedTools", "",          # no tools granted
            "--model", model,
            "--output-format", "text",
            "--",                          # end of flags: prompt can never be parsed as a flag
            prompt,
        ]
        try:
            p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout, env=env, stdin=subprocess.DEVNULL)
            fc = classify(p.returncode, p.stdout, p.stderr, False, marker)
            return {"failureClass": fc, "returncode": p.returncode,
                    "stdout": p.stdout, "stderr": p.stderr, "timedOut": False}
        except subprocess.TimeoutExpired as exc:
            return {"failureClass": "timeout_or_hung", "returncode": None,
                    "stdout": exc.stdout or "", "stderr": exc.stderr or "", "timedOut": True}


def run_one_account(account: dict[str, Any], prompt: str, model: str,
                    timeout: int, marker: str | None) -> dict[str, Any]:
    alias = account["alias"]
    raw = keychain_read_secret(account["keychainService"], account.get("keychainAccount"))
    if not raw:
        return {"alias": alias, "failureClass": "auth_invalid", "returncode": None,
                "stdout": "", "stderr": "missing Keychain credential", "timedOut": False,
                "credentialMeta": {"credentialFormat": "missing"}}
    token, meta = extract_oauth_token(raw)
    if not token:
        return {"alias": alias, "failureClass": "auth_invalid", "returncode": None,
                "stdout": "", "stderr": f"unusable credential format: {meta.get('credentialFormat')}",
                "timedOut": False, "credentialMeta": meta}
    if meta.get("expired"):
        return {"alias": alias, "failureClass": "auth_invalid", "returncode": None,
                "stdout": "", "stderr": "credential access token expired", "timedOut": False,
                "credentialMeta": meta}
    result = invoke_claude(token, prompt, model, timeout, marker, alias)
    result["alias"] = alias
    result["credentialMeta"] = meta
    return result


# ----------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    accounts = enabled_accounts(load_accounts(args.accounts))
    if not accounts:
        die("no enabled accounts in pool", 2)

    if args.brief:
        prompt = Path(args.brief).read_text(encoding="utf-8")
    else:
        prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        die("empty prompt/brief", 2)

    attempts: list[dict[str, Any]] = []
    selected_output = ""
    final_status = "no_usable_account"

    for account in accounts:
        model = args.model or account.get("preferredModel") or "opus"
        r = run_one_account(account, prompt, model, args.timeout, args.require_marker)
        attempts.append({
            "alias": r["alias"], "failureClass": r["failureClass"],
            "returncode": r["returncode"], "timedOut": r["timedOut"],
            "credentialMeta": r.get("credentialMeta"),
            "stderrPreview": redact(r.get("stderr"))[:400],
        })
        if r["failureClass"] == "ok":
            # Defense-in-depth: the token must never reach here, but redact the returned
            # text anyway so a hypothetical echo can never cross the boundary to the caller.
            selected_output = redact(r["stdout"])
            final_status = "ok"
            break
        if r["failureClass"] == "invocation_bug":
            final_status = "invocation_bug"  # config/flag bug — rotating won't help
            break
        if r["failureClass"] not in ROTATABLE_FAILURES:
            break

    meta = {
        "tool": "claude-audit-gateway.py",
        "ts": utc_now(),
        "finalStatus": final_status,
        "accountsFile": str(args.accounts),
        "requireMarker": args.require_marker,
        "attemptedAliases": [a["alias"] for a in attempts],
        "attempts": attempts,
        "tokenDisclosure": "no token printed or written",
    }

    if args.out:
        out_path = safe_out_path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(selected_output, encoding="utf-8")
        meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"claude_audit_out={out_path}")
        print(f"claude_audit_meta={meta_path}")
    else:
        if selected_output:
            print(selected_output.rstrip())
        print(json.dumps(meta, ensure_ascii=False, indent=2), file=sys.stderr)

    return 0 if final_status == "ok" else 1


def cmd_check(args: argparse.Namespace) -> int:
    accounts = load_accounts(args.accounts)
    present = 0
    print(f"accounts_file={args.accounts}")
    for a in accounts:
        ok = keychain_has_item(a["keychainService"], a.get("keychainAccount"))
        present += int(ok and a.get("enabled", True))
        print(f"{a['alias']}: enabled={a.get('enabled', True)} "
              f"keychain={'present' if ok else 'MISSING'} "
              f"service={a['keychainService']} model={a.get('preferredModel', 'opus')}")
    total = len([a for a in accounts if a.get("enabled", True)])
    print(f"pool_usable={present}/{total}")
    print(f"pool_degraded={str(present < total).lower()}")
    print(f"claude_bin={CLAUDE_BIN}{'' if CLAUDE_BIN.exists() else ' (MISSING!)'}")
    return 0 if present > 0 else 1


def cmd_store(args: argparse.Namespace) -> int:
    """Owner-operated credential population. Run AFTER `claude /login`-ing the target
    account so the live `Claude Code-credentials` holds that account's token; this
    copies it into the dedicated pool service `claude-audit-pool/<alias>`.

    Owner-only and NON-BYPASSABLE: this is the one place that reads the live primary, so
    it hard-requires an interactive TTY (a Codex caller is never a TTY) — there is no flag
    to skip it."""
    if not sys.stdin.isatty():
        die("`store` is owner-operated and requires an interactive terminal "
            "(it reads the live primary credential; a non-TTY caller is refused)", 2)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.alias):
        die(f"invalid --alias {args.alias!r} (allowed: letters, digits, '_', '-', <=64)", 2)
    raw = keychain_read_secret(args.from_service, args.from_account)
    if not raw:
        die(f"source credential not found: service={args.from_service} account={args.from_account}", 2)
    token, meta = extract_oauth_token(raw)
    if not token:
        die(f"source credential unusable: {meta.get('credentialFormat')}", 2)
    if meta.get("expired"):
        die("source credential is expired — re-run `claude /login` first", 2)
    target_service = f"{POOL_SERVICE_PREFIX}{args.alias}"
    cmd = [SECURITY_BIN, "add-generic-password", "-a", args.alias, "-s", target_service,
           "-w", token, "-U"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                           timeout=KEYCHAIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        die("keychain write timed out (is the Keychain locked?)", 2)
    if p.returncode != 0:
        die(f"keychain write failed: {redact(p.stderr)}", 2)
    print(f"stored: service={target_service} account={args.alias} "
          f"subscription={meta.get('subscriptionType')} tier={meta.get('rateLimitTier')}")
    print("no token printed or written to files")
    return 0


def cmd_self_test(_args: argparse.Namespace) -> int:
    cases = [
        ("ok", (0, "AUDIT_OK\n", "", False, "AUDIT_OK"), "ok"),
        ("marker missing", (0, "nope\n", "", False, "AUDIT_OK"), "output_integrity_failure"),
        ("empty success", (0, "", "", False, None), "output_integrity_failure"),
        ("invocation bug", (1, "", "unknown option --frob", False, None), "invocation_bug"),
        ("auth invalid", (1, "", "Invalid token", False, None), "auth_invalid"),
        ("spend limit", (1, "monthly spend limit reached", "", False, None), "quota_exhausted_spend_limit"),
        ("rate limit", (1, "", "HTTP 429 too many requests", False, None), "rate_limited"),
        ("quota", (1, "", "usage limit will reset tomorrow", False, None), "quota_exhausted"),
        ("transient", (1, "", "service unavailable 503", False, None), "provider_transient"),
        ("network", (1, "", "proxy connection refused", False, None), "network_or_proxy"),
        ("timeout", (None, "", "", True, None), "timeout_or_hung"),
        ("unknown", (1, "", "weird thing happened", False, None), "unknown_failure"),
    ]
    failures = [f"{n}: expected {exp}, got {classify(*p)}"
                for n, p, exp in cases if classify(*p) != exp]
    # token extraction
    tok, meta = extract_oauth_token(json.dumps(
        {"claudeAiOauth": {"accessToken": "sk-ant-FAKE", "expiresAt": 99999999999999,
                           "subscriptionType": "max"}}))
    if tok != "sk-ant-FAKE" or meta.get("subscriptionType") != "max":
        failures.append("oauth json extraction failed")
    if extract_oauth_token('{"nope": 1}')[0] is not None:
        failures.append("missing claudeAiOauth should yield no token")
    # redaction (incl. realistic OAuth access-token prefix)
    if "sk-ant-" in redact("token sk-ant-oat01-abc123def456ghi789jkl012mno345pqr678"):
        failures.append("redaction leaked a secret")
    # env allowlist must exclude dynamic-load hooks (the P0 exfil vector)
    for danger in ("NODE_OPTIONS", "DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "PYTHONPATH"):
        if danger in SAFE_ENV_ALLOWLIST:
            failures.append(f"env allowlist must not include {danger}")
    # positive-allowlist guard: pool service must be under claude-audit-pool/
    import tempfile as _tf
    for bad in ("Claude Code-credentials", "claude code-credentials", " claude-audit-pool/x"):
        with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"accounts": [{"alias": "x", "keychainService": bad}]}, fh)
            bad_path = fh.name
        try:
            load_accounts(Path(bad_path))
            failures.append(f"guard should reject keychainService {bad!r}")
        except SystemExit:
            pass
        finally:
            os.unlink(bad_path)
    # --out confused-deputy guard: must refuse sensitive dirs
    for bad_out in (str(HOME / ".local" / "bin" / "claude"), str(HOME / ".claude" / "x"), "/usr/bin/x"):
        try:
            safe_out_path(bad_out)
            failures.append(f"--out guard should reject {bad_out}")
        except SystemExit:
            pass
    # HOME must be pinned to the real pwd home, not a hijacked $HOME
    if HOME != Path(pwd.getpwuid(os.getuid()).pw_dir):
        failures.append("HOME not pinned to real pwd home")
    result = {"selfTest": "pass" if not failures else "fail",
              "checked": len(cases) + 3 + 4 + 3 + 3 + 1, "failures": failures}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude-governed external-audit gateway.")
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run a Claude audit over the account pool; return audit text.")
    run.add_argument("--brief")
    run.add_argument("--prompt")
    run.add_argument("--out")
    run.add_argument("--model")
    run.add_argument("--timeout", type=int, default=300)
    run.add_argument("--require-marker", default=None)

    sub.add_parser("check", help="Report pool health and Keychain presence (no token read).")
    sub.add_parser("self-test", help="Offline classifier / extraction / redaction tests (no network, no token).")

    store = sub.add_parser("store", help="Owner-operated: copy the just-logged-in credential into the pool.")
    store.add_argument("--alias", required=True)
    store.add_argument("--from-service", default="Claude Code-credentials")
    store.add_argument("--from-account", default="chenmingzhong")

    args = parser.parse_args()
    return {
        "run": cmd_run, "check": cmd_check, "self-test": cmd_self_test, "store": cmd_store,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
```
