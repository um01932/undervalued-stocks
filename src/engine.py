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
    "compute_dcf_ddm",
    "compute_graham_number",
    "compute_piotroski",
    "compute_altman_z",
    "compute_beneish_m_score",
    "compute_roic",
    "compute_wacc",
    "compute_sustainable_growth",
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
    graham_number: Optional[float] = None          # Graham Number = sqrt(22.5 × EPS × BVPS)
    dcf_intrinsic_value: Optional[float] = None    # Average of available methods

    margin_of_safety_pct: Optional[float] = None

    status: Literal["OK", "INSUFFICIENT_DATA", "VALUE_TRAP"] = "INSUFFICIENT_DATA"

    # Sector routing metadata
    sector_excluded: bool = False                  # True when DCF was skipped (financial sector)
    dcf_model_used: Optional[str] = None           # "GGM", "Exit", "GGM+Exit", "DDM", or None

    # Quality metrics (Phase 2)
    piotroski_score: Optional[int] = None          # 0–7 (F6/F7 skipped); None = unavailable/financial
    altman_z: Optional[float] = None               # Z-score; None = unavailable/financial
    beneish_m: Optional[float] = None              # Beneish M-Score; None = unavailable
    beneish_flag: bool = False                     # True when M > -1.78 (potential manipulator)
    roic: Optional[float] = None                   # Return on Invested Capital (decimal); None = unavailable
    composite_score: Optional[float] = None        # 0–100 composite rank score

    # Dynamic WACC (Phase 3)
    wacc_used: Optional[float] = None              # Effective WACC applied to this ticker's DCF
    growth_used: Optional[float] = None            # Effective growth rate applied to this ticker's DCF

    # Extended quality metrics (Sub-Task 1)
    roe: Optional[float] = None              # Return on Equity (decimal, e.g. 0.15 = 15%)
    roa: Optional[float] = None              # Return on Assets (decimal)
    beta: Optional[float] = None             # Market beta vs S&P 500
    gross_margin: Optional[float] = None     # Gross margin (decimal)
    operating_margin: Optional[float] = None # Operating margin (decimal)

    # Dividend metrics (Sub-Task 6)
    dividend_yield: Optional[float] = None   # e.g. 0.035 = 3.5%
    payout_ratio_fcf: Optional[float] = None # dividends paid / avg_fcf_3y
    sbc_to_fcf_pct: Optional[float] = None   # SBC as % of reported FCF (red flag if > 30%)
    sbc_adjusted_fcf: Optional[float] = None # FCF - avg_sbc_3y (per share)
    shares_dilution_pct: Optional[float] = None  # YoY share count change % (positive = diluting)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FINANCIAL_SECTORS: frozenset[str] = frozenset({"Financial Services", "Insurance"})


def _is_financial_sector(sector: Optional[str]) -> bool:
    """Return True if sector is one where DCF (FCF/EBITDA-based) is unreliable."""
    if sector is None:
        return False
    return sector in _FINANCIAL_SECTORS


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


# ── Graham Number ─────────────────────────────────────────────────────────────


def compute_graham_number(ticker_data: TickerData) -> Optional[float]:
    """
    Graham Number = sqrt(22.5 × EPS × BVPS)
    EPS  = most recent annual net_income / shares_outstanding
    BVPS = most recent stockholders_equity / shares_outstanding
    Returns None if either is <= 0 or data missing.
    """
    shares = _safe_val(ticker_data.info.get("sharesOutstanding"))
    if not shares or shares <= 0:
        return None

    if not ticker_data.financials:
        return None
    net_income = _safe_val(ticker_data.financials[0].get("net_income"))
    if net_income is None or net_income <= 0:
        return None

    if not ticker_data.balance_sheet:
        return None
    equity = _safe_val(ticker_data.balance_sheet[0].get("stockholders_equity"))
    if equity is None or equity <= 0:
        return None

    eps = net_income / shares
    bvps = equity / shares

    return round(math.sqrt(22.5 * eps * bvps), 2)


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


# ── DCF — Dividend Discount Model (DDM / Gordon Growth) ──────────────────────


