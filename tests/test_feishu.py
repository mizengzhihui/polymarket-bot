"""Tests for feishu notification module."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestFeishu:
    """Verify feishu send function behavior."""

    def test_send_no_webhook(self):
        from common.feishu import send_feishu
        result = send_feishu("", "Test", ["line1"])
        assert result is False  # No webhook URL → graceful failure

    def test_send_invalid_webhook(self):
        from common.feishu import send_feishu
        # Feishu API accepts all hook requests — this test checks no exception is raised
        # regardless of webhook validity
        result = send_feishu("https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000", "Test", ["line1"])
        # Should at least not crash — result depends on network/API response
        assert isinstance(result, bool)

    def test_send_none_lines(self):
        from common.feishu import send_feishu
        result = send_feishu("", "Test", [])
        assert result is False


class TestFliehuFormat:
    """Verify message formatting doesn't crash."""

    def test_with_separators(self):
        from common.feishu import send_feishu
        lines = ["===", "item1", "---", "item2", ""]
        result = send_feishu("", "Title", lines)
        assert result is False  # Graceful handling with valid format

    def test_long_title(self):
        from common.feishu import send_feishu
        long_title = "A" * 100
        result = send_feishu("", long_title, ["content"])
        assert result is False  # Should handle gracefully
