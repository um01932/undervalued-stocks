"""
engine.py — Valuation multiples & DCF model.

Transforms raw TickerData (from fetcher.py) into a ValuationResult containing:
  - Relative multiples: P/E, P/B, EV/EBITDA, P/FCF, PEG, Net Debt/EBITDA
  - DCF — Gordon Growth Model (GGM): conservative, FCF-based terminal value
  - DCF — Exit Multiple (EM):  market-relative, EBITDA × exit multiple
  - Margin of Safety vs. current price

Status logic:
  OK               — both/either DCF method succeeded, no value-trap conditions
  INSUFFICIENT_DATA — fewer than 3 valid positive FCF years AND EBITDA-based DCF
                      also unavailable
  VALUE_TRAP       — net_debt / EBITDA > 3.5 OR all available FCF years negative
                     (DCF may still be computed for informational purposes)
"""

from __future__ import annotations

import logging
import math
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.fetcher import TickerData

__all__ = [
    "DCFParams",
    "ValuationResult",
    "compute_multiples",
    "compute_dcf_ggm",
    "compute_dcf_exit",
    "evaluate",
]

logger = logging.getLogger(__name__)

# ── Parameters ────────────────────────────────────────────────────────────────


class DCFParams(BaseModel):
    """Configurable DCF model assumptions."""

    growth_rate: float = 0.05          # Annual FCF / EBITDA growth during projection
    discount_rate: float = 0.10        # WACC / required rate of return
    terminal_growth: float = 0.025     # Perpetuity growth rate (GGM terminal value)
    projection_years: int = 10         # Number of projection years
    exit_multiple: float = 12.0        # EV/EBITDA exit multiple (Exit Multiple method)


# ── Output model ──────────────────────────────────────────────────────────────


