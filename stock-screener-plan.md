# Stock Screener & Intrinsic Value Engine — Implementation Plan

## Top-Level Overview

Build a fully local, modular, parallel Python application that:
- Assembles a stock universe automatically — S&P 500, NASDAQ 100, a bundled global seed file (~2,000
  major international tickers), or any custom CSV — **zero mandatory user input at runtime**.
- Fetches financial data via `yfinance` with a **DuckDB-backed structured cache** (time-based TTL per
  data type, columnar storage, no JSON serialisation of DataFrames).
- Computes relative valuation multiples and a conservative DCF model (Gordon Growth Model as primary
  terminal value; Exit Multiple as a secondary estimate shown side-by-side).
- Screens and ranks companies by Margin of Safety.
- Exports results to CSV / Excel and renders a `rich` CLI dashboard.
- Includes both **unit tests** (mocked, fast) and **integration tests** (real yfinance calls, clearly
  separated, skippable via a pytest marker).

**Tech stack:** Python 3.11+, `yfinance`, `pandas`, `duckdb`, `pydantic` v2, `rich`, `tqdm`, `argparse`, `PyYAML`.
**Storage:** Single DuckDB file `data/cache.duckdb` managed by a `CacheStore` abstraction.
**Concurrency:** `concurrent.futures.ThreadPoolExecutor` with configurable worker count and per-request throttle.

---

## Folder Structure

```
UndervaluedStocks/
├── data/
│   ├── cache.duckdb            # DuckDB cache (gitignored)
│   ├── global_tickers.csv      # Bundled ~2 000 international tickers (tracked in git)
│   └── reports/                # CSV / Excel exports (gitignored)
├── config/
│   └── screener_profiles.yaml  # Optional user-defined screener overrides
├── src/
│   ├── __init__.py
│   ├── universe.py             # Stock universe assembly (auto-discovery)
│   ├── fetcher.py              # Concurrent data pipeline + DuckDB cache
│   ├── engine.py               # Multiples & DCF valuation (GGM + Exit Multiple)
│   ├── screener.py             # Filtering & ranking presets
│   └── main.py                 # CLI entry point
├── tests/
│   ├── conftest.py             # Shared fixtures, pytest markers
│   ├── unit/
│   │   ├── test_universe.py
│   │   ├── test_fetcher.py
│   │   ├── test_engine.py
│   │   └── test_screener.py
│   └── integration/
│       ├── test_fetch_real.py  # Real yfinance calls (marked @pytest.mark.integration)
│       └── test_pipeline_e2e.py
├── requirements.txt
├── pytest.ini                  # Registers "integration" marker; default: unit only
├── .gitignore
└── README.md
```

---

## Sub-Tasks

---

### Sub-Task 1 — Project Scaffolding

**Intent**
Create the repository skeleton so every subsequent sub-task has a stable foundation: folder layout,
dependency manifest, pytest configuration, gitignore, and a minimal README.

**Expected Outcomes**
- All directories exist with appropriate placeholder files.
- `requirements.txt` is complete and pinned to compatible versions.
- `pytest.ini` registers the `integration` marker so `pytest` (no flags) runs only unit tests.
- `.gitignore` excludes `data/cache.duckdb`, `data/reports/`, `__pycache__/`, `.env`, `*.pyc`, `.venv/`.
- `README.md` documents purpose, quick-start, CLI usage, and how to run integration tests.
- `data/global_tickers.csv` is a curated seed file of ~2 000 major international tickers (NYSE, NASDAQ,
  LSE, Euronext, TSE, ASX, etc.) with columns: `ticker`, `name`, `exchange`, `country`.

**Todo List**
1. Create directory tree: `data/reports/`, `config/`, `src/`, `tests/unit/`, `tests/integration/`.
2. Write `requirements.txt`:
   - `yfinance>=0.2.40`
   - `pandas>=2.2`
   - `duckdb>=0.10`
   - `pydantic>=2.7`
   - `rich>=13.7`
   - `tqdm>=4.66`
   - `requests>=2.32`
   - `openpyxl>=3.1`
   - `PyYAML>=6.0`
   - `pytest>=8.0`
   - `pytest-mock>=3.14`
