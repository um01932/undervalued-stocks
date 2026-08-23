"""
universe.py — Stock universe assembly.

Provides functions to retrieve ticker lists from:
  - S&P 500 (Wikipedia scrape)
  - NASDAQ-100 (Wikipedia scrape)
  - Russell 2000 (Wikipedia scrape + iShares fallback)
  - Euro Stoxx 50 (Wikipedia scrape + hardcoded fallback)
  - BET Romania (hardcoded static list)
  - World (bundled global_tickers.csv — zero network call)
  - Custom CSV file supplied by the user

Entry point:
    get_universe(source=UniverseSource.WORLD, csv_path=None) -> list[str]

Tickers are normalised: whitespace stripped, dots replaced with hyphens
(yfinance convention, e.g. BRK-B instead of BRK.B), deduped, sorted.

CLI refresh:
    python src/universe.py --refresh-world
    Re-scrapes S&P 500 + NASDAQ-100 and appends any new tickers to
    data/global_tickers.csv (existing entries preserved).
"""

from __future__ import annotations

import argparse
import logging
from enum import StrEnum
from pathlib import Path
from typing import Optional

import pandas as pd

__all__ = [
    "UniverseSource",
    "get_sp500_tickers",
    "get_nasdaq100_tickers",
    "get_dow30_tickers",
    "get_russell2000_tickers",
    "get_eurostoxx50_tickers",
    "get_bet_tickers",
    "get_world_tickers",
    "get_tickers_from_csv",
    "get_multi_universe",
    "get_universe",
]

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).parent.parent / "data"
_GLOBAL_CSV = _DATA_DIR / "global_tickers.csv"

_SP500_URL      = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NASDAQ100_URL  = "https://en.wikipedia.org/wiki/Nasdaq-100"
_DOW30_URL      = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
_RUSSELL2000_URL = "https://en.wikipedia.org/wiki/Russell_2000_Index"
_EUROSTOXX50_URL = "https://en.wikipedia.org/wiki/Euro_Stoxx_50"

# Hardcoded Dow 30 as a reliable fallback (updated August 2026)
_DOW30_FALLBACK = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX",
    "DIS", "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD",
    "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "TRV", "UNH", "V", "VZ", "WMT",
]

# Hardcoded Russell 2000 fallback — ~50 well-known small-cap members (August 2026)
_RUSSELL2000_FALLBACK = [
    "ACLS", "ACMR", "AEIS", "AGIO", "AGYS", "ALRM", "AMN", "AMSF", "AMWD",
    "APAM", "ARCO", "ARLO", "ATRC", "BOOT", "CALM", "CATO", "CENT", "CENTA",
    "CHCO", "CODI", "CONN", "CPRX", "DAKT", "DCOM", "DIOD", "EFC", "ENVA",
    "EVRI", "FBIZ", "FIVE", "FLGT", "FORM", "GFF", "GRWG", "HCSG", "HIFS",
    "HNI", "HOPE", "HTLD", "HUBG", "IIIV", "INVA", "IPAR", "JBSS", "KFRC",
    "KINS", "LAKE", "LANC", "LAWS", "LCNB",
]

# Hardcoded Euro Stoxx 50 fallback (August 2026 constituents, yfinance suffix)
_EUROSTOXX50_FALLBACK = [
    "ABI.BR", "AD.AS", "ADS.DE", "AI.PA", "AIR.PA", "ALV.DE", "ASML.AS",
    "AXA.PA", "BAS.DE", "BAYN.DE", "BBVA.MC", "BMW.DE", "BNP.PA", "CRH.IR",
    "CS.PA", "DG.PA", "DTE.DE", "ENEL.MI", "ENI.MI", "EssilorLuxottica.PA",
    "EL.PA", "FLTR.IR", "FME.DE", "FRE.DE", "GS.MI", "IBE.MC", "IFX.DE",
    "INGA.AS", "ISP.MI", "ITX.MC", "KER.PA", "LIN.DE", "MC.PA", "MBG.DE",
    "ML.PA", "MRK.DE", "MUV2.DE", "NOKIA.HE", "OR.PA", "ORA.PA", "PHIA.AS",
    "PRX.AS", "RMS.PA", "SAN.MC", "SAN.PA", "SAP.DE", "SGO.PA", "SIE.DE",
    "TTE.PA", "UCG.MI",
]

# Hardcoded BET Romania tickers — static list (BET index, ~20 liquid members)
_BET_TICKERS = [
    "BRD.RO", "TLV.RO", "SNP.RO", "SNG.RO", "FP.RO", "TGN.RO",
    "COTE.RO", "BVB.RO", "M.RO", "EL.RO", "SNN.RO", "TEL.RO",
    "DIGI.RO", "ONE.RO", "TRP.RO", "WINE.RO", "TRANSELM.RO", "AQ.RO",
]


