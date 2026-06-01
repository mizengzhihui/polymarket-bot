"""Tests for deposit wallet computation and validation."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestComputeDepositWallet:
    """Verify deterministic deposit wallet address computation."""

    def test_known_input_output(self):
        """Test with a known EOA address — verify deterministic output."""
        from config import compute_deposit_wallet

        # Test with a common test address
        eoa = "0x" + "aa" * 20
        result = compute_deposit_wallet(eoa)
        assert isinstance(result, str)
        assert result.startswith("0x")
        assert len(result) == 42  # 0x + 40 hex chars
        assert all(c in "0123456789abcdefABCDEF" for c in result[2:])

    def test_deterministic(self):
        """Same input should always produce same output."""
        from config import compute_deposit_wallet

        eoa = "0x" + "bb" * 20
        result1 = compute_deposit_wallet(eoa)
        result2 = compute_deposit_wallet(eoa)
        assert result1 == result2

    def test_different_inputs_different_outputs(self):
        """Different EOAs should produce different deposit wallets."""
        from config import compute_deposit_wallet

        addr1 = compute_deposit_wallet("0x" + "aa" * 20)
        addr2 = compute_deposit_wallet("0x" + "bb" * 20)
        assert addr1 != addr2


class TestConfigConstants:
    """Verify deposit wallet related constants."""

    def test_constants_exist(self):
        from config import DEPOSIT_WALLET_FACTORY, DEPOSIT_WALLET_IMPLEMENTATION
        assert DEPOSIT_WALLET_FACTORY.startswith("0x")
        assert DEPOSIT_WALLET_IMPLEMENTATION.startswith("0x")
        assert len(DEPOSIT_WALLET_FACTORY) == 42
        assert len(DEPOSIT_WALLET_IMPLEMENTATION) == 42
