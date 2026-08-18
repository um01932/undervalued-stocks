# Stock Screener v2 — Improvements Plan

## Overview

The current system works correctly as an initial screener but has several quantitative issues
that reduce real-world signal quality:

1. **False positives from financial sector companies** — banks and insurers make up 7/11 deep-value
   candidates because classical FCF-based DCF is invalid for them (deposits ≠ debt,
   CapEx ≈ 0, regulated capital structures).
2. **Negative P/B passes filters** — HPQ's P/B of -190× passes the `<= 1.5` check because
   `_passes_filter()` in `screener.py` has no lower-bound guard (`_check()` returns `True`
   when `value is None`; negative values also satisfy `<= 1.5`).
3. **Static WACC for all companies** — a 10% flat discount rate ignores each company's actual
   risk profile (beta, leverage, sector).
4. **No quality filter** — a low P/E company with ROIC < WACC is destroying value, not creating it.
5. **No historical validation** — there is no way to know whether the current rule set would have
   outperformed the S&P 500 index in past years.

This plan addresses all five issues in five sequentially-ordered phases. Each phase is
self-contained and can be implemented and deployed independently.

---

## Architecture Overview

```
src/
  universe.py      (unchanged)
  fetcher.py       (Phase 1: add more yfinance fields; Phase 3: beta + div yield)
  engine.py        (Phase 1: fix P/B guard; Phase 2: Piotroski + Altman + ROIC;
                   Phase 3: dynamic WACC; Phase 4: composite score + sensitivity)
  screener.py      (Phase 1: sector routing; Phase 2: score-based ranking)
  main.py          (Phase 2: --score-mode flag; Phase 4: --backtest flag)
  backtester.py    (Phase 4: NEW)
  dashboard/
    app.py         (Phase 5: NEW — Streamlit entry point)

data/
  cache.duckdb     (auto-migrated with new columns as needed)
```

---

## Phase 1 — Critical Bug Fixes & Sector Routing

**Status:** [ ] pending

### Intent
Fix the two most impactful correctness bugs before adding any new features:
- Negative P/B passing filters (HPQ -190×)
- Financial-sector companies dominating results due to invalid DCF assumptions

### Expected Outcomes
- HPQ (and any company with negative book equity) is rejected at the filter stage
- Banks, diversified financials, and insurance companies are excluded from standard DCF
  and either (a) valued with a simplified Dividend Discount Model or (b) excluded from
  DCF scoring while still appearing in the screener with multiples-only evaluation
- Unit tests confirm the new behaviour

### Sub-tasks

#### 1a — Fix P/B negative lower-bound guard (`screener.py`)

**File:** `src/screener.py`, function `_passes_filter()` (~line 171)

Current code:
```python
def _check(value, max_val):
    if max_val is None: return True
    if value is None: return True      # ← missing data passes
    return value <= max_val            # ← negative value also passes (HPQ: -190 <= 1.5)
```

Change `_check()` to reject **negative values** for multiples where negative is meaningless
(P/B, P/E, P/FCF, EV/EBITDA). Net Debt/EBITDA may legitimately be negative (net cash),
so it keeps the old behaviour.

Add a `allow_negative: bool = False` parameter to `_check()`. Call it with
`allow_negative=True` only for `net_debt_ebitda`.

Also add `ScreenerProfile` field `min_pb: float = 0.0` (default 0) so YAML overrides can
relax this if needed.

**Tests to update:** `tests/unit/test_screener.py` — add cases for negative P/B, negative P/E.

#### 1b — Add `sector` classification utility (`engine.py` or new `src/sector_utils.py`)

Create a small helper `is_financial_sector(sector: str | None) -> bool` that returns `True`
for sectors where classical DCF is unreliable:
- `"Financial Services"` (banks, credit services, asset management)
- `"Insurance"` (already captured under Financial Services in yfinance)

Sectors where DCF is valid: all others (Technology, Healthcare, Industrials, etc.).

**File:** `src/engine.py` — add `_is_financial_sector(sector)` helper.

#### 1c — Sector-aware routing in `evaluate()` (`engine.py`)

Modify `evaluate()` to check `data.info.get("sector")`:

- If financial sector → skip `compute_dcf_ggm()` and `compute_dcf_exit()`.
  Set `status = "SECTOR_EXCLUDED"` (new literal) OR set `dcf_intrinsic_value = None`
  and `status = "INSUFFICIENT_DATA"` (simpler — no downstream changes needed).