# ── Enum ──────────────────────────────────────────────────────────────────────

class UniverseSource(StrEnum):
    SP500       = "sp500"
    NASDAQ100   = "nasdaq100"
    DOW30       = "dow30"
    RUSSELL2000 = "russell2000"
    EUROSTOXX50 = "eurostoxx50"
    BET         = "bet"
    WORLD       = "world"
    CUSTOM      = "custom"
    MULTI       = "multi"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise(tickers: list[str]) -> list[str]:
    """Strip whitespace, replace dots with hyphens, dedup, sort."""
    normalised = {t.strip().replace(".", "-") for t in tickers if t and t.strip()}
    return sorted(normalised)


# ── Helpers ───────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def _fetch_html(url: str) -> str:
    """Fetch URL with a browser User-Agent to avoid 403 blocks."""
    import requests as _requests
    resp = _requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


# ── Per-source functions ───────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    """
    Scrape the current S&P 500 constituent list from Wikipedia.

    Returns:
        Sorted, normalised list of ticker symbols.

    Raises:
        RuntimeError: if the Wikipedia page cannot be parsed.
    """
    logger.info("Fetching S&P 500 tickers from Wikipedia …")
    try:
        import io as _io
        html = _fetch_html(_SP500_URL)
        tables = pd.read_html(_io.StringIO(html), attrs={"id": "constituents"})
        df = tables[0]
        tickers: list[str] = df["Symbol"].dropna().tolist()
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch S&P 500 tickers: {exc}") from exc
    result = _normalise(tickers)
    logger.info("S&P 500: %d tickers retrieved.", len(result))
    return result


def get_nasdaq100_tickers() -> list[str]:
    """
    Scrape the current NASDAQ-100 constituent list from Wikipedia.

    Returns:
        Sorted, normalised list of ticker symbols.

    Raises:
        RuntimeError: if the Wikipedia page cannot be parsed.
    """
    logger.info("Fetching NASDAQ-100 tickers from Wikipedia …")
    try:
        import io as _io
        html = _fetch_html(_NASDAQ100_URL)
        tables = pd.read_html(_io.StringIO(html))
        # The constituents table is the first table that has a 'Ticker' column.
        df: Optional[pd.DataFrame] = None
        for table in tables:
            cols = [str(c).strip() for c in table.columns]
            if "Ticker" in cols:
                df = table
                break
        if df is None:
            raise ValueError("Could not locate a table with a 'Ticker' column.")
        tickers: list[str] = df["Ticker"].dropna().tolist()
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch NASDAQ-100 tickers: {exc}") from exc
    result = _normalise(tickers)
    logger.info("NASDAQ-100: %d tickers retrieved.", len(result))
    return result


def get_russell2000_tickers() -> list[str]:
    """
    Return the current Russell 2000 constituent list.

    Primary: scrapes the Wikipedia Russell 2000 Index page looking for any
    table that has a 'Ticker' or 'Symbol' column with >100 rows.
    Fallback: returns the hardcoded _RUSSELL2000_FALLBACK list of ~50 names.

    Returns:
        Sorted, normalised list of ticker symbols.
    """
    logger.info("Fetching Russell 2000 tickers …")
    try:
        import io as _io
        html = _fetch_html(_RUSSELL2000_URL)
        tables = pd.read_html(_io.StringIO(html))
        for table in tables:
            cols = [str(c).strip() for c in table.columns]
            for col in ("Ticker", "Symbol", "ticker", "symbol"):
                if col in cols and len(table) > 100:
                    tickers: list[str] = table[col].dropna().tolist()
                    result = _normalise(tickers)
                    if len(result) > 100:
                        logger.info("Russell 2000: %d tickers from Wikipedia.", len(result))
                        return result
        raise ValueError("No large-enough table with Ticker/Symbol column found.")
    except Exception as exc:
        logger.warning(
            "Russell 2000 Wikipedia scrape failed (%s) — using hardcoded fallback list.", exc
        )
        result = _normalise(_RUSSELL2000_FALLBACK)
        logger.info("Russell 2000: %d tickers loaded from fallback.", len(result))
        return result


def get_eurostoxx50_tickers() -> list[str]:
    """
    Return the current Euro Stoxx 50 constituent list.

    Primary: scrapes the Wikipedia Euro Stoxx 50 page looking for a table
    that has a 'Ticker' or 'Symbol' column with ~50 rows.
    Fallback: returns the hardcoded _EUROSTOXX50_FALLBACK list.

    Returns:
        Sorted, normalised list of ticker symbols (with exchange suffix, e.g. 'ASML.AS').
    """
    logger.info("Fetching Euro Stoxx 50 tickers …")
    try:
        import io as _io
        html = _fetch_html(_EUROSTOXX50_URL)
        tables = pd.read_html(_io.StringIO(html))
        for table in tables:
            cols = [str(c).strip() for c in table.columns]
            for col in ("Ticker", "Symbol", "ticker", "symbol"):
                if col in cols and 30 <= len(table) <= 70:
                    tickers: list[str] = table[col].dropna().tolist()
                    result = _normalise(tickers)
                    if 30 <= len(result) <= 70:
                        logger.info("Euro Stoxx 50: %d tickers from Wikipedia.", len(result))
                        return result
        raise ValueError("No ~50-row table with Ticker/Symbol column found.")
    except Exception as exc:
        logger.warning(
            "Euro Stoxx 50 Wikipedia scrape failed (%s) — using hardcoded fallback list.", exc
        )
        result = _normalise(_EUROSTOXX50_FALLBACK)
        logger.info("Euro Stoxx 50: %d tickers loaded from fallback.", len(result))
        return result


def get_bet_tickers() -> list[str]:
    """
    Return BET (Bucharest Exchange Trading) index tickers.

    The BET index is small (~18-25 liquid members) and not reliably scraped
    from a public URL, so a hardcoded static list is used.

    Returns:
        Sorted, normalised list of ticker symbols (e.g. 'BRD.RO').
    """
    result = _normalise(_BET_TICKERS)
    logger.info("BET: %d tickers loaded from static list.", len(result))
    return result


def get_dow30_tickers() -> list[str]:
    """
    Return the current Dow Jones Industrial Average 30 constituents.

    Tries to scrape the live list from Wikipedia first; falls back to the
    hardcoded _DOW30_FALLBACK list if the page structure cannot be parsed.

    Returns:
        Sorted, normalised list of 30 ticker symbols.
    """
    logger.info("Fetching Dow Jones 30 tickers …")
    try:
        import io as _io
        html = _fetch_html(_DOW30_URL)
        tables = pd.read_html(_io.StringIO(html))
        # Look for a table that has a 'Symbol' or 'Ticker' column with ~30 rows
        for table in tables:
            cols = [str(c).strip() for c in table.columns]
            for col in ("Symbol", "Ticker", "symbol", "ticker"):
                if col in cols and 25 <= len(table) <= 35:
                    tickers: list[str] = table[col].dropna().tolist()
                    result = _normalise(tickers)
                    if 25 <= len(result) <= 35:
                        logger.info("Dow 30: %d tickers retrieved from Wikipedia.", len(result))
                        return result
        raise ValueError("Could not locate a ~30-row table with Symbol/Ticker column.")
    except Exception as exc:
        logger.warning(
            "Dow 30 Wikipedia scrape failed (%s) — using hardcoded fallback list.", exc
        )
        result = _normalise(_DOW30_FALLBACK)
        logger.info("Dow 30: %d tickers loaded from fallback.", len(result))
        return result


def get_world_tickers() -> list[str]:
    """
    Load the bundled global ticker list from data/global_tickers.csv.

    No network call is made.  The CSV is resolved relative to this module's
    parent directory so it works regardless of the current working directory.

    Returns:
        Sorted, normalised list of ticker symbols.

    Raises:
        FileNotFoundError: if global_tickers.csv does not exist.
        ValueError: if the CSV has no 'ticker' column.
    """
    if not _GLOBAL_CSV.exists():
        raise FileNotFoundError(
            f"Global ticker file not found: {_GLOBAL_CSV}\n"
            "Run `python scripts/gen_global_tickers.py` to regenerate it."
        )
    df = pd.read_csv(_GLOBAL_CSV, dtype=str)
    # Case-insensitive column lookup
    col_map = {c.lower(): c for c in df.columns}
    if "ticker" not in col_map:
        raise ValueError(f"'ticker' column not found in {_GLOBAL_CSV}. Columns: {list(df.columns)}")
    tickers: list[str] = df[col_map["ticker"]].dropna().tolist()
    result = _normalise(tickers)
    logger.info("World universe: %d tickers loaded from %s.", len(result), _GLOBAL_CSV.name)
    return result


def get_tickers_from_csv(path: str) -> list[str]:
    """
    Load tickers from a user-supplied CSV file.

    The CSV must contain a column named 'ticker' (case-insensitive).

    Args:
        path: Path to the CSV file.

    Returns:
        Sorted, normalised list of ticker symbols.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file has no 'ticker' column.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Custom ticker file not found: {csv_path}")
    df = pd.read_csv(csv_path, dtype=str)
    col_map = {c.lower(): c for c in df.columns}
    if "ticker" not in col_map:
        raise ValueError(
            f"'ticker' column not found in {csv_path}. "
            f"Available columns: {list(df.columns)}"
        )
    tickers: list[str] = df[col_map["ticker"]].dropna().tolist()
    result = _normalise(tickers)
    logger.info("Custom CSV: %d tickers loaded from %s.", len(result), csv_path.name)
    return result


