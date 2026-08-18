"""
main.py — CLI entry point for the Stock Screener & Intrinsic Value Engine.

Usage (non-interactive):
    python src/main.py --universe sp500 --profile deep_value --workers 10 --export csv

Usage (interactive wizard — launched when no flags are provided):
    python src/main.py

See README.md for full argument reference.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path when running as `python src/main.py`
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich import box

from src.engine import DCFParams, ValuationResult, evaluate
from src.fetcher import CacheStore, TickerData, fetch_universe
from src.screener import ScreenerProfile, apply_profile, apply_dow30_ranking, load_profiles
from src.universe import UniverseSource, get_universe

# ── Constants ─────────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).parent.parent / "data"
_REPORTS_DIR = _DATA_DIR / "reports"
_CACHE_PATH = _DATA_DIR / "cache.duckdb"
_CONFIG_DIR = Path(__file__).parent.parent / "config"
_PROFILES_YAML = _CONFIG_DIR / "screener_profiles.yaml"

console = Console()

# ── Argument parser ───────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-screener",
        description="Stock Screener & Intrinsic Value Engine — local, parallel, worldwide.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--universe",
        choices=["sp500", "nasdaq100", "dow30", "world", "custom"],
        default="world",
        help="Stock universe to screen.",
    )
    parser.add_argument(
        "--csv-path",
        metavar="PATH",
        default=None,
        help="Path to custom ticker CSV (required when --universe custom).",
    )
    parser.add_argument(
        "--profile",
        choices=["deep_value", "buffett_quality", "high_fcf_yield"],
        default="deep_value",
        help="Screener preset to apply.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="Number of parallel fetch threads.",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=2.0,
        metavar="FLOAT",
        help="Maximum API requests per second.",
    )
    parser.add_argument(
        "--export",
        choices=["csv", "excel", "both", "none"],
        default="csv",
        help="Export format for the results.",
    )
    parser.add_argument("--dcf-growth",    type=float, default=0.05,  metavar="FLOAT", help="Annual FCF growth rate.")
    parser.add_argument("--dcf-discount",  type=float, default=0.10,  metavar="FLOAT", help="Discount rate / WACC.")
    parser.add_argument("--dcf-terminal",  type=float, default=0.025, metavar="FLOAT", help="Terminal growth rate.")
    parser.add_argument("--dcf-years",     type=int,   default=10,    metavar="N",     help="Projection horizon.")
    parser.add_argument("--dcf-exit-multiple", type=float, default=12.0, metavar="FLOAT", help="EV/EBITDA exit multiple.")
    return parser


# ── Interactive wizard ────────────────────────────────────────────────────────


def _prompt(label: str, default: str) -> str:
    """Display a prompt with a default; return default on empty input."""
    try:
        value = input(f"  {label} [{default}]: ").strip()
        return value if value else default
    except (EOFError, KeyboardInterrupt):
        return default


def interactive_wizard() -> argparse.Namespace:
    """
    Interactive parameter wizard.  Displays defaults so the user can press
    Enter to accept each one.

    Returns:
        argparse.Namespace with all required fields populated.
    """
    console.rule("[bold blue]Stock Screener — Interactive Setup[/bold blue]")
    console.print()

    universe    = _prompt("Universe (sp500/nasdaq100/dow30/world/custom)", "world")
    csv_path    = None
    if universe == "custom":
        csv_path = _prompt("Path to ticker CSV", "data/custom_tickers.csv")

    profile     = _prompt("Screener profile (deep_value/buffett_quality/high_fcf_yield)", "deep_value")
    workers     = int(_prompt("Parallel fetch threads", "8"))
    rps         = float(_prompt("Max requests per second", "2.0"))
    export_fmt  = _prompt("Export format (csv/excel/both/none)", "csv")

    console.print()
    console.rule("[dim]DCF Parameters[/dim]")
    dcf_growth   = float(_prompt("Annual FCF growth rate",  "0.05"))
    dcf_discount = float(_prompt("Discount rate / WACC",    "0.10"))
    dcf_terminal = float(_prompt("Terminal growth rate",    "0.025"))
    dcf_years    = int(_prompt("Projection years",          "10"))
    dcf_exit     = float(_prompt("EV/EBITDA exit multiple", "12.0"))

    console.print()

    return argparse.Namespace(
        universe=universe,
        csv_path=csv_path,
        profile=profile,
        workers=workers,
        rps=rps,
        export=export_fmt,
        dcf_growth=dcf_growth,
        dcf_discount=dcf_discount,
        dcf_terminal=dcf_terminal,
        dcf_years=dcf_years,
        dcf_exit_multiple=dcf_exit,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────

def _mos_style(mos: Optional[float]) -> str:
    """Return rich colour style for a Margin of Safety value."""
    if mos is None:
        return "dim"
    if mos >= 30:
        return "bold green"
    if mos >= 15:
        return "yellow"
    return "red"


def render_table(df: pd.DataFrame) -> None:
    """Render the ranked screener DataFrame as a rich terminal table."""
    if df.empty:
        console.print("[yellow]No companies passed the screener filters.[/yellow]")
        return

    table = Table(
        title="Screener Results",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        highlight=True,
    )

    def _fmt(val, decimals: int = 1) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return "—"
        return f"{val:,.{decimals}f}"

    # Detect Dow 30 ranking layout vs standard screener layout
    is_dow30 = "Rank" in df.columns

    if is_dow30:
        col_defs = [
            ("Rank",          "right", "bold"),
            ("Ticker",        "left",  "bold cyan"),
            ("Company",       "left",  ""),
            ("Sector",        "left",  "dim"),
            ("Price",         "right", ""),
            ("52w Low",       "right", "dim"),
            ("52w High",      "right", "dim"),
            ("52w Position%", "right", "bold yellow"),
            ("Mkt Cap $B",    "right", ""),
            ("P/E",           "right", ""),
            ("P/B",           "right", ""),
            ("MoS%",          "right", ""),
        ]
    else:
        col_defs = [
            ("Ticker",        "left",  "bold cyan"),
            ("Company",       "left",  ""),
            ("Sector",        "left",  "dim"),
            ("Industry",      "left",  "dim"),
            ("Price",         "right", ""),
            ("52w Low",       "right", "dim"),
            ("52w High",      "right", "dim"),
            ("52w Pos%",      "right", "yellow"),
            ("MoS%",          "right", ""),
            ("P/E",           "right", ""),
            ("P/B",           "right", ""),
            ("EV/EBITDA",     "right", ""),
            ("P/FCF",         "right", ""),
            ("NetDebt/EBITDA","right", ""),
            ("DCF GGM",       "right", "green"),
            ("DCF Exit",      "right", "green"),
            ("DCF Avg",       "right", "bold green"),
        ]

    for header, justify, style in col_defs:
        table.add_column(header, justify=justify, style=style, no_wrap=(header in ("Ticker", "Rank")))

    for _, row in df.iterrows():
        mos = row.get("MoS%")
        mos_style = _mos_style(mos)
        pos = row.get("52w Position%")
        pos_str = f"{pos:.1f}%" if pos is not None and not (isinstance(pos, float) and pd.isna(pos)) else "—"
        # Colour 52w position: green = near low (opportunity), red = near high
        if pos is not None and not (isinstance(pos, float) and pd.isna(pos)):
            pos_colour = "green" if pos < 33 else ("yellow" if pos < 66 else "red")
            pos_str = f"[{pos_colour}]{pos_str}[/{pos_colour}]"

        if is_dow30:
            table.add_row(
                str(int(row["Rank"])),
                str(row["Ticker"]),
                str(row["Company"]) if row["Company"] else "—",
                str(row["Sector"])  if row["Sector"]  else "—",
                _fmt(row["Price"], 2),
                _fmt(row.get("52w Low"), 2),
                _fmt(row.get("52w High"), 2),
                pos_str,
                _fmt(row.get("Market Cap ($B)"), 0),
                _fmt(row.get("P/E"), 1),
                _fmt(row.get("P/B"), 2),
                f"[{mos_style}]{_fmt(mos, 1)}%[/{mos_style}]",
            )
        else:
            table.add_row(
                str(row["Ticker"]),
                str(row["Company"]) if row["Company"] else "—",
                str(row["Sector"])  if row["Sector"]  else "—",
                str(row["Industry"]) if row["Industry"] else "—",
                _fmt(row["Price"], 2),
                _fmt(row.get("52w Low"), 2),
                _fmt(row.get("52w High"), 2),
                pos_str,
                f"[{mos_style}]{_fmt(mos, 1)}%[/{mos_style}]",
                _fmt(row["P/E"], 1),
                _fmt(row["P/B"], 2),
                _fmt(row["EV/EBITDA"], 1),
                _fmt(row["P/FCF"], 1),
                _fmt(row["NetDebt/EBITDA"], 2),
                _fmt(row["DCF GGM"], 2),
                _fmt(row["DCF Exit"], 2),
                _fmt(row["DCF Avg"], 2),
            )

    console.print(table)


# ── Export ────────────────────────────────────────────────────────────────────


def export_results(df: pd.DataFrame, fmt: str, profile_name: str) -> list[Path]:
    """
    Save results to data/reports/.

    Returns:
        List of paths written.
    """
    if fmt == "none" or df.empty:
        return []

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{timestamp}_{profile_name}"
    written: list[Path] = []

    if fmt in ("csv", "both"):
        csv_path = _REPORTS_DIR / f"{stem}.csv"
        df.to_csv(csv_path, index=False)
        written.append(csv_path)

    if fmt in ("excel", "both"):
        xlsx_path = _REPORTS_DIR / f"{stem}.xlsx"
        df.to_excel(xlsx_path, index=False, engine="openpyxl")
        written.append(xlsx_path)

    return written


def _save_failed(failed: list[str], profile_name: str) -> Optional[Path]:
    """Save list of failed tickers to a text file."""
    if not failed:
        return None
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _REPORTS_DIR / f"{timestamp}_{profile_name}_failed.txt"
    path.write_text("\n".join(failed), encoding="utf-8")
    return path


# ── Orchestration ─────────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> None:
    """Main pipeline: universe → fetch → evaluate → screen → display → export."""

    # ── 1. Universe ───────────────────────────────────────────────────────────
    source = UniverseSource(args.universe)
    csv_path: Optional[str] = getattr(args, "csv_path", None)

    console.print(f"\n[bold]Universe:[/bold] {source.value.upper()}")
    tickers = get_universe(source, csv_path=csv_path)
    console.print(f"[dim]{len(tickers)} tickers loaded.[/dim]\n")

    # ── 2. Fetch ──────────────────────────────────────────────────────────────
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = CacheStore(str(_CACHE_PATH))

    ticker_data_list, failed = fetch_universe(
        tickers,
        cache=cache,
        max_workers=args.workers,
        requests_per_second=args.rps,
    )

    console.print(
        f"\n[green]OK Fetched:[/green] {len(ticker_data_list)}  "
        f"[red]FAIL:[/red] {len(failed)}  "
        f"[dim]/ {len(tickers)} total[/dim]\n"
    )

    # ── 3. Evaluate ───────────────────────────────────────────────────────────
    dcf_params = DCFParams(
        growth_rate=args.dcf_growth,
        discount_rate=args.dcf_discount,
        terminal_growth=args.dcf_terminal,
        projection_years=args.dcf_years,
        exit_multiple=args.dcf_exit_multiple,
    )

    valuation_results: list[ValuationResult] = []
    for td in ticker_data_list:
        try:
            result = evaluate(td, dcf_params)
            valuation_results.append(result)
        except Exception as exc:
            logging.warning("Evaluation failed for %s: %s", td.ticker, exc)

    ok_count       = sum(1 for r in valuation_results if r.status == "OK")
    trap_count     = sum(1 for r in valuation_results if r.status == "VALUE_TRAP")
    insuff_count   = sum(1 for r in valuation_results if r.status == "INSUFFICIENT_DATA")
    console.print(
        f"[bold]Valuation:[/bold]  OK={ok_count}  VALUE_TRAP={trap_count}  "
        f"INSUFFICIENT_DATA={insuff_count}\n"
    )

    # ── 4. Screen ─────────────────────────────────────────────────────────────
    is_dow30_mode = (source == UniverseSource.DOW30)

    if is_dow30_mode:
        # Dow 30: pure ranking by 52w position, no MoS filter
        profile_name = "dow30_ranking"
        df = apply_dow30_ranking(valuation_results)
        console.print(f"[bold]Mode:[/bold] Dow Jones 30 — ranked by 52-week position\n")
    else:
        profiles = load_profiles(str(_PROFILES_YAML))
        profile_name = args.profile
        profile: ScreenerProfile = profiles.get(profile_name, next(iter(profiles.values())))
        console.print(f"[bold]Profile:[/bold] {profile_name}\n")
        df = apply_profile(valuation_results, profile)

    # ── 5. Display ────────────────────────────────────────────────────────────
    render_table(df)
    if is_dow30_mode:
        console.print(f"\n[dim]{len(df)} Dow Jones companies ranked (lowest 52w position first).[/dim]")
        console.print("[dim]Interpretation: rank #1 = trading closest to 52-week low = most upside potential.[/dim]")
    else:
        console.print(f"\n[dim]{len(df)} companies passed the '{profile_name}' filter.[/dim]")

    # ── 6. Export ─────────────────────────────────────────────────────────────
    written = export_results(df, args.export, profile_name)
    failed_path = _save_failed(failed, profile_name)

    for path in written:
        console.print(f"[green]Saved:[/green] {path}")
    if failed_path:
        console.print(f"[dim]Failed tickers logged to:[/dim] {failed_path}")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = _build_parser()
    # Detect whether the user passed any flags
    raw_args = sys.argv[1:]
    if not raw_args:
        args = interactive_wizard()
    else:
        args = parser.parse_args(raw_args)

    # Validate custom universe
    if args.universe == "custom" and not getattr(args, "csv_path", None):
        parser.error("--csv-path is required when --universe is 'custom'.")

    run(args)


if __name__ == "__main__":
    main()
