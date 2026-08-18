"""
tests/unit/test_backtester.py — unit tests for src/backtester.py.

All tests use mocks; no real yfinance calls.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.backtester import (
    AnnualRow,
    BacktestResult,
    LIMITATIONS,
    _cagr,
    _max_drawdown,
    _sharpe,
    _sortino,
    run_backtest,
)
from src.engine import DCFParams
from src.screener import BUILTIN_PROFILES


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_cache(tmp_path):
    from src.fetcher import CacheStore
    return CacheStore(str(tmp_path / "test_cache.duckdb"))


def _minimal_ticker_data(ticker: str) -> dict[str, Any]:
    """Return a minimal but valid info payload for a ticker."""
    return {
        "current_price": 100.0,
        "market_cap": 10e9,
        "trailing_pe": 12.0,
        "price_to_book": 1.2,
        "enterprise_to_ebitda": 7.0,
        "peg_ratio": 0.8,
        "free_cashflow": 1e9,
        "total_debt": 500e6,
        "total_cash": 200e6,
        "ebitda": 1.5e9,
        "shares_outstanding": 100e6,
        "short_name": f"Company {ticker}",
        "sector": "Technology",
        "industry": "Software",
        "week52_low": 80.0,
        "week52_high": 120.0,
        "dividend_yield": 0.01,
        "dividend_rate": 1.0,
        "beta": 1.0,
        "roe": 0.15,
        "roa": 0.08,
        "gross_margin": 0.60,
        "operating_margin": 0.20,
    }


def _populate_cache(cache, tickers):
    """Populate cache with minimal valid data for each ticker."""
    cf_rows = [
        {"period_date": "2023-12-31", "operating_cashflow": 1.2e9, "capital_expenditure": -0.2e9, "free_cash_flow": 1.0e9},
        {"period_date": "2022-12-31", "operating_cashflow": 1.1e9, "capital_expenditure": -0.1e9, "free_cash_flow": 1.0e9},
        {"period_date": "2021-12-31", "operating_cashflow": 1.0e9, "capital_expenditure": -0.1e9, "free_cash_flow": 0.9e9},
    ]
    fin_rows = [
        {"period_date": "2023-12-31", "total_revenue": 5e9, "gross_profit": 3e9, "ebit": 1.5e9, "net_income": 1.0e9},
        {"period_date": "2022-12-31", "total_revenue": 4.5e9, "gross_profit": 2.7e9, "ebit": 1.3e9, "net_income": 0.9e9},
        {"period_date": "2021-12-31", "total_revenue": 4e9, "gross_profit": 2.4e9, "ebit": 1.1e9, "net_income": 0.8e9},
    ]
    bs_rows = [
        {"period_date": "2023-12-31", "total_assets": 8e9, "total_liabilities": 4e9,
         "total_debt": 0.5e9, "total_cash": 0.2e9, "stockholders_equity": 4e9},
        {"period_date": "2022-12-31", "total_assets": 7e9, "total_liabilities": 3.5e9,
         "total_debt": 0.5e9, "total_cash": 0.2e9, "stockholders_equity": 3.5e9},
        {"period_date": "2021-12-31", "total_assets": 6e9, "total_liabilities": 3e9,
         "total_debt": 0.5e9, "total_cash": 0.2e9, "stockholders_equity": 3e9},
    ]
    for ticker in tickers:
        cache.set_info(ticker, _minimal_ticker_data(ticker))
        cache.set_financials(ticker, "cashflow", cf_rows)
        cache.set_financials(ticker, "financials", fin_rows)
        cache.set_financials(ticker, "balance_sheet", bs_rows)


# ── Pydantic model tests ───────────────────────────────────────────────────────

class TestAnnualRowModel:
    def test_create_valid(self):
        row = AnnualRow(
            year=2020,
            selected_tickers=["AAPL", "MSFT"],
            portfolio_return=0.15,
            benchmark_return=0.10,
            excess_return=0.05,
            winning_picks=1,
            total_picks=2,
        )
        assert row.year == 2020
        assert row.portfolio_return == pytest.approx(0.15)
        assert row.excess_return == pytest.approx(0.05)

    def test_selected_tickers_is_list(self):
        row = AnnualRow(
            year=2021, selected_tickers=["X"],
            portfolio_return=0.0, benchmark_return=0.0,
            excess_return=0.0, winning_picks=0, total_picks=1,
        )
        assert isinstance(row.selected_tickers, list)


class TestBacktestResultModel:
    def _make_result(self, **kwargs):
        defaults = dict(
            profile_name="deep_value",
            start_year=2018,
            end_year=2024,
            top_n=10,
            annual_rows=[],
            cagr_portfolio=0.12,
            cagr_benchmark=0.10,
            sharpe_ratio=0.8,
            sortino_ratio=1.2,
            max_drawdown=-0.15,
            win_rate=0.60,
            total_picks=70,
        )
        defaults.update(kwargs)
        return BacktestResult(**defaults)

    def test_create_valid(self):
        result = self._make_result()
        assert result.profile_name == "deep_value"
        assert result.cagr_portfolio == pytest.approx(0.12)
        assert result.win_rate == pytest.approx(0.60)

    def test_sharpe_can_be_none(self):
        result = self._make_result(sharpe_ratio=None)
        assert result.sharpe_ratio is None

    def test_sortino_can_be_none(self):
        result = self._make_result(sortino_ratio=None)
        assert result.sortino_ratio is None

    def test_annual_rows_list(self):
        row = AnnualRow(
            year=2020, selected_tickers=["A"],
            portfolio_return=0.1, benchmark_return=0.08,
            excess_return=0.02, winning_picks=1, total_picks=1,
        )
        result = self._make_result(annual_rows=[row])
        assert len(result.annual_rows) == 1
        assert result.annual_rows[0].year == 2020


# ── CAGR tests ─────────────────────────────────────────────────────────────────

class TestComputeCAGR:
    def test_zero_growth(self):
        assert _cagr(1.0, 1.0, 5) == pytest.approx(0.0)

    def test_known_value(self):
        # 100 → 200 in 10 years → CAGR = 2^(1/10) - 1 ≈ 7.18%
        cagr = _cagr(100.0, 200.0, 10)
        assert cagr == pytest.approx(2 ** (1 / 10) - 1, rel=1e-6)

    def test_single_year(self):
        # 100 → 110 in 1 year = 10% CAGR
        assert _cagr(100.0, 110.0, 1) == pytest.approx(0.10)

    def test_negative_growth(self):
        # 100 → 80 in 2 years
        cagr = _cagr(100.0, 80.0, 2)
        assert cagr == pytest.approx((0.8) ** 0.5 - 1.0, rel=1e-6)

    def test_zero_n_years_returns_zero(self):
        assert _cagr(100.0, 200.0, 0) == pytest.approx(0.0)

    def test_zero_start_returns_zero(self):
        assert _cagr(0.0, 200.0, 5) == pytest.approx(0.0)


# ── Sharpe ratio tests ────────────────────────────────────────────────────────

class TestComputeSharpe:
    def test_known_value(self):
        # returns = [0.10, 0.12, 0.08, 0.14, 0.11], rf = 0.04
        returns = [0.10, 0.12, 0.08, 0.14, 0.11]
        rf = 0.04
        import statistics
        expected = (statistics.mean(returns) - rf) / statistics.stdev(returns)
        assert _sharpe(returns, rf) == pytest.approx(expected, rel=1e-6)

    def test_single_return_gives_none(self):
        assert _sharpe([0.10], 0.04) is None

    def test_all_same_returns_gives_none(self):
        # std dev = 0 → Sharpe undefined
        assert _sharpe([0.10, 0.10, 0.10], 0.04) is None

    def test_negative_excess_gives_negative_sharpe(self):
        returns = [0.01, 0.02, 0.01]
        sharpe = _sharpe(returns, 0.10)  # rf > mean return
        assert sharpe is not None
        assert sharpe < 0


# ── Max drawdown tests ────────────────────────────────────────────────────────

class TestComputeMaxDrawdown:
    def test_no_drawdown(self):
        # Monotonically increasing — no drawdown
        assert _max_drawdown([1.0, 1.1, 1.2, 1.3]) == pytest.approx(0.0)

    def test_known_drawdown(self):
        # Peak at 1.2, trough at 0.9 → drawdown = (0.9 - 1.2) / 1.2 = -0.25
        dd = _max_drawdown([1.0, 1.2, 0.9, 1.1])
        assert dd == pytest.approx(-0.25, rel=1e-6)

    def test_full_loss(self):
        # Drops to 0 → -100%
        dd = _max_drawdown([1.0, 0.5, 0.0])
        assert dd == pytest.approx(-1.0)

    def test_empty_list_returns_zero(self):
        assert _max_drawdown([]) == pytest.approx(0.0)

    def test_single_value(self):
        assert _max_drawdown([1.0]) == pytest.approx(0.0)

    def test_multiple_peaks(self):
        # Peak at 1.5, drop to 1.0 → -1/3
        dd = _max_drawdown([1.0, 1.5, 1.2, 1.0, 1.4])
        assert dd == pytest.approx(-1 / 3, rel=1e-5)


# ── Win rate tests (via BacktestResult model) ─────────────────────────────────

class TestComputeWinRate:
    def test_all_wins(self):
        result = BacktestResult(
            profile_name="p", start_year=2018, end_year=2020,
            top_n=3, annual_rows=[], cagr_portfolio=0.15, cagr_benchmark=0.10,
            sharpe_ratio=None, sortino_ratio=None, max_drawdown=-0.05,
            win_rate=1.0, total_picks=9,
        )
        assert result.win_rate == pytest.approx(1.0)

    def test_half_wins(self):
        result = BacktestResult(
            profile_name="p", start_year=2018, end_year=2020,
            top_n=3, annual_rows=[], cagr_portfolio=0.10, cagr_benchmark=0.10,
            sharpe_ratio=None, sortino_ratio=None, max_drawdown=-0.05,
            win_rate=0.5, total_picks=10,
        )
        assert result.win_rate == pytest.approx(0.5)

    def test_no_picks(self):
        result = BacktestResult(
            profile_name="p", start_year=2018, end_year=2020,
            top_n=3, annual_rows=[], cagr_portfolio=0.0, cagr_benchmark=0.0,
            sharpe_ratio=None, sortino_ratio=None, max_drawdown=0.0,
            win_rate=0.0, total_picks=0,
        )
        assert result.total_picks == 0
        assert result.win_rate == pytest.approx(0.0)


# ── run_backtest — empty results test ─────────────────────────────────────────

class TestRunBacktestEmptyResults:
    @patch("src.backtester.apply_profile")
    @patch("src.backtester._load_ticker_data")
    @patch("src.backtester.evaluate")
    def test_empty_profile_results_returns_gracefully(
        self, mock_evaluate, mock_load, mock_apply_profile, tmp_path
    ):
        """When profile produces 0 results, run_backtest returns a valid empty BacktestResult."""
        import pandas as pd

        cache = _make_cache(tmp_path)

        # evaluate returns a result, but apply_profile returns empty DF
        mock_load.return_value = []
        mock_evaluate.return_value = MagicMock()
        mock_apply_profile.return_value = pd.DataFrame()

        profile = BUILTIN_PROFILES["deep_value"]
        dcf_params = DCFParams()

        result = run_backtest(
            tickers=["AAPL"],
            cache=cache,
            profile=profile,
            dcf_params=dcf_params,
            start_year=2020,
            end_year=2021,
            top_n=5,
        )

        assert isinstance(result, BacktestResult)
        assert result.annual_rows == []
        assert result.cagr_portfolio == pytest.approx(0.0)
        assert result.total_picks == 0

    @patch("src.backtester.fetch_historical_prices")
    @patch("src.backtester.apply_profile")
    @patch("src.backtester._load_ticker_data")
    @patch("src.backtester.evaluate")
    def test_run_backtest_with_price_data(
        self, mock_evaluate, mock_load, mock_apply_profile, mock_fetch_prices, tmp_path
    ):
        """run_backtest with valid price data produces correct AnnualRow entries."""
        import pandas as pd

        cache = _make_cache(tmp_path)

        mock_load.return_value = []
        mock_evaluate.return_value = MagicMock()

        # Profile returns a 2-ticker DataFrame
        df = pd.DataFrame({"Ticker": ["AAPL", "MSFT"], "Score": [80.0, 75.0]})
        mock_apply_profile.return_value = df

        # Price data: AAPL +20%, MSFT +10%, benchmark +12%
        mock_fetch_prices.return_value = {
            "AAPL":  {"2020-01-02": 100.0, "2021-01-02": 120.0},
            "MSFT":  {"2020-01-02": 200.0, "2021-01-02": 220.0},
            "^GSPC": {"2020-01-02": 3000.0, "2021-01-02": 3360.0},
        }

        profile = BUILTIN_PROFILES["deep_value"]
        dcf_params = DCFParams()

        result = run_backtest(
            tickers=["AAPL", "MSFT"],
            cache=cache,
            profile=profile,
            dcf_params=dcf_params,
            start_year=2020,
            end_year=2020,
            top_n=2,
            benchmark_ticker="^GSPC",
        )

        assert len(result.annual_rows) == 1
        row = result.annual_rows[0]
        assert row.year == 2020
        # Equal-weighted: (0.20 + 0.10) / 2 = 0.15
        assert row.portfolio_return == pytest.approx(0.15, rel=1e-4)
        # Benchmark: (3360 - 3000) / 3000 = 0.12
        assert row.benchmark_return == pytest.approx(0.12, rel=1e-4)
        assert row.excess_return == pytest.approx(0.03, rel=1e-4)
        # AAPL beats benchmark (0.20 > 0.12), MSFT does not (0.10 < 0.12)
        assert row.winning_picks == 1
        assert row.total_picks == 2

    @patch("src.backtester.fetch_historical_prices")
    @patch("src.backtester.apply_profile")
    @patch("src.backtester._load_ticker_data")
    @patch("src.backtester.evaluate")
    def test_cagr_and_metrics_computed(
        self, mock_evaluate, mock_load, mock_apply_profile, mock_fetch_prices, tmp_path
    ):
        """run_backtest computes CAGR / Sharpe / max_drawdown from multi-year data."""
        import pandas as pd

        cache = _make_cache(tmp_path)
        mock_load.return_value = []
        mock_evaluate.return_value = MagicMock()

        df = pd.DataFrame({"Ticker": ["AAPL"], "Score": [80.0]})
        mock_apply_profile.return_value = df

        # 3 years: +10%, -5%, +20%
        mock_fetch_prices.return_value = {
            "AAPL":  {
                "2018-01-02": 100.0, "2019-01-02": 110.0,
                "2020-01-02": 104.5, "2021-01-02": 125.4,
            },
            "^GSPC": {
                "2018-01-02": 2700.0, "2019-01-02": 2485.0,  # -8%
                "2020-01-02": 3230.0, "2021-01-02": 3756.0,  # +16.3%
            },
        }

        profile = BUILTIN_PROFILES["deep_value"]
        dcf_params = DCFParams()

        result = run_backtest(
            tickers=["AAPL"],
            cache=cache,
            profile=profile,
            dcf_params=dcf_params,
            start_year=2018,
            end_year=2020,
            top_n=1,
        )

        assert len(result.annual_rows) == 3
        # CAGR should be computed (non-zero)
        assert result.cagr_portfolio != 0.0
        # max_drawdown should be <= 0
        assert result.max_drawdown <= 0.0
        # total_picks = 3 years × 1 ticker = 3
        assert result.total_picks == 3


# ── LIMITATIONS constant ──────────────────────────────────────────────────────

class TestLimitationsConstant:
    def test_is_string(self):
        assert isinstance(LIMITATIONS, str)

    def test_mentions_look_ahead_bias(self):
        assert "Look-ahead bias" in LIMITATIONS or "look-ahead bias" in LIMITATIONS.lower()

    def test_mentions_survivorship_bias(self):
        assert "survivorship" in LIMITATIONS.lower()

    def test_mentions_transaction_costs(self):
        assert "transaction" in LIMITATIONS.lower()
