from handoff_fanout import config as C


def test_unified_spawn_default_on_but_killable(tmp_path):
    (tmp_path / "config.json").write_text('{"unified_spawn_enabled": false}')
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is False


def test_unified_spawn_default_is_on_when_absent(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is True


def test_unified_spawn_string_false_is_disabled(tmp_path):
    # The footgun: bool("false") is True. A JSON STRING "false" is an owner trying to
    # KILL the feature — it MUST resolve to False, never silently stay enabled.
    (tmp_path / "config.json").write_text('{"unified_spawn_enabled": "false"}')
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is False


def test_unified_spawn_zero_is_disabled(tmp_path):
    (tmp_path / "config.json").write_text('{"unified_spawn_enabled": 0}')
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is False


def test_unified_spawn_string_truthy_is_enabled(tmp_path):
    (tmp_path / "config.json").write_text('{"unified_spawn_enabled": "true"}')
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is True


def test_unified_spawn_null_defaults_on_silently(tmp_path, capsys):
    # JSON null == "unset" → feature default (ON), and NOT a mis-parse → no warn noise.
    (tmp_path / "config.json").write_text('{"unified_spawn_enabled": null}')
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is True
    assert capsys.readouterr().err == ""


def test_unified_spawn_garbage_defaults_on_with_loud_warn(tmp_path, capsys):
    # Genuinely unrecognised value → default ON, but LOUD (non-silent) so the owner
    # learns their kill-switch value was ignored.
    (tmp_path / "config.json").write_text('{"unified_spawn_enabled": "banana"}')
    cfg = C.load(home=tmp_path)
    assert cfg.unified_spawn_enabled is True
    assert "unified_spawn_enabled" in capsys.readouterr().err


def test_worker_isolation_explicit_no_guess(tmp_path):
    (tmp_path / "config.json").write_text(
        '{"worker_isolation": {"erp":"worktree","wilde-hexe":"singlepane"}}'
    )
    cfg = C.load(home=tmp_path)
    assert cfg.worker_isolation_for("erp") == "worktree"
    assert cfg.worker_isolation_for("wilde-hexe") == "singlepane"


def test_worker_isolation_missing_is_none_not_guessed(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    cfg = C.load(home=tmp_path)
    assert cfg.worker_isolation_for("unknown") is None  # 调用方 fail-closed,不默认猜


def test_worker_isolation_invalid_value_dropped_to_none(tmp_path):
    # A typo'd / unknown isolation mode must NOT pass through (it would route a
    # spawn down an unrecognized path). Drop it → None → caller fails closed.
    (tmp_path / "config.json").write_text(
        '{"worker_isolation": {"erp":"worktre", "ok":"singlepane", "bad": 123}}'
    )
    cfg = C.load(home=tmp_path)
    assert cfg.worker_isolation_for("erp") is None  # typo dropped
    assert cfg.worker_isolation_for("bad") is None  # non-string dropped
    assert cfg.worker_isolation_for("ok") == "singlepane"  # valid survives


def test_worker_isolation_non_dict_is_empty(tmp_path):
    # Mirror _parse_project_inject_blocks: a non-dict shape → {} (no crash).
    (tmp_path / "config.json").write_text('{"worker_isolation": "erp"}')
    cfg = C.load(home=tmp_path)
    assert cfg.worker_isolation_for("erp") is None
