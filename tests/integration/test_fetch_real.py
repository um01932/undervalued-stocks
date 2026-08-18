"""
tests/integration/test_fetch_real.py — integration tests for fetcher.py.

These tests make REAL yfinance API calls and require internet access.
Run with:  pytest -m integration

A small, stable set of tickers is used (AAPL, MSFT, NESN.SW) to keep
the run time short while covering US and international tickers.
"""

from __future__ import annotations

import pytest

from src.fetcher import CacheStore, TickerData, fetch_ticker, fetch_universe


STABLE_TICKERS = ["AAPL", "MSFT", "NESN.SW"]


@pytest.fixture(scope="module")
def real_cache(tmp_path_factory) -> CacheStore:
    db = tmp_path_factory.mktemp("integration") / "cache.duckdb"
    return CacheStore(str(db))


@pytest.mark.integration
class TestRealFetch:
    def test_fetch_aapl(self, real_cache):
        result = fetch_ticker("AAPL", real_cache)
        assert result is not None
        assert result.ticker == "AAPL"
        assert result.info.get("short_name") is not None or result.info.get("current_price") is not None

    def test_fetch_msft(self, real_cache):
        result = fetch_ticker("MSFT", real_cache)
        assert result is not None
        assert result.ticker == "MSFT"

    def test_fetch_international_nesn(self, real_cache):
        result = fetch_ticker("NESN.SW", real_cache)
        # NESN.SW might return limited data; just assert no crash
        # result can be None if yfinance has no data for this ticker
        assert isinstance(result, (TickerData, type(None)))

    def test_cache_populated_after_fetch(self, real_cache):
        fetch_ticker("AAPL", real_cache)
        cached = real_cache.get_info("AAPL")
        assert cached is not None

    def test_fetch_universe_batch(self, real_cache):
        results, failed = fetch_universe(
            STABLE_TICKERS,
            cache=real_cache,
            max_workers=2,
            requests_per_second=1.0,
        )
        # At least AAPL and MSFT should succeed
        tickers_ok = {r.ticker for r in results}
        assert "AAPL" in tickers_ok or "MSFT" in tickers_ok

    def test_cashflow_has_periods(self, real_cache):
        result = fetch_ticker("AAPL", real_cache)
        if result is not None and result.cashflow:
            # Each row must have a period_date key
            for row in result.cashflow:
                assert "period_date" in row
                assert "free_cash_flow" in row