def compute_dcf_ddm(data: TickerData, params: DCFParams) -> Optional[float]:
    """
    Compute intrinsic value per share using the Dividend Discount Model (DDM).

    Intended for financial-sector companies where FCF/EBITDA-based DCF is
    unreliable.  Uses the Gordon Growth Model formula on dividends:

        P = D1 / (r - g)
        D1 = dividend_rate * (1 + g)
        g  = min(params.growth_rate, 0.05)   # cap DDM growth at 5%
        r  = params.discount_rate

    Returns:
        Intrinsic value per share, or None if dividend_rate is missing or <= 0.
    """
    dividend_rate = _safe_val(data.info.get("dividend_rate"))
    if dividend_rate is None or dividend_rate <= 0:
        return None

    r = params.discount_rate
    g = min(params.growth_rate, 0.05)

    if r <= g:
        logger.warning(
            "%s: DDM skipped — discount_rate (%s) <= growth_rate (%s).",
            data.ticker, r, g,
        )
        return None

    d1 = dividend_rate * (1 + g)
    return d1 / (r - g)


# ── Piotroski F-Score ─────────────────────────────────────────────────────────


def compute_piotroski(data: TickerData) -> Optional[int]:
    """
    Compute the Piotroski F-Score (max 7 achievable — F6 and F7 are skipped).

    Requires at least 2 years of complete data from financials, cashflow, and
    balance_sheet.  Returns None for financial-sector companies (banks/insurers
    have different accounting) and when fewer than 2 years of data are available.

    Signals implemented (1 point each):
      Profitability:
        F1 — ROA > 0
        F2 — Operating cash flow > 0
        F3 — ROA increasing year-over-year
        F4 — Accruals < 0  (CFO/assets > net_income/assets)
      Leverage & Liquidity:
        F5 — Leverage (total_debt/assets) falling year-over-year
        F6 — SKIPPED: current_assets/current_liabilities not stored (awards 0)
        F7 — SKIPPED: shares_outstanding has no historical data (awards 0)
      Operating Efficiency:
        F8 — Gross margin improving year-over-year
        F9 — Asset turnover improving year-over-year
    """
    sector = data.info.get("sector")
    if _is_financial_sector(sector):
        return None  # F-score unreliable for banks/insurers

    # Build year-indexed dicts keyed by period_date for alignment
    def _rows_by_date(rows: list[dict]) -> dict[str, dict]:
        return {r["period_date"]: r for r in rows if r.get("period_date")}

    fin_by_date = _rows_by_date(data.financials)
    cf_by_date  = _rows_by_date(data.cashflow)
    bs_by_date  = _rows_by_date(data.balance_sheet)

    # Common dates across all three statements, sorted descending (most recent first)
    common_dates = sorted(
        set(fin_by_date) & set(cf_by_date) & set(bs_by_date),
        reverse=True,
    )

    if len(common_dates) < 2:
        return None  # need at least current year + prior year

    def _get(rows_by_date: dict, date: str, key: str) -> Optional[float]:
        return _safe_val(rows_by_date.get(date, {}).get(key))

    # Current year (index 0) and prior year (index 1)
    d0, d1 = common_dates[0], common_dates[1]

    net_income_0    = _get(fin_by_date, d0, "net_income")
    net_income_1    = _get(fin_by_date, d1, "net_income")
    total_revenue_0 = _get(fin_by_date, d0, "total_revenue")
    total_revenue_1 = _get(fin_by_date, d1, "total_revenue")
    gross_profit_0  = _get(fin_by_date, d0, "gross_profit")
    gross_profit_1  = _get(fin_by_date, d1, "gross_profit")

    opcf_0          = _get(cf_by_date, d0, "operating_cashflow")

    total_assets_0  = _get(bs_by_date, d0, "total_assets")
    total_assets_1  = _get(bs_by_date, d1, "total_assets")
    total_debt_0    = _get(bs_by_date, d0, "total_debt")
    total_debt_1    = _get(bs_by_date, d1, "total_debt")

    # ROA helpers
    roa_0 = _safe_div(net_income_0, total_assets_0)
    roa_1 = _safe_div(net_income_1, total_assets_1)

    score = 0

    # F1: ROA > 0
    if roa_0 is not None and roa_0 > 0:
        score += 1

    # F2: Operating cash flow > 0
    if opcf_0 is not None and opcf_0 > 0:
        score += 1

    # F3: ROA increasing
    if roa_0 is not None and roa_1 is not None and roa_0 > roa_1:
        score += 1

    # F4: Accruals < 0 — CFO/assets > net_income/assets (cash earnings beat accruals)
    cfo_assets_0 = _safe_div(opcf_0, total_assets_0)
    if cfo_assets_0 is not None and roa_0 is not None and cfo_assets_0 > roa_0:
        score += 1

    # F5: Leverage falling (total_debt / total_assets decreased)
    lev_0 = _safe_div(total_debt_0, total_assets_0)
    lev_1 = _safe_div(total_debt_1, total_assets_1)
    if lev_0 is not None and lev_1 is not None and lev_0 < lev_1:
        score += 1

    # F6: SKIPPED — current_assets / current_liabilities not stored → 0 points
    # F7: SKIPPED — shares_outstanding has no annual history → 0 points

    # F8: Gross margin improving
    gm_0 = _safe_div(gross_profit_0, total_revenue_0)
    gm_1 = _safe_div(gross_profit_1, total_revenue_1)
    if gm_0 is not None and gm_1 is not None and gm_0 > gm_1:
        score += 1

    # F9: Asset turnover improving (total_revenue / total_assets)
    at_0 = _safe_div(total_revenue_0, total_assets_0)
    at_1 = _safe_div(total_revenue_1, total_assets_1)
    if at_0 is not None and at_1 is not None and at_0 > at_1:
        score += 1

    return score


