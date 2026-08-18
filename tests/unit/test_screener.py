"""
tests/unit/test_screener.py — unit tests for src/screener.py.

Uses synthetic ValuationResult fixtures; no real network calls or filesystem I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from src.engine import ValuationResult
from src.screener import (
    BUILTIN_PROFILES,
    ScreenerProfile,
    apply_profile,
    load_profiles,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_result(
    ticker: str = "TEST",
    status: str = "OK",
    pe_ratio: Optional[float] = 12.0,
    pb_ratio: Optional[float] = 1.2,
    ev_ebitda: Optional[float] = 7.0,
    p_fcf: Optional[float] = 10.0,
    net_debt_ebitda: Optional[float] = 1.0,
    margin_of_safety_pct: Optional[float] = 30.0,
    current_price: float = 100.0,
    dcf_intrinsic_value: float = 142.86,
    sector: str = "Finance",
    industry: str = "Banking",
    company_name: str = "Test Corp",
) -> ValuationResult:
    return ValuationResult(
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        industry=industry,
        current_price=current_price,
        market_cap=10e9,
        pe_ratio=pe_ratio,
        pb_ratio=pb_ratio,
        ev_ebitda=ev_ebitda,
        p_fcf=p_fcf,
        net_debt_ebitda=net_debt_ebitda,
        dcf_ggm_intrinsic=150.0,
        dcf_exit_intrinsic=135.72,
        dcf_intrinsic_value=dcf_intrinsic_value,
        margin_of_safety_pct=margin_of_safety_pct,
        status=status,
    )


DEEP_VALUE_PROFILE = BUILTIN_PROFILES["deep_value"]


# ── BUILTIN_PROFILES ──────────────────────────────────────────────────────────

class TestBuiltinProfiles:
    def test_all_three_profiles_exist(self):
        assert "deep_value" in BUILTIN_PROFILES
        assert "buffett_quality" in BUILTIN_PROFILES
        assert "high_fcf_yield" in BUILTIN_PROFILES

    def test_deep_value_thresholds(self):
        p = BUILTIN_PROFILES["deep_value"]
        assert p.max_pe == 15.0
        assert p.max_pb == 1.5
        assert p.min_margin_of_safety_pct == 20.0

    def test_buffett_quality_thresholds(self):
        p = BUILTIN_PROFILES["buffett_quality"]
        assert p.max_pe == 25.0
        assert p.max_net_debt_ebitda == 1.5

    def test_high_fcf_yield_p_fcf(self):
        p = BUILTIN_PROFILES["high_fcf_yield"]
        assert p.max_p_fcf == 12.0


# ── load_profiles ─────────────────────────────────────────────────────────────

class TestLoadProfiles:
    def test_returns_builtin_when_no_file(self, tmp_path):
        profiles = load_profiles(str(tmp_path / "nonexistent.yaml"))
        assert "deep_value" in profiles
        assert "buffett_quality" in profiles

    def test_yaml_overrides_single_field(self, tmp_path):
        yaml_file = tmp_path / "overrides.yaml"
        yaml_file.write_text("deep_value:\n  max_pe: 20\n")
        profiles = load_profiles(str(yaml_file))
        assert profiles["deep_value"].max_pe == 20.0
        # Other fields unchanged
        assert profiles["deep_value"].max_pb == 1.5

    def test_yaml_adds_new_profile(self, tmp_path):
        yaml_file = tmp_path / "overrides.yaml"
        yaml_file.write_text(
            "custom_profile:\n  name: custom_profile\n  max_pe: 10\n  min_margin_of_safety_pct: 25\n"
        )
        profiles = load_profiles(str(yaml_file))
        assert "custom_profile" in profiles
        assert profiles["custom_profile"].max_pe == 10.0

    def test_invalid_yaml_returns_builtins(self, tmp_path):
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text("[[[[not valid yaml")
        profiles = load_profiles(str(yaml_file))
        assert "deep_value" in profiles

    def test_non_mapping_yaml_returns_builtins(self, tmp_path):
        yaml_file = tmp_path / "list.yaml"
        yaml_file.write_text("- item1\n- item2\n")
        profiles = load_profiles(str(yaml_file))
        assert "deep_value" in profiles

    def test_builtin_profiles_not_mutated_between_calls(self, tmp_path):
        yaml_file = tmp_path / "overrides.yaml"
        yaml_file.write_text("deep_value:\n  max_pe: 99\n")
        load_profiles(str(yaml_file))
        # Original built-in should be unchanged
        assert BUILTIN_PROFILES["deep_value"].max_pe == 15.0


# ── apply_profile ─────────────────────────────────────────────────────────────

class TestApplyProfile:
    def test_passing_result_included(self):
        results = [_make_result()]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert len(df) == 1
        assert df.iloc[0]["Ticker"] == "TEST"

    def test_insufficient_data_excluded(self):
        results = [_make_result(status="INSUFFICIENT_DATA")]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert len(df) == 0

    def test_value_trap_excluded_by_default(self):
        results = [_make_result(status="VALUE_TRAP")]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert len(df) == 0

    def test_value_trap_included_when_profile_allows(self):
        profile = ScreenerProfile(
            name="test",
            include_value_traps=True,
            min_margin_of_safety_pct=None,
        )
        results = [_make_result(status="VALUE_TRAP", margin_of_safety_pct=None)]
        df = apply_profile(results, profile)
        assert len(df) == 1

    def test_pe_filter_excludes_high_pe(self):
        results = [_make_result(pe_ratio=30.0)]  # deep_value max_pe = 15
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert len(df) == 0

    def test_min_mos_filter(self):
        results = [_make_result(margin_of_safety_pct=10.0)]  # deep_value min_mos = 20
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert len(df) == 0

    def test_none_multiple_passes_filter(self):
        # Missing PE should not disqualify the company
        results = [_make_result(pe_ratio=None)]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert len(df) == 1

    def test_sorted_by_mos_descending(self):
        results = [
            _make_result(ticker="A", margin_of_safety_pct=25.0),
            _make_result(ticker="B", margin_of_safety_pct=35.0),
            _make_result(ticker="C", margin_of_safety_pct=21.0),
        ]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        assert df.iloc[0]["Ticker"] == "B"
        assert df.iloc[1]["Ticker"] == "A"
        assert df.iloc[2]["Ticker"] == "C"

    def test_empty_results_returns_empty_df(self):
        df = apply_profile([], DEEP_VALUE_PROFILE)
        assert df.empty
        assert "Ticker" in df.columns

    def test_output_has_all_required_columns(self):
        results = [_make_result()]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        required = ["Ticker", "Company", "Sector", "Industry", "Price", "MoS%",
                    "P/E", "P/B", "EV/EBITDA", "P/FCF", "NetDebt/EBITDA",
                    "DCF GGM", "DCF Exit", "DCF Avg"]
        for col in required:
            assert col in df.columns

    def test_multiple_results_filtered_and_ranked(self):
        results = [
            _make_result(ticker="CHEAP", pe_ratio=10.0, margin_of_safety_pct=40.0),
            _make_result(ticker="FAIR",  pe_ratio=13.0, margin_of_safety_pct=22.0),
            _make_result(ticker="PRICEY", pe_ratio=20.0, margin_of_safety_pct=30.0),  # fails PE filter
        ]
        df = apply_profile(results, DEEP_VALUE_PROFILE)
        tickers = df["Ticker"].tolist()
        assert "PRICEY" not in tickers
        assert tickers[0] == "CHEAP"
        assert tickers[1] == "FAIR"