3. Write `pytest.ini` with `markers = integration: marks tests as requiring real network access`.
   Default `addopts = -m "not integration"` so unit tests run by default.
4. Create `.gitignore`.
5. Create `README.md` with project overview, prerequisites, install steps, example CLI commands, and
   a note that `pytest -m integration` runs real-network tests.
6. Create `src/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py`.
7. Generate `data/global_tickers.csv` — a well-curated seed of ~2 000 tickers covering major
   international exchanges, usable as the `WORLD` universe with zero user input.

**Relevant Context**
- `data/cache.duckdb` and `data/reports/` are gitignored; `data/global_tickers.csv` is tracked.
- `data/.gitkeep` is not needed since `global_tickers.csv` keeps the `data/` directory.

**Status:** [x] done

---

### Sub-Task 2 — `universe.py`: Stock Universe Assembly

**Intent**
Provide a single, typed API for assembling the ticker list. Supports S&P 500 (Wikipedia scrape),
NASDAQ 100 (Wikipedia scrape), a zero-input `WORLD` mode (reads the bundled `global_tickers.csv`),
and custom CSV files supplied by the user. No mandatory user input is required at runtime — the
default universe is `WORLD`.

**Expected Outcomes**
- `UniverseSource(StrEnum)` with values: `SP500`, `NASDAQ100`, `WORLD`, `CUSTOM`.
- `get_sp500_tickers() -> list[str]` — scrapes Wikipedia S&P 500 constituents.
- `get_nasdaq100_tickers() -> list[str]` — scrapes Wikipedia NASDAQ-100 constituents.
- `get_world_tickers() -> list[str]` — reads `data/global_tickers.csv` (bundled, no network call).
- `get_tickers_from_csv(path: str) -> list[str]` — user-supplied CSV with a `ticker` column.
- `get_universe(source: UniverseSource = UniverseSource.WORLD, csv_path: str | None = None) -> list[str]`
  — unified entry point; `WORLD` is the default.
- Tickers are normalised: stripped of whitespace, dots replaced with hyphens (yfinance convention),
  deduped, sorted.

**Todo List**
1. Define `UniverseSource(StrEnum)` with values `SP500`, `NASDAQ100`, `WORLD`, `CUSTOM`.
2. Implement `get_sp500_tickers()` using `pandas.read_html` on the Wikipedia S&P 500 page; extract
   `Symbol` column; normalise.
3. Implement `get_nasdaq100_tickers()` using same approach on the Wikipedia NASDAQ-100 page; extract
   `Ticker` column.
4. Implement `get_world_tickers()` reading `data/global_tickers.csv`; resolve path relative to the
   package root using `Path(__file__).parent.parent / "data" / "global_tickers.csv"`.
5. Implement `get_tickers_from_csv(path)` with validation (file must exist, must have a `ticker`
   column, case-insensitive header match).
6. Implement `get_universe(source, csv_path)` dispatcher with `WORLD` as default.
7. Add module-level docstring and `__all__`.
8. Write `tests/unit/test_universe.py` — mock `pandas.read_html` and filesystem; no real HTTP calls.

**Relevant Context**
- Wikipedia S&P 500 URL: `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`
- Wikipedia NASDAQ-100 URL: `https://en.wikipedia.org/wiki/Nasdaq-100`
- yfinance international ticker examples: `ASML.AS` (Amsterdam), `7203.T` (Tokyo), `NOVO-B.CO`
  (Copenhagen), `BHP.AX` (ASX), `RIO.L` (LSE).
- `pandas.read_html` returns a list of DataFrames; index 0 is the constituents table for both
  Wikipedia pages.
- yfinance expects `BRK-B` not `BRK.B` — normalise dots to hyphens.

**Status:** [x] done

---

### Sub-Task 3 — `fetcher.py`: Concurrent Data Pipeline & DuckDB Cache