# ── Altman Z-Score ────────────────────────────────────────────────────────────


def compute_altman_z(data: TickerData) -> Optional[float]:
    """
    Compute the Altman Z-Score using available data (proxies where needed).

    Skipped for financial-sector companies (returns None).

    Formula:  Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5

    Components (with proxies for missing balance-sheet sub-items):
      X1 = (stockholders_equity * 0.4) / total_assets
             (proxy for working capital / total assets; avoids needing current items)
      X2 = stockholders_equity / total_assets
             (proxy for retained earnings / total assets)
      X3 = ebit / total_assets
      X4 = market_cap / total_liabilities
      X5 = total_revenue / total_assets

    Interpretation:
      Z > 2.99   → Safe zone
      1.81–2.99  → Grey zone
      Z < 1.81   → Distress zone
    """
    sector = data.info.get("sector")
    if _is_financial_sector(sector):
        return None

    # Most recent balance sheet row
    bs_rows = sorted(data.balance_sheet, key=lambda r: r.get("period_date", ""), reverse=True)
    fin_rows = sorted(data.financials, key=lambda r: r.get("period_date", ""), reverse=True)

    if not bs_rows or not fin_rows:
        return None

    bs  = bs_rows[0]
    fin = fin_rows[0]

    total_assets        = _safe_val(bs.get("total_assets"))
    total_liabilities   = _safe_val(bs.get("total_liabilities"))
    stockholders_equity = _safe_val(bs.get("stockholders_equity"))
    ebit                = _safe_val(fin.get("ebit"))
    total_revenue       = _safe_val(fin.get("total_revenue"))
    market_cap          = _safe_val(data.info.get("market_cap"))

    # All components required
    if any(v is None for v in (total_assets, total_liabilities, stockholders_equity,
                               ebit, total_revenue, market_cap)):
        return None
    if total_assets == 0 or total_liabilities == 0:
        return None

    x1 = (stockholders_equity * 0.4) / total_assets   # working capital proxy
    x2 = stockholders_equity / total_assets             # retained earnings proxy
    x3 = ebit / total_assets
    x4 = market_cap / total_liabilities
    x5 = total_revenue / total_assets

    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    return None if math.isnan(z) or math.isinf(z) else round(z, 4)


