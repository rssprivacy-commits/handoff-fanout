"""Unified CLI dispatcher: `handoff <subcommand>`.

Subcommands delegate to the per-module `main()` so each module is also
independently invokable via its own `handoff-<subcommand>` entry point.
"""

from __future__ import annotations

import argparse
import sys

from handoff_fanout import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="handoff",
        description="Project-agnostic auto-handoff & parallel fan-out for AI coding sessions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser(
        "dump",
        help="Generate handoff queue file for next task (full args: see `handoff-dump --help`). "
        "batch/fan-in paths (--batch-*/--open-batch) MUST NOT be used for coordinator "
        "succession (中枢接力) — that goes through `handoff audit-close --coordinator "
        "--status active`",
    )
    sub.add_parser(
        "spawn",
        help="Fresh-spawn intent producer (no retro gate / no roadmap; see `handoff spawn "
        "--help`). --role supervisor_succession is NOT a manual path: it requires the "
        "one-time --succession-token issued by `handoff audit-close --coordinator "
        "--status active` (G4 收口)",
    )
    sub.add_parser(
        "watchdog", help="Run watchdog scan (fail-safe fan-in trigger; meant for launchd/cron)"
    )
    sub.add_parser(
        "heartbeat",
        help="Fan-in tab heartbeat / completion / metrics (see `handoff-heartbeat --help`)",
    )
    sub.add_parser(
        "safe-commit", help="Hijack-safe git commit wrapper (see `handoff-safe-commit --help`)"
    )
    sub.add_parser(
        "precheck",
        help="v5.4 retro-evidence precheck (see `handoff-precheck --help`)",
    )
    sub.add_parser(
        "prune",
        help="Remove leftover heartbeat/529/uri sidecars for terminal tasks (dry-run by default)",
    )
    sub.add_parser(
        "coord-dispatch",
        help="Low-friction coordinator fan-out: draft worker briefs from a tasks-json, run the "
        "HARD machine-judged concurrency-conflict gate, and CONCURRENTLY dispatch the proven-"
        "disjoint wave (dry-run by default; --execute fans the wave out via dx-spawn-session.sh "
        "in parallel, load-/width-capped + failure-isolated, deferring conflicting tasks to a "
        "later wave)",
    )
    sub.add_parser(
        "gc-singlepane",
        help="Quarantine STALE singlepane coordinator sidecars + workspace files so the shared "
        "identity resolver resolves uniquely again (liveness-gated, dry-run by default, reversible)",
    )
    sub.add_parser(
        "audit-run",
        help="Register one codex audit run (findings artifact + sidecar manifest)",
    )
    sub.add_parser(
        "audit-disposition",
        help="Validate + persist one disposition for an original codex finding",
    )
    sub.add_parser(
        "audit-close",
        help="Single-process: assemble codex_audit block → write evidence → dump. With "
        "--coordinator --status active this is THE coordinator relay (中枢交棒) path: "
        "retro-gated, and the sole issuer of the one-time succession authority token",
    )
    sub.add_parser(
        "audit-discharge",
        help="req3: write the non-forgeable audited-to-terminal signal "
        "(ack/<task>.audit_discharged) for a worker the coordinator merged + audited GREEN; "
        "the autoclose driver corroborates it (git ancestry + spawn anchors) before closing",
    )
    sub.add_parser(
        "audit-check",
        help="Delivery-audit machine gate: verify dual-brain evidence exists for a "
        "repo+range (used by pre-push / post-merge / post-commit hooks)",
    )
    sub.add_parser(
        "audit-override",
        help="OWNER-ONLY (tty-gated): accept a RED-verdict audit evidence with a "
        "checksummed override record — AI sessions cannot run this",
    )
    sub.add_parser(
        "worktree",
        help="Inspect / reclaim per-session git worktrees (see `handoff worktree --help`)",
    )
    # --- slice S5a: minimal observable + rescuable status board (status_board.py) ---
    # Additive only: these delegate to the lazily-imported ``status_board`` module so the
    # live engine paths above (dump / worktree / audit-close / …) NEVER import supervisor
    # or status_board (S5a 红线: 只增不改运行路径 — the imports are inside the dispatch).
    sub.add_parser("status", help="Human status board (real runtime business view + DAG overlay)")
    sub.add_parser("sessions", help="Which sessions can I close? (visible window ∩ central done)")
    sub.add_parser("pause", help="Pause auto-continue (write STOP_AUTO sentinel; reversible)")
    sub.add_parser("resume", help="Resume auto-continue (remove STOP_AUTO sentinel)")
    sub.add_parser(
        "stop",
        help="Stop auto-continue (= pause; reversible — `touch ~/.claude-handoff/done` to "
        "stop permanently)",
    )
    sub.add_parser("approve", help="Approve a bound supervisor run's AWAIT_APPROVAL node")
    sub.add_parser("force-sync", help="Split-brain escape hatch: detach a run's DAG overlay")
    sub.add_parser("bind", help="Bridge: bind task ↔ supervisor run/node (overlay/approve entry)")

    # We parse only the first arg, then delegate the rest to the subcommand's own argparse.
    args, rest = parser.parse_known_args(argv)

    _STATUS_BOARD_SUBCOMMANDS = {
        "status",
        "sessions",
        "pause",
        "resume",
        "stop",
        "approve",
        "force-sync",
        "bind",
    }
    if args.subcommand in _STATUS_BOARD_SUBCOMMANDS:
        from handoff_fanout import status_board

        # Re-prepend the subcommand so status_board's own argparse dispatches it.
        return status_board.main([args.subcommand, *rest])

    if args.subcommand == "dump":
        from handoff_fanout import dump

        return dump.main(rest)
    if args.subcommand == "spawn":
        from handoff_fanout import spawn

        return spawn.main(rest)
    if args.subcommand == "watchdog":
        from handoff_fanout import watchdog

        return watchdog.main()
    if args.subcommand == "heartbeat":
        from handoff_fanout import heartbeat

        return heartbeat.main(rest)
    if args.subcommand == "safe-commit":
        from handoff_fanout import safe_commit

        return safe_commit.main(rest)
    if args.subcommand == "precheck":
        from handoff_fanout import handoff_precheck

        return handoff_precheck.main(rest)
    if args.subcommand == "gc-singlepane":
        from handoff_fanout import gc_singlepane

        return gc_singlepane.main(rest)

    if args.subcommand == "prune":
        from handoff_fanout import prune

        return prune.main(rest)
    if args.subcommand == "coord-dispatch":
        from handoff_fanout import coord_dispatch

        return coord_dispatch.main(rest)
    if args.subcommand == "audit-run":
        from handoff_fanout import codex_audit

        return codex_audit.main_audit_run(rest)
    if args.subcommand == "audit-disposition":
        from handoff_fanout import codex_audit

        return codex_audit.main_audit_disposition(rest)
    if args.subcommand == "audit-close":
        from handoff_fanout import codex_audit

        return codex_audit.main_audit_close(rest)
    if args.subcommand == "audit-discharge":
        from handoff_fanout import codex_audit

        return codex_audit.main_audit_discharge(rest)
    if args.subcommand == "audit-check":
        from handoff_fanout import audit_evidence

        return audit_evidence.main_check(rest)
    if args.subcommand == "audit-override":
        from handoff_fanout import audit_evidence

        return audit_evidence.main_override(rest)
    if args.subcommand == "worktree":
        from handoff_fanout import worktree

        return worktree.main(rest)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
