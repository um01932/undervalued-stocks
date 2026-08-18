"""
backtester.py — Walk-forward portfolio backtesting.

Simulates running the screener at the start of each year using current fundamentals
(as a proxy for historical — documented limitation), holds top-N companies for 12 months,
and measures performance vs S&P 500 benchmark.

Usage:
    python src/main.py --backtest --universe sp500 --profile deep_value --workers 6
"""

from __future__ import annotations

import logging
import math
import statistics
from typing import Optional

from pydantic import BaseModel

from src.engine import DCFParams, evaluate
from src.fetcher import CacheStore, TickerData, fetch_historical_prices
from src.screener import ScreenerProfile, apply_profile

__all__ = [
    "AnnualRow",
    "BacktestResult",
    "run_backtest",
    "LIMITATIONS",
]

logger = logging.getLogger(__name__)

# ── Limitations documentation ─────────────────────────────────────────────────

LIMITATIONS = """
IMPORTANT LIMITATIONS OF THIS BACKTEST:

1. Look-ahead bias: This backtester uses CURRENT financial fundamentals (latest
   yfinance filings) as the screening criteria for ALL historical years. Real
   point-in-time financials would differ. Results will be OPTIMISTIC.

2. Survivorship bias: The universe only contains CURRENT index constituents.
   Companies that were in the S&P 500 in 2018 but have since been removed
   (failed, merged, or delisted) are not included.

3. Single-day pricing: Entry/exit prices use a single date per year rather
   than a TWAP (Time-Weighted Average Price). Actual execution would differ.

4. No transaction costs: Commissions, bid-ask spread, and slippage are not modelled.

These limitations mean backtesting results should be treated as DIRECTIONAL
indicators of strategy quality, not as reliable predictions of future returns.
"""

# ── Pydantic models ───────────────────────────────────────────────────────────


class AnnualRow(BaseModel):
    """Performance metrics for one calendar year of the backtest."""

    year: int
    selected_tickers: list[str]
    portfolio_return: float    # decimal, e.g. 0.12 = 12%
    benchmark_return: float
    excess_return: float       # portfolio - benchmark
    winning_picks: int
    total_picks: int


class BacktestResult(BaseModel):
    """Aggregate results of a walk-forward backtest."""

    profile_name: str
    start_year: int
    end_year: int
    top_n: int
    annual_rows: list[AnnualRow]  # one per year
    cagr_portfolio: float
    cagr_benchmark: float
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    max_drawdown: float
    win_rate: float    # fraction of individual stock picks that beat benchmark
    total_picks: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def _entry_exit_dates(year: int) -> tuple[str, str]:
    """
    Return (entry_date, exit_date) for a given year.

    Uses Jan 2 as a proxy for the first trading day (avoids Jan 1 holiday).
    """
    entry = f"{year}-01-02"
    exit_ = f"{year + 1}-01-02"
    return entry, exit_


def _cagr(start_value: float, end_value: float, n_years: int) -> float:
    """Compute Compound Annual Growth Rate."""
    if n_years <= 0 or start_value <= 0:
        return 0.0
    return (end_value / start_value) ** (1.0 / n_years) - 1.0


def _sharpe(returns: list[float], rf_rate: float) -> Optional[float]:
    """Compute annualised Sharpe ratio from a list of annual returns."""
    if len(returns) < 2:
        return None
    vol = statistics.stdev(returns)
    if vol <= 0:
        return None
    excess = statistics.mean(returns) - rf_rate
    return excess / vol


def _sortino(returns: list[float], rf_rate: float) -> Optional[float]:
    """Compute Sortino ratio using downside deviation."""
    if len(returns) < 2:
        return None
    downside = [r for r in returns if r < 0]
    if not downside:
        # No negative years — Sortino is technically infinite; return None
        return None
    downside_dev = math.sqrt(sum(r ** 2 for r in downside) / len(returns))
    if downside_dev <= 0:
        return None
    excess = statistics.mean(returns) - rf_rate
    return excess / downside_dev


def _max_drawdown(values: list[float]) -> float:
    """
    Compute max peak-to-trough drawdown from a series of portfolio values.

    Returns a negative decimal (e.g. -0.15 means -15% drawdown).
    """
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


# ── Main backtest function ────────────────────────────────────────────────────