# ── Beneish M-Score ───────────────────────────────────────────────────────────


def compute_beneish_m_score(ticker_data: TickerData) -> Optional[float]:
    """
    Beneish M-Score — detects probability of earnings manipulation.
    Requires 2+ consecutive years of financials, cashflow, balance_sheet.
    Returns M-Score float. M > -1.78 indicates potential manipulator.
    Returns None if insufficient data.

    8 indices (Beneish 1999):
      DSRI  = Days Sales Receivable Index  (proxy: revenue growth vs asset growth)
      GMI   = Gross Margin Index
      AQI   = Asset Quality Index          (proxy: non-current assets / total assets)
      SGI   = Sales Growth Index
      DEPI  = Depreciation Index           (not in data → use 1.0 neutral)
      SGAI  = SGA Expense Index            (not in data → use 1.0 neutral)
      LVGI  = Leverage Index               (total_liabilities / total_assets)
      TATA  = Total Accruals to Total Assets (net_income - operating_cashflow) / total_assets
    """
    def _rows_by_date(rows: list[dict]) -> dict[str, dict]:
        return {r["period_date"]: r for r in rows if r.get("period_date")}

    fin_by_date = _rows_by_date(ticker_data.financials)
    cf_by_date  = _rows_by_date(ticker_data.cashflow)
    bs_by_date  = _rows_by_date(ticker_data.balance_sheet)

    common_dates = sorted(
        set(fin_by_date) & set(cf_by_date) & set(bs_by_date),
        reverse=True,
    )

    if len(common_dates) < 2:
        return None

    def _get(rows_by_date: dict, date: str, key: str) -> Optional[float]:
        return _safe_val(rows_by_date.get(date, {}).get(key))

    d0, d1 = common_dates[0], common_dates[1]

    rev_t        = _get(fin_by_date, d0, "total_revenue")
    rev_t1       = _get(fin_by_date, d1, "total_revenue")
    gp_t         = _get(fin_by_date, d0, "gross_profit")
    gp_t1        = _get(fin_by_date, d1, "gross_profit")
    net_income_t = _get(fin_by_date, d0, "net_income")

    op_cf_t      = _get(cf_by_date, d0, "operating_cashflow")

    assets_t     = _get(bs_by_date, d0, "total_assets")
    assets_t1    = _get(bs_by_date, d1, "total_assets")
    cash_t       = _get(bs_by_date, d0, "total_cash")
    cash_t1      = _get(bs_by_date, d1, "total_cash")
    liab_t       = _get(bs_by_date, d0, "total_liabilities")
    liab_t1      = _get(bs_by_date, d1, "total_liabilities")

    # SGI — Sales Growth Index
    sgi = rev_t / rev_t1 if (rev_t is not None and rev_t1 and rev_t1 != 0) else 1.0

    # GMI — Gross Margin Index
    cogs_t  = (rev_t  - gp_t)  if (rev_t  is not None and gp_t  is not None) else None
    cogs_t1 = (rev_t1 - gp_t1) if (rev_t1 is not None and gp_t1 is not None) else None
    gm_t  = (rev_t  - (cogs_t  or 0)) / rev_t  if rev_t  else None
    gm_t1 = (rev_t1 - (cogs_t1 or 0)) / rev_t1 if rev_t1 else None
    gmi = (gm_t1 / gm_t) if (gm_t and gm_t > 0 and gm_t1 is not None) else 1.0

    # AQI — Asset Quality Index (simplified: non-cash assets / total assets)
    aqi_t  = (assets_t  - (cash_t  or 0)) / assets_t  if assets_t  else 0.5
    aqi_t1 = (assets_t1 - (cash_t1 or 0)) / assets_t1 if assets_t1 else 0.5
    aqi = aqi_t / aqi_t1 if aqi_t1 != 0 else 1.0

    # LVGI — Leverage Index
    lev_t  = liab_t  / assets_t  if (liab_t  is not None and assets_t)  else 0.5
    lev_t1 = liab_t1 / assets_t1 if (liab_t1 is not None and assets_t1) else 0.5
    lvgi = lev_t / lev_t1 if lev_t1 != 0 else 1.0

    # TATA — Total Accruals to Total Assets
    tata = ((net_income_t or 0) - (op_cf_t or 0)) / assets_t if assets_t else 0.0

    # DSRI — Days Sales Receivable Index (simplified: revenue growth / asset growth)
    rev_growth   = rev_t  / rev_t1  if (rev_t  is not None and rev_t1  and rev_t1  != 0) else 1.0
    asset_growth = assets_t / assets_t1 if (assets_t is not None and assets_t1 and assets_t1 != 0) else 1.0
    dsri = rev_growth / asset_growth if asset_growth != 0 else 1.0

    # DEPI, SGAI — not available → neutral value
    depi = 1.0
    sgai = 1.0

    m = (
        -4.84
        + 0.920 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )

    return None if (math.isnan(m) or math.isinf(m)) else round(m, 3)


