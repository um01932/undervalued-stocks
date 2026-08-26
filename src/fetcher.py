"""
fetcher.py — Concurrent data pipeline with DuckDB-backed cache.

Architecture:
  - CacheStore  : thread-safe DuckDB wrapper with TTL per data type.
  - TickerData  : Pydantic model holding raw yfinance output for one ticker.
  - fetch_ticker: cache-first fetch for a single ticker with retry/backoff.
  - fetch_universe: parallel fetch across all tickers via ThreadPoolExecutor.

Cache schema (data/cache.duckdb):
  ticker_info(ticker PK, fetched_at, current_price, market_cap, trailing_pe,
              price_to_book, enterprise_to_ebitda, peg_ratio, free_cashflow,
              total_debt, total_cash, ebitda, shares_outstanding,
              short_name, sector, industry)

  financials(ticker, period_date PK, fetched_at,
             total_revenue, gross_profit, ebit, net_income)

  cashflow(ticker, period_date PK, fetched_at,
           operating_cashflow, capital_expenditure, free_cash_flow,
           stock_based_compensation)

  balance_sheet(ticker, period_date PK, fetched_at,
                total_assets, total_liabilities, total_debt,
                total_cash, stockholders_equity)

  price_history(ticker, date PK, fetched_at, close)

TTL constants:
  INFO_TTL           = 30 days
  FINANCIALS_TTL     =  7 days
  PRICE_TTL          =  1 day  (applied to current_price inside info)
  PRICE_HISTORY_TTL  = 30 days (historical prices don't change)
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Optional


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime (for DuckDB storage)."""
    return datetime.now(UTC).replace(tzinfo=None)

import duckdb
import pandas as pd
import yfinance as yf
from pydantic import BaseModel, Field
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

__all__ = [
    "CacheStore",
    "TickerData",
    "fetch_ticker",
    "fetch_universe",
    "fetch_risk_free_rate",
    "fetch_historical_prices",
    "INFO_TTL",
    "FINANCIALS_TTL",
    "PRICE_TTL",
    "PRICE_HISTORY_TTL",
]

logger = logging.getLogger(__name__)

# ── TTL constants ─────────────────────────────────────────────────────────────
# INFO / FINANCIALS cached for 7 days — avoids re-hammering Yahoo Finance on
# large universes (6000+ tickers). Fundamental data (P/E, balance sheet) does
# not change intra-week; re-running the screener within the same week reuses
# the local DuckDB cache entirely.
# PRICE_HISTORY cached for 7 days — historical closes are immutable intra-day.

INFO_TTL           = timedelta(days=7)   # re-fetch prices/multiples once per week
FINANCIALS_TTL     = timedelta(days=7)   # re-fetch income/balance/cashflow once per week
PRICE_TTL          = timedelta(days=7)   # re-fetch current price once per week
PRICE_HISTORY_TTL  = timedelta(days=7)   # historical closes: 7 days is plenty

# ── yfinance → schema field maps ──────────────────────────────────────────────

INFO_FIELD_MAP: dict[str, str] = {
    "currentPrice":         "current_price",
    "marketCap":            "market_cap",
    "trailingPE":           "trailing_pe",
    "priceToBook":          "price_to_book",
    "enterpriseToEbitda":   "enterprise_to_ebitda",
    "pegRatio":             "peg_ratio",
    "freeCashflow":         "free_cashflow",
    "totalDebt":            "total_debt",
    "totalCash":            "total_cash",
    "ebitda":               "ebitda",
    "sharesOutstanding":    "shares_outstanding",
    "shortName":            "short_name",
    "sector":               "sector",
    "industry":             "industry",
    "fiftyTwoWeekLow":      "week52_low",
    "fiftyTwoWeekHigh":     "week52_high",
    "dividendYield":        "dividend_yield",
    "dividendRate":         "dividend_rate",
    "beta":                 "beta",
    "returnOnEquity":       "roe",
    "returnOnAssets":       "roa",
    "grossMargins":         "gross_margin",
    "operatingMargins":     "operating_margin",
    # Sub-Task 3 — Short Interest Contrarian
    "shortPercentOfFloat":  "short_float_pct",
    # Sub-Task 2 — Momentum (52w price return proxy)
    "52WeekChange":         "price_momentum_12m",
}

# yfinance cashflow row-name variants to handle version differences
_FCF_ROW_NAMES = ("Free Cash Flow", "freeCashflow", "FreeCashFlow")
_OPCF_ROW_NAMES = ("Operating Cash Flow", "operatingCashflow", "Total Cash From Operating Activities")
_CAPEX_ROW_NAMES = ("Capital Expenditure", "capitalExpenditures", "Capital Expenditures")
_SBC_ROW_NAMES = ("Stock Based Compensation", "stockBasedCompensation")

