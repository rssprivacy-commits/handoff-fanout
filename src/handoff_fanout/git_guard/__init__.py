"""Locate the bundled `git` wrapper that PATH-blocks sub-task commits."""

from __future__ import annotations

from pathlib import Path


def git_guard_dir() -> Path:
    """Return the directory containing the `git` wrapper.

    The handoff-fanout `.env` files prepend this directory to $PATH for
    sub-task tabs so that `git commit` etc. hit the wrapper before the real
    git binary.
    """
    return Path(__file__).resolve().parent