# ── ROIC ──────────────────────────────────────────────────────────────────────


def compute_roic(data: TickerData) -> Optional[float]:
    """
    Compute Return on Invested Capital (ROIC).

    Skipped for financial-sector companies (returns None).

    Formula:
      NOPAT          = ebit * (1 - 0.21)           [US corporate tax rate 21%]
      Invested Capital = total_assets - total_cash   [simplified; avoids current liabilities]
      ROIC           = NOPAT / Invested Capital

    Returns as a decimal (e.g. 0.15 = 15%).  Returns None if data is missing.
    """
    sector = data.info.get("sector")
    if _is_financial_sector(sector):
        return None

    fin_rows = sorted(data.financials, key=lambda r: r.get("period_date", ""), reverse=True)
    bs_rows  = sorted(data.balance_sheet, key=lambda r: r.get("period_date", ""), reverse=True)

    if not fin_rows or not bs_rows:
        return None

    ebit         = _safe_val(fin_rows[0].get("ebit"))
    total_assets = _safe_val(bs_rows[0].get("total_assets"))
    total_cash   = _safe_val(bs_rows[0].get("total_cash"))

    if ebit is None or total_assets is None or total_cash is None:
        return None

    nopat            = ebit * (1 - 0.21)
    invested_capital = total_assets - total_cash

    roic = _safe_div(nopat, invested_capital)
    return roic


# ── Dynamic WACC ─────────────────────────────────────────────────────────────

_ERP = 0.055  # Equity Risk Premium — US historical average


def compute_wacc(data: TickerData, rf_rate: float, params: DCFParams) -> float:
    """
    Compute a company-specific WACC using CAPM for cost of equity.

    Formula:
        ke   = rf_rate + beta * ERP
        kd   = 0.05  (proxy — interest expense not stored)
        E    = market_cap
        D    = max(total_debt - total_cash, 0)
        WACC = (E/V)*ke + (D/V)*kd*(1-T)

    Returns a value clamped to [0.06, 0.18].
    Falls back to params.discount_rate if V <= 0.
    """
    beta_raw = _safe_val(data.info.get("beta"))
    beta = beta_raw if beta_raw is not None else 1.0
    ke = rf_rate + beta * _ERP

    market_cap = _safe_val(data.info.get("market_cap")) or 0.0
    total_debt = _safe_val(data.info.get("total_debt")) or 0.0
    total_cash = _safe_val(data.info.get("total_cash")) or 0.0

    kd = 0.05  # proxy: 5% cost of debt
    E = market_cap
    D = max(total_debt - total_cash, 0.0)
    V = E + D

    if V <= 0:
        return params.discount_rate

    T = 0.21  # US corporate tax rate
    wacc = (E / V) * ke + (D / V) * kd * (1 - T)
    return max(0.06, min(0.18, wacc))


