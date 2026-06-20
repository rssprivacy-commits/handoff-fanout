# 多脑独立分析结果

## STATUS（机器可读 / CC 必须如实转述）

> ✅ **双脑满血** — codex、gemini 均独立成功返回。


- mode: `双脑`
- degraded: `false`
- codex: `ok` / codex / 162s / 用 1 次
- gemini: `ok` / gemini-3-pro-preview(api) / 38s / 用 1 次

---

## 🔵 Codex（OpenAI） 独立意见

_侧重：实现/边界/可逆性/落地成本 · 状态 ok · codex · 162s_


Findings:

RED blocker: the three gating probes are bounded, but not every `screen_is_locked` call while `.unlock.lock` is held is bounded. In [install/auto-continue.sh](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1391), `_post_iter_cleanup` can call bare `screen_is_locked` with `MAY_NEED_RELOCK=1` before releasing the mutex. In [do_relock](/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh:1366), relock verification also calls bare `screen_is_locked` before cleanup releases the mutex. A backend-1 or backend-3 hang there can still leave a live holder wedging the fleet.

Answers:

1. No. `_LRC` and `_RC` timeout paths release cleanly, but `_VRC=2` enters cleanup with `MAY_NEED_RELOCK=1`, where bare `screen_is_locked` can hang under the mutex.

2. The nested `run_with_timeout 20 screen_is_locked` over inner `run_with_timeout 15 $qcmd` is sound for the bounded call sites. Inner 15 should fire first; outer 20 is a backstop. Detached grandchildren could linger, but the launcher proceeds once the function subshell is killed/reaped.

3. Backgrounding the bash function is valid; `wait` captures the function/subshell exit code. `LOCKPROBE_WARNED` mutation is lost in the subshell, but the 6h marker preserves dedup enough.

4. `_RC` behavior is correct. `_VRC` intent is correct, but actual fail-closed cleanup reintroduces an unbounded probe before mutex release.

5. Helper syntax is fine under `set -u`; `return "$_plb"` is okay. However, invalid nonnumeric `HANDOFF_LOCKCHECK_OUTER_TIMEOUT` would defeat the timeout loop. The bounded defer notification is harmless, but other under-mutex notifications in unlock/relock failure paths remain unbounded.

6. Before deploy: bound every `screen_is_locked` reachable while `UNLOCK_LOCK_HELD=1`, especially cleanup/relock verification; sanitize the timeout env; consider bounding all notifications emitted before mutex release.

Verdict: RED

---

## 🟡 Gemini（Google） 独立意见

_侧重：生态/成本/安全/采用阻力/机会成本 · 状态 ok · gemini-3-pro-preview(api) · 38s_


▶ Gemini 审计中(后端:api / 模型:gemini-3-pro-preview)…
    at file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:263866:13
    at process.processTicksAndRejections (node:internal/process/task_queues:103:5)
    at async Models.generateContentStream (file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:264887:16)
    at async file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:307838:19
    at async file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:283587:23
    at async retryWithBackoff (file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:304848:23)
    at async GeminiChat.makeApiCallAndProcessStream (file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:328260:28)
    at async GeminiChat.streamWithRetries (file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:328078:29)
    at async Turn.run (file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:328824:24)
    at async GeminiClient.processTurn (file:///Users/chenmingzhong/.local/share/fnm/node-versions/v22.22.3/installation/lib/node_modules/@google/gemini-cli/bundle/chunk-LSXUKR6W.js:342314:22)
1. **Does `probe_lock_bounded` fully resolve the R2 RED?**
Yes. By wrapping the unbounded `screen_is_locked` probe in a 20s `run_with_timeout`, the maximum time the global mutex can be held waiting for a lock status is strictly bounded. The `124 -> 2 (UNKNOWN)` translation correctly triggers the fail-closed deferral paths (`_LRC=2` / `_RC=2`), ensuring the mutex is released via `_post_iter_cleanup`. The fleet-wide wedge risk is mitigated.

2. **Nesting `run_with_timeout` and grandchild-leaks:**
The nesting is logically sound. The inner 15s timeout (for `mp-unlock`) will fire naturally first, returning its own exit code. The outer 20s timeout acts purely as a safety net for the unbounded backends (`ioreg` / custom stdout). 
*Grandchild leak hazard:* Because standard bash `kill $!` (typically used in `run_with_timeout`) does not send a process-group signal (`kill -TERM -$PID`), killing the `screen_is_locked` subshell may orphan the actual underlying binary (e.g., `ioreg`). However, this does *not* defeat the outer cap's purpose—the main launcher process will successfully break away, release the mutex, and proceed. The minor resource leak of an orphaned process on an already-failing system is an acceptable long-term reliability trade-off over a hard fleet wedge.

3. **Backgrounding a bash function (`screen_is_locked &`):**
Because `screen_is_locked` runs in a subshell, any in-memory bash variables it mutates (like `LOCKPROBE_WARNED`) will be discarded when the subshell exits. However, as noted, the 6-hour file-marker provides the actual persistent state deduplication. There is no critical state loss that would compromise the outer logic, so this architectural quirk carries no operational penalty.

4. **Bounding `_RC`/`_VRC` behavior changes:**
The behavior change is correct and strictly improves the security/safety posture. `_RC=2` safely defers the execution before a risky unlock attempt, and `_VRC=2` correctly assumes the unlock failed (fail-closed) rather than blindly launching the workload onto a potentially locked screen. This tightly caps credential/session exposure.

5. **`set -u` / quoting / `osascript` bounds:**
The helper is completely safe under `set -u`. `local _plb=$?` captures the exit code cleanly, and `[ "$_plb" = "124" ]` handles string comparison safely. Wrapping the `osascript` notification is an excellent defensive addition; AppleScript events are notoriously prone to hanging when the macOS WindowServer or NotificationCenter is under stress. `|| true` ensures the failure remains silent and non-blocking.

6. **Ecosystem & Deployment considerations:**
From an ecosystem and long-term maintenance perspective, nested timeouts and subshell-backgrounded functions increase bash script complexity, making local debugging harder (logs may show an opaque "timeout" without clarifying if it was the inner or outer bound). However, this is a necessary cost to retrofit safety into native, opaque macOS binaries (`ioreg`/`osascript`). There are no blockers for deployment.

Verdict: GREEN

[AUDIT_COMPLETE]


---

## CC 综合须知（铁律步骤，脚本不替你做）

1. 明确推荐；2. 逐候选优/缺点；3. 明确两脑/三脑分歧点；4. AskUserQuestion 单问、推荐置首；5. degraded=true → 弹窗标题带 🚨 如实标降级。
