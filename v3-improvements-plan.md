# V3 Improvements Plan — Stock Screener & Intrinsic Value Engine

## Overview

This plan covers all improvements requested across four themes:
1. **Valuation refinement** — sensitivity matrix, Graham Number, additional DCF context
2. **Anti-value trap indicators** — Beneish M-Score, SBC/dilution, buyback signal
3. **UX & interactivity** — sector filter, score history tracking, watchlist
4. **Universe expansion** — Russell 2000, European markets (Euro Stoxx 50 / BET)

### Guiding Constraints
- All changes must keep 258 unit tests passing
- No new pip dependencies unless strictly necessary
- Engine changes must add to `ValuationResult` — never break existing fields
- Each sub-task is independently deployable (own commit + test pass)
- Every sub-task ends with: `pytest tests/unit/ -q` + `python scripts/export_full_report.py` + `git push`

### Current State (from codebase exploration)

| Component | Status |
|-----------|--------|
| DCF GGM, Exit, DDM | ✅ exist in engine.py |
| Piotroski F-Score | ✅ exists (max 7 pts used) |
| Altman Z-Score | ✅ exists in engine.py |
| ROIC, WACC, Sustainable Growth | ✅ exist |
| ROE, ROA, Beta, Gross Margin, Operating Margin | ✅ **fetched** in ticker_info, **NOT used** in screener/engine output |
| Dividend Yield, Dividend Rate | ✅ **fetched**, NOT used in screener |
| Beneish M-Score | ❌ missing |
| SBC / Share Dilution | ❌ missing (not fetched) |
| Buyback data | ❌ missing (not fetched) |
| Graham Number | ❌ missing |
| DCF Sensitivity Matrix | ❌ missing |
| Score History table | ❌ missing |
| Russell 2000 | ❌ missing in universe.py |
| Sector-relative valuation | ❌ missing |
| Magic Formula (Greenblatt) | ❌ missing |
| Dividend Growth screener profile | ❌ missing |

---

## Sub-Task 1 — Expose ROE, ROA, Beta, Gross Margin already in DuckDB

**Status:** `[ ] pending`

### Intent
`ticker_info` already stores `roe`, `roa`, `beta`, `gross_margin`, `operating_margin` but they are
never propagated to `ValuationResult`, never appear in output CSVs, and never appear in the HTML report.
This sub-task surfaces them at zero data-fetch cost.