def compute_sustainable_growth(data: TickerData, params: DCFParams) -> float:
    """
    Compute a sustainable growth rate from ROIC and reinvestment rate.

    Formula:
        reinvestment_rate = 1 - (FCF / net_income)   [clamped to [0, 1]]
        g = ROIC * reinvestment_rate                  [clamped to [0.02, 0.12]]

    Falls back to params.growth_rate when ROIC is unavailable/non-positive
    or when net_income is unavailable/non-positive.
    """
    roic_val = compute_roic(data)
    if roic_val is None or roic_val <= 0:
        return params.growth_rate

    # Most recent FCF and net income
    cf_rows = sorted(data.cashflow, key=lambda r: r.get("period_date", ""), reverse=True)
    fin_rows = sorted(data.financials, key=lambda r: r.get("period_date", ""), reverse=True)

    fcf = _safe_val(cf_rows[0].get("free_cash_flow")) if cf_rows else None
    net_income = _safe_val(fin_rows[0].get("net_income")) if fin_rows else None

    if net_income is None or net_income <= 0 or fcf is None:
        return params.growth_rate

    reinvestment_rate = 1.0 - (fcf / net_income)
    reinvestment_rate = max(0.0, min(1.0, reinvestment_rate))

    g = roic_val * reinvestment_rate
    return max(0.02, min(0.12, g))


# ── Top-level evaluator ───────────────────────────────────────────────────────


