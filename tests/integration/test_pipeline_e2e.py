"""
tests/integration/test_pipeline_e2e.py — end-to-end integration test.

Runs the full pipeline for a small set of stable tickers:
  universe (custom CSV) → fetch → evaluate → screen

Requires real internet access.
Run with:  pytest -m integration
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.engine import DCFParams, evaluate
from src.fetcher import CacheStore, fetch_universe
from src.screener import BUILTIN_PROFILES, apply_profile
from src.universe import UniverseSource, get_universe


# Small, stable, large-cap tickers with good data availability
_E2E_TICKERS = ["AAPL", "MSFT", "JNJ", "KO", "PG"]


@pytest.fixture(scope="module")
def e2e_cache(tmp_path_factory) -> CacheStore:
    db = tmp_path_factory.mktemp("e2e") / "cache.duckdb"
    return CacheStore(str(db))


@pytest.fixture(scope="module")
def custom_csv(tmp_path_factory) -> str:
    """Write a small custom ticker CSV and return its path."""
    p = tmp_path_factory.mktemp("e2e") / "tickers.csv"
    with p.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker"])
        for t in _E2E_TICKERS:
            writer.writerow([t])
    return str(p)


@pytest.mark.integration
class TestE2EPipeline:
    def test_universe_loads_custom_csv(self, custom_csv):
        tickers = get_universe(UniverseSource.CUSTOM, csv_path=custom_csv)
        assert set(_E2E_TICKERS).issubset(set(tickers))

    def test_fetch_returns_results(self, e2e_cache, custom_csv):
        tickers = get_universe(UniverseSource.CUSTOM, csv_path=custom_csv)
        results, failed = fetch_universe(
            tickers,
            cache=e2e_cache,
            max_workers=3,
            requests_per_second=1.0,
        )
        # At least 3 of 5 stable tickers should succeed
        assert len(results) >= 3

    def test_evaluate_produces_valuation_results(self, e2e_cache, custom_csv):
        tickers = get_universe(UniverseSource.CUSTOM, csv_path=custom_csv)
        ticker_data_list, _ = fetch_universe(
            tickers,
            cache=e2e_cache,
            max_workers=3,
            requests_per_second=1.0,
        )

        params = DCFParams()
        valuations = [evaluate(td, params) for td in ticker_data_list]

        assert len(valuations) == len(ticker_data_list)
        for v in valuations:
            assert v.ticker in _E2E_TICKERS
            assert v.status in ("OK", "INSUFFICIENT_DATA", "VALUE_TRAP")

    def test_valuation_result_has_required_fields(self, e2e_cache, custom_csv):
        tickers = get_universe(UniverseSource.CUSTOM, csv_path=custom_csv)
        ticker_data_list, _ = fetch_universe(
            tickers,
            cache=e2e_cache,
            max_workers=3,
            requests_per_second=1.0,
        )
        params = DCFParams()
        for td in ticker_data_list:
            v = evaluate(td, params)
            assert hasattr(v, "ticker")
            assert hasattr(v, "status")
            assert hasattr(v, "dcf_intrinsic_value")
            assert hasattr(v, "margin_of_safety_pct")

    def test_screener_applies_profile(self, e2e_cache, custom_csv):
        tickers = get_universe(UniverseSource.CUSTOM, csv_path=custom_csv)
        ticker_data_list, _ = fetch_universe(
            tickers,
            cache=e2e_cache,
            max_workers=3,
            requests_per_second=1.0,
        )
        params = DCFParams()
        valuations = [evaluate(td, params) for td in ticker_data_list]

        # Use a very relaxed profile to avoid filtering out all results
        from src.screener import ScreenerProfile
        relaxed = ScreenerProfile(
            name="relaxed",
            max_pe=100.0,
            max_pb=50.0,
            max_ev_ebitda=50.0,
            max_p_fcf=100.0,
            max_net_debt_ebitda=10.0,
            min_margin_of_safety_pct=None,
        )

        df = apply_profile(valuations, relaxed)
        # DataFrame must have the correct columns
        assert "Ticker" in df.columns
        assert "MoS%" in df.columns
        assert "DCF Avg" in df.columns