# ── Unified entry point ───────────────────────────────────────────────────────

def get_universe(
    source: UniverseSource = UniverseSource.WORLD,
    csv_path: Optional[str] = None,
) -> list[str]:
    """
    Assemble and return a list of ticker symbols for the given universe source.

    Args:
        source:   Which universe to load.  Defaults to WORLD.
        csv_path: Required when source is CUSTOM.

    Returns:
        Sorted, normalised, deduplicated list of ticker strings.

    Raises:
        ValueError: if source is CUSTOM but csv_path is not provided.
    """
    match source:
        case UniverseSource.SP500:
            return get_sp500_tickers()
        case UniverseSource.NASDAQ100:
            return get_nasdaq100_tickers()
        case UniverseSource.DOW30:
            return get_dow30_tickers()
        case UniverseSource.RUSSELL2000:
            return get_russell2000_tickers()
        case UniverseSource.EUROSTOXX50:
            return get_eurostoxx50_tickers()
        case UniverseSource.BET:
            return get_bet_tickers()
        case UniverseSource.WORLD:
            return get_world_tickers()
        case UniverseSource.MULTI:
            return get_multi_universe()
        case UniverseSource.CUSTOM:
            if not csv_path:
                raise ValueError("csv_path must be provided when source is CUSTOM.")
            return get_tickers_from_csv(csv_path)
        case _:  # pragma: no cover
            raise ValueError(f"Unknown universe source: {source!r}")


