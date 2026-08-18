"""
tests/unit/test_fetcher.py — unit tests for src/fetcher.py.

All tests use mocks; no real yfinance calls, no real filesystem I/O.
DuckDB is used in-memory (':memory:') for isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.fetcher import (
    CacheStore,
    TickerData,
    _df_to_cashflow_rows,
    _df_to_financials_rows,
    _df_to_balance_sheet_rows,
    _extract_info,
    _safe_float,
    fetch_ticker,
    FINANCIALS_TTL,
    INFO_TTL,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def cache(tmp_path) -> CacheStore:
    """In-memory DuckDB CacheStore for each test."""
    return CacheStore(str(tmp_path / "test_cache.duckdb"))


def _make_info_payload() -> dict[str, Any]:
    return {
        "current_price": 150.0,
        "market_cap": 2_400_000_000_000.0,
        "trailing_pe": 28.5,
        "price_to_book": 45.0,
        "enterprise_to_ebitda": 22.0,
        "peg_ratio": 2.1,
        "free_cashflow": 90_000_000_000.0,
        "total_debt": 110_000_000_000.0,
        "total_cash": 50_000_000_000.0,
        "ebitda": 130_000_000_000.0,
        "shares_outstanding": 16_000_000_000.0,
        "short_name": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }


def _make_cf_rows() -> list[dict[str, Any]]:
    return [
        {"period_date": "2023-09-30", "operating_cashflow": 110e9, "capital_expenditure": -11e9, "free_cash_flow": 99e9},
        {"period_date": "2022-09-30", "operating_cashflow": 100e9, "capital_expenditure": -10e9, "free_cash_flow": 90e9},
        {"period_date": "2021-09-30", "operating_cashflow": 90e9,  "capital_expenditure": -9e9,  "free_cash_flow": 81e9},
    ]


# ── _safe_float ───────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_int(self):
        assert _safe_float(42) == 42.0

    def test_float(self):
        assert _safe_float(3.14) == pytest.approx(3.14)

    def test_none_returns_none(self):
        assert _safe_float(None) is None

    def test_string_number(self):
        assert _safe_float("123.45") == pytest.approx(123.45)

    def test_nan_returns_none(self):
        import math
        assert _safe_float(float("nan")) is None

    def test_inf_returns_none(self):
        assert _safe_float(float("inf")) is None

    def test_non_numeric_string(self):
        assert _safe_float("N/A") is None


# ── _extract_info ─────────────────────────────────────────────────────────────

class TestExtractInfo:
    def test_maps_keys(self):
        raw = {"currentPrice": 100.0, "shortName": "Test Corp", "sector": "Tech",
               "industry": "Software", "marketCap": 1e12}
        result = _extract_info(raw)
        assert result["current_price"] == 100.0
        assert result["short_name"] == "Test Corp"
        assert result["sector"] == "Tech"
        assert result["market_cap"] == 1e12

    def test_missing_keys_return_none(self):
        result = _extract_info({})
        assert result["current_price"] is None
        assert result["trailing_pe"] is None


# ── _df_to_cashflow_rows ──────────────────────────────────────────────────────

class TestDfToCashflowRows:
    def _make_cf_df(self) -> pd.DataFrame:
        dates = pd.to_datetime(["2023-09-30", "2022-09-30", "2021-09-30"])
        return pd.DataFrame(
            {
                "Operating Cash Flow": [110e9, 100e9, 90e9],
                "Capital Expenditure": [-11e9, -10e9, -9e9],
                "Free Cash Flow": [99e9, 90e9, 81e9],
            },
            index=dates,
        )

    def test_returns_correct_number_of_rows(self):
        rows = _df_to_cashflow_rows(self._make_cf_df())
        assert len(rows) == 3

    def test_free_cash_flow_extracted(self):
        rows = _df_to_cashflow_rows(self._make_cf_df())
        assert rows[0]["free_cash_flow"] == pytest.approx(99e9)

    def test_period_date_format(self):
        rows = _df_to_cashflow_rows(self._make_cf_df())
        assert rows[0]["period_date"] == "2023-09-30"

    def test_empty_df_returns_empty_list(self):
        assert _df_to_cashflow_rows(pd.DataFrame()) == []

    def test_derives_fcf_when_no_explicit_row(self):
        dates = pd.to_datetime(["2023-09-30"])
        df = pd.DataFrame(
            {"Operating Cash Flow": [100e9], "Capital Expenditure": [-10e9]},
            index=dates,
        )
        rows = _df_to_cashflow_rows(df)
        assert rows[0]["free_cash_flow"] == pytest.approx(90e9)


# ── _df_to_financials_rows ────────────────────────────────────────────────────

class TestDfToFinancialsRows:
    def _make_fin_df(self) -> pd.DataFrame:
        dates = pd.to_datetime(["2023-09-30", "2022-09-30"])
        return pd.DataFrame(
            {"Total Revenue": [400e9, 380e9], "Gross Profit": [170e9, 160e9],
             "EBIT": [120e9, 110e9], "Net Income": [100e9, 95e9]},
            index=dates,
        )

    def test_correct_rows(self):
        rows = _df_to_financials_rows(self._make_fin_df())
        assert len(rows) == 2
        assert rows[0]["total_revenue"] == pytest.approx(400e9)
        assert rows[0]["net_income"] == pytest.approx(100e9)


# ── _df_to_balance_sheet_rows ─────────────────────────────────────────────────

class TestDfToBalanceSheetRows:
    def _make_bs_df(self) -> pd.DataFrame:
        dates = pd.to_datetime(["2023-09-30"])
        return pd.DataFrame(
            {"Total Assets": [350e9], "Total Liabilities Net Minority Interest": [250e9],
             "Total Debt": [110e9], "Cash And Cash Equivalents": [50e9],
             "Stockholders Equity": [100e9]},
            index=dates,
        )

    def test_correct_mapping(self):
        rows = _df_to_balance_sheet_rows(self._make_bs_df())
        assert len(rows) == 1
        assert rows[0]["total_assets"] == pytest.approx(350e9)
        assert rows[0]["stockholders_equity"] == pytest.approx(100e9)


# ── CacheStore ────────────────────────────────────────────────────────────────

class TestCacheStore:
    def test_set_and_get_info_within_ttl(self, cache):
        payload = _make_info_payload()
        cache.set_info("AAPL", payload)
        result = cache.get_info("AAPL")
        assert result is not None
        assert result["current_price"] == pytest.approx(150.0)
        assert result["short_name"] == "Apple Inc."

    def test_get_info_returns_none_for_missing(self, cache):
        assert cache.get_info("MISSING") is None

    def test_get_info_returns_none_when_expired(self, cache):
        payload = _make_info_payload()
        cache.set_info("AAPL", payload)

        # Manually backdate the fetched_at timestamp (naive UTC — matches DB storage)
        expired = datetime.now(UTC).replace(tzinfo=None) - INFO_TTL - timedelta(seconds=1)
        cache._conn().execute(
            "UPDATE ticker_info SET fetched_at = ? WHERE ticker = 'AAPL'",
            [expired],
        )
        assert cache.get_info("AAPL") is None

    def test_set_financials_and_get_cashflow(self, cache):
        rows = _make_cf_rows()
        cache.set_financials("AAPL", "cashflow", rows)
        result = cache.get_financials("AAPL", "cashflow")
        assert result is not None
        assert len(result) == 3
        dates = {r["period_date"] for r in result}
        assert "2023-09-30" in dates

    def test_get_financials_returns_none_for_missing(self, cache):
        assert cache.get_financials("MISSING", "cashflow") is None

    def test_get_financials_returns_none_when_expired(self, cache):
        rows = _make_cf_rows()
        cache.set_financials("AAPL", "cashflow", rows)
        # Naive UTC — matches DB storage
        expired = datetime.now(UTC).replace(tzinfo=None) - FINANCIALS_TTL - timedelta(seconds=1)
        cache._conn().execute(
            "UPDATE cashflow SET fetched_at = ? WHERE ticker = 'AAPL'",
            [expired],
        )
        assert cache.get_financials("AAPL", "cashflow") is None

    def test_upsert_overwrites_existing(self, cache):
        cache.set_info("AAPL", _make_info_payload())
        updated = _make_info_payload()
        updated["current_price"] = 200.0
        cache.set_info("AAPL", updated)
        result = cache.get_info("AAPL")
        assert result["current_price"] == pytest.approx(200.0)

    def test_invalid_statement_raises(self, cache):
        with pytest.raises(ValueError):
            cache.get_financials("AAPL", "invalid_table")
        with pytest.raises(ValueError):
            cache.set_financials("AAPL", "invalid_table", [])


# ── fetch_ticker ──────────────────────────────────────────────────────────────

class TestFetchTicker:
    def _make_yf_ticker_mock(self) -> MagicMock:
        """Build a realistic yfinance.Ticker mock."""
        mock = MagicMock()
        mock.info = {
            "currentPrice": 150.0, "marketCap": 2.4e12,
            "trailingPE": 28.5, "priceToBook": 45.0,
            "enterpriseToEbitda": 22.0, "pegRatio": 2.1,
            "freeCashflow": 90e9, "totalDebt": 110e9,
            "totalCash": 50e9, "ebitda": 130e9,
            "sharesOutstanding": 16e9, "shortName": "Apple Inc.",
            "sector": "Technology", "industry": "Consumer Electronics",
        }
        dates = pd.to_datetime(["2023-09-30", "2022-09-30", "2021-09-30"])
        mock.cashflow = pd.DataFrame(
            {"Operating Cash Flow": [110e9, 100e9, 90e9],
             "Capital Expenditure": [-11e9, -10e9, -9e9],
             "Free Cash Flow": [99e9, 90e9, 81e9]},
            index=dates,
        )
        mock.financials = pd.DataFrame(
            {"Total Revenue": [400e9, 380e9, 360e9],
             "Gross Profit": [170e9, 160e9, 150e9],
             "EBIT": [120e9, 110e9, 100e9],
             "Net Income": [100e9, 95e9, 90e9]},
            index=dates,
        )
        mock.balance_sheet = pd.DataFrame(
            {"Total Assets": [350e9, 330e9, 310e9],
             "Total Liabilities Net Minority Interest": [250e9, 240e9, 230e9],
             "Total Debt": [110e9, 105e9, 100e9],
             "Cash And Cash Equivalents": [50e9, 48e9, 45e9],
             "Stockholders Equity": [100e9, 90e9, 80e9]},
            index=dates,
        )
        return mock

    @patch("src.fetcher.yf.Ticker")
    def test_successful_fetch(self, mock_yf_class, cache):
        mock_yf_class.return_value = self._make_yf_ticker_mock()
        result = fetch_ticker("AAPL", cache)
        assert result is not None
        assert result.ticker == "AAPL"
        assert result.info["current_price"] == pytest.approx(150.0)
        assert len(result.cashflow) == 3

    @patch("src.fetcher.yf.Ticker")
    def test_cache_is_populated_after_fetch(self, mock_yf_class, cache):
        mock_yf_class.return_value = self._make_yf_ticker_mock()
        fetch_ticker("AAPL", cache)
        # Second call should hit cache — mock should NOT be called again
        mock_yf_class.reset_mock()
        result = fetch_ticker("AAPL", cache)
        mock_yf_class.assert_not_called()
        assert result is not None

    @patch("src.fetcher.yf.Ticker")
    def test_returns_none_on_empty_info(self, mock_yf_class, cache):
        mock = MagicMock()
        mock.info = {}
        mock.cashflow = pd.DataFrame()
        mock.financials = pd.DataFrame()
        mock.balance_sheet = pd.DataFrame()
        mock_yf_class.return_value = mock
        result = fetch_ticker("INVALID", cache)
        assert result is None

    @patch("src.fetcher.yf.Ticker")
    def test_returns_none_after_all_retries_fail(self, mock_yf_class, cache):
        mock_yf_class.side_effect = Exception("network error")
        result = fetch_ticker("FAIL", cache)
        assert result is None