- Non-financial → current logic unchanged.

**Decision:** Use `status = "INSUFFICIENT_DATA"` for financial companies with no valid DCF.
This excludes them from MoS ranking while still showing multiples.

Add a `ValuationResult` field `sector_excluded: bool = False` for reporting transparency.

**Tests to update:** `tests/unit/test_engine.py` — add `test_evaluate_financial_sector_skips_dcf`.

#### 1d — DDM fallback for financials (optional, additive)

If a financial-sector company pays dividends (fetch `dividendYield` and `dividendRate` from
yfinance info), compute a simple Gordon Growth DDM:

```
P_intrinsic = (Dividend_per_share * (1 + g)) / (r - g)
```

Using `g = 0.03` (conservative dividend growth) and `r = 0.10`.

Store result in `dcf_ggm_intrinsic` (reusing the field). Mark `ValuationResult.dcf_model_used`
(new string field, e.g. `"DDM"` vs `"GGM"` vs `"Exit"`) for transparency.

**Add to `INFO_FIELD_MAP` in `fetcher.py`:** `"dividendYield"`, `"dividendRate"`.

**Priority:** Nice-to-have in Phase 1. Can defer to Phase 2 if timeline is tight.

#### 1e — Update `export_html_report.py` to note sector exclusions

Add a small info box in both deep_value and dow30 reports when `sector_excluded` companies
are detected in the failed ticker list.

---

## Phase 2 — Quality Filters: Piotroski F-Score, Altman Z-Score, ROIC

**Status:** [ ] pending

### Intent
Add three quantitative quality filters that distinguish "cheap and deteriorating" (value trap)
from "cheap and fundamentally sound." All three can be computed from data already cached in
DuckDB (financials + balance_sheet + cashflow tables).

### Expected Outcomes
- Each `ValuationResult` carries `piotroski_score: Optional[int]` (0–9),
  `altman_z: Optional[float]`, and `roic: Optional[float]`
- `ScreenerProfile` gains `min_piotroski: Optional[int]` and `min_roic: Optional[float]`
- Deep value preset updated: `min_piotroski = 6`
- New screener profile `quality_value` combining MoS + Piotroski + ROIC

### Sub-tasks

#### 2a — Compute Piotroski F-Score (`engine.py`)

The F-Score has 9 binary signals (1 point each, max 9). All inputs come from existing
cached tables (financials + cashflow + balance_sheet), 2–3 years of annual data needed.

**9 signals:**
```
Profitability (4 points):
  F1: ROA > 0          (net_income / total_assets > 0)
  F2: CFO > 0          (operating_cashflow > 0)
  F3: ROA increasing   (current_year ROA > prior_year ROA)
  F4: Accruals < 0     (CFO / total_assets > ROA — cash earnings exceed accrual earnings)

Leverage & Liquidity (3 points):
  F5: Leverage falling (long-term debt / total_assets decreased year-over-year)
  F6: Liquidity rising (current ratio increased — needs current_assets & current_liabilities)
  F7: No equity dilution (shares_outstanding did not increase)

Operating Efficiency (2 points):
  F8: Gross margin improving (gross_profit / total_revenue increasing)
  F9: Asset turnover improving (total_revenue / total_assets increasing)
```

**Data gaps:** `current_assets` and `current_liabilities` are not currently stored in
`balance_sheet`. Add them to `_BS_ROW_MAP` in `fetcher.py` (yfinance keys:
`"Current Assets"` and `"Current Liabilities"`).

**New function:** `compute_piotroski(data: TickerData) -> Optional[int]` in `engine.py`.
Returns `None` if fewer than 2 years of data are available.

#### 2b — Compute Altman Z-Score (`engine.py`)

Classic 5-factor formula for non-financial companies:
```
Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5

X1 = Working Capital / Total Assets
     Working Capital = Current Assets - Current Liabilities
X2 = Retained Earnings / Total Assets
     Retained Earnings = stockholders_equity - paid_in_capital (approx: use net_income cumulative)
     Simplification: use (stockholders_equity - total_assets * 0.3) as proxy
X3 = EBIT / Total Assets
X4 = Market Cap / Total Liabilities
X5 = Total Revenue / Total Assets

Zones:
  Z > 2.99  → Safe Zone
  1.81-2.99 → Grey Zone
  Z < 1.81  → Distress Zone (exclude from deep_value)
```

**Note:** Altman Z-Score is only valid for non-financial companies. Skip for
`is_financial_sector()` companies.

