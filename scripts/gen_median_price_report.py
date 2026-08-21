"""
gen_median_price_report.py — Generate a CSV with median historical close prices
for every company in data/global_tickers.csv.

Columns produced (one row per ticker):
  ticker, name, exchange, country,
  current_price,
  median_5y, median_4y, median_3y, median_2y, median_1y

"Median" is the median of all daily CLOSE prices in the rolling window
measured backwards from today.  Uses the project's DuckDB cache
(data/cache.duckdb) so previously downloaded data is reused.

Usage:
    python scripts/gen_median_price_report.py
    python scripts/gen_median_price_report.py --out data/reports/my_file.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# ── Project paths ─────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.fetcher import CacheStore, PRICE_HISTORY_TTL, _safe_float  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TICKERS_CSV = ROOT / "data" / "global_tickers.csv"
REPORTS_DIR = ROOT / "data" / "reports"
DB_PATH     = str(ROOT / "data" / "cache.duckdb")

# Window sizes (years) — from largest to smallest so one download covers all.
WINDOWS = [5, 4, 3, 2, 1]
FETCH_YEARS = max(WINDOWS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_tickers() -> list[dict]:
    with TICKERS_CSV.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _fetch_prices(ticker: str, cache: CacheStore) -> dict[str, float]:
    """
    Return {date_str: close} for the last FETCH_YEARS years.

    Checks the DuckDB cache first; fetches from yfinance only if needed.
    Stores new data back into the cache.
    """
    today    = datetime.utcnow().date()
    start_dt = today - timedelta(days=FETCH_YEARS * 366)   # slight overrun for leap years
    start_str = str(start_dt)
    end_str   = str(today + timedelta(days=1))             # yfinance end is exclusive

    # Build the list of all calendar dates in the window so we can check cache coverage.
    # We only need the ones that COULD be trading days; in practice we just request the
    # full range and accept whatever is cached.
    cutoff = datetime.utcnow() - PRICE_HISTORY_TTL

    # Pull everything already in cache for this ticker
    from src.fetcher import _utcnow
    import duckdb

    with cache._lock:
        rows = cache._conn_obj.execute(
            "SELECT date, close FROM price_history "
            "WHERE ticker = ? AND date >= ? AND fetched_at >= ?",
            [ticker, start_str, cutoff.replace(tzinfo=None)],
        ).fetchall()
    cached: dict[str, float] = {r[0]: r[1] for r in rows}

    # Decide whether we need a fresh download: if cache has ≥ 200 entries in
    # the window we trust it (trading days/year ≈ 252).
    if len(cached) >= 200:
        return cached

    log.debug("Downloading price history for %s …", ticker)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(
                ticker,
                start=start_str,
                end=end_str,
                auto_adjust=True,
                progress=False,
            )
    except Exception as exc:
        log.warning("%-8s  download failed: %s", ticker, exc)
        return cached

    if df is None or df.empty:
        log.warning("%-8s  no data returned", ticker)
        return cached

    # Flatten MultiIndex if present (single-ticker download may produce one)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close_col = next(
        (c for c in df.columns if str(c).lower() in ("close", "adj close")), None
    )
    if close_col is None:
        log.warning("%-8s  no 'close' column in downloaded data", ticker)
        return cached

    fetched: dict[str, float] = {}
    for idx, val in df[close_col].items():
        f = _safe_float(val)
        if f is not None:
            fetched[str(idx)[:10]] = f

    if fetched:
        cache.set_price_history(ticker, fetched)
        cached.update(fetched)

    return cached


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _window_median(prices: dict[str, float], years: int) -> float | None:
    """Median of closes in the last `years` calendar years."""
    cutoff = str((datetime.utcnow().date() - timedelta(days=years * 366)))
    vals = [v for k, v in prices.items() if k >= cutoff]
    return _median(vals)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(out_path: Path | None = None) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        out_path = REPORTS_DIR / f"median_price_report_{ts}.csv"

    tickers = _load_tickers()
    log.info("Loaded %d tickers from %s", len(tickers), TICKERS_CSV)

    cache = CacheStore(DB_PATH)

    fieldnames = [
        "ticker", "name", "exchange", "country",
        "current_price",
        "median_5y", "median_4y", "median_3y", "median_2y", "median_1y",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()

        for i, row in enumerate(tickers, 1):
            ticker = row["ticker"]
            log.info("[%d/%d] %s", i, len(tickers), ticker)

            prices = _fetch_prices(ticker, cache)

            # Current price: most recent available close
            current = prices[max(prices)] if prices else None

            record = {
                "ticker":        ticker,
                "name":          row.get("name", ""),
                "exchange":      row.get("exchange", ""),
                "country":       row.get("country", ""),
                "current_price": f"{current:.4f}" if current is not None else "",
            }
            for yrs in WINDOWS:
                med = _window_median(prices, yrs)
                record[f"median_{yrs}y"] = f"{med:.4f}" if med is not None else ""

            writer.writerow(record)
            fh.flush()   # write incrementally so partial results are saved

    log.info("Report written to %s", out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate median price report CSV.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output CSV path (default: data/reports/median_price_report_<ts>.csv)")
    args = parser.parse_args()
    main(args.out)