def run_backtest(
    tickers: list[str],
    cache: CacheStore,
    profile: ScreenerProfile,
    dcf_params: DCFParams,
    rf_rate: float = 0.045,
    start_year: int = 2018,
    end_year: int = 2024,
    top_n: int = 10,
    benchmark_ticker: str = "^GSPC",
) -> BacktestResult:
    """
    Run a walk-forward portfolio backtest.

    Uses current fundamentals to select top-N tickers per the screener profile,
    then measures actual price returns for each calendar year.

    Args:
        tickers:          Universe of ticker symbols to screen.
        cache:            Shared CacheStore (for both fundamentals & price history).
        profile:          ScreenerProfile defining filter thresholds.
        dcf_params:       DCF model parameters.
        rf_rate:          Risk-free rate (decimal) used for Sharpe/Sortino.
        start_year:       First calendar year of the backtest.
        end_year:         Last calendar year (inclusive).
        top_n:            Number of top-ranked tickers to hold each year.
        benchmark_ticker: Ticker for the benchmark index.

    Returns:
        BacktestResult with per-year rows and aggregate statistics.
    """
    years = list(range(start_year, end_year + 1))

    # ── 1. Evaluate all tickers with current fundamentals ────────────────────
    ticker_data_map: dict[str, TickerData] = {td.ticker: td for td in _load_ticker_data(tickers, cache)}
    valuation_results = []
    for td in ticker_data_map.values():
        try:
            result = evaluate(td, dcf_params, rf_rate=rf_rate)
            valuation_results.append(result)
        except Exception as exc:
            logger.debug("Evaluation failed for %s: %s", td.ticker, exc)

    # ── 2. Apply profile filter to get ranked top-N tickers ──────────────────
    ranked_df = apply_profile(valuation_results, profile)
    if ranked_df.empty:
        logger.warning("Profile '%s' produced 0 results — backtest will be empty.", profile.name)
        return BacktestResult(
            profile_name=profile.name,
            start_year=start_year,
            end_year=end_year,
            top_n=top_n,
            annual_rows=[],
            cagr_portfolio=0.0,
            cagr_benchmark=0.0,
            sharpe_ratio=None,
            sortino_ratio=None,
            max_drawdown=0.0,
            win_rate=0.0,
            total_picks=0,
        )

    selected_tickers: list[str] = list(ranked_df["Ticker"].head(top_n))

    # ── 3. Build list of all dates needed ────────────────────────────────────
    all_dates: list[str] = []
    for year in years:
        entry, exit_ = _entry_exit_dates(year)
        all_dates.extend([entry, exit_])
    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_dates: list[str] = []
    for d in all_dates:
        if d not in seen:
            seen.add(d)
            unique_dates.append(d)

    all_tickers_to_fetch = selected_tickers + [benchmark_ticker]

    # ── 4. Fetch historical prices ────────────────────────────────────────────
    price_data = fetch_historical_prices(all_tickers_to_fetch, unique_dates, cache)

    # ── 5 & 6. Compute per-year returns and portfolio value series ────────────
    annual_rows: list[AnnualRow] = []
    portfolio_values: list[float] = [1.0]   # starts at 1.0 (i.e. $1 invested)
    benchmark_values: list[float] = [1.0]
    portfolio_returns: list[float] = []
    benchmark_returns_list: list[float] = []
    total_wins = 0
    total_picks_count = 0

    for year in years:
        entry_date, exit_date = _entry_exit_dates(year)
        bm_prices = price_data.get(benchmark_ticker, {})
        bm_entry = bm_prices.get(entry_date)
        bm_exit  = bm_prices.get(exit_date)

        if bm_entry is None or bm_exit is None or bm_entry == 0:
            logger.warning(
                "Benchmark %s missing price data for year %d. Skipping year.",
                benchmark_ticker, year,
            )
            continue

        bm_return = (bm_exit - bm_entry) / bm_entry

        # Compute per-ticker returns
        ticker_returns: list[float] = []
        wins = 0
        for tkr in selected_tickers:
            tkr_prices = price_data.get(tkr, {})
            entry_p = tkr_prices.get(entry_date)
            exit_p  = tkr_prices.get(exit_date)
            if entry_p is None or exit_p is None or entry_p == 0:
                continue  # skip tickers with no price data for this year
            ret = (exit_p - entry_p) / entry_p
            ticker_returns.append(ret)
            if ret > bm_return:
                wins += 1

        if not ticker_returns:
            logger.warning("No valid price data for any selected ticker in year %d.", year)
            continue

        port_return = sum(ticker_returns) / len(ticker_returns)  # equal-weighted
        excess = port_return - bm_return

        annual_rows.append(AnnualRow(
            year=year,
            selected_tickers=selected_tickers,
            portfolio_return=port_return,
            benchmark_return=bm_return,
            excess_return=excess,
            winning_picks=wins,
            total_picks=len(ticker_returns),
        ))

        # Compound portfolio and benchmark values
        portfolio_values.append(portfolio_values[-1] * (1 + port_return))
        benchmark_values.append(benchmark_values[-1] * (1 + bm_return))
        portfolio_returns.append(port_return)
        benchmark_returns_list.append(bm_return)
        total_wins += wins
        total_picks_count += len(ticker_returns)

    # ── 7. Compute aggregate metrics ──────────────────────────────────────────
    n_years = len(portfolio_returns)
    cagr_port = _cagr(portfolio_values[0], portfolio_values[-1], n_years) if n_years > 0 else 0.0
    cagr_bm   = _cagr(benchmark_values[0], benchmark_values[-1], n_years) if n_years > 0 else 0.0
    sharpe    = _sharpe(portfolio_returns, rf_rate) if n_years >= 2 else None
    sortino   = _sortino(portfolio_returns, rf_rate) if n_years >= 2 else None
    max_dd    = _max_drawdown(portfolio_values)
    win_rate  = (total_wins / total_picks_count) if total_picks_count > 0 else 0.0

    return BacktestResult(
        profile_name=profile.name,
        start_year=start_year,
        end_year=end_year,
        top_n=top_n,
        annual_rows=annual_rows,
        cagr_portfolio=cagr_port,
        cagr_benchmark=cagr_bm,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown=max_dd,
        win_rate=win_rate,
        total_picks=total_picks_count,
    )


def _load_ticker_data(tickers: list[str], cache: CacheStore) -> list[TickerData]:
    """Load TickerData objects from cache (no network calls)."""
    from src.fetcher import CacheStore as _CS  # already imported above; just for clarity
    results = []
    for ticker in tickers:
        info = cache.get_info(ticker)
        cf   = cache.get_financials(ticker, "cashflow")
        fin  = cache.get_financials(ticker, "financials")
        bs   = cache.get_financials(ticker, "balance_sheet")
        if info and cf and fin and bs:
            results.append(TickerData(
                ticker=ticker,
                info=info,
                cashflow=cf,
                financials=fin,
                balance_sheet=bs,
            ))
    return results