_FINANCIALS_ROW_MAP = {
    "Total Revenue": "total_revenue",
    "Gross Profit": "gross_profit",
    "EBIT": "ebit",
    "Net Income": "net_income",
}
_BS_ROW_MAP = {
    "Total Assets":                             "total_assets",
    "Total Liabilities Net Minority Interest":  "total_liabilities",
    "Total Debt":                               "total_debt",
    "Cash And Cash Equivalents":                "total_cash",
    "Stockholders Equity":                      "stockholders_equity",
    "Common Stock Equity":                      "stockholders_equity",  # fallback key
    # Sub-Task 1 — NCAV needs current assets and current liabilities
    "Current Assets":                           "current_assets",
    "Total Current Assets":                     "current_assets",       # fallback key
    "Current Liabilities":                      "current_liabilities",
    "Total Current Liabilities Net Minority Interest": "current_liabilities",  # fallback
}


# ── Pydantic models ───────────────────────────────────────────────────────────

class TickerData(BaseModel):
    """Raw financial data for a single ticker as extracted from yfinance."""

    ticker: str
    info: dict[str, Any] = Field(default_factory=dict)
    # Each element is one annual period: {"period_date": "2023-12-31", "free_cash_flow": 12345, "stock_based_compensation": 123, ...}
    cashflow: list[dict[str, Any]] = Field(default_factory=list)
    financials: list[dict[str, Any]] = Field(default_factory=list)
    balance_sheet: list[dict[str, Any]] = Field(default_factory=list)


# ── DuckDB schema SQL ─────────────────────────────────────────────────────────

_CREATE_TICKER_INFO = """
CREATE TABLE IF NOT EXISTS ticker_info (
    ticker                TEXT PRIMARY KEY,
    fetched_at            TIMESTAMP NOT NULL,
    current_price         DOUBLE,
    market_cap            DOUBLE,
    trailing_pe           DOUBLE,
    price_to_book         DOUBLE,
    enterprise_to_ebitda  DOUBLE,
    peg_ratio             DOUBLE,
    free_cashflow         DOUBLE,
    total_debt            DOUBLE,
    total_cash            DOUBLE,
    ebitda                DOUBLE,
    shares_outstanding    DOUBLE,
    short_name            TEXT,
    sector                TEXT,
    industry              TEXT,
    week52_low            DOUBLE,
    week52_high           DOUBLE,
    dividend_yield        DOUBLE,
    dividend_rate         DOUBLE,
    beta                  DOUBLE,
    roe                   DOUBLE,
    roa                   DOUBLE,
    gross_margin          DOUBLE,
    operating_margin      DOUBLE
);
"""

_CREATE_MACRO_DATA = """
CREATE TABLE IF NOT EXISTS macro_data (
    key        TEXT PRIMARY KEY,
    value      DOUBLE NOT NULL,
    fetched_at TIMESTAMP NOT NULL
);
"""

_CREATE_PRICE_HISTORY = """
CREATE TABLE IF NOT EXISTS price_history (
    ticker     TEXT NOT NULL,
    date       TEXT NOT NULL,
    close      DOUBLE NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, date)
);
"""

_CREATE_CASHFLOW = """
CREATE TABLE IF NOT EXISTS cashflow (
    ticker                   TEXT NOT NULL,
    period_date              TEXT NOT NULL,
    fetched_at               TIMESTAMP NOT NULL,
    operating_cashflow       DOUBLE,
    capital_expenditure      DOUBLE,
    free_cash_flow           DOUBLE,
    stock_based_compensation DOUBLE,
    PRIMARY KEY (ticker, period_date)
);
"""

_CREATE_FINANCIALS = """
CREATE TABLE IF NOT EXISTS financials (
    ticker        TEXT NOT NULL,
    period_date   TEXT NOT NULL,
    fetched_at    TIMESTAMP NOT NULL,
    total_revenue DOUBLE,
    gross_profit  DOUBLE,
    ebit          DOUBLE,
    net_income    DOUBLE,
    PRIMARY KEY (ticker, period_date)
);
"""

_CREATE_BALANCE_SHEET = """
CREATE TABLE IF NOT EXISTS balance_sheet (
    ticker               TEXT NOT NULL,
    period_date          TEXT NOT NULL,
    fetched_at           TIMESTAMP NOT NULL,
    total_assets         DOUBLE,
    total_liabilities    DOUBLE,
    total_debt           DOUBLE,
    total_cash           DOUBLE,
    stockholders_equity  DOUBLE,
    current_assets       DOUBLE,
    current_liabilities  DOUBLE,
    PRIMARY KEY (ticker, period_date)
);
"""