**Intent**
Create a robust, rate-limited, concurrent fetcher that downloads financial data from `yfinance` and
persists it in a **DuckDB structured cache** with TTL-based invalidation. DataFrames are stored as
native DuckDB tables (one row per ticker + period), eliminating JSON round-trips and enabling direct
SQL analytics over the cache.

**Expected Outcomes**
- `CacheStore` class backed by `data/cache.duckdb` with:
  - Structured tables: `ticker_info`, `financials`, `cashflow`, `balance_sheet` — each has columns
    `ticker TEXT`, `fetched_at TIMESTAMP`, plus data columns matching the yfinance output schema.
  - `get_info(ticker) -> dict | None` — returns cached info dict if within 30-day TTL.
  - `set_info(ticker, payload: dict)` — upserts into `ticker_info`.
  - `get_financials(ticker, statement: str) -> pd.DataFrame | None` — returns cached DataFrame
    (financials / cashflow / balance_sheet) if within 7-day TTL.
  - `set_financials(ticker, statement: str, df: pd.DataFrame)` — upserts rows into the corresponding
    table.
  - TTL constants: `INFO_TTL = 30 days`, `FINANCIALS_TTL = 7 days`, `PRICE_TTL = 1 day`.
- `TickerData(BaseModel)` holding all raw fields for one ticker.
- `fetch_ticker(ticker: str, cache: CacheStore) -> TickerData | None` — cache-first, then yfinance;
  returns `None` on unrecoverable error (delisted, no data, all fields empty).
- `fetch_universe(tickers, max_workers, requests_per_second) -> tuple[list[TickerData], list[str]]`
  — concurrent fetch with `rich` progress bar; returns `(results, failed_tickers)`.
- Retry: up to 3 attempts with exponential backoff (1 s → 2 s → 4 s) on transient errors.
- Thread-safety: DuckDB connections are per-thread (one `duckdb.connect()` per thread via
  `threading.local()`); the database file is opened in read-write mode with DuckDB's built-in
  concurrency handling.

**Todo List**
1. Design DuckDB schema:
   - `ticker_info(ticker, fetched_at, current_price, market_cap, trailing_pe, price_to_book,
     enterprise_to_ebitda, peg_ratio, free_cashflow, total_debt, total_cash, ebitda,
     shares_outstanding, short_name, sector, industry)`.
   - `financials(ticker, fetched_at, period_date, total_revenue, gross_profit, ebit, net_income)`.
   - `cashflow(ticker, fetched_at, period_date, operating_cashflow, capital_expenditure,
     free_cash_flow)`.
   - `balance_sheet(ticker, fetched_at, period_date, total_assets, total_liabilities,
     total_debt, total_cash, stockholders_equity)`.
   - Primary keys: `(ticker)` for `ticker_info`; `(ticker, period_date)` for statement tables.
2. Implement `CacheStore.__init__` — creates DuckDB file and all tables if absent; uses
   `CREATE TABLE IF NOT EXISTS`.
3. Implement per-thread DuckDB connection via `threading.local()`.
4. Implement `get_info` / `set_info` with TTL check on `fetched_at`.
5. Implement `get_financials` / `set_financials` using `INSERT OR REPLACE` equivalent in DuckDB
   (`INSERT INTO ... ON CONFLICT ... DO UPDATE`).
6. Define `TickerData(BaseModel)` with fields: `ticker`, `info: dict`, `financials: list[dict]`,
   `cashflow: list[dict]`, `balance_sheet: list[dict]` (list of row dicts, one per annual period).
7. Implement `fetch_ticker()` with cache-first logic, yfinance extraction, error handling, retry.
8. Implement `fetch_universe()` with `ThreadPoolExecutor`, throttle, `rich.progress.Progress` bar.
9. Write `tests/unit/test_fetcher.py` — mock `yfinance.Ticker` and DuckDB; no real I/O.
10. Write `tests/integration/test_fetch_real.py` — decorated `@pytest.mark.integration`; fetches
    a small list of 3–5 well-known stable tickers (e.g. AAPL, MSFT, NESN.SW) with real yfinance
    calls; asserts non-empty results and cache population.

