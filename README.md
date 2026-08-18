# Stock Screener & Intrinsic Value Engine

A fully local, modular, parallel Python application that screens thousands of stocks worldwide and ranks them by **Margin of Safety** using relative valuation multiples, a two-method DCF model, and 52-week price position analysis.

> **100% local execution. No paid data subscriptions. No API keys required.**  
> Data source: Yahoo Finance via `yfinance`. Cache: DuckDB (structured, columnar, TTL-aware).

---

## What It Does

1. **Downloads** the live constituent list for S&P 500, NASDAQ-100, Dow Jones 30, or any custom CSV — automatically, from Wikipedia.
2. **Fetches** financial data for every ticker concurrently via `yfinance`, with a DuckDB-backed local cache (cold run ≈ 3 min for 503 tickers; cached re-run ≈ 8 seconds).
3. **Computes** per-company:
   - Relative multiples: P/E, P/B, EV/EBITDA, P/FCF, Net Debt/EBITDA
   - Two independent DCF intrinsic values: Gordon Growth Model + Exit Multiple
   - Margin of Safety % vs current price
   - **52-week Low / High / Position %** — where in the annual range the stock trades today
4. **Screens & ranks** using three built-in presets or your own YAML overrides.
5. **Dow Jones 30 mode** — pure ranking by 52-week position (closest to annual low = most upside potential), no MoS filter needed.
6. **Exports** results to CSV / Excel and generates a self-contained HTML executive report.

---

## Prerequisites

- Python 3.11+
- `pip`

---

## Installation

```bash
# 1. Clone / unzip the project
cd UndervaluedStocks

# 2. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows PowerShell
# source .venv/bin/activate   # macOS / Linux

# 3. Install all dependencies
pip install -r requirements.txt
```

---

## Quick Start

```bash
# Interactive wizard — press Enter to accept all defaults
python src/main.py

# S&P 500, Deep Value filter, 6 workers, export CSV
python src/main.py --universe sp500 --profile deep_value --workers 6 --export csv

# Dow Jones 30 — ranked by 52-week position (no MoS filter)
python src/main.py --universe dow30 --workers 6 --export csv

# NASDAQ-100, Buffett Quality filter
python src/main.py --universe nasdaq100 --profile buffett_quality --workers 8 --export both

# Custom ticker list from a CSV file
python src/main.py --universe custom --csv-path my_tickers.csv --profile high_fcf_yield

# Generate HTML executive report from the most recent CSV
python scripts/export_html_report.py

# Generate HTML from a specific CSV
python scripts/export_html_report.py --csv data/reports/20260818_200708_deep_value.csv
```

---

## CLI Reference

```
python src/main.py [OPTIONS]

Universe & Data
  --universe      sp500 | nasdaq100 | dow30 | world | custom   (default: world)
  --csv-path      Path to custom ticker CSV; required when --universe custom
                  CSV must have a column named 'ticker' (case-insensitive)
  --workers       Number of parallel fetch threads               (default: 8)
  --rps           Max yfinance API requests per second           (default: 2.0)

Screener
  --profile       deep_value | buffett_quality | high_fcf_yield  (default: deep_value)
                  Ignored when --universe dow30 (Dow mode always uses 52w ranking)

Export
  --export        csv | excel | both | none                      (default: csv)
                  Results saved to data/reports/<timestamp>_<profile>.[csv|xlsx]

DCF Model Assumptions
  --dcf-growth          Annual FCF / EBITDA growth rate          (default: 0.05)
  --dcf-discount        Discount rate / WACC                     (default: 0.10)
  --dcf-terminal        Terminal growth rate                     (default: 0.025)
  --dcf-years           Projection horizon in years              (default: 10)
  --dcf-exit-multiple   EV/EBITDA exit multiple                  (default: 12.0)
```

---

## Screener Profiles

Three built-in presets — all thresholds can be overridden in `config/screener_profiles.yaml`:

| Profile           | max P/E | max P/B | max EV/EBITDA | max P/FCF | max NetDebt/EBITDA | min MoS | Philosophy                                  |
|-------------------|---------|---------|---------------|-----------|---------------------|---------|---------------------------------------------|
| `deep_value`      | 15×     | 1.5×    | 8×            | 15×       | 2.5×                | 20%     | Benjamin Graham — tight multiples, high MoS |
| `buffett_quality` | 25×     | 4.0×    | 15×           | 25×       | 1.5×                | 15%     | Quality at a reasonable price, low leverage |
| `high_fcf_yield`  | 30×     | 5.0×    | 20×           | 12×       | 3.0×                | 10%     | Cash-flow focused — real earnings only      |

### Custom YAML overrides

Edit `config/screener_profiles.yaml` to override any threshold — unspecified fields keep their built-in defaults:

```yaml
deep_value:
  max_pe: 12.0
  min_margin_of_safety_pct: 30.0

my_custom_profile:
  name: my_custom_profile
  max_pe: 20.0
  max_pb: 3.0
  min_margin_of_safety_pct: 25.0
```

---

## Dow Jones 30 — 52-Week Ranking Mode

When `--universe dow30` is used, the screener switches to a **pure ranking** — no MoS filter applied. All 30 Dow companies are ranked by **52-week Position %**:

```
52w Position% = (Current Price − 52w Low) / (52w High − 52w Low) × 100
```

- **0%** = stock is exactly at its 52-week low (maximum potential upside, maximum downside safety)
- **100%** = stock is exactly at its 52-week high (minimum near-term upside)

This metric, combined with Market Cap and MoS columns, provides a quick view of which blue-chip companies are most attractively priced *relative to recent history* — without relying on the DCF model.

> **Real example (August 2026):**  NKE (Nike) ranked **#1** at 3.7% position — trading just 3.7% above its 52-week low while its fundamentals remain intact. Ibm Mihai identified this same opportunity using this criterion.

---

## Output Columns

### Standard screener output (deep_value / buffett_quality / high_fcf_yield)

| Column          | Description                                                          |
|-----------------|----------------------------------------------------------------------|
| Ticker          | Stock symbol                                                         |
| Company         | Short company name                                                   |
| Sector          | GICS sector                                                          |
| Industry        | GICS industry                                                        |
| Price           | Current market price (USD)                                           |
| 52w Low         | 52-week lowest price                                                 |
| 52w High        | 52-week highest price                                                |
| 52w Position%   | Where in the annual range the stock trades (0% = low, 100% = high)  |
| MoS%            | Margin of Safety vs DCF intrinsic value                              |
| P/E             | Price-to-Earnings (trailing)                                         |
| P/B             | Price-to-Book                                                        |
| EV/EBITDA       | Enterprise Value / EBITDA                                            |
| P/FCF           | Price / Free Cash Flow                                               |
| NetDebt/EBITDA  | Leverage ratio                                                       |
| DCF GGM         | Intrinsic value — Gordon Growth Model                                |
| DCF Exit        | Intrinsic value — Exit Multiple method                               |
| DCF Avg         | Average intrinsic value (used to compute MoS%)                       |

### Dow Jones 30 ranking output

| Column          | Description                                        |
|-----------------|----------------------------------------------------|
| Rank            | 1 = closest to 52-week low                         |
| Ticker          | Stock symbol                                       |
| Company         | Short company name                                 |
| Sector          | GICS sector                                        |
| Price           | Current price                                      |
| 52w Low         | 52-week lowest price                               |
| 52w High        | 52-week highest price                              |
| 52w Position%   | Annual range position (green < 33%, red > 66%)     |
| Market Cap ($B) | Market capitalisation in billions USD              |
| P/E             | Price-to-Earnings                                  |
| P/B             | Price-to-Book                                      |
| MoS%            | DCF Margin of Safety (informational only)          |

---

## HTML Executive Report

```bash
# Auto-detects the most recent CSV in data/reports/
python scripts/export_html_report.py

# Specify input and output explicitly
python scripts/export_html_report.py \
  --csv data/reports/20260818_200708_deep_value.csv \
  --out my_report.html
```

The script generates a **self-contained HTML file** (no external dependencies) with:
- Executive summary header with run metadata
- For **deep_value** reports: ranked company cards with MoS bar, all metrics, DCF explanation, value trap analysis, per-stock conviction text
- For **dow30** reports: ranked 52-week position table with visual position gauge per stock, sector distribution, methodology explanation
- Plain-English explanations of every metric and algorithm — suitable for non-technical readers
- Sector distribution chart
- Limitations and disclaimer section

---

## Streamlit Dashboard

```bash
# Install additional dependencies if not already installed
pip install streamlit plotly

# Launch the interactive dashboard
streamlit run dashboard/app.py
```

Features:
- Live parameter adjustment via sidebar sliders (DCF growth, WACC, terminal rate, exit multiple)
- Results table with sortable columns and progress bar for 52w Position
- Composite Score bar chart + sector distribution pie chart
- Per-company DCF sensitivity matrix (3×3 WACC × terminal growth)
- Works with the same DuckDB cache — cached re-runs are instant

---

## Value Trap Detection

A company is flagged `VALUE_TRAP` and excluded from results if **either** condition is true:

- **Net Debt / EBITDA > 3.5×** — debt burden exceeds 3.5 years of operating profit
- **All available FCF years ≤ 0** — the business has never generated positive free cash flow in the data window

Value Traps are always excluded from screener filters (unless `include_value_traps: true` is set in the YAML profile).

---

## Running Tests