**New function:** `compute_altman_z(data: TickerData) -> Optional[float]` in `engine.py`.

#### 2c — Compute ROIC (`engine.py`)

```
ROIC = NOPAT / Invested Capital

NOPAT = EBIT * (1 - tax_rate)
      ≈ ebit * 0.79  (using 21% US corporate tax rate as default)

Invested Capital = Total Assets - Current Liabilities - Excess Cash
                 ≈ total_assets - total_cash - (current_liabilities if available else 0)
```

Fetch `ebit` from the `financials` table (already stored). Use most recent year.

**New function:** `compute_roic(data: TickerData) -> Optional[float]` in `engine.py`.

#### 2d — Add new fields to `ValuationResult` and `ScreenerProfile`

In `engine.py` — add to `ValuationResult`:
```python
piotroski_score: Optional[int] = None
altman_z: Optional[float] = None
roic: Optional[float] = None
sector_excluded: bool = False
dcf_model_used: Optional[str] = None   # "GGM", "Exit", "GGM+Exit", "DDM", None
```

In `screener.py` — add to `ScreenerProfile`:
```python
min_piotroski: Optional[int] = None
min_roic: Optional[float] = None
exclude_altman_distress: bool = True   # default: exclude Z < 1.81
```

Update `_passes_filter()` to check these three new fields.

Update `BUILTIN_PROFILES`:
- `deep_value`: `min_piotroski=6`, `exclude_altman_distress=True`, `min_roic=8.0`
- `buffett_quality`: `min_piotroski=7`, `min_roic=12.0`
- `high_fcf_yield`: `min_piotroski=5`

#### 2e — Composite Score (0–100) in `screener.py`

Add function `compute_composite_score(result: ValuationResult) -> Optional[float]`.

Four pillars with weights:
```
Valuation      (30%): normalised MoS% + P/FCF rank + EV/EBITDA rank
Quality/Moat   (25%): ROIC rank + gross margin rank (gross_profit/revenue)
Financial Health(25%): Piotroski/9 × 100 + Altman safety zone bonus
Price Momentum (20%): (100 - 52w_position_pct) — lower position = higher score
```

Add `composite_score: Optional[float]` to `ValuationResult`.
Add `composite_score` column to all output DataFrames in `screener.py`.

A new screener profile `quality_value` sorts by `composite_score` descending instead of `MoS%`.

#### 2f — Update output columns and HTML report

Add columns `Piotroski`, `Altman Z`, `ROIC%`, `Score` to:
- `_OUTPUT_COLUMNS` in `screener.py`
- `render_table()` in `main.py` (with colour coding: Piotroski green ≥ 7, yellow 5-6, red < 5)
- Per-company metric cells in `export_html_report.py`

Add a "Quality Indicators" section to the deep_value HTML report with plain-English
explanation of Piotroski, Altman, and ROIC.

---

## Phase 3 — Dynamic WACC Per Company

**Status:** [ ] pending

### Intent
Replace the flat 10% discount rate with a per-company WACC calculated from real market data:
beta, current risk-free rate (US 10Y Treasury), and the company's actual capital structure.

### Expected Outcomes
- Each company's DCF uses a WACC calibrated to its risk profile
- High-beta tech companies get a higher discount rate (→ lower intrinsic value)
- Low-beta utilities get a lower discount rate (→ higher intrinsic value)
- `DCFParams` becomes a fallback when live data is unavailable

### Sub-tasks

#### 3a — Fetch additional yfinance fields (`fetcher.py`)

Add to `INFO_FIELD_MAP`:
```python
"beta":             "beta",
"dividendYield":    "dividend_yield",
"dividendRate":     "dividend_rate",
"payoutRatio":      "payout_ratio",
"returnOnEquity":   "roe",
"returnOnAssets":   "roa",
"grossMargins":     "gross_margin",
"operatingMargins": "operating_margin",
"interestExpense":  "interest_expense",   # from financials statement
```

Add corresponding columns to `ticker_info` DuckDB table. The auto-migration code in
`CacheStore.__init__()` will handle existing caches.

#### 3b — Fetch US 10Y Treasury yield (`fetcher.py`)

Use yfinance ticker `"^TNX"` (CBOE 10Y Treasury Note Yield) to get the current risk-free rate.
Cache it in a new `macro_data` DuckDB table with a 1-day TTL:
```sql
CREATE TABLE IF NOT EXISTS macro_data (
    key        TEXT PRIMARY KEY,
    value      DOUBLE,
    fetched_at TIMESTAMP NOT NULL
)
```

