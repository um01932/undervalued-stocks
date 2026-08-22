"""
tests/unit/test_engine.py — unit tests for src/engine.py.

Uses synthetic TickerData fixtures; no real network calls.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from src.engine import (
    DCFParams,
    ValuationResult,
    _is_financial_sector,
    _is_value_trap,
    _safe_div,
    _safe_val,
    compute_dcf_ddm,
    compute_dcf_exit,
    compute_dcf_ggm,
    compute_graham_number,
    compute_multiples,
    evaluate,
)
from src.fetcher import TickerData


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_info(
    current_price: float = 150.0,
    market_cap: float = 2.4e12,
    shares_outstanding: float = 16e9,
    trailing_pe: float = 28.5,
    price_to_book: float = 45.0,
    enterprise_to_ebitda: float = 22.0,
    peg_ratio: float = 2.1,
    free_cashflow: float = 90e9,
    total_debt: float = 110e9,
    total_cash: float = 50e9,
    ebitda: float = 130e9,
    short_name: str = "Acme Corp",
    sector: str = "Technology",
    industry: str = "Software",
) -> dict[str, Any]:
    return {
        "current_price": current_price,
        "market_cap": market_cap,
        "shares_outstanding": shares_outstanding,
        "trailing_pe": trailing_pe,
        "price_to_book": price_to_book,
        "enterprise_to_ebitda": enterprise_to_ebitda,
        "peg_ratio": peg_ratio,
        "free_cashflow": free_cashflow,
        "total_debt": total_debt,
        "total_cash": total_cash,
        "ebitda": ebitda,
        "short_name": short_name,
        "sector": sector,
        "industry": industry,
    }


def _make_cf_rows(fcf_values: list[float]) -> list[dict[str, Any]]:
    dates = ["2023-09-30", "2022-09-30", "2021-09-30", "2020-09-30", "2019-09-30"]
    return [
        {"period_date": dates[i], "free_cash_flow": v, "operating_cashflow": v * 1.1, "capital_expenditure": -v * 0.1}
        for i, v in enumerate(fcf_values)
    ]


def _make_ticker_data(
    ticker: str = "TEST",
    info: dict | None = None,
    cf_values: list[float] | None = None,
) -> TickerData:
    return TickerData(
        ticker=ticker,
        info=info if info is not None else _make_info(),
        cashflow=_make_cf_rows(cf_values if cf_values is not None else [90e9, 80e9, 75e9]),
        financials=[{"period_date": "2023-09-30", "total_revenue": 400e9, "gross_profit": 170e9, "ebit": 120e9, "net_income": 100e9}],
        balance_sheet=[{"period_date": "2023-09-30", "total_assets": 350e9, "total_liabilities": 250e9, "total_debt": 110e9, "total_cash": 50e9, "stockholders_equity": 100e9}],
    )


DEFAULT_PARAMS = DCFParams()


# ── _safe_div ─────────────────────────────────────────────────────────────────

class TestSafeDiv:
    def test_normal(self):
        assert _safe_div(10.0, 2.0) == pytest.approx(5.0)

    def test_zero_denominator(self):
        assert _safe_div(10.0, 0.0) is None

    def test_none_numerator(self):
        assert _safe_div(None, 5.0) is None

    def test_none_denominator(self):
        assert _safe_div(5.0, None) is None

    def test_both_none(self):
        assert _safe_div(None, None) is None


# ── compute_multiples ─────────────────────────────────────────────────────────

class TestComputeMultiples:
    def test_basic_multiples(self):
        data = _make_ticker_data()
        m = compute_multiples(data)
        assert m["pe_ratio"] == pytest.approx(28.5)
        assert m["pb_ratio"] == pytest.approx(45.0)
        assert m["ev_ebitda"] == pytest.approx(22.0)
        assert m["peg_ratio"] == pytest.approx(2.1)

    def test_p_fcf_derived(self):
        data = _make_ticker_data(info=_make_info(market_cap=2.4e12, free_cashflow=90e9))
        m = compute_multiples(data)
        expected = 2.4e12 / 90e9
        assert m["p_fcf"] == pytest.approx(expected, rel=1e-4)

    def test_net_debt_ebitda(self):
        info = _make_info(total_debt=110e9, total_cash=50e9, ebitda=130e9)
        data = _make_ticker_data(info=info)
        m = compute_multiples(data)
        # (110 - 50) / 130 ≈ 0.4615
        assert m["net_debt_ebitda"] == pytest.approx(60e9 / 130e9, rel=1e-4)

    def test_missing_market_cap_returns_none_pfcf(self):
        info = _make_info()
        info["market_cap"] = None
        data = _make_ticker_data(info=info)
        m = compute_multiples(data)
        assert m["p_fcf"] is None

    def test_zero_ebitda_returns_none_net_debt_ebitda(self):
        info = _make_info(ebitda=0.0)
        data = _make_ticker_data(info=info)
        m = compute_multiples(data)
        assert m["net_debt_ebitda"] is None


# ── compute_dcf_ggm ───────────────────────────────────────────────────────────

class TestComputeDcfGgm:
    def test_returns_positive_value(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        result = compute_dcf_ggm(data, DEFAULT_PARAMS)
        assert result is not None
        assert result > 0

    def test_fewer_than_3_fcf_returns_none(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9])
        result = compute_dcf_ggm(data, DEFAULT_PARAMS)
        assert result is None

    def test_all_negative_fcf_returns_none(self):
        data = _make_ticker_data(cf_values=[-10e9, -20e9, -15e9])
        result = compute_dcf_ggm(data, DEFAULT_PARAMS)
        assert result is None

    def test_mixed_fcf_uses_only_positive(self):
        # 2 positive values → insufficient, returns None
        data = _make_ticker_data(cf_values=[90e9, -10e9, 80e9, -5e9])
        result = compute_dcf_ggm(data, DEFAULT_PARAMS)
        assert result is None  # only 2 positive years

    def test_no_shares_returns_none(self):
        info = _make_info()
        info["shares_outstanding"] = None
        data = _make_ticker_data(info=info, cf_values=[90e9, 80e9, 75e9])
        result = compute_dcf_ggm(data, DEFAULT_PARAMS)
        assert result is None

    def test_discount_rate_equal_to_terminal_growth_returns_none(self):
        params = DCFParams(discount_rate=0.025, terminal_growth=0.025)
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        result = compute_dcf_ggm(data, params)
        assert result is None

    def test_higher_growth_gives_higher_value(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        low_growth = compute_dcf_ggm(data, DCFParams(growth_rate=0.02))
        high_growth = compute_dcf_ggm(data, DCFParams(growth_rate=0.10))
        assert high_growth > low_growth

    def test_higher_discount_rate_gives_lower_value(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        low_discount = compute_dcf_ggm(data, DCFParams(discount_rate=0.08))
        high_discount = compute_dcf_ggm(data, DCFParams(discount_rate=0.15))
        assert high_discount < low_discount


# ── compute_dcf_exit ──────────────────────────────────────────────────────────

class TestComputeDcfExit:
    def test_returns_positive_value(self):
        data = _make_ticker_data()
        result = compute_dcf_exit(data, DEFAULT_PARAMS)
        assert result is not None
        assert result > 0

    def test_missing_ebitda_returns_none(self):
        info = _make_info()
        info["ebitda"] = None
        data = _make_ticker_data(info=info)
        result = compute_dcf_exit(data, DEFAULT_PARAMS)
        assert result is None

    def test_negative_ebitda_returns_none(self):
        data = _make_ticker_data(info=_make_info(ebitda=-5e9))
        result = compute_dcf_exit(data, DEFAULT_PARAMS)
        assert result is None

    def test_no_shares_returns_none(self):
        info = _make_info()
        info["shares_outstanding"] = None
        data = _make_ticker_data(info=info)
        result = compute_dcf_exit(data, DEFAULT_PARAMS)
        assert result is None

    def test_higher_exit_multiple_gives_higher_value(self):
        data = _make_ticker_data()
        low_mult = compute_dcf_exit(data, DCFParams(exit_multiple=8.0))
        high_mult = compute_dcf_exit(data, DCFParams(exit_multiple=15.0))
        assert high_mult > low_mult


# ── _is_value_trap ────────────────────────────────────────────────────────────

class TestIsValueTrap:
    def test_high_net_debt_ebitda_is_trap(self):
        data = _make_ticker_data()
        assert _is_value_trap(data, net_debt_ebitda=4.0) is True

    def test_low_net_debt_ebitda_is_not_trap(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        assert _is_value_trap(data, net_debt_ebitda=1.0) is False

    def test_all_negative_fcf_is_trap(self):
        data = _make_ticker_data(cf_values=[-10e9, -20e9, -15e9])
        assert _is_value_trap(data, net_debt_ebitda=1.0) is True

    def test_mixed_fcf_is_not_trap(self):
        data = _make_ticker_data(cf_values=[90e9, -10e9, 75e9])
        assert _is_value_trap(data, net_debt_ebitda=1.0) is False

    def test_none_net_debt_ebitda_not_trap_by_debt(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        assert _is_value_trap(data, net_debt_ebitda=None) is False


# ── evaluate ──────────────────────────────────────────────────────────────────

class TestEvaluate:
    def test_ok_status(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.status == "OK"
        assert result.dcf_intrinsic_value is not None
        assert result.margin_of_safety_pct is not None

    def test_insufficient_data_when_no_fcf_and_no_ebitda(self):
        info = _make_info()
        info["ebitda"] = None
        info["shares_outstanding"] = None
        data = TickerData(ticker="NONE", info=info, cashflow=[], financials=[], balance_sheet=[])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.status == "INSUFFICIENT_DATA"
        assert result.margin_of_safety_pct is None

    def test_value_trap_status(self):
        data = _make_ticker_data(cf_values=[-10e9, -20e9, -15e9])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.status == "VALUE_TRAP"

    def test_high_net_debt_value_trap(self):
        info = _make_info(total_debt=1000e9, total_cash=10e9, ebitda=100e9)
        data = _make_ticker_data(info=info, cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.status == "VALUE_TRAP"

    def test_result_fields_populated(self):
        data = _make_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.ticker == "TEST"
        assert result.company_name == "Acme Corp"
        assert result.sector == "Technology"
        assert result.pe_ratio == pytest.approx(28.5)

    def test_both_dcf_averaged(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        if result.dcf_ggm_intrinsic and result.dcf_exit_intrinsic:
            expected_avg = (result.dcf_ggm_intrinsic + result.dcf_exit_intrinsic) / 2.0
            assert result.dcf_intrinsic_value == pytest.approx(expected_avg, rel=1e-6)

    def test_margin_of_safety_formula(self):
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        if result.status == "OK" and result.dcf_intrinsic_value and result.current_price:
            expected_mos = (result.dcf_intrinsic_value - result.current_price) / result.dcf_intrinsic_value * 100
            assert result.margin_of_safety_pct == pytest.approx(expected_mos, rel=1e-6)

    def test_no_margin_of_safety_for_insufficient_data(self):
        info = _make_info()
        info["ebitda"] = None
        data = TickerData(ticker="X", info=info, cashflow=[], financials=[], balance_sheet=[])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.margin_of_safety_pct is None


# ── _is_financial_sector ──────────────────────────────────────────────────────

class TestIsFinancialSector:
    def test_financial_services_is_financial(self):
        assert _is_financial_sector("Financial Services") is True

    def test_insurance_is_financial(self):
        assert _is_financial_sector("Insurance") is True

    def test_technology_is_not_financial(self):
        assert _is_financial_sector("Technology") is False

    def test_none_is_not_financial(self):
        assert _is_financial_sector(None) is False

    def test_empty_string_is_not_financial(self):
        assert _is_financial_sector("") is False


# ── compute_dcf_ddm ───────────────────────────────────────────────────────────

class TestComputeDcfDdm:
    def test_basic_ddm_calculation(self):
        """P = D1 / (r - g), D1 = dividend_rate * (1 + g), g capped at 5%."""
        info = _make_info()
        info["dividend_rate"] = 2.0   # $2/share annual dividend
        data = _make_ticker_data(info=info)
        params = DCFParams(discount_rate=0.10, growth_rate=0.05)
        result = compute_dcf_ddm(data, params)
        # g = min(0.05, 0.05) = 0.05; D1 = 2.0 * 1.05 = 2.10; P = 2.10 / 0.05 = 42.0
        assert result == pytest.approx(42.0, rel=1e-6)

    def test_ddm_growth_rate_capped_at_5pct(self):
        """Growth > 5% should be capped at 5% for DDM stability."""
        info = _make_info()
        info["dividend_rate"] = 2.0
        data = _make_ticker_data(info=info)
        params_high = DCFParams(discount_rate=0.10, growth_rate=0.15)
        params_capped = DCFParams(discount_rate=0.10, growth_rate=0.05)
        result_high = compute_dcf_ddm(data, params_high)
        result_capped = compute_dcf_ddm(data, params_capped)
        # Both should produce the same value because the cap kicks in
        assert result_high == pytest.approx(result_capped, rel=1e-6)

    def test_ddm_no_dividend_returns_none(self):
        """No dividend → DDM returns None."""
        info = _make_info()
        info["dividend_rate"] = None
        data = _make_ticker_data(info=info)
        result = compute_dcf_ddm(data, DEFAULT_PARAMS)
        assert result is None

    def test_ddm_zero_dividend_returns_none(self):
        """Zero dividend → DDM returns None."""
        info = _make_info()
        info["dividend_rate"] = 0.0
        data = _make_ticker_data(info=info)
        result = compute_dcf_ddm(data, DEFAULT_PARAMS)
        assert result is None

    def test_ddm_negative_dividend_returns_none(self):
        """Negative dividend_rate → DDM returns None."""
        info = _make_info()
        info["dividend_rate"] = -1.0
        data = _make_ticker_data(info=info)
        result = compute_dcf_ddm(data, DEFAULT_PARAMS)
        assert result is None

    def test_ddm_returns_positive(self):
        """DDM should return a positive value for valid inputs."""
        info = _make_info()
        info["dividend_rate"] = 3.0
        data = _make_ticker_data(info=info)
        result = compute_dcf_ddm(data, DEFAULT_PARAMS)
        assert result is not None
        assert result > 0


# ── evaluate — financial sector routing ───────────────────────────────────────

class TestEvaluateFinancialSector:
    def _make_financial_data(self, dividend_rate: float | None = None) -> TickerData:
        info = _make_info(sector="Financial Services")
        info["dividend_rate"] = dividend_rate
        return _make_ticker_data(info=info, cf_values=[90e9, 80e9, 75e9])

    def test_financial_sector_skips_dcf(self):
        """Financial Services company: GGM and Exit should be None; sector_excluded=True."""
        data = self._make_financial_data(dividend_rate=None)
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.sector_excluded is True
        assert result.dcf_ggm_intrinsic is None
        assert result.dcf_exit_intrinsic is None

    def test_financial_sector_no_dividend_insufficient_data(self):
        """No dividend → DDM fails → INSUFFICIENT_DATA for financial company."""
        data = self._make_financial_data(dividend_rate=None)
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.status == "INSUFFICIENT_DATA"
        assert result.dcf_intrinsic_value is None
        assert result.dcf_model_used is None

    def test_financial_sector_with_dividend_uses_ddm(self):
        """Financial company with dividend → DDM succeeds → dcf_model_used='DDM'."""
        data = self._make_financial_data(dividend_rate=2.0)
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.dcf_model_used == "DDM"
        assert result.dcf_intrinsic_value is not None
        assert result.dcf_intrinsic_value > 0

    def test_non_financial_sector_not_excluded(self):
        """Technology company: sector_excluded=False; GGM+Exit used."""
        data = _make_ticker_data(info=_make_info(sector="Technology"), cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.sector_excluded is False
        assert result.dcf_model_used in ("GGM+Exit", "GGM", "Exit")

    def test_dcf_model_used_ggm_plus_exit_when_both_available(self):
        """Non-financial with both DCF methods → dcf_model_used='GGM+Exit'."""
        data = _make_ticker_data(cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.dcf_model_used == "GGM+Exit"

    def test_insurance_sector_also_excluded(self):
        """Insurance sector should also be treated as financial."""
        info = _make_info(sector="Insurance")
        info["dividend_rate"] = None
        data = _make_ticker_data(info=info, cf_values=[90e9, 80e9, 75e9])
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.sector_excluded is True
        assert result.dcf_ggm_intrinsic is None


# ── Fixtures for Phase 2 tests ────────────────────────────────────────────────

def _make_financials_2y() -> list[dict]:
    """Two years of income-statement data (most recent first)."""
    return [
        {
            "period_date": "2023-09-30",
            "total_revenue": 400e9,
            "gross_profit": 160e9,
            "ebit": 120e9,
            "net_income": 100e9,
        },
        {
            "period_date": "2022-09-30",
            "total_revenue": 360e9,
            "gross_profit": 130e9,   # lower gross margin → F8 fires
            "ebit": 100e9,
            "net_income": 80e9,      # lower ROA → F3 fires
        },
    ]


def _make_cashflow_2y() -> list[dict]:
    """Two years of cashflow data (most recent first)."""
    return [
        {
            "period_date": "2023-09-30",
            "operating_cashflow": 115e9,  # positive → F2 fires; > net_income → F4 fires
            "free_cash_flow": 100e9,
        },
        {
            "period_date": "2022-09-30",
            "operating_cashflow": 90e9,
            "free_cash_flow": 75e9,
        },
    ]


def _make_balance_sheet_2y() -> list[dict]:
    """Two years of balance-sheet data (most recent first)."""
    return [
        {
            "period_date": "2023-09-30",
            "total_assets": 350e9,
            "total_liabilities": 250e9,
            "total_debt": 100e9,       # lower leverage than 2022 → F5 fires
            "total_cash": 50e9,
            "stockholders_equity": 100e9,
        },
        {
            "period_date": "2022-09-30",
            "total_assets": 320e9,
            "total_liabilities": 240e9,
            "total_debt": 110e9,       # higher leverage → F5 fires
            "total_cash": 45e9,
            "stockholders_equity": 80e9,
        },
    ]


def _make_full_ticker_data(sector: str = "Technology") -> "TickerData":
    """TickerData with 2 years across all three statements."""
    return TickerData(
        ticker="FULL",
        info=_make_info(sector=sector, market_cap=2.4e12),
        cashflow=_make_cashflow_2y(),
        financials=_make_financials_2y(),
        balance_sheet=_make_balance_sheet_2y(),
    )


# ── compute_piotroski ─────────────────────────────────────────────────────────

from src.engine import compute_piotroski, compute_altman_z, compute_roic


class TestComputePiotroski:
    def test_basic_calculation_all_positive_signals(self):
        """Full fixture should score: F1+F2+F3+F4+F5+F8+F9 = 7."""
        data = _make_full_ticker_data()
        score = compute_piotroski(data)
        assert score is not None
        assert isinstance(score, int)
        # Verify each flag individually rather than hard-coding the sum,
        # but all 7 implemented signals should fire on our fixture.
        assert score == 7

    def test_insufficient_data_fewer_than_2_years(self):
        """Only 1 year of data → returns None."""
        data = TickerData(
            ticker="T",
            info=_make_info(),
            cashflow=[{"period_date": "2023-09-30", "operating_cashflow": 50e9}],
            financials=[{"period_date": "2023-09-30", "total_revenue": 100e9,
                         "gross_profit": 40e9, "ebit": 20e9, "net_income": 15e9}],
            balance_sheet=[{"period_date": "2023-09-30", "total_assets": 200e9,
                             "total_debt": 80e9, "total_cash": 20e9,
                             "stockholders_equity": 120e9, "total_liabilities": 80e9}],
        )
        assert compute_piotroski(data) is None

    def test_all_negative_profitability_scores_zero_profitability(self):
        """Negative net income → F1 and F3 award 0 pts."""
        fins = [
            {"period_date": "2023-09-30", "total_revenue": 100e9,
             "gross_profit": 20e9, "ebit": -10e9, "net_income": -5e9},
            {"period_date": "2022-09-30", "total_revenue": 110e9,
             "gross_profit": 30e9, "ebit": 10e9, "net_income": 2e9},
        ]
        cf = [
            {"period_date": "2023-09-30", "operating_cashflow": -8e9, "free_cash_flow": -10e9},
            {"period_date": "2022-09-30", "operating_cashflow": 5e9, "free_cash_flow": 4e9},
        ]
        bs = [
            {"period_date": "2023-09-30", "total_assets": 200e9, "total_debt": 80e9,
             "total_cash": 20e9, "stockholders_equity": 120e9, "total_liabilities": 80e9},
            {"period_date": "2022-09-30", "total_assets": 190e9, "total_debt": 70e9,
             "total_cash": 18e9, "stockholders_equity": 120e9, "total_liabilities": 70e9},
        ]
        data = TickerData(ticker="NEG", info=_make_info(), cashflow=cf, financials=fins, balance_sheet=bs)
        score = compute_piotroski(data)
        assert score is not None
        assert score < 4  # F1, F2, F3, F4 should all be 0 given negative income & CFO

    def test_all_positive_returns_7_max(self):
        """All 7 implemented signals fire → score == 7."""
        data = _make_full_ticker_data()
        assert compute_piotroski(data) == 7

    def test_financial_sector_returns_none(self):
        """Financial Services sector → score is always None."""
        data = _make_full_ticker_data(sector="Financial Services")
        assert compute_piotroski(data) is None

    def test_insurance_sector_returns_none(self):
        """Insurance sector → score is always None."""
        data = _make_full_ticker_data(sector="Insurance")
        assert compute_piotroski(data) is None

    def test_no_common_dates_returns_none(self):
        """Mismatched dates across statements → fewer than 2 common dates → None."""
        data = TickerData(
            ticker="T",
            info=_make_info(),
            cashflow=[{"period_date": "2023-09-30", "operating_cashflow": 50e9}],
            financials=[{"period_date": "2021-09-30", "total_revenue": 100e9,
                          "gross_profit": 40e9, "ebit": 20e9, "net_income": 15e9}],
            balance_sheet=[{"period_date": "2020-09-30", "total_assets": 200e9,
                             "total_debt": 80e9, "total_cash": 20e9,
                             "stockholders_equity": 120e9, "total_liabilities": 80e9}],
        )
        assert compute_piotroski(data) is None

    def test_score_is_integer(self):
        """Return type must be int (or None)."""
        data = _make_full_ticker_data()
        score = compute_piotroski(data)
        assert isinstance(score, int)

    def test_score_within_valid_range(self):
        """Score must be in [0, 7]."""
        data = _make_full_ticker_data()
        score = compute_piotroski(data)
        assert score is not None
        assert 0 <= score <= 7


# ── compute_altman_z ──────────────────────────────────────────────────────────

class TestComputeAltmanZ:
    def test_basic_calculation_returns_float(self):
        """With all data present the Z-score should be a finite float."""
        data = _make_full_ticker_data()
        z = compute_altman_z(data)
        assert z is not None
        assert isinstance(z, float)

    def test_basic_calculation_expected_value(self):
        """Verify formula produces the expected Z-score for known inputs."""
        data = _make_full_ticker_data()
        z = compute_altman_z(data)
        # Using most-recent BS row:
        # total_assets=350e9, total_liabilities=250e9, stockholders_equity=100e9,
        # ebit=120e9, total_revenue=400e9, market_cap=2.4e12
        x1 = (100e9 * 0.4) / 350e9
        x2 = 100e9 / 350e9
        x3 = 120e9 / 350e9
        x4 = 2.4e12 / 250e9
        x5 = 400e9 / 350e9
        expected = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
        assert z == pytest.approx(round(expected, 4), rel=1e-4)

    def test_financial_sector_returns_none(self):
        """Financial Services → None."""
        data = _make_full_ticker_data(sector="Financial Services")
        assert compute_altman_z(data) is None

    def test_missing_market_cap_returns_none(self):
        """Missing market_cap → None."""
        data = _make_full_ticker_data()
        data = TickerData(
            ticker=data.ticker,
            info={**data.info, "market_cap": None},
            cashflow=data.cashflow,
            financials=data.financials,
            balance_sheet=data.balance_sheet,
        )
        assert compute_altman_z(data) is None

    def test_missing_balance_sheet_returns_none(self):
        """Empty balance sheet → None."""
        data = TickerData(
            ticker="T",
            info=_make_info(market_cap=2.4e12),
            cashflow=_make_cashflow_2y(),
            financials=_make_financials_2y(),
            balance_sheet=[],
        )
        assert compute_altman_z(data) is None

    def test_safe_zone_high_z(self):
        """Strong company should land in safe zone (Z > 2.99)."""
        data = _make_full_ticker_data()
        z = compute_altman_z(data)
        assert z is not None
        assert z > 2.99, f"Expected safe zone (>2.99), got {z}"


# ── compute_roic ──────────────────────────────────────────────────────────────

class TestComputeRoic:
    def test_basic_calculation(self):
        """ROIC = NOPAT / (total_assets - total_cash)."""
        data = _make_full_ticker_data()
        roic = compute_roic(data)
        # ebit=120e9, total_assets=350e9, total_cash=50e9
        expected = (120e9 * 0.79) / (350e9 - 50e9)
        assert roic == pytest.approx(expected, rel=1e-6)

    def test_financial_sector_returns_none(self):
        """Financial Services → None."""
        data = _make_full_ticker_data(sector="Financial Services")
        assert compute_roic(data) is None

    def test_negative_ebit_returns_negative_roic(self):
        """Negative EBIT should produce a negative ROIC (not None)."""
        fins = [{"period_date": "2023-09-30", "total_revenue": 100e9,
                 "gross_profit": 20e9, "ebit": -30e9, "net_income": -25e9}]
        bs = [{"period_date": "2023-09-30", "total_assets": 200e9,
               "total_debt": 80e9, "total_cash": 20e9,
               "stockholders_equity": 120e9, "total_liabilities": 80e9}]
        data = TickerData(ticker="NEG", info=_make_info(), cashflow=[], financials=fins, balance_sheet=bs)
        roic = compute_roic(data)
        assert roic is not None
        assert roic < 0

    def test_missing_financials_returns_none(self):
        """No income statement data → None."""
        data = TickerData(
            ticker="T",
            info=_make_info(),
            cashflow=[],
            financials=[],
            balance_sheet=_make_balance_sheet_2y(),
        )
        assert compute_roic(data) is None

    def test_missing_balance_sheet_returns_none(self):
        """No balance sheet → None."""
        data = TickerData(
            ticker="T",
            info=_make_info(),
            cashflow=[],
            financials=_make_financials_2y(),
            balance_sheet=[],
        )
        assert compute_roic(data) is None

    def test_result_is_decimal(self):
        """ROIC should be returned as a decimal (e.g. 0.15 not 15)."""
        data = _make_full_ticker_data()
        roic = compute_roic(data)
        assert roic is not None
        # For strong companies ROIC decimal should be well below 10
        assert abs(roic) < 10, "ROIC should be a decimal fraction, not a percentage"


# ── evaluate — quality fields populated ───────────────────────────────────────

class TestEvaluateQualityFields:
    def test_evaluate_populates_piotroski(self):
        """evaluate() should populate piotroski_score for non-financial companies."""
        data = _make_full_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.piotroski_score is not None
        assert isinstance(result.piotroski_score, int)

    def test_evaluate_populates_altman_z(self):
        """evaluate() should populate altman_z for non-financial companies."""
        data = _make_full_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.altman_z is not None

    def test_evaluate_populates_roic(self):
        """evaluate() should populate roic for non-financial companies."""
        data = _make_full_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.roic is not None

    def test_evaluate_populates_composite_score(self):
        """evaluate() should populate composite_score."""
        data = _make_full_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.composite_score is not None
        assert 0 <= result.composite_score <= 100

    def test_financial_sector_quality_fields_none(self):
        """Financial sector → piotroski, altman_z, roic all None."""
        data = _make_full_ticker_data(sector="Financial Services")
        data = TickerData(
            ticker=data.ticker,
            info={**data.info, "dividend_rate": None},
            cashflow=data.cashflow,
            financials=data.financials,
            balance_sheet=data.balance_sheet,
        )
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.piotroski_score is None
        assert result.altman_z is None
        assert result.roic is None


# ── compute_composite_score ───────────────────────────────────────────────────

from src.screener import compute_composite_score


class TestComputeCompositeScore:
    def _make_vr(self, **kwargs) -> ValuationResult:
        defaults = dict(
            ticker="T",
            status="OK",
            margin_of_safety_pct=30.0,
            roic=0.20,              # 20% = full quality marks
            piotroski_score=7,      # full health marks
            price_vs_52w_low_pct=0.0,  # at 52w low → full momentum marks
        )
        defaults.update(kwargs)
        return ValuationResult(**defaults)

    def test_perfect_score_is_100(self):
        """MoS=100%, ROIC=20%, Piotroski=7, 52w position=0% → score ≈ 100."""
        vr = self._make_vr(margin_of_safety_pct=100.0)
        score = compute_composite_score(vr)
        assert score == pytest.approx(100.0, abs=0.1)

    def test_all_neutral_when_data_missing(self):
        """All data missing → neutral score = 0 (val) + 12.5 + 12.5 + 10 = 35.0."""
        vr = ValuationResult(ticker="T", status="INSUFFICIENT_DATA")
        score = compute_composite_score(vr)
        assert score == pytest.approx(35.0, abs=0.1)

    def test_valuation_pillar(self):
        """30% weight on MoS%: MoS=50% → val_score=15."""
        vr = self._make_vr(margin_of_safety_pct=50.0, roic=None, piotroski_score=None, price_vs_52w_low_pct=None)
        score = compute_composite_score(vr)
        expected = 50.0 * 0.30 + 12.5 + 12.5 + 10.0
        assert score == pytest.approx(expected, abs=0.1)

    def test_quality_pillar_capped_at_20pct_roic(self):
        """ROIC > 20% should be capped at 25 pts."""
        vr = self._make_vr(roic=0.50)  # 50% ROIC — should still cap at 25
        score_high = compute_composite_score(vr)
        vr_cap = self._make_vr(roic=0.20)
        score_cap = compute_composite_score(vr_cap)
        assert score_high == score_cap

    def test_score_bounded_0_to_100(self):
        """Score should always be in [0, 100]."""
        vr = self._make_vr(margin_of_safety_pct=200.0, roic=1.0, piotroski_score=7, price_vs_52w_low_pct=0.0)
        score = compute_composite_score(vr)
        assert 0 <= score <= 100

    def test_higher_mos_gives_higher_score(self):
        """Higher MoS% should yield a strictly higher composite score."""
        low_mos = self._make_vr(margin_of_safety_pct=20.0)
        high_mos = self._make_vr(margin_of_safety_pct=60.0)
        assert compute_composite_score(high_mos) > compute_composite_score(low_mos)


# ── Phase 3: compute_wacc ────────────────────────────────────────────────────

from src.engine import compute_wacc, compute_sustainable_growth


class TestComputeWacc:
    def _make_data(self, beta=1.0, market_cap=2.4e12, total_debt=110e9, total_cash=50e9) -> "TickerData":
        info = _make_info(
            market_cap=market_cap,
            total_debt=total_debt,
            total_cash=total_cash,
        )
        info["beta"] = beta
        return _make_ticker_data(info=info)

    def test_basic_calculation(self):
        """With known inputs, WACC should match manual formula."""
        # beta=1.0, rf=0.045, ERP=0.055 → ke = 0.10
        # E=2400B, D=max(110-50,0)=60B, V=2460B
        # kd=0.05, T=0.21
        # WACC = (2400/2460)*0.10 + (60/2460)*0.05*0.79
        data = self._make_data(beta=1.0, market_cap=2400e9, total_debt=110e9, total_cash=50e9)
        params = DCFParams()
        wacc = compute_wacc(data, rf_rate=0.045, params=params)
        E, D = 2400e9, 60e9
        V = E + D
        ke = 0.045 + 1.0 * 0.055
        kd = 0.05
        expected = (E / V) * ke + (D / V) * kd * 0.79
        assert wacc == pytest.approx(expected, rel=1e-6)

    def test_clamp_floor_at_6pct(self):
        """Very low beta / all equity → result should not go below 6%."""
        data = self._make_data(beta=0.0, market_cap=1e12, total_debt=0.0, total_cash=0.0)
        # ke = 0.01 + 0*0.055 = 0.01 → WACC would be ~1%, clamped to 6%
        wacc = compute_wacc(data, rf_rate=0.01, params=DCFParams())
        assert wacc == pytest.approx(0.06)

    def test_clamp_ceiling_at_18pct(self):
        """Very high beta → result should not exceed 18%."""
        data = self._make_data(beta=5.0, market_cap=1e12, total_debt=0.0, total_cash=0.0)
        # ke = 0.045 + 5*0.055 = 0.32 → clamped to 0.18
        wacc = compute_wacc(data, rf_rate=0.045, params=DCFParams())
        assert wacc == pytest.approx(0.18)

    def test_fallback_when_v_is_zero(self):
        """When market_cap=0 and net_debt=0 → V=0 → fall back to params.discount_rate."""
        data = self._make_data(beta=1.0, market_cap=0.0, total_debt=0.0, total_cash=0.0)
        params = DCFParams(discount_rate=0.11)
        wacc = compute_wacc(data, rf_rate=0.045, params=params)
        assert wacc == pytest.approx(0.11)

    def test_missing_beta_defaults_to_1(self):
        """None beta should fall back to market beta (1.0)."""
        info = _make_info(market_cap=2.4e12, total_debt=0.0, total_cash=0.0)
        info["beta"] = None
        data = _make_ticker_data(info=info)
        wacc_none = compute_wacc(data, rf_rate=0.045, params=DCFParams())
        # beta=1.0 explicitly
        info2 = _make_info(market_cap=2.4e12, total_debt=0.0, total_cash=0.0)
        info2["beta"] = 1.0
        data2 = _make_ticker_data(info=info2)
        wacc_one = compute_wacc(data2, rf_rate=0.045, params=DCFParams())
        assert wacc_none == pytest.approx(wacc_one, rel=1e-6)

    def test_net_debt_floored_at_zero(self):
        """When total_cash > total_debt, net_debt should be 0 (not negative)."""
        # cash > debt → D=0 → all-equity financing
        data = self._make_data(beta=1.0, market_cap=1e12, total_debt=50e9, total_cash=200e9)
        wacc = compute_wacc(data, rf_rate=0.045, params=DCFParams())
        ke = 0.045 + 1.0 * 0.055
        # all equity: WACC = ke, clamped
        expected = max(0.06, min(0.18, ke))
        assert wacc == pytest.approx(expected, rel=1e-6)


# ── Phase 3: compute_sustainable_growth ──────────────────────────────────────

class TestComputeSustainableGrowth:
    def _make_data_for_growth(
        self,
        ebit=120e9,
        total_assets=350e9,
        total_cash_bs=50e9,
        fcf=90e9,
        net_income=100e9,
        sector="Technology",
    ) -> "TickerData":
        return TickerData(
            ticker="GRW",
            info=_make_info(sector=sector),
            cashflow=[{"period_date": "2023-09-30", "free_cash_flow": fcf, "operating_cashflow": fcf * 1.1}],
            financials=[{"period_date": "2023-09-30", "total_revenue": 400e9,
                         "gross_profit": 160e9, "ebit": ebit, "net_income": net_income}],
            balance_sheet=[{"period_date": "2023-09-30", "total_assets": total_assets,
                             "total_debt": 80e9, "total_cash": total_cash_bs,
                             "stockholders_equity": 120e9, "total_liabilities": 80e9}],
        )

    def test_basic_calculation(self):
        """g = ROIC * reinvestment_rate, clamped to [0.02, 0.12]."""
        # ebit=120e9, assets=350e9, cash=50e9 → ROIC = 120e9*0.79/(350e9-50e9)
        # fcf=90e9, net_income=100e9 → reinvestment_rate = 1 - 90/100 = 0.10
        data = self._make_data_for_growth(fcf=90e9, net_income=100e9)
        params = DCFParams()
        g = compute_sustainable_growth(data, params)
        roic = (120e9 * 0.79) / (350e9 - 50e9)
        reinvest = 1.0 - (90e9 / 100e9)
        expected = max(0.02, min(0.12, roic * reinvest))
        assert g == pytest.approx(expected, rel=1e-6)

    def test_fallback_when_roic_none(self):
        """No financials → ROIC is None → return params.growth_rate."""
        data = TickerData(
            ticker="T",
            info=_make_info(),
            cashflow=[{"period_date": "2023-09-30", "free_cash_flow": 50e9}],
            financials=[],
            balance_sheet=[],
        )
        params = DCFParams(growth_rate=0.07)
        assert compute_sustainable_growth(data, params) == pytest.approx(0.07)

    def test_fallback_when_net_income_zero(self):
        """Net income = 0 → return params.growth_rate."""
        data = self._make_data_for_growth(net_income=0.0)
        params = DCFParams(growth_rate=0.06)
        assert compute_sustainable_growth(data, params) == pytest.approx(0.06)

    def test_fallback_when_roic_negative(self):
        """Negative ROIC → return params.growth_rate."""
        data = self._make_data_for_growth(ebit=-30e9)
        params = DCFParams(growth_rate=0.05)
        assert compute_sustainable_growth(data, params) == pytest.approx(0.05)

    def test_clamp_floor_at_2pct(self):
        """Very low ROIC * reinvestment should be floored at 2%."""
        # tiny ROIC + very low reinvestment rate
        data = self._make_data_for_growth(
            ebit=1e9,          # tiny EBIT → tiny ROIC
            total_assets=350e9,
            total_cash_bs=50e9,
            fcf=99e9,          # almost all earnings returned → low reinvestment
            net_income=100e9,
        )
        g = compute_sustainable_growth(data, DCFParams())
        assert g >= 0.02

    def test_clamp_ceiling_at_12pct(self):
        """Very high ROIC * reinvestment should be capped at 12%."""
        # high ROIC + high reinvestment
        data = self._make_data_for_growth(
            ebit=300e9,        # very high EBIT
            total_assets=350e9,
            total_cash_bs=50e9,
            fcf=0.0,           # all retained → reinvestment_rate = 1.0
            net_income=100e9,
        )
        g = compute_sustainable_growth(data, DCFParams())
        assert g <= 0.12


# ── Phase 3: evaluate with dynamic WACC ──────────────────────────────────────

class TestEvaluateWithDynamicWacc:
    def test_wacc_used_and_growth_used_populated(self):
        """evaluate() should populate wacc_used and growth_used for non-financial."""
        data = _make_full_ticker_data()
        info_with_beta = {**data.info, "beta": 1.2}
        data = TickerData(
            ticker=data.ticker,
            info=info_with_beta,
            cashflow=data.cashflow,
            financials=data.financials,
            balance_sheet=data.balance_sheet,
        )
        result = evaluate(data, DEFAULT_PARAMS, rf_rate=0.045)
        assert result.wacc_used is not None
        assert result.growth_used is not None

    def test_wacc_used_within_bounds(self):
        """wacc_used should always be in [0.06, 0.18]."""
        data = _make_full_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS, rf_rate=0.045)
        if result.wacc_used is not None:
            assert 0.06 <= result.wacc_used <= 0.18

    def test_growth_used_within_bounds(self):
        """growth_used should always be in [0.02, 0.12] for non-financial."""
        data = _make_full_ticker_data()
        result = evaluate(data, DEFAULT_PARAMS, rf_rate=0.045)
        if result.growth_used is not None:
            assert 0.02 <= result.growth_used <= 0.12

    def test_financial_sector_wacc_used_is_none(self):
        """Financial sector → wacc_used and growth_used should be None."""
        data = _make_full_ticker_data(sector="Financial Services")
        info = {**data.info, "dividend_rate": 2.0}
        data = TickerData(
            ticker=data.ticker,
            info=info,
            cashflow=data.cashflow,
            financials=data.financials,
            balance_sheet=data.balance_sheet,
        )
        result = evaluate(data, DEFAULT_PARAMS, rf_rate=0.045)
        assert result.wacc_used is None
        assert result.growth_used is None

    def test_rf_rate_affects_wacc_used(self):
        """A higher rf_rate should produce a higher wacc_used."""
        data = _make_full_ticker_data()
        result_low  = evaluate(data, DEFAULT_PARAMS, rf_rate=0.02)
        result_high = evaluate(data, DEFAULT_PARAMS, rf_rate=0.07)
        if result_low.wacc_used is not None and result_high.wacc_used is not None:
            assert result_high.wacc_used >= result_low.wacc_used

    def test_default_rf_rate_is_4_5pct(self):
        """evaluate() with no rf_rate uses the 4.5% default."""
        data = _make_full_ticker_data()
        result_default = evaluate(data, DEFAULT_PARAMS)
        result_explicit = evaluate(data, DEFAULT_PARAMS, rf_rate=0.045)
        assert result_default.wacc_used == pytest.approx(result_explicit.wacc_used or 0, rel=1e-6)


# ── Sub-Task 1: ROE / ROA / Beta / Gross Margin / Operating Margin ────────────

class TestEvaluateExtendedQualityFields:
    def _make_data_with_extended_info(self, **extra_info) -> "TickerData":
        """TickerData with extended quality keys in info dict."""
        info = _make_info()
        info.update(extra_info)
        return _make_ticker_data(info=info)

    def test_evaluate_roe_roa_beta_populated(self):
        """roe/roa/beta/gross_margin/operating_margin populated when info has those keys."""
        data = self._make_data_with_extended_info(
            returnOnEquity=0.22,
            returnOnAssets=0.09,
            beta=1.15,
            grossMargins=0.43,
            operatingMargins=0.28,
        )
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.roe == pytest.approx(0.22)
        assert result.roa == pytest.approx(0.09)
        assert result.beta == pytest.approx(1.15)
        assert result.gross_margin == pytest.approx(0.43)
        assert result.operating_margin == pytest.approx(0.28)

    def test_evaluate_roe_missing_gracefully(self):
        """No crash when returnOnEquity / related keys are absent; fields default to None."""
        data = _make_ticker_data()   # info has no returnOnEquity etc.
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.roe is None
        assert result.roa is None
        assert result.gross_margin is None
        assert result.operating_margin is None

    def test_evaluate_roe_nan_becomes_none(self):
        """NaN values in info are treated as None (not stored as NaN)."""
        import math
        data = self._make_data_with_extended_info(
            returnOnEquity=float("nan"),
            returnOnAssets=float("nan"),
        )
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.roe is None
        assert result.roa is None

    def test_evaluate_beta_stored_as_is(self):
        """Beta is stored as a raw float (not scaled)."""
        data = self._make_data_with_extended_info(beta=0.75)
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.beta == pytest.approx(0.75)

    def test_evaluate_negative_roe_stored(self):
        """Negative ROE (loss-making) is stored as-is (not coerced to None)."""
        data = self._make_data_with_extended_info(returnOnEquity=-0.05)
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.roe is not None
        assert result.roe < 0


# ── compute_graham_number ─────────────────────────────────────────────────────

class TestComputeGrahamNumber:
    """Tests for compute_graham_number()."""

    def _make_graham_data(
        self,
        net_income: float = 5.0,
        stockholders_equity: float = 20.0,
        shares: float = 1.0,
    ) -> TickerData:
        """Minimal TickerData for Graham Number tests (per-share inputs)."""
        info = _make_info()
        info["sharesOutstanding"] = shares
        return TickerData(
            ticker="GTEST",
            info=info,
            cashflow=[],
            financials=[{"period_date": "2023-09-30", "net_income": net_income}],
            balance_sheet=[{"period_date": "2023-09-30", "stockholders_equity": stockholders_equity}],
        )

    def test_graham_number_valid(self):
        """EPS=5, BVPS=20 → sqrt(22.5 × 5 × 20) = sqrt(2250) ≈ 47.43."""
        # With shares=1.0 the raw values ARE eps and bvps directly
        data = self._make_graham_data(net_income=5.0, stockholders_equity=20.0, shares=1.0)
        result = compute_graham_number(data)
        assert result == pytest.approx(math.sqrt(22.5 * 5.0 * 20.0), rel=1e-3)

    def test_graham_number_negative_equity(self):
        """Negative stockholders_equity → None."""
        data = self._make_graham_data(stockholders_equity=-10.0)
        assert compute_graham_number(data) is None

    def test_graham_number_negative_earnings(self):
        """Negative net_income → None."""
        data = self._make_graham_data(net_income=-5.0)
        assert compute_graham_number(data) is None

    def test_graham_number_missing_shares(self):
        """Missing sharesOutstanding → None."""
        data = self._make_graham_data()
        data.info["sharesOutstanding"] = None
        assert compute_graham_number(data) is None

    def test_graham_number_zero_shares(self):
        """Zero sharesOutstanding → None."""
        data = self._make_graham_data()
        data.info["sharesOutstanding"] = 0
        assert compute_graham_number(data) is None

    def test_evaluate_graham_number_in_result(self):
        """evaluate() populates graham_number when financials/balance_sheet are present."""
        info = _make_info()
        info["sharesOutstanding"] = 16e9
        data = TickerData(
            ticker="GEVAL",
            info=info,
            cashflow=_make_cf_rows([90e9, 80e9, 75e9]),
            financials=[{"period_date": "2023-09-30", "net_income": 100e9}],
            balance_sheet=[{"period_date": "2023-09-30", "stockholders_equity": 200e9,
                            "total_assets": 350e9, "total_liabilities": 150e9,
                            "total_debt": 110e9, "total_cash": 50e9}],
        )
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.graham_number is not None
        # eps = 100e9/16e9, bvps = 200e9/16e9 → should be > 0
        assert result.graham_number > 0


# ── compute_beneish_m_score ───────────────────────────────────────────────────

from src.engine import compute_beneish_m_score


def _make_beneish_data(
    rev_t: float = 400e9,
    rev_t1: float = 360e9,
    gp_t: float = 160e9,
    gp_t1: float = 130e9,
    net_income_t: float = 100e9,
    op_cf_t: float = 115e9,
    assets_t: float = 350e9,
    assets_t1: float = 320e9,
    cash_t: float = 50e9,
    cash_t1: float = 45e9,
    liab_t: float = 250e9,
    liab_t1: float = 240e9,
) -> "TickerData":
    """Two-year TickerData fixture for Beneish M-Score tests."""
    return TickerData(
        ticker="BEN",
        info=_make_info(),
        financials=[
            {"period_date": "2023-09-30", "total_revenue": rev_t,
             "gross_profit": gp_t, "ebit": 120e9, "net_income": net_income_t},
            {"period_date": "2022-09-30", "total_revenue": rev_t1,
             "gross_profit": gp_t1, "ebit": 100e9, "net_income": net_income_t * 0.8},
        ],
        cashflow=[
            {"period_date": "2023-09-30", "operating_cashflow": op_cf_t,
             "free_cash_flow": op_cf_t * 0.9},
            {"period_date": "2022-09-30", "operating_cashflow": op_cf_t * 0.85,
             "free_cash_flow": op_cf_t * 0.75},
        ],
        balance_sheet=[
            {"period_date": "2023-09-30", "total_assets": assets_t,
             "total_liabilities": liab_t, "total_debt": 100e9, "total_cash": cash_t,
             "stockholders_equity": assets_t - liab_t},
            {"period_date": "2022-09-30", "total_assets": assets_t1,
             "total_liabilities": liab_t1, "total_debt": 110e9, "total_cash": cash_t1,
             "stockholders_equity": assets_t1 - liab_t1},
        ],
    )


class TestComputeBeneishMScore:
    def test_beneish_returns_float_with_sufficient_data(self):
        """2 years of data → returns a finite float."""
        data = _make_beneish_data()
        m = compute_beneish_m_score(data)
        assert m is not None
        assert isinstance(m, float)
        assert math.isfinite(m)

    def test_beneish_returns_none_with_one_year(self):
        """Only 1 year across all statements → None."""
        data = TickerData(
            ticker="ONE",
            info=_make_info(),
            financials=[{"period_date": "2023-09-30", "total_revenue": 400e9,
                         "gross_profit": 160e9, "ebit": 120e9, "net_income": 100e9}],
            cashflow=[{"period_date": "2023-09-30", "operating_cashflow": 115e9,
                       "free_cash_flow": 100e9}],
            balance_sheet=[{"period_date": "2023-09-30", "total_assets": 350e9,
                             "total_liabilities": 250e9, "total_debt": 100e9,
                             "total_cash": 50e9, "stockholders_equity": 100e9}],
        )
        assert compute_beneish_m_score(data) is None

    def test_beneish_returns_none_with_no_data(self):
        """Empty lists → None."""
        data = TickerData(
            ticker="EMP",
            info=_make_info(),
            financials=[],
            cashflow=[],
            balance_sheet=[],
        )
        assert compute_beneish_m_score(data) is None

    def test_beneish_flag_above_threshold(self):
        """Manipulator scenario (large accruals, high leverage growth) → beneish_flag True."""
        # Large positive accruals (net_income >> op_cf → TATA big positive),
        # rising leverage (liab_t/assets_t > liab_t1/assets_t1)
        data = _make_beneish_data(
            net_income_t=200e9,  # high net income
            op_cf_t=20e9,        # but very low operating cash flow → big accruals
            liab_t=300e9,        # leverage increased
            liab_t1=200e9,
        )
        m = compute_beneish_m_score(data)
        assert m is not None
        assert m > -1.78, f"Expected M > -1.78 (manipulator), got {m}"

    def test_beneish_flag_below_threshold(self):
        """Clean company (positive OCF > net_income, stable leverage) → M well below threshold."""
        data = _make_beneish_data(
            net_income_t=80e9,
            op_cf_t=130e9,   # OCF > net_income → negative accruals → lower M
            liab_t=240e9,
            liab_t1=260e9,   # leverage declining
        )
        m = compute_beneish_m_score(data)
        assert m is not None
        assert m < -1.78, f"Expected M < -1.78 (clean), got {m}"

    def test_beneish_flag_promotes_status_to_value_trap(self):
        """evaluate() with beneish_flag=True on an otherwise OK company → VALUE_TRAP."""
        # Use a data fixture where DCF will compute (so status would be OK),
        # but configure large accruals to trigger beneish_flag
        info = _make_info()
        info["sharesOutstanding"] = 16e9
        data = TickerData(
            ticker="TRAP",
            info=info,
            financials=[
                {"period_date": "2023-09-30", "total_revenue": 400e9,
                 "gross_profit": 160e9, "ebit": 120e9, "net_income": 200e9},
                {"period_date": "2022-09-30", "total_revenue": 360e9,
                 "gross_profit": 130e9, "ebit": 100e9, "net_income": 160e9},
            ],
            cashflow=[
                {"period_date": "2023-09-30", "operating_cashflow": 10e9,
                 "free_cash_flow": 90e9},
                {"period_date": "2022-09-30", "operating_cashflow": 80e9,
                 "free_cash_flow": 70e9},
            ],
            balance_sheet=[
                {"period_date": "2023-09-30", "total_assets": 350e9,
                 "total_liabilities": 310e9, "total_debt": 100e9, "total_cash": 50e9,
                 "stockholders_equity": 40e9},
                {"period_date": "2022-09-30", "total_assets": 320e9,
                 "total_liabilities": 240e9, "total_debt": 110e9, "total_cash": 45e9,
                 "stockholders_equity": 80e9},
            ],
        )
        # Verify beneish_flag fires for this fixture
        m = compute_beneish_m_score(data)
        assert m is not None and m > -1.78, f"Fixture should trigger flag, got M={m}"

        result = evaluate(data, DEFAULT_PARAMS)
        assert result.beneish_m is not None
        assert result.beneish_flag is True
        assert result.status == "VALUE_TRAP", (
            f"Expected VALUE_TRAP due to beneish_flag, got {result.status}"
        )

    def test_beneish_clean_company_no_value_trap(self):
        """Clean cash-generative company → beneish_flag False, no VALUE_TRAP promotion."""
        data = _make_full_ticker_data()
        # _make_full_ticker_data has OCF=115e9 > net_income=100e9, so negative accruals
        result = evaluate(data, DEFAULT_PARAMS)
        assert result.beneish_m is not None
        assert result.beneish_flag is False
