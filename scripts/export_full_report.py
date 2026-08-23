"""
export_full_report.py — Generate a single consolidated HTML executive report
combining ALL screener profiles + backtest results with S&P 500 benchmark comparison.

Reads the most recent CSV for each known profile from data/reports/ and the most
recent backtest CSV, then writes one self-contained HTML file.

Usage:
    python scripts/export_full_report.py
    python scripts/export_full_report.py --out data/reports/my_report.html
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPORTS_DIR = Path(__file__).parent.parent / "data" / "reports"
DB_PATH     = Path(__file__).parent.parent / "data" / "cache.duckdb"

# ── Price history cache (OHLCV for Why-Buy charts) ────────────────────────────

def _ensure_ohlcv_table() -> None:
    """Create ohlcv_cache table if it doesn't exist."""
    import duckdb
    con = duckdb.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_cache (
            ticker      VARCHAR NOT NULL,
            date        VARCHAR NOT NULL,
            open        DOUBLE,
            high        DOUBLE,
            low         DOUBLE,
            close       DOUBLE,
            volume      BIGINT,
            fetched_at  TIMESTAMP NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    con.close()


def _fetch_price_history(tickers: list[str]) -> dict[str, list[dict]]:
    """
    Fetch 1-year daily OHLCV for each ticker.
    Uses ohlcv_cache in DuckDB (TTL = 1 day).
    Returns {ticker: [{date, open, high, low, close, volume}, ...]} sorted asc.
    """
    try:
        import duckdb
        import yfinance as yf
    except ImportError:
        return {}

    _ensure_ohlcv_table()
    con    = duckdb.connect(str(DB_PATH))
    result = {}
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    for tkr in tickers:
        # Check cache first
        cached = con.execute("""
            SELECT date, open, high, low, close, volume
            FROM ohlcv_cache
            WHERE ticker = ? AND fetched_at >= ?
            ORDER BY date ASC
        """, [tkr, cutoff]).fetchall()

        if len(cached) >= 200:          # enough data in cache
            result[tkr] = [
                {"date": r[0], "open": r[1], "high": r[2],
                 "low": r[3], "close": r[4], "volume": r[5]}
                for r in cached
            ]
            continue

        # Fetch from yfinance
        try:
            time.sleep(0.3)             # gentle rate limit
            hist = yf.Ticker(tkr).history(period="1y", interval="1d", auto_adjust=True)
            if hist.empty:
                continue
            rows = []
            now_ts = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            for dt, row in hist.iterrows():
                date_str = str(dt)[:10]
                rows.append({
                    "date":   date_str,
                    "open":   float(row["Open"])   if row["Open"]   == row["Open"] else None,
                    "high":   float(row["High"])   if row["High"]   == row["High"] else None,
                    "low":    float(row["Low"])    if row["Low"]    == row["Low"]  else None,
                    "close":  float(row["Close"])  if row["Close"]  == row["Close"]else None,
                    "volume": int(row["Volume"])   if row["Volume"] == row["Volume"] else None,
                })
            # Upsert into cache
            con.execute("DELETE FROM ohlcv_cache WHERE ticker = ?", [tkr])
            for r in rows:
                con.execute("""
                    INSERT INTO ohlcv_cache
                        (ticker, date, open, high, low, close, volume, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [tkr, r["date"], r["open"], r["high"], r["low"],
                      r["close"], r["volume"], now_ts])
            result[tkr] = rows
        except Exception as exc:
            print(f"  [chart] {tkr}: {exc}")

    con.close()
    return result

# ── Module-level data (populated at build time) ───────────────────────────────
_PRICE_DATA:         dict[str, list[dict]]  = {}
_SCORE_HISTORY_DATA: dict[str, list[float]] = {}

# ── Score history ─────────────────────────────────────────────────────────────

def _load_score_history(tickers: list[str]) -> dict[str, list[float]]:
    """Load composite_score history for given tickers from DuckDB score_history table.
    Returns {ticker: [score1, score2, ...]} sorted by run_date ASC, last 8 entries."""
    try:
        import duckdb
        con = duckdb.connect(str(DB_PATH))
        result = {}
        for tkr in tickers:
            rows = con.execute(
                """
                SELECT run_date, AVG(composite_score) as avg_score
                FROM score_history
                WHERE ticker = ? AND composite_score IS NOT NULL
                GROUP BY run_date
                ORDER BY run_date ASC
                """,
                [tkr],
            ).fetchall()
            if len(rows) >= 2:
                result[tkr] = [r[1] for r in rows[-8:]]  # last 8 data points
        con.close()
        return result
    except Exception:
        return {}


def _sparkline_svg(scores: list[float]) -> str:
    """Mini 80×20 SVG sparkline for score history. Green trend up, red trend down."""
    if len(scores) < 2:
        return ""
    mn, mx = min(scores), max(scores)
    span = mx - mn or 1.0
    W, H = 80, 20
    pts = []
    for i, s in enumerate(scores):
        x = round(i / (len(scores) - 1) * W, 1)
        y = round(H - (s - mn) / span * H, 1)
        pts.append(f"{x},{y}")
    colour = "#16a34a" if scores[-1] >= scores[0] else "#ef4444"
    trend  = "↑" if scores[-1] >= scores[0] else "↓"
    polyline = " ".join(pts)
    return (
        f'<span style="display:inline-flex;align-items:center;gap:4px;margin-left:8px">'
        f'<svg width="{W}" height="{H}" style="display:inline-block;vertical-align:middle">'
        f'<polyline points="{polyline}" fill="none" stroke="{colour}" stroke-width="1.5" stroke-linejoin="round"/>'
        f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="2.5" fill="{colour}"/>'
        f'</svg>'
        f'<span style="font-size:10px;font-weight:700;color:{colour}">{trend} {scores[-1]:.0f}</span>'
        f'</span>'
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fv(val: str) -> float | None:
    if not val or val.strip() in ("", "nan", "inf", "-inf", "None"):
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except ValueError:
        return None


def _fmt(val: str, decimals: int = 2, suffix: str = "", prefix: str = "") -> str:
    f = _fv(val)
    if f is None:
        return "—"
    return f"{prefix}{f:,.{decimals}f}{suffix}"


def _pct_fmt(val: str, decimals: int = 1) -> str:
    f = _fv(val)
    if f is None:
        return "—"
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.{decimals}f}%"


def _load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _most_recent(pattern: str, exclude_backtest: bool = False) -> Path | None:
    files = sorted(REPORTS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if exclude_backtest:
        files = [f for f in files if "backtest" not in f.stem]
    return files[0] if files else None


def _mos_colour(mos: float) -> str:
    if mos >= 60: return "#16a34a"
    if mos >= 45: return "#22c55e"
    if mos >= 30: return "#84cc16"
    if mos >= 20: return "#eab308"
    return "#f97316"


def _mos_grade(mos: float) -> tuple[str, str]:
    if mos >= 60: return "A+", "Exceptional"
    if mos >= 45: return "A",  "Strong Buy"
    if mos >= 30: return "B+", "Buy"
    if mos >= 20: return "B",  "Moderate Buy"
    return "C", "Watch"


def _pos_colour(pos: float) -> str:
    if pos < 33: return "#16a34a"
    if pos < 66: return "#eab308"
    return "#e11d48"


def _return_colour(v: float) -> str:
    return "#16a34a" if v >= 0 else "#e11d48"


def _piotroski_colour(v: float) -> str:
    if v >= 7: return "#16a34a"
    if v >= 4: return "#eab308"
    return "#e11d48"


def _altman_colour(v: float) -> str:
    if v >= 2.99: return "#16a34a"
    if v >= 1.0:  return "#eab308"
    return "#e11d48"


# ── Why Buy reasoning generator ──────────────────────────────────────────────

# Sector-specific plain-English descriptions used in the reasoning text
_SECTOR_CONTEXT = {
    "Technology":              "technology",
    "Communication Services":  "media and communications",
    "Healthcare":              "healthcare",
    "Financials":              "financial services",
    "Financial Services":      "financial services",
    "Consumer Defensive":      "consumer staples",
    "Consumer Cyclical":       "consumer discretionary",
    "Energy":                  "energy",
    "Industrials":             "industrials",
    "Basic Materials":         "basic materials",
    "Real Estate":             "real estate",
    "Utilities":               "utilities",
}

# What each profile means in plain English
_PROFILE_PLAIN = {
    "deep_value":      "all six Deep Value quality filters simultaneously (the tightest possible screen)",
    "buffett_quality": "the Buffett Quality screen — combining strong returns on capital with a reasonable price",
    "high_fcf_yield":  "the High Free Cash Flow Yield screen — exceptional real cash generation per dollar invested",
    "quality_value":   "the Quality Value screen — financially healthy balance sheet with a clear discount to intrinsic value",
    "dividend_growth": "the Dividend Growth screen — targeting companies with sustainable dividends backed by strong free cash flow",
}




def _52w_bar(price_v: float | None, low_v: float | None, high_v: float | None) -> str:
    """Pure-CSS 52-week range bar.
    ONE outer position:relative box — price label, track, and dot all share
    the exact same coordinate space, so left:N% is always accurate."""
    if low_v is None or high_v is None or price_v is None:
        return ""
    span = high_v - low_v
    if span <= 0:
        return ""
    pct     = min(max((price_v - low_v) / span, 0.0), 1.0)
    pct_css = f"{pct * 100:.3f}%"
    mc  = "#16a34a" if pct < 0.33 else ("#eab308" if pct < 0.66 else "#e11d48")
    lbl = "near annual low ✓" if pct < 0.25 else ("near annual high ⚠" if pct > 0.75 else f"{pct*100:.0f}% of range")

    # Single position:relative wrapper — 46px tall:
    #   0–16px  : price label area
    #   17–29px : track bar (height 12px, centered at y=23)
    #   30–46px : low/high text (rendered outside via flex row below)
    return f"""
<div style="margin:4px 0 0">
  <!-- single coordinate-space wrapper -->
  <div style="position:relative;height:30px">
    <!-- gradient track (sits at bottom of wrapper) -->
    <div style="position:absolute;left:0;right:0;bottom:0;height:12px;border-radius:6px;
                background:linear-gradient(to right,#16a34a88,#eab30888,#e11d4888)"></div>
    <!-- price label — same left% as dot, centered above track -->
    <div style="position:absolute;left:{pct_css};top:0;transform:translateX(-50%);
                font-size:10px;font-weight:700;color:{mc};font-family:monospace;
                white-space:nowrap;line-height:1">${price_v:,.2f}</div>
    <!-- dot — same left% on the track -->
    <div style="position:absolute;left:{pct_css};bottom:-1px;transform:translateX(-50%);
                width:14px;height:14px;border-radius:50%;
                background:{mc};border:2px solid #fff;
                box-shadow:0 1px 4px rgba(0,0,0,.28)"></div>
  </div>
  <!-- low / label / high below -->
  <div style="display:flex;justify-content:space-between;
              margin-top:5px;font-size:10px;font-family:monospace">
    <span style="color:#8d96a0">${low_v:,.0f}</span>
    <span style="color:{mc};font-weight:700">{pct*100:.0f}% — {lbl}</span>
    <span style="color:#8d96a0">${high_v:,.0f}</span>
  </div>
</div>"""


def _price_chart_svg(ohlc: list[dict], ticker: str = "") -> str:
    """
    Inline SVG candlestick chart for 52-week daily OHLC data.
    Width: 100% of container. Height: 130px chart + 18px date labels.
    Green candle = close >= open. Red candle = close < open.
    Wick = high/low thin line. Volume bars rendered at the bottom (20px).
    """
    if not ohlc:
        return ""

    # ── Filter valid rows ─────────────────────────────────────────────────────
    rows = [r for r in ohlc
            if r.get("open") and r.get("high") and r.get("low") and r.get("close")]
    if len(rows) < 10:
        return ""

    # ── Geometry ──────────────────────────────────────────────────────────────
    W       = 700   # SVG coordinate width (viewBox)
    H_CHART = 110   # candle area height
    H_VOL   = 22    # volume bar area height
    H_LABEL = 14    # date label area height
    H_TOTAL = H_CHART + H_VOL + H_LABEL + 6
    PAD_L   = 6
    PAD_R   = 6
    chart_w = W - PAD_L - PAD_R

    n       = len(rows)
    col_w   = chart_w / n                # width per candle column
    body_w  = max(col_w * 0.6, 1.5)     # candle body width

    # ── Price scale ───────────────────────────────────────────────────────────
    all_highs = [r["high"] for r in rows]
    all_lows  = [r["low"]  for r in rows]
    p_max = max(all_highs) * 1.01
    p_min = min(all_lows)  * 0.99
    p_span = p_max - p_min or 1.0

    def py(price: float) -> float:
        """Map price to SVG Y coordinate (top=0 = highest price)."""
        return round(H_CHART * (1.0 - (price - p_min) / p_span), 2)

    # ── Volume scale ─────────────────────────────────────────────────────────
    vols     = [r.get("volume") or 0 for r in rows]
    v_max    = max(vols) or 1
    vol_y0   = H_CHART + 4   # top of volume area

    def vy(vol: int) -> float:
        return round(H_VOL * (vol / v_max), 2)

    # ── Build SVG elements ────────────────────────────────────────────────────
    candles   = []
    vol_bars  = []
    date_lbls = []

    # Decide which dates to label (every ~8 weeks ≈ 40 candles)
    label_step = max(1, n // 8)

    for i, r in enumerate(rows):
        cx   = PAD_L + (i + 0.5) * col_w   # center x of candle
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        green   = c >= o
        colour  = "#22c55e" if green else "#ef4444"
        body_t  = py(max(o, c))
        body_b  = py(min(o, c))
        body_h  = max(body_b - body_t, 1.0)

        # Wick (high–low)
        candles.append(
            f'<line x1="{cx:.1f}" y1="{py(h):.1f}" x2="{cx:.1f}" y2="{py(l):.1f}"'
            f' stroke="{colour}" stroke-width="0.8" stroke-opacity="0.8"/>'
        )
        # Body
        x_left = cx - body_w / 2
        candles.append(
            f'<rect x="{x_left:.1f}" y="{body_t:.1f}"'
            f' width="{body_w:.1f}" height="{body_h:.1f}"'
            f' fill="{colour}" fill-opacity="0.9"/>'
        )

        # Volume bar
        vol    = r.get("volume") or 0
        vbar_h = vy(vol)
        vbar_y = vol_y0 + H_VOL - vbar_h
        vol_bars.append(
            f'<rect x="{x_left:.1f}" y="{vbar_y:.1f}"'
            f' width="{body_w:.1f}" height="{vbar_h:.1f}"'
            f' fill="{colour}" fill-opacity="0.4"/>'
        )

        # Date label every N steps
        if i % label_step == 0 or i == n - 1:
            dstr = r["date"][5:]    # MM-DD
            date_lbls.append(
                f'<text x="{cx:.1f}" y="{H_CHART + H_VOL + H_LABEL + 2}"'
                f' font-size="8" fill="#9ca3af" text-anchor="middle"'
                f' font-family="monospace">{dstr}</text>'
            )

    # ── Price axis labels (3 levels) ─────────────────────────────────────────
    price_lbls = []
    for frac in (0.0, 0.5, 1.0):
        p = p_min + frac * p_span
        y = py(p)
        price_lbls.append(
            f'<text x="{W - PAD_R}" y="{y + 3:.1f}" font-size="8" fill="#9ca3af"'
            f' text-anchor="end" font-family="monospace">${p:,.0f}</text>'
        )
        # horizontal guide line
        price_lbls.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}"'
            f' stroke="#e5e7eb" stroke-width="0.5" stroke-dasharray="3,3"/>'
        )

    # ── Current price marker ─────────────────────────────────────────────────
    last_close  = rows[-1]["close"]
    lc_y        = py(last_close)
    lc_colour   = "#22c55e" if rows[-1]["close"] >= rows[-1]["open"] else "#ef4444"
    last_marker = (
        f'<line x1="{PAD_L}" y1="{lc_y:.1f}" x2="{W - 50}" y2="{lc_y:.1f}"'
        f' stroke="{lc_colour}" stroke-width="0.8" stroke-dasharray="4,2" stroke-opacity="0.7"/>'
        f'<rect x="{W-48}" y="{lc_y - 7:.1f}" width="42" height="13" rx="3"'
        f' fill="{lc_colour}" fill-opacity="0.15" stroke="{lc_colour}" stroke-width="0.5"/>'
        f'<text x="{W - 27}" y="{lc_y + 4:.1f}" font-size="8.5" fill="{lc_colour}"'
        f' font-weight="bold" text-anchor="middle" font-family="monospace">'
        f'${last_close:,.2f}</text>'
    )

    all_elems = (
        "".join(price_lbls) +
        "".join(vol_bars) +
        "".join(candles) +
        last_marker +
        "".join(date_lbls)
    )

    # Ticker label top-left
    tkr_lbl = (
        f'<text x="{PAD_L + 2}" y="12" font-size="10" fill="#374151"'
        f' font-weight="bold" font-family="monospace">{ticker} — 52w Daily</text>'
    ) if ticker else ""

    return (
        f'<div style="margin:10px 0 4px;background:#fafafa;border:1px solid #e5e7eb;'
        f'border-radius:8px;padding:8px 6px 4px;overflow:hidden">'
        f'<svg viewBox="0 0 {W} {H_TOTAL}" preserveAspectRatio="xMidYMid meet"'
        f' style="display:block;width:100%;height:auto">'
        f'{tkr_lbl}{all_elems}'
        f'</svg></div>'
    )


def _dcf_sensitivity_table(row: dict) -> str:
    """
    3×3 DCF sensitivity matrix: WACC (−2%, base, +2%) × Terminal Growth (−1%, base, +1%).
    Uses ratio scaling: IV_new = IV_base × (WACC_base − g_base) / (WACC_new − g_new)
    Returns empty string if DCF Avg or Price missing.
    """
    iv_base = _fv(row.get("DCF Avg", ""))
    price_v = _fv(row.get("Price", ""))
    if iv_base is None or price_v is None or iv_base <= 0 or price_v <= 0:
        return ""

    wacc_base = 0.10   # 10% — DCFParams default
    g_base    = 0.025  # 2.5% — DCFParams terminal_growth default
    spread_base = wacc_base - g_base  # must be > 0

    wacc_deltas = [-0.02, 0.0, +0.02]   # columns: Optimistic / Base / Pessimistic
    g_deltas    = [+0.01, 0.0, -0.01]   # rows: High growth / Base / Low growth

    wacc_labels = ["WACC 8%", "WACC 10%", "WACC 12%"]
    g_labels    = ["g = 3.5%", "g = 2.5%", "g = 1.5%"]

    # Count how many of the 9 scenarios still show a discount
    all_discount = True
    any_discount = False

    rows_html = ""
    for i, (gd, g_lbl) in enumerate(zip(g_deltas, g_labels)):
        cells = ""
        for j, (wd, w_lbl) in enumerate(zip(wacc_deltas, wacc_labels)):
            new_wacc = wacc_base + wd
            new_g    = g_base + gd
            spread_new = new_wacc - new_g
            if spread_new <= 0:
                iv_new = iv_base * 3.0   # degenerate case — very high value
            else:
                iv_new = iv_base * (spread_base / spread_new)

            mos_new = ((iv_new - price_v) / iv_new) * 100
            if mos_new < 0:
                all_discount = False
            else:
                any_discount = True

            # colour by MoS
            if mos_new >= 40:   cell_c = "#16a34a"; bg = "#f0fdf4"
            elif mos_new >= 20: cell_c = "#65a30d"; bg = "#f7fee7"
            elif mos_new >= 0:  cell_c = "#d97706"; bg = "#fffbeb"
            else:               cell_c = "#dc2626"; bg = "#fef2f2"

            is_base = (wd == 0.0 and gd == 0.0)
            border = "2px solid #3b82d4" if is_base else "1px solid #e5e7eb"
            cells += (
                f'<td style="padding:6px 8px;text-align:center;background:{bg};'
                f'border:{border};border-radius:4px;min-width:80px">'
                f'<div style="font-size:13px;font-weight:900;color:{cell_c}">'
                f'${iv_new:,.0f}</div>'
                f'<div style="font-size:10px;color:{cell_c};font-weight:700">'
                f'{"+" if mos_new >= 0 else ""}{mos_new:.0f}% MoS</div>'
                f'</td>'
            )
        rows_html += (
            f'<tr><td style="padding:6px 8px;font-size:10px;font-weight:700;'
            f'color:#57606a;white-space:nowrap">{g_lbl}</td>{cells}</tr>'
        )

    # Header row
    header_cells = "".join(
        f'<th style="padding:6px 8px;font-size:10px;font-weight:700;color:#fff;'
        f'background:#1f2328;text-align:center;min-width:80px">{lbl}</th>'
        for lbl in wacc_labels
    )
    header = (
        f'<tr><th style="padding:6px 8px;font-size:10px;color:#fff;background:#1f2328"></th>'
        f'{header_cells}</tr>'
    )

    if all_discount:
        badge = ('<span style="background:#dcfce7;color:#15803d;font-size:11px;'
                 'font-weight:700;padding:3px 10px;border-radius:20px;margin-left:10px">'
                 '✓ All 9 scenarios show discount</span>')
    elif any_discount:
        badge = ('<span style="background:#fffbeb;color:#92400e;font-size:11px;'
                 'font-weight:700;padding:3px 10px;border-radius:20px;margin-left:10px">'
                 '⚠ Discount in most scenarios</span>')
    else:
        badge = ('<span style="background:#fef2f2;color:#991b1b;font-size:11px;'
                 'font-weight:700;padding:3px 10px;border-radius:20px;margin-left:10px">'
                 '✗ Overvalued in all scenarios</span>')

    return f"""
    <div style="margin-bottom:14px">
      <div style="font-size:10px;font-weight:700;color:#8d96a0;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">
        DCF Sensitivity Analysis — Intrinsic Value &amp; Margin of Safety
        {badge}
        <span style="font-weight:400;color:#c0c4cb;margin-left:6px">
          (base case highlighted in blue border)</span>
      </div>
      <div style="overflow-x:auto">
        <table style="border-collapse:separate;border-spacing:4px;font-size:12px">
          <thead>{header}</thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>"""


def _score_cards(row: dict, overall_score: float | None = None) -> str:
    """4 metric cards: MoS, Piotroski, ROIC, ROE + composite/overall score badges."""
    def _norm(v, lo, hi):
        if v is None: return 0.0
        return min(max((v - lo) / (hi - lo), 0.0), 1.0)

    mos_v  = _fv(row.get("MoS%", ""))
    pio_v  = _fv(row.get("Piotroski", ""))
    roic_v = _fv(row.get("ROIC%", ""))
    roe_v  = _fv(row.get("ROE%", ""))
    comp_v = _fv(row.get("Score", ""))

    mos_n  = _norm(mos_v,  0, 60)
    pio_n  = _norm(pio_v,  0, 9)
    roic_n = _norm(roic_v, 0, 30)
    roe_n  = _norm(roe_v,  0, 25)

    def _bar(n, colour):
        w = round(n * 80, 1)
        return (
            f'<svg width="80" height="7" style="display:block;margin-top:4px">'
            f'<rect width="80" height="7" fill="#e5e7eb" rx="3"/>'
            f'<rect width="{w}" height="7" fill="{colour}" rx="3"/>'
            f'</svg>'
        )

    def _card(label, val_str, norm, colour, weight, contrib):
        return f"""<div style="flex:1;min-width:100px;background:#f9fafb;border:1px solid #e5e7eb;
                               border-radius:8px;padding:10px 12px;text-align:center">
          <div style="font-size:10px;color:#8d96a0;text-transform:uppercase;letter-spacing:.05em;
                      margin-bottom:4px">{label}</div>
          <div style="font-size:18px;font-weight:900;color:{colour};line-height:1">{val_str}</div>
          {_bar(norm, colour)}
          <div style="font-size:10px;color:#8d96a0;margin-top:4px">wt {weight} → <strong style="color:#374151">{contrib:.1f}pts</strong></div>
        </div>"""

    cards = (
        _card("Margin of Safety",
              f"{mos_v:.1f}%" if mos_v is not None else "—",
              mos_n, "#3b82d4", "40%", mos_n * 40) +
        _card("Piotroski F-Score",
              f"{pio_v:.0f}/9" if pio_v is not None else "—",
              pio_n, "#7c3aed", "25%", pio_n * 25) +
        _card("ROIC",
              f"{roic_v:.1f}%" if roic_v is not None else "—",
              roic_n, "#059669", "25%", roic_n * 25) +
        _card("ROE",
              f"{roe_v:.1f}%" if roe_v is not None else "—",
              roe_n, "#d97706", "10%", roe_n * 10)
    )

    # totals row
    totals = ""
    if comp_v is not None:
        cc = "#16a34a" if comp_v >= 70 else ("#eab308" if comp_v >= 45 else "#e11d48")
        totals += (
            f'<div style="display:inline-flex;align-items:center;gap:6px;padding:6px 14px;'
            f'background:#f0f2f5;border-radius:6px;margin-right:10px">'
            f'<span style="font-size:11px;color:#57606a">Composite Score</span>'
            f'<span style="font-size:20px;font-weight:900;color:{cc}">{comp_v:.0f}</span>'
            f'<span style="font-size:11px;color:#8d96a0">/100</span></div>'
        )
    if overall_score is not None:
        oc = "#16a34a" if overall_score >= 75 else ("#eab308" if overall_score >= 55 else "#e11d48")
        totals += (
            f'<div style="display:inline-flex;align-items:center;gap:6px;padding:6px 14px;'
            f'background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px">'
            f'<span style="font-size:11px;color:#3b82d4;font-weight:700">Overall Cross-Profile Score</span>'
            f'<span style="font-size:20px;font-weight:900;color:{oc}">{overall_score:.0f}</span>'
            f'<span style="font-size:11px;color:#8d96a0">/100</span></div>'
        )

    return f"""
    <div style="margin-bottom:12px">
      <div style="font-size:10px;font-weight:700;color:#8d96a0;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">Score Breakdown
        <span style="font-weight:400;color:#c0c4cb"> — MoS×40% + Piotroski×25% + ROIC×25% + ROE (informational)</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">{cards}</div>
      <div style="margin-top:4px">{totals}</div>
    </div>"""


def _why_buy(row: dict, profile_key: str | None = None,
             profiles: list[str] | None = None,
             overall_score: float | None = None,
             ohlc: list[dict] | None = None,
             score_history: list[float] | None = None) -> str:
    """
    Generate a 'Why buy X?' expandable panel.
    Layout (top to bottom, no scroll):
      1. Score breakdown cards + 52w chart (side by side)
      2. Plain-English analysis text
    """
    ticker  = row.get("Ticker", "").strip()
    company = row.get("Company", "").strip() or ticker
    sector  = row.get("Sector", "").strip()
    sector_plain = _SECTOR_CONTEXT.get(sector, sector.lower() if sector else "its sector")

    mos_v   = _fv(row.get("MoS%", ""))
    price_v = _fv(row.get("Price", ""))
    dcf_v   = _fv(row.get("DCF Avg", ""))
    pe_v    = _fv(row.get("P/E", ""))
    pb_v    = _fv(row.get("P/B", ""))
    pfcf_v  = _fv(row.get("P/FCF", ""))
    ev_v    = _fv(row.get("EV/EBITDA", ""))
    nd_v    = _fv(row.get("NetDebt/EBITDA", ""))
    pio_v   = _fv(row.get("Piotroski", ""))
    roic_v  = _fv(row.get("ROIC%", ""))
    pos_v   = _fv(row.get("52w Position%", ""))
    low_v   = _fv(row.get("52w Low", ""))
    high_v  = _fv(row.get("52w High", ""))
    beneish_v  = _fv(row.get("Beneish M", ""))
    manip_risk = str(row.get("Manip.Risk", "")).strip().upper() == "YES"

    sentences: list[str] = []

    # ── Beneish M-Score warning (prepended as sentence 0) ────────────────────
    if manip_risk and beneish_v is not None:
        sentences.insert(0,
            f'<span style="background:#fef2f2;border:1px solid #fca5a5;border-radius:4px;'
            f'padding:2px 8px;color:#dc2626;font-weight:700">&#9888; MANIPULATION RISK</span> '
            f'The Beneish M-Score of <strong>{beneish_v:.2f}</strong> (threshold: &minus;1.78) '
            f'suggests elevated probability of earnings manipulation. '
            f'Treat reported financials with additional skepticism and verify independently.'
        )
    elif beneish_v is not None and beneish_v < -2.5:
        sentences.append(
            f'The Beneish M-Score of <strong>{beneish_v:.2f}</strong> is well below the &minus;1.78 '
            f'manipulation threshold, suggesting the reported financials appear credible.'
        )

    # ── Sentence 1: Core MoS + intrinsic value ───────────────────────────────
    if mos_v is not None and mos_v > 0:
        iv_part = ""
        if dcf_v is not None:
            iv_part = f" of <strong>${dcf_v:,.2f} per share</strong>"
        sentences.append(
            f"Our quantitative model estimates that <strong>{company} ({ticker})</strong> "
            f"is currently trading at a <strong>{mos_v:.0f}% discount</strong> to its "
            f"calculated intrinsic value{iv_part}. "
            f"In plain terms: for every $1 of estimated value, the market is charging "
            f"only <strong>${1 - mos_v/100:.2f}</strong> — a rare margin of safety."
        )

    # ── Sentence 2: P/E context ───────────────────────────────────────────────
    if pe_v is not None and pe_v > 0:
        if pe_v < 10:
            pe_comment = (
                f"At a P/E of <strong>{pe_v:.1f}×</strong> — versus the S&amp;P 500 average of ~22× — "
                f"the market is pricing {ticker} as if earnings will decline sharply. "
                f"Our cash-flow analysis does not support that pessimism."
            )
        elif pe_v < 15:
            pe_comment = (
                f"A P/E of <strong>{pe_v:.1f}×</strong> is well below the S&amp;P 500 average of ~22×, "
                f"meaning the market is demanding very little premium for {ticker}'s earnings power."
            )
        else:
            pe_comment = (
                f"The P/E ratio of <strong>{pe_v:.1f}×</strong> is moderate; "
                f"the investment case here rests primarily on free cash flow and asset value rather than earnings cheapness alone."
            )
        sentences.append(pe_comment)

    # ── Sentence 3: P/B context ───────────────────────────────────────────────
    if pb_v is not None and 0 < pb_v < 1.5:
        sentences.append(
            f"A Price-to-Book of <strong>{pb_v:.2f}×</strong> means you are acquiring "
            f"{ticker}'s net assets — factories, intellectual property, cash — "
            f"for <strong>less than their stated accounting value</strong>. "
            f"This is unusual for an S&amp;P 500 company and historically associated with above-average future returns."
        )
    elif pb_v is not None and pb_v >= 1.5:
        sentences.append(
            f"The P/B of <strong>{pb_v:.2f}×</strong> reflects the market's recognition of "
            f"{ticker}'s business quality; the valuation opportunity here comes from "
            f"free cash flow generation rather than asset cheapness."
        )

    # ── Sentence 4: P/FCF — the most important metric ────────────────────────
    if pfcf_v is not None and pfcf_v > 0:
        if pfcf_v < 10:
            fcf_comment = (
                f"Most importantly, the P/FCF ratio of <strong>{pfcf_v:.1f}×</strong> is exceptionally low: "
                f"the business is generating so much real cash — money that actually hits the bank account, "
                f"not just accounting profits — that at the current price you are paying only "
                f"<strong>{pfcf_v:.1f} years of free cash flow</strong> for the entire company."
            )
        elif pfcf_v < 15:
            fcf_comment = (
                f"The P/FCF of <strong>{pfcf_v:.1f}×</strong> confirms that {ticker} converts revenue "
                f"into real cash at a healthy rate — free cash flow is the most reliable indicator of "
                f"a company's true earning power because it is far harder to manipulate than net income."
            )
        else:
            fcf_comment = (
                f"The P/FCF ratio of <strong>{pfcf_v:.1f}×</strong> is within a reasonable range; "
                f"the valuation discount here is driven primarily by the DCF model rather than current-year cash yield."
            )
        sentences.append(fcf_comment)

    # ── Sentence 5: EV/EBITDA ────────────────────────────────────────────────
    if ev_v is not None and ev_v > 0 and ev_v < 8:
        sentences.append(
            f"An EV/EBITDA of <strong>{ev_v:.1f}×</strong> means a hypothetical acquirer "
            f"would pay back the full purchase price from operating profit alone in under 8 years — "
            f"a threshold historically associated with cheap acquisition targets."
        )

    # ── Sentence 6: Debt safety ───────────────────────────────────────────────
    if nd_v is not None:
        if nd_v < 0:
            sentences.append(
                f"{ticker} is in a <strong>net cash position</strong> — it holds more cash than debt, "
                f"which provides a substantial financial cushion and eliminates near-term refinancing risk."
            )
        elif nd_v < 1.5:
            sentences.append(
                f"With Net Debt/EBITDA of <strong>{nd_v:.2f}×</strong>, {ticker} carries a conservative "
                f"debt load — it could theoretically pay off all net debt in under 2 years from operating profit alone."
            )
        elif nd_v > 3.0:
            sentences.append(
                f"Note: the leverage ratio of <strong>{nd_v:.2f}×</strong> is elevated; "
                f"while still within our filter threshold, monitor this metric in a rising interest-rate environment."
            )

    # ── Sentence 7: Piotroski quality signal ─────────────────────────────────
    if pio_v is not None:
        if pio_v >= 7:
            sentences.append(
                f"The <strong>Piotroski F-Score of {pio_v:.0f}/9</strong> is in the strong zone: "
                f"this 9-point accounting quality checklist covers profitability trends, balance sheet "
                f"improvement and operating efficiency. A score of {pio_v:.0f} means the fundamentals "
                f"are improving across almost every dimension simultaneously — exactly what you want to see "
                f"in a value stock before it re-rates."
            )
        elif pio_v >= 4:
            sentences.append(
                f"The Piotroski F-Score of <strong>{pio_v:.0f}/9</strong> indicates a fundamentally "
                f"stable business — not deteriorating — which reduces the risk that this discount "
                f"is a 'value trap' masking real fundamental problems."
            )

    # ── Sentence 8: ROIC moat signal ─────────────────────────────────────────
    if roic_v is not None and roic_v > 0:
        if roic_v >= 15:
            sentences.append(
                f"A <strong>ROIC of {roic_v:.1f}%</strong> — well above the typical 10% cost of capital — "
                f"signals a genuine competitive advantage: this company earns significantly more on each dollar "
                f"invested back into the business than most of its S&amp;P 500 peers."
            )
        elif roic_v >= 10:
            sentences.append(
                f"With a ROIC of <strong>{roic_v:.1f}%</strong>, {ticker} clears the 10% cost-of-capital "
                f"hurdle — meaning every dollar reinvested creates shareholder value rather than destroying it."
            )
        elif roic_v >= 5:
            sentences.append(
                f"ROIC of <strong>{roic_v:.1f}%</strong> is modest; the investment case rests on the "
                f"price discount rather than capital efficiency."
            )

    # ── Sentence 8b: ROE quality signal ──────────────────────────────────────
    roe_v2 = _fv(row.get("ROE%", ""))
    if roe_v2 is not None and roe_v2 >= 15:
        sentences.append(
            f"A <strong>Return on Equity of {roe_v2:.1f}%</strong> — above the 15% threshold Buffett uses "
            f"as a signal of durable competitive advantage — shows that {ticker} generates strong profits "
            f"relative to shareholders' book value, a hallmark of high-quality franchises."
        )

    # ── Sentence 8c: Gross margin signal ─────────────────────────────────────
    gm_v = _fv(row.get("Gross Margin%", ""))
    if gm_v is not None and gm_v >= 40:
        sentences.append(
            f"A gross margin of <strong>{gm_v:.1f}%</strong> is exceptional — companies that retain "
            f"40%+ of each revenue dollar after direct costs typically possess strong pricing power "
            f"and structural cost advantages that are difficult for competitors to replicate."
        )

    # ── Sentence 8d: Graham Number ────────────────────────────────────────────
    graham_v = _fv(row.get("Graham", ""))
    if graham_v is not None:
        if price_v is not None and price_v < graham_v:
            sentences.append(
                f"Even the conservative <strong>Graham Number of ${graham_v:,.2f}</strong> — which uses "
                f"only earnings and book value, no growth assumptions — sits above the current price, "
                f"providing a second independent confirmation of undervaluation."
            )
        elif price_v is not None and price_v > graham_v * 1.2:
            sentences.append(
                f"Note: the Graham Number of <strong>${graham_v:,.2f}</strong> (based purely on book value "
                f"and earnings) is below the current price, so the valuation case here rests primarily "
                f"on the DCF model and future cash flow growth."
            )

    # ── Sentence 8e: Dividend yield + FCF payout ─────────────────────────────
    div_y   = _fv(row.get("Dividend Yield%", ""))
    payout_v = _fv(row.get("Payout (FCF)%", ""))
    sbc_pct = _fv(row.get("SBC/FCF%", ""))
    if div_y is not None and div_y > 0:
        if payout_v is not None and payout_v < 70:
            sentences.append(
                f"{ticker} pays a <strong>{div_y:.1f}% dividend yield</strong>, "
                f"covered by only <strong>{payout_v:.0f}% of free cash flow</strong> — "
                f"leaving ample room for dividend growth without straining the balance sheet."
            )
        else:
            sentences.append(
                f"{ticker} offers a <strong>{div_y:.1f}% dividend yield</strong>."
            )

    if sbc_pct is not None and sbc_pct > 25:
        sentences.append(
            f'<span style="color:#d97706;font-weight:700">⚠ SBC Note:</span> '
            f"Stock-based compensation equals <strong>{sbc_pct:.0f}% of free cash flow</strong>. "
            f"Reported FCF includes this non-cash add-back — true shareholder cash generation "
            f"is proportionally lower. This is common in tech/growth companies but should be "
            f"considered when assessing FCF yield."
        )
    elif sbc_pct is not None and sbc_pct <= 10:
        sentences.append(
            f"Stock-based compensation is minimal at <strong>{sbc_pct:.0f}% of FCF</strong>, "
            f"meaning reported free cash flow closely reflects true shareholder returns."
        )

    # ── Sector percentile annotation ─────────────────────────────────────────
    sec_pe_pct = _fv(row.get("Sector P/E %ile", ""))
    if sec_pe_pct is not None and sec_pe_pct <= 20:
        sector_name = row.get("Sector", "its sector") or "its sector"
        sentences.append(
            f"Within <strong>{sector_name}</strong>, {ticker} ranks in the "
            f"<strong>{sec_pe_pct:.0f}th percentile for P/E</strong> — "
            f"meaning ~{100 - sec_pe_pct:.0f}% of sector peers are more expensive on an earnings basis."
        )

    # ── Sentence 9: 52w position ─────────────────────────────────────────────
    if pos_v is not None and low_v is not None and high_v is not None:
        if pos_v < 20:
            sentences.append(
                f"Technically, the stock is at <strong>{pos_v:.0f}% of its 52-week range</strong> "
                f"(annual low ${low_v:,.2f} / high ${high_v:,.2f}) — essentially at its annual floor. "
                f"This provides an additional layer of near-term downside protection independent of the DCF model."
            )
        elif pos_v < 40:
            sentences.append(
                f"At <strong>{pos_v:.0f}% of its 52-week range</strong> "
                f"(low ${low_v:,.2f} / high ${high_v:,.2f}), the stock is trading in the lower portion "
                f"of its annual band, offering a favourable technical entry point alongside the valuation discount."
            )
        elif pos_v > 75:
            sentences.append(
                f"Note: at <strong>{pos_v:.0f}% of its 52-week range</strong>, the stock is trading "
                f"closer to its annual high (${high_v:,.2f}). "
                f"The DCF discount is real, but consider a staged entry or waiting for a pullback."
            )

    # ── Sentence 10: Profile context ─────────────────────────────────────────
    if profiles and len(profiles) > 1:
        profile_names = " and ".join(
            f"<strong>{_PROFILE_META.get(p, {}).get('label', p)}</strong>"
            for p in profiles
        )
        sentences.append(
            f"{ticker} operates in the <strong>{sector_plain}</strong> sector and is one of only a handful of "
            f"S&amp;P 500 companies to simultaneously pass {profile_names} — "
            f"{len(profiles)} independent investment philosophies reaching the same conclusion."
        )
    elif profile_key:
        profile_plain = _PROFILE_PLAIN.get(profile_key, "the selected screen")
        sentences.append(
            f"The company operates in the <strong>{sector_plain}</strong> sector "
            f"and passed {profile_plain}."
        )

    if not sentences and overall_score is None and low_v is None:
        return ticker, "", None, None

    body       = '  '.join(sentences)
    cards_html = _score_cards(row, overall_score)
    bar_html   = _52w_bar(price_v, low_v, high_v)
    chart_html = _price_chart_svg(ohlc or [], ticker) if ohlc else ""
    sens_html  = _dcf_sensitivity_table(row)

    fit_v = _fv(row.get("ProfileFit", ""))
    sc_v  = overall_score if overall_score is not None else fit_v
    sc_c  = ("#16a34a" if (sc_v or 0) >= 75 else ("#eab308" if (sc_v or 0) >= 55 else "#9ca3af")) if sc_v else "#9ca3af"

    bar_section = f"""
        <div style="margin-bottom:10px">
          <div style="font-size:10px;font-weight:700;color:#8d96a0;text-transform:uppercase;
                      letter-spacing:.06em;margin-bottom:4px">52-Week Range</div>
          {bar_html}
        </div>""" if bar_html else ""

    chart_section = f"""
        <div style="margin-bottom:14px">
          {chart_html}
        </div>""" if chart_html else ""

    # Resolve score_history: prefer explicit parameter, fall back to module-level cache
    _sh = score_history if score_history is not None else _SCORE_HISTORY_DATA.get(ticker)
    sparkline_html = _sparkline_svg(_sh) if _sh and len(_sh) >= 2 else ""
    spark_section  = (
        f'<div style="margin-bottom:10px;color:#57606a;font-size:12px">Score history: {sparkline_html}</div>'
        if sparkline_html else ""
    )

    panel_html = f"""
        <!-- Score cards -->
        <div style="margin-bottom:14px">{cards_html}</div>
        <!-- DCF Sensitivity -->
        {f'<div style="margin-bottom:14px">{sens_html}</div>' if sens_html else ""}
        {spark_section}
        {bar_section}
        {chart_section}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 12px">
        <!-- Plain-English analysis -->
        <div style="font-size:13px;line-height:1.7;color:#374151">{body}</div>"""

    return ticker, panel_html, sc_v, sc_c


def _why_btn(ticker: str, sc_v: float | None, sc_c: str) -> str:
    """Toggle button rendered inside the data row <td>. Calls toggleWhy(id)."""
    why_id = f"why-{ticker.replace('.', '-')}"
    sc_badge = (
        f'<span style="font-size:10px;font-weight:900;color:{sc_c};'
        f'background:{sc_c}18;border-radius:3px;padding:1px 5px;margin-left:4px">'
        f'{sc_v:.0f}</span>'
    ) if sc_v is not None else ""
    return (
        f'<button class="why-btn" onclick="toggleWhy(\'{why_id}\')" '
        f'id="btn-{why_id}">'
        f'<span class="why-arrow" id="arr-{why_id}">&#9654;</span>'
        f'&nbsp;Why buy?{sc_badge}'
        f'</button>'
    )


def _why_tr(panel_html: str, col_count: int, ticker: str) -> str:
    """A hidden <tr> that expands inline below the data row — full table width."""
    why_id = f"why-{ticker.replace('.', '-')}"
    return (
        f'<tr class="why-row" id="{why_id}" style="display:none">'
        f'<td colspan="{col_count}" style="padding:0;border-bottom:2px solid #bfdbfe">'
        f'<div class="why-body-inline">{panel_html}</div>'
        f'</td></tr>'
    )


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
       font-size: 13px; line-height: 1.6; background: #f0f2f5; color: #1f2328; }
.page { max-width: 1430px; margin: 0 auto; padding: 32px 24px 60px; }

/* nav */
.toc { background:#fff; border:1px solid #e5e7eb; border-radius:10px;
       padding:18px 24px; margin-bottom:28px; }
.toc-title { font-size:12px; font-weight:700; color:#57606a; text-transform:uppercase;
             letter-spacing:.07em; margin-bottom:10px; }
.toc a { display:inline-block; margin:3px 6px 3px 0; padding:4px 12px;
         background:#f7f8fa; border:1px solid #e5e7eb; border-radius:20px;
         font-size:12px; color:#3b82d4; text-decoration:none; font-weight:600; }
.toc a:hover { background:#eff6ff; }

/* header */
.report-header { background:linear-gradient(135deg,#1f2328 0%,#2d3748 100%);
                 color:#fff; border-radius:12px; padding:36px 40px;
                 margin-bottom:28px; }
.report-header h1 { font-size:28px; font-weight:800; margin-bottom:6px; }
.report-header .subtitle { color:#9ca3af; font-size:14px; }
.header-meta { display:flex; gap:28px; margin-top:22px; flex-wrap:wrap; }
.hm-item .hm-label { font-size:11px; color:#9ca3af; text-transform:uppercase;
                     letter-spacing:.07em; }
.hm-item .hm-value { font-size:16px; font-weight:700; color:#fff; margin-top:1px; }

/* section */
.section { background:#fff; border-radius:12px; padding:28px 32px;
           margin-bottom:28px; border:1px solid #e5e7eb; }
.section-anchor { display:block; height:1px; margin-top:-80px; padding-top:80px; }
.section-title { font-size:18px; font-weight:800; margin-bottom:4px; color:#1f2328; }
.section-sub { font-size:13px; color:#57606a; margin-bottom:20px; }
.divider { border:none; border-top:1px solid #e5e7eb; margin:20px 0; }

/* pills */
.stats-bar { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }
.stat-pill { flex:1; min-width:110px; background:#f7f8fa; border-radius:10px;
             padding:14px 18px; border:1px solid #e5e7eb; text-align:center; }
.sp-value { font-size:24px; font-weight:800; }
.sp-label { font-size:11px; color:#57606a; text-transform:uppercase;
            letter-spacing:.06em; margin-top:2px; }

/* profile badge */
.profile-badge { display:inline-flex; align-items:center; gap:6px;
                 background:#eff6ff; border:1px solid #bfdbfe; border-radius:6px;
                 padding:4px 10px; font-size:12px; font-weight:700; color:#1d4ed8;
                 margin-bottom:16px; }

/* screener table — full width, no scrollbar, PDF-safe */
.stbl { width:100%; border-collapse:collapse; font-size:11.5px;
        table-layout:fixed; }
.stbl thead th { background:#1f2328; color:#fff; padding:8px 8px;
                 font-size:10px; text-transform:uppercase; letter-spacing:.04em;
                 text-align:left; white-space:normal; overflow-wrap:break-word; }
.stbl thead th.r { text-align:right; }
.stbl tbody td { padding:8px 8px; border-bottom:1px solid #f0f2f5;
                 vertical-align:middle; word-break:break-word; overflow-wrap:break-word; }
.stbl tbody tr:last-child td { border-bottom:none; }
.stbl tbody tr:hover td { background:#f7f8fa; }
.stbl td.r { text-align:right; }
.ticker-lbl { font-weight:800; font-size:13px; }
.company-lbl { font-size:11px; color:#57606a; }

/* gauge bar */
.gauge-wrap { display:flex; align-items:center; gap:4px; }
.gauge-track { flex:1; min-width:30px; height:6px; background:#e5e7eb;
               border-radius:4px; overflow:hidden; }
.gauge-fill  { height:100%; border-radius:4px; }
.gauge-pct   { font-size:11px; font-weight:700; width:34px; text-align:right; flex-shrink:0; }

/* quality badges */
.qbadge { display:inline-block; padding:2px 7px; border-radius:4px;
          font-size:11px; font-weight:700; }

/* ── BACKTEST ── */
.bt-header { display:flex; gap:24px; flex-wrap:wrap; margin-bottom:24px; }
.bt-kpi { flex:1; min-width:130px; background:#f7f8fa; border-radius:10px;
          padding:16px 18px; border:1px solid #e5e7eb; text-align:center; }
.bt-kpi .kv { font-size:26px; font-weight:800; }
.bt-kpi .kl { font-size:11px; color:#57606a; text-transform:uppercase;
              letter-spacing:.06em; margin-top:2px; }

/* Dual bar chart */
.chart-wrap { margin:20px 0; }
.chart-year-row { display:flex; align-items:center; gap:10px;
                  margin-bottom:10px; }
.chart-year-lbl { width:36px; font-size:13px; font-weight:700; color:#57606a;
                  flex-shrink:0; text-align:right; }
.chart-bars { flex:1; }
.chart-bar-row { display:flex; align-items:center; gap:6px; margin-bottom:4px; }
.chart-bar-lbl { width:72px; font-size:11px; color:#57606a; flex-shrink:0; text-align:right; }
.chart-bar-track { flex:1; height:20px; background:#f0f2f5; border-radius:4px;
                   overflow:visible; position:relative; }
.chart-bar-fill { height:100%; border-radius:4px; display:flex;
                  align-items:center; justify-content:flex-end;
                  padding-right:6px; font-size:11px; font-weight:700;
                  color:#fff; white-space:nowrap; min-width:4px; }
.chart-bar-neg { position:absolute; right:50%; height:100%; border-radius:4px 0 0 4px; }
.chart-bar-pos { position:absolute; left:50%; height:100%; border-radius:0 4px 4px 0; }
.chart-zero-line { position:absolute; left:50%; top:0; height:100%;
                   border-left:2px solid #9ca3af; }

/* bt table */
.bt-tbl { width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }
.bt-tbl th { background:#1f2328; color:#fff; padding:10px 14px;
             font-size:11px; text-transform:uppercase; letter-spacing:.05em;
             text-align:left; white-space:nowrap; }
.bt-tbl th.r { text-align:right; }
.bt-tbl td { padding:10px 14px; border-bottom:1px solid #f0f2f5; vertical-align:middle; }
.bt-tbl tr:last-child td { border-bottom:none; }
.bt-tbl tr:hover td { background:#f7f8fa; }
.bt-tbl td.r { text-align:right; }
.excess-pos { color:#16a34a; font-weight:700; }
.excess-neg { color:#e11d48; font-weight:700; }

/* limitation box */
.limit-box { background:#fffbeb; border:1px solid #fde68a; border-radius:8px;
             padding:16px 20px; margin-top:20px; font-size:12px;
             color:#92400e; line-height:1.7; }
.limit-box strong { color:#78350f; }

/* disclaimer */
.disclaimer { background:#fffbeb; border:1px solid #fde68a; border-radius:8px;
              padding:14px 18px; margin-bottom:24px; font-size:12px;
              color:#92400e; }

/* info boxes */
.ib { border-radius:8px; padding:14px 18px; margin:14px 0;
      font-size:13px; line-height:1.6; }
.ib.blue  { background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af; }
.ib.green { background:#f0fdf4; border:1px solid #bbf7d0; color:#14532d; }

/* ── Why-buy inline expansion ───────────────────────────────────────────────── */

/* Toggle button (lives in the data row td) */
.why-btn {
  display:inline-flex; align-items:center; gap:5px;
  font-size:11px; font-weight:700; color:#3b82d4; text-transform:uppercase;
  letter-spacing:.06em; cursor:pointer; user-select:none;
  padding:4px 10px; background:#eff6ff; border:1px solid #bfdbfe;
  border-radius:20px; transition:background .15s;
  white-space:nowrap; margin-top:4px;
}
.why-btn:hover { background:#dbeafe; }
.why-btn.open  { background:#dbeafe; border-color:#93c5fd; }
.why-arrow { display:inline-block; transition:transform .2s ease; flex-shrink:0; }
.why-arrow.open { transform:rotate(90deg); }

/* The hidden expansion row */
.why-row td { padding:0 !important; }
.why-body-inline {
  padding:20px 28px;
  background:#f8faff;
  border-left:4px solid #3b82d4;
  animation:why-slide .18s ease;
}
@keyframes why-slide {
  from { opacity:0; transform:translateY(-6px); }
  to   { opacity:1; transform:translateY(0);    }
}

/* ── Collapsible section wrappers ──────────────────────────────────────────── */
details.sec-wrap { margin-bottom:20px; }
details.sec-wrap > summary {
  display:flex; align-items:center; gap:12px;
  padding:0; cursor:pointer; list-style:none; user-select:none;
}
details.sec-wrap > summary::-webkit-details-marker { display:none; }
/* The visible header bar */
.sec-hdr {
  flex:1; display:flex; align-items:center; gap:12px;
  background:#fff; border:1px solid #e5e7eb; border-radius:12px;
  padding:14px 22px; transition:background .15s, box-shadow .15s;
  box-shadow:0 1px 3px rgba(0,0,0,.06);
}
details.sec-wrap > summary:hover .sec-hdr { background:#f0f7ff; box-shadow:0 2px 8px rgba(59,130,212,.12); }
details.sec-wrap[open] > summary .sec-hdr { background:#eff6ff; border-color:#bfdbfe; border-bottom-left-radius:0; border-bottom-right-radius:0; }
.sec-arrow { display:inline-block; transition:transform .2s ease; color:#3b82d4; font-size:15px; flex-shrink:0; }
details.sec-wrap[open] > summary .sec-arrow { transform:rotate(90deg); }
.sec-badge { display:inline-flex; align-items:center; justify-content:center;
             width:32px; height:32px; border-radius:8px;
             font-size:11px; font-weight:800; flex-shrink:0; }
.sec-title { font-size:15px; font-weight:800; color:#1f2328; }
.sec-meta  { font-size:12px; color:#8d96a0; margin-left:auto; white-space:nowrap; }
/* The content panel — flush with header */
details.sec-wrap > .sec-body {
  background:#fff; border:1px solid #bfdbfe; border-top:none;
  border-radius:0 0 12px 12px; padding:28px 32px;
}
details.sec-wrap:not([open]) > .sec-body { display:none; }

/* footer */
.footer { text-align:center; font-size:11px; color:#8d96a0;
          border-top:1px solid #e5e7eb; padding-top:20px; margin-top:40px; }

/* ── Live Filter Bar (ST12) ─────────────────────────────────────────────────── */
.filter-bar {
  display:flex; flex-wrap:wrap; gap:8px; align-items:center;
  padding:12px 16px; background:#f7f8fa; border:1px solid #e5e7eb;
  border-radius:8px; margin-bottom:14px;
}
.filter-bar label { font-size:11px; font-weight:700; color:#57606a;
                    text-transform:uppercase; letter-spacing:.05em; white-space:nowrap; }
.filter-bar select, .filter-bar input[type=number] {
  padding:4px 8px; border:1px solid #d1d5db; border-radius:6px;
  font-size:12px; background:#fff; color:#1f2328; height:28px;
  min-width:80px; max-width:110px;
}
.filter-bar select { min-width:120px; }
.filter-bar .filter-reset {
  padding:4px 12px; background:#e5e7eb; border:1px solid #d1d5db;
  border-radius:6px; font-size:12px; font-weight:700; color:#374151;
  cursor:pointer; height:28px; white-space:nowrap;
}
.filter-bar .filter-reset:hover { background:#d1d5db; }
.filter-count { font-size:11px; color:#6b7280; margin-left:auto; white-space:nowrap; }

/* ── Watchlist (ST13) ────────────────────────────────────────────────────────── */
.watchlist-section {
  background:#fff; border:1px solid #e5e7eb; border-radius:12px;
  padding:20px 28px; margin-bottom:24px;
}
.watchlist-header {
  display:flex; align-items:center; gap:12px; margin-bottom:14px;
}
.watchlist-title { font-size:16px; font-weight:800; color:#1f2328; }
.watchlist-subtitle { font-size:12px; color:#8d96a0; margin-left:auto; }
.watchlist-empty { font-size:13px; color:#9ca3af; padding:12px 0; }
.watchlist-tbl { width:100%; border-collapse:collapse; font-size:12px; }
.watchlist-tbl th { background:#f7f8fa; color:#57606a; padding:7px 10px;
                    font-size:10px; text-transform:uppercase; letter-spacing:.04em;
                    text-align:left; border-bottom:1px solid #e5e7eb; }
.watchlist-tbl th.r { text-align:right; }
.watchlist-tbl td { padding:7px 10px; border-bottom:1px solid #f0f2f5; vertical-align:middle; }
.watchlist-tbl tr:last-child td { border-bottom:none; }
.watchlist-tbl td.r { text-align:right; }
.wl-export-btn {
  padding:5px 14px; background:#1f2328; color:#fff; border:none;
  border-radius:6px; font-size:12px; font-weight:700; cursor:pointer;
}
.wl-export-btn:hover { background:#374151; }
.star-btn {
  background:none; border:none; cursor:pointer; font-size:15px;
  padding:2px 4px; border-radius:4px; transition:opacity .15s;
  line-height:1; vertical-align:middle;
}
.star-btn:hover { opacity:.7; }
.star-btn.starred { filter: drop-shadow(0 0 3px #fbbf24); }

@media(max-width:640px){
  .header-meta, .bt-header, .stats-bar { flex-direction:column; }
  .stbl { font-size:11px; }
  .stbl th, .stbl td { padding:5px 5px; }
}

@media print {
  body { background:#fff !important; font-size:11px; }
  .page { max-width:100% !important; padding:12px 10px 30px !important; }
  .section { border-radius:6px !important; padding:16px 18px !important;
             page-break-inside:avoid; }
  .toc, .section-anchor { display:none !important; }
  .report-header { background:#1f2328 !important; -webkit-print-color-adjust:exact;
                   print-color-adjust:exact; padding:20px 24px !important; }
  .stbl thead th { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  .bt-header, .stats-bar { gap:8px !important; }
  .stat-pill, .bt-kpi { padding:10px 12px !important; }
  .sp-value, .bt-kpi .kv { font-size:18px !important; }
  .chart-wrap { page-break-inside:avoid; }
  a { color:inherit !important; text-decoration:none !important; }
}
"""

# ── Screener section builder ──────────────────────────────────────────────────

_PROFILE_META = {
    "deep_value": {
        "label": "Deep Value",
        "icon": "DV",
        "desc": "All 6 valuation filters pass simultaneously — the tightest screen. P/E ≤ 15, P/B ≤ 1.5, EV/EBITDA ≤ 8, P/FCF ≤ 15, Net Debt/EBITDA ≤ 2.5, MoS ≥ 20%. Piotroski ≥ 4.",
        "colour": "#3b82d4",
    },
    "buffett_quality": {
        "label": "Buffett Quality",
        "icon": "BQ",
        "desc": "Quality at a reasonable price. ROIC ≥ 10%, Piotroski ≥ 6, P/FCF ≤ 20, EV/EBITDA ≤ 12, MoS ≥ 15%. Focuses on wide-moat businesses with strong returns on capital.",
        "colour": "#7c3aed",
    },
    "high_fcf_yield": {
        "label": "High FCF Yield",
        "icon": "FCF",
        "desc": "Maximum free-cash-flow generation. P/FCF ≤ 12, MoS ≥ 30%. Companies that convert revenue into real cash at an exceptional rate — the most direct measure of earning power.",
        "colour": "#059669",
    },
    "quality_value": {
        "label": "Quality Value",
        "icon": "QV",
        "desc": "Balanced blend: EV/EBITDA ≤ 10, P/E ≤ 18, ROIC ≥ 8%, Piotroski ≥ 6, Altman Z ≥ 1.0, MoS ≥ 20%. Avoids distressed companies while still requiring a clear discount.",
        "colour": "#d97706",
    },
    "dividend_growth": {
        "label": "Dividend Growth",
        "icon": "DIV",
        "desc": "Income-oriented screen. Min dividend yield 2.5%, FCF payout ≤ 70%, Piotroski ≥ 5, Net Debt/EBITDA ≤ 2.0. Targets financially healthy companies that return capital to shareholders sustainably.",
        "colour": "#0891b2",
    },
    "magic_formula": {
        "label": "Magic Formula",
        "icon": "MF",
        "desc": "Greenblatt Magic Formula: ranks all S&P 500 companies by Earnings Yield (E/P) + ROIC simultaneously. Lower combined rank = stronger signal. Excludes financials and utilities. Top 30 shown.",
        "colour": "#be185d",
    },
}


def _quality_badge(val: str, kind: str) -> str:
    """Render a colour-coded badge for Piotroski / Altman / ROIC."""
    f = _fv(val)
    if f is None:
        return '<span class="qbadge" style="background:#f0f2f5;color:#8d96a0">—</span>'
    if kind == "piotroski":
        bg = _piotroski_colour(f)
        return f'<span class="qbadge" style="background:{bg}22;color:{bg};border:1px solid {bg}55">{f:.0f}/9</span>'
    if kind == "altman":
        bg = _altman_colour(f)
        return f'<span class="qbadge" style="background:{bg}22;color:{bg};border:1px solid {bg}55">{f:.2f}</span>'
    if kind == "roic":
        bg = "#16a34a" if f >= 10 else ("#eab308" if f >= 5 else "#e11d48")
        sign = "+" if f >= 0 else ""
        return f'<span class="qbadge" style="background:{bg}22;color:{bg};border:1px solid {bg}55">{sign}{f:.1f}%</span>'
    return f'<span class="qbadge" style="background:#f0f2f5;color:#8d96a0">{val}</span>'


def _row_to_table_tr(row: dict, rank: int, colour: str, show_fit: bool = False,
                     passes: bool | None = None) -> str:
    """Render a data row + inline why-buy expansion row for screener tables.
    Returns two <tr> elements concatenated as a single string."""
    mos_v = _fv(row.get("MoS%", "")) or 0.0
    mc    = _mos_colour(mos_v)
    grade, glabel = _mos_grade(mos_v)
    pos_v = _fv(row.get("52w Position%", ""))
    pc    = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"
    pos_bar = (
        f'<div class="gauge-wrap"><div class="gauge-track">'
        f'<div class="gauge-fill" style="width:{min(pos_v,100):.1f}%;background:{pc}"></div>'
        f'</div><div class="gauge-pct" style="color:{pc}">{pos_v:.0f}%</div></div>'
        if pos_v is not None else "—"
    )
    mos_bar = (
        f'<div class="gauge-wrap"><div class="gauge-track">'
        f'<div class="gauge-fill" style="width:{min(mos_v,100):.1f}%;background:{mc}"></div>'
        f'</div><div class="gauge-pct" style="color:{mc}">{mos_v:.0f}%</div></div>'
    )
    dcf_model = row.get("DCF Model", "").strip() or "—"
    fit_v = _fv(row.get("ProfileFit", ""))
    fit_cell = ""
    # col_count = 17 normally, 18 with fit column
    col_count = 17
    if show_fit and fit_v is not None:
        fc = "#16a34a" if fit_v >= 70 else ("#eab308" if fit_v >= 40 else "#e11d48")
        fit_cell = f'<td class="r" style="width:6%"><span style="font-weight:800;color:{fc}">{fit_v:.0f}</span></td>'
        col_count = 18

    # Pass/fail badge
    if passes is None:
        passes_str = row.get("Passes", "")
        passes = str(passes_str).strip().lower() in ("true", "1", "yes")
    badge = (
        '<span style="font-size:9px;background:#dcfce7;color:#15803d;border-radius:3px;'
        'padding:1px 5px;font-weight:700">PASS</span>'
        if passes else
        '<span style="font-size:9px;background:#f1f5f9;color:#94a3b8;border-radius:3px;'
        'padding:1px 5px;font-weight:600">NEAR</span>'
    )
    status = row.get("Status", "")
    if status == "VALUE_TRAP":
        badge = ('<span style="font-size:9px;background:#fef2f2;color:#dc2626;border-radius:3px;'
                 'padding:1px 5px;font-weight:700">TRAP</span>')

    # Why Buy — generate panel + button
    ticker, panel_html, sc_v, sc_c = _why_buy(row, profile_key=None,
                                               ohlc=_PRICE_DATA.get(ticker) if _PRICE_DATA else None)
    sc_c = sc_c or "#9ca3af"
    why_btn_html = _why_btn(ticker, sc_v, sc_c) if panel_html else ""
    why_exp_row  = _why_tr(panel_html, col_count, ticker) if panel_html else ""

    rank_html = f'<span style="font-weight:800;color:{colour};font-size:13px">#{rank}</span>'

    # ── data-* attributes for live filtering (ST12) + watchlist JSON (ST13) ──
    import json as _json
    _ticker_raw  = row.get('Ticker', '')
    _mos_raw     = _fv(row.get('MoS%', '')) or 0.0
    _pe_raw      = _fv(row.get('P/E', '')) or 999.0
    _pfcf_raw    = _fv(row.get('P/FCF', '')) or 999.0
    _pio_raw     = _fv(row.get('Piotroski', '')) or 0.0
    _sector_attr = (row.get('Sector', '') or '').strip().replace('"', '&quot;')
    _row_json    = _json.dumps({
        "ticker":    _ticker_raw,
        "company":   row.get('Company', ''),
        "mos":       round(_mos_raw, 1),
        "pe":        round(_pe_raw, 1) if _pe_raw < 900 else None,
        "pfcf":      round(_pfcf_raw, 1) if _pfcf_raw < 900 else None,
        "piotroski": round(_pio_raw, 0),
        "sector":    row.get('Sector', ''),
        "price":     _fv(row.get('Price', '')),
        "fit":       _fv(row.get('ProfileFit', '')),
    }, separators=(',', ':')).replace('"', '&quot;')
    _star_btn = (
        f'<button class="star-btn" title="Add to watchlist" '
        f'data-ticker="{_ticker_raw}" data-row-json="{_row_json}" '
        f'onclick="toggleStar(this)">☆</button>'
    )

    data_tr = f"""<tr data-sector="{_sector_attr}" data-mos="{_mos_raw:.2f}" data-pe="{_pe_raw:.2f}" data-pfcf="{_pfcf_raw:.2f}" data-piotroski="{_pio_raw:.0f}">
      <td style="width:3%">{rank_html}</td>
      <td style="width:12%">
        <div class="ticker-lbl">{_ticker_raw} {_star_btn}</div>
        <div class="company-lbl">{row.get('Company','')}</div>
        <div style="margin-top:2px">{badge}</div>
        {why_btn_html}
      </td>
      <td style="width:9%;color:#57606a;font-size:11px">{row.get('Sector','') or '—'}</td>
      <td class="r" style="width:6%">{_fmt(row.get('Price',''),2,prefix='$')}</td>
      <td class="r" style="width:7%">{_fmt(row.get('DCF Avg',''),2,prefix='$')}</td>
      <td style="width:9%">{mos_bar}</td>
      <td style="width:8%">{pos_bar}</td>
      <td class="r" style="width:5%">{_fmt(row.get('P/E',''),1,suffix='x')}</td>
      <td class="r" style="width:5%">{_fmt(row.get('P/B',''),2,suffix='x')}</td>
      <td class="r" style="width:6%">{_fmt(row.get('EV/EBITDA',''),1,suffix='x')}</td>
      <td class="r" style="width:5%">{_fmt(row.get('P/FCF',''),1,suffix='x')}</td>
      <td class="r" style="width:6%">{_fmt(row.get('NetDebt/EBITDA',''),2,prefix='')}</td>
      <td style="width:5%;text-align:center">{_quality_badge(row.get('Piotroski',''),'piotroski')}</td>
      <td class="r" style="width:5%">{_quality_badge(row.get('ROIC%',''),'roic')}</td>
      <td style="width:6%;text-align:center">
        <span style="font-size:10px;background:#f0f2f5;padding:2px 4px;border-radius:4px;font-weight:600">{dcf_model}</span>
      </td>
      <td class="r" style="width:5%;font-weight:800">
        <span style="color:{mc}">{grade}</span>
        <div style="font-size:10px;color:#8d96a0;font-weight:400">{glabel}</div>
      </td>
      {fit_cell}
    </tr>"""
    return data_tr + why_exp_row


def _table_header(show_fit: bool = False) -> str:
    fit_th = '<th class="r" style="width:6%">Fit Score</th>' if show_fit else ""
    return f"""<thead><tr>
      <th style="width:3%">#</th>
      <th style="width:12%">Ticker / Company</th>
      <th style="width:9%">Sector</th>
      <th class="r" style="width:6%">Price</th>
      <th class="r" style="width:7%">Intrinsic Val.</th>
      <th style="width:9%">Margin of Safety</th>
      <th style="width:8%">52w Position</th>
      <th class="r" style="width:5%">P/E</th>
      <th class="r" style="width:5%">P/B</th>
      <th class="r" style="width:6%">EV/EBITDA</th>
      <th class="r" style="width:5%">P/FCF</th>
      <th class="r" style="width:6%">Net Debt/EBITDA</th>
      <th style="width:5%;text-align:center">Piotroski</th>
      <th class="r" style="width:5%">ROIC</th>
      <th style="width:6%;text-align:center">DCF Model</th>
      <th class="r" style="width:5%">Grade</th>
      {fit_th}
    </tr></thead>"""


def _compact_row(row: dict, rank: int) -> str:
    """Minimal one-line row for the 'rest of companies' compact table."""
    mos_v = _fv(row.get("MoS%", ""))
    mc    = _mos_colour(mos_v) if mos_v is not None else "#8d96a0"
    pos_v = _fv(row.get("52w Position%", ""))
    pc    = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"
    fit_v = _fv(row.get("ProfileFit", ""))
    fit_str = f"{fit_v:.0f}" if fit_v is not None else "—"
    fit_c = "#16a34a" if (fit_v or 0) >= 70 else ("#eab308" if (fit_v or 0) >= 40 else "#6b7280")

    passes_str = row.get("Passes", "")
    passes = str(passes_str).strip().lower() in ("true", "1", "yes")
    status = row.get("Status", "")

    if status == "VALUE_TRAP":
        status_badge = '<span style="font-size:9px;color:#dc2626;font-weight:700">TRAP</span>'
    elif passes:
        status_badge = '<span style="font-size:9px;color:#16a34a;font-weight:700">PASS</span>'
    else:
        status_badge = '<span style="font-size:9px;color:#9ca3af">—</span>'

    _c_ticker  = row.get('Ticker', '')
    _c_mos     = mos_v if mos_v is not None else 0.0
    _c_pe      = _fv(row.get('P/E', '')) or 999.0
    _c_pfcf    = _fv(row.get('P/FCF', '')) or 999.0
    _c_pio     = _fv(row.get('Piotroski', '')) or 0.0
    _c_sector  = (row.get('Sector', '') or '').strip().replace('"', '&quot;')

    return f"""<tr data-sector="{_c_sector}" data-mos="{_c_mos:.2f}" data-pe="{_c_pe:.2f}" data-pfcf="{_c_pfcf:.2f}" data-piotroski="{_c_pio:.0f}">
      <td style="color:#6b7280;font-size:12px">{rank}</td>
      <td>
        <span style="font-weight:700;font-size:12px">{_c_ticker}</span>
        <span style="color:#9ca3af;font-size:11px;margin-left:4px">{row.get('Company','')[:28]}</span>
        {status_badge}
      </td>
      <td style="font-size:11px;color:#9ca3af">{row.get('Sector','')[:18] or '—'}</td>
      <td class="r" style="font-size:12px">{_fmt(row.get('Price',''),2,prefix='$')}</td>
      <td class="r" style="font-size:12px;color:{mc};font-weight:700">{_fmt(row.get('MoS%',''),1,suffix='%') if mos_v is not None else '—'}</td>
      <td class="r" style="font-size:12px;color:{pc}">{_fmt(row.get('52w Position%',''),0,suffix='%') if pos_v is not None else '—'}</td>
      <td class="r" style="font-size:12px">{_fmt(row.get('P/E',''),1,suffix='x')}</td>
      <td class="r" style="font-size:12px">{_fmt(row.get('P/FCF',''),1,suffix='x')}</td>
      <td style="text-align:center">{_quality_badge(row.get('Piotroski',''),'piotroski')}</td>
      <td class="r" style="font-weight:800;color:{fit_c}">{fit_str}</td>
    </tr>"""


def _build_screener_section(profile_key: str, rows: list[dict], run_ts: str,
                             top_n: int = 10) -> str:
    """Build a profile section: KPI pills + top-N detailed + rest compact."""
    meta = _PROFILE_META.get(profile_key, {
        "label": profile_key.replace("_", " ").title(),
        "icon": profile_key[:2].upper(),
        "desc": "",
        "colour": "#3b82d4",
    })
    colour = meta["colour"]
    n = len(rows)

    # sort by ProfileFit desc (already sorted from CSV, but ensure it)
    rows_sorted = sorted(rows, key=lambda r: _fv(r.get("ProfileFit","")) or 0, reverse=True)

    passing   = [r for r in rows_sorted if str(r.get("Passes","")).strip().lower() in ("true","1","yes")]
    not_trap  = [r for r in rows_sorted if r.get("Status","") != "INSUFFICIENT_DATA"]
    top_rows  = rows_sorted[:top_n]
    rest_rows = rows_sorted[top_n:]

    n_pass = len(passing)
    best_fit = _fv(rows_sorted[0].get("ProfileFit","")) if rows_sorted else 0.0

    pills = f"""
    <div class="stats-bar">
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{n_pass}</div>
        <div class="sp-label">Strict Pass</div>
      </div>
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{len(not_trap)}</div>
        <div class="sp-label">Ranked Total</div>
      </div>
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{best_fit:.0f}/100</div>
        <div class="sp-label">Best Fit Score</div>
      </div>
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{run_ts}</div>
        <div class="sp-label">Data Run</div>
      </div>
    </div>"""

    # ── Top N detailed rows ────────────────────────────────────────────────────
    top_html = "".join(
        _row_to_table_tr(r, i+1, colour, show_fit=True) for i, r in enumerate(top_rows)
    )

    # ── Rest: compact rows ─────────────────────────────────────────────────────
    rest_html = ""
    if rest_rows:
        compact_rows = "".join(_compact_row(r, i+top_n+1) for i, r in enumerate(rest_rows))
        rest_html = f"""
        <details style="margin-top:16px">
          <summary style="cursor:pointer;font-size:13px;font-weight:700;color:#374151;
                          padding:10px 14px;background:#f7f8fa;border-radius:8px;
                          border:1px solid #e5e7eb;list-style:none;user-select:none">
            &#9660; Show all {len(rest_rows)} remaining companies (ranked #{top_n+1} – #{n})
            &nbsp;<span style="font-weight:400;color:#9ca3af;font-size:12px">
              — sorted by Fit Score descending, PASS/NEAR/TRAP status shown</span>
          </summary>
          <div style="margin-top:8px;overflow-x:hidden">
            <div id="filter-{profile_key}-rest" class="filter-bar">
              <label>Sector</label>
              <select onchange="applyFilter('{profile_key}-rest')"><option value="">All sectors</option></select>
              <label>Min MoS%</label>
              <input type="number" placeholder="0" min="-100" max="100" step="1" onchange="applyFilter('{profile_key}-rest')">
              <label>Max P/E</label>
              <input type="number" placeholder="any" min="0" max="999" step="1" onchange="applyFilter('{profile_key}-rest')">
              <label>Max P/FCF</label>
              <input type="number" placeholder="any" min="0" max="999" step="1" onchange="applyFilter('{profile_key}-rest')">
              <label>Min Piotroski</label>
              <input type="number" placeholder="0" min="0" max="9" step="1" onchange="applyFilter('{profile_key}-rest')">
              <button class="filter-reset" onclick="resetFilter('{profile_key}-rest')">Reset</button>
              <span class="filter-count" id="fc-{profile_key}-rest"></span>
            </div>
            <table class="stbl" style="font-size:11.5px" id="tbl-{profile_key}-rest">
              <thead style="background:#1f2328">
                <tr>
                  <th style="width:4%;color:#fff">#</th>
                  <th style="width:25%;color:#fff">Ticker / Company</th>
                  <th style="width:12%;color:#fff">Sector</th>
                  <th class="r" style="width:8%;color:#fff">Price</th>
                  <th class="r" style="width:8%;color:#fff">MoS%</th>
                  <th class="r" style="width:8%;color:#fff">52w Pos</th>
                  <th class="r" style="width:8%;color:#fff">P/E</th>
                  <th class="r" style="width:8%;color:#fff">P/FCF</th>
                  <th style="width:8%;text-align:center;color:#fff">Piotroski</th>
                  <th class="r" style="width:11%;color:#fff">Fit Score</th>
                </tr>
              </thead>
              <tbody>{compact_rows}</tbody>
            </table>
          </div>
        </details>"""

    return f"""
    <span class="section-anchor" id="{profile_key}"></span>
    <details class="sec-wrap">
      <summary>
        <div class="sec-hdr">
          <span class="sec-arrow">&#9654;</span>
          <span class="sec-badge" style="background:{colour}18;color:{colour}">{meta['icon']}</span>
          <span class="sec-title">{meta['label']} Screen</span>
          <span class="sec-meta">{n_pass} PASS &nbsp;·&nbsp; {n} ranked &nbsp;·&nbsp; best fit {best_fit:.0f}/100 &nbsp;·&nbsp; {run_ts}</span>
        </div>
      </summary>
      <div class="sec-body">
        <div style="font-size:13px;color:#57606a;margin-bottom:16px">{meta['desc']}</div>
        <div class="ib blue" style="margin-bottom:16px">
          <strong>Fit Score explained:</strong> 0–100, calculated as
          70% proximity to all profile thresholds + 30% composite quality score.
          <strong style="color:#16a34a">PASS</strong> = meets ALL strict criteria.
          <strong style="color:#9ca3af">NEAR</strong> = misses one or more criteria but still ranked.
          <strong style="color:#dc2626">TRAP</strong> = value trap flag (high debt / negative FCF).
          No company is hidden — all {n} ranked companies visible below.
        </div>
        {pills}
        <div id="filter-{profile_key}-top" class="filter-bar">
          <label>Sector</label>
          <select onchange="applyFilter('{profile_key}-top')"><option value="">All sectors</option></select>
          <label>Min MoS%</label>
          <input type="number" placeholder="0" min="-100" max="100" step="1" onchange="applyFilter('{profile_key}-top')">
          <label>Max P/E</label>
          <input type="number" placeholder="any" min="0" max="999" step="1" onchange="applyFilter('{profile_key}-top')">
          <label>Max P/FCF</label>
          <input type="number" placeholder="any" min="0" max="999" step="1" onchange="applyFilter('{profile_key}-top')">
          <label>Min Piotroski</label>
          <input type="number" placeholder="0" min="0" max="9" step="1" onchange="applyFilter('{profile_key}-top')">
          <button class="filter-reset" onclick="resetFilter('{profile_key}-top')">Reset</button>
          <span class="filter-count" id="fc-{profile_key}-top"></span>
        </div>
        <div style="font-size:13px;font-weight:700;margin-bottom:8px;color:#1f2328">
          Top {min(top_n, n)} — Detailed View
        </div>
        <table class="stbl" id="tbl-{profile_key}-top">
          {_table_header(show_fit=True)}
          <tbody>{top_html}</tbody>
        </table>
        {rest_html}
      </div>
    </details>"""


# ── Magic Formula section ─────────────────────────────────────────────────────

def _build_magic_formula_section(rows: list[dict], run_ts: str) -> str:
    """Build the Greenblatt Magic Formula section (simple ranked table, top 30)."""
    meta = _PROFILE_META["magic_formula"]
    colour = meta["colour"]
    n = len(rows)

    if not rows:
        return f"""
    <span class="section-anchor" id="magic_formula"></span>
    <details class="sec-wrap">
      <summary>
        <div class="sec-hdr">
          <span class="sec-arrow">&#9654;</span>
          <span class="sec-badge" style="background:{colour}18;color:{colour}">{meta['icon']}</span>
          <span class="sec-title">{meta['label']} Screen</span>
          <span class="sec-meta">No data available yet &nbsp;·&nbsp; run main.py to generate</span>
        </div>
      </summary>
      <div class="sec-body">
        <div style="font-size:13px;color:#57606a">{meta['desc']}</div>
        <div class="ib blue" style="margin-top:14px">
          No Magic Formula CSV found in <code>data/reports/</code>.
          Run <code>python src/main.py</code> to generate it.
        </div>
      </div>
    </details>"""

    tbl_rows = ""
    for row in rows:
        rank_v  = row.get("Magic Rank", "")
        ey_r    = row.get("EY Rank", "")
        roic_r  = row.get("ROIC Rank", "")
        ticker  = row.get("Ticker", "")
        company = row.get("Company", "")
        sector  = row.get("Sector", "")
        price_v = _fv(row.get("Price", ""))
        mos_v   = _fv(row.get("MoS%", ""))
        ey_v    = _fv(row.get("Earnings Yield%", ""))
        roic_v  = _fv(row.get("ROIC%", ""))
        pe_v    = _fv(row.get("P/E", ""))
        ev_v    = _fv(row.get("EV/EBITDA", ""))
        pio_v   = row.get("Piotroski", "")
        graham_v = _fv(row.get("Graham", ""))

        # rank colour: top5 = pink accent, else muted
        try:
            rank_int = int(rank_v)
            rc = colour if rank_int <= 5 else "#57606a"
        except (ValueError, TypeError):
            rc = "#57606a"

        mos_str = (
            f'<span style="color:{_mos_colour(mos_v)};font-weight:700">{mos_v:.1f}%</span>'
            if mos_v is not None else "—"
        )
        ey_str  = f'<strong style="color:{colour}">{ey_v:.2f}%</strong>' if ey_v is not None else "—"
        roic_str = (
            f'<span style="color:{"#16a34a" if roic_v >= 10 else ("#eab308" if roic_v >= 5 else "#e11d48")};font-weight:700">{roic_v:.1f}%</span>'
            if roic_v is not None else "—"
        )

        tbl_rows += f"""<tr>
          <td style="font-weight:800;color:{rc};font-size:14px;text-align:center">{rank_v}</td>
          <td style="text-align:center;font-size:12px;color:#8d96a0">{ey_r}</td>
          <td style="text-align:center;font-size:12px;color:#8d96a0">{roic_r}</td>
          <td>
            <div style="font-weight:800;font-size:13px">{ticker}</div>
            <div style="font-size:11px;color:#57606a">{company}</div>
          </td>
          <td style="font-size:11px;color:#57606a">{sector or '—'}</td>
          <td class="r">{_fmt(str(price_v) if price_v is not None else '', 2, prefix='$')}</td>
          <td class="r">{mos_str}</td>
          <td class="r">{ey_str}</td>
          <td class="r">{roic_str}</td>
          <td class="r">{_fmt(str(pe_v) if pe_v is not None else '', 1, suffix='x')}</td>
          <td class="r">{_fmt(str(ev_v) if ev_v is not None else '', 1, suffix='x')}</td>
          <td style="text-align:center">{_quality_badge(str(pio_v), 'piotroski')}</td>
          <td class="r">{_fmt(str(graham_v) if graham_v is not None else '', 2, prefix='$')}</td>
        </tr>"""

    return f"""
    <span class="section-anchor" id="magic_formula"></span>
    <details class="sec-wrap">
      <summary>
        <div class="sec-hdr">
          <span class="sec-arrow">&#9654;</span>
          <span class="sec-badge" style="background:{colour}18;color:{colour}">{meta['icon']}</span>
          <span class="sec-title">{meta['label']} Screen</span>
          <span class="sec-meta">Top {n} companies &nbsp;·&nbsp; EY + ROIC dual rank &nbsp;·&nbsp; {run_ts}</span>
        </div>
      </summary>
      <div class="sec-body">
        <div style="font-size:13px;color:#57606a;margin-bottom:16px">{meta['desc']}</div>
        <div class="ib blue" style="margin-bottom:16px">
          <strong>How to read:</strong> EY Rank = Earnings Yield rank (1 = highest yield = cheapest).
          ROIC Rank = Return on Invested Capital rank (1 = highest quality).
          <strong>Magic Rank</strong> = EY Rank + ROIC Rank — the company with the <em>lowest combined rank</em>
          is the top Magic Formula pick. Financials, Utilities, and Real Estate are excluded per Greenblatt.
        </div>
        <div class="stats-bar">
          <div class="stat-pill">
            <div class="sp-value" style="color:{colour}">{n}</div>
            <div class="sp-label">Top Companies</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:{colour}">EY+ROIC</div>
            <div class="sp-label">Dual Rank Method</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:{colour}">{run_ts}</div>
            <div class="sp-label">Data Run</div>
          </div>
        </div>
        <table class="stbl" style="table-layout:fixed">
          <thead><tr>
            <th style="width:5%;text-align:center">Magic #</th>
            <th style="width:5%;text-align:center">EY Rnk</th>
            <th style="width:5%;text-align:center">ROIC Rnk</th>
            <th style="width:14%">Ticker / Company</th>
            <th style="width:11%">Sector</th>
            <th class="r" style="width:7%">Price</th>
            <th class="r" style="width:7%">MoS%</th>
            <th class="r" style="width:8%">EY%</th>
            <th class="r" style="width:7%">ROIC%</th>
            <th class="r" style="width:6%">P/E</th>
            <th class="r" style="width:8%">EV/EBITDA</th>
            <th style="width:8%;text-align:center">Piotroski</th>
            <th class="r" style="width:9%">Graham</th>
          </tr></thead>
          <tbody>{tbl_rows}</tbody>
        </table>
      </div>
    </details>"""


# ── Backtest section ──────────────────────────────────────────────────────────

def _build_backtest_section(rows: list[dict], run_ts: str) -> str:
    # Split data rows from SUMMARY row
    data_rows   = [r for r in rows if r.get("Year","").upper() != "SUMMARY"]
    summary_row = next((r for r in rows if r.get("Year","").upper() == "SUMMARY"), {})

    cagr_port = _fv(summary_row.get("Portfolio%","")) or 0.0
    cagr_bm   = _fv(summary_row.get("Benchmark%","")) or 0.0
    excess    = cagr_port - cagr_bm
    win_rate  = _fv(summary_row.get("WinRate%","")) or 0.0
    total_pk  = summary_row.get("Picks","—")
    sharpe    = "—"
    sortino   = "—"
    maxdd     = "—"
    extra_txt = summary_row.get("SelectedTickers","")
    for part in extra_txt.split():
        if part.startswith("Sharpe="):   sharpe  = part.split("=",1)[1]
        if part.startswith("Sortino="):  sortino = part.split("=",1)[1]
        if part.startswith("MaxDD="):    maxdd   = part.split("=",1)[1]

    # year range
    years = [r.get("Year","") for r in data_rows]
    start_yr = years[0] if years else "—"
    end_yr   = years[-1] if years else "—"

    cagr_colour   = _return_colour(cagr_port)
    excess_colour = _return_colour(excess)

    # ── KPI bar ──────────────────────────────────────────────────────────────
    sign_port = "+" if cagr_port >= 0 else ""
    sign_bm   = "+" if cagr_bm   >= 0 else ""
    sign_exc  = "+" if excess     >= 0 else ""

    kpis = f"""
    <div class="bt-header">
      <div class="bt-kpi">
        <div class="kv" style="color:{cagr_colour}">{sign_port}{cagr_port:.2f}%</div>
        <div class="kl">Portfolio CAGR</div>
      </div>
      <div class="bt-kpi">
        <div class="kv" style="color:#57606a">{sign_bm}{cagr_bm:.2f}%</div>
        <div class="kl">S&amp;P 500 CAGR (Benchmark)</div>
      </div>
      <div class="bt-kpi">
        <div class="kv" style="color:{excess_colour}">{sign_exc}{excess:.2f}%</div>
        <div class="kl">Excess Return vs S&amp;P 500</div>
      </div>
      <div class="bt-kpi">
        <div class="kv">{sharpe}</div>
        <div class="kl">Sharpe Ratio</div>
      </div>
      <div class="bt-kpi">
        <div class="kv">{sortino}</div>
        <div class="kl">Sortino Ratio</div>
      </div>
      <div class="bt-kpi">
        <div class="kv" style="color:#e11d48">{maxdd}</div>
        <div class="kl">Max Drawdown</div>
      </div>
      <div class="bt-kpi">
        <div class="kv">{win_rate:.0f}%</div>
        <div class="kl">Stock Win Rate</div>
      </div>
    </div>"""

    # ── Visual bar chart — Portfolio vs S&P 500 per year ─────────────────────
    # Find the max absolute return to scale bars
    all_vals = []
    for r in data_rows:
        pv = _fv(r.get("Portfolio%",""))
        bv = _fv(r.get("Benchmark%",""))
        if pv is not None: all_vals.append(abs(pv))
        if bv is not None: all_vals.append(abs(bv))
    max_abs = max(all_vals) if all_vals else 30.0
    scale   = 100.0 / (max_abs * 2 + 4)   # 50% of track width = max_abs %

    chart_html = '<div class="chart-wrap">'
    chart_html += f"""
    <div style="display:flex;gap:18px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:14px;height:14px;background:#3b82d4;border-radius:3px"></div>
        <span style="font-size:12px;font-weight:600;color:#374151">Portfolio (Deep Value picks)</span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:14px;height:14px;background:#9ca3af;border-radius:3px"></div>
        <span style="font-size:12px;font-weight:600;color:#374151">S&amp;P 500 Benchmark (^GSPC)</span>
      </div>
      <div style="font-size:11px;color:#8d96a0;margin-left:auto">Zero line at centre — bars extend left (negative) or right (positive)</div>
    </div>"""

    for r in data_rows:
        yr   = r.get("Year","")
        pv   = _fv(r.get("Portfolio%",""))
        bv   = _fv(r.get("Benchmark%",""))
        ev   = _fv(r.get("Excess%",""))
        pkn  = r.get("Picks","—")
        wn   = r.get("WinningPicks","—")
        sign_pv = "+" if (pv or 0) >= 0 else ""
        sign_bv = "+" if (bv or 0) >= 0 else ""
        sign_ev = "+" if (ev or 0) >= 0 else ""
        exc_cls = "excess-pos" if (ev or 0) >= 0 else "excess-neg"
        p_colour = _return_colour(pv or 0)
        b_colour = "#9ca3af"

        def _bar(val: float | None, colour: str) -> str:
            if val is None:
                return '<div style="height:22px;background:#f0f2f5;border-radius:4px;display:flex;align-items:center;padding:0 8px"><span style="font-size:11px;color:#8d96a0">—</span></div>'
            pct_w = min(abs(val) * scale * 100, 50)
            sign_str = "+" if val >= 0 else ""
            if val >= 0:
                return (
                    f'<div style="position:relative;height:22px;background:#f0f2f5;border-radius:4px">'
                    f'<div style="position:absolute;left:50%;top:0;width:{pct_w:.2f}%;height:100%;'
                    f'background:{colour};border-radius:0 4px 4px 0;display:flex;align-items:center;'
                    f'padding-left:6px"><span style="font-size:11px;font-weight:700;color:#fff;white-space:nowrap">{sign_str}{val:.1f}%</span></div>'
                    f'<div style="position:absolute;left:50%;top:0;height:100%;border-left:2px solid #9ca3af"></div>'
                    f'</div>'
                )
            else:
                return (
                    f'<div style="position:relative;height:22px;background:#f0f2f5;border-radius:4px">'
                    f'<div style="position:absolute;right:50%;top:0;width:{pct_w:.2f}%;height:100%;'
                    f'background:{colour};border-radius:4px 0 0 4px;display:flex;align-items:center;'
                    f'justify-content:flex-end;padding-right:6px"><span style="font-size:11px;font-weight:700;color:#fff;white-space:nowrap">{val:.1f}%</span></div>'
                    f'<div style="position:absolute;left:50%;top:0;height:100%;border-left:2px solid #9ca3af"></div>'
                    f'</div>'
                )

        chart_html += f"""
        <div style="display:flex;align-items:stretch;gap:12px;margin-bottom:14px;
                    background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px">
          <div style="width:40px;flex-shrink:0;display:flex;align-items:center;
                      justify-content:center;font-size:15px;font-weight:800;color:#1f2328">{yr}</div>
          <div style="flex:1;display:flex;flex-direction:column;gap:5px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="width:80px;font-size:11px;color:#374151;font-weight:600;flex-shrink:0">Portfolio</span>
              <div style="flex:1">{_bar(pv, p_colour)}</div>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="width:80px;font-size:11px;color:#374151;font-weight:600;flex-shrink:0">S&amp;P 500</span>
              <div style="flex:1">{_bar(bv, b_colour)}</div>
            </div>
          </div>
          <div style="width:120px;flex-shrink:0;display:flex;flex-direction:column;
                      align-items:flex-end;justify-content:center;gap:2px">
            <div style="font-size:11px;color:#8d96a0">Excess Return</div>
            <div class="{exc_cls}" style="font-size:16px">{sign_ev}{(ev or 0):.1f}%</div>
            <div style="font-size:11px;color:#8d96a0">{wn}/{pkn} picks beat SPX</div>
          </div>
        </div>"""

    chart_html += "</div>"

    # ── Detailed table ────────────────────────────────────────────────────────
    tbl_rows = ""
    for r in data_rows:
        yr   = r.get("Year","")
        pv   = _fv(r.get("Portfolio%",""))
        bv   = _fv(r.get("Benchmark%",""))
        ev   = _fv(r.get("Excess%",""))
        pkn  = r.get("Picks","—")
        wn   = r.get("WinningPicks","—")
        wr   = _fv(r.get("WinRate%",""))
        tkrs = r.get("SelectedTickers","").replace("|", " · ")
        p_col = _return_colour(pv or 0)
        b_col = _return_colour(bv or 0)
        exc_cls = "excess-pos" if (ev or 0) >= 0 else "excess-neg"
        sign_pv = "+" if (pv or 0) >= 0 else ""
        sign_bv = "+" if (bv or 0) >= 0 else ""
        sign_ev = "+" if (ev or 0) >= 0 else ""
        tbl_rows += f"""<tr>
          <td style="font-weight:800;font-size:15px">{yr}</td>
          <td class="r" style="color:{p_col};font-weight:700">{sign_pv}{(pv or 0):.2f}%</td>
          <td class="r" style="color:{b_col};font-weight:700">{sign_bv}{(bv or 0):.2f}%</td>
          <td class="r"><span class="{exc_cls}">{sign_ev}{(ev or 0):.2f}%</span></td>
          <td class="r">{wn}/{pkn}</td>
          <td class="r">{(wr or 0):.0f}%</td>
          <td style="font-size:12px;color:#57606a">{tkrs}</td>
        </tr>"""

    # Summary row
    sp_port = "+" if cagr_port >= 0 else ""
    sp_bm   = "+" if cagr_bm   >= 0 else ""
    sp_exc  = "+" if excess     >= 0 else ""
    exc_cls_s = "excess-pos" if excess >= 0 else "excess-neg"
    tbl_rows += f"""<tr style="background:#f7f8fa;font-weight:800">
      <td>CAGR</td>
      <td class="r" style="color:{_return_colour(cagr_port)}">{sp_port}{cagr_port:.2f}%</td>
      <td class="r" style="color:{_return_colour(cagr_bm)}">{sp_bm}{cagr_bm:.2f}%</td>
      <td class="r"><span class="{exc_cls_s}">{sp_exc}{excess:.2f}%</span></td>
      <td class="r">{total_pk}</td>
      <td class="r">{win_rate:.0f}%</td>
      <td style="font-size:12px;color:#57606a">Sharpe {sharpe} · Sortino {sortino} · MaxDD {maxdd}</td>
    </tr>"""

    return f"""
    <span class="section-anchor" id="backtest"></span>
    <div class="section">
      <div class="profile-badge" style="background:#1f232811;border-color:#1f232844;color:#1f2328">
        BT &nbsp; Backtest
      </div>
      <div class="section-title">Walk-Forward Backtest — Deep Value vs S&amp;P 500</div>
      <div class="section-sub">
        Simulated {start_yr}–{end_yr} performance of the Deep Value top picks measured against the
        <strong>S&amp;P 500 Index (^GSPC)</strong> as benchmark. Entry/exit on the first trading day
        of each calendar year. Equal-weighted portfolio. Run date: {run_ts}.
      </div>

      {kpis}

      <hr class="divider">
      <div style="font-size:14px;font-weight:700;margin-bottom:8px;color:#1f2328">
        Annual Returns — Portfolio vs S&amp;P 500 Benchmark
      </div>
      <div style="font-size:12px;color:#57606a;margin-bottom:14px">
        Each year shows the portfolio return (blue) and the S&amp;P 500 return (grey).
        The bar extends <strong>right for gains</strong> and <strong>left for losses</strong>.
        The centre line represents zero. Excess Return = Portfolio minus S&amp;P 500.
      </div>
      {chart_html}

      <hr class="divider">
      <div style="font-size:14px;font-weight:700;margin-bottom:10px;color:#1f2328">
        Detailed Year-by-Year Results
      </div>
      <table class="bt-tbl" style="width:100%;table-layout:fixed">
        <thead><tr>
          <th style="width:8%">Year</th>
          <th class="r" style="width:14%">Portfolio Return</th>
          <th class="r" style="width:14%">S&amp;P 500 Return</th>
          <th class="r" style="width:14%">Excess vs SPX</th>
          <th class="r" style="width:10%">Wins / Picks</th>
          <th class="r" style="width:10%">Win Rate</th>
          <th style="width:30%">Selected Tickers</th>
        </tr></thead>
        <tbody>{tbl_rows}</tbody>
      </table>

      <div class="limit-box">
        <strong>Important Limitations of this Backtest:</strong><br>
        (1) <strong>Look-ahead bias</strong> — uses current financials as screening criteria for all historical years.
        Real point-in-time data would differ; results are likely optimistic.<br>
        (2) <strong>Survivorship bias</strong> — universe contains only current S&amp;P 500 constituents.
        Companies removed since {start_yr} (failed, merged, delisted) are excluded.<br>
        (3) <strong>Single-day pricing</strong> — entry/exit on one date per year, no slippage or bid-ask spread.<br>
        (4) <strong>No transaction costs</strong> — commissions and taxes are not modelled.<br>
        Treat results as <em>directional indicators</em> of strategy quality, not as reliable future predictions.
      </div>
    </div>"""


# ── Top Convictions section ───────────────────────────────────────────────────

_PROFILE_LABEL_SHORT = {
    "deep_value":      ("DV",  "#3b82d4", "Deep Value"),
    "buffett_quality": ("BQ",  "#7c3aed", "Buffett Quality"),
    "high_fcf_yield":  ("FCF", "#059669", "High FCF Yield"),
    "quality_value":   ("QV",  "#d97706", "Quality Value"),
    "dividend_growth": ("DIV", "#0891b2", "Dividend Growth"),
}

def _build_convictions_section(all_profile_rows: dict[str, list[dict]]) -> str:
    """
    Build a 'Top Convictions' section: companies that STRICTLY PASS 2+ profiles,
    ranked by number of profile overlaps then by best MoS%.

    NOTE: all_profile_rows now contains ALL companies (new CSV format with
    ProfileFit/Passes/Status columns), so we must filter to Passes=True rows
    before determining conviction overlaps.
    """
    # Collect only strictly-passing tickers and which profiles they pass
    ticker_profiles: dict[str, list[str]] = {}
    ticker_data: dict[str, dict] = {}   # best data row per ticker (highest MoS)

    for key, rows in all_profile_rows.items():
        for row in rows:
            tkr = row.get("Ticker", "").strip()
            if not tkr:
                continue
            # Only consider rows where the company strictly passes all criteria
            is_pass = str(row.get("Passes", "")).strip().lower() in ("true", "1", "yes")
            if not is_pass:
                continue
            if tkr not in ticker_profiles:
                ticker_profiles[tkr] = []
                ticker_data[tkr] = row
            if key not in ticker_profiles[tkr]:
                ticker_profiles[tkr].append(key)
            # Keep the row with the highest MoS as representative data
            existing_mos = _fv(ticker_data[tkr].get("MoS%", "")) or 0.0
            new_mos      = _fv(row.get("MoS%", "")) or 0.0
            if new_mos > existing_mos:
                ticker_data[tkr] = row

    # Filter to tickers in 2+ profiles and sort: overlap count desc, MoS desc
    multi = {t: ps for t, ps in ticker_profiles.items() if len(ps) >= 2}
    if not multi:
        return ""   # no overlaps — skip section entirely

    ranked = sorted(
        multi.items(),
        key=lambda x: (len(x[1]), _fv(ticker_data[x[0]].get("MoS%","")) or 0),
        reverse=True,
    )

    rows_html = ""
    for tkr, profiles in ranked:
        row     = ticker_data[tkr]
        n_prof  = len(profiles)
        mos_v   = _fv(row.get("MoS%","")) or 0.0
        mc      = _mos_colour(mos_v)
        grade, glabel = _mos_grade(mos_v)
        pos_v   = _fv(row.get("52w Position%",""))
        pc      = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"

        # conviction level: dynamic based on how many profiles have data loaded
        n_total = len([k for k, v in all_profile_rows.items() if v])
        n_total = max(n_total, 2)   # safety floor
        if n_prof >= n_total:
            conv_colour, conv_label = "#d97706", f"GOLD — {n_prof}/{n_total} profiles"
        elif n_prof >= n_total - 1:
            conv_colour, conv_label = "#16a34a", f"HIGH — {n_prof}/{n_total} profiles"
        else:
            conv_colour, conv_label = "#3b82d4", f"MODERATE — {n_prof}/{n_total} profiles"

        # Profile badges
        badge_html = " ".join(
            f'<span style="display:inline-block;padding:2px 7px;border-radius:4px;'
            f'font-size:11px;font-weight:700;background:{_PROFILE_LABEL_SHORT[p][1]}18;'
            f'color:{_PROFILE_LABEL_SHORT[p][1]};border:1px solid {_PROFILE_LABEL_SHORT[p][1]}44">'
            f'{_PROFILE_LABEL_SHORT[p][0]}</span>'
            for p in profiles
        )

        pos_bar = (
            f'<div class="gauge-wrap"><div class="gauge-track">'
            f'<div class="gauge-fill" style="width:{min(pos_v,100):.1f}%;background:{pc}"></div>'
            f'</div><div class="gauge-pct" style="color:{pc}">{pos_v:.0f}%</div></div>'
            if pos_v is not None else "—"
        )
        mos_bar = (
            f'<div class="gauge-wrap"><div class="gauge-track">'
            f'<div class="gauge-fill" style="width:{min(mos_v,100):.1f}%;background:{mc}"></div>'
            f'</div><div class="gauge-pct" style="color:{mc}">{mos_v:.0f}%</div></div>'
        )

        _, panel_html, sc_v, sc_c = _why_buy(row, profiles=profiles,
                                              ohlc=_PRICE_DATA.get(tkr) if _PRICE_DATA else None)
        sc_c = sc_c or "#9ca3af"
        why_btn_html = _why_btn(tkr, sc_v, sc_c) if panel_html else ""
        why_exp_row  = _why_tr(panel_html, 12, tkr) if panel_html else ""

        rows_html += f"""<tr>
          <td style="width:11%">
            <div style="font-weight:700;font-size:10px;color:{conv_colour};
                        background:{conv_colour}12;border:1px solid {conv_colour}33;
                        border-radius:4px;padding:2px 6px;display:inline-block">{conv_label}</div>
          </td>
          <td style="width:13%">
            <div style="font-weight:800;font-size:13px">{tkr}</div>
            <div style="font-size:11px;color:#57606a">{row.get('Company','')}</div>
            {why_btn_html}
          </td>
          <td style="width:9%;font-size:11px;color:#57606a">{row.get('Sector','') or '—'}</td>
          <td class="r" style="width:6%;font-weight:700">{_fmt(row.get('Price',''),2,prefix='$')}</td>
          <td class="r" style="width:7%;font-weight:700">{_fmt(row.get('DCF Avg',''),2,prefix='$')}</td>
          <td style="width:9%">{mos_bar}</td>
          <td style="width:8%">{pos_bar}</td>
          <td class="r" style="width:5%">{_fmt(row.get('P/E',''),1,suffix='x')}</td>
          <td class="r" style="width:5%">{_fmt(row.get('P/FCF',''),1,suffix='x')}</td>
          <td style="width:6%;text-align:center">{_quality_badge(row.get('Piotroski',''),'piotroski')}</td>
          <td class="r" style="width:6%">{_quality_badge(row.get('ROIC%',''),'roic')}</td>
          <td style="width:15%">{badge_html}</td>
        </tr>""" + why_exp_row

    n_conv = len(ranked)
    top_ticker = ranked[0][0] if ranked else "—"
    top_n      = len(ranked[0][1]) if ranked else 0

    n_gold = len([t for t,ps in ranked if len(ps)==4])
    n_high = len([t for t,ps in ranked if len(ps)==3])

    return f"""
    <span class="section-anchor" id="convictions"></span>
    <details class="sec-wrap">
      <summary>
        <div class="sec-hdr" style="border-left:4px solid #d97706">
          <span class="sec-arrow">&#9654;</span>
          <span class="sec-badge" style="background:#d9770618;color:#d97706">&#9733;</span>
          <span class="sec-title">Top Convictions &mdash; Multi-Profile Overlap</span>
          <span class="sec-meta">{n_conv} companies &nbsp;·&nbsp; {n_gold} Gold &nbsp;·&nbsp; {n_high} High &nbsp;·&nbsp; strongest: {top_ticker} ({top_n} profiles)</span>
        </div>
      </summary>
      <div class="sec-body">
        <p style="font-size:13px;color:#57606a;margin-bottom:14px">
          Companies that passed <strong>2 or more screener profiles simultaneously</strong>.
          The more profiles a company passes, the stronger the quantitative signal.
        </p>
        <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
                    padding:12px 16px;margin-bottom:16px;font-size:13px;color:#92400e">
          <strong style="color:#d97706">GOLD (4/4)</strong> = passes every screen.
          <strong style="color:#16a34a">HIGH (3/4)</strong> = 3 independent philosophies agree.
          <strong style="color:#3b82d4">MODERATE (2/4)</strong> = 2 independent approaches confirm.
        </div>
        <div class="stats-bar">
          <div class="stat-pill">
            <div class="sp-value" style="color:#d97706">{n_conv}</div>
            <div class="sp-label">Multi-Profile</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:#d97706">{n_gold}</div>
            <div class="sp-label">Gold (4/4)</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:#16a34a">{n_high}</div>
            <div class="sp-label">High (3/4)</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:#d97706">{top_ticker}</div>
            <div class="sp-label">Strongest ({top_n} profiles)</div>
          </div>
        </div>
        <table class="stbl">
          <thead><tr>
            <th style="width:11%">Conviction</th>
            <th style="width:13%">Ticker / Company</th>
            <th style="width:9%">Sector</th>
            <th class="r" style="width:6%">Price</th>
            <th class="r" style="width:7%">Intrinsic Val.</th>
            <th style="width:9%">Margin of Safety</th>
            <th style="width:8%">52w Position</th>
            <th class="r" style="width:5%">P/E</th>
            <th class="r" style="width:5%">P/FCF</th>
            <th style="width:6%;text-align:center">Piotroski</th>
            <th class="r" style="width:6%">ROIC</th>
            <th style="width:15%">Profiles</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </details>"""


# ── Overall Top section (cross-profile ranking) ───────────────────────────────

def _build_overall_top(
    all_profile_rows: dict[str, list[dict]],
    top_n: int = 10,
) -> str:
    """
    Cross-profile 'Top Overall' section.

    For every company in the universe, collects its ProfileFit score from each
    profile CSV it appears in, then computes a weighted-average Overall Score.
    Deep Value carries weight 1.3 (strictest), down to FCF Yield at 1.0.

    Shows top-N with Why-Buy reasoning for each.
    """
    # Profile weights — stricter profiles carry more signal
    weights = {
        "deep_value":      1.30,
        "buffett_quality": 1.20,
        "quality_value":   1.10,
        "high_fcf_yield":  1.00,
        "dividend_growth": 1.05,
    }

    ticker_raw_fits: dict[str, dict[str, float]] = {}   # tkr -> {profile: raw fit}
    ticker_data:     dict[str, dict]              = {}   # best representative row
    ticker_passes:   dict[str, list[str]]         = {}   # profiles strictly passed

    for key, rows in all_profile_rows.items():
        w = weights.get(key, 1.0)
        for row in rows:
            tkr = row.get("Ticker", "").strip()
            if not tkr:
                continue
            fit_v = _fv(row.get("ProfileFit", ""))
            if fit_v is None:
                continue

            if tkr not in ticker_raw_fits:
                ticker_raw_fits[tkr] = {}
                ticker_data[tkr]     = row
                ticker_passes[tkr]   = []

            ticker_raw_fits[tkr][key] = fit_v   # store raw fit (multiply at calc time)

            is_pass = str(row.get("Passes", "")).strip().lower() in ("true", "1", "yes")
            if is_pass and key not in ticker_passes[tkr]:
                ticker_passes[tkr].append(key)

            existing_mos = _fv(ticker_data[tkr].get("MoS%", "")) or 0.0
            new_mos      = _fv(row.get("MoS%", "")) or 0.0
            if new_mos > existing_mos:
                ticker_data[tkr] = row

    if not ticker_raw_fits:
        return ""

    def _overall(tkr: str) -> float:
        fits  = ticker_raw_fits[tkr]
        w_sum = sum(weights.get(k, 1.0) for k in fits)
        w_fit = sum(fits[k] * weights.get(k, 1.0) for k in fits)
        return round(w_fit / w_sum, 1) if w_sum > 0 else 0.0

    ranked_all = sorted(ticker_raw_fits.keys(), key=_overall, reverse=True)
    top        = ranked_all[:top_n]
    if not top:
        return ""

    best_score = _overall(top[0])
    n_universe = len(ticker_raw_fits)
    n_strict   = sum(1 for t in ticker_raw_fits if ticker_passes.get(t))

    rows_html = ""
    for i, tkr in enumerate(top):
        row       = ticker_data[tkr]
        score     = _overall(tkr)
        passes_in = ticker_passes.get(tkr, [])

        sc = "#16a34a" if score >= 75 else ("#eab308" if score >= 55 else "#e11d48")
        mos_v = _fv(row.get("MoS%", "")) or 0.0
        mc    = _mos_colour(mos_v)
        grade, glabel = _mos_grade(mos_v)
        pos_v = _fv(row.get("52w Position%", ""))
        pc    = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"

        mos_bar = (
            f'<div class="gauge-wrap"><div class="gauge-track">'
            f'<div class="gauge-fill" style="width:{min(mos_v,100):.1f}%;background:{mc}"></div>'
            f'</div><div class="gauge-pct" style="color:{mc}">{mos_v:.0f}%</div></div>'
        )
        pos_bar = (
            f'<div class="gauge-wrap"><div class="gauge-track">'
            f'<div class="gauge-fill" style="width:{min(pos_v,100):.1f}%;background:{pc}"></div>'
            f'</div><div class="gauge-pct" style="color:{pc}">{pos_v:.0f}%</div></div>'
            if pos_v is not None else "—"
        )

        badge_html = ""
        for pk in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value"):
            if pk in ticker_raw_fits.get(tkr, {}):
                info = _PROFILE_LABEL_SHORT[pk]
                is_p = pk in passes_in
                bg   = info[1] if is_p else "#94a3b8"
                badge_html += (
                    f'<span style="display:inline-block;padding:2px 6px;border-radius:4px;'
                    f'font-size:10px;font-weight:700;background:{bg}18;color:{bg};'
                    f'border:1px solid {bg}44;margin:1px">{info[0]}</span>'
                )

        _, panel_html, _sc_v, _sc_c = _why_buy(row, profiles=passes_in if passes_in else None,
                                                overall_score=score,
                                                ohlc=_PRICE_DATA.get(tkr) if _PRICE_DATA else None)
        _sc_c = _sc_c or "#9ca3af"
        why_btn_html = _why_btn(tkr, _sc_v if _sc_v is not None else score, _sc_c) if panel_html else ""
        why_exp_row  = _why_tr(panel_html, 14, tkr) if panel_html else ""

        rank_colour = "#d97706" if i == 0 else ("#3b82d4" if i < 3 else "#57606a")

        rows_html += f"""<tr>
          <td style="width:4%;text-align:center">
            <span style="font-weight:800;color:{rank_colour};font-size:15px">#{i+1}</span>
          </td>
          <td style="width:13%">
            <div style="font-weight:800;font-size:14px">{tkr}</div>
            <div style="font-size:11px;color:#57606a">{row.get('Company','')}</div>
            {why_btn_html}
          </td>
          <td style="width:9%;font-size:11px;color:#57606a">{row.get('Sector','') or '&mdash;'}</td>
          <td class="r" style="width:7%">
            <span style="font-size:22px;font-weight:900;color:{sc}">{score:.0f}</span>
            <div style="font-size:10px;color:#9ca3af">/ 100</div>
          </td>
          <td style="width:10%">{badge_html}</td>
          <td class="r" style="width:6%;font-weight:700">{_fmt(row.get('Price',''),2,prefix='$')}</td>
          <td class="r" style="width:7%;font-weight:700">{_fmt(row.get('DCF Avg',''),2,prefix='$')}</td>
          <td style="width:9%">{mos_bar}</td>
          <td style="width:8%">{pos_bar}</td>
          <td class="r" style="width:5%">{_fmt(row.get('P/E',''),1,suffix='x')}</td>
          <td class="r" style="width:5%">{_fmt(row.get('P/FCF',''),1,suffix='x')}</td>
          <td style="width:5%;text-align:center">{_quality_badge(row.get('Piotroski',''),'piotroski')}</td>
          <td class="r" style="width:5%">{_quality_badge(row.get('ROIC%',''),'roic')}</td>
          <td class="r" style="width:5%;font-weight:800;color:{mc}">
            {grade}
            <div style="font-size:10px;color:#8d96a0;font-weight:400">{glabel}</div>
          </td>
        </tr>""" + why_exp_row

    return f"""
    <span class="section-anchor" id="overall_top"></span>
    <details class="sec-wrap">
      <summary>
        <div class="sec-hdr" style="border-left:4px solid #3b82d4">
          <span class="sec-arrow">&#9654;</span>
          <span class="sec-badge" style="background:#3b82d418;color:#3b82d4">&#9650;</span>
          <span class="sec-title">Top Overall &mdash; Cross-Profile Ranking</span>
          <span class="sec-meta">{n_universe} ranked &nbsp;·&nbsp; {n_strict} any PASS &nbsp;·&nbsp; best score {best_score:.0f}/100 &nbsp;·&nbsp; top {top_n} shown</span>
        </div>
      </summary>
      <div class="sec-body">
        <div class="ib blue" style="margin-bottom:18px">
          <strong>Overall Score (0&ndash;100)</strong> =
          weighted average (Deep Value &times;1.3 + Buffett Quality &times;1.2 + Quality Value &times;1.1 + FCF Yield &times;1.0).
          <strong>filled badge</strong> = strict PASS &nbsp;|&nbsp;
          <strong style="color:#94a3b8">grey badge</strong> = ranked but did not strictly pass.
        </div>
        <div class="stats-bar">
          <div class="stat-pill">
            <div class="sp-value" style="color:#3b82d4">{n_universe}</div>
            <div class="sp-label">Companies Ranked</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:#16a34a">{n_strict}</div>
            <div class="sp-label">Any Strict Pass</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:#3b82d4">{best_score:.0f}/100</div>
            <div class="sp-label">Best Overall Score</div>
          </div>
          <div class="stat-pill">
            <div class="sp-value" style="color:#3b82d4">Top {top_n}</div>
            <div class="sp-label">Shown Here</div>
          </div>
        </div>
        <table class="stbl">
          <thead><tr>
            <th style="width:4%;text-align:center">#</th>
            <th style="width:13%">Ticker / Company</th>
            <th style="width:9%">Sector</th>
            <th class="r" style="width:7%">Overall Score</th>
            <th style="width:10%">Profiles</th>
            <th class="r" style="width:6%">Price</th>
            <th class="r" style="width:7%">Intrinsic Val.</th>
            <th style="width:9%">Margin of Safety</th>
            <th style="width:8%">52w Position</th>
            <th class="r" style="width:5%">P/E</th>
            <th class="r" style="width:5%">P/FCF</th>
            <th style="width:5%;text-align:center">Piotroski</th>
            <th class="r" style="width:5%">ROIC</th>
            <th class="r" style="width:5%">Grade</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </details>"""


# ── Full report builder ───────────────────────────────────────────────────────

def build_full_report(out_path: Path) -> None:
    global _PRICE_DATA
    now = datetime.now().strftime("%d %B %Y, %H:%M")

    # Load most recent CSV for each profile
    all_profile_rows: dict[str, list[dict]] = {}
    profile_sections: list[str] = []
    n_pass_per_profile: dict[str, int] = {}

    for key in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value", "dividend_growth"):
        p = _most_recent(f"*_{key}.csv", exclude_backtest=True)
        if p is None:
            rows, ts = [], "—"
        else:
            rows = _load_csv(p)
            ts   = p.stem[:15]
            try: ts = datetime.strptime(ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
            except Exception: pass
        all_profile_rows[key] = rows
        # Count only strict-pass companies (new CSV format: Passes column)
        n_pass = sum(
            1 for r in rows
            if str(r.get("Passes", "")).strip().lower() in ("true", "1", "yes")
        )
        n_pass_per_profile[key] = n_pass
        profile_sections.append(_build_screener_section(key, rows, ts))

    total_passed = sum(n_pass_per_profile.values())
    total_ranked = sum(len(rows) for rows in all_profile_rows.values())

    # Dynamic universe label — derived from unique tickers across all loaded CSVs
    total_unique_tickers = len({
        r.get("Ticker", "").strip()
        for rows in all_profile_rows.values()
        for r in rows
        if r.get("Ticker", "").strip()
    })
    n_active_profiles = sum(1 for rows in all_profile_rows.values() if rows)
    universe_label = f"Multi-Universe ({total_unique_tickers} tickers analyzed)"

    # ── Fetch 1-year OHLCV for Why-Buy charts ─────────────────────────────────
    # Collect unique tickers that will appear in Why-Buy panels (top-N per profile
    # + top overall). Limit to top 15 per profile sorted by ProfileFit to keep
    # build time reasonable (~0.3s × 40 tickers ≈ 12s extra).
    chart_tickers: set[str] = set()
    for rows in all_profile_rows.values():
        sorted_rows = sorted(rows, key=lambda r: _fv(r.get("ProfileFit","")) or 0, reverse=True)
        for r in sorted_rows[:15]:
            tkr = r.get("Ticker","").strip()
            if tkr:
                chart_tickers.add(tkr)

    if chart_tickers:
        print(f"  Fetching 1y OHLCV for {len(chart_tickers)} tickers (Why-Buy charts)…")
        _PRICE_DATA = _fetch_price_history(sorted(chart_tickers))
        print(f"  Fetched price history for {len(_PRICE_DATA)} tickers.")
    else:
        _PRICE_DATA = {}

    # Load score history for sparklines (stored in module-level var for access by _why_buy)
    global _SCORE_HISTORY_DATA
    all_tickers = list({
        r.get("Ticker", "").strip()
        for rows in all_profile_rows.values()
        for r in rows
        if r.get("Ticker", "").strip()
    })
    _SCORE_HISTORY_DATA = _load_score_history(all_tickers)

    # Dow 30
    dow_path = _most_recent("*_dow30_ranking.csv")
    dow_rows: list[dict] = []
    dow_ts = "—"
    if dow_path:
        dow_rows = _load_csv(dow_path)
        dow_ts = dow_path.stem[:15]
        try: dow_ts = datetime.strptime(dow_ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
        except Exception: pass

    # Magic Formula
    mf_path = _most_recent("*_magic_formula.csv")
    mf_rows: list[dict] = []
    mf_ts = "—"
    if mf_path:
        mf_rows = _load_csv(mf_path)
        mf_ts = mf_path.stem[:15]
        try: mf_ts = datetime.strptime(mf_ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
        except Exception: pass

    # Backtest
    bt_path = _most_recent("*_backtest_*.csv")
    bt_rows: list[dict] = []
    bt_ts = "—"
    if bt_path:
        bt_rows = _load_csv(bt_path)
        bt_ts = bt_path.stem[:15]
        try: bt_ts = datetime.strptime(bt_ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
        except Exception: pass

    # ── Dow 30 table (inline, simpler) ───────────────────────────────────────
    dow_section = ""
    if dow_rows:
        trows = ""
        for row in dow_rows:
            pos_v = _fv(row.get("52w Position%",""))
            pc    = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"
            gauge = (
                f'<div class="gauge-wrap">'
                f'<div class="gauge-track"><div class="gauge-fill" style="width:{min(pos_v,100):.1f}%;background:{pc}"></div></div>'
                f'<div class="gauge-pct" style="color:{pc}">{pos_v:.1f}%</div></div>'
                if pos_v is not None else "—"
            )
            mos_v = _fv(row.get("MoS%",""))
            mos_str = (
                f'<span style="color:{_mos_colour(mos_v)};font-weight:700">{mos_v:.1f}%</span>'
                if mos_v is not None else "—"
            )
            trows += f"""<tr>
              <td style="font-weight:800;color:#3b82d4">{row.get('Rank','')}</td>
              <td class="ticker-lbl">{row.get('Ticker','')}</td>
              <td class="company-lbl">{row.get('Company','')}</td>
              <td style="font-size:12px;color:#57606a">{row.get('Sector','')}</td>
              <td class="r">{_fmt(row.get('Price',''),2,prefix='$')}</td>
              <td style="min-width:140px">{gauge}</td>
              <td class="r">{_fmt(row.get('P/E',''),1,suffix='x')}</td>
              <td class="r">{_fmt(row.get('P/B',''),2,suffix='x')}</td>
              <td class="r">{mos_str}</td>
            </tr>"""

        dow_section = f"""
        <span class="section-anchor" id="dow30"></span>
        <details class="sec-wrap">
          <summary>
            <div class="sec-hdr">
              <span class="sec-arrow">&#9654;</span>
              <span class="sec-badge" style="background:#0891b218;color:#0891b2">D30</span>
              <span class="sec-title">Dow Jones 30 &mdash; 52-Week Ranking</span>
              <span class="sec-meta">{len(dow_rows)} companies &nbsp;·&nbsp; ranked by 52w position &nbsp;·&nbsp; {dow_ts}</span>
            </div>
          </summary>
          <div class="sec-body">
            <div style="font-size:13px;color:#57606a;margin-bottom:16px">
              All 30 blue-chip companies ranked by proximity to 52-week low.
              <strong style="color:#16a34a">Green</strong> = near annual low (best opportunity).
              <strong style="color:#e11d48">Red</strong> = near annual high.
            </div>
            <table class="stbl" style="table-layout:fixed">
              <thead><tr>
                <th style="width:5%">#</th>
                <th style="width:8%">Ticker</th>
                <th style="width:22%">Company</th>
                <th style="width:16%">Sector</th>
                <th class="r" style="width:8%">Price</th>
                <th style="width:16%">52w Position</th>
                <th class="r" style="width:7%">P/E</th>
                <th class="r" style="width:7%">P/B</th>
                <th class="r" style="width:11%">MoS%</th>
              </tr></thead>
              <tbody>{trows}</tbody>
            </table>
          </div>
        </details>"""

    bt_section_inner = _build_backtest_section(bt_rows, bt_ts) if bt_rows else ""
    if bt_rows:
        cagr_val = ""
        try:
            sr = next((r for r in bt_rows if r.get("Year","").upper()=="SUMMARY"), {})
            cp = _fv(sr.get("Portfolio%",""))
            cb = _fv(sr.get("Benchmark%",""))
            if cp is not None and cb is not None:
                sign = "+" if cp-cb >= 0 else ""
                cagr_val = f"{'+' if cp>=0 else ''}{cp:.1f}% portfolio &nbsp;·&nbsp; {sign}{cp-cb:.1f}% vs S&P 500"
        except Exception:
            pass
        bt_section = f"""
        <span class="section-anchor" id="backtest"></span>
        <details class="sec-wrap">
          <summary>
            <div class="sec-hdr">
              <span class="sec-arrow">&#9654;</span>
              <span class="sec-badge" style="background:#1f232818;color:#1f2328">BT</span>
              <span class="sec-title">Backtest vs S&amp;P 500 &mdash; Walk-Forward Simulation</span>
              <span class="sec-meta">{cagr_val} &nbsp;·&nbsp; {bt_ts}</span>
            </div>
          </summary>
          <div class="sec-body">{bt_section_inner}</div>
        </details>"""
    else:
        bt_section = ""

    # ── Overall Top (cross-profile) ───────────────────────────────────────────
    overall_top_section = _build_overall_top(all_profile_rows, top_n=10)

    # ── Top Convictions ───────────────────────────────────────────────────────
    convictions_section = _build_convictions_section(all_profile_rows)

    # ── Magic Formula section ─────────────────────────────────────────────────
    magic_formula_section = _build_magic_formula_section(mf_rows, mf_ts)

    # ── TOC ───────────────────────────────────────────────────────────────────
    toc_links  = '<a href="#overall_top">&#9650; Top Overall</a>'
    toc_links += '<a href="#convictions">&#9733; Top Convictions</a>'
    toc_links += "".join(
        f'<a href="#{k}">{_PROFILE_META[k]["label"]}</a>'
        for k in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value", "dividend_growth")
    )
    toc_links += f'<a href="#magic_formula">{_PROFILE_META["magic_formula"]["label"]}</a>'
    if dow_rows:   toc_links += '<a href="#dow30">Dow 30 Ranking</a>'
    if bt_rows:    toc_links += '<a href="#backtest">Backtest vs S&amp;P 500</a>'
    toc_links += '<a href="#methodology">Methodology</a>'

    # ── Methodology ───────────────────────────────────────────────────────────
    methodology = """
    <span class="section-anchor" id="methodology"></span>
    <details class="sec-wrap">
      <summary>
        <div class="sec-hdr">
          <span class="sec-arrow">&#9654;</span>
          <span class="sec-badge" style="background:#57606a18;color:#57606a">&#9881;</span>
          <span class="sec-title">How the Engine Works &mdash; Methodology</span>
          <span class="sec-meta">8 sections &nbsp;·&nbsp; data pipeline, valuation models, 5 screens + magic formula, quality scores, backtest</span>
        </div>
      </summary>
      <div class="sec-body">
      <div style="font-size:13px;color:#57606a;margin-bottom:16px">
        Every sub-section below is also collapsible. All descriptions reflect what is actually running in the code &mdash; no planned features included.
      </div>

      <!-- helper styles scoped to this section -->
      <style>
        .mdet { border:1px solid #e5e7eb; border-radius:10px; margin-bottom:10px; overflow:hidden; }
        .mdet summary {
          display:flex; align-items:center; gap:10px; padding:14px 18px;
          cursor:pointer; user-select:none; list-style:none;
          background:#f7f8fa; font-weight:700; font-size:13px; color:#1f2328;
          transition:background .15s;
        }
        .mdet summary::-webkit-details-marker { display:none; }
        .mdet summary:hover { background:#eff6ff; }
        .mdet[open] summary { background:#eff6ff; border-bottom:1px solid #e5e7eb; }
        .mdet[open] summary .marrow { transform:rotate(90deg); }
        .marrow { display:inline-block; transition:transform .2s; color:#3b82d4; font-size:14px; flex-shrink:0; }
        .mdet-icon { width:28px; height:28px; border-radius:6px; display:flex;
                     align-items:center; justify-content:center;
                     font-size:12px; font-weight:800; flex-shrink:0; }
        .mdet-body { padding:18px 20px; }
        .mdet table { width:100%; border-collapse:collapse; font-size:13px; }
        .mdet table th { padding:8px 12px; text-align:left; background:#f7f8fa;
                          border-bottom:2px solid #e5e7eb; font-size:11px;
                          text-transform:uppercase; letter-spacing:.05em; color:#57606a; }
        .mdet table td { padding:9px 12px; border-bottom:1px solid #f0f2f5; vertical-align:top; }
        .mdet table tr:last-child td { border-bottom:none; }
        .thresh { display:inline-block; font-family:monospace; font-size:12px; font-weight:700;
                  background:#1f232810; border-radius:4px; padding:1px 6px; }
        .thresh.pass { background:#dcfce7; color:#15803d; }
        .thresh.fail { background:#fef2f2; color:#dc2626; }
        .thresh.neutral { background:#eff6ff; color:#1d4ed8; }
        .lim-list { padding-left:18px; margin:0; }
        .lim-list li { margin-bottom:6px; line-height:1.6; }
      </style>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 1. DATA PIPELINE                                                   -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#3b82d418;color:#3b82d4">DB</span>
          1 &nbsp;&mdash;&nbsp; Data Pipeline &amp; Caching
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:12px;line-height:1.7">
            8 universes are supported: <strong>S&amp;P 500, NASDAQ-100, Dow Jones 30, Russell 2000,
            Euro Stoxx 50, BET Romania, World (global_tickers.csv), Custom CSV</strong>.
            The ticker list is fetched live by scraping Wikipedia (with hardcoded fallbacks).
            Running <code>python src/main.py</code> without <code>--profile</code> processes
            <strong>all 5 screener profiles + Magic Formula in a single run</strong> — data fetched once, reused for all profiles.
            Financial data is pulled from <strong>Yahoo Finance via <code>yfinance</code></strong>:
            current price, balance sheet, income statement, cash flow statement (3&ndash;5 years of history),
            beta, ROE, ROA, gross/operating margin, dividends, SBC, shares outstanding, and 52-week range.
            Every ticker goes through a <strong>retry loop with exponential backoff</strong> (3 attempts, ~1s delay doubling).
            Fetching runs concurrently via <code>ThreadPoolExecutor</code> with a configurable number of workers.
          </p>
          <table>
            <tr><th style="width:22%">What is cached</th><th>TTL policy</th></tr>
            <tr><td><strong>Prices, multiples, financials</strong></td><td><span class="thresh neutral">TTL = 0</span> &mdash; always re-fetched fresh from Yahoo Finance on every run. Stale prices would produce incorrect valuations.</td></tr>
            <tr><td><strong>Historical price series</strong></td><td><span class="thresh neutral">TTL = 1 day</span> &mdash; historical closes never change; one-day cache avoids redundant downloads during the backtest.</td></tr>
            <tr><td><strong>Cache storage</strong></td><td>Single DuckDB file at <code>data/cache.duckdb</code>. Thread-safe writes via a lock. A full re-run from a warm cache completes in ~8 seconds.</td></tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Limitation:</strong> Yahoo Finance is an unofficial, undocumented data source.
            Some tickers may return incomplete or stale data; the engine marks those as
            <code>INSUFFICIENT_DATA</code> and skips them gracefully.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 2. VALUATION MODELS                                                -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#059669 18;color:#059669">IV</span>
          2 &nbsp;&mdash;&nbsp; Valuation Models &mdash; How Intrinsic Value is Computed
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            Every company gets a per-share intrinsic value from two independent DCF models (or DDM for
            financial-sector companies). The final value is their arithmetic average.
            <strong>Margin of Safety = (Intrinsic &minus; Price) / Intrinsic &times; 100.</strong>
          </p>
          <table>
            <tr><th style="width:22%">Model</th><th>Logic</th><th style="width:18%">Used when</th></tr>
            <tr>
              <td><strong>DCF &mdash; Gordon Growth Model (GGM)</strong></td>
              <td>
                3&ndash;5 year average FCF projected 10 years at a per-company growth rate
                <em>g = ROE &times; Retention Ratio</em> (capped at WACC &minus; 1%, floored at 1%).
                Terminal value via perpetuity: <em>TV = FCF&#x2099; &times; (1+g) / (WACC &minus; g)</em>.
                Everything discounted at the per-company WACC.
                Result divided by diluted shares outstanding.
              </td>
              <td>All non-financial sectors where FCF &gt; 0</td>
            </tr>
            <tr>
              <td><strong>DCF &mdash; Exit Multiple</strong></td>
              <td>
                Average EBITDA projected 10 years at the same growth rate,
                multiplied by a <strong>12&times; EV/EBITDA exit multiple</strong> at year 10,
                net debt subtracted, result discounted at WACC, divided by shares.
                Acts as a cross-check on GGM &mdash; if both agree, conviction is higher.
              </td>
              <td>All non-financial sectors where EBITDA &gt; 0</td>
            </tr>
            <tr>
              <td><strong>Graham Number</strong></td>
              <td>
                &radic;(22.5 &times; EPS &times; Book Value per share). A conservative upper bound
                on fair value derived purely from accounting data — independent of DCF assumptions.
                Provides a third cross-check alongside GGM and Exit Multiple.
              </td>
              <td>All sectors where EPS &gt; 0 and BV &gt; 0</td>
            </tr>
            <tr>
              <td><strong>DDM &mdash; Dividend Discount</strong></td>
              <td>
                Gordon growth formula: <em>P = D&#x2081; / (r &minus; g)</em>
                where D&#x2081; = next-year dividend, r = WACC, g = sustainable dividend growth.
                Used because FCF and EBITDA from cash flow statements are unreliable for
                banks and insurers (capital tied up in regulatory reserves is not &ldquo;free&rdquo;).
              </td>
              <td>Financial Services &amp; Insurance sectors only</td>
            </tr>
            <tr>
              <td><strong>Dynamic WACC</strong></td>
              <td>
                Per-company: <em>WACC = Ke &times; (E/V) + Kd &times; (1&minus;t) &times; (D/V)</em>.
                Ke = risk-free rate (US 10Y ^TNX, live) + beta &times; 5.5% equity risk premium.
                Kd = interest expense / total debt. Fallback to 10% if data is missing.
              </td>
              <td>All models above</td>
            </tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Limitations:</strong>
            DCF is inherently sensitive to the growth rate assumption &mdash; a 1pp change in g
            can move intrinsic value by 15&ndash;30%.
            FCF figures from yfinance use the reported statement; capitalised R&amp;D or leases may distort results.
            The 12&times; exit multiple is fixed; cyclical sectors (Energy, Basic Materials)
            may warrant a lower multiple at trough.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 3. QUALITY SCORES                                                  -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#7c3aed18;color:#7c3aed">QS</span>
          3 &nbsp;&mdash;&nbsp; Quality Scores &mdash; Piotroski, Altman Z, ROIC, Composite
        </summary>
        <div class="mdet-body">
          <table>
            <tr><th style="width:22%">Score</th><th>How it is calculated</th><th style="width:22%">Thresholds &amp; flags</th></tr>
            <tr>
              <td><strong>Piotroski F-Score</strong></td>
              <td>
                9 binary (0/1) accounting signals summed to a score 0&ndash;9. Four profitability signals
                (positive ROA, positive operating cash flow, improving ROA YoY, CFO &gt; Net Income);
                three leverage/liquidity signals (falling long-term debt ratio, improving current ratio,
                no new share dilution); two efficiency signals (improving gross margin, improving asset
                turnover). Signals F6 and F7 are computed where data allows; otherwise capped at 7.
              </td>
              <td>
                <span class="thresh pass">&ge; 7 strong</span>
                <span class="thresh neutral">4&ndash;6 stable</span>
                <span class="thresh fail">&le; 3 weak</span>
              </td>
            </tr>
            <tr>
              <td><strong>Altman Z-Score</strong></td>
              <td>
                Z = 1.2&times;X&#x2081; + 1.4&times;X&#x2082; + 3.3&times;X&#x2083; + 0.6&times;X&#x2084; + 1.0&times;X&#x2085;
                (Working Capital/TA, Retained Earnings/TA, EBIT/TA, Market Cap/Total Debt, Revenue/TA).
                Threshold <strong>calibrated at 1.0</strong> instead of the textbook 1.81 to avoid
                over-excluding media and telecom companies with large intangible asset bases whose
                tangible asset ratios are structurally lower.
              </td>
              <td>
                <span class="thresh pass">&ge; 3.0 safe</span>
                <span class="thresh neutral">1.0&ndash;2.99 grey zone</span>
                <span class="thresh fail">&lt; 1.0 distress</span>
              </td>
            </tr>
            <tr>
              <td><strong>ROIC</strong></td>
              <td>
                ROIC = NOPAT / Invested Capital, where NOPAT = Operating Income &times; (1 &minus; tax rate)
                and Invested Capital = Total Equity + Total Debt &minus; Cash &amp; Equivalents.
                Measures how efficiently the business converts capital into profit regardless
                of financing structure.
              </td>
              <td>
                <span class="thresh pass">&ge; 15% moat</span>
                <span class="thresh neutral">10&ndash;14% good</span>
                <span class="thresh fail">&lt; 10% weak</span>
              </td>
            </tr>
            <tr>
              <td><strong>Beneish M-Score</strong></td>
              <td>
                8-index earnings-manipulation detector:
                DSRI (receivables), GMI (gross margin), AQI (asset quality), SGI (sales growth),
                DEPI (depreciation — set to neutral 1.0), SGAI (SG&amp;A — neutral 1.0),
                LVGI (leverage), TATA (accruals).
                <em>M = &minus;4.84 + 0.92&times;DSRI + 0.528&times;GMI + 0.404&times;AQI + 0.892&times;SGI
                + 0.115&times;DEPI &minus; 0.172&times;SGAI + 4.679&times;TATA &minus; 0.327&times;LVGI</em>
              </td>
              <td>
                <span class="thresh fail">M &gt; &minus;1.78 = MANIPULATION_RISK flag</span><br>
                <span class="thresh neutral">M &le; &minus;1.78 = likely clean</span>
              </td>
            </tr>
            <tr>
              <td><strong>SBC / Share Dilution</strong></td>
              <td>
                Stock-Based Compensation as % of Free Cash Flow (<code>SBC/FCF%</code>).
                SBC-adjusted FCF = FCF &minus; SBC (cash-equivalent real FCF).
                Share dilution % = YoY change in diluted shares outstanding.
                High SBC &gt; 30% of FCF indicates earnings may be partially illusory.
              </td>
              <td>
                <span class="thresh fail">SBC/FCF &gt; 30% = dilution alert</span><br>
                <span class="thresh neutral">Shares dilution &gt; 3%/yr = caution</span>
              </td>
            </tr>
            <tr>
              <td><strong>Sector-Relative Percentiles</strong></td>
              <td>
                P/E, P/FCF and EV/EBITDA of each company are ranked within its sector.
                A company in the 20th percentile is cheaper than 80% of sector peers,
                even if its absolute multiple looks elevated compared to the full index.
              </td>
              <td>Shown in Why-Buy panel; used in Top Overall cross-profile scoring</td>
            </tr>
            <tr>
              <td><strong>Score History Sparklines</strong></td>
              <td>
                Composite Score (0&ndash;100) is persisted in DuckDB <code>score_history</code>
                table on every run. The Why-Buy panel shows an SVG sparkline of the score
                evolution across runs — useful for identifying improving or deteriorating companies.
              </td>
              <td>DuckDB append-only; displayed inline in HTML report</td>
            </tr>
            <tr>
              <td><strong>Composite Score (0&ndash;100)</strong></td>
              <td>
                Weighted blend of four normalised signals:<br>
                &nbsp;&bull; <strong>Margin of Safety %</strong> &mdash; 40% weight<br>
                &nbsp;&bull; <strong>Piotroski F-Score</strong> &mdash; 25% weight<br>
                &nbsp;&bull; <strong>ROIC</strong> &mdash; 25% weight<br>
                &nbsp;&bull; <strong>52-week Position</strong> (inverted: lower = better) &mdash; 10% weight<br>
                Used internally for ranking and as the 30% quality component of ProfileFit.
              </td>
              <td>&mdash;</td>
            </tr>
          </table>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 4A. SCREEN: DEEP VALUE                                             -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#3b82d418;color:#3b82d4">DV</span>
          4A &nbsp;&mdash;&nbsp; Screen: Deep Value &mdash; the tightest six-filter pass
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            <strong>Philosophy:</strong> Benjamin Graham&ndash;style deep discount. A company must pass
            <em>all six criteria simultaneously</em> to receive a strict PASS.
            No single strong metric compensates for a failure in another.
            Designed to find statistically cheap stocks where the price alone offers a wide margin of safety.
          </p>
          <table>
            <tr><th style="width:28%">Criterion</th><th>Threshold</th><th>Rationale</th></tr>
            <tr><td><strong>P/E ratio</strong></td><td><span class="thresh pass">&le; 15&times;</span></td><td>Below long-run S&amp;P 500 average (&sim;22&times;). At &le;15&times; the market prices in meaningful pessimism about earnings.</td></tr>
            <tr><td><strong>P/B ratio</strong></td><td><span class="thresh pass">&le; 1.5&times;</span></td><td>Near or below book value. Classic Graham criterion: paying &le;1.5&times; assets provides downside protection.</td></tr>
            <tr><td><strong>EV/EBITDA</strong></td><td><span class="thresh pass">&le; 8&times;</span></td><td>An acquirer could recover the full enterprise value from operating profit in &le;8 years — historically cheap.</td></tr>
            <tr><td><strong>P/FCF</strong></td><td><span class="thresh pass">&le; 15&times;</span></td><td>Free cash flow yield &ge;6.7%. Real cash generation, not accounting earnings.</td></tr>
            <tr><td><strong>Net Debt / EBITDA</strong></td><td><span class="thresh pass">&le; 2.5&times;</span></td><td>Conservative leverage. Debt repayable from operating profit in &le;2.5 years.</td></tr>
            <tr><td><strong>Margin of Safety</strong></td><td><span class="thresh pass">&ge; 20%</span></td><td>DCF intrinsic value at least 20% above current price.</td></tr>
            <tr><td><strong>Piotroski F-Score</strong></td><td><span class="thresh pass">&ge; 4</span></td><td>Minimum accounting quality check. Prevents buying deteriorating businesses that happen to look statistically cheap.</td></tr>
            <tr><td><strong>Altman Z &lt; 1.0 excluded</strong></td><td><span class="thresh fail">Auto-exclude</span></td><td>Companies in real financial distress are removed even if multiples look cheap.</td></tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Sorted by:</strong> Margin of Safety % (descending).
            <strong>Typical pass rate:</strong> 1&ndash;5 companies from 503 tickers &mdash; the strictest screen in the system.
            <strong>Known limitation:</strong> Very cheap multiples in cyclical sectors (Energy, Materials)
            may reflect a trough in the cycle, not a permanent discount; combine with sector context.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 4B. SCREEN: BUFFETT QUALITY                                        -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#7c3aed18;color:#7c3aed">BQ</span>
          4B &nbsp;&mdash;&nbsp; Screen: Buffett Quality &mdash; wide-moat business at a fair price
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            <strong>Philosophy:</strong> &ldquo;It&rsquo;s far better to buy a wonderful company at a fair price
            than a fair company at a wonderful price.&rdquo; Focuses on <em>quality first</em>:
            strong return on invested capital, improving accounting fundamentals, low debt,
            and a DCF-confirmed discount. Multiples are more relaxed than Deep Value
            because the quality premium is justified.
          </p>
          <table>
            <tr><th style="width:28%">Criterion</th><th>Threshold</th><th>Rationale</th></tr>
            <tr><td><strong>P/E ratio</strong></td><td><span class="thresh neutral">&le; 25&times;</span></td><td>Allows paying a modest quality premium above the market average.</td></tr>
            <tr><td><strong>P/B ratio</strong></td><td><span class="thresh neutral">&le; 4&times;</span></td><td>High-ROIC businesses trade above book; 4&times; still filters out the most expensive growth names.</td></tr>
            <tr><td><strong>EV/EBITDA</strong></td><td><span class="thresh neutral">&le; 15&times;</span></td><td>Enterprise value check; still materially below the top quintile of the S&amp;P 500.</td></tr>
            <tr><td><strong>P/FCF</strong></td><td><span class="thresh neutral">&le; 25&times;</span></td><td>FCF yield &ge;4%. Ensures real cash generation even for quality businesses.</td></tr>
            <tr><td><strong>Net Debt / EBITDA</strong></td><td><span class="thresh pass">&le; 1.5&times;</span></td><td>Stricter than Deep Value &mdash; Buffett-style businesses should not need excessive leverage to generate returns.</td></tr>
            <tr><td><strong>Margin of Safety</strong></td><td><span class="thresh pass">&ge; 15%</span></td><td>Slightly relaxed vs Deep Value; quality businesses rarely trade at a 20%+ discount.</td></tr>
            <tr><td><strong>Piotroski F-Score</strong></td><td><span class="thresh pass">&ge; 5</span></td><td>Above-average accounting quality. Rising profitability and falling leverage.</td></tr>
            <tr><td><strong>ROIC</strong></td><td><span class="thresh pass">&ge; 10%</span></td><td>Must exceed typical cost of capital &mdash; the defining criterion separating Buffett-style from pure value.</td></tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Sorted by:</strong> Margin of Safety % (descending).
            <strong>Typical pass rate:</strong> 2&ndash;10 companies.
            <strong>Known limitation:</strong> ROIC data from yfinance may be missing for recent IPOs or
            companies undergoing restructuring; those are scored 0 on the ROIC criterion but not excluded.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 4C. SCREEN: HIGH FCF YIELD                                         -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#05966918;color:#059669">FCF</span>
          4C &nbsp;&mdash;&nbsp; Screen: High FCF Yield &mdash; maximum cash generation
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            <strong>Philosophy:</strong> Free cash flow is the most honest measure of a company&rsquo;s
            earning power because it is harder to manipulate than net income (no non-cash charges,
            no accruals). This screen finds companies where the cash yield on the market price is
            exceptionally high &mdash; meaning the business is generating so much cash that even at
            a conservative DCF valuation it still looks cheap. Multiples filters are intentionally
            broad to avoid excluding asset-light or high-depreciation businesses that score well on FCF.
          </p>
          <table>
            <tr><th style="width:28%">Criterion</th><th>Threshold</th><th>Rationale</th></tr>
            <tr><td><strong>P/FCF</strong></td><td><span class="thresh pass">&le; 12&times;</span></td><td>The core criterion. FCF yield &ge;8.3% &mdash; an exceptional cash return at any interest rate environment.</td></tr>
            <tr><td><strong>Margin of Safety</strong></td><td><span class="thresh pass">&ge; 10%</span></td><td>Minimum DCF confirmation that the cash yield is not just a one-year artefact.</td></tr>
            <tr><td><strong>P/E ratio</strong></td><td><span class="thresh neutral">&le; 30&times;</span></td><td>Guardrail only &mdash; broad enough not to exclude high-depreciation or capital-intensive companies.</td></tr>
            <tr><td><strong>EV/EBITDA</strong></td><td><span class="thresh neutral">&le; 20&times;</span></td><td>Broad ceiling to catch companies with unusual capital structures.</td></tr>
            <tr><td><strong>Net Debt / EBITDA</strong></td><td><span class="thresh neutral">&le; 3.0&times;</span></td><td>Slightly more lenient than other screens because strong FCF can service higher debt levels.</td></tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Sorted by:</strong> Margin of Safety % (descending).
            <strong>Typical pass rate:</strong> 8&ndash;20 companies.
            <strong>Known limitation:</strong> A single year of elevated FCF (asset sale, working capital
            release) can produce a deceptively low P/FCF. The engine uses a 3&ndash;5 year FCF average
            in the DCF, but the P/FCF multiple is based on trailing twelve months reported FCF.
            Always cross-check with the FCF trend over 3+ years.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 4D. SCREEN: QUALITY VALUE                                          -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#d9770618;color:#d97706">QV</span>
          4D &nbsp;&mdash;&nbsp; Screen: Quality Value &mdash; financially healthy at a discount
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            <strong>Philosophy:</strong> A balanced blend of quality and price discipline.
            The screen requires a demonstrably healthy balance sheet (Altman Z &ge; 1.0),
            above-average capital efficiency (ROIC &ge; 10%), improving fundamentals (Piotroski &ge; 5)
            <em>and</em> a clear DCF discount. It avoids the &ldquo;too cheap for a reason&rdquo; problem
            by requiring quality, and avoids overpaying by requiring a margin of safety.
            Results are sorted by Composite Score (not just MoS) to surface the best risk-adjusted picks.
          </p>
          <table>
            <tr><th style="width:28%">Criterion</th><th>Threshold</th><th>Rationale</th></tr>
            <tr><td><strong>P/E ratio</strong></td><td><span class="thresh neutral">&le; 25&times;</span></td><td>Moderate multiple ceiling. Allows quality businesses that trade above deep-value levels.</td></tr>
            <tr><td><strong>P/B ratio</strong></td><td><span class="thresh neutral">&le; 4&times;</span></td><td>Same as Buffett Quality &mdash; permits intangible-rich businesses.</td></tr>
            <tr><td><strong>EV/EBITDA</strong></td><td><span class="thresh neutral">&le; 15&times;</span></td><td>Enterprise value discipline; below the top third of the S&amp;P 500 by this metric.</td></tr>
            <tr><td><strong>P/FCF</strong></td><td><span class="thresh neutral">&le; 20&times;</span></td><td>FCF yield &ge;5%. Meaningful but not extreme cash yield required.</td></tr>
            <tr><td><strong>Net Debt / EBITDA</strong></td><td><span class="thresh pass">&le; 2.5&times;</span></td><td>Moderate leverage ceiling to keep balance sheet risk manageable.</td></tr>
            <tr><td><strong>Margin of Safety</strong></td><td><span class="thresh pass">&ge; 15%</span></td><td>DCF discount of at least 15% required to confirm valuation, not just quality.</td></tr>
            <tr><td><strong>Piotroski F-Score</strong></td><td><span class="thresh pass">&ge; 5</span></td><td>Above-average accounting quality. Fundamentals improving, not deteriorating.</td></tr>
            <tr><td><strong>ROIC</strong></td><td><span class="thresh pass">&ge; 10%</span></td><td>Capital efficiency above cost of capital. Confirms the business earns real returns.</td></tr>
            <tr><td><strong>Altman Z &lt; 1.0 excluded</strong></td><td><span class="thresh fail">Auto-exclude</span></td><td>Financial distress guard. Unlike Deep Value, Quality Value explicitly removes distressed names even if all other criteria pass.</td></tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Sorted by:</strong> Composite Score (descending) &mdash; not raw MoS%.
            This surfaces the best-balanced companies rather than just the most deeply discounted.
            <strong>Typical pass rate:</strong> 2&ndash;8 companies.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 4E. SCREEN: DIVIDEND GROWTH                                        -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#0891b218;color:#0891b2">DIV</span>
          4E &nbsp;&mdash;&nbsp; Screen: Dividend Growth &mdash; sustainable income investing
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            <strong>Philosophy:</strong> Income-focused. Targets companies that pay a meaningful dividend,
            cover it comfortably with free cash flow, carry low debt, and show no signs of earnings manipulation.
            The FCF payout ratio is the key metric &mdash; it confirms dividends are funded by real cash, not debt.
          </p>
          <table>
            <tr><th style="width:28%">Criterion</th><th>Threshold</th><th>Rationale</th></tr>
            <tr><td><strong>Dividend Yield</strong></td><td><span class="thresh pass">&ge; 2.5%</span></td><td>Minimum meaningful income yield. Screens out nominal dividend payers.</td></tr>
            <tr><td><strong>FCF Payout Ratio</strong></td><td><span class="thresh pass">&le; 70%</span></td><td>Dividend covered by real free cash flow with a 30% buffer — sustainable.</td></tr>
            <tr><td><strong>Net Debt / EBITDA</strong></td><td><span class="thresh pass">&le; 2.0&times;</span></td><td>Conservative leverage — overleveraged companies cut dividends first.</td></tr>
            <tr><td><strong>Piotroski F-Score</strong></td><td><span class="thresh pass">&ge; 5</span></td><td>Fundamentals improving, not deteriorating. Reduces dividend-cut risk.</td></tr>
            <tr><td><strong>Beneish M-Score flag</strong></td><td><span class="thresh fail">Exclude MANIPULATION_RISK</span></td><td>Earnings manipulation is a leading indicator of dividend cuts and restatements.</td></tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>Typical pass rate:</strong> 10&ndash;30 companies from S&amp;P 500.
            <strong>Note:</strong> Dividend yield data from Yahoo Finance may lag ex-dividend dates by 1&ndash;2 days.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 4F. MAGIC FORMULA (GREENBLATT)                                     -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#be185d18;color:#be185d">MF</span>
          4F &nbsp;&mdash;&nbsp; Magic Formula (Greenblatt) &mdash; cheap &amp; good simultaneously
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            <strong>Philosophy:</strong> Joel Greenblatt&rsquo;s <em>The Little Book That Beats the Market</em>
            (2005). Rank all companies by two criteria simultaneously: <strong>Earnings Yield (E/P)</strong>
            (cheap) and <strong>ROIC</strong> (good). Add the two ranks. The company with the lowest
            combined rank is the best combination of cheap <em>and</em> high-quality.
          </p>
          <table>
            <tr><th style="width:28%">Criterion</th><th>How it works</th></tr>
            <tr>
              <td><strong>Earnings Yield rank</strong></td>
              <td>EY = 1 / P/E (E/P proxy). All eligible companies ranked descending — rank #1 = highest earnings yield = cheapest.</td>
            </tr>
            <tr>
              <td><strong>ROIC rank</strong></td>
              <td>All eligible companies ranked descending by ROIC — rank #1 = highest ROIC = best quality.</td>
            </tr>
            <tr>
              <td><strong>Magic Score</strong></td>
              <td>EY_rank + ROIC_rank. Lower = better. Top 30 displayed.</td>
            </tr>
            <tr>
              <td><strong>Excluded sectors</strong></td>
              <td>Financials, Financial Services, Utilities, Real Estate (valuation metrics not comparable to operating companies).</td>
            </tr>
          </table>
          <p style="margin-top:12px;color:#6b7280;font-size:12px">
            <strong>No strict PASS/FAIL:</strong> Magic Formula is a pure ranking — every non-excluded company with P/E &gt; 0 and valid ROIC is ranked.
            Typical eligible pool: 250&ndash;350 companies from S&amp;P 500 after sector exclusions.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 5. PROFILEFIT & RANKING                                            -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#0891b218;color:#0891b2">PF</span>
          5 &nbsp;&mdash;&nbsp; ProfileFit Score &amp; Full-Universe Ranking
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            Rather than a binary pass/fail, every company receives a <strong>ProfileFit Score
            (0&ndash;100)</strong> per profile. This allows ranking all 503 companies,
            not just the handful that strictly pass. Companies that almost pass are visible and
            comparable to those that do pass.
          </p>
          <table>
            <tr><th style="width:28%">Component</th><th>Weight</th><th>How it is computed</th></tr>
            <tr>
              <td><strong>Criterion proximity score</strong></td>
              <td>70%</td>
              <td>
                For each threshold in the profile, a score 0&ndash;1 is given:
                1.0 = at or better than threshold; partial credit for being close
                (e.g. if threshold is P/E &le;15 and actual P/E is 20, score = 15/20 = 0.75).
                The average across all profile criteria gives the proximity score.
              </td>
            </tr>
            <tr>
              <td><strong>Composite Score</strong></td>
              <td>30%</td>
              <td>The 0&ndash;100 quality-adjusted ranking score described in section 3.</td>
            </tr>
          </table>
          <p style="margin-top:12px;line-height:1.7">
            <strong style="color:#16a34a">PASS</strong> = meets every criterion in the profile strictly.<br>
            <strong style="color:#9ca3af">NEAR</strong> = misses one or more criteria but still has a meaningful ProfileFit score.<br>
            <strong style="color:#dc2626">TRAP</strong> = Value Trap flag active: Net Debt/EBITDA &gt; 3.5&times; <em>or</em> all FCF years are negative.<br>
            <code>INSUFFICIENT_DATA</code> = fewer than 2 financial data points available; always placed last.
          </p>
          <p style="margin-top:10px;color:#6b7280;font-size:12px">
            The <strong>Top Overall</strong> section aggregates ProfileFit scores across all 5 profiles
            using a weighted average (Deep Value &times;1.3, Buffett Quality &times;1.2,
            Quality Value &times;1.1, Dividend Growth &times;1.05, FCF Yield &times;1.0).
            <strong>Top Convictions</strong> shows companies that pass 2+ profiles strictly —
            with GOLD/HIGH/MODERATE conviction levels scaled dynamically to the number of profiles with data.
          </p>
        </div>
      </details>

      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <!-- 6. BACKTEST                                                        -->
      <!-- ═══════════════════════════════════════════════════════════════════ -->
      <details class="mdet">
        <summary>
          <span class="marrow">&#9654;</span>
          <span class="mdet-icon" style="background:#1f232818;color:#1f2328">BT</span>
          6 &nbsp;&mdash;&nbsp; Walk-Forward Backtest vs S&amp;P 500
        </summary>
        <div class="mdet-body">
          <p style="margin-bottom:14px;line-height:1.7">
            Annual simulation of the Deep Value screen applied to historical data.
            Top-N picks bought on the <strong>first trading day of each year</strong>,
            held for exactly one year, sold on the first trading day of the following year.
            Portfolio is <strong>equal-weighted</strong>.
            Benchmark is <strong>^GSPC (S&amp;P 500 total price return)</strong>.
          </p>
          <table>
            <tr><th style="width:28%">Metric</th><th>How it is computed</th></tr>
            <tr><td><strong>CAGR</strong></td><td>Compound Annual Growth Rate of a $1 investment over the full backtest period.</td></tr>
            <tr><td><strong>Sharpe Ratio</strong></td><td>(Mean annual return &minus; risk-free rate) / standard deviation of annual returns. Risk-free rate = US 10Y yield at the time.</td></tr>
            <tr><td><strong>Sortino Ratio</strong></td><td>Same as Sharpe but uses only downside deviation in the denominator. Penalises losses more than gains.</td></tr>
            <tr><td><strong>Max Drawdown</strong></td><td>Largest peak-to-trough loss of the cumulative portfolio value across all years.</td></tr>
            <tr><td><strong>Win Rate</strong></td><td>Percentage of individual stock picks that outperformed the S&amp;P 500 in their holding year.</td></tr>
            <tr><td><strong>Excess Return</strong></td><td>Portfolio return minus S&amp;P 500 return for each year, and CAGR difference over the full period.</td></tr>
          </table>
          <div class="ib" style="background:#fffbeb;border:1px solid #fde68a;color:#92400e;margin-top:14px;margin-bottom:0">
            <strong>Critical limitations &mdash; read before drawing conclusions:</strong>
            <ul class="lim-list" style="margin-top:8px">
              <li><strong>Look-ahead bias:</strong> screening criteria use <em>current</em> financial data for all historical years. A company that passes the screen today may not have passed in 2018 with its then-current financials. Results are optimistic.</li>
              <li><strong>Survivorship bias:</strong> universe = current S&amp;P 500 constituents only. Companies that failed, were acquired, or were removed from the index since the start year are absent. This overstates historical returns.</li>
              <li><strong>Single entry/exit date:</strong> one buy and one sell date per year, no dollar-cost averaging, no intra-year rebalancing.</li>
              <li><strong>No costs:</strong> commissions, bid-ask spread, market impact, taxes, and short-selling costs are not modelled.</li>
              <li><strong>Interpretation:</strong> treat as a directional indicator of whether the strategy has historically identified above-average companies &mdash; not as a reliable forecast of future returns.</li>
            </ul>
          </div>
        </div>
      </details>

    </div>
    </div>
    </details>"""

    # ── Assemble full HTML ─────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Screener — Full Executive Report</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">

  <div class="report-header">
    <div style="font-size:11px;color:#9ca3af;letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px">
      Stock Screener &amp; Intrinsic Value Engine — v3 Full Report
    </div>
    <h1>Executive Summary Report</h1>
    <div class="subtitle">{universe_label} &nbsp;·&nbsp; {n_active_profiles} Screener Profiles &nbsp;·&nbsp;
      Magic Formula &nbsp;·&nbsp; Dow Jones 30 &nbsp;·&nbsp; Walk-Forward Backtest vs S&amp;P 500</div>
    <div class="header-meta">
      <div class="hm-item"><div class="hm-label">Generated</div><div class="hm-value">{now}</div></div>
      <div class="hm-item"><div class="hm-label">Universe</div><div class="hm-value">{universe_label}</div></div>
      <div class="hm-item"><div class="hm-label">Strict Passed</div><div class="hm-value">{total_passed} (across {n_active_profiles} profiles)</div></div>
      <div class="hm-item"><div class="hm-label">Ranked Total</div><div class="hm-value">{total_ranked} rows</div></div>
      <div class="hm-item"><div class="hm-label">Benchmark</div><div class="hm-value">^GSPC (S&amp;P 500)</div></div>
      <div class="hm-item"><div class="hm-label">Data Source</div><div class="hm-value">Yahoo Finance / yfinance</div></div>
      <div class="hm-item"><div class="hm-label">Storage</div><div class="hm-value">DuckDB (local cache)</div></div>
    </div>
  </div>

  <div class="disclaimer">
    <strong>Important Disclaimer:</strong> Generated by an automated quantitative algorithm for
    <strong>informational and educational purposes only</strong>. This does <strong>not</strong>
    constitute financial advice or a solicitation to buy or sell any security.
    Always conduct your own due diligence before making any investment decision.
  </div>

  <div class="toc">
    <div class="toc-title">Jump to section</div>
    {toc_links}
  </div>

  <!-- Watchlist section — populated by localStorage on load (ST13) -->
  <div id="watchlist-section" class="watchlist-section" style="display:none">
    <div class="watchlist-header">
      <span style="font-size:20px">⭐</span>
      <span class="watchlist-title">My Watchlist</span>
      <span class="watchlist-subtitle" id="wl-count">0 companies saved</span>
      <button class="wl-export-btn" onclick="exportWatchlistCSV()" style="margin-left:12px">⬇ Export CSV</button>
    </div>
    <div id="watchlist-body">
      <div class="watchlist-empty">No companies starred yet. Click ☆ on any row to add.</div>
    </div>
  </div>

  {overall_top_section}
  {convictions_section}
  {''.join(profile_sections)}
  {magic_formula_section}
  {dow_section}
  {bt_section}
  {methodology}

  <div class="footer">
    <p>Generated by <strong>Stock Screener &amp; Intrinsic Value Engine v2</strong>
      &nbsp;·&nbsp; Python 3.11+ &nbsp;·&nbsp; yfinance &nbsp;·&nbsp; DuckDB &nbsp;·&nbsp; 100% local execution</p>
    <p style="margin-top:6px">Report generated: {now}</p>
    <p style="margin-top:6px;color:#c0c4cb">For informational and educational purposes only. Not financial advice.</p>
    <p style="margin-top:12px;padding-top:12px;border-top:1px solid #e5e7eb">Made with IBM Bob</p>
  </div>

</div>

<script>
/* ── Why-Buy row toggle ───────────────────────────────────────────────────── */
function toggleWhy(id) {{
  var row = document.getElementById(id);
  var btn = document.getElementById('btn-' + id);
  var arr = document.getElementById('arr-' + id);
  if (!row) return;
  var isOpen = row.style.display !== 'none';
  document.querySelectorAll('.why-row').forEach(function(r) {{
    if (r !== row && r.style.display !== 'none') {{
      r.style.display = 'none';
      var otherId = r.id;
      var otherBtn = document.getElementById('btn-' + otherId);
      var otherArr = document.getElementById('arr-' + otherId);
      if (otherBtn) otherBtn.classList.remove('open');
      if (otherArr) otherArr.classList.remove('open');
    }}
  }});
  if (isOpen) {{
    row.style.display = 'none';
    if (btn) btn.classList.remove('open');
    if (arr) arr.classList.remove('open');
  }} else {{
    row.style.display = '';
    if (btn) btn.classList.add('open');
    if (arr) arr.classList.add('open');
    setTimeout(function() {{ row.scrollIntoView({{behavior:'smooth', block:'nearest'}}); }}, 50);
  }}
}}

/* ── Live Filter Bar (ST12) ──────────────────────────────────────────────── */
function applyFilter(sectionId) {{
  var filterBar = document.getElementById('filter-' + sectionId);
  var table     = document.getElementById('tbl-' + sectionId);
  var counter   = document.getElementById('fc-' + sectionId);
  if (!filterBar || !table) return;

  var inputs    = filterBar.querySelectorAll('input[type=number]');
  var selSector = filterBar.querySelector('select');
  var minMos    = inputs[0] && inputs[0].value !== '' ? parseFloat(inputs[0].value) : null;
  var maxPe     = inputs[1] && inputs[1].value !== '' ? parseFloat(inputs[1].value) : null;
  var maxPfcf   = inputs[2] && inputs[2].value !== '' ? parseFloat(inputs[2].value) : null;
  var minPio    = inputs[3] && inputs[3].value !== '' ? parseFloat(inputs[3].value) : null;
  var sector    = selSector ? selSector.value : '';

  var rows   = table.querySelectorAll('tbody tr[data-sector]');
  var shown  = 0;
  var total  = 0;
  rows.forEach(function(tr) {{
    total++;
    var rSector = tr.getAttribute('data-sector') || '';
    var rMos    = parseFloat(tr.getAttribute('data-mos')  || '0');
    var rPe     = parseFloat(tr.getAttribute('data-pe')   || '999');
    var rPfcf   = parseFloat(tr.getAttribute('data-pfcf') || '999');
    var rPio    = parseFloat(tr.getAttribute('data-piotroski') || '0');
    var visible = true;
    if (sector  && rSector !== sector)           visible = false;
    if (minMos  !== null && rMos  < minMos)      visible = false;
    if (maxPe   !== null && rPe   > maxPe)       visible = false;
    if (maxPfcf !== null && rPfcf > maxPfcf)     visible = false;
    if (minPio  !== null && rPio  < minPio)      visible = false;
    tr.style.display = visible ? '' : 'none';
    /* also hide/show the adjacent why-row if present */
    var next = tr.nextElementSibling;
    if (next && next.classList.contains('why-row')) next.style.display = 'none';
    if (visible) shown++;
  }});
  if (counter) counter.textContent = 'Showing ' + shown + ' of ' + total;
}}

function resetFilter(sectionId) {{
  var filterBar = document.getElementById('filter-' + sectionId);
  if (!filterBar) return;
  filterBar.querySelectorAll('input[type=number]').forEach(function(el) {{ el.value = ''; }});
  var sel = filterBar.querySelector('select');
  if (sel) sel.value = '';
  applyFilter(sectionId);
}}

function initFilter(sectionId) {{
  var table    = document.getElementById('tbl-' + sectionId);
  var filterBar = document.getElementById('filter-' + sectionId);
  if (!table || !filterBar) return;
  /* populate sector dropdown from table rows */
  var sel     = filterBar.querySelector('select');
  var sectors = {{}};
  table.querySelectorAll('tbody tr[data-sector]').forEach(function(tr) {{
    var s = tr.getAttribute('data-sector');
    if (s) sectors[s] = true;
  }});
  Object.keys(sectors).sort().forEach(function(s) {{
    var opt = document.createElement('option');
    opt.value = s; opt.textContent = s;
    sel.appendChild(opt);
  }});
  /* show total count */
  var counter = document.getElementById('fc-' + sectionId);
  var total   = table.querySelectorAll('tbody tr[data-sector]').length;
  if (counter) counter.textContent = 'Showing ' + total + ' of ' + total;
}}

/* ── Watchlist + localStorage (ST13) ────────────────────────────────────── */
var WL_KEY = 'uv_watchlist';

function _getWatchlist() {{
  try {{ return JSON.parse(localStorage.getItem(WL_KEY) || '[]'); }}
  catch(e) {{ return []; }}
}}
function _saveWatchlist(arr) {{
  localStorage.setItem(WL_KEY, JSON.stringify(arr));
}}

function toggleStar(btn) {{
  var ticker  = btn.getAttribute('data-ticker');
  if (!ticker) return;
  var rawJson = btn.getAttribute('data-row-json') || '{{}}';
  var rowData;
  try {{ rowData = JSON.parse(rawJson); }} catch(e) {{ rowData = {{ticker: ticker}}; }}
  var wl = _getWatchlist();
  var idx = wl.findIndex(function(x) {{ return x.ticker === ticker; }});
  if (idx >= 0) {{
    wl.splice(idx, 1);
    btn.textContent = '☆';
    btn.classList.remove('starred');
    btn.title = 'Add to watchlist';
  }} else {{
    wl.push(rowData);
    btn.textContent = '⭐';
    btn.classList.add('starred');
    btn.title = 'Remove from watchlist';
  }}
  _saveWatchlist(wl);
  renderWatchlist();
}}

function renderWatchlist() {{
  var wl      = _getWatchlist();
  var section = document.getElementById('watchlist-section');
  var body    = document.getElementById('watchlist-body');
  var count   = document.getElementById('wl-count');
  if (!section) return;
  if (count) count.textContent = wl.length + ' compan' + (wl.length === 1 ? 'y' : 'ies') + ' saved';
  if (wl.length === 0) {{
    section.style.display = 'none';
    if (body) body.innerHTML = '<div class="watchlist-empty">No companies starred yet. Click ☆ on any row to add.</div>';
    return;
  }}
  section.style.display = '';
  var html = '<table class="watchlist-tbl"><thead><tr>'
    + '<th>Ticker</th><th>Company</th><th>Sector</th>'
    + '<th class="r">Price</th><th class="r">MoS%</th>'
    + '<th class="r">P/E</th><th class="r">P/FCF</th>'
    + '<th class="r">Piotroski</th><th class="r">Fit</th><th></th>'
    + '</tr></thead><tbody>';
  wl.forEach(function(d) {{
    var mosColor = (d.mos >= 30) ? '#16a34a' : (d.mos >= 15 ? '#eab308' : '#e11d48');
    html += '<tr>'
      + '<td><strong>' + (d.ticker||'') + '</strong></td>'
      + '<td style="font-size:11px;color:#57606a">' + (d.company||'') + '</td>'
      + '<td style="font-size:11px;color:#9ca3af">' + (d.sector||'') + '</td>'
      + '<td class="r">' + (d.price != null ? '$' + d.price.toFixed(2) : '—') + '</td>'
      + '<td class="r" style="color:' + mosColor + ';font-weight:700">' + (d.mos != null ? d.mos.toFixed(1)+'%' : '—') + '</td>'
      + '<td class="r">' + (d.pe != null ? d.pe.toFixed(1)+'x' : '—') + '</td>'
      + '<td class="r">' + (d.pfcf != null ? d.pfcf.toFixed(1)+'x' : '—') + '</td>'
      + '<td class="r">' + (d.piotroski != null ? d.piotroski+'/9' : '—') + '</td>'
      + '<td class="r">' + (d.fit != null ? d.fit.toFixed(0) : '—') + '</td>'
      + '<td><button class="star-btn starred" data-ticker="' + d.ticker + '" data-row-json=""'
      + ' onclick="removeFromWatchlist(\'' + d.ticker + '\')">⭐</button></td>'
      + '</tr>';
  }});
  html += '</tbody></table>';
  if (body) body.innerHTML = html;
}}

function removeFromWatchlist(ticker) {{
  var wl  = _getWatchlist().filter(function(x) {{ return x.ticker !== ticker; }});
  _saveWatchlist(wl);
  /* update star buttons in main tables */
  document.querySelectorAll('.star-btn[data-ticker="' + ticker + '"]').forEach(function(btn) {{
    btn.textContent = '☆';
    btn.classList.remove('starred');
    btn.title = 'Add to watchlist';
  }});
  renderWatchlist();
}}

function exportWatchlistCSV() {{
  var wl = _getWatchlist();
  if (wl.length === 0) return;
  var header = ['ticker','company','sector','price','mos','pe','pfcf','piotroski','fit'];
  var rows   = wl.map(function(d) {{
    return header.map(function(k) {{
      var v = d[k];
      if (v == null) return '';
      var s = String(v);
      return s.indexOf(',') >= 0 ? '"' + s + '"' : s;
    }}).join(',');
  }});
  var csv = header.join(',') + '\n' + rows.join('\n');
  var blob = new Blob([csv], {{type:'text/csv'}});
  var url  = URL.createObjectURL(blob);
  var a    = document.createElement('a');
  a.href   = url; a.download = 'watchlist.csv';
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}

function loadWatchlist() {{
  /* Restore star button states from localStorage */
  var wl = _getWatchlist();
  var inWl = {{}};
  wl.forEach(function(d) {{ if (d.ticker) inWl[d.ticker] = true; }});
  document.querySelectorAll('.star-btn[data-ticker]').forEach(function(btn) {{
    var t = btn.getAttribute('data-ticker');
    if (inWl[t]) {{
      btn.textContent = '⭐';
      btn.classList.add('starred');
      btn.title = 'Remove from watchlist';
    }}
  }});
  renderWatchlist();
}}

/* ── Initialise on DOMContentLoaded ─────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', function() {{
  /* Init all filter bars */
  ['deep_value-top','deep_value-rest',
   'buffett_quality-top','buffett_quality-rest',
   'high_fcf_yield-top','high_fcf_yield-rest',
   'quality_value-top','quality_value-rest',
   'dividend_growth-top','dividend_growth-rest'
  ].forEach(function(sid) {{ initFilter(sid); }});
  /* Restore watchlist stars */
  loadWatchlist();
}});
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    print(f"Full report saved: {out_path}")
    print(f"  Strict passes — "
          + ", ".join(f"{k}: {n_pass_per_profile.get(k,0)}" for k in n_pass_per_profile))
    print(f"  Total ranked rows: {total_ranked}")
    print(f"  Report size: {out_path.stat().st_size / 1024:.0f} KB")

    # ── Publish to docs/index.html for GitHub Pages ───────────────────────────
    docs_dir  = Path(__file__).parent.parent / "docs"
    docs_path = docs_dir / "index.html"
    wrote_ok  = False
    try:
        docs_dir.mkdir(parents=True, exist_ok=True)
        docs_path.write_text(html, encoding="utf-8")
        print(f"  GitHub Pages copy: {docs_path}")
        wrote_ok = True
    except Exception as exc:
        print(f"  [warn] Could not write docs/index.html: {exc}")

    # ── Auto git add + commit + push ──────────────────────────────────────────
    if wrote_ok:
        _git_push_pages(docs_path, now)


def _git_push_pages(docs_path: Path, run_ts: str) -> None:
    """git add docs/index.html → commit → push to main (reads token from .github_credentials)."""
    import subprocess

    repo_root   = Path(__file__).parent.parent
    creds_file  = repo_root / ".github_credentials"

    # Read token
    token = None
    if creds_file.exists():
        for line in creds_file.read_text().splitlines():
            if line.startswith("GITHUB_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break

    def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=str(repo_root),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            **kw
        )

    try:
        # stage only docs/index.html
        _run(["git", "add", "docs/index.html"])

        # check if there's anything to commit
        status = _run(["git", "status", "--porcelain", "docs/index.html"])
        if not status.stdout.strip():
            print("  GitHub Pages: no changes to push.")
            return

        # commit
        commit_msg = f"chore: update GitHub Pages report [{run_ts}]"
        _run(["git", "commit", "-m", commit_msg])

        # set remote with token, push, restore clean remote
        if token:
            _run(["git", "remote", "set-url", "origin",
                  f"https://{token}@github.com/um01932/undervalued-stocks.git"])

        push = _run(["git", "push", "origin", "main"])

        if token:
            _run(["git", "remote", "set-url", "origin",
                  "https://github.com/um01932/undervalued-stocks.git"])

        if push.returncode == 0:
            print("  GitHub Pages pushed -> https://um01932.github.io/undervalued-stocks/")
        else:
            print(f"  [warn] git push failed: {push.stderr.strip()[:200]}")

    except Exception as exc:
        print(f"  [warn] Auto-push failed: {exc}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate consolidated HTML executive report")
    parser.add_argument("--out", default=None, help="Output HTML path (default: data/reports/full_report.html)")
    args = parser.parse_args()

    out = Path(args.out) if args.out else REPORTS_DIR / "full_report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    build_full_report(out)


if __name__ == "__main__":
    main()