def evaluate(data: TickerData, params: DCFParams, rf_rate: float = 0.045) -> ValuationResult:
    """
    Compute a full ValuationResult for the given ticker.

    Steps:
      1. Compute relative multiples.
      2. For financial-sector companies: skip GGM/Exit DCF; try DDM instead.
         For all other companies: run GGM and Exit Multiple DCF independently.
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
    sector = info.get("sector")
    financial = _is_financial_sector(sector)

    # Dynamic WACC and growth (skipped for financial sector — use params as-is)
    if financial:
        params_dynamic = params
    else:
        wacc = compute_wacc(data, rf_rate, params)
        g_sustainable = compute_sustainable_growth(data, params)
        params_dynamic = params.model_copy(
            update={"discount_rate": wacc, "growth_rate": g_sustainable}
        )

    ggm: Optional[float] = None
    exit_m: Optional[float] = None
    graham: Optional[float] = None
    intrinsic: Optional[float] = None
    dcf_model_used: Optional[str] = None

    if financial:
        # Financial-sector companies: use DDM only
        ddm = compute_dcf_ddm(data, params_dynamic)
        if ddm is not None:
            intrinsic = ddm
            dcf_model_used = "DDM"
    else:
        # Non-financial companies: run GGM, Exit Multiple, and Graham Number
        ggm = compute_dcf_ggm(data, params_dynamic)
        exit_m = compute_dcf_exit(data, params_dynamic)
        graham = compute_graham_number(data)

        available = [v for v in (ggm, exit_m, graham) if v is not None]
        if available:
            intrinsic = sum(available) / len(available)
            parts: list[str] = []
            if ggm is not None:
                parts.append("GGM")
            if exit_m is not None:
                parts.append("Exit")
            if graham is not None:
                parts.append("Graham")
            dcf_model_used = "+".join(parts)

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
        "%s: status=%s ggm=%.2f exit=%.2f intrinsic=%.2f mos=%.1f%% model=%s",
        data.ticker,
        status,
        ggm or 0,
        exit_m or 0,
        intrinsic or 0,
        mos or 0,
        dcf_model_used or "—",
    )

    # 52-week range metrics
    week52_low  = _safe_val(info.get("week52_low"))
    week52_high = _safe_val(info.get("week52_high"))
    price_vs_52w_low_pct: Optional[float] = None
    if current_price is not None and week52_low is not None and week52_high is not None:
        price_range = week52_high - week52_low
        if price_range > 0:
            price_vs_52w_low_pct = (current_price - week52_low) / price_range * 100.0

    piotroski  = compute_piotroski(data)
    altman_z_v = compute_altman_z(data)
    beneish_m_v = compute_beneish_m_score(data)
    roic_v     = compute_roic(data)

    # Beneish flag — potential earnings manipulator
    beneish_flag_v = beneish_m_v is not None and beneish_m_v > -1.78
    # Promote OK → VALUE_TRAP when manipulation risk detected
    if beneish_flag_v and status == "OK":
        status = "VALUE_TRAP"

    # Extended quality metrics — read directly from info dict
    try:
        roe_v             = _safe_val(info.get("returnOnEquity"))
        roa_v             = _safe_val(info.get("returnOnAssets"))
        beta_v            = _safe_val(info.get("beta"))
        gross_margin_v    = _safe_val(info.get("grossMargins"))
        operating_margin_v = _safe_val(info.get("operatingMargins"))
    except Exception:
        roe_v = roa_v = beta_v = gross_margin_v = operating_margin_v = None

    # Dividend yield — directly from info
    dividend_yield_v = _safe_val(info.get("dividendYield"))

    # payout_ratio_fcf: (dividend_rate × shares) / mean(positive_fcf_3y)
    div_rate_v = _safe_val(info.get("dividendRate"))
    shares_v   = _safe_val(info.get("sharesOutstanding"))
    fcf_vals = [
        _safe_val(cf.get("free_cash_flow"))
        for cf in data.cashflow
        if _safe_val(cf.get("free_cash_flow")) is not None
           and _safe_val(cf.get("free_cash_flow")) > 0
    ][:3]
    payout_ratio_fcf_v: Optional[float] = None
    if div_rate_v and shares_v and fcf_vals:
        total_divs = div_rate_v * shares_v
        avg_fcf    = sum(fcf_vals) / len(fcf_vals)
        if avg_fcf > 0:
            payout_ratio_fcf_v = round(total_divs / avg_fcf, 4)

    sbc_vals = []
    for cf in data.cashflow[:3]:
        sbc = _safe_val(cf.get("stock_based_compensation"))
        if sbc is not None and sbc > 0:
            sbc_vals.append(sbc)

    fcf_vals_raw = [
        _safe_val(cf.get("free_cash_flow"))
        for cf in data.cashflow[:3]
        if _safe_val(cf.get("free_cash_flow")) is not None
    ]

    sbc_to_fcf_pct_v: Optional[float] = None
    sbc_adjusted_fcf_v: Optional[float] = None
    if sbc_vals and fcf_vals_raw:
        avg_sbc = sum(sbc_vals) / len(sbc_vals)
        avg_fcf = sum(fcf_vals_raw) / len(fcf_vals_raw)
        if avg_fcf > 0:
            sbc_to_fcf_pct_v = round(avg_sbc / avg_fcf * 100, 1)
        if shares_v and shares_v > 0:
            sbc_adjusted_fcf_v = round((avg_fcf - avg_sbc) / shares_v, 2)

    shares_dilution_pct_v: Optional[float] = None
    shares_now = _safe_val(data.info.get("sharesOutstanding"))
    shares_3y = _safe_val(data.info.get("impliedSharesOutstanding"))
    if shares_now and shares_3y and shares_3y > 0:
        shares_dilution_pct_v = round((shares_now / shares_3y - 1) * 100, 2)

    result = ValuationResult(
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
        graham_number=graham,
        dcf_intrinsic_value=intrinsic,
        margin_of_safety_pct=mos,
        status=status,
        sector_excluded=financial,
        dcf_model_used=dcf_model_used,
        piotroski_score=piotroski,
        altman_z=altman_z_v,
        beneish_m=beneish_m_v,
        beneish_flag=beneish_flag_v,
        roic=roic_v,
        wacc_used=None if financial else params_dynamic.discount_rate,
        growth_used=None if financial else params_dynamic.growth_rate,
        roe=roe_v,
        roa=roa_v,
        beta=beta_v,
        gross_margin=gross_margin_v,
        operating_margin=operating_margin_v,
        dividend_yield=dividend_yield_v,
        payout_ratio_fcf=payout_ratio_fcf_v,
        sbc_to_fcf_pct=sbc_to_fcf_pct_v,
        sbc_adjusted_fcf=sbc_adjusted_fcf_v,
        shares_dilution_pct=shares_dilution_pct_v,
    )
    from src.screener import compute_composite_score  # late import to avoid circular dep
    result.composite_score = compute_composite_score(result)
    return result