**Relevant Context**
- DuckDB supports multiple concurrent readers but only one writer at a time per file — use
  `threading.local()` for connection objects to avoid cross-thread sharing.
- yfinance `financials`, `cashflow`, `balance_sheet` DataFrames are indexed by metric name with
  date columns — transpose before storing so each row = one time period.
- Map yfinance metric names to schema column names explicitly in a `FIELD_MAP` dict to make the
  mapping auditable and easy to fix when yfinance changes key names.

**Status:** [x] done

---

### Sub-Task 4 — `engine.py`: Valuation Multiples & DCF Model

**Intent**
Transform raw `TickerData` into a `ValuationResult` containing relative multiples and **two
DCF-based intrinsic value estimates**: Gordon Growth Model (conservative, default) and Exit Multiple
(market-relative, secondary). Companies with insufficient or negative FCF data are explicitly marked
`INSUFFICIENT_DATA` and excluded from ranking. Value traps are flagged separately.

**Expected Outcomes**
- `DCFParams(BaseModel)` with fields and defaults:
  `growth_rate=0.05`, `discount_rate=0.10`, `terminal_growth=0.025`,
  `projection_years=10`, `exit_multiple=12.0` (EV/EBITDA used in Exit Multiple method).
- `ValuationResult(BaseModel)` fields:
  `ticker`, `company_name`, `sector`, `industry`, `current_price`, `market_cap`,
  `pe_ratio`, `pb_ratio`, `ev_ebitda`, `p_fcf`, `peg_ratio`, `net_debt_ebitda`,
  `dcf_ggm_intrinsic` (Gordon Growth Model per-share value),
  `dcf_exit_intrinsic` (Exit Multiple per-share value),
  `dcf_intrinsic_value` (average of the two when both available, else whichever is available),
  `margin_of_safety_pct`,
  `status: Literal["OK", "INSUFFICIENT_DATA", "VALUE_TRAP"]`.
- `compute_multiples(data: TickerData) -> dict` — extracts all multiples from `info` and derived
  fields; guards all divisions against zero/None.
- `compute_dcf_ggm(data, params) -> float | None` — Gordon Growth Model; requires ≥ 3 valid positive
  FCF years; returns `None` otherwise.
- `compute_dcf_exit(data, params) -> float | None` — Exit Multiple on projected EBITDA; requires
  EBITDA > 0 and shares outstanding > 0; returns `None` otherwise.
- `evaluate(data: TickerData, params: DCFParams) -> ValuationResult` — orchestrates multiples + both
  DCF methods + status assignment.
- Value Trap detection: `net_debt_ebitda > 3.5` **or** all available FCF years are negative.
- Margin of Safety = `(intrinsic - price) / intrinsic * 100`; `None` when status is not `OK`.

**Todo List**
1. Define `DCFParams(BaseModel)` with all fields and defaults.
2. Define `ValuationResult(BaseModel)` with all output fields.
3. Implement `compute_multiples()`:
   - Source from `TickerData.info` dict: `trailingPE`, `priceToBook`, `enterpriseToEbitda`,
     `pegRatio`, `freeCashflow`, `totalDebt`, `totalCash`, `ebitda`, `sharesOutstanding`,
     `currentPrice`, `marketCap`, `shortName`, `sector`, `industry`.
   - Compute `p_fcf = marketCap / freeCashflow`.
   - Compute `net_debt_ebitda = (totalDebt - totalCash) / ebitda`.
4. Implement `compute_dcf_ggm()`:
   - Extract `free_cash_flow` column from `cashflow` rows.
   - Filter to valid (non-NaN, > 0) annual values; require ≥ 3.
   - Mean FCF as base; project N years at `growth_rate`.
   - Terminal value: `FCF_N * (1 + terminal_growth) / (discount_rate - terminal_growth)`.
   - Discount all cash flows to present value; divide by `shares_outstanding`.
