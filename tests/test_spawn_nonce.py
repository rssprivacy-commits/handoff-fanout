import re

from handoff_fanout.spawn_nonce import new_nonce, nonce_in_title, title_for


def test_nonce_unguessable_and_unique():
    a, b = new_nonce(), new_nonce()
    assert a != b
    assert re.fullmatch(r"[0-9a-f]{16}", a)  # 64-bit hex, 不可猜


def test_title_carries_all_fields():
    t = title_for(project="erp", task_id="fix-46", role="worker", nonce="deadbeefcafef00d")
    assert "erp" in t and "fix-46" in t and "worker" in t and "deadbeefcafef00d" in t


def test_nonce_in_title_exact_match_only():
    t = title_for(project="erp", task_id="t1", role="worker", nonce="deadbeefcafef00d")
    assert nonce_in_title(t, "deadbeefcafef00d") is True
    assert nonce_in_title(t, "deadbeefcafef00e") is False  # 单字符差 → 不匹配
    assert nonce_in_title("erp · t1 · worker", "deadbeefcafef00d") is False  # 无 nonce