def get_multi_universe() -> list[str]:
    """
    Combine S&P 500 + NASDAQ-100 + Russell 2000 + Euro Stoxx 50 + BET Romania
    into a single deduplicated, normalised ticker list.

    Each sub-universe is fetched independently (with fallbacks on failure).
    The result is the union of all tickers, sorted and deduplicated.

    Returns:
        Sorted, normalised, deduplicated list of ticker symbols.
    """
    combined: list[str] = []
    sources = [
        ("S&P 500",      get_sp500_tickers),
        ("NASDAQ-100",   get_nasdaq100_tickers),
        ("Russell 2000", get_russell2000_tickers),
        ("Euro Stoxx 50", get_eurostoxx50_tickers),
        ("BET Romania",  get_bet_tickers),
    ]
    for name, fn in sources:
        try:
            tickers = fn()
            combined.extend(tickers)
            logger.info("Multi-universe: added %d tickers from %s.", len(tickers), name)
        except Exception as exc:
            logger.warning("Multi-universe: failed to load %s (%s) — skipping.", name, exc)

    result = _normalise(combined)
    logger.info("Multi-universe total: %d unique tickers.", len(result))
    return result


# ── CLI refresh helper ────────────────────────────────────────────────────────

def _refresh_world() -> None:
    """
    Re-scrape S&P 500 and NASDAQ-100 and merge any new tickers into
    data/global_tickers.csv, preserving existing entries.
    """
    import csv as _csv

    logger.info("Refreshing world ticker list …")

    # Load existing
    existing: dict[str, list[str]] = {}
    if _GLOBAL_CSV.exists():
        df_existing = pd.read_csv(_GLOBAL_CSV, dtype=str)
        for _, row in df_existing.iterrows():
            existing[str(row.get("ticker", "")).strip()] = row.tolist()

    new_rows: list[list[str]] = []
    for ticker in get_sp500_tickers() + get_nasdaq100_tickers():
        if ticker not in existing:
            new_rows.append([ticker, "", "US", "US"])
            existing[ticker] = [ticker, "", "US", "US"]

    if new_rows:
        with _GLOBAL_CSV.open("a", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerows(new_rows)
        logger.info("Added %d new tickers to %s.", len(new_rows), _GLOBAL_CSV.name)
    else:
        logger.info("No new tickers found; %s is already up to date.", _GLOBAL_CSV.name)

    print(f"World universe updated — total tickers: {len(existing)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="universe.py CLI helper")
    parser.add_argument(
        "--refresh-world",
        action="store_true",
        help="Re-scrape S&P 500 + NASDAQ-100 and merge new tickers into global_tickers.csv",
    )
    args = parser.parse_args()
    if args.refresh_world:
        _refresh_world()
    else:
        parser.print_help()