```bash
# Unit tests only — fast, fully mocked, no internet required (default)
pytest

# Integration tests — real yfinance calls, requires internet
pytest -m integration

# All tests
pytest -m ""
```

Current test coverage: **113 unit tests** + **11 integration tests**, all passing.

---

## Cache Management

The DuckDB cache at `data/cache.duckdb` is auto-created on first run.

| Data type          | TTL      | Notes                               |
|--------------------|----------|-------------------------------------|
| ticker_info        | 30 days  | Price, multiples, 52w range, sector |
| financials         | 7 days   | Income statement (annual periods)   |
| cashflow           | 7 days   | Operating CF, CapEx, Free CF        |
| balance_sheet      | 7 days   | Assets, liabilities, equity         |

The cache **auto-migrates** when new columns are added (e.g. `week52_low`, `week52_high`) — no manual action needed. To force a full re-fetch:

```bash
# Windows PowerShell
Remove-Item data\cache.duckdb

# macOS / Linux
rm data/cache.duckdb
```

To refresh the bundled world ticker list with the latest S&P 500 + NASDAQ-100 constituents:

```bash
python src/universe.py --refresh-world
```

---

## Project Structure

```
UndervaluedStocks/
├── src/
│   ├── universe.py      Ticker list assembly (S&P 500, NASDAQ-100, Dow 30, world, custom)
│   ├── fetcher.py       Concurrent pipeline + DuckDB cache + auto-migration
│   ├── engine.py        Multiples + GGM DCF + Exit Multiple DCF + 52w metrics
│   ├── screener.py      Filter/rank presets + Dow 30 ranking mode
│   └── main.py          CLI entry point (argparse + interactive wizard)
│
├── scripts/
│   ├── export_html_report.py   Self-contained HTML report generator
│   └── gen_global_tickers.py   Regenerates data/global_tickers.csv
│
├── tests/
│   ├── unit/            Mocked tests — always run with plain `pytest`
│   └── integration/     Real network tests — `pytest -m integration`
│
├── data/
│   ├── global_tickers.csv    ~552 international tickers (tracked in git)
│   ├── cache.duckdb          Local DuckDB cache (gitignored, auto-created)
│   └── reports/              CSV / Excel / HTML exports (gitignored)
│
├── config/
│   └── screener_profiles.yaml   Optional YAML overrides for screener thresholds
│
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## DCF Model — How It Works

The engine computes two independent intrinsic value estimates per company and averages them.

### Method 1 — Gordon Growth Model (GGM)
> Conservative, cash-flow based.

1. Extract annual Free Cash Flow from the last 3–5 years.
2. Require **≥ 3 years of positive FCF** (otherwise: `INSUFFICIENT_DATA`).
3. Use the mean FCF as the base year estimate.
4. Project forward `N` years at `growth_rate`, discount at `discount_rate`.
5. Add terminal value: `TV = FCF_N × (1 + g) / (r − g)` (Gordon Growth formula).
6. Divide total present value by shares outstanding → intrinsic value per share.

### Method 2 — Exit Multiple
> Market-relative, EBITDA based.

1. Use current EBITDA as base.
2. Project EBITDA forward `N` years at `growth_rate`.
3. Terminal enterprise value = `EBITDA_N × exit_multiple`.
4. Subtract net debt → equity value; discount to present; divide by shares → per share.

### Default parameters

| Parameter           | Default | Rationale                                   |
|---------------------|---------|---------------------------------------------|
| `growth_rate`       | 5%      | Conservative — US long-run GDP average       |
| `discount_rate`     | 10%     | S&P 500 historical long-run average return   |
| `terminal_growth`   | 2.5%    | Long-run inflation + nominal GDP             |
| `projection_years`  | 10      | Standard DCF horizon                        |
| `exit_multiple`     | 12×     | Approximate S&P 500 median EV/EBITDA         |

All parameters are configurable via CLI flags (`--dcf-growth`, `--dcf-discount`, etc.).

---

## Filter Thresholds — Origin

All Deep Value thresholds trace back to classic value investing literature:

| Filter              | Source                                                           |
|---------------------|------------------------------------------------------------------|
| P/E ≤ 15×           | Benjamin Graham, *The Intelligent Investor* (1949)              |
| P/B ≤ 1.5×          | Graham's combined rule: P/E × P/B ≤ 22.5                       |
| EV/EBITDA ≤ 8×      | Joel Greenblatt, *The Little Book That Beats the Market*        |
| P/FCF ≤ 15×         | FCF yield ≥ 6.7% — attractive vs historical bond yields         |
| Net Debt/EBITDA ≤ 2.5× | Moody's / S&P investment-grade threshold for most sectors   |
| Margin of Safety ≥ 20% | Graham / Buffett — *Security Analysis* (1934)               |

---

## License

MIT — free to use, modify and distribute. Not financial advice.
