"""Unit tests for the notification _sanitize helper."""

from core.notify import _sanitize


def test_sanitize_strips_dangerous_preserves_safe():
    """Special chars (${}|&;) are stripped, safe chars (.,: etc.) preserved."""
    dangerous = "hello ${}|&; world"
    result = _sanitize(dangerous)
    for ch in "${}|&;":
        assert ch not in result
    assert "hello" in result
    assert "world" in result

    safe_input = "task=done, status: ok (v1.2-beta) +info @user #tag"
    result = _sanitize(safe_input)
    assert result == safe_input


def test_sanitize_truncates_to_500():
    """Input longer than 500 chars is truncated to exactly 500."""
    long_input = "a" * 1000
    result = _sanitize(long_input)
    assert len(result) == 500
