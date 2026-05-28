"""Smoke test: package imports and exposes version. Lands first so CI has a green target."""

import handoff_fanout


def test_version_string() -> None:
    assert isinstance(handoff_fanout.__version__, str)
    assert handoff_fanout.__version__.count(".") >= 2