5. Implement `compute_dcf_exit()`:
   - Project EBITDA N years at `growth_rate`.
   - Terminal value: `EBITDA_N * exit_multiple`.
   - Subtract net debt; discount to PV; divide by `shares_outstanding`.
6. Implement `evaluate()`:
   - Call both DCF methods; average results when both non-None; pick whichever is available.
   - Set status: `INSUFFICIENT_DATA` if both DCF methods return `None`; `VALUE_TRAP` if
     value-trap conditions met (even if DCF succeeded); `OK` otherwise.
   - Compute `margin_of_safety_pct` only when status is `OK`.
7. Write `tests/unit/test_engine.py` with synthetic `TickerData` fixtures for: OK path,
   < 3 FCF years, all-negative FCF, value trap (high net debt), and both DCF methods available.
8. Write `tests/integration/test_pipeline_e2e.py` (`@pytest.mark.integration`) — runs full pipeline
   for 5 tickers end-to-end (universe → fetch → evaluate); asserts `ValuationResult` structure is
   valid and status is populated.

**Relevant Context**
- GGM formula: `IV = Σ(FCF_t / (1+r)^t) + TV/(1+r)^N`, then divide by shares outstanding.
- Exit Multiple formula: `IV = (EBITDA_N * exit_multiple - net_debt) / (1+r)^N / shares`.
- Both methods work independently — one can succeed while the other fails.
- `dcf_intrinsic_value` = average when both succeed; single value when only one succeeds; `None`
  when both fail (→ `INSUFFICIENT_DATA`).

**Status:** [ ] pending

---

### Sub-Task 5 — `screener.py`: Filtering & Ranking Presets

**Intent**
Apply named filter profiles to a list of `ValuationResult` objects and return a sorted, ranked
DataFrame ready for display and export. Built-in presets are hardcoded; a YAML override file is
merged at runtime.

**Expected Outcomes**
- Three built-in `ScreenerProfile` presets: `deep_value`, `buffett_quality`, `high_fcf_yield`.
- `load_profiles(yaml_path: str | None) -> dict[str, ScreenerProfile]` — loads built-in defaults
  and merges YAML overrides if the file exists.
- `apply_profile(results: list[ValuationResult], profile: ScreenerProfile) -> pd.DataFrame` —
  filters by thresholds, excludes `INSUFFICIENT_DATA` and `VALUE_TRAP` by default (configurable),
  sorts by `margin_of_safety_pct` descending.
- Output DataFrame columns: `Ticker`, `Company`, `Sector`, `Industry`, `Price`, `MoS%`,
  `P/E`, `P/B`, `EV/EBITDA`, `P/FCF`, `NetDebt/EBITDA`, `DCF GGM`, `DCF Exit`, `DCF Avg`.
- `ScreenerProfile(BaseModel)` fields: `name`, `max_pe`, `max_pb`, `max_ev_ebitda`, `max_p_fcf`,
  `max_net_debt_ebitda`, `min_margin_of_safety_pct`, `sort_by`, `include_value_traps`.

**Preset Defaults**

| Profile          | max_pe | max_pb | max_ev_ebitda | max_p_fcf | max_netdebt_ebitda | min_mos |
|------------------|--------|--------|---------------|-----------|---------------------|---------|
| deep_value       | 15     | 1.5    | 8             | 15        | 2.5                 | 20%     |
| buffett_quality  | 25     | 4.0    | 15            | 25        | 1.5                 | 15%     |
| high_fcf_yield   | 30     | 5.0    | 20            | 12        | 3.0                 | 10%     |

**Todo List**
1. Define `ScreenerProfile(BaseModel)` with all filter fields and defaults.
2. Hardcode `BUILTIN_PROFILES: dict[str, ScreenerProfile]` with three presets.
3. Implement `load_profiles()` — reads optional `config/screener_profiles.yaml`; YAML values
   override built-in fields (merge, not replace).
4. Implement `apply_profile()` — filter `ValuationResult` list by each threshold (skip `None`
   values gracefully), build output DataFrame, sort by `sort_by` column descending.