Fetch once per run (not per ticker). Use value divided by 100 as decimal rate.

#### 3c — Dynamic WACC calculation (`engine.py`)

Add function `compute_wacc(data: TickerData, rf_rate: float) -> float`:

```
Ke = rf_rate + beta * ERP       (ERP = Equity Risk Premium = 5.5% historical average)
Kd = (interest_expense / total_debt) if both available else 0.05
T  = 0.21                        (US corporate tax rate)

E  = market_cap
D  = total_debt
V  = E + D

WACC = (E/V * Ke) + (D/V * Kd * (1 - T))

Bounds: clamp WACC to [0.06, 0.18] to avoid extreme values from data noise
Fallback: if beta or capital structure unavailable → use DCFParams.discount_rate
```

#### 3d — Sustainable growth rate (`engine.py`)

Replace the flat `DCFParams.growth_rate` with a company-specific estimate when possible:

```
Sustainable Growth Rate = ROIC × Reinvestment Rate
Reinvestment Rate = 1 - (FCF / Net_Income)   (fraction of earnings retained)

Clamp to [0.02, 0.12] — never assume growth below inflation or above 12%
Fallback: DCFParams.growth_rate (5%)
```

Modify `compute_dcf_ggm()` and `compute_dcf_exit()` to accept optional
`wacc_override: float` and `growth_override: float` parameters.

#### 3e — Surface WACC in output

Add `wacc_used: Optional[float]` and `growth_used: Optional[float]` to `ValuationResult`.
Show them as tooltip-only metric cells in the HTML report (`cursor:help`).

---

## Phase 4 — Backtesting Engine

**Status:** [ ] pending

### Intent
Validate the rule set historically: simulate running the screener at the start of each year
from 2018 to 2024 using only data available at that point (no look-ahead bias), then measure
the portfolio performance vs S&P 500.

### Expected Outcomes
- Command: `python src/main.py --backtest --universe sp500 --profile deep_value`
- Output: summary table of CAGR, Sharpe Ratio, Max Drawdown, Win Rate per year and overall
- Output file: `data/reports/<timestamp>_backtest_<profile>.csv`

### Sub-tasks

#### 4a — Historical data source

Use `yfinance.download(tickers, start=..., end=..., auto_adjust=True)` for:
- Annual price snapshots (first trading day of each year)
- S&P 500 benchmark via `"^GSPC"` ticker

For financial fundamentals, yfinance does not provide point-in-time historical financials
(it only provides the latest filings). **This is the key limitation.** Document it clearly.

