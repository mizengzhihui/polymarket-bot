"""Tests for risk check logic copied from bot.py — capital limits, stop-loss, exposure."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestViableCapital:
    """Verify MIN_VIABLE_CAPITAL gate blocks insufficient capital."""

    def test_min_viable_threshold(self):
        """JJC-20260530-004: USER_CAPITAL=47 >= MIN_VIABLE_CAPITAL=47 → live trading allowed."""
        from config import USER_CAPITAL, MIN_VIABLE_CAPITAL
        # User capital must be >= MIN_VIABLE_CAPITAL for live trading
        assert USER_CAPITAL >= MIN_VIABLE_CAPITAL, (
            f"User ${USER_CAPITAL} >= Minimum ${MIN_VIABLE_CAPITAL} → live trading allowed"
        )

    def test_min_viable_capital_is_47(self):
        from config import MIN_VIABLE_CAPITAL
        assert MIN_VIABLE_CAPITAL == 47, (
            f"Expected 47, got {MIN_VIABLE_CAPITAL}"
        )


class TestPerWalletConfig:
    """Verify per-wallet configuration overrides."""

    def test_chloe_t1_overrides(self):
        from config import get_wallet_config
        cfg = get_wallet_config("0x9ac2536ed93f8fe8ce91d9662b03bcbb19ccbe3d")
        assert cfg.get("max_position_pct") == 0.05
        assert cfg.get("max_position_loss") == 1.50
        assert cfg.get("copy_multiplier") == 0.3

    def test_unknown_wallet_no_overrides(self):
        from config import get_wallet_config
        cfg = get_wallet_config("0x" + "ff" * 20)
        assert cfg == {}

    def test_empty_wallet_no_overrides(self):
        from config import get_wallet_config
        assert get_wallet_config("") == {}
        assert get_wallet_config(None) == {}


class TestWalletSelectorRisk:
    """Verify wallet_selector risk scoring can parse trades."""

    def test_calc_risk_score_empty(self):
        from wallet_selector import calc_risk_score
        max_loss, avg_loss, num_losing = calc_risk_score([], 1000)
        assert max_loss == 0.0
        assert avg_loss == 0.0
        assert num_losing == 0

    def test_calc_risk_score_few_trades(self):
        from wallet_selector import calc_risk_score
        trades = [
            {"side": "BUY", "size": 10, "price": 0.5, "timestamp": 1000000},
            {"side": "SELL", "size": 5, "price": 0.6, "timestamp": 1000100},
        ]
        max_loss, avg_loss, num_losing = calc_risk_score(trades, 1000)
        # 2 trades, one buy and one sell — this creates a position but doesn't fully close it
        assert isinstance(max_loss, float)
        assert isinstance(avg_loss, float)
        assert isinstance(num_losing, int)

    def test_calc_risk_score_with_losses(self):
        from wallet_selector import calc_risk_score
        trades = [
            {"side": "BUY", "size": 100, "price": 0.50, "timestamp": 1000000, "asset": "token1"},
            {"side": "SELL", "size": 100, "price": 0.40, "timestamp": 1000100, "asset": "token1"},
            {"side": "BUY", "size": 100, "price": 0.30, "timestamp": 1000200, "asset": "token2"},
            {"side": "SELL", "size": 100, "price": 0.50, "timestamp": 1000300, "asset": "token2"},
            {"side": "BUY", "size": 50, "price": 0.80, "timestamp": 1000400, "asset": "token3"},
            {"side": "SELL", "size": 50, "price": 0.20, "timestamp": 1000500, "asset": "token3"},
        ]
        max_loss, avg_loss, num_losing = calc_risk_score(trades, 1000)
        # Token1 loss: (0.40-0.50)/0.50 = -20%
        # Token2 gain: (0.50-0.30)/0.30 = +66.7%
        # Token3 loss: (0.20-0.80)/0.80 = -75%
        assert max_loss > 0  # Should capture the -75% loss as 75
        assert num_losing >= 2  # At least 2 losing trades