5. Write `tests/unit/test_screener.py` with synthetic `ValuationResult` lists covering: pass all
   filters, fail one filter, value-trap exclusion, YAML override merge.

**Relevant Context**
- YAML override file: `config/screener_profiles.yaml`; key = profile name (e.g. `deep_value`),
  value = dict of overriding fields only.
- `None` field values in `ValuationResult` must not crash filter logic — use `pd.notna()` guards.
- `sort_by` defaults to `margin_of_safety_pct`.

**Status:** [x] done

---

### Sub-Task 6 — `main.py`: CLI Entry Point

**Intent**
Wire all modules together into a user-facing CLI that works fully automatically with zero required
flags (defaults to `WORLD` universe + `deep_value` profile), supports argparse for scripting, and
renders a coloured `rich` table.

**Expected Outcomes**
- `python src/main.py` with no arguments: interactive wizard for all parameters.
- `python src/main.py --universe sp500 --profile deep_value --workers 10 --export csv` runs
  non-interactively.
- Running with `--universe world` (the default) processes ~2 000 global tickers automatically.
- Progress bars shown during fetch phase via `rich`.
- Final ranked table printed to terminal via `rich.table.Table` with colour coding:
  green MoS ≥ 30%, yellow 15–30%, red < 15%.
- CSV saved to `data/reports/<timestamp>_<profile>.csv`; Excel to `.xlsx` when requested.
- Failed tickers logged to `data/reports/<timestamp>_failed.txt`.
- Summary line: total screened, passed filters, export path.

**Todo List**
1. Define argparse arguments: `--universe` (sp500/nasdaq100/world/custom, default world),
   `--csv-path`, `--profile` (deep_value/buffett_quality/high_fcf_yield, default deep_value),
   `--workers` (default 8), `--rps` (requests per second, default 2.0),
   `--export` (csv/excel/both/none, default csv),
   `--dcf-growth`, `--dcf-discount`, `--dcf-terminal`, `--dcf-years`, `--dcf-exit-multiple`.
2. Implement `interactive_wizard() -> argparse.Namespace` using `input()` prompts with displayed
   defaults so the user can just press Enter for each.
3. Implement `run(args)` orchestration:
   - `get_universe(source, csv_path)`.
   - `fetch_universe(tickers, max_workers, rps)`.
   - `evaluate(data, dcf_params)` for each `TickerData`.
   - `load_profiles()` + `apply_profile()`.
   - `render_table(df)`.
   - `export_results(df, format, profile_name)`.
4. Implement `render_table(df)` using `rich.table.Table` with colour-coded MoS% column.
5. Implement `export_results(df, format, profile_name)` — creates `data/reports/` if absent,
   saves CSV and/or Excel, prints path.
6. `if __name__ == "__main__"`: parse args; if no flags provided launch wizard; else run directly.

**Relevant Context**
- `DCFParams` from `engine.py` is populated from CLI args.
- `data/reports/` created via `Path.mkdir(parents=True, exist_ok=True)`.
- `rich.console.Console` instance should be a module-level singleton.
- The interactive wizard should display the default value for each parameter and accept empty input
  as "keep default".

**Status:** [ ] pending

---

## Cross-Cutting Concerns

- **Logging:** `logging` module throughout (`INFO`/`WARNING`/`ERROR`); `main.py` calls
  `logging.basicConfig`. Never use bare `print` except in `main.py` for user-facing output via `rich`.
- **Type safety:** All public functions fully annotated; `pydantic` models for all data contracts.
- **No silent failures:** Every `None` return is documented; callers must handle it explicitly.
- **Test isolation:** Unit tests use mocks only — zero real network calls, zero real filesystem I/O
  in `tests/unit/`. Integration tests in `tests/integration/` require `pytest -m integration`.
- **`global_tickers.csv` maintenance:** The bundled file is a static snapshot. Users can refresh it
  by running `python src/universe.py --refresh-world` which re-scrapes public sources and overwrites
  the file.
