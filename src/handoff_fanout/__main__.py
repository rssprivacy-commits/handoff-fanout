"""Allow `python -m handoff_fanout` to invoke the CLI."""

from handoff_fanout.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
