"""Smoke test: package imports and exposes version. Lands first so CI has a green target."""

from importlib.metadata import version

import handoff_fanout


def test_version_string() -> None:
    assert isinstance(handoff_fanout.__version__, str)
    assert handoff_fanout.__version__.count(".") >= 2


def test_version_matches_installed_metadata() -> None:
    """__version__ must track pyproject (via package metadata), not a hardcoded
    literal. Guards the 1.5.0 regression where __init__.py was pinned to 1.4.0
    and shipped a self-misreporting wheel."""
    assert handoff_fanout.__version__ == version("handoff-fanout")
    assert handoff_fanout.__version__ != "0.0.0+unknown"