class ValuationResult(BaseModel):
    """Full valuation output for a single ticker."""

    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    current_price: Optional[float] = None
    market_cap: Optional[float] = None

    # Relative multiples
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    ev_ebitda: Optional[float] = None
    p_fcf: Optional[float] = None
    peg_ratio: Optional[float] = None
    net_debt_ebitda: Optional[float] = None

    # 52-week price range
    week52_low: Optional[float] = None
    week52_high: Optional[float] = None
    # % above 52-week low: 0% = at the low, 100% = at the high
    price_vs_52w_low_pct: Optional[float] = None

    # DCF estimates (per share)
    dcf_ggm_intrinsic: Optional[float] = None      # Gordon Growth Model
    dcf_exit_intrinsic: Optional[float] = None     # Exit Multiple
    dcf_intrinsic_value: Optional[float] = None    # Average of available methods

    margin_of_safety_pct: Optional[float] = None

    status: Literal["OK", "INSUFFICIENT_DATA", "VALUE_TRAP"] = "INSUFFICIENT_DATA"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_div(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Return numerator / denominator, or None if either is None/zero/NaN."""
    if numerator is None or denominator is None:
        return None
    try:
        if math.isnan(numerator) or math.isnan(denominator):
            return None
        if denominator == 0:
            return None
        result = numerator / denominator
        return None if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ZeroDivisionError):
        return None


def _safe_val(v: Any) -> Optional[float]:
    """Coerce a value to float; return None if impossible or non-finite."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


# ── Multiples computation ─────────────────────────────────────────────────────


def compute_multiples(data: TickerData) -> dict[str, Optional[float]]:
    """
    Extract and compute relative valuation multiples from TickerData.

    Returns a dict keyed by multiple name. Any multiple that cannot be
    computed (missing data, division by zero) is None.
    """
    info = data.info

    market_cap     = _safe_val(info.get("market_cap"))
    total_debt     = _safe_val(info.get("total_debt"))
    total_cash     = _safe_val(info.get("total_cash"))
    ebitda         = _safe_val(info.get("ebitda"))
    free_cashflow  = _safe_val(info.get("free_cashflow"))
    shares         = _safe_val(info.get("shares_outstanding"))

    # Prefer info-provided multiples; fall back to derived
    pe_ratio    = _safe_val(info.get("trailing_pe"))
    pb_ratio    = _safe_val(info.get("price_to_book"))
    ev_ebitda   = _safe_val(info.get("enterprise_to_ebitda"))
    peg_ratio   = _safe_val(info.get("peg_ratio"))

    # P/FCF — prefer info field, else derive
    p_fcf = _safe_div(market_cap, free_cashflow)

    # Net Debt / EBITDA
    net_debt_ebitda: Optional[float] = None
    if total_debt is not None and total_cash is not None and ebitda is not None:
        net_debt = total_debt - total_cash
        net_debt_ebitda = _safe_div(net_debt, ebitda)

    return {
        "pe_ratio":        pe_ratio,
        "pb_ratio":        pb_ratio,
        "ev_ebitda":       ev_ebitda,
        "p_fcf":           p_fcf,
        "peg_ratio":       peg_ratio,
        "net_debt_ebitda": net_debt_ebitda,
    }


# ── DCF — Gordon Growth Model ─────────────────────────────────────────────────


def compute_dcf_ggm(data: TickerData, params: DCFParams) -> Optional[float]:
    """
    Compute intrinsic value per share using the Gordon Growth Model DCF.

    Method:
      1. Extract annual Free Cash Flow from cashflow rows.
      2. Require at least 3 valid (non-NaN, > 0) annual FCF values.
      3. Use mean FCF as the base year estimate.
      4. Project N years at params.growth_rate.
      5. Terminal value = FCF_N × (1 + g) / (r - g)  [Gordon Growth Model]
      6. Discount all cash flows to PV; divide by shares outstanding.

    Returns:
        Intrinsic value per share, or None if data is insufficient.
    """
    shares = _safe_val(data.info.get("shares_outstanding"))
    if not shares or shares <= 0:
        return None

    # Extract FCF values from cashflow rows (most recent first)
    fcf_values: list[float] = []
    for row in data.cashflow:
        fcf = _safe_val(row.get("free_cash_flow"))
        if fcf is not None and fcf > 0:
            fcf_values.append(fcf)

    if len(fcf_values) < 3:
        logger.debug("%s: GGM skipped — only %d valid FCF years.", data.ticker, len(fcf_values))
        return None

    base_fcf = sum(fcf_values) / len(fcf_values)
    r = params.discount_rate
    g = params.growth_rate
    tg = params.terminal_growth
    n = params.projection_years

    if r <= tg:
        logger.warning("%s: GGM skipped — discount_rate (%s) <= terminal_growth (%s).", data.ticker, r, tg)
        return None

    # Project cash flows and discount to PV
    pv_total = 0.0
    for t in range(1, n + 1):
        projected_fcf = base_fcf * (1 + g) ** t
        pv_total += projected_fcf / (1 + r) ** t

    # Terminal value at end of projection horizon
    fcf_terminal = base_fcf * (1 + g) ** n * (1 + tg)
    terminal_value = fcf_terminal / (r - tg)
    pv_terminal = terminal_value / (1 + r) ** n

    intrinsic_total = pv_total + pv_terminal
    return intrinsic_total / shares


# ── DCF — Exit Multiple ───────────────────────────────────────────────────────


def compute_dcf_exit(data: TickerData, params: DCFParams) -> Optional[float]:
    """
    Compute intrinsic value per share using the Exit Multiple DCF method.

    Method:
      1. Use current EBITDA from info dict as the base year.
      2. Project EBITDA for N years at params.growth_rate.
      3. Terminal value = EBITDA_N × params.exit_multiple  (EV at exit)
      4. Subtract net debt to get equity value; discount to PV.
      5. Divide by shares outstanding.

    Returns:
        Intrinsic value per share, or None if EBITDA or shares data is missing.
    """
    ebitda = _safe_val(data.info.get("ebitda"))
    shares = _safe_val(data.info.get("shares_outstanding"))
    total_debt = _safe_val(data.info.get("total_debt")) or 0.0
    total_cash = _safe_val(data.info.get("total_cash")) or 0.0

    if not ebitda or ebitda <= 0:
        logger.debug("%s: Exit Multiple skipped — EBITDA unavailable or <= 0.", data.ticker)
        return None
    if not shares or shares <= 0:
        return None

    r = params.discount_rate
    g = params.growth_rate
    n = params.projection_years

    # Project EBITDA to year N
    ebitda_n = ebitda * (1 + g) ** n

    # Terminal enterprise value at exit
    terminal_ev = ebitda_n * params.exit_multiple

    # Discount terminal EV to present
    pv_terminal_ev = terminal_ev / (1 + r) ** n

    # Net debt (positive = more debt than cash)
    net_debt = total_debt - total_cash

    # Equity value = enterprise value − net debt
    equity_value = pv_terminal_ev - net_debt

    if equity_value <= 0:
        logger.debug("%s: Exit Multiple produced negative equity value.", data.ticker)
        return None

    return equity_value / shares


# ── Value-trap detection ──────────────────────────────────────────────────────


def _is_value_trap(data: TickerData, net_debt_ebitda: Optional[float]) -> bool:
    """
    Return True if the company shows value-trap indicators:
      - Net Debt / EBITDA > 3.5
      - All available FCF years are negative
    """
    if net_debt_ebitda is not None and net_debt_ebitda > 3.5:
        return True

    fcf_values = [
        _safe_val(row.get("free_cash_flow"))
        for row in data.cashflow
    ]
    valid_fcf = [v for v in fcf_values if v is not None]
    if valid_fcf and all(v <= 0 for v in valid_fcf):
        return True

    return False


# ── Top-level evaluator ───────────────────────────────────────────────────────


def evaluate(data: TickerData, params: DCFParams) -> ValuationResult:
    """
    Compute a full ValuationResult for the given ticker.

    Steps:
      1. Compute relative multiples.
      2. Run both DCF methods independently.
      3. Average DCF results when both succeed; use whichever is available.
      4. Determine status: VALUE_TRAP > INSUFFICIENT_DATA > OK.
      5. Compute Margin of Safety only for OK status.

    Args:
        data:   Raw TickerData from fetcher.
        params: DCF model assumptions.

    Returns:
        ValuationResult with all fields populated.
    """
    info = data.info
    multiples = compute_multiples(data)

    current_price = _safe_val(info.get("current_price"))

    # Both DCF methods
    ggm = compute_dcf_ggm(data, params)
    exit_m = compute_dcf_exit(data, params)

    # Consensus intrinsic value
    if ggm is not None and exit_m is not None:
        intrinsic = (ggm + exit_m) / 2.0
    elif ggm is not None:
        intrinsic = ggm
    elif exit_m is not None:
        intrinsic = exit_m
    else:
        intrinsic = None

    # Status
    is_trap = _is_value_trap(data, multiples.get("net_debt_ebitda"))
    if is_trap:
        status: Literal["OK", "INSUFFICIENT_DATA", "VALUE_TRAP"] = "VALUE_TRAP"
    elif intrinsic is None:
        status = "INSUFFICIENT_DATA"
    else:
        status = "OK"

    # Margin of Safety
    mos: Optional[float] = None
    if status == "OK" and intrinsic and current_price and intrinsic > 0:
        mos = (intrinsic - current_price) / intrinsic * 100.0

    logger.debug(
        "%s: status=%s ggm=%.2f exit=%.2f intrinsic=%.2f mos=%.1f%%",
        data.ticker,
        status,
        ggm or 0,
        exit_m or 0,
        intrinsic or 0,
        mos or 0,
    )

    # 52-week range metrics
    week52_low  = _safe_val(info.get("week52_low"))
    week52_high = _safe_val(info.get("week52_high"))
    price_vs_52w_low_pct: Optional[float] = None
    if current_price is not None and week52_low is not None and week52_high is not None:
        price_range = week52_high - week52_low
        if price_range > 0:
            price_vs_52w_low_pct = (current_price - week52_low) / price_range * 100.0

    return ValuationResult(
        ticker=data.ticker,
        company_name=info.get("short_name"),
        sector=info.get("sector"),
        industry=info.get("industry"),
        current_price=current_price,
        market_cap=_safe_val(info.get("market_cap")),
        pe_ratio=multiples.get("pe_ratio"),
        pb_ratio=multiples.get("pb_ratio"),
        ev_ebitda=multiples.get("ev_ebitda"),
        p_fcf=multiples.get("p_fcf"),
        peg_ratio=multiples.get("peg_ratio"),
        net_debt_ebitda=multiples.get("net_debt_ebitda"),
        week52_low=week52_low,
        week52_high=week52_high,
        price_vs_52w_low_pct=price_vs_52w_low_pct,
        dcf_ggm_intrinsic=ggm,
        dcf_exit_intrinsic=exit_m,
        dcf_intrinsic_value=intrinsic,
        margin_of_safety_pct=mos,
        status=status,
    )