_CREATE_SCORE_HISTORY = """
CREATE TABLE IF NOT EXISTS score_history (
    ticker          VARCHAR NOT NULL,
    run_date        VARCHAR NOT NULL,
    profile         VARCHAR NOT NULL,
    composite_score DOUBLE,
    mos_pct         DOUBLE,
    profile_fit     DOUBLE,
    fetched_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, run_date, profile)
)
"""


# ── CacheStore ────────────────────────────────────────────────────────────────

class CacheStore:
    """
    Thread-safe DuckDB-backed cache for yfinance financial data.

    Uses a single DuckDB connection shared across all threads, protected by a
    threading.Lock for writes.  DuckDB's in-process mode supports concurrent
    reads from the same connection object when no write is in progress.
    """

    def __init__(self, db_path: str) -> None:
        self._conn_obj = duckdb.connect(db_path)
        self._lock = threading.Lock()
        # Initialise schema
        self._conn_obj.execute(_CREATE_TICKER_INFO)
        self._conn_obj.execute(_CREATE_CASHFLOW)
        self._conn_obj.execute(_CREATE_FINANCIALS)
        self._conn_obj.execute(_CREATE_BALANCE_SHEET)
        self._conn_obj.execute(_CREATE_MACRO_DATA)
        self._conn_obj.execute(_CREATE_PRICE_HISTORY)
        self._conn_obj.execute(_CREATE_SCORE_HISTORY)
        # Migrate: add new columns to existing caches that pre-date this schema
        existing_cols = {
            row[0] for row in self._conn_obj.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'ticker_info'"
            ).fetchall()
        }
        for col in (
            "week52_low", "week52_high", "dividend_yield", "dividend_rate",
            "beta", "roe", "roa", "gross_margin", "operating_margin",
        ):
            if col not in existing_cols:
                self._conn_obj.execute(
                    f"ALTER TABLE ticker_info ADD COLUMN {col} DOUBLE"
                )
                logger.debug("Migrated ticker_info: added column %s", col)
        try:
            self._conn_obj.execute(
                "ALTER TABLE cashflow ADD COLUMN stock_based_compensation DOUBLE"
            )
        except Exception:
            pass

    def _conn(self) -> duckdb.DuckDBPyConnection:
        """Return the shared DuckDB connection."""
        return self._conn_obj

    # ── Info ──────────────────────────────────────────────────────────────────

    def get_info(self, ticker: str) -> Optional[dict[str, Any]]:
        """Return cached info dict if within INFO_TTL, else None."""
        with self._lock:
            cur = self._conn().execute(
                "SELECT * FROM ticker_info WHERE ticker = ?", [ticker]
            )
            row = cur.fetchone()
            if row is None:
                return None
            col_names = [d[0] for d in cur.description]
        record = dict(zip(col_names, row))
        fetched_at: datetime = record["fetched_at"]
        if not isinstance(fetched_at, datetime):
            fetched_at = datetime.fromisoformat(str(fetched_at))
        # Normalise to naive UTC for comparison (DuckDB returns naive datetimes)
        fetched_at = fetched_at.replace(tzinfo=None)
        if _utcnow() - fetched_at > INFO_TTL:
            return None
        return {v: record.get(v) for v in INFO_FIELD_MAP.values()}

    def set_info(self, ticker: str, payload: dict[str, Any]) -> None:
        """Upsert info record into ticker_info."""
        now = _utcnow()
        with self._lock:
            self._conn_obj.execute(
                """
                INSERT INTO ticker_info
                    (ticker, fetched_at, current_price, market_cap, trailing_pe,
                     price_to_book, enterprise_to_ebitda, peg_ratio, free_cashflow,
                     total_debt, total_cash, ebitda, shares_outstanding,
                     short_name, sector, industry, week52_low, week52_high,
                     dividend_yield, dividend_rate,
                     beta, roe, roa, gross_margin, operating_margin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker) DO UPDATE SET
                    fetched_at            = EXCLUDED.fetched_at,
                    current_price         = EXCLUDED.current_price,
                    market_cap            = EXCLUDED.market_cap,
                    trailing_pe           = EXCLUDED.trailing_pe,
                    price_to_book         = EXCLUDED.price_to_book,
                    enterprise_to_ebitda  = EXCLUDED.enterprise_to_ebitda,
                    peg_ratio             = EXCLUDED.peg_ratio,
                    free_cashflow         = EXCLUDED.free_cashflow,
                    total_debt            = EXCLUDED.total_debt,
                    total_cash            = EXCLUDED.total_cash,
                    ebitda                = EXCLUDED.ebitda,
                    shares_outstanding    = EXCLUDED.shares_outstanding,
                    short_name            = EXCLUDED.short_name,
                    sector                = EXCLUDED.sector,
                    industry              = EXCLUDED.industry,
                    week52_low            = EXCLUDED.week52_low,
                    week52_high           = EXCLUDED.week52_high,
                    dividend_yield        = EXCLUDED.dividend_yield,
                    dividend_rate         = EXCLUDED.dividend_rate,
                    beta                  = EXCLUDED.beta,
                    roe                   = EXCLUDED.roe,
                    roa                   = EXCLUDED.roa,
                    gross_margin          = EXCLUDED.gross_margin,
                    operating_margin      = EXCLUDED.operating_margin
                """,
                [
                    ticker, now,
                    payload.get("current_price"), payload.get("market_cap"),
                    payload.get("trailing_pe"), payload.get("price_to_book"),
                    payload.get("enterprise_to_ebitda"), payload.get("peg_ratio"),
                    payload.get("free_cashflow"), payload.get("total_debt"),
                    payload.get("total_cash"), payload.get("ebitda"),
                    payload.get("shares_outstanding"), payload.get("short_name"),
                    payload.get("sector"), payload.get("industry"),
                    payload.get("week52_low"), payload.get("week52_high"),
                    payload.get("dividend_yield"), payload.get("dividend_rate"),
                    payload.get("beta"), payload.get("roe"),
                    payload.get("roa"), payload.get("gross_margin"),
                    payload.get("operating_margin"),
                ],
            )

    # ── Financial statements ──────────────────────────────────────────────────

    def get_financials(self, ticker: str, statement: str) -> Optional[list[dict[str, Any]]]:
        """
        Return cached rows for the given statement if within FINANCIALS_TTL.

        Args:
            ticker:    Ticker symbol.
            statement: One of 'cashflow', 'financials', 'balance_sheet'.

        Returns:
            List of row dicts (one per annual period), or None if stale/absent.
        """
        table = self._validated_table(statement)
        with self._lock:
            cur = self._conn().execute(
                f"SELECT * FROM {table} WHERE ticker = ? ORDER BY period_date DESC",
                [ticker],
            )
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description] if rows else []
        if not rows:
            return None
        records = [dict(zip(col_names, r)) for r in rows]
        # Check TTL against the most-recently fetched record
        fetched_at = records[0].get("fetched_at")
        if fetched_at is None:
            return None
        if not isinstance(fetched_at, datetime):
            fetched_at = datetime.fromisoformat(str(fetched_at))
        # Normalise to naive UTC for comparison (DuckDB returns naive datetimes)
        fetched_at = fetched_at.replace(tzinfo=None)
        if _utcnow() - fetched_at > FINANCIALS_TTL:
            return None
        return records

    def set_financials(
        self, ticker: str, statement: str, rows: list[dict[str, Any]]
    ) -> None:
        """Upsert financial statement rows."""
        table = self._validated_table(statement)
        now = _utcnow()
        columns, placeholders = self._table_columns(table)
        with self._lock:
            for row in rows:
                values = [ticker, row.get("period_date"), now] + [
                    row.get(c) for c in columns[3:]  # skip ticker, period_date, fetched_at
                ]
                set_clause = ", ".join(
                    f"{c} = EXCLUDED.{c}" for c in columns if c not in ("ticker", "period_date")
                )
                self._conn_obj.execute(
                    f"""
                    INSERT INTO {table} ({', '.join(columns)})
                    VALUES ({placeholders})
                    ON CONFLICT (ticker, period_date) DO UPDATE SET {set_clause}
                    """,
                    values,
                )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _validated_table(statement: str) -> str:
        allowed = {"cashflow", "financials", "balance_sheet"}
        if statement not in allowed:
            raise ValueError(f"statement must be one of {allowed}, got {statement!r}")
        return statement

    _COLUMNS: dict[str, list[str]] = {
        "cashflow": [
            "ticker", "period_date", "fetched_at",
            "operating_cashflow", "capital_expenditure", "free_cash_flow",
            "stock_based_compensation",
        ],
        "financials": [
            "ticker", "period_date", "fetched_at",
            "total_revenue", "gross_profit", "ebit", "net_income",
        ],
        "balance_sheet": [
            "ticker", "period_date", "fetched_at",
            "total_assets", "total_liabilities", "total_debt",
            "total_cash", "stockholders_equity",
            "current_assets", "current_liabilities",
        ],
    }

    def _table_columns(self, table: str) -> tuple[list[str], str]:
        cols = self._COLUMNS[table]
        return cols, ", ".join(["?"] * len(cols))

    # ── Macro data ────────────────────────────────────────────────────────────

    def get_macro(self, key: str, ttl: timedelta) -> Optional[float]:
        """Return cached macro value if within TTL, else None."""
        with self._lock:
            row = self._conn_obj.execute(
                "SELECT value, fetched_at FROM macro_data WHERE key = ?", [key]
            ).fetchone()
        if row is None:
            return None
        value, fetched_at = row
        if not isinstance(fetched_at, datetime):
            fetched_at = datetime.fromisoformat(str(fetched_at))
        fetched_at = fetched_at.replace(tzinfo=None)
        if _utcnow() - fetched_at > ttl:
            return None
        return float(value)

    def set_macro(self, key: str, value: float) -> None:
        """Upsert a macro data value."""
        now = _utcnow()
        with self._lock:
            self._conn_obj.execute(
                """
                INSERT INTO macro_data (key, value, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    fetched_at = EXCLUDED.fetched_at
                """,
                [key, value, now],
            )

    # ── Price history ─────────────────────────────────────────────────────────

    def get_price_history(self, ticker: str, dates: list[str]) -> dict[str, float]:
        """
        Return cached close prices for the requested dates that are still fresh.

        Args:
            ticker: Ticker symbol.
            dates:  List of date strings (YYYY-MM-DD).

        Returns:
            Dict of {date_str: close_price} for all cached & fresh entries.
            Dates that are missing or stale are omitted.
        """
        if not dates:
            return {}
        cutoff = _utcnow() - PRICE_HISTORY_TTL
        placeholders = ", ".join(["?"] * len(dates))
        with self._lock:
            rows = self._conn_obj.execute(
                f"SELECT date, close FROM price_history "
                f"WHERE ticker = ? AND date IN ({placeholders}) AND fetched_at >= ?",
                [ticker, *dates, cutoff],
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def set_price_history(self, ticker: str, prices: dict[str, float]) -> None:
        """
        Upsert close prices for the given ticker.

        Args:
            ticker: Ticker symbol.
            prices: Dict of {date_str: close_price}.
        """
        if not prices:
            return
        now = _utcnow()
        with self._lock:
            for date_str, close in prices.items():
                self._conn_obj.execute(
                    """
                    INSERT INTO price_history (ticker, date, close, fetched_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        close      = EXCLUDED.close,
                        fetched_at = EXCLUDED.fetched_at
                    """,
                    [ticker, date_str, close, now],
                )


    # ── Score history ─────────────────────────────────────────────────────────

    def append_score_history(self, rows: list[dict]) -> None:
        """Upsert score history rows. Each row: ticker, run_date, profile, composite_score, mos_pct, profile_fit."""
        now_ts = _utcnow()
        with self._lock:
            for r in rows:
                self._conn_obj.execute(
                    """
                    INSERT OR REPLACE INTO score_history
                        (ticker, run_date, profile, composite_score, mos_pct, profile_fit, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        r["ticker"], r["run_date"], r["profile"],
                        r.get("composite_score"), r.get("mos_pct"),
                        r.get("profile_fit"), now_ts,
                    ],
                )


# ── DataFrame → row-dict conversion ──────────────────────────────────────────

def _orient_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure DataFrame has dates as the index and metric names as columns.

    yfinance returns DataFrames with metric names as the index (rows) and
    date columns — we transpose so rows = dates, columns = metrics.
    Detects orientation: if the index is already datetime-like, no transpose needed.
    """
    import warnings
    if pd.api.types.is_datetime64_any_dtype(df.index):
        return df          # already date-indexed (e.g. test fixtures)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pd.to_datetime(df.index, errors="raise")
        return df          # index is parseable as dates
    except Exception:
        return df.T        # index is metric names → transpose


def _df_to_cashflow_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """
    Convert a yfinance cashflow DataFrame into a list of row dicts,
    one per annual period.  Accepts both yfinance native (metrics × dates)
    and date-indexed (dates × metrics) orientation.
    """
    if df is None or df.empty:
        return []
    df_dates = _orient_df(df)  # rows = dates, columns = metrics
    rows: list[dict[str, Any]] = []
    for date_idx, row in df_dates.iterrows():
        period_date = str(date_idx)[:10]  # YYYY-MM-DD
        record: dict[str, Any] = {"period_date": period_date}

        # Operating cash flow
        for name in _OPCF_ROW_NAMES:
            if name in df_dates.columns:
                record["operating_cashflow"] = _safe_float(row.get(name))
                break

        # Capital expenditure
        for name in _CAPEX_ROW_NAMES:
            if name in df_dates.columns:
                record["capital_expenditure"] = _safe_float(row.get(name))
                break

        for name in _SBC_ROW_NAMES:
            if name in df_dates.columns:
                record["stock_based_compensation"] = _safe_float(row.get(name))
                break

        # Free cash flow — try explicit row first, then derive
        fcf: Optional[float] = None
        for name in _FCF_ROW_NAMES:
            if name in df_dates.columns:
                fcf = _safe_float(row.get(name))
                break
        if fcf is None:
            opcf = record.get("operating_cashflow")
            capex = record.get("capital_expenditure")
            if opcf is not None and capex is not None:
                fcf = opcf - abs(capex)
        record["free_cash_flow"] = fcf

        rows.append(record)
    return rows


def _df_to_financials_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    df_dates = _orient_df(df)
    rows: list[dict[str, Any]] = []
    for date_idx, row in df_dates.iterrows():
        period_date = str(date_idx)[:10]
        record: dict[str, Any] = {"period_date": period_date}
        for yf_key, col in _FINANCIALS_ROW_MAP.items():
            if yf_key in df_dates.columns and col not in record:
                record[col] = _safe_float(row.get(yf_key))
        rows.append(record)
    return rows


def _df_to_balance_sheet_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    df_dates = _orient_df(df)
    rows: list[dict[str, Any]] = []
    for date_idx, row in df_dates.iterrows():
        period_date = str(date_idx)[:10]
        record: dict[str, Any] = {"period_date": period_date}
        for yf_key, col in _BS_ROW_MAP.items():
            if yf_key in df_dates.columns and col not in record:
                record[col] = _safe_float(row.get(yf_key))
        rows.append(record)
    return rows


def _safe_float(value: Any) -> Optional[float]:
    """Convert a value to float, returning None on failure."""
    try:
        f = float(value)
        import math
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


# Text fields that must not be coerced to float
_TEXT_INFO_FIELDS = frozenset({"shortName", "sector", "industry"})


def _extract_info(raw_info: dict[str, Any]) -> dict[str, Any]:
    """Map yfinance info dict keys to our schema column names."""
    return {schema_col: _safe_float(raw_info.get(yf_key))
            if yf_key not in _TEXT_INFO_FIELDS
            else raw_info.get(yf_key)
            for yf_key, schema_col in INFO_FIELD_MAP.items()}


# ── Risk-free rate fetch ──────────────────────────────────────────────────────

def fetch_risk_free_rate(cache: CacheStore) -> float:
    """Fetch current US 10Y Treasury yield (^TNX) via yfinance. Cache for 1 day.

    Returns the yield as a decimal (e.g. 0.045 for 4.5%).
    Falls back to 4.5% if the API call fails.
    """
    cached = cache.get_macro("us_10y_yield", timedelta(days=1))
    if cached is not None:
        return cached / 100.0  # stored as percent (e.g. 4.5), convert to decimal

    try:
        tkr = yf.Ticker("^TNX")
        price = tkr.info.get("regularMarketPrice") or tkr.fast_info.get("lastPrice")
        if price and price > 0:
            rate = price / 100.0
            cache.set_macro("us_10y_yield", price)  # store as percent
            return rate
    except Exception:
        pass
    return 0.045  # fallback: 4.5%


# ── Historical price fetch ────────────────────────────────────────────────────

def fetch_historical_prices(
    tickers: list[str],
    dates: list[str],
    cache: CacheStore,
) -> dict[str, dict[str, float]]:
    """
    Fetch historical close prices for a list of tickers on specific dates.

    Cache-first: checks DuckDB cache per ticker/date (TTL 30 days).
    Batch-fetches missing dates via ``yfinance.download`` for each ticker.

    Args:
        tickers: List of ticker symbols (including benchmark, e.g. "^GSPC").
        dates:   List of date strings ("YYYY-MM-DD") needed.
        cache:   Shared CacheStore instance.

    Returns:
        ``{ticker: {date_str: close_price}}``.
        Tickers with no data are omitted from the result with a warning.
    """
    from datetime import date as date_type
    import warnings

    if not tickers or not dates:
        return {}

    sorted_dates = sorted(dates)
    # Expand download window by 5 days on each side so that dates that fall on
    # weekends / public holidays (e.g. Jan 2 = Saturday) are bracketed by real
    # trading days that yfinance will return.
    start_dt = (
        datetime.strptime(sorted_dates[0], "%Y-%m-%d") - timedelta(days=5)
    ).strftime("%Y-%m-%d")
    # end is exclusive in yfinance download — add 5 days to capture the last date
    end_dt_obj = datetime.strptime(sorted_dates[-1], "%Y-%m-%d") + timedelta(days=5)
    end_dt = end_dt_obj.strftime("%Y-%m-%d")

    result: dict[str, dict[str, float]] = {}

    for ticker in tickers:
        cached = cache.get_price_history(ticker, dates)
        missing_dates = [d for d in dates if d not in cached]

        if missing_dates:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    df = yf.download(
                        ticker,
                        start=start_dt,
                        end=end_dt,
                        auto_adjust=True,
                        progress=False,
                    )
            except Exception as exc:
                logger.warning("Failed to download price history for %s: %s", ticker, exc)
                df = pd.DataFrame()

            if df is not None and not df.empty:
                # Flatten MultiIndex columns if present (single ticker returns plain columns)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                close_col = next(
                    (c for c in df.columns if str(c).lower() in ("close", "adj close")),
                    None,
                )
                fetched: dict[str, float] = {}
                if close_col is not None:
                    for idx, price_val in df[close_col].items():
                        date_str = str(idx)[:10]
                        f = _safe_float(price_val)
                        if f is not None:
                            fetched[date_str] = f

                if fetched:
                    cache.set_price_history(ticker, fetched)
                    # Find closest available trading day for each requested date.
                    # Searches both prior AND next available dates and picks the
                    # nearest one within ±5 calendar days (covers weekends + most
                    # public holidays such as Jan 1 / Jan 2 scenarios).
                    available = sorted(fetched.keys())
                    for d in missing_dates:
                        if d in fetched:
                            cached[d] = fetched[d]
                        else:
                            d_dt = datetime.strptime(d, "%Y-%m-%d")
                            prior = [a for a in available if a <= d]
                            after = [a for a in available if a > d]
                            best: Optional[str] = None
                            best_gap = 999
                            if prior:
                                gap = (d_dt - datetime.strptime(prior[-1], "%Y-%m-%d")).days
                                if gap <= 5:
                                    best, best_gap = prior[-1], gap
                            if after:
                                gap = (datetime.strptime(after[0], "%Y-%m-%d") - d_dt).days
                                if gap <= 5 and gap < best_gap:
                                    best = after[0]
                            if best is not None:
                                cached[d] = fetched[best]
                            else:
                                logger.debug(
                                    "No trading day within ±5 days of %s for %s.",
                                    d, ticker,
                                )
            else:
                logger.warning(
                    "No price history data returned for %s. Skipping.", ticker
                )
        else:
            # All dates cached — also fill in any nearest-date lookups for dates
            # that may have been stored under a slightly different trading-day date.
            pass

        if cached:
            result[ticker] = cached

    return result


# ── Single-ticker fetch with retry ───────────────────────────────────────────

def _fetch_with_retry(
    ticker: str,
    cache: CacheStore,
    max_retries: int = 3,
) -> Optional[TickerData]:
    """
    Attempt to fetch data for *ticker* with exponential backoff.

    Returns TickerData on success, None if all retries are exhausted or the
    ticker has fundamentally no usable data.
    """
    delay = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return _fetch_once(ticker, cache)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning(
                    "Ticker %s — attempt %d/%d failed: %s. Retrying in %.0fs …",
                    ticker, attempt, max_retries, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.warning(
                    "Ticker %s — all %d attempts failed: %s. Skipping.",
                    ticker, max_retries, last_exc,
                )
    return None


def _fetch_once(ticker: str, cache: CacheStore) -> TickerData:
    """
    Cache-first fetch for a single ticker.

    Raises:
        Exception: on any yfinance or parsing error (caller handles retries).
    """
    # ── Try info from cache ───────────────────────────────────────────────────
    cached_info = cache.get_info(ticker)

    # ── Try financial statements from cache ───────────────────────────────────
    cached_cf = cache.get_financials(ticker, "cashflow")
    cached_fin = cache.get_financials(ticker, "financials")
    cached_bs = cache.get_financials(ticker, "balance_sheet")

    if cached_info and cached_cf and cached_fin and cached_bs:
        logger.debug("Cache hit for %s.", ticker)
        return TickerData(
            ticker=ticker,
            info=cached_info,
            cashflow=cached_cf,
            financials=cached_fin,
            balance_sheet=cached_bs,
        )

    # ── Partial or full cache miss — fetch from yfinance ─────────────────────
    logger.debug("Cache miss for %s — fetching from yfinance.", ticker)
    yticker = yf.Ticker(ticker)

    # Info
    if cached_info is None:
        raw_info = yticker.info or {}
        if not raw_info or raw_info.get("trailingPE") is None and raw_info.get("marketCap") is None:
            # Empty info usually means delisted / invalid ticker
            if not raw_info.get("shortName"):
                raise ValueError(f"No usable data for ticker {ticker!r} (possibly delisted).")
        info_payload = _extract_info(raw_info)
        cache.set_info(ticker, info_payload)
        cached_info = info_payload

    # Cashflow
    if cached_cf is None:
        cf_df = yticker.cashflow
        cf_rows = _df_to_cashflow_rows(cf_df)
        if cf_rows:
            cache.set_financials(ticker, "cashflow", cf_rows)
        cached_cf = cf_rows

    # Income statement
    if cached_fin is None:
        fin_df = yticker.financials
        fin_rows = _df_to_financials_rows(fin_df)
        if fin_rows:
            cache.set_financials(ticker, "financials", fin_rows)
        cached_fin = fin_rows

    # Balance sheet
    if cached_bs is None:
        bs_df = yticker.balance_sheet
        bs_rows = _df_to_balance_sheet_rows(bs_df)
        if bs_rows:
            cache.set_financials(ticker, "balance_sheet", bs_rows)
        cached_bs = bs_rows

    return TickerData(
        ticker=ticker,
        info=cached_info,
        cashflow=cached_cf,
        financials=cached_fin,
        balance_sheet=cached_bs,
    )


def fetch_ticker(ticker: str, cache: CacheStore) -> Optional[TickerData]:
    """
    Fetch data for a single ticker (cache-first).

    Returns:
        TickerData on success, or None if the ticker is unusable.
    """
    return _fetch_with_retry(ticker, cache)


# ── Batch fetch ───────────────────────────────────────────────────────────────

def fetch_universe(
    tickers: list[str],
    cache: CacheStore,
    max_workers: int = 8,
    requests_per_second: float = 2.0,
) -> tuple[list[TickerData], list[str]]:
    """
    Fetch financial data for all tickers concurrently.

    All futures are submitted immediately; rate limiting is applied *inside*
    each worker only when a real network call is required — cache hits bypass
    the rate limiter and return instantly.

    Args:
        tickers:             List of ticker symbols.
        cache:               Shared CacheStore instance.
        max_workers:         Thread-pool size.
        requests_per_second: Max yfinance API calls per second.

    Returns:
        Tuple of (successful TickerData list, failed ticker list).
    """
    results: list[TickerData] = []
    failed: list[str] = []

    # Shared rate limiter — applied only for real yfinance calls
    _rps_lock = threading.Lock()
    _last_request_time: list[float] = [0.0]
    min_interval = 1.0 / max(requests_per_second, 0.1)

    def _throttled_fetch(ticker: str) -> Optional[TickerData]:
        # Cheap full-cache check — no throttle needed
        cached_info = cache.get_info(ticker)
        cached_cf = cache.get_financials(ticker, "cashflow")
        cached_fin = cache.get_financials(ticker, "financials")
        cached_bs = cache.get_financials(ticker, "balance_sheet")
        if cached_info and cached_cf and cached_fin and cached_bs:
            return TickerData(
                ticker=ticker,
                info=cached_info,
                cashflow=cached_cf,
                financials=cached_fin,
                balance_sheet=cached_bs,
            )
        # Need to hit the network — acquire rate limit slot
        with _rps_lock:
            elapsed = time.monotonic() - _last_request_time[0]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            _last_request_time[0] = time.monotonic()
        return fetch_ticker(ticker, cache)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Fetching[/bold blue] {task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    )

    with progress:
        task = progress.add_task("tickers …", total=len(tickers))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_ticker = {
                executor.submit(_throttled_fetch, ticker): ticker
                for ticker in tickers
            }
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                progress.update(task, advance=1, description=ticker)
                try:
                    data = future.result()
                    if data is not None:
                        results.append(data)
                    else:
                        failed.append(ticker)
                except Exception as exc:
                    logger.error("Unexpected error for %s: %s", ticker, exc)
                    failed.append(ticker)

    logger.info(
        "Fetch complete — success: %d, failed: %d / %d total.",
        len(results), len(failed), len(tickers),
    )
    return results, failed
