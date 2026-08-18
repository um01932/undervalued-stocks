"""
tests/unit/test_engine.py — unit tests for src/engine.py.

Uses synthetic TickerData fixtures; no real network calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.engine import (
    DCFParams,
    ValuationResult,
    _is_value_trap,
    _safe_div,
    _safe_val,
    compute_dcf_exit,
    compute_dcf_ggm,
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
