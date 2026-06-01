"""Tests for config module — loading, safe_float, safe_int, api_retry, deposit_wallet."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestConfigLoading:
    """Verify config loads from environment with proper defaults."""

    def test_safe_float_valid(self):
        from config import _safe_float
        os.environ["TEST_FLOAT"] = "42.5"
        assert _safe_float("TEST_FLOAT", "0") == 42.5
        del os.environ["TEST_FLOAT"]

    def test_safe_float_invalid(self):
        from config import _safe_float
        os.environ["TEST_FLOAT"] = "not-a-number"
        assert _safe_float("TEST_FLOAT", "10.0") == 10.0
        del os.environ["TEST_FLOAT"]

    def test_safe_float_missing(self):
        from config import _safe_float
        # Key doesn't exist, should use default
        assert "TEST_MISSING" not in os.environ
        assert _safe_float("TEST_MISSING", "99.9") == 99.9

    def test_safe_int_valid(self):
        from config import _safe_int
        os.environ["TEST_INT"] = "7"
        assert _safe_int("TEST_INT", "0") == 7
        del os.environ["TEST_INT"]

    def test_safe_int_invalid(self):
        from config import _safe_int
        os.environ["TEST_INT"] = "abc"
        assert _safe_int("TEST_INT", "14") == 14
        del os.environ["TEST_INT"]

    def test_safe_int_float_string(self):
        from config import _safe_int
        os.environ["TEST_INT"] = "3.14"
        # Should truncate to 3 via int(float(x))
        assert _safe_int("TEST_INT", "0") == 3
        del os.environ["TEST_INT"]

    def test_default_constants(self):
        """Verify constant values exist and are plausible."""
        from config import (
            CHAIN_ID, DATA_API, GAMMA_API, CLOB_HOST,
            MIN_VIABLE_CAPITAL, COPY_MULTIPLIER, MIN_TRADE_SIZE,
        )
        assert CHAIN_ID == 137  # Polygon
        assert DATA_API.startswith("http")
        assert MIN_VIABLE_CAPITAL == 47  # JJC-20260530-004: lowered to enable $47 live
        assert COPY_MULTIPLIER > 0
        assert MIN_TRADE_SIZE > 0


class TestApiRetry:
    """Test the api_retry decorator and safe_get/safe_post helpers."""

    def test_api_retry_exists(self):
        from config import api_retry, safe_get, safe_post
        assert callable(api_retry)
        assert callable(safe_get)
        assert callable(safe_post)

    def test_safe_get_no_retry_on_valid_url(self):
        from config import safe_get
        # Test that safe_get actually works for valid URLs (function exists and doesn't crash)
        import requests
        result = safe_get("https://httpbin.org/get", timeout=5, max_retries=1)
        assert result.status_code == 200


class TestRPCUrls:
    """Verify RPC endpoints are configured."""

    def test_rpc_urls_defined(self):
        from config import RPC_URLS
        assert len(RPC_URLS) >= 3
        assert "1rpc.io" in RPC_URLS[0]
        assert "polygon-rpc.com" in RPC_URLS[1]

    def test_rpc_urls_no_duplicates(self):
        from config import RPC_URLS
        assert len(RPC_URLS) == len(set(RPC_URLS))


class TestDepositWallet:
    """Verify deposit wallet computation."""

    def test_compute_deposit_wallet_exists(self):
        from config import compute_deposit_wallet
        assert callable(compute_deposit_wallet)

    def test_compute_deposit_wallet_returns_address(self):
        from config import compute_deposit_wallet
        addr = compute_deposit_wallet("0x" + "ab" * 20)  # 40 hex chars
        assert addr.startswith("0x")
        assert len(addr) == 42  # 0x + 40 hex chars