**Approach:** Use the most recent available fundamentals as a proxy (acceptable for
multi-year backtesting where sector composition doesn't change dramatically).

Store historical price data in a new `price_history` DuckDB table:
```sql
CREATE TABLE IF NOT EXISTS price_history (
    ticker      TEXT NOT NULL,
    date        DATE NOT NULL,
    close       DOUBLE NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, date)
)
```

#### 4b — Walk-forward portfolio simulation (`src/backtester.py`)

New module. Entry function:
```python
def run_backtest(
    tickers: list[str],
    cache: CacheStore,
    profile: ScreenerProfile,
    dcf_params: DCFParams,
    start_year: int = 2018,
    end_year: int = 2024,
    top_n: int = 10,
    benchmark: str = "^GSPC",
) -> pd.DataFrame
```

Algorithm per year:
1. Evaluate all tickers using current fundamentals (proxy for historical)
2. Apply profile filter → select top N by MoS
3. Record portfolio weights (equal-weight)
4. Fetch price from Jan 1 of year and Jan 1 of year+1
5. Compute annual return for portfolio vs benchmark

#### 4c — Performance metrics (`src/backtester.py`)

Compute from the annual return series:
- **CAGR**: `(end_value / start_value)^(1/n) - 1`
- **Volatility**: `std(annual_returns) * sqrt(1)` (annual)
- **Sharpe Ratio**: `(CAGR - rf_rate) / volatility`
- **Sortino Ratio**: `(CAGR - rf_rate) / downside_deviation`
- **Max Drawdown**: worst peak-to-trough decline in portfolio value
- **Win Rate**: % of selected tickers that outperformed benchmark in their holding period

#### 4d — CLI integration (`main.py`)

Add `--backtest` flag to `_build_parser()`. When set:
- Skip the normal screen/display pipeline
- Run `run_backtest()` from `backtester.py`
- Print a summary rich table
- Export to CSV

---

## Phase 5 — Streamlit Interactive Dashboard

**Status:** [ ] pending

### Intent
Replace the static HTML report with a live interactive dashboard that allows non-technical
users to adjust DCF assumptions via sliders and see results update instantly.

### Expected Outcomes
- `streamlit run dashboard/app.py` launches a local web app
- User can select universe, profile, adjust all DCF parameters via sliders
- Sensitivity matrix table: intrinsic value at 3×3 grid of WACC vs terminal growth
- Results table is sortable and filterable in-browser
- No external server or paid service required

### Sub-tasks

#### 5a — Project setup

Add `streamlit>=1.35` and `plotly>=5.20` to `requirements.txt`.

Create `dashboard/` directory with `__init__.py` and `app.py`.

#### 5b — Core layout (`dashboard/app.py`)

Sidebar controls:
- Universe selector (sp500 / nasdaq100 / dow30 / world)
- Profile selector (deep_value / buffett_quality / high_fcf_yield / quality_value)
- DCF sliders: growth rate, discount rate, terminal growth, exit multiple
- Workers slider (2–16)
- Run button

Main panel:
- Stats bar (tickers screened / evaluated / passed / value traps)
- Results DataFrame (interactive, sortable via `st.dataframe` with column config)
- Composite Score bar chart (Plotly)
- Sector distribution pie chart (Plotly)
- Per-company expander with DCF sensitivity matrix

#### 5c — DCF Sensitivity Matrix

For each company in results, display a 3×3 table showing intrinsic value at:
- WACC: base−2%, base, base+2%
- Terminal growth: 1.5%, 2.5%, 3.5%

Colour-code cells: green if intrinsic > current price, red if not.

#### 5d — Caching for Streamlit (`@st.cache_data`)

Wrap `fetch_universe()` and `evaluate()` calls with `@st.cache_data(ttl=3600)` so the
dashboard doesn't re-fetch on every slider interaction.

The existing DuckDB cache already handles the heavy lifting — Streamlit caching only
prevents re-running the pipeline on parameter changes that don't affect data.

---

## Recommended Implementation Order

The phases above are ordered by **impact-to-effort ratio**:

| Phase | Impact | Effort | Priority |
|-------|--------|--------|----------|
| 1 — Bug fixes + sector routing | High (fixes false positives) | Low | **Do first** |
| 2 — Piotroski + Altman + ROIC + Score | High (adds real quality signal) | Medium | **Do second** |
| 3 — Dynamic WACC | Medium (improves DCF accuracy) | Medium | Do third |
| 4 — Backtesting | High (validates the whole system) | High | Do fourth |
| 5 — Streamlit dashboard | Medium (UX improvement) | Medium | Do last |

Phase 1 has zero new dependencies and fixes two known false positives visible in current output.
It should be implemented before sharing the tool with others.

Phase 2 adds the most analytical depth and is achievable purely from data already in the
DuckDB cache (no new yfinance calls needed for Piotroski/Altman).

---

## New Dependencies Required

| Phase | Package | Version | Purpose |
|-------|---------|---------|---------|
| 3 | none | — | Beta and Treasury from yfinance (already installed) |
| 4 | none | — | yfinance.download already available |
| 5 | `streamlit` | ≥1.35 | Interactive dashboard |
| 5 | `plotly` | ≥5.20 | Charts in Streamlit |

---

## Files Touched Per Phase

| File | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
|------|---------|---------|---------|---------|---------|
| `src/fetcher.py` | DDM fields | new BS fields | beta/rf/margins | price_history table | — |
| `src/engine.py` | sector routing, P/B fix | Piotroski, Altman, ROIC, composite score | dynamic WACC | — | — |
| `src/screener.py` | negative guard, SECTOR_EXCLUDED | new filter fields, quality_value profile | — | — | — |
| `src/main.py` | — | --score-mode | — | --backtest | — |
| `src/backtester.py` | — | — | — | NEW | — |
| `dashboard/app.py` | — | — | — | — | NEW |
| `scripts/export_html_report.py` | sector note | Piotroski/score cards | sensitivity matrix | backtest section | — |
| `tests/unit/test_engine.py` | sector routing | Piotroski, Altman, ROIC | WACC calc | — | — |
| `tests/unit/test_screener.py` | negative P/B | new filter fields | — | — | — |
| `requirements.txt` | — | — | — | — | streamlit, plotly |
