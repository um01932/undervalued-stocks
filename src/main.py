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

from src.backtester import BacktestResult, run_backtest, LIMITATIONS
from src.engine import DCFParams, ValuationResult, evaluate
from src.fetcher import CacheStore, TickerData, fetch_universe, fetch_risk_free_rate
from src.screener import ScreenerProfile, apply_profile, rank_all, apply_dow30_ranking, load_profiles, apply_magic_formula, compute_sector_percentiles
from src.universe import UniverseSource, get_universe

# ── Constants ─────────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).parent.parent / "data"
_REPORTS_DIR = _DATA_DIR / "reports"
_CACHE_PATH = _DATA_DIR / "cache.duckdb"
_CONFIG_DIR = Path(__file__).parent.parent / "config"
_PROFILES_YAML = _CONFIG_DIR / "screener_profiles.yaml"

console = Console()
logger = logging.getLogger(__name__)

# ── Argument parser ───────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-screener",
        description="Stock Screener & Intrinsic Value Engine — local, parallel, worldwide.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--universe",
        choices=["sp500", "nasdaq100", "dow30", "russell2000", "eurostoxx50", "bet", "world", "custom", "multi"],
        default="sp500",
        help=(
            "Stock universe to screen. "
            "Use 'multi' to combine S&P 500 + NASDAQ-100 + Russell 2000 + Euro Stoxx 50 + BET Romania "
            "into a single deduped run (~700+ unique tickers)."
        ),
    )
    parser.add_argument(
        "--csv-path",
        metavar="PATH",
        default=None,
        help="Path to custom ticker CSV (required when --universe custom).",
    )
    parser.add_argument(
        "--profile",
        choices=["deep_value", "buffett_quality", "high_fcf_yield", "quality_value", "dividend_growth"],
        default=None,
        help=(
            "Screener preset to apply. "
            "If omitted, ALL 5 profiles are run sequentially on the same fetched data "
            "(deep_value, buffett_quality, high_fcf_yield, quality_value, dividend_growth)."
        ),
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
    parser.add_argument("--backtest", action="store_true",
        help="Run walk-forward backtest instead of live screen.")
    parser.add_argument("--backtest-start", type=int, default=2018, metavar="YEAR",
        help="First year of the backtest (default: 2018).")
    parser.add_argument("--backtest-end", type=int, default=2024, metavar="YEAR",
        help="Last year of the backtest, inclusive (default: 2024).")
    parser.add_argument("--backtest-top-n", type=int, default=10, metavar="N",
        help="Number of top-ranked tickers to hold each year (default: 10).")
    parser.add_argument("--backtest-benchmark", default="^GSPC", metavar="TICKER",
        help="Benchmark ticker for comparison (default: ^GSPC).")
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

    universe    = _prompt("Universe (sp500/nasdaq100/dow30/russell2000/eurostoxx50/bet/world/custom/multi)", "multi")
    csv_path    = None
    if universe == "custom":
        csv_path = _prompt("Path to ticker CSV", "data/custom_tickers.csv")

    profile_raw = _prompt(
        "Screener profile (deep_value/buffett_quality/high_fcf_yield/quality_value/dividend_growth/all)",
        "all",
    )
    profile     = None if profile_raw.strip().lower() in ("all", "") else profile_raw.strip()
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
        profile=profile,  # None = all profiles
        workers=workers,
        rps=rps,
        export=export_fmt,
        dcf_growth=dcf_growth,
        dcf_discount=dcf_discount,
        dcf_terminal=dcf_terminal,
        dcf_years=dcf_years,
        dcf_exit_multiple=dcf_exit,
        # backtest params — not prompted in wizard, use defaults
        backtest=False,
        backtest_start=2018,
        backtest_end=2024,
        backtest_top_n=10,
        backtest_benchmark="^GSPC",
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
            ("DCF Model",     "right", "dim"),
            ("Piotroski",     "right", ""),
            ("ROIC%",         "right", ""),
            ("Score",         "right", "bold"),
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
            piotroski_val = row.get("Piotroski")
            piotroski_str = str(int(piotroski_val)) if piotroski_val is not None and not (isinstance(piotroski_val, float) and pd.isna(piotroski_val)) else "—"
            roic_val = row.get("ROIC%")
            roic_str = f"{roic_val:.1f}%" if roic_val is not None and not (isinstance(roic_val, float) and pd.isna(roic_val)) else "—"
            score_val = row.get("Score")
            score_str = f"{score_val:.1f}" if score_val is not None and not (isinstance(score_val, float) and pd.isna(score_val)) else "—"
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
                str(row.get("DCF Model", "—")),
                piotroski_str,
                roic_str,
                score_str,
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


# ── Backtest mode ─────────────────────────────────────────────────────────────


def _run_backtest_mode(
    args: argparse.Namespace,
    tickers: list[str],
    cache: CacheStore,
    valuation_results: list[ValuationResult],
    dcf_params: DCFParams,
    rf_rate: float,
) -> None:
    """Run walk-forward backtest and display/export results."""
    from src.backtester import run_backtest, LIMITATIONS  # already imported at top; safe re-import

    # Load screener profile
    profiles = load_profiles(str(_PROFILES_YAML))
    profile_name = getattr(args, "profile", "deep_value")
    profile: ScreenerProfile = profiles.get(profile_name, next(iter(profiles.values())))

    console.print(f"\n[bold yellow]BACKTEST MODE[/bold yellow]  profile=[cyan]{profile_name}[/cyan]  "
                  f"years={args.backtest_start}-{args.backtest_end}  top_n={args.backtest_top_n}\n")

    # Run the backtest
    bt: BacktestResult = run_backtest(
        tickers=tickers,
        cache=cache,
        profile=profile,
        dcf_params=dcf_params,
        rf_rate=rf_rate,
        start_year=args.backtest_start,
        end_year=args.backtest_end,
        top_n=args.backtest_top_n,
        benchmark_ticker=args.backtest_benchmark,
    )

    # ── Print limitations warning ─────────────────────────────────────────────
    console.print(f"[bold red]{'=' * 70}[/bold red]")
    for line in LIMITATIONS.strip().splitlines():
        console.print(f"[dim]{line}[/dim]")
    console.print(f"[bold red]{'=' * 70}[/bold red]\n")

    if not bt.annual_rows:
        console.print("[yellow]No annual rows were produced. "
                      "Check that the profile has passing tickers and price data is available.[/yellow]")
        return

    # ── Print annual results table ────────────────────────────────────────────
    table = Table(
        title=f"Backtest Results - {profile_name} ({bt.start_year}-{bt.end_year})",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        highlight=True,
    )
    table.add_column("Year",        justify="right",  style="bold")
    table.add_column("Portfolio%",  justify="right",  style="bold green")
    table.add_column("Benchmark%",  justify="right",  style="dim")
    table.add_column("Excess%",     justify="right",  style="bold")
    table.add_column("Picks",       justify="right",  style="dim")
    table.add_column("Win Rate",    justify="right",  style="")

    def _pct(v: float) -> str:
        colour = "green" if v >= 0 else "red"
        return f"[{colour}]{v * 100:+.1f}%[/{colour}]"

    for row in bt.annual_rows:
        win_pct = (row.winning_picks / row.total_picks * 100) if row.total_picks > 0 else 0.0
        table.add_row(
            str(row.year),
            _pct(row.portfolio_return),
            _pct(row.benchmark_return),
            _pct(row.excess_return),
            str(row.total_picks),
            f"{win_pct:.0f}%",
        )

    console.print(table)

    # ── Print summary ─────────────────────────────────────────────────────────
    console.print()
    console.rule("[bold]Summary[/bold]")
    sharpe_str  = f"{bt.sharpe_ratio:.2f}"  if bt.sharpe_ratio  is not None else "N/A"
    sortino_str = f"{bt.sortino_ratio:.2f}" if bt.sortino_ratio is not None else "N/A"
    console.print(
        f"  [bold]CAGR Portfolio:[/bold]  {bt.cagr_portfolio * 100:+.2f}%   "
        f"[bold]CAGR Benchmark:[/bold]  {bt.cagr_benchmark * 100:+.2f}%\n"
        f"  [bold]Sharpe:[/bold]          {sharpe_str}   "
        f"[bold]Sortino:[/bold]         {sortino_str}\n"
        f"  [bold]Max Drawdown:[/bold]    {bt.max_drawdown * 100:.2f}%   "
        f"[bold]Win Rate:[/bold]        {bt.win_rate * 100:.1f}%  "
        f"({bt.total_picks} picks)\n"
    )

    # ── Export to CSV ─────────────────────────────────────────────────────────
    export_fmt = getattr(args, "export", "csv")
    if export_fmt in ("csv", "both", "excel"):
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = _REPORTS_DIR / f"{timestamp}_backtest_{profile_name}.csv"

        import csv as _csv
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow([
                "Year", "Portfolio%", "Benchmark%", "Excess%",
                "Picks", "WinningPicks", "WinRate%",
                "SelectedTickers",
            ])
            for row in bt.annual_rows:
                win_rate_pct = (row.winning_picks / row.total_picks * 100) if row.total_picks > 0 else 0.0
                writer.writerow([
                    row.year,
                    f"{row.portfolio_return * 100:.4f}",
                    f"{row.benchmark_return * 100:.4f}",
                    f"{row.excess_return * 100:.4f}",
                    row.total_picks,
                    row.winning_picks,
                    f"{win_rate_pct:.2f}",
                    "|".join(row.selected_tickers),
                ])
            # SUMMARY row
            writer.writerow([
                "SUMMARY",
                f"{bt.cagr_portfolio * 100:.4f}",
                f"{bt.cagr_benchmark * 100:.4f}",
                f"{(bt.cagr_portfolio - bt.cagr_benchmark) * 100:.4f}",
                bt.total_picks,
                "",
                f"{bt.win_rate * 100:.2f}",
                f"Sharpe={sharpe_str} Sortino={sortino_str} MaxDD={bt.max_drawdown * 100:.2f}%",
            ])

        console.print(f"[green]Backtest saved:[/green] {csv_path}")


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

    rf_rate = fetch_risk_free_rate(cache)
    logger.info("Risk-free rate: %.2f%%", rf_rate * 100)

    valuation_results: list[ValuationResult] = []
    for td in ticker_data_list:
        try:
            result = evaluate(td, dcf_params, rf_rate=rf_rate)
            valuation_results.append(result)
        except Exception as exc:
            logging.warning("Evaluation failed for %s: %s", td.ticker, exc)

    compute_sector_percentiles(valuation_results)

    ok_count       = sum(1 for r in valuation_results if r.status == "OK")
    trap_count     = sum(1 for r in valuation_results if r.status == "VALUE_TRAP")
    insuff_count   = sum(1 for r in valuation_results if r.status == "INSUFFICIENT_DATA")
    console.print(
        f"[bold]Valuation:[/bold]  OK={ok_count}  VALUE_TRAP={trap_count}  "
        f"INSUFFICIENT_DATA={insuff_count}\n"
    )

    # ── 3b. Backtest mode — branch off here ───────────────────────────────────
    if getattr(args, "backtest", False):
        _run_backtest_mode(args, tickers, cache, valuation_results, dcf_params, rf_rate)
        return

    # ── 4. Screen ─────────────────────────────────────────────────────────────
    is_dow30_mode = (source == UniverseSource.DOW30)

    if is_dow30_mode:
        # Dow 30: pure ranking by 52w position, no MoS filter
        profile_name = "dow30_ranking"
        df = apply_dow30_ranking(valuation_results)
        console.print(f"[bold]Mode:[/bold] Dow Jones 30 — ranked by 52-week position\n")

        # ── 5. Display
        render_table(df)
        console.print(f"\n[dim]{len(df)} Dow Jones companies ranked (lowest 52w position first).[/dim]")
        console.print("[dim]Interpretation: rank #1 = trading closest to 52-week low = most upside potential.[/dim]")

        # ── 6. Export
        written = export_results(df, args.export, profile_name)
        failed_path = _save_failed(failed, profile_name)
        for path in written:
            console.print(f"[green]Saved:[/green] {path}")
        if failed_path:
            console.print(f"[dim]Failed tickers logged to:[/dim] {failed_path}")
        return

    # ── Multi-profile or single-profile screening ─────────────────────────────
    profiles = load_profiles(str(_PROFILES_YAML))

    _ALL_PROFILE_KEYS = [
        "deep_value", "buffett_quality", "high_fcf_yield",
        "quality_value", "dividend_growth",
    ]

    # Determine which profiles to run
    selected_profile = getattr(args, "profile", None)
    if selected_profile:
        profiles_to_run = [selected_profile]
    else:
        profiles_to_run = _ALL_PROFILE_KEYS
        console.print(
            f"[bold]Mode:[/bold] All {len(profiles_to_run)} profiles "
            f"({', '.join(profiles_to_run)})\n"
        )

    run_date = datetime.now().strftime("%Y%m%d")
    failed_path = _save_failed(failed, profiles_to_run[0])
    if failed_path:
        console.print(f"[dim]Failed tickers logged to:[/dim] {failed_path}")

    for profile_name in profiles_to_run:
        profile: ScreenerProfile = profiles.get(profile_name, next(iter(profiles.values())))
        console.print(f"\n[bold cyan]--- Profile: {profile_name} ---[/bold cyan]")

        # rank_all: ALL companies with ProfileFit score; apply_profile: strict-pass only
        df          = rank_all(valuation_results, profile)
        df_filtered = apply_profile(valuation_results, profile)

        # Display (only for single-profile runs to avoid console clutter)
        if selected_profile:
            render_table(df_filtered)
        passes = df["Passes"].sum() if "Passes" in df.columns else 0
        console.print(
            f"[dim]{passes} PASS | {len(df)} ranked | profile=[cyan]{profile_name}[/cyan][/dim]"
        )

        # Export
        written = export_results(df, args.export, profile_name)
        for path in written:
            console.print(f"[green]Saved:[/green] {path.name}")

        # Score history
        if args.export in ("csv", "both") and not df.empty:
            history_rows = []
            for _, row in df.iterrows():
                history_rows.append({
                    "ticker":          row.get("Ticker", ""),
                    "run_date":        run_date,
                    "profile":         profile_name,
                    "composite_score": row.get("Score"),
                    "mos_pct":         row.get("MoS%"),
                    "profile_fit":     row.get("ProfileFit"),
                })
            if history_rows:
                cache.append_score_history(history_rows)

    # ── Magic Formula (once per run, after all profiles) ─────────────────────
    if args.export in ("csv", "both"):
        mf_df = apply_magic_formula(valuation_results)
        if not mf_df.empty:
            _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            mf_path = _REPORTS_DIR / f"{ts}_magic_formula.csv"
            mf_df.to_csv(mf_path, index=False)
            console.print(f"\n[green]Magic Formula:[/green] {len(mf_df)} companies saved to {mf_path.name}")
        else:
            console.print("\n[dim]Magic Formula: no eligible companies (check ROIC/PE data).[/dim]")


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
