# handoff-fanout

> 跨项目的 AI 编程会话自动接力 + 并行扇出工具.
> 5 层防御: 任务孤儿 / git index 劫持 / 半成品 baseline 接力.

[![CI](https://github.com/rssprivacy-commits/handoff-fanout/actions/workflows/ci.yml/badge.svg)](https://github.com/rssprivacy-commits/handoff-fanout/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**[English README](README.md)**

![30 秒演示](docs/demo/handoff-fanout-demo.gif)

---

## 它解决什么问题

你在一台 Mac/Linux 上跑 AI 编程 agent (Claude Code / Cursor / Aider), 经常**同时开多个 IDE tab**, 还**跨多个项目**. 会遇到 4 类痛点:

| # | 现象 | 根因 |
|---|---|---|
| 1 | 一个 tab 任务完成了, 但下一个 task 永远不 spawn. | 没有标准 handoff 协议, 每个项目自己造接力棒 |
| 2 | Tab B 的 `git commit` 把 Tab A 刚 `git add` 的文件**悄悄带走**. | `.git/index` 是仓库共享文件, `add → commit` 之间没有锁, 不是原子操作 |
| 3 | 某个 session 中途崩了, 后续 session 拿到半成品 baseline 继续跑. | 没有持久化的"上次成功 baseline"记录 + 没有孤儿检测 |
| 4 | 想把一个 task 拆成 N 个并行 sub-task, 但合并阶段无法协调. | 缺 fan-out / fan-in 原语, 也没有文件归属边界 |

`handoff-fanout` 是一个**零运行时依赖的小型 Python 工具集**, 4 个痛点全覆盖. 它从一个跑了一年的生产级 ERP 项目里抽出来, 经历过 3 次实际 commit hijack 事故 (现在 4 层防御挡死).

## 你拿到什么

- **`handoff dump`** — 原子写一个 queue 文件描述下一个 task; IDE auto-spawn helper 监听到后自动开新 tab.
- **`handoff dump --open-batch`** — fan-out: 把一个 task 拆成 N 个 sub-task, 每个有严格 `file_ownership` 边界; 用 fan-in tab 汇总结果.
- **`handoff watchdog`** — 兜底扫描器: 最后一个 sub-task 结束时触发 fan-in; 标记孤儿 tab (例如 launchd 派出后 batch dir 被 rm).
- **`handoff safe-commit`** — `git commit` 包装器, 跨进程 `flock` + `HANDOFF_EXPECTED_FILES` 不变式 + pre-commit hook 联动. 不再被劫持.
- **`handoff heartbeat`** — fan-in tab 心跳守护, Amdahl 加速比指标, 运行时校正 — 让下次 batch 拆分决策有数据.
- **`handoff git-guard`** — `PATH` 注入的 `git` 包装器, **物理拦截** sub-task tab 调 `commit/push/rebase/cherry-pick/reset/revert/tag/am/format-patch/merge`. fan-in tab 是唯一的提交者.

## 5 层防御 (头条)

```
                ┌──────────────────────────────────────────────────┐
                │  Layer 1 — git-guard (PATH 注入 git 包装器)        │
                │  Sub-task tab 根本无法执行 git commit             │
                └──────────────────────────────────────────────────┘
                                       │
                                       ▼
              ┌────────────────────────────────────────────────────┐
              │  Layer 2 — pre-commit hook (HANDOFF_EXPECTED_FILES) │
              │  staged 文件集 ≠ 预期集 → 拒绝提交                 │
              └────────────────────────────────────────────────────┘
                                       │
                                       ▼
          ┌─────────────────────────────────────────────────────────┐
          │  Layer 3 — safe-commit wrapper (flock + 不变式)         │
          │  跨进程锁 ~/.handoff/git-commit.lock                    │
          │  校验 git diff --cached --name-only ⊆ 预期              │
          └─────────────────────────────────────────────────────────┘
                                       │
                                       ▼
       ┌────────────────────────────────────────────────────────────┐
       │  Layer 4 — 原子文件原语 (queue / batch 写入)               │
       │  atomic_create / write_with_fsync / acquire_dir_lock      │
       │  无半成品 queue 文件, 无撕裂的 batch manifest              │
       └────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
      ┌──────────────────────────────────────────────────────────────┐
      │  Layer 5 — watchdog (孤儿 / 失活 / 心跳扫描)                 │
      │  launchd/cron 周期跑. 最后一个 sub-task 静默死时重触发 fan-in │
      │  孤儿 sub-task tab 标 BLOCKED                                │
      └──────────────────────────────────────────────────────────────┘
```

每一层互相独立. 要发生一次实际 hijack, **commit 路径上 4 层 (1-4) 必须同时失效**.

## 快速开始

```bash
pip install handoff-fanout

# 幂等安装: symlink bin/ → ~/.local/bin, 生成 ~/.handoff/config.json,
# 装 git hooks, macOS 可选装 launchd plist.
curl -L https://raw.githubusercontent.com/rssprivacy-commits/handoff-fanout/main/install/install.sh | bash

# 从当前项目目录 dump 下一个 task:
cd ~/Projects/my-repo
handoff dump \
    --task fix-bug-123 \
    --next "修折扣计算的 off-by-one." \
    --status active \
    --tests "tests/test_discount.py"
```

`~/.handoff/my-repo/queue/fix-bug-123.md` 出现, launchd 监听 1 秒内消费, 新 IDE tab 弹起来已经定位在你的 repo, 加载好 handoff.

## 横向对比

| | **handoff-fanout** | Celery | Argo Workflows | Temporal |
|---|---|---|---|---|
| **目标场景** | 单工作站, 多 IDE tab | 分布式服务, 多 worker | Kubernetes 集群 | 分布式服务 |
| **协调单位** | 一个 AI 编程 tab | 一个 Python 函数 | 一个 pod | 一个 workflow 函数 |
| **状态存储** | `~/.handoff/` 下的普通文件 | Redis / RabbitMQ broker + result backend | etcd + K8s 对象 | Cassandra / MySQL + Temporal server |
| **外部依赖** | 无 (zero-dep Python) | broker + (通常) Redis result backend | 整个 K8s 集群 | Temporal server + DB |
| **失败模型** | 原子文件写 + 文件锁 + watchdog | broker 持久性 + ack 语义 | K8s controller 协调 | 事件溯源持久执行 |
| **跨进程 commit 安全** | 一等公民 (4 层) | 不在范围 | 不在范围 | 不在范围 |
| **Fan-out / fan-in** | 是, 带文件归属边界 | 是 (canvas: group/chord/chain) | 是 (DAG) | 是 (子 workflow) |
| **安装成本** | `pip install` + `install.sh` | 数小时 (broker, worker, 监控) | 数天 (集群 + manifest) | 数小时 (server + worker) |
| **设计目标** | AI 编程会话编排 | 后台任务队列 | CI/CD & ML 管线 | 长跑业务工作流 |

正确读法: handoff-fanout **不是** Celery / Argo / Temporal 的竞品, 它占据这些工具刻意不进入的设计角落 (单工作站, IDE-tab 粒度, 无 broker, 懂 git). 需要分布式持久 workflow 执行就挑那三个之一.

## 文档

- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** — queue 文件格式, 状态机, 原子性保证.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — 5 层防御逐层细讲 + 时序图.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — 开发环境, 测试布局, PR 规范.
- **[CHANGELOG.md](CHANGELOG.md)** — 版本历史 & 抽离 roadmap.

## 状态

`v0.1.0` — **公开抽离进行中**. 源代码模块已 port 完. 双语文档 / 安装器 / CI 矩阵是 `v1.0.0` milestone (本分支). 原实现自 2024-Q2 起在一个 70+ DB 表 / 250+ 测试的 ERP 项目里每天用.

`v1.0.0` 发出后, ERP 项目会迁移到一个薄 shim 调用本包.

## License

MIT — 见 [LICENSE](LICENSE).