### Expected Outcomes
- `ValuationResult` has 5 new optional fields: `roe`, `roa`, `beta`, `gross_margin`, `operating_margin`
- All 5 appear in `_OUTPUT_COLUMNS` in screener.py and in exported CSVs
- `_score_cards()` in export_full_report.py shows a 4th card "ROE" (replaces removed 52w card)
- `_why_buy()` generates one additional sentence about ROE / gross margin
- `ScreenerProfile` gets two new optional filter fields: `min_roe`, `min_gross_margin`
- `buffett_quality` profile updated: `min_roe=15.0` (Buffett's key threshold)
- 258 tests still pass; new tests added for the new fields

### Todo List
1. **`src/engine.py`** — add to `ValuationResult`: `roe: Optional[float]`, `roa: Optional[float]`,
   `beta: Optional[float]`, `gross_margin: Optional[float]`, `operating_margin: Optional[float]`
2. **`src/engine.py` `evaluate()`** — populate the 5 fields from `TickerData.info` dict
   (keys: `returnOnEquity`, `returnOnAssets`, `beta`, `grossMargins`, `operatingMargins`)
3. **`src/screener.py` `_OUTPUT_COLUMNS`** — append `ROE%`, `ROA%`, `Beta`, `Gross Margin%`
4. **`src/screener.py` `apply_profile()` and `rank_all()`** — populate the 4 new columns
5. **`src/screener.py` `ScreenerProfile`** — add `min_roe: Optional[float]`, `min_gross_margin: Optional[float]`
6. **`src/screener.py` `_passes_filter()`** — enforce the 2 new filters
7. **`src/screener.py` `BUILTIN_PROFILES`** — add `min_roe=15.0` to `buffett_quality`
8. **`scripts/export_full_report.py` `_score_cards()`** — add ROE card (orange, wt 10%)
9. **`scripts/export_full_report.py` `_why_buy()`** — add ROE/gross_margin sentence
10. **`scripts/export_full_report.py` `_PROFILE_META`** — update `buffett_quality` description
11. **`tests/unit/test_engine.py`** — add tests for the 5 new ValuationResult fields
12. **`tests/unit/test_screener.py`** — add tests for `min_roe` filter

### Relevant Context
- `src/fetcher.py` lines 97-105: field map shows `returnOnEquity → roe`, `returnOnAssets → roa`,
  `beta → beta`, `grossMargins → gross_margin`, `operatingMargins → operating_margin`
- `src/engine.py` lines 61-107: `ValuationResult` model — add after `net_debt_ebitda`
- `src/screener.py` lines 224-237: `_OUTPUT_COLUMNS` and `DOW30_OUTPUT_COLUMNS`
- `scripts/export_full_report.py` lines 421-493: `_score_cards()` function

---

## Sub-Task 2 — Graham Number (3rd intrinsic value method)

**Status:** `[ ] pending`

### Intent
Add `Graham Number = sqrt(22.5 × EPS × Book Value Per Share)` as a third parallel intrinsic
value estimate alongside DCF GGM and DCF Exit. It uses only balance sheet + earnings data
(no growth assumptions), so it anchors value even when FCF data is sparse.
The existing `dcf_intrinsic_value` (avg of GGM+Exit) becomes a 3-way average when Graham is available.

### Expected Outcomes
- `ValuationResult` has new field `graham_number: Optional[float]`
- `dcf_intrinsic_value` (DCF Avg) is updated to be mean of all available methods (2 or 3)
- `dcf_model_used` reflects which methods contributed (e.g. `"GGM+Exit+Graham"`)
- HTML report shows Graham Number in the intrinsic value column tooltip / Why Buy text
- New sentence in `_why_buy()`: "Graham Number of $X confirms / diverges from DCF"
- 258 tests pass; new unit tests for `compute_graham_number()`

### Todo List
1. **`src/engine.py`** — add `compute_graham_number(ticker_data) → Optional[float]`:
   - `eps = net_income_latest / shares_outstanding`
   - `bvps = stockholders_equity_latest / shares_outstanding`
   - Return `sqrt(22.5 × eps × bvps)` if both > 0, else None
2. **`src/engine.py` `ValuationResult`** — add `graham_number: Optional[float]`
3. **`src/engine.py` `evaluate()`** — call `compute_graham_number()`, include in avg computation
4. **`src/screener.py` `_OUTPUT_COLUMNS`** — add `Graham Number` column
5. **`src/screener.py` `apply_profile()` / `rank_all()`** — populate `Graham Number`
6. **`scripts/export_full_report.py`** — show Graham Number in table + Why Buy
7. **`tests/unit/test_engine.py`** — add `test_compute_graham_number_*` tests
8. **`__all__`** in engine.py — export `compute_graham_number`

### Relevant Context
- `src/engine.py` `compute_dcf_ggm()` line 194 — follow same pattern for data access
- EPS proxy: use `financials[-1].net_income / info.shares_outstanding`
- BVPS proxy: use `balance_sheet[-1].stockholders_equity / info.shares_outstanding`
- Both inputs must be strictly positive (skip if equity is negative)

---

## Sub-Task 3 — DCF Sensitivity Matrix (Bear / Base / Bull scenarios)

**Status:** `[ ] pending`

### Intent
Instead of a single intrinsic value, show a 3×3 grid varying WACC (−2%, base, +2%) and
Terminal Growth Rate (−1%, base, +1%). This shows investors that the stock is undervalued
even under pessimistic assumptions — much more persuasive than a single number.
Implemented entirely in the HTML report (no engine changes needed — just re-run DCF with different params).

### Expected Outcomes
- `_why_buy()` panel in the HTML report contains a "Sensitivity Analysis" table when DCF data is available
- Table: 3 columns (WACC −2% / Base / +2%) × 3 rows (g −1% / Base / +1%) = 9 cells
- Each cell shows: Intrinsic Value + MoS% (colour-coded green/yellow/red)
- "All 9 scenarios show discount" → strong conviction signal shown as badge
- No changes to engine.py, screener.py, or CSV output — report-only feature

### Todo List
1. **`scripts/export_full_report.py`** — add `_dcf_sensitivity_table(row: dict) → str`:
   - Read base values: `DCF Avg`, `Price`, `ROIC%`, `MoS%` from row dict
   - Infer base `wacc` ≈ 10%, base `g` ≈ 2.5% (DCFParams defaults)
   - For each (wacc_delta, g_delta) combination, recalculate FCF-based DCF inline:
     - Use `DCF Avg` as proxy FCF yield: `iv = dcf_avg × (1/(wacc-g))` scaled
     - Simpler: use the ratio approach — `iv(w,g) = dcf_base × (wacc_base - g_base) / (w - g)`
   - Render HTML table with colour-coded cells
2. **`scripts/export_full_report.py` `_why_buy()`** — call `_dcf_sensitivity_table(row)`,
   insert between score cards and 52w bar
3. Add "All green" / "Mixed" / "Bearish scenario still +X% discount" summary badge

### Relevant Context
- `row` dict in `_why_buy()` contains: `DCF Avg`, `Price`, `MoS%`, `ROIC%` as strings
- DCF sensitivity formula (simplified): `IV_new = IV_base × (WACC_base - g_base) / (WACC_new - g_new)`
- This is a report-only feature — zero risk to existing tests

---

## Sub-Task 4 — Beneish M-Score (Earnings Manipulation Detection)

**Status:** `[ ] pending`

### Intent
The Beneish M-Score uses 8 financial ratios computed from two consecutive annual reports to detect
earnings manipulation. Score > −1.78 = manipulator flag. This is a genuine anti-value-trap signal
that goes beyond Piotroski and Altman Z (both of which exist already).

### Expected Outcomes
- `compute_beneish_m_score()` added to `engine.py`
- `ValuationResult` has `beneish_m: Optional[float]` and `beneish_flag: bool`
- `beneish_flag=True` (M > −1.78) adds a red "⚠ MANIPULATION RISK" badge in Why Buy
- New anti-value-trap check: if `beneish_flag=True`, `status` promoted to `VALUE_TRAP`
- `_score_cards()` shows Beneish badge when flag is raised
- 258 tests pass; new unit tests added

### Todo List
1. **`src/engine.py`** — add `compute_beneish_m_score(ticker_data) → Optional[float]`:
   - Requires 2 consecutive years of: total_revenue, gross_profit, net_income, total_assets,
     total_liabilities, operating_cashflow from `financials`, `cashflow`, `balance_sheet`
   - Implement the 8 index ratios (DSRI, GMI, AQI, SGI, DEPI, SGAI, LVGI, TATA)
   - Return M-Score float; None if fewer than 2 years of data
   - Threshold: M > −1.78 = potential manipulator
2. **`src/engine.py` `ValuationResult`** — add `beneish_m: Optional[float]`, `beneish_flag: bool = False`
3. **`src/engine.py` `evaluate()`** — call `compute_beneish_m_score()`, set `beneish_flag`
4. **`src/engine.py` `evaluate()` status logic** — if `beneish_flag=True` and status=OK → set VALUE_TRAP
5. **`src/screener.py`** — add `exclude_beneish_risk: bool = False` to `ScreenerProfile`
6. **`src/screener.py` `_passes_filter()`** — enforce `exclude_beneish_risk`
7. **`src/screener.py` BUILTIN_PROFILES** — set `exclude_beneish_risk=True` in `quality_value` and `buffett_quality`
8. **`scripts/export_full_report.py` `_why_buy()`** — show red warning badge when `beneish_flag=True`
9. **`tests/unit/test_engine.py`** — add `test_compute_beneish_*` with known fixture values

### Relevant Context
- Data availability: `financials` + `cashflow` + `balance_sheet` each hold 3-5 years per ticker
- Beneish M formula reference: Beneish (1999), "The Detection of Earnings Manipulation"
- 8 indices needed; TATA (Total Accruals to Total Assets) is key: `(net_income − operating_cashflow) / total_assets`
- Most formulas require `year[t]` and `year[t-1]` data → need sorted period_date

---

## Sub-Task 5 — Share Dilution & FCF Dilution-Adjusted

**Status:** `[ ] pending`

### Intent
Stock-Based Compensation (SBC) inflates reported FCF because it's a non-cash expense added back
in operating cash flow, but it dilutes shareholders. The "true" FCF = reported FCF − SBC.
Additionally, track share count change YoY as a dilution signal.

### Expected Outcomes
- `fetcher.py` fetches `stockBasedCompensation` from yfinance cashflow statement
- New DuckDB column `cashflow.stock_based_compensation`
- `ValuationResult` has `sbc_adjusted_fcf: Optional[float]` and `shares_dilution_pct: Optional[float]`
- `p_fcf` in engine uses SBC-adjusted FCF when available (configurable)
- New sentence in `_why_buy()`: "FCF adjusted for $Xm SBC = $Y/share" or "⚠ SBC is X% of reported FCF"
- `ScreenerProfile` gets `max_sbc_to_fcf_pct: Optional[float]` filter

### Todo List
1. **`src/fetcher.py`** — add `stock_based_compensation` to cashflow fetch and DB schema
2. **`data/cache.duckdb`** — add column via `ALTER TABLE cashflow ADD COLUMN stock_based_compensation DOUBLE`
   (handled automatically via CacheStore `_migrate_schema()` if that exists, otherwise explicit)
3. **`src/engine.py`** — add `compute_sbc_adjusted_fcf()`: `fcf − avg(sbc_3y)`
4. **`src/engine.py` `ValuationResult`** — add `sbc_adjusted_fcf`, `shares_dilution_pct`
5. **`src/engine.py` `evaluate()`** — compute and populate both fields
6. **`src/screener.py` `ScreenerProfile`** — add `max_sbc_to_fcf_pct`
7. **`scripts/export_full_report.py` `_why_buy()`** — add SBC warning sentence
8. **`tests/unit/test_engine.py`** — add SBC adjustment tests

### Relevant Context
- yfinance `cashflow` DataFrame key: `'Stock Based Compensation'` (varies by version)
- Fallback: if SBC not available, `sbc_adjusted_fcf = None` (do not break existing valuations)
- `shares_dilution_pct`: `(shares_now / shares_3y_ago) − 1`, positive = diluting

---

## Sub-Task 6 — Dividend Growth Screener Profile + DDM Score

**Status:** `[ ] pending`

### Intent
Add a "Dividend Income" screener profile for income-oriented investors. Uses data already
fetched (`dividend_yield`, `dividend_rate`) plus a new `payout_ratio_fcf` metric computed
from cashflow data. The DDM method already exists in engine.py but is only used for
financial-sector stocks — extend its usage.

### Expected Outcomes
- New built-in profile: `dividend_growth` with filters: `min_dividend_yield=2.5`, `max_payout_fcf=70`, `min_piotroski=5`, `max_net_debt_ebitda=2.0`
- `ValuationResult` has `dividend_yield: Optional[float]`, `payout_ratio_fcf: Optional[float]`, `dividend_consecutive_years: Optional[int]`
- `_PROFILE_META` in export_full_report.py updated with `dividend_growth` entry
- `main.py` includes `dividend_growth` in the default run loop
- Report HTML shows "Dividend Yield X%, FCF Payout Y%" in Why Buy for qualifying companies
- 258 tests pass; tests added for the new profile

### Todo List
1. **`src/engine.py` `ValuationResult`** — add `dividend_yield`, `payout_ratio_fcf`, `dividend_consecutive_years`
2. **`src/engine.py` `evaluate()`** — compute `payout_ratio_fcf = (dividend_rate × shares) / avg_fcf`
3. **`src/screener.py` `ScreenerProfile`** — add `min_dividend_yield`, `max_payout_fcf`
4. **`src/screener.py` `_passes_filter()`** — enforce new filters
5. **`src/screener.py` `BUILTIN_PROFILES`** — add `dividend_growth` profile
6. **`src/screener.py` `_OUTPUT_COLUMNS`** — add `Dividend Yield%`, `Payout (FCF)%`
7. **`src/main.py`** — add `dividend_growth` to default profile run list
8. **`scripts/export_full_report.py` `_PROFILE_META`** — add `dividend_growth` entry (label, icon, desc, colour)
9. **`scripts/export_full_report.py` `_why_buy()`** — add dividend sentence
10. **`tests/unit/test_screener.py`** — add tests for `dividend_growth` profile

### Relevant Context
- `dividend_yield` and `dividend_rate` already in `ticker_info` — zero new fetch needed
- `payout_ratio_fcf = (info.dividend_rate × info.shares_outstanding) / mean(cashflow.free_cash_flow[-3:])`
- `PROFILE_PLAIN` and `_PROFILE_LABEL_SHORT` dicts in export_full_report.py need updating

---

## Sub-Task 7 — Magic Formula (Greenblatt) Screener

**Status:** `[ ] pending`

### Intent
Implement Joel Greenblatt's Magic Formula: rank all S&P 500 companies by
(1) Earnings Yield = EBIT/EV and (2) ROIC, then combine ranks. The top 30 companies
by combined rank form the "Magic Formula" portfolio. Historically backtested with ~30% CAGR.
This is a pure ranking formula — no pass/fail filter, just a sorted output.

### Expected Outcomes
- New function `apply_magic_formula(results) → pd.DataFrame` in screener.py
- Output: top 30 companies ranked by `magic_rank = rank_earnings_yield + rank_roic`
- `main.py` runs Magic Formula and exports `{timestamp}_magic_formula.csv`
- New section in HTML report: "Magic Formula — Greenblatt Top 30"
- `_PROFILE_META` updated with `magic_formula` entry
- 258 tests pass; tests added

### Todo List
1. **`src/screener.py`** — add `apply_magic_formula(results: list[ValuationResult]) → pd.DataFrame`:
   - Filter: exclude INSUFFICIENT_DATA, VALUE_TRAP (optional), financial sector
   - Compute `earnings_yield = ebit / (market_cap + total_debt - total_cash)` (= EBIT/EV)
   - Rank 1-N by earnings_yield DESC (rank 1 = highest yield = cheapest)
   - Rank 1-N by roic DESC (rank 1 = highest ROIC = best quality)
   - `magic_score = rank_ey + rank_roic` (lower = better)
   - Sort by `magic_score` ASC, return top 30
2. **`src/screener.py` `__all__`** — export `apply_magic_formula`
3. **`src/main.py`** — add Magic Formula run after existing profiles; export CSV
4. **`scripts/export_full_report.py` `_PROFILE_META`** — add `magic_formula` entry
5. **`scripts/export_full_report.py` `build_full_report()`** — add Magic Formula section
6. **`tests/unit/test_screener.py`** — add `test_apply_magic_formula_*` tests

### Relevant Context
- `ebit` available from `financials` table (used in Altman Z already)
- `roic` already computed in `ValuationResult.roic`
- Exclude: `sector == "Financial Services"` or `sector == "Financials"` (ROIC meaningless for banks)
- EBIT/EV: `ev = market_cap + total_debt - total_cash` — all available in ValuationResult

---

## Sub-Task 8 — Sector-Relative Valuation Percentiles

**Status:** `[ ] pending`

### Intent
Show each company's P/E, P/FCF, EV/EBITDA as a **percentile within its sector** —
not just absolute numbers. A bank with P/E 8x looks cheap in absolute terms,
but if the median bank P/E is 6x it's actually expensive. This context is crucial
for avoiding sector-biased conclusions.

### Expected Outcomes
- `ValuationResult` gets 3 new fields: `sector_pe_percentile`, `sector_pfcf_percentile`, `sector_ev_percentile` (all 0–100, lower = cheaper within sector)
- Percentiles computed in `main.py` after all valuations are done (batch step over full universe)
- Shown in HTML report: small "(8th %ile in sector)" annotation next to multiples in Why Buy
- New optional filter in `ScreenerProfile`: `max_sector_pe_percentile` (e.g. 25 = bottom quartile)
- 258 tests pass

### Todo List
1. **`src/screener.py`** — add `compute_sector_percentiles(results: list[ValuationResult]) → None`:
   - Group results by sector
   - For each sector group, compute percentile rank of pe_ratio, p_fcf, ev_ebitda
   - Mutate each ValuationResult in-place: set `sector_pe_percentile` etc.
2. **`src/engine.py` `ValuationResult`** — add 3 optional percentile fields
3. **`src/main.py`** — call `compute_sector_percentiles(results)` after `evaluate()` loop
4. **`src/screener.py` `ScreenerProfile`** — add `max_sector_pe_percentile: Optional[float]`
5. **`scripts/export_full_report.py` `_why_buy()`** — show sector percentile annotation
6. **`tests/unit/test_screener.py`** — add percentile computation tests

### Relevant Context
- Must run AFTER all tickers are evaluated (requires full results list)
- In-place mutation of ValuationResult is acceptable here (batch post-processing step)
- percentileofscore available in scipy.stats OR simple manual rank/len computation

---

## Sub-Task 9 — Score History Tracking (Sparkline Evolution)

**Status:** `[ ] pending`

### Intent
After every run of `main.py`, persist each company's composite score, MoS%, and ProfileFit
to a new `score_history` DuckDB table with a timestamp. The HTML report then shows a tiny
sparkline SVG in the Why Buy panel: "Score: 71 → 75 → 88 over 3 months".

### Expected Outcomes
- New DuckDB table `score_history(ticker, run_date, composite_score, mos_pct, profile, profile_fit)`
- `main.py` appends to `score_history` after each successful run (non-destructive)
- `export_full_report.py` reads score history at build time and shows sparkline in Why Buy
- Sparkline: mini SVG line chart (80px wide, 20px tall) showing score trend over last 6 runs
- "Trending up ↑" or "Trending down ↓" label based on first vs last value
- 258 tests pass; new test for `save_score_history()`

### Todo List
1. **`src/fetcher.py` `CacheStore`** — add `create_score_history_table()` and `append_score_history(rows: list[dict])`
2. **`data/cache.duckdb`** — `score_history(ticker, run_date, composite_score, mos_pct, profile, profile_fit, TIMESTAMP)`
3. **`src/main.py`** — after exporting CSVs, call `cache.append_score_history(rows)` for each profile
4. **`scripts/export_full_report.py`** — add `_load_score_history(tickers: list[str]) → dict[str, list]` reading from DuckDB
5. **`scripts/export_full_report.py`** — add `_sparkline_svg(scores: list[float]) → str`
6. **`scripts/export_full_report.py` `_why_buy()`** — pass score history, insert sparkline above analysis text
7. **`tests/unit/test_fetcher.py`** — add tests for `append_score_history()`

### Relevant Context
- `append_score_history` must be idempotent per (ticker, run_date, profile) — use UPSERT
- Sparkline: normalize scores to 0–1 within the series for SVG coordinates
- Load history in `build_full_report()` before building sections, pass as `score_history` dict

---

## Sub-Task 10 — Russell 2000 Universe Support

**Status:** `[ ] pending`

### Intent
Add Russell 2000 (small-cap) to the supported universes. Deep value opportunities are more
frequent in small-cap stocks due to lower analyst coverage. Uses the same Wikipedia scraping
pattern as S&P 500 / NASDAQ-100 already implemented.

### Expected Outcomes
- `UniverseSource.RUSSELL2000` enum value added to `universe.py`
- `get_universe("russell2000")` returns ~2000 tickers from Wikipedia scrape + fallback
- `main.py` `--universe russell2000` works end-to-end
- Fallback static list of ~50 well-known Russell 2000 members hardcoded as backup
- 258 tests pass; `test_universe.py` updated

### Todo List
1. **`src/universe.py`** — add `RUSSELL2000 = "russell2000"` to `UniverseSource` enum
2. **`src/universe.py` `get_universe()`** — add `russell2000` branch:
   - Primary: scrape `https://en.wikipedia.org/wiki/Russell_2000_Index`
   - Fallback: static hardcoded list of ~50 large Russell 2000 members
3. **`src/main.py`** — add `"russell2000"` to `--universe` choices in argparse
4. **`tests/unit/test_universe.py`** — add `test_get_universe_russell2000_fallback()`
   using monkeypatch (same pattern as existing SP500 test)

### Relevant Context
- Wikipedia Russell 2000 page: `https://en.wikipedia.org/wiki/Russell_2000_Index`
  (note: full list may not be on Wikipedia — alternative: iShares IWM holdings CSV)
- Alternative source: `https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv`
- The fallback list should include 50 well-known small-cap names (e.g. FIVE, CALM, BOOT, etc.)
- Follow existing `_fetch_sp500()` pattern exactly

---

## Sub-Task 11 — European Markets (Euro Stoxx 50 + BET Romania)

**Status:** `[ ] pending`

### Intent
Extend the screener to European markets. Euro Stoxx 50 covers blue-chip European companies.
BET (Bucharest Exchange) covers Romanian market — niche but relevant given the user context.
yfinance supports European tickers with suffix (e.g. `SAN.PA`, `BRD.RO`).

### Expected Outcomes
- `UniverseSource.EUROSTOXX50` and `UniverseSource.BET` added to `universe.py`
- `get_universe("eurostoxx50")` returns 50 European tickers (e.g. `TTE.PA`, `ASML.AS`)
- `get_universe("bet")` returns BET index tickers with `.RO` suffix (e.g. `BRD.RO`, `TLV.RO`, `SNP.RO`)
- Currency handling: prices shown in native currency (EUR/RON), MoS still comparable
- `main.py` supports `--universe eurostoxx50` and `--universe bet`
- A dedicated section in HTML report for European results when run with those universes
- 258 tests pass

### Todo List
1. **`src/universe.py`** — add `EUROSTOXX50 = "eurostoxx50"`, `BET = "bet"` to enum
2. **`src/universe.py`** — add `_fetch_eurostoxx50()`: scrape Wikipedia Euro Stoxx 50 page
3. **`src/universe.py`** — add `_BET_TICKERS` hardcoded list (BET has only ~20 liquid tickers):
   `["BRD.RO", "TLV.RO", "SNP.RO", "SNG.RO", "FP.RO", "TGN.RO", "COTE.RO", "BVB.RO", ...]`
4. **`src/universe.py` `get_universe()`** — add both branches
5. **`src/main.py`** — add `"eurostoxx50"`, `"bet"` to `--universe` choices
6. **`tests/unit/test_universe.py`** — add tests for both (fallback path, offline)

### Relevant Context
- Euro Stoxx 50 Wikipedia: `https://en.wikipedia.org/wiki/Euro_Stoxx_50`
- BET index is small (18-25 tickers), a static hardcoded list is sufficient and more reliable
- yfinance supports `.PA` (Euronext Paris), `.AS` (Amsterdam), `.DE` (Xetra), `.RO` (Bucharest)
- Exchange rate conversion not needed — MoS is ratio-based (price vs DCF), currency-neutral

---

## Sub-Task 12 — Live Frontend Filtering (Sector + Threshold Sliders)

**Status:** `[ ] pending`

### Intent
Add JavaScript-powered live filtering directly in the HTML report without any server required.
Users can filter the displayed screener table by: sector, min MoS%, max P/E, max P/FCF,
min Piotroski — all client-side with no page reload.

### Expected Outcomes
- Filter bar above each screener section: sector dropdown + 4 numeric inputs + "Reset" button
- Rows hidden/shown in real-time as user types (JS dataset attributes on `<tr>`)
- Row count badge updates live: "Showing 4 of 10"
- Works 100% offline (self-contained HTML, no CDN)
- Existing functionality unchanged (tests still pass — this is HTML/JS only)

### Todo List
1. **`scripts/export_full_report.py` `_row_to_table_tr()`** — add `data-sector`, `data-mos`,
   `data-pe`, `data-pfcf`, `data-piotroski` attributes to each `<tr>`
2. **`scripts/export_full_report.py` `_build_screener_section()`** — add filter bar HTML
   above the `<table>`: sector `<select>`, 4 `<input type="number">`, reset `<button>`
3. **`scripts/export_full_report.py` JS block** — add `initFilter(sectionId)` function:
   - Reads all unique sectors from `<tr data-sector>` → populates `<select>`
   - On any input change: iterate all rows, show/hide based on all active filters
   - Update "Showing N of M" counter
4. Call `initFilter()` for each section on DOMContentLoaded
5. Add CSS for filter bar in `_CSS`

### Relevant Context
- `data-*` attributes on `<tr>` elements are the standard approach for client-side filtering
- Sector values come from `row.get('Sector','')` — already available in `_row_to_table_tr()`
- Filter bar should be visually subtle — not distract from the table itself
- The "rest" compact rows (non-top-10) should also be filterable

---

## Sub-Task 13 — Watchlist (localStorage) + Quick Export

**Status:** `[ ] pending`

### Intent
Let users "star" companies in the HTML report and save them to browser `localStorage`.
A persistent "My Watchlist" section at the top of the page shows the saved companies
with their current scores. One-click export of watchlist to CSV via JavaScript.

### Expected Outcomes
- Each company row has a ⭐ star button (toggles on/off)
- Starred companies persist across page reloads via `localStorage`
- "My Watchlist" section at top of report dynamically populated from localStorage
- "Export Watchlist CSV" button downloads a CSV of starred companies
- 100% client-side, no server needed
- Existing tests unaffected

### Todo List
1. **`scripts/export_full_report.py` `_row_to_table_tr()`** — add `data-ticker`, `data-row-json`
   attributes; add star button `<button class="star-btn" onclick="toggleStar(this)">⭐</button>`
2. **`scripts/export_full_report.py` JS block** — add:
   - `toggleStar(btn)`: read `data-ticker`, toggle in `localStorage.getItem('watchlist')`
   - `loadWatchlist()`: on page load, restore star states + build watchlist section
   - `exportWatchlistCSV()`: generate CSV from localStorage data, trigger download
3. **`scripts/export_full_report.py` HTML** — add `<div id="watchlist-section">` near top of page
4. **`scripts/export_full_report.py` CSS** — add `.star-btn`, `.watchlist-section` styles

### Relevant Context
- `localStorage` key: `"uv_watchlist"` → JSON array of `{ticker, company, score, mos}`
- `data-row-json` attribute on each `<tr>`: compact JSON of key metrics for the watchlist display
- Export: `URL.createObjectURL(new Blob([csv], {type:'text/csv'}))` + temp `<a>` click

---

## Implementation Order (Recommended)

Tasks are ordered by value-to-effort ratio:

```
Phase 1 — Quick wins (data already fetched, low risk):
  Sub-Task 1  — ROE, ROA, Beta, Gross Margin exposed
  Sub-Task 2  — Graham Number
  Sub-Task 6  — Dividend Growth profile

Phase 2 — Engine enrichment:
  Sub-Task 4  — Beneish M-Score
  Sub-Task 5  — SBC / Share Dilution
  Sub-Task 3  — DCF Sensitivity Matrix (report-only)

Phase 3 — Ranking algorithms:
  Sub-Task 7  — Magic Formula
  Sub-Task 8  — Sector-Relative Percentiles
  Sub-Task 9  — Score History Tracking

Phase 4 — Universe expansion:
  Sub-Task 10 — Russell 2000
  Sub-Task 11 — European Markets (Euro Stoxx 50 + BET)

Phase 5 — UX / Interactivity:
  Sub-Task 12 — Live Frontend Filtering
  Sub-Task 13 — Watchlist + Export
```

---

## Testing Policy

Every sub-task must:
1. Run `pytest tests/unit/ -q` → all 258 (+ new) tests pass
2. Run `python scripts/export_full_report.py` → report generates without error
3. Run `git push` → GitHub Pages updated automatically

New tests per sub-task:
- Sub-Task 1: ~4 new tests (ValuationResult fields, min_roe filter)
- Sub-Task 2: ~3 new tests (graham_number positive/negative/missing cases)
- Sub-Task 3: 0 new tests (report-only)
- Sub-Task 4: ~5 new tests (Beneish 8 indices, threshold, flag)
- Sub-Task 5: ~3 new tests (SBC adjustment, dilution)
- Sub-Task 6: ~4 new tests (dividend profile, payout ratio)
- Sub-Task 7: ~3 new tests (magic formula ranking)
- Sub-Task 8: ~2 new tests (percentile computation)
- Sub-Task 9: ~3 new tests (score history append/read)
- Sub-Task 10: ~2 new tests (Russell 2000 fallback)
- Sub-Task 11: ~2 new tests (BET/EuroStoxx fallback)
- Sub-Task 12-13: 0 new unit tests (JS/HTML only)
