"""handoff-fanout: project-agnostic auto-handoff & parallel fan-out for AI coding sessions."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: read the installed package version (from
    # pyproject) so __version__ can never drift out of sync with the release.
    __version__ = version("handoff-fanout")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+unknown"
