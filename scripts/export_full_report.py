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

REPORTS_DIR  = Path(__file__).parent.parent / "data" / "reports"
DB_PATH      = Path(__file__).parent.parent / "data" / "cache.duckdb"

_BT_START    = 2019                    # backtest start year (fixed)
_BT_END      = datetime.now().year     # backtest end year  (dynamic — always current year)
_BT_RANGE    = f"{_BT_START}–{_BT_END}"  # e.g. "2019–2026"

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
    """Score breakdown cards: MoS, Piotroski, ROIC, FCF Growth, Op.Margin + composite/overall badges."""
    def _norm(v, lo, hi):
        if v is None: return 0.0
        return min(max((v - lo) / (hi - lo), 0.0), 1.0)

    mos_v    = _fv(row.get("MoS%", ""))
    pio_v    = _fv(row.get("Piotroski", ""))
    roic_v   = _fv(row.get("ROIC%", ""))
    fcfg_v   = _fv(row.get("FCF Growth 3yr%", ""))
    opm_v    = _fv(row.get("Op.Margin%", ""))
    dil_v    = _fv(row.get("Dilution%", ""))
    comp_v   = _fv(row.get("Score", ""))

    mos_n    = _norm(mos_v,   0, 60)
    pio_n    = _norm(pio_v,   0, 9)
    roic_n   = _norm(roic_v,  0, 30)
    fcfg_n   = _norm(fcfg_v,  0, 30)
    opm_n    = _norm(opm_v,   0, 30)

    def _bar(n, colour):
        w = round(n * 80, 1)
        return (
            f'<svg width="80" height="7" style="display:block;margin-top:4px">'
            f'<rect width="80" height="7" fill="#e5e7eb" rx="3"/>'
            f'<rect width="{w}" height="7" fill="{colour}" rx="3"/>'
            f'</svg>'
        )

    def _card(label, val_str, norm, colour, weight, contrib):
        return f"""<div style="flex:1;min-width:90px;background:#f9fafb;border:1px solid #e5e7eb;
                               border-radius:8px;padding:10px 12px;text-align:center">
          <div style="font-size:10px;color:#8d96a0;text-transform:uppercase;letter-spacing:.05em;
                      margin-bottom:4px">{label}</div>
          <div style="font-size:18px;font-weight:900;color:{colour};line-height:1">{val_str}</div>
          {_bar(norm, colour)}
          <div style="font-size:10px;color:#8d96a0;margin-top:4px">wt {weight} → <strong style="color:#374151">{contrib:.1f}pts</strong></div>
        </div>"""

    # Dilution badge (penalty, shown only if diluting)
    dil_badge = ""
    if dil_v is not None and dil_v > 0:
        pen = min(dil_v / 5.0, 1.0) * 4
        dil_badge = (
            f'<div style="flex:1;min-width:90px;background:#fef2f2;border:1px solid #fecaca;'
            f'border-radius:8px;padding:10px 12px;text-align:center">'
            f'<div style="font-size:10px;color:#b91c1c;text-transform:uppercase;letter-spacing:.05em;'
            f'margin-bottom:4px">Dilution</div>'
            f'<div style="font-size:18px;font-weight:900;color:#e11d48;line-height:1">+{dil_v:.1f}%</div>'
            f'<div style="font-size:10px;color:#b91c1c;margin-top:8px">penalty −{pen:.1f}pts</div>'
            f'</div>'
        )

    cards = (
        _card("Margin of Safety",
              f"{mos_v:.1f}%" if mos_v is not None else "—",
              mos_n, "#3b82d4", "28%", mos_n * 28) +
        _card("Piotroski F-Score",
              f"{pio_v:.0f}/9" if pio_v is not None else "—",
              pio_n, "#7c3aed", "24%", pio_n * 24) +
        _card("ROIC",
              f"{roic_v:.1f}%" if roic_v is not None else "—",
              roic_n, "#059669", "24%", roic_n * 24) +
        _card("FCF Growth 3yr",
              f"{fcfg_v:+.0f}%" if fcfg_v is not None else "—",
              fcfg_n, "#0891b2", "8%", fcfg_n * 8) +
        _card("Op. Margin",
              f"{opm_v:.1f}%" if opm_v is not None else "—",
              opm_n, "#d97706", "4%", opm_n * 4) +
        dil_badge
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
        <span style="font-weight:400;color:#c0c4cb"> — 7 pillars: MoS (28pts) + FCF Yield (24pts) + Piotroski (24pts) + ROIC (10pts) + Op.Margin (8pts) + FCF Growth (4pts) − Dilution penalty (−4pts max)</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">{cards}</div>
      <div style="margin-top:4px">{totals}</div>
    </div>"""


def _why_buy(row: dict, profile_key: str | None = None,
             profiles: list[str] | None = None,
             overall_score: float | None = None,
             ohlc: list[dict] | None = None,
             score_history: list[float] | None = None,
             profile_fits: dict | None = None) -> str:
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
            f"This is unusual for a <strong>{_index_label(ticker)}</strong> company and historically associated with above-average future returns."
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
            f"screened companies to simultaneously pass {profile_names} — "
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

    # ── Overall Score breakdown section ──────────────────────────────────────
    overall_breakdown_html = ""
    if profile_fits and overall_score is not None:
        _weights = {
            "deep_value":      1.30,
            "buffett_quality": 1.20,
            "quality_value":   1.10,
            "dividend_growth": 1.05,
            "high_fcf_yield":  1.00,
        }
        _w_sum = sum(_weights.get(k, 1.0) for k in profile_fits)
        _rows = ""
        for pk in ("deep_value", "buffett_quality", "quality_value", "dividend_growth", "high_fcf_yield",
                   "net_net", "momentum_quality", "contrarian"):
            if pk not in profile_fits:
                continue
            fit    = profile_fits[pk]
            w      = _weights.get(pk, 1.0)
            contrib = fit * w / _w_sum if _w_sum else 0.0
            info   = _PROFILE_LABEL_SHORT[pk]
            is_p   = profiles and pk in profiles
            badge_bg = info[1] if is_p else "#94a3b8"
            fit_c  = "#16a34a" if fit >= 70 else ("#eab308" if fit >= 45 else "#e11d48")
            bar_w  = round(min(fit, 100))
            _rows += f"""<tr>
              <td style="padding:5px 8px">
                <span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;
                             font-weight:700;background:{badge_bg}18;color:{badge_bg};
                             border:1px solid {badge_bg}44">{info[0]}</span>
                <span style="font-size:11px;color:#57606a;margin-left:4px">{info[2]}</span>
                {"<span style='font-size:9px;background:#dcfce7;color:#15803d;border-radius:3px;padding:1px 5px;font-weight:700;margin-left:4px'>PASS</span>" if is_p else ""}
              </td>
              <td style="padding:5px 8px;text-align:right">
                <span style="font-size:13px;font-weight:800;color:{fit_c}">{fit:.0f}</span>
                <span style="font-size:10px;color:#9ca3af">/100</span>
              </td>
              <td style="padding:5px 8px;text-align:right;color:#57606a;font-size:11px">{w:.2f}&times;</td>
              <td style="padding:5px 8px;min-width:80px">
                <div style="display:flex;align-items:center;gap:6px">
                  <div style="flex:1;min-width:40px;height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden">
                    <div style="width:{bar_w}%;height:100%;background:{fit_c};border-radius:3px"></div>
                  </div>
                  <span style="font-size:11px;font-weight:700;color:#374151;white-space:nowrap;text-align:right">{contrib:.1f}pts</span>
                </div>
              </td>
            </tr>"""
        oc = "#16a34a" if overall_score >= 75 else ("#eab308" if overall_score >= 55 else "#e11d48")
        overall_breakdown_html = f"""
        <div style="margin-bottom:14px">
          <div style="font-size:10px;font-weight:700;color:#8d96a0;text-transform:uppercase;
                      letter-spacing:.06em;margin-bottom:8px">Overall Cross-Profile Score Breakdown</div>
          <div style="overflow-x:auto">
          <table style="width:100%;min-width:320px;border-collapse:collapse;font-size:12px;background:#f9fafb;
                        border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
            <thead>
              <tr style="background:#f0f2f5;font-size:10px;color:#57606a;text-transform:uppercase;letter-spacing:.04em">
                <th style="padding:5px 8px;text-align:left;font-weight:700">Screen</th>
                <th style="padding:5px 8px;text-align:right;font-weight:700">Fit Score</th>
                <th style="padding:5px 8px;text-align:right;font-weight:700">Weight</th>
                <th style="padding:5px 8px;text-align:left;font-weight:700">Contribution</th>
              </tr>
            </thead>
            <tbody>{_rows}</tbody>
            <tfoot>
              <tr style="background:#eff6ff;border-top:2px solid #bfdbfe">
                <td colspan="3" style="padding:6px 8px;font-size:11px;font-weight:700;color:#3b82d4">
                  Weighted Average Overall Score
                </td>
                <td style="padding:6px 8px;text-align:left">
                  <span style="font-size:18px;font-weight:900;color:{oc}">{overall_score:.0f}</span>
                  <span style="font-size:10px;color:#9ca3af">/100</span>
                </td>
              </tr>
            </tfoot>
          </table>
          </div>
        </div>"""

    panel_html = f"""
        <!-- Score cards -->
        <div style="margin-bottom:14px">{cards_html}</div>
        <!-- Overall Score Breakdown -->
        {overall_breakdown_html}
        <!-- DCF Sensitivity -->
        {f'<div style="margin-bottom:14px">{sens_html}</div>' if sens_html else ""}
        {spark_section}
        {bar_section}
        {chart_section}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 12px">
        <!-- Plain-English analysis -->
        <div style="font-size:13px;line-height:1.7;color:#374151">{body}</div>"""

    return ticker, panel_html, sc_v, sc_c


# ── Index / exchange origin badge ────────────────────────────────────────────

_INDEX_RULES: list[tuple] = [
    # (suffix_or_pattern, label, bg, fg)
    (".RO",    "BVB",     "#e8f5e9", "#2e7d32"),
    (".DE",    "XETRA",   "#e3f2fd", "#1565c0"),
    (".PA",    "EURONX",  "#e3f2fd", "#1565c0"),
    (".AS",    "AMS",     "#e3f2fd", "#1565c0"),
    (".MC",    "BME",     "#fce4ec", "#880e4f"),
    (".MI",    "MIL",     "#e8f5e9", "#2e7d32"),
    (".BR",    "EURONX",  "#e3f2fd", "#1565c0"),
    (".HE",    "OMXH",    "#e3f2fd", "#1565c0"),
    (".IR",    "ISE",     "#e8f5e9", "#2e7d32"),
    (".L",     "LSE",     "#f3e5f5", "#6a1b9a"),
    (".SW",    "SIX",     "#fff3e0", "#e65100"),
    ("-T",     "TSX",     "#fff8e1", "#f57f17"),
]

# S&P 500 constituents (used to label US tickers)
_SP500_SET: set[str] = set()
_DOW30_SET: set[str] = set()
_NASDAQ100_SET: set[str] = set()

def _load_index_sets() -> None:
    """Populate _SP500_SET, _DOW30_SET, _NASDAQ100_SET from most recent CSVs."""
    global _SP500_SET, _DOW30_SET, _NASDAQ100_SET
    import csv as _csv
    # Load S&P 500 from universe module (fast, no network)
    try:
        from src.universe import get_sp500_tickers
        _SP500_SET = set(get_sp500_tickers())
    except Exception:
        _SP500_SET = set()
    # DOW 30 from most recent dow30 CSV
    try:
        dow_p = _most_recent("*_dow30_ranking.csv")
        if dow_p:
            with open(dow_p, encoding="utf-8") as f:
                _DOW30_SET = {r["Ticker"].strip() for r in _csv.DictReader(f) if r.get("Ticker")}
    except Exception:
        _DOW30_SET = set()
    # NASDAQ-100 hardcoded core (avoids network call)
    _NASDAQ100_SET = {
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","COST",
        "NFLX","ASML","AZN","TMUS","CSCO","ADBE","AMD","PEP","INTC","INTU",
        "CMCSA","AMGN","AMAT","MU","ISRG","HON","LRCX","VRTX","KLAC","PANW",
        "REGN","SNPS","CDNS","MRVL","ADI","ORLY","CRWD","CEG","ABNB","FTNT",
        "MAR","DASH","WDAY","MELI","PYPL","CSGP","DXCM","ARM","ROP","ROST",
        "AEP","IDXX","TTD","ODFL","VRSK","FAST","BIIB","CTSH","EA","FANG",
        "KDP","XEL","ANSS","TEAM","DLTR","GEHC","PCAR","ZS","CDNS","WBD",
        "MNST","SIRI","MDLZ","JD","BIDU","NTES","PDD","CDW","NXPI","MCHP",
    }

def _index_badge(ticker: str) -> str:
    """Return a small HTML badge showing which index/exchange this ticker belongs to."""
    t = ticker.strip().upper()
    # European / non-US suffix rules
    for suffix, label, bg, fg in _INDEX_RULES:
        if t.endswith(suffix.upper()):
            return (f'<span style="font-size:9px;font-weight:700;background:{bg};color:{fg};'
                    f'border-radius:3px;padding:1px 5px;margin-left:3px">{label}</span>')
    # US tickers — check index membership
    if t in _DOW30_SET:
        return ('<span style="font-size:9px;font-weight:700;background:#fff3e0;color:#e65100;'
                'border-radius:3px;padding:1px 5px;margin-left:3px">DOW</span>')
    if t in _NASDAQ100_SET:
        return ('<span style="font-size:9px;font-weight:700;background:#e8eaf6;color:#283593;'
                'border-radius:3px;padding:1px 5px;margin-left:3px">NDQ</span>')
    if t in _SP500_SET:
        return ('<span style="font-size:9px;font-weight:700;background:#e3f2fd;color:#1565c0;'
                'border-radius:3px;padding:1px 5px;margin-left:3px">SPX</span>')
    # Unknown US small-cap
    return ('<span style="font-size:9px;font-weight:700;background:#f5f5f5;color:#9e9e9e;'
            'border-radius:3px;padding:1px 5px;margin-left:3px">OTC</span>')

def _index_label(ticker: str) -> str:
    """Return plain-text index label for use in Why Buy prose."""
    t = ticker.strip().upper()
    for suffix, label, _bg, _fg in _INDEX_RULES:
        if t.endswith(suffix.upper()):
            return label
    if t in _DOW30_SET:    return "Dow Jones 30"
    if t in _NASDAQ100_SET: return "NASDAQ-100"
    if t in _SP500_SET:    return "S&P 500"
    return "listed"


def _why_btn(ticker: str, sc_v: float | None, sc_c: str, ns: str = "") -> str:
    """Toggle button rendered inside the data row <td>. Calls toggleWhy(id)."""
    suffix = f"-{ns}" if ns else ""
    why_id = f"why-{ticker.replace('.', '-')}{suffix}"
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


def _why_tr(panel_html: str, col_count: int, ticker: str, ns: str = "") -> str:
    """A hidden <tr> that expands inline below the data row — full table width."""
    suffix = f"-{ns}" if ns else ""
    why_id = f"why-{ticker.replace('.', '-')}{suffix}"
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

/* mob-label — hidden on desktop, shown on mobile via media query */
.mob-label { display:none; }

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

/* ── RESPONSIVE ─────────────────────────────────────────────────────────────── */

/* Tablet ≤ 900px */
@media(max-width:900px){
  .page { padding:20px 16px 40px; }
  .section { padding:20px 20px; }
  .sec-body { padding:20px 20px !important; }
  .report-header { padding:24px 22px; }
  .report-header h1 { font-size:22px; }
  .header-meta { gap:16px; }
  .hm-item .hm-value { font-size:14px; }
  .sec-meta { display:none; }
  .stbl { font-size:11px; }
}

/* Mobile ≤ 600px */
@media(max-width:768px){
  .page { padding:12px 10px 32px; }

  /* Header */
  .report-header { padding:18px 16px; border-radius:10px; }
  .report-header h1 { font-size:18px; margin-bottom:4px; }
  .report-header .subtitle { font-size:12px; }
  .header-meta { flex-direction:column; gap:10px; margin-top:14px; }
  .hm-item { display:flex; justify-content:space-between; align-items:center; }
  .hm-item .hm-label { font-size:10px; }
  .hm-item .hm-value { font-size:13px; }

  /* Sections */
  .section { padding:16px 14px; border-radius:10px; margin-bottom:16px; }
  details.sec-wrap > .sec-body { padding:16px 14px !important; }
  .sec-hdr { padding:12px 14px; border-radius:10px; }
  .sec-title { font-size:13px; }
  .sec-badge { width:26px; height:26px; font-size:10px; }
  .section-title { font-size:16px; }

  /* Pills / stats bar */
  .stats-bar { gap:8px; }
  .stat-pill { min-width:calc(50% - 4px); padding:10px 12px; }
  .sp-value { font-size:20px; }

  /* TOC — wrap pills */
  .toc { padding:14px 14px; }
  .toc a { font-size:11px; padding:3px 9px; margin:2px 3px 2px 0; }

  /* Disclaimer */
  .disclaimer { padding:10px 12px; }

  /* ── Screener table → card layout on mobile ── */
  .stbl, .stbl thead, .stbl tbody, .stbl th, .stbl td, .stbl tr {
    display: block;
  }
  .stbl thead tr { position:absolute; top:-9999px; left:-9999px; }
  .stbl tbody tr {
    border:1px solid #e5e7eb; border-radius:10px;
    margin-bottom:12px; padding:12px 14px;
    background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.06);
  }
  .stbl tbody tr:hover td { background:transparent; }
  .stbl tbody td[data-label="#"] { display:none; }
  .stbl tbody td {
    display:flex !important; justify-content:space-between; align-items:center;
    padding:6px 2px; border-bottom:1px solid #f0f2f5;
    font-size:12px; text-align:left !important;
    width:100% !important; box-sizing:border-box;
  }
  .stbl tbody td:last-child { border-bottom:none; }
  /* Use CSS ::before pseudo-element to inject column labels from data-label attribute.
     This is more reliable than <span class="mob-label"> because it works regardless
     of the cell's inner HTML structure. */
  .stbl tbody td[data-label]::before {
    content: attr(data-label);
    font-size:10px; font-weight:700; color:#8d96a0;
    text-transform:uppercase; letter-spacing:.05em;
    flex-shrink:0; min-width:100px; margin-right:10px;
    line-height:1.4; white-space:nowrap;
  }
  /* Ticker cell — suppress ::before label (already shows ticker prominently) */
  .stbl tbody td[data-label="Ticker"]::before,
  .stbl tbody td[data-label="#"]::before { content: none; }
  /* Ticker cell — full-width block header of the card */
  .stbl tbody td[data-label="Ticker"] {
    flex-direction:column; align-items:flex-start;
    border-bottom:2px solid #e5e7eb; padding-bottom:10px; margin-bottom:2px;
  }
  /* Legacy mob-label spans — hide on mobile (replaced by ::before) */
  .mob-label { display:none !important; }
  /* gauge bars full width inside card */
  .gauge-wrap { width:100%; }
  .gauge-track { min-width:80px; flex:1; }

  /* Backtest tables — keep horizontal scroll */
  .bt-tbl { font-size:11px; min-width:480px; }
  .bt-tbl th, .bt-tbl td { padding:7px 8px; }

  /* Backtest KPI boxes */
  .bt-header { flex-direction:column; gap:8px; }
  .bt-kpi { padding:10px 12px; }
  .bt-kpi .kv { font-size:20px; }

  /* Stats pills */
  .bt-header { gap:8px; }

  /* Why-buy body */
  .why-body-inline { padding:14px 14px; }

  /* Filter bar — stack */
  .filter-bar { gap:6px; }
  .filter-bar input[type=number],
  .filter-bar select { max-width:90px; min-width:70px; font-size:11px; }
  .filter-count { margin-left:0; width:100%; }

  /* Watchlist */
  .watchlist-section { padding:14px 12px; }
  .watchlist-header { flex-wrap:wrap; gap:8px; }
  .watchlist-subtitle { margin-left:0; width:100%; }
  .wl-export-btn { font-size:11px; padding:4px 10px; }

  /* Backtest tab buttons */
  .bt-hold-btn, .bt-pt-btn { font-size:11px !important; padding:5px 12px !important; }

  /* Backtest $10k value boxes — stack on mobile */
  .bt-val-box { flex-direction:column !important; }
  .bt-val-item { border-left:none !important; padding-left:0 !important;
                 border-top:1px solid #e5e7eb; padding-top:10px !important;
                 margin-top:4px; }
  .bt-val-item:first-child { border-top:none; padding-top:0 !important; }

  /* Equity SVG full width */
  .bt-eq-svg { width:100% !important; height:auto !important; }

  /* KPI pills row — 2 per row */
  .bt-kpi-row { gap:6px !important; }
  .bt-kpi-row > div { min-width:calc(50% - 3px) !important; }
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
    "net_net": {
        "label": "Net-Net (NCAV)",
        "icon": "NN",
        "desc": "Benjamin Graham's deepest value screen. Price must be below Net Current Asset Value (current assets − all liabilities). The most conservative valuation floor — you're paying less than liquidation value.",
        "colour": "#b45309",
    },
    "momentum_quality": {
        "label": "Momentum + Quality",
        "icon": "MQ",
        "desc": "Combines price momentum (52w return ≥ 5%) with quality fundamentals (ROIC ≥ 12%, Op.Margin ≥ 15%, Piotroski ≥ 5). Companies already moving in the right direction with strong underlying economics.",
        "colour": "#ea580c",
    },
    "contrarian": {
        "label": "Short Contrarian",
        "icon": "CON",
        "desc": "Heavily shorted companies (short float ≥ 10%) with solid fundamentals. High short interest signals crowd pessimism — when combined with good Piotroski and margin of safety, creates contrarian opportunity.",
        "colour": "#dc2626",
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
                     passes: bool | None = None, profile_key: str | None = None) -> str:
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
    _tkr_pre = row.get("Ticker", "").strip()
    ticker, panel_html, sc_v, sc_c = _why_buy(row, profile_key=profile_key,
                                               ohlc=_PRICE_DATA.get(_tkr_pre) if _PRICE_DATA else None)
    sc_c = sc_c or "#9ca3af"
    _ns = profile_key or ""
    why_btn_html = _why_btn(ticker, sc_v, sc_c, ns=_ns) if panel_html else ""
    why_exp_row  = _why_tr(panel_html, col_count, ticker, ns=_ns) if panel_html else ""

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
        f'onclick="toggleStar(this)">&#9734;</button>'
    )

    data_tr = f"""<tr data-sector="{_sector_attr}" data-mos="{_mos_raw:.2f}" data-pe="{_pe_raw:.2f}" data-pfcf="{_pfcf_raw:.2f}" data-piotroski="{_pio_raw:.0f}">
      <td data-label="#" style="width:3%">{rank_html}</td>
      <td data-label="Ticker" style="width:12%">
        <div class="ticker-lbl">{_ticker_raw} {_index_badge(_ticker_raw)} {_star_btn}</div>
        <div class="company-lbl">{row.get('Company','')}</div>
        <div style="margin-top:2px">{badge}</div>
        {why_btn_html}
      </td>
      <td data-label="Sector" style="width:9%;color:#57606a;font-size:11px"><span class="mob-label">Sector</span>{row.get('Sector','') or '—'}</td>
      <td data-label="Price" class="r" style="width:6%"><span class="mob-label">Price</span>{_fmt(row.get('Price',''),2,prefix='$')}</td>
      <td data-label="Intrinsic" class="r" style="width:7%"><span class="mob-label">Intrinsic Val.</span>{_fmt(row.get('DCF Avg',''),2,prefix='$')}</td>
      <td data-label="MoS%" style="width:9%"><span class="mob-label">Margin of Safety</span>{mos_bar}</td>
      <td data-label="52w" style="width:8%"><span class="mob-label">52w Position</span>{pos_bar}</td>
      <td data-label="P/E" class="r" style="width:5%"><span class="mob-label">P/E</span>{_fmt(row.get('P/E',''),1,suffix='x')}</td>
      <td data-label="P/B" class="r" style="width:5%"><span class="mob-label">P/B</span>{_fmt(row.get('P/B',''),2,suffix='x')}</td>
      <td data-label="EV/EBITDA" class="r" style="width:6%"><span class="mob-label">EV/EBITDA</span>{_fmt(row.get('EV/EBITDA',''),1,suffix='x')}</td>
      <td data-label="P/FCF" class="r" style="width:5%"><span class="mob-label">P/FCF</span>{_fmt(row.get('P/FCF',''),1,suffix='x')}</td>
      <td data-label="ND/EBITDA" class="r" style="width:6%"><span class="mob-label">Net Debt/EBITDA</span>{_fmt(row.get('NetDebt/EBITDA',''),2,prefix='')}</td>
      <td data-label="Piotroski" style="width:5%;text-align:center"><span class="mob-label">Piotroski</span>{_quality_badge(row.get('Piotroski',''),'piotroski')}</td>
      <td data-label="ROIC" class="r" style="width:5%"><span class="mob-label">ROIC</span>{_quality_badge(row.get('ROIC%',''),'roic')}</td>
      <td data-label="DCF" style="width:6%;text-align:center"><span class="mob-label">DCF Model</span><span style="font-size:10px;background:#f0f2f5;padding:2px 4px;border-radius:4px;font-weight:600">{dcf_model}</span></td>
      <td data-label="Grade" class="r" style="width:5%;font-weight:800"><span class="mob-label">Grade</span><div><span style="color:{mc}">{grade}</span><div style="font-size:10px;color:#8d96a0;font-weight:400">{glabel}</div></div></td>
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
                             top_n: int = 10, max_rest: int = 150) -> str:
    """Build a profile section: KPI pills + top-N detailed + rest compact.
    max_rest caps the 'rest' (collapsed) table to keep HTML size manageable.
    """
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
    rest_rows = rows_sorted[top_n:top_n + max_rest]  # cap to keep HTML size under control

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
        _row_to_table_tr(r, i+1, colour, show_fit=True, profile_key=profile_key) for i, r in enumerate(top_rows)
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
            &#9660; Show top {len(rest_rows)} remaining companies (ranked #{top_n+1} – #{top_n + len(rest_rows)}{f' of {n} total' if n > top_n + len(rest_rows) else ''})
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
          <strong>Fit Score (0–100):</strong>
          70% criterion proximity (how close each metric is to the profile threshold)
          + 30% Composite Score (7-pillar quality score: MoS, FCF Yield, Piotroski, ROIC, Op.Margin, FCF Growth, Dilution).
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
            <div style="font-weight:800;font-size:13px">{ticker} {_index_badge(ticker)}</div>
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


# ── Backtest simulation engine ────────────────────────────────────────────────

def _fetch_bt_prices(tickers: list[str]) -> dict[str, dict[str, float]]:
    """
    Fetch ~5.5 years of daily adjusted closes (Jan 2020 – today) for backtest.
    Returns {ticker: {date_str: close_price}} using DuckDB ohlcv_cache.
    Falls back to yfinance if not cached.
    """
    try:
        import duckdb
        import yfinance as yf
    except ImportError:
        return {}

    _ensure_ohlcv_table()
    con    = duckdb.connect(str(DB_PATH))
    result: dict[str, dict[str, float]] = {}
    # For backtest we need data from 2018-01-01 onwards
    # (2019 start_year needs 12M lookback → prices from Jan 2018)
    bt_start = "2018-01-01"

    for tkr in tickers:
        # Check cache: do we have at least 1000 rows (≈4 years of trading days)?
        cached = con.execute("""
            SELECT date, close FROM ohlcv_cache
            WHERE ticker = ? AND date >= ?
            ORDER BY date ASC
        """, [tkr, bt_start]).fetchall()

        if len(cached) >= 800:
            result[tkr] = {r[0]: r[1] for r in cached if r[1] is not None}
            continue

        # Fetch full 5-year history from yfinance
        try:
            time.sleep(0.25)
            hist = yf.Ticker(tkr).history(start="2018-01-01", interval="1d", auto_adjust=True)
            if hist.empty:
                continue
            rows = []
            now_ts = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            for dt, row in hist.iterrows():
                date_str = str(dt)[:10]
                close_v  = float(row["Close"]) if row["Close"] == row["Close"] else None
                open_v   = float(row["Open"])  if row["Open"]  == row["Open"]  else None
                high_v   = float(row["High"])  if row["High"]  == row["High"]  else None
                low_v    = float(row["Low"])   if row["Low"]   == row["Low"]   else None
                vol_v    = int(row["Volume"])  if row["Volume"] == row["Volume"] else None
                rows.append((date_str, open_v, high_v, low_v, close_v, vol_v))
            # Upsert into cache (keep existing rows for other periods too)
            for r in rows:
                try:
                    con.execute("""
                        INSERT INTO ohlcv_cache
                            (ticker, date, open, high, low, close, volume, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (ticker, date) DO UPDATE SET
                            close = excluded.close,
                            fetched_at = excluded.fetched_at
                    """, [tkr, r[0], r[1], r[2], r[3], r[4], r[5], now_ts])
                except Exception:
                    pass
            result[tkr] = {r[0]: r[4] for r in rows if r[4] is not None}
        except Exception as exc:
            print(f"  [bt-price] {tkr}: {exc}")

    con.close()
    return result


def _price_on_or_after(prices: dict[str, float], target_date: str) -> float | None:
    """Return the close price on target_date or the next available trading day."""
    for i in range(10):
        # add i days to target_date
        from datetime import date as _date
        y, m, d = int(target_date[:4]), int(target_date[5:7]), int(target_date[8:10])
        import calendar as _cal
        td = _date(y, m, d)
        from datetime import timedelta as _td
        nd = td + _td(days=i)
        ds = nd.strftime("%Y-%m-%d")
        if ds in prices:
            return prices[ds]
    return None


def _month_end(year: int, month: int) -> str:
    """Return last calendar day of given month as YYYY-MM-DD."""
    import calendar
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last:02d}"


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    """Add n months to (year, month), return (new_year, new_month)."""
    month += n
    year  += (month - 1) // 12
    month  = (month - 1) % 12 + 1
    return year, month


def _momentum_rank(
    tickers: list[str],
    prices:  dict[str, dict[str, float]],
    as_of_date: str,
    lookback_months: int = 12,
) -> list[str]:
    """
    Re-rank tickers by 12-month price momentum as of `as_of_date`.
    Only uses price data available BEFORE that date — no look-ahead.
    Returns tickers sorted best momentum first.
    Falls back to the original order for tickers with no historical data.
    """
    look_back_y, look_back_m = _add_months(
        int(as_of_date[:4]), int(as_of_date[5:7]), -lookback_months
    )
    lookback_date = f"{look_back_y:04d}-{look_back_m:02d}-01"


    scored = []
    no_data = []
    for tkr in tickers:
        tp = prices.get(tkr, {})
        p_now  = _price_on_or_after(tp, as_of_date)
        p_then = _price_on_or_after(tp, lookback_date)
        if p_now and p_then and p_then > 0:
            momentum = (p_now / p_then - 1.0) * 100.0
            scored.append((tkr, momentum))
        else:
            no_data.append(tkr)

    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored] + no_data


def _fundamental_rank(
    tickers: list[str],
    raw_fits: dict[str, dict[str, float]],
    weights: dict[str, float] | None = None,
) -> list[str]:
    """
    Rank tickers by their cross-profile Overall Score (weighted ProfileFit average).
    Used as the 'Fundamental' ranking alternative in the backtest.
    Tickers not in raw_fits fall to the bottom.

    `weights` — optional per-profile multipliers; defaults to the standard 8-profile weights.
    """
    _default_weights = {
        "deep_value":       1.30,
        "net_net":          1.25,
        "buffett_quality":  1.20,
        "quality_value":    1.10,
        "dividend_growth":  1.05,
        "high_fcf_yield":   1.00,
        "momentum_quality": 0.90,
        "contrarian":       0.85,
    }
    _w = weights if weights is not None else _default_weights

    def _score(tkr: str) -> float:
        fits = raw_fits.get(tkr, {})
        if not fits:
            return 0.0
        w_sum = sum(_w.get(k, 1.0) for k in fits)
        w_fit = sum(fits[k] * _w.get(k, 1.0) for k in fits)
        return w_fit / w_sum if w_sum > 0 else 0.0

    return sorted(tickers, key=_score, reverse=True)


def _run_monthly_backtest(
    tickers: list[str],                      # candidate universe — re-ranked each period
    prices:  dict[str, dict[str, float]],    # {ticker: {date: close}}
    spx:     dict[str, float],               # {date: spx_close}
    holding_months: int,                     # 1, 3, 6, or 12
    top_n:   int = 5,
    start_year: int = _BT_START,
    end_year:   int = _BT_END,
    ranking_method: str = "momentum",        # "momentum" or "fundamental"
    raw_fits: dict[str, dict[str, float]] | None = None,  # needed for "fundamental"
    fund_weights: dict[str, float] | None = None,          # custom profile weights for fundamental rank
    blend_alpha: float = 0.0,                # 0=pure fundamental, 1=pure momentum, 0.5=blend
    min_momentum: float = -999.0,            # momentum gate: skip stocks below this 12M % threshold
) -> dict:
    """
    Non-overlapping walk-forward simulation with dynamic ranking.

    ranking_method:
      "momentum"    — at each entry date re-rank by 12M price momentum (default)
      "fundamental" — use static cross-profile Overall Score order (raw_fits)

    blend_alpha (0–1): when > 0, blends normalised fundamental score with normalised
      12M momentum score at each rebalance. blend_alpha=0 → pure fundamental,
      blend_alpha=1 → pure momentum, blend_alpha=0.5 → equal blend.

    min_momentum: momentum gate — exclude any candidate whose 12M momentum at
      rebalance is below this percentage threshold. -999 = no gate.

    Trades are entered every `holding_months` months (non-overlapping):
      - 1M hold  → 12 trades/year  (Jan, Feb, Mar, …)
      - 3M hold  →  4 trades/year  (Jan, Apr, Jul, Oct)
      - 6M hold  →  2 trades/year  (Jan, Jul)
      - 12M hold →  1 trade/year   (Jan)
    """
    # Pre-compute fundamental scores for all tickers (normalised 0–1 for blending)
    _f_weights = fund_weights
    _raw_fits  = raw_fits or {}
    _default_fw = {
        "deep_value": 1.30, "net_net": 1.25, "buffett_quality": 1.20,
        "quality_value": 1.10, "dividend_growth": 1.05, "high_fcf_yield": 1.00,
        "momentum_quality": 0.90, "contrarian": 0.85,
    }
    _fw = _f_weights if _f_weights is not None else _default_fw

    def _fund_score(tkr: str) -> float:
        fits = _raw_fits.get(tkr, {})
        if not fits: return 0.0
        w_sum = sum(_fw.get(k, 1.0) for k in fits)
        w_fit = sum(fits[k] * _fw.get(k, 1.0) for k in fits)
        return w_fit / w_sum if w_sum > 0 else 0.0

    # Static fundamental rank (used when blend_alpha == 0)
    _fund_scored = {t: _fund_score(t) for t in tickers}
    _fund_ranked = sorted(tickers, key=lambda t: _fund_scored.get(t, 0.0), reverse=True)

    trade_results = []

    # Build non-overlapping entry points: start Jan of start_year, step = holding_months
    year, month = start_year, 1
    while year < end_year or (year == end_year and month <= 12):
        exit_year, exit_month = _add_months(year, month, holding_months)
        # Only include if exit is within our data window (≤ end of end_year + 1 month buffer)
        ey_lim, em_lim = _add_months(end_year, 12, 1)
        if (exit_year, exit_month) > (ey_lim, em_lim):
            break

        # Entry: first trading day of this month
        entry_date = f"{year:04d}-{month:02d}-01"
        # Exit: first trading day of exit month
        exit_date  = f"{exit_year:04d}-{exit_month:02d}-01"

        # Compute 12M momentum for every candidate at this entry date (needed for gate + blend)
        mom_scores: dict[str, float] = {}
        for tkr in tickers:
            look_back_y, look_back_m = _add_months(year, month, -12)
            lb_date = f"{look_back_y:04d}-{look_back_m:02d}-01"
            tp = prices.get(tkr, {})
            p_now  = _price_on_or_after(tp, entry_date)
            p_then = _price_on_or_after(tp, lb_date)
            if p_now and p_then and p_then > 0:
                mom_scores[tkr] = (p_now / p_then - 1.0) * 100.0

        # Apply momentum gate — exclude stocks below threshold
        if min_momentum > -999.0:
            eligible = [t for t in tickers if mom_scores.get(t, -999.0) >= min_momentum]
            if not eligible:
                eligible = tickers   # fallback: don't filter out everything
        else:
            eligible = tickers

        # Build the combined rank for this rebalance
        if blend_alpha > 0.0 and mom_scores:
            # Normalise fundamental scores to [0,1] among eligible
            f_vals = [_fund_scored.get(t, 0.0) for t in eligible]
            f_min, f_max = min(f_vals), max(f_vals)
            f_rng = (f_max - f_min) or 1.0

            m_vals = [mom_scores.get(t, 0.0) for t in eligible]
            m_min, m_max = min(m_vals), max(m_vals)
            m_rng = (m_max - m_min) or 1.0

            def _blend_score(tkr: str) -> float:
                fn = (_fund_scored.get(tkr, 0.0) - f_min) / f_rng
                mn = (mom_scores.get(tkr, 0.0) - m_min) / m_rng
                return (1.0 - blend_alpha) * fn + blend_alpha * mn

            ranked_at_entry = sorted(eligible, key=_blend_score, reverse=True)
        elif ranking_method == "fundamental":
            # Pure fundamental — filter eligible from precomputed rank
            ranked_at_entry = [t for t in _fund_ranked if t in set(eligible)]
        else:
            # Pure momentum re-rank at each entry
            ranked_at_entry = _momentum_rank(eligible, prices, entry_date, lookback_months=12)

        # Pick top-N from dynamically ranked list that have both entry and exit prices
        picks = []
        for tkr in ranked_at_entry:
            tp = prices.get(tkr, {})
            ep = _price_on_or_after(tp, entry_date)
            xp = _price_on_or_after(tp, exit_date)
            if ep and xp and ep > 0:
                picks.append((tkr, ep, xp))
            if len(picks) >= top_n:
                break

        if not picks:
            # still advance the pointer
            year, month = _add_months(year, month, holding_months)
            continue

        # Portfolio return = equal-weight average of individual returns
        returns = [(xp / ep - 1.0) * 100.0 for _, ep, xp in picks]
        port_ret = sum(returns) / len(returns)

        # Benchmark (SPX) return over same window
        spx_ep = _price_on_or_after(spx, entry_date)
        spx_xp = _price_on_or_after(spx, exit_date)
        if spx_ep and spx_xp and spx_ep > 0:
            bm_ret = (spx_xp / spx_ep - 1.0) * 100.0
        else:
            bm_ret = None

        excess = port_ret - bm_ret if bm_ret is not None else None
        wins   = sum(1 for r in returns if r > (bm_ret or 0))

        trade_results.append({
            "year":    year,
            "month":   month,
            "period":  f"{year:04d}-{month:02d}",
            "entry":   entry_date,
            "exit":    exit_date,
            "tickers": [t for t, _, _ in picks],
            "returns": returns,
            "picks_detail": [
                {"ticker": t, "entry_price": ep, "exit_price": xp,
                 "return_pct": (xp / ep - 1.0) * 100.0}
                for t, ep, xp in picks
            ],
            "port":    port_ret,
            "bm":      bm_ret,
            "excess":  excess,
            "wins":    wins,
            "n_picks": len(picks),
        })

        # Advance to next non-overlapping entry
        year, month = _add_months(year, month, holding_months)

    if not trade_results:
        return {"monthly": [], "yearly": {}, "summary": {}}

    # Aggregate by year (each year contains 12/holding_months non-overlapping trades)
    yearly: dict[int, dict] = {}
    for r in trade_results:
        y = r["year"]
        if y not in yearly:
            yearly[y] = {"port_rets": [], "bm_rets": [], "picks": 0, "wins": 0}
        yearly[y]["port_rets"].append(r["port"])
        if r["bm"] is not None:
            yearly[y]["bm_rets"].append(r["bm"])
        yearly[y]["picks"] += r["n_picks"]
        yearly[y]["wins"]  += r["wins"]

    yearly_rows = []
    for y in sorted(yearly.keys()):
        d = yearly[y]
        # Compound the non-overlapping trade returns for this year
        port_annual = 1.0
        for tr in d["port_rets"]:
            port_annual *= (1.0 + tr / 100.0)
        port_annual = (port_annual - 1.0) * 100.0

        if d["bm_rets"]:
            bm_annual = 1.0
            for tr in d["bm_rets"]:
                bm_annual *= (1.0 + tr / 100.0)
            bm_annual = (bm_annual - 1.0) * 100.0
        else:
            bm_annual = None

        exc = port_annual - bm_annual if bm_annual is not None else None
        total_p = d["picks"]
        wr = (d["wins"] / total_p * 100.0) if total_p else 0.0

        yearly_rows.append({
            "year":    y,
            "port":    port_annual,
            "bm":      bm_annual,
            "excess":  exc,
            "picks":   total_p,
            "wins":    d["wins"],
            "win_rate": wr,
        })

    # Summary stats — compound all non-overlapping trade returns
    all_trade_rets = [r["port"] / 100.0 for r in trade_results]
    compound = 1.0
    for tr in all_trade_rets:
        compound *= (1.0 + tr)
    n_trades = len(all_trade_rets)
    # Number of effective years = total trades × holding_months / 12
    n_years  = n_trades * holding_months / 12.0
    cagr_port = (compound ** (1.0 / n_years) - 1.0) * 100.0 if n_years > 0 else 0.0

    # Benchmark CAGR (same non-overlapping windows)
    bm_trade_rets = [r["bm"] / 100.0 for r in trade_results if r["bm"] is not None]
    if bm_trade_rets:
        compound_bm = 1.0
        for tr in bm_trade_rets:
            compound_bm *= (1.0 + tr)
        n_bm = len(bm_trade_rets) * holding_months / 12.0
        cagr_bm = (compound_bm ** (1.0 / n_bm) - 1.0) * 100.0 if n_bm > 0 else 0.0
    else:
        cagr_bm = 0.0

    # Sharpe — annualise per-trade returns
    # Convert each trade return to annualised equivalent for consistency
    ann_factor = 12.0 / holding_months    # trades per year
    if len(all_trade_rets) > 1:
        avg_t  = sum(all_trade_rets) / len(all_trade_rets)
        std_t  = math.sqrt(sum((x - avg_t)**2 for x in all_trade_rets) / (len(all_trade_rets) - 1))
        sharpe = round((avg_t / std_t) * math.sqrt(ann_factor), 2) if std_t > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown on compounded equity curve
    equity = [1.0]
    for tr in all_trade_rets:
        equity.append(equity[-1] * (1.0 + tr))
    peak, maxdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        maxdd = max(maxdd, (peak - v) / peak * 100.0)

    total_picks = sum(r["n_picks"] for r in trade_results)
    total_wins  = sum(r["wins"]    for r in trade_results)
    win_rate    = total_wins / total_picks * 100.0 if total_picks else 0.0

    # ── Equity curve: $10,000 compounded through every trade ─────────────────
    INITIAL = 10_000.0
    port_equity = [INITIAL]
    for tr in all_trade_rets:
        port_equity.append(port_equity[-1] * (1.0 + tr))

    bm_equity = [INITIAL]
    for tr in bm_trade_rets:
        bm_equity.append(bm_equity[-1] * (1.0 + tr))
    # Pad bm to same length if needed
    while len(bm_equity) < len(port_equity):
        bm_equity.append(bm_equity[-1])

    port_final = port_equity[-1]
    bm_final   = bm_equity[-1]

    # ── True SPX annual returns: Jan 1 → Jan 1 of next year ──────────────────
    # Used for "real" year-by-year SPX comparison independent of holding period.
    # Use a wider 20-day forward-scan so holiday clusters (new-year, long weekends)
    # never produce a None for the SPX endpoint lookup.
    def _spx_price(date_str: str) -> float | None:
        """Find the first SPX close on or after date_str, scanning up to 20 days."""
        from datetime import date as _d, timedelta as _td
        y0, m0, d0 = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
        for offset in range(20):
            ds = (_d(y0, m0, d0) + _td(days=offset)).strftime("%Y-%m-%d")
            if ds in spx:
                return spx[ds]
        return None

    spx_annual: dict[int, float | None] = {}
    for y in range(start_year, end_year + 1):
        sp_ep = _spx_price(f"{y:04d}-01-01")
        sp_xp = _spx_price(f"{y+1:04d}-01-01")
        if sp_ep and sp_xp and sp_ep > 0:
            spx_annual[y] = round((sp_xp / sp_ep - 1.0) * 100.0, 2)
        else:
            # Fallback: use compound of trade-segmented bm_rets for this year
            yr_row = next((r for r in yearly_rows if r["year"] == y), None)
            spx_annual[y] = yr_row["bm"] if (yr_row and yr_row.get("bm") is not None) else None

    # Patch yearly_rows with true SPX annual returns
    for row in yearly_rows:
        row["bm_true"] = spx_annual.get(row["year"])
        row["excess_true"] = (
            row["port"] - row["bm_true"]
            if row["bm_true"] is not None else None
        )

    return {
        "monthly":      trade_results,   # kept as "monthly" key for rendering compat
        "yearly":       yearly_rows,
        "port_equity":  port_equity,     # list of floats, len = n_trades+1
        "bm_equity":    bm_equity,
        "port_final":   round(port_final, 2),
        "bm_final":     round(bm_final,   2),
        "holding_months": holding_months,  # ← stored so callers can compute year labels
        "summary":  {
            "cagr_port": round(cagr_port, 2),
            "cagr_bm":   round(cagr_bm,   2),
            "excess":    round(cagr_port - cagr_bm, 2),
            "sharpe":    sharpe,
            "maxdd":     round(-maxdd, 2),
            "win_rate":  round(win_rate, 1),
            "n_months":  n_trades,
            "n_picks":   total_picks,
        },
    }


def _bt_kpi_bar(s: dict) -> str:
    """Render the KPI summary bar for a single backtest result dict."""
    cp  = s.get("cagr_port", 0.0)
    cb  = s.get("cagr_bm",   0.0)
    exc = s.get("excess",    0.0)
    sh  = s.get("sharpe",    0.0)
    dd  = s.get("maxdd",     0.0)
    wr  = s.get("win_rate",  0.0)
    cc  = _return_colour(cp)
    ec  = _return_colour(exc)
    sp  = "+" if cp  >= 0 else ""
    se  = "+" if exc >= 0 else ""
    sb  = "+" if cb  >= 0 else ""
    return f"""
    <div class="bt-header">
      <div class="bt-kpi">
        <div class="kv" style="color:{cc}">{sp}{cp:.2f}%</div>
        <div class="kl">Portfolio CAGR</div>
      </div>
      <div class="bt-kpi">
        <div class="kv" style="color:#57606a">{sb}{cb:.2f}%</div>
        <div class="kl">S&amp;P 500 CAGR</div>
      </div>
      <div class="bt-kpi">
        <div class="kv" style="color:{ec}">{se}{exc:.2f}%</div>
        <div class="kl">Excess vs S&amp;P 500</div>
      </div>
      <div class="bt-kpi">
        <div class="kv">{sh:.2f}</div>
        <div class="kl">Sharpe Ratio (ann.)</div>
      </div>
      <div class="bt-kpi">
        <div class="kv" style="color:#e11d48">{dd:.1f}%</div>
        <div class="kl">Max Drawdown</div>
      </div>
      <div class="bt-kpi">
        <div class="kv">{wr:.0f}%</div>
        <div class="kl">Trade Win Rate</div>
      </div>
    </div>"""


def _bt_equity_svg(port_eq: list[float], bm_eq: list[float],
                   port_colour: str = "#3b82d4",
                   start_year: int = _BT_START,
                   holding_months: int = 3) -> str:
    """
    SVG line chart of $10,000 compounded equity curve with year labels on X-axis.
    port_eq and bm_eq are lists of portfolio values (len = n_trades+1).
    start_year:     first calendar year (for X-axis labels).
    holding_months: trade holding period — used to map trade-index → calendar year.
                    1M → 12 trades/year, 3M → 4, 6M → 2, 12M → 1.
    """
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 580, 175, 38, 12, 12, 24
    all_vals = port_eq + bm_eq
    mn = min(all_vals) * 0.97
    mx = max(all_vals) * 1.03
    span_v = mx - mn or 1.0
    n = max(len(port_eq), len(bm_eq)) - 1 or 1

    chart_w = W - PAD_L - PAD_R
    chart_h = H - PAD_T - PAD_B

    def _x(i: int) -> float:
        return round(PAD_L + i / n * chart_w, 1)

    def _y(v: float) -> float:
        return round(H - PAD_B - (v - mn) / span_v * chart_h, 1)

    def pts(eq: list[float]) -> str:
        return " ".join(f"{_x(i)},{_y(v)}" for i, v in enumerate(eq))

    # Y-axis: 3 reference lines with $ labels
    y_refs = ""
    for ref in [mn, (mn + mx) / 2, mx]:
        y = _y(ref)
        lbl = f"${ref:,.0f}"
        y_refs += (
            f'<line x1="{PAD_L}" y1="{y}" x2="{W - PAD_R}" y2="{y}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{PAD_L - 4}" y="{y + 3.5}" text-anchor="end" '
            f'font-size="9" fill="#9ca3af">{lbl}</text>'
        )

    # X-axis year labels — computed from ACTUAL trade count and holding period.
    # n trades × (holding_months / 12) = total years covered.
    # Each year boundary falls at trade index i = round(year_offset * 12 / holding_months).
    trades_per_year = 12.0 / holding_months           # e.g. 12 for 1M, 4 for 3M, 2 for 6M
    total_years_f   = n / trades_per_year             # e.g. 84/12 = 7.0 for 1M×84 trades
    end_year        = start_year + int(round(total_years_f))   # e.g. 2019+7 = 2026

    x_labels = ""
    for yr in range(start_year, end_year + 1):
        trade_idx = round((yr - start_year) * trades_per_year)
        if trade_idx > n:
            break
        x = round(PAD_L + trade_idx / n * chart_w, 1)
        x_labels += (
            f'<line x1="{x}" y1="{PAD_T}" x2="{x}" y2="{H - PAD_B}" '
            f'stroke="#f0f2f5" stroke-width="1"/>'
            f'<text x="{x}" y="{H - 4}" text-anchor="middle" '
            f'font-size="9" fill="#9ca3af" font-weight="600">{yr}</text>'
        )

    port_pts = pts(port_eq)
    bm_pts   = pts(bm_eq)

    # Final endpoint dots
    px_last = _x(len(port_eq) - 1)
    py_last = _y(port_eq[-1])
    bx_last = _x(len(bm_eq) - 1)
    by_last = _y(bm_eq[-1])

    return (
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px;height:auto;'
        f'display:block;overflow:visible">'
        f'{y_refs}'
        f'{x_labels}'
        f'<polyline points="{bm_pts}" fill="none" stroke="#9ca3af" stroke-width="2" '
        f'stroke-dasharray="4 3" stroke-linejoin="round"/>'
        f'<polyline points="{port_pts}" fill="none" stroke="{port_colour}" stroke-width="2.5" '
        f'stroke-linejoin="round"/>'
        f'<circle cx="{bx_last}" cy="{by_last}" r="4" fill="#9ca3af"/>'
        f'<circle cx="{px_last}" cy="{py_last}" r="5" fill="{port_colour}"/>'
        f'</svg>'
    )


def _bt_yearly_chart(yearly_rows: list[dict], portfolio_label: str) -> str:
    """Render the horizontal dual-bar chart for yearly returns.
    Uses bm_true (Jan→Jan SPX) for the benchmark bar, not the trade-segmented BM."""
    all_vals = []
    for r in yearly_rows:
        if r.get("port") is not None: all_vals.append(abs(r["port"]))
        bm_display = r.get("bm_true") if r.get("bm_true") is not None else r.get("bm")
        if bm_display is not None: all_vals.append(abs(bm_display))
    max_abs = max(all_vals) if all_vals else 30.0
    scale   = 50.0 / (max_abs + 2.0)

    def _bar(val: float | None, colour: str, na_label: str = "no data") -> str:
        if val is None:
            return (
                f'<div style="height:22px;background:#f0f2f5;border-radius:4px;'
                f'display:flex;align-items:center;padding-left:8px">'
                f'<span style="font-size:10px;color:#9ca3af;font-style:italic">{na_label}</span></div>'
            )
        pct_w = min(abs(val) * scale, 50.0)
        lbl   = f'{"+" if val >= 0 else ""}{val:.1f}%'
        if val >= 0:
            return (
                f'<div style="position:relative;height:22px;background:#f0f2f5;border-radius:4px;overflow:hidden">'
                f'<div style="position:absolute;left:50%;top:0;width:{pct_w:.2f}%;height:100%;'
                f'background:{colour};border-radius:0 4px 4px 0;display:flex;align-items:center;'
                f'padding-left:4px"><span style="font-size:11px;font-weight:700;color:#fff;white-space:nowrap">{lbl}</span></div>'
                f'<div style="position:absolute;left:50%;top:0;height:100%;border-left:2px solid #9ca3af"></div>'
                f'</div>'
            )
        else:
            return (
                f'<div style="position:relative;height:22px;background:#f0f2f5;border-radius:4px;overflow:hidden">'
                f'<div style="position:absolute;right:50%;top:0;width:{pct_w:.2f}%;height:100%;'
                f'background:{colour};border-radius:4px 0 0 4px;display:flex;align-items:center;'
                f'justify-content:flex-end;padding-right:4px"><span style="font-size:11px;font-weight:700;color:#fff;white-space:nowrap">{lbl}</span></div>'
                f'<div style="position:absolute;left:50%;top:0;height:100%;border-left:2px solid #9ca3af"></div>'
                f'</div>'
            )

    html = '<div class="chart-wrap">'
    html += f"""
    <div style="display:flex;gap:16px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:12px;height:12px;background:#3b82d4;border-radius:2px"></div>
        <span style="font-size:11px;font-weight:600;color:#374151">{portfolio_label} (annual, compounded trades)</span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:12px;height:12px;background:#9ca3af;border-radius:2px"></div>
        <span style="font-size:11px;font-weight:600;color:#374151">S&amp;P 500 (Jan–Jan real)</span>
      </div>
      <div style="font-size:10px;color:#8d96a0;margin-left:auto">right = gain &nbsp;|&nbsp; left = loss</div>
    </div>"""

    for r in yearly_rows:
        yr  = r["year"]
        pv  = r["port"]
        # Use true Jan→Jan SPX as benchmark bar
        bv  = r.get("bm_true") if r.get("bm_true") is not None else r.get("bm")
        ev  = r.get("excess_true") if r.get("excess_true") is not None else r.get("excess")
        exc_c = "excess-pos" if (ev or 0) >= 0 else "excess-neg"
        html += f"""
        <div style="display:flex;align-items:stretch;gap:10px;margin-bottom:10px;
                    background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:8px 12px">
          <div style="width:38px;flex-shrink:0;display:flex;align-items:center;
                      justify-content:center;font-size:14px;font-weight:800;color:#1f2328">{yr}</div>
          <div style="flex:1;display:flex;flex-direction:column;gap:4px">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="width:72px;font-size:10px;color:#374151;font-weight:600;flex-shrink:0">Portfolio</span>
              <div style="flex:1">{_bar(pv, _return_colour(pv or 0))}</div>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <span style="width:72px;font-size:10px;color:#374151;font-weight:600;flex-shrink:0">S&amp;P 500</span>
              <div style="flex:1">{_bar(bv, "#9ca3af")}</div>
            </div>
          </div>
          <div style="width:100px;flex-shrink:0;display:flex;flex-direction:column;
                      align-items:flex-end;justify-content:center;gap:1px">
            <div style="font-size:10px;color:#8d96a0">Excess</div>
            <div class="{exc_c}" style="font-size:15px">{("+" if ev >= 0 else "") + f"{ev:.1f}%" if ev is not None else '<span style="color:#9ca3af;font-size:11px">N/A</span>'}</div>
          </div>
        </div>"""

    html += "</div>"
    return html


def _bt_monthly_detail(monthly: list[dict]) -> str:
    """Collapsible trade detail table with per-ticker expandable rows."""
    rows_html = ""
    for i, r in enumerate(monthly):
        port_c = _return_colour(r["port"])
        bm_c   = "#9ca3af"
        exc_c  = "excess-pos" if (r["excess"] or 0) >= 0 else "excess-neg"
        sp     = "+" if r["port"] >= 0 else ""
        sb     = "+" if (r["bm"] or 0) >= 0 else ""
        se     = "+" if (r["excess"] or 0) >= 0 else ""
        entry  = r.get("entry", r["period"])[:10]   # YYYY-MM-DD
        exit_  = r.get("exit",  "")[:10]
        detail = r.get("picks_detail", [])
        bm_val = r["bm"] or 0

        # Build ticker chips for summary column
        tkr_chips = ""
        for d in detail:
            rc = _return_colour(d["return_pct"])
            s  = "+" if d["return_pct"] >= 0 else ""
            tkr_chips += (
                f'<span style="display:inline-block;background:#f7f8fa;border:1px solid #e5e7eb;'
                f'border-radius:4px;padding:1px 5px;margin:1px 2px;font-size:10px;white-space:nowrap">'
                f'<strong>{d["ticker"]}</strong>'
                f'<span style="color:{rc};margin-left:4px">{s}{d["return_pct"]:.1f}%</span>'
                f'</span>'
            )

        # Per-ticker sub-table rows
        detail_rows = ""
        for d in detail:
            rc  = _return_colour(d["return_pct"])
            exc = d["return_pct"] - bm_val
            ec  = _return_colour(exc)
            s   = "+" if d["return_pct"] >= 0 else ""
            se2 = "+" if exc >= 0 else ""
            win_icon = "✓" if d["return_pct"] > bm_val else "✗"
            win_c    = "#16a34a" if d["return_pct"] > bm_val else "#e11d48"
            detail_rows += (
                f'<tr style="background:#f7fbff">'
                f'<td style="padding:4px 8px;font-size:11px;color:#57606a;padding-left:32px">↳</td>'
                f'<td style="padding:4px 8px;font-size:12px;font-weight:700">{d["ticker"]}</td>'
                f'<td style="padding:4px 8px;font-size:11px;text-align:right">${d["entry_price"]:.2f}</td>'
                f'<td style="padding:4px 8px;font-size:11px;text-align:right">${d["exit_price"]:.2f}</td>'
                f'<td style="padding:4px 8px;font-size:12px;font-weight:700;text-align:right;color:{rc}">{s}{d["return_pct"]:.2f}%</td>'
                f'<td style="padding:4px 8px;font-size:11px;text-align:right;color:{ec}">{se2}{exc:.2f}%</td>'
                f'<td style="padding:4px 8px;font-size:12px;text-align:center;color:{win_c};font-weight:700">{win_icon}</td>'
                f'<td></td>'
                f'</tr>'
            )

        row_id = f"bt-tr-{i}"
        rows_html += f"""<tr style="cursor:pointer" onclick="btToggleTrade('{row_id}')">
          <td style="font-weight:700;white-space:nowrap">{entry}</td>
          <td style="color:#57606a;white-space:nowrap">{exit_}</td>
          <td class="r" style="color:{port_c};font-weight:700">{sp}{r["port"]:.2f}%</td>
          <td class="r" style="color:{bm_c}">{sb}{(r["bm"] or 0):.2f}%</td>
          <td class="r"><span class="{exc_c}">{se}{(r["excess"] or 0):.2f}%</span></td>
          <td class="r">{r["wins"]}/{r["n_picks"]}</td>
          <td style="font-size:11px">{tkr_chips}</td>
        </tr>
        <tr id="{row_id}" style="display:none">
          <td colspan="8" style="padding:0 0 6px 0">
            <table style="width:100%;border-collapse:collapse;font-size:11px">
              <thead>
                <tr style="background:#e8f0fe">
                  <th style="padding:4px 8px;text-align:left"></th>
                  <th style="padding:4px 8px;text-align:left">Ticker</th>
                  <th style="padding:4px 8px;text-align:right">Buy-in</th>
                  <th style="padding:4px 8px;text-align:right">Sell-out</th>
                  <th style="padding:4px 8px;text-align:right">Return</th>
                  <th style="padding:4px 8px;text-align:right">vs S&amp;P</th>
                  <th style="padding:4px 8px;text-align:center">Win?</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>{detail_rows}</tbody>
            </table>
          </td>
        </tr>"""

    return f"""
    <details style="margin-top:14px">
      <summary style="cursor:pointer;font-size:12px;font-weight:700;color:#3b82d4;
                      padding:6px 0;user-select:none">
        &#9654; Show trade detail ({len(monthly)} trades) &nbsp;<span style="font-weight:400;color:#57606a">· click any row to expand ticker breakdown</span>
      </summary>
      <div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:8px">
        <table class="bt-tbl" style="width:100%;font-size:12px;min-width:520px">
          <thead><tr>
            <th>Entry</th>
            <th>Exit</th>
            <th class="r">Portfolio</th>
            <th class="r">S&amp;P 500</th>
            <th class="r">Excess</th>
            <th class="r">Wins/Picks</th>
            <th>Tickers &amp; Returns</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </details>"""



def _build_optimizer_section(top5: list[dict]) -> str:
    """
    Render the Weight Optimizer panel: a row of strategy buttons (Strategy 1–5)
    plus a panel for each showing KPI, equity curve, yearly chart and the weight formula used.
    Default = best strategy (rank 1) is selected.
    """
    if not top5:
        return ""

    _colours = ["#059669", "#3b82d4", "#7c3aed", "#d97706", "#dc2626"]
    _medal   = ["🥇", "🥈", "🥉", "4th", "5th"]

    PROFILE_FULL = {
        "deep_value":       "Deep Value",
        "net_net":          "Net-Net (NCAV)",
        "buffett_quality":  "Buffett Quality",
        "quality_value":    "Quality Value",
        "dividend_growth":  "Dividend Growth",
        "high_fcf_yield":   "High FCF Yield",
        "momentum_quality": "Momentum+Quality",
        "contrarian":       "Short Contrarian",
    }

    strat_btns = ""
    strat_panels = ""

    for idx, item in enumerate(top5):
        sid    = f"opt-strat-{idx}"
        colour = _colours[idx]
        cagr   = item["cagr"]
        excess = item["excess"]
        hold   = item.get("holding_months", 3)
        alpha  = item.get("blend_alpha", 0.0)
        gate   = item.get("min_momentum", -999.0)
        is_default = (idx == 0)

        btn_sty = (
            f"background:{colour};color:#fff;border-color:{colour}"
            if is_default else
            "background:#fff;color:#374151;border-color:#e5e7eb"
        )
        exc_sign = "+" if excess >= 0 else ""

        # Compact strategy descriptor for button subtitle
        blend_lbl = (
            "Pure Fund." if alpha == 0.0 else
            "Pure Mom."  if alpha == 1.0 else
            f"Blend {alpha:.0%}"
        )
        gate_lbl  = "" if gate <= -999.0 else f" · Mom≥{gate:.0f}%"
        strat_btns += (
            f'<button class="opt-strat-btn" id="opt-btn-{idx}" data-optid="{idx}" '
            f'onclick="optSwitchStrat({idx})" '
            f'style="padding:8px 18px;font-size:12px;font-weight:700;border:2px solid;'
            f'border-radius:10px;cursor:pointer;{btn_sty};text-align:left;min-width:160px">'
            f'<div style="font-size:10px;opacity:.85;margin-bottom:2px">Strategy {idx+1} &nbsp;·&nbsp; {hold}M hold</div>'
            f'<div>{_medal[idx]} CAGR <strong>{cagr:+.1f}%</strong></div>'
            f'<div style="font-size:10px;margin-top:1px">vs S&amp;P {exc_sign}{excess:.1f}% &nbsp;·&nbsp; {blend_lbl}{gate_lbl}</div>'
            f'</button>'
        )

        # Build weight table
        w = item["weights"]
        w_sorted = sorted(w.items(), key=lambda x: -x[1])
        w_rows = ""
        max_w = max(w.values()) if w else 1.0
        for pk, pv in w_sorted:
            bar_pct = round(pv / max_w * 100, 1)
            w_rows += (
                f'<tr style="border-bottom:1px solid #f0f2f5">'
                f'<td style="padding:5px 8px;font-size:12px;font-weight:600">{PROFILE_FULL.get(pk, pk)}</td>'
                f'<td style="padding:5px 8px;font-size:13px;font-weight:800;color:#1f2328">×{pv:.3f}</td>'
                f'<td style="padding:5px 8px;width:130px">'
                f'<div style="background:#e5e7eb;border-radius:4px;height:10px">'
                f'<div style="background:{colour};border-radius:4px;height:10px;width:{bar_pct}%"></div>'
                f'</div></td>'
                f'</tr>'
            )

        s      = item["result"].get("summary", {})
        cp     = s.get("cagr_port", 0.0)
        cb     = s.get("cagr_bm",   0.0)
        exc    = s.get("excess",    0.0)
        sh     = s.get("sharpe",    0.0)
        dd     = s.get("maxdd",     0.0)
        wr     = s.get("win_rate",  0.0)
        nm     = s.get("n_months",  0)
        cc     = _return_colour(cp)
        ec     = _return_colour(exc)
        pf     = item["result"].get("port_final",  10000)
        pf_c   = _return_colour(pf - 10000)
        port_eq  = item["result"].get("port_equity",    [10000])
        bm_eq    = item["result"].get("bm_equity",      [10000])
        yr       = item["result"].get("yearly",          [])
        i_hold   = item.get("holding_months", item["result"].get("holding_months", 3))
        equity_svg   = _bt_equity_svg(port_eq, bm_eq, colour, start_year=_BT_START, holding_months=i_hold)
        yearly_chart = _bt_yearly_chart(yr, f"Strategy {idx+1}")
        fmt_w = _fmt_weights(w)

        # Strategy descriptor line
        blend_desc = (
            "Pure Fundamental ranking (no momentum blend)"  if alpha == 0.0 else
            "Pure Momentum ranking (no fundamental blend)"  if alpha == 1.0 else
            f"Blended: {(1-alpha)*100:.0f}% Fundamental + {alpha*100:.0f}% Momentum"
        )
        gate_desc = "No momentum gate" if gate <= -999.0 else f"Momentum gate: only stocks with 12M momentum ≥ {gate:.0f}%"

        strat_panels += f"""
        <div class="opt-panel" id="{sid}" style="{'display:block' if is_default else 'display:none'}">
          <div style="background:#f7f8fa;border:1px solid #e5e7eb;border-left:4px solid {colour};
                      border-radius:8px;padding:12px 16px;margin-bottom:16px">
            <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">
              <span style="font-size:11px;font-weight:700;color:{colour}">Hold: {hold}M</span>
              <span style="font-size:11px;color:#374151">{blend_desc}</span>
              <span style="font-size:11px;color:#57606a">{gate_desc}</span>
            </div>
            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
                        color:#57606a;margin-bottom:4px">Weight Formula — Top 15 Picks</div>
            <div style="font-size:12px;color:#374151;line-height:2">{fmt_w}</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
            <div style="flex:1;min-width:80px;background:#f7f8fa;border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800;color:{cc}">{cp:+.1f}%</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">CAGR</div>
            </div>
            <div style="flex:1;min-width:80px;background:#f7f8fa;border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800;color:#57606a">{cb:+.1f}%</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">S&amp;P 500</div>
            </div>
            <div style="flex:1;min-width:80px;background:#f7f8fa;border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800;color:{ec}">{exc:+.1f}%</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">vs S&amp;P</div>
            </div>
            <div style="flex:1;min-width:80px;background:#f7f8fa;border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800">{sh:.2f}</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">Sharpe</div>
            </div>
            <div style="flex:1;min-width:80px;background:#f7f8fa;border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800;color:#e11d48">{dd:.1f}%</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">Max DD</div>
            </div>
            <div style="flex:1;min-width:80px;background:#f7f8fa;border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800">{wr:.0f}%</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">Win Rate</div>
            </div>
            <div style="flex:1;min-width:90px;background:{colour}12;border:1px solid {colour}33;
                        border-radius:8px;padding:10px 12px;text-align:center">
              <div style="font-size:20px;font-weight:800;color:{pf_c}">${pf:,.0f}</div>
              <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">$10k → now</div>
            </div>
          </div>
          <div style="margin-bottom:16px">{equity_svg}</div>
          <div style="font-size:10px;color:#9ca3af;margin-bottom:12px;text-align:right">
            — Portfolio &nbsp;|&nbsp; - - S&amp;P 500 &nbsp;|&nbsp; {nm} trades · {hold}M hold · Top 15 equal-weight
          </div>
          {yearly_chart}
          <details style="margin-top:14px">
            <summary style="cursor:pointer;font-size:12px;font-weight:700;color:{colour};
                            padding:6px 10px;background:{colour}10;border:1px solid {colour}33;
                            border-radius:6px;user-select:none;list-style:none">
              &#9654;&nbsp; Full weight breakdown by profile
            </summary>
            <div style="margin-top:8px;overflow-x:auto">
              <table style="width:100%;border-collapse:collapse">
                <thead><tr style="background:#f0f2f5">
                  <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:700">Profile</th>
                  <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:700">Weight</th>
                  <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:700">Relative</th>
                </tr></thead>
                <tbody>{w_rows}</tbody>
              </table>
            </div>
          </details>
        </div>"""

    return f"""
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;
                  padding:20px 22px;margin-bottom:24px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;flex-wrap:wrap">
          <span style="font-size:16px;font-weight:800;color:#15803d">⚡ Weight Optimizer</span>
          <span style="font-size:11px;color:#57606a;font-weight:500;background:#fff;
                       border:1px solid #e5e7eb;border-radius:12px;padding:2px 10px">
            120 combinations · 4-dimensional search · Top 15 picks · {_BT_RANGE}
          </span>
        </div>
        <div style="font-size:12px;color:#57606a;margin-bottom:16px;line-height:1.6">
          Searches across <strong>profile weights</strong>, <strong>holding period</strong> (1M/3M/6M),
          <strong>momentum blend</strong> (0–100% momentum mixed with fundamentals),
          and <strong>momentum gate</strong> (exclude stocks below a 12M return threshold).
          <strong style="color:#15803d">Strategy 1 = best CAGR</strong> out of 120 combinations.
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px">
          {strat_btns}
        </div>
        {strat_panels}
      </div>"""



# ── Weight optimiser ──────────────────────────────────────────────────────────

_PROFILE_KEYS_ALL = [
    "deep_value", "net_net", "buffett_quality", "quality_value",
    "dividend_growth", "high_fcf_yield", "momentum_quality", "contrarian",
]

def _generate_optimizer_combos(n: int = 120) -> list[dict]:
    """
    Generate n reproducible multi-dimensional search combinations.
    Each combo has:
      - weights:       {profile: weight}  — normalised, mean=1.0
      - holding_months: int              — 1, 3, or 6
      - blend_alpha:   float [0,1]       — 0=pure fundamental, 1=pure momentum
      - min_momentum:  float             — momentum gate threshold (% 12M)

    Combo 0 = current defaults (baseline, no blending, no gate, 3M hold).
    """
    import random
    combos: list[dict] = []

    # Combo 0: pure baseline (matches standard Top Overall behaviour)
    combos.append({
        "weights": {
            "deep_value":       1.30, "net_net":          1.25,
            "buffett_quality":  1.20, "quality_value":    1.10,
            "dividend_growth":  1.05, "high_fcf_yield":   1.00,
            "momentum_quality": 0.90, "contrarian":       0.85,
        },
        "holding_months": 3,
        "blend_alpha":    0.0,
        "min_momentum":  -999.0,
    })

    rng = random.Random(42)   # fixed seed → fully reproducible
    while len(combos) < n:
        # Profile weights: each in [0.3, 2.5], normalised so mean = 1.0
        raw = {k: rng.uniform(0.3, 2.5) for k in _PROFILE_KEYS_ALL}
        mean_w = sum(raw.values()) / len(raw)
        w = {k: round(v / mean_w, 3) for k, v in raw.items()}

        # Holding period: 1M is noisy/costly, 3M balanced, 6M lower turnover
        holding = rng.choice([1, 3, 3, 6])   # 3M weighted 2× more likely

        # Blend alpha: concentrate search around useful values
        blend = round(rng.choice([
            0.0, 0.0,             # pure fundamental (2× weight)
            0.2, 0.3, 0.4, 0.5,  # hybrid zone
            0.6, 0.7,             # momentum-leaning hybrid
            1.0,                  # pure momentum
        ]), 2)

        # Momentum gate: -999=off, 0=no negative, 5,10,15=positive threshold
        gate = rng.choice([-999.0, -999.0, 0.0, 0.0, 5.0, 10.0, 15.0])

        combos.append({
            "weights":        w,
            "holding_months": holding,
            "blend_alpha":    blend,
            "min_momentum":   gate,
        })

    return combos


def _run_weight_optimizer(
    overall_tickers:    list[str],
    conviction_tickers: list[str],
    prices: dict[str, dict[str, float]],
    spx:    dict[str, float],
    raw_fits: dict[str, dict[str, float]],
    n_combos: int = 120,
    top_n: int = 15,
) -> list[dict]:
    """
    Run n_combos multi-dimensional search combinations and return top-5 by CAGR.

    Each combo varies: profile weights + holding period + blend alpha + momentum gate.
    Returns list of result dicts sorted by CAGR descending:
      {
        "rank":          int,
        "weights":       {profile: weight},
        "holding_months":int,
        "blend_alpha":   float,
        "min_momentum":  float,
        "cagr":          float,
        "excess":        float,
        "sharpe":        float,
        "maxdd":         float,
        "win_rate":      float,
        "result":        full _run_monthly_backtest result dict,
      }
    """
    combos = _generate_optimizer_combos(n_combos)
    scored: list[dict] = []

    all_candidates = list(dict.fromkeys(overall_tickers + conviction_tickers))

    for combo in combos:
        w       = combo["weights"]
        hold    = combo["holding_months"]
        alpha   = combo["blend_alpha"]
        gate    = combo["min_momentum"]

        # Run backtest with this combo's unique combination of all 4 dimensions
        res = _run_monthly_backtest(
            all_candidates, prices, spx,
            holding_months=hold,
            top_n=top_n,
            start_year=_BT_START, end_year=_BT_END,
            ranking_method="fundamental",
            raw_fits=raw_fits,
            fund_weights=w,
            blend_alpha=alpha,
            min_momentum=gate,
        )
        s = res.get("summary", {})
        if not s:
            continue
        scored.append({
            "weights":        w,
            "holding_months": hold,
            "blend_alpha":    alpha,
            "min_momentum":   gate,
            "cagr":           s.get("cagr_port", 0.0),
            "excess":         s.get("excess",    0.0),
            "sharpe":         s.get("sharpe",    0.0),
            "maxdd":          s.get("maxdd",     0.0),
            "win_rate":       s.get("win_rate",  0.0),
            "result":         res,
        })

    # Sort by CAGR descending, take top 5
    scored.sort(key=lambda x: x["cagr"], reverse=True)
    top5 = scored[:5]
    for i, item in enumerate(top5):
        item["rank"] = i + 1
    return top5


def _fmt_weights(w: dict[str, float]) -> str:
    """Format a weights dict as a compact readable formula string."""
    short = {
        "deep_value":       "DV",
        "net_net":          "NN",
        "buffett_quality":  "BQ",
        "quality_value":    "QV",
        "dividend_growth":  "DIV",
        "high_fcf_yield":   "FCF",
        "momentum_quality": "MQ",
        "contrarian":       "CON",
    }
    return " · ".join(
        f"{short.get(k, k)} ×{v:.2f}"
        for k, v in sorted(w.items(), key=lambda x: -x[1])
    )


def _build_backtest_section(
    overall_tickers:    list[str],
    conviction_tickers: list[str],
    prices: dict[str, dict[str, float]],
    spx:    dict[str, float],
    raw_fits: dict[str, dict[str, float]] | None = None,
    top5_strategies: list[dict] | None = None,
) -> str:
    """
    Build the backtest section with three axes of tabs:
     - Ranking method: Momentum / Fundamental
     - Holding period: 1M / 3M / 6M / 1Y
     - Portfolio size: Top 5 / Top 10 / Top 15 / Top 20
     - Two standard strategies: Top Overall and Top Convictions
     - Up to 5 optimised weight strategies (from weight optimizer)
    """
    if not prices or not spx:
        return ""

    ranking_configs = [
        ("momentum",    "Momentum",    "RM",  "#3b82d4"),
        ("fundamental", "Fundamental", "RF",  "#7c3aed"),
    ]

    holding_configs = [
        (1,  "1 Month",  "1M"),
        (3,  "3 Months", "3M"),
        (6,  "6 Months", "6M"),
        (12, "1 Year",   "1Y"),
    ]

    portfolio_configs = [
        (5,  "Top 5",  "P5"),
        (10, "Top 10", "P10"),
        (15, "Top 15", "P15"),
        (20, "Top 20", "P20"),
    ]

    strategies = [
        ("overall",     overall_tickers,    "Top Overall", "#3b82d4"),
        ("convictions", conviction_tickers, "Top Convictions", "#7c3aed"),
    ]

    # Run all simulations: ranking × strategy × holding_months × top_n
    # results[rank_method][strat_key][hm][top_n] = result dict
    results: dict[str, dict[str, dict[int, dict[int, dict]]]] = {}
    for rk_method, _rklabel, _rktag, _rkcolour in ranking_configs:
        results[rk_method] = {}
        for strat_key, tickers, _label, _colour in strategies:
            results[rk_method][strat_key] = {}
            for hm, _hlabel, _htag in holding_configs:
                results[rk_method][strat_key][hm] = {}
                for top_n, _plabel, _ptag in portfolio_configs:
                    res = _run_monthly_backtest(
                        tickers, prices, spx, holding_months=hm, top_n=top_n,
                        start_year=_BT_START, end_year=_BT_END,
                        ranking_method=rk_method,
                        raw_fits=raw_fits,
                    )
                    results[rk_method][strat_key][hm][top_n] = res

    def _render_strategy_card(strat_key, tickers, slabel, scolour, hm, hlabel, top_n, plabel, rk_method="momentum"):
        res = results[rk_method][strat_key][hm][top_n]
        s   = res.get("summary", {})
        yr  = res.get("yearly",  [])
        mo  = res.get("monthly", [])
        if not s:
            return f"""
            <div style="flex:1;min-width:280px;background:#f7f8fa;border:1px solid #e5e7eb;
                        border-radius:10px;padding:16px;opacity:0.5">
              <div style="font-weight:700;color:{scolour};margin-bottom:8px">{slabel}</div>
              <div style="font-size:12px;color:#9ca3af">Not enough data.</div>
            </div>"""

        cp  = s.get("cagr_port", 0.0)
        cb  = s.get("cagr_bm",   0.0)
        exc = s.get("excess",    0.0)
        sh  = s.get("sharpe",    0.0)
        dd  = s.get("maxdd",     0.0)
        wr  = s.get("win_rate",  0.0)
        nm  = s.get("n_months",  0)
        cc  = _return_colour(cp)
        ec  = _return_colour(exc)
        sp  = "+" if cp  >= 0 else ""
        se  = "+" if exc >= 0 else ""
        sb  = "+" if cb  >= 0 else ""

        port_eq = res.get("port_equity", [10000.0])
        bm_eq   = res.get("bm_equity",   [10000.0])
        pf      = res.get("port_final",  port_eq[-1])
        bf      = res.get("bm_final",    bm_eq[-1])
        pf_c    = _return_colour(pf - 10000)

        res_hold = res.get("holding_months", hm)
        equity_svg    = _bt_equity_svg(port_eq, bm_eq, scolour, start_year=_BT_START, holding_months=res_hold)
        yearly_chart  = _bt_yearly_chart(yr, slabel)
        monthly_dtail = _bt_monthly_detail(mo)

        return f"""
            <div style="width:100%;background:#fff;border:1px solid #e5e7eb;
                        border-left:4px solid {scolour};border-radius:10px;padding:18px 20px">
              <div style="font-size:13px;font-weight:800;color:{scolour};margin-bottom:14px;
                          letter-spacing:.02em">{slabel} &nbsp;<span style="font-size:11px;font-weight:500;color:#9ca3af">· hold {hlabel} · {plabel} picks · {nm} trades · non-overlapping</span></div>

              <!-- $10,000 final value box -->
              <div class="bt-val-box" style="display:flex;gap:12px;margin-bottom:16px;padding:14px 16px;
                          background:#f7f8fa;border-radius:10px;border:1px solid #e5e7eb;flex-wrap:wrap">
                <div class="bt-val-item" style="flex:1;min-width:130px">
                  <div style="font-size:10px;color:#57606a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">
                    $10,000 invested Jan 2019 →
                  </div>
                  <div style="font-size:26px;font-weight:900;color:{pf_c}">${pf:,.0f}</div>
                  <div style="font-size:11px;color:#57606a;margin-top:2px">
                    {slabel} &nbsp;·&nbsp; CAGR <strong style="color:{pf_c}">{sp}{cp:.1f}%/yr</strong>
                  </div>
                </div>
                <div class="bt-val-item" style="flex:1;min-width:130px;border-left:1px solid #e5e7eb;padding-left:14px">
                  <div style="font-size:10px;color:#57606a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">
                    vs S&amp;P 500 same period
                  </div>
                  <div style="font-size:26px;font-weight:900;color:#57606a">${bf:,.0f}</div>
                  <div style="font-size:11px;color:#57606a;margin-top:2px">
                    S&amp;P 500 &nbsp;·&nbsp; CAGR <strong>{sb}{cb:.1f}%/yr</strong>
                  </div>
                </div>
                <div class="bt-val-item" style="flex:1;min-width:100px;border-left:1px solid #e5e7eb;padding-left:14px;display:flex;align-items:center">
                  <div>
                    <div style="font-size:10px;color:#57606a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">Difference</div>
                    <div style="font-size:22px;font-weight:900;color:{_return_colour(pf - bf)}">${pf - bf:+,.0f}</div>
                  </div>
                </div>
              </div>

              <!-- Equity curve -->
              <div style="margin-bottom:8px">
                <div style="font-size:10px;font-weight:700;color:#8d96a0;text-transform:uppercase;
                            letter-spacing:.06em;margin-bottom:6px">Equity Curve — $10,000 start</div>
                <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px;font-size:10px">
                  <span style="color:{scolour};font-weight:700">— {slabel}</span>
                  <span style="color:#9ca3af">- - - S&amp;P 500</span>
                </div>
                <div class="bt-eq-svg">{equity_svg}</div>
              </div>

              <!-- KPI row -->
              <div class="bt-kpi-row" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
                <div style="flex:1;min-width:75px;background:#f7f8fa;border-radius:8px;padding:8px 10px;text-align:center">
                  <div style="font-size:18px;font-weight:800;color:{cc}">{sp}{cp:.1f}%</div>
                  <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">CAGR</div>
                </div>
                <div style="flex:1;min-width:75px;background:#f7f8fa;border-radius:8px;padding:8px 10px;text-align:center">
                  <div style="font-size:18px;font-weight:800;color:{ec}">{se}{exc:.1f}%</div>
                  <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">vs S&amp;P</div>
                </div>
                <div style="flex:1;min-width:75px;background:#f7f8fa;border-radius:8px;padding:8px 10px;text-align:center">
                  <div style="font-size:18px;font-weight:800">{sh:.2f}</div>
                  <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">Sharpe</div>
                </div>
                <div style="flex:1;min-width:75px;background:#f7f8fa;border-radius:8px;padding:8px 10px;text-align:center">
                  <div style="font-size:18px;font-weight:800;color:#e11d48">{dd:.1f}%</div>
                  <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">Max DD</div>
                </div>
                <div style="flex:1;min-width:75px;background:#f7f8fa;border-radius:8px;padding:8px 10px;text-align:center">
                  <div style="font-size:18px;font-weight:800">{wr:.0f}%</div>
                  <div style="font-size:9px;color:#57606a;text-transform:uppercase;letter-spacing:.05em">Win Rate</div>
                </div>
              </div>
              {yearly_chart}
              {monthly_dtail}
            </div>"""

    # ── Build tab panels: ranking × holding × portfolio_size × strategy ──────
    # Default: Momentum ranking + 3M holding + Top 5, first strategy visible
    rank_tab_blocks = {}   # rk_tag -> html string of all panels for that ranking
    for rk_method, rklabel, rktag, rkcolour in ranking_configs:
        tab_blocks = ""
        for hm, hlabel, htag in holding_configs:
            for top_n, plabel, ptag in portfolio_configs:
                panel_id   = f"bt-panel-{rktag}-{htag}-{ptag}"
                is_default = (hm == 3 and top_n == 5)

                strat_btns = ""
                strat_panels = ""
                for si, (strat_key, tickers, slabel, scolour) in enumerate(strategies):
                    sp_id    = f"{panel_id}-s{si}"
                    s_active = si == 0
                    s_btn_sty = (
                        f"background:{scolour};color:#fff;border-color:{scolour}"
                        if s_active else
                        "background:#fff;color:#374151;border-color:#e5e7eb"
                    )
                    strat_btns += (
                        f'<button class="bt-strat-btn" '
                        f'data-panel="{panel_id}" data-strat="{sp_id}" '
                        f'onclick="btSwitchStrat(\'{panel_id}\',\'{sp_id}\')" '
                        f'style="padding:6px 18px;font-size:12px;font-weight:700;border:1px solid;'
                        f'border-radius:20px;cursor:pointer;{s_btn_sty}">'
                        f'<span style="width:8px;height:8px;border-radius:50%;background:{scolour};'
                        f'display:inline-block;margin-right:6px;vertical-align:middle"></span>'
                        f'{slabel}</button>'
                    )
                    card_html = _render_strategy_card(
                        strat_key, tickers, slabel, scolour, hm, hlabel, top_n, plabel,
                        rk_method=rk_method,
                    )
                    strat_panels += f"""
            <div class="bt-strat-panel" id="{sp_id}" style="{'display:block' if s_active else 'display:none'}">
              {card_html}
            </div>"""

                tab_blocks += f"""
        <div class="bt-tab-panel bt-pt-panel bt-rk-{rktag}" id="{panel_id}" style="{'display:block' if is_default else 'display:none'}">
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center">
            <span style="font-size:11px;color:#57606a;font-weight:600;margin-right:4px">Strategy:</span>
            {strat_btns}
          </div>
          {strat_panels}
        </div>"""

        rank_tab_blocks[rktag] = tab_blocks

    # ── Ranking method tab buttons (Momentum / Fundamental) ───────────────────
    rank_btns = ""
    for rk_method, rklabel, rktag, rkcolour in ranking_configs:
        is_first = rktag == ranking_configs[0][2]
        active = (
            f"background:{rkcolour};color:#fff;border-color:{rkcolour}"
            if is_first else
            "background:#fff;color:#374151;border-color:#e5e7eb"
        )
        desc = (
            "Re-ranks picks by 12M trailing price return at each entry"
            if rk_method == "momentum" else
            "Re-ranks picks by cross-profile ProfileFit score at each entry"
        )
        rank_btns += (
            f'<button class="bt-rk-btn" data-rk="{rktag}" '
            f'onclick="btSwitchRank(\'{rktag}\')" '
            f'style="padding:8px 22px;font-size:12px;font-weight:700;border:1px solid;'
            f'border-radius:20px;cursor:pointer;{active}" title="{desc}">'
            f'{rklabel} Ranking</button>'
        )

    # ── Holding period tab buttons ─────────────────────────────────────────────
    hold_btns = ""
    for hm, hlabel, htag in holding_configs:
        active = "background:#3b82d4;color:#fff;border-color:#3b82d4" if hm == 3 else "background:#fff;color:#374151;border-color:#e5e7eb"
        hold_btns += (
            f'<button class="bt-tab-btn bt-hold-btn" data-hold="{htag}" '
            f'onclick="btSwitchHold(\'{htag}\')" '
            f'style="padding:7px 20px;font-size:12px;font-weight:700;border:1px solid;'
            f'border-radius:20px;cursor:pointer;{active}">'
            f'{htag} &nbsp;<span style="font-size:10px;font-weight:400">({hlabel})</span></button>'
        )

    # ── Portfolio size tab buttons ─────────────────────────────────────────────
    port_btns = ""
    for top_n, plabel, ptag in portfolio_configs:
        active = "background:#059669;color:#fff;border-color:#059669" if top_n == 5 else "background:#fff;color:#374151;border-color:#e5e7eb"
        port_btns += (
            f'<button class="bt-pt-btn" data-pt="{ptag}" '
            f'onclick="btSwitchPt(\'{ptag}\')" '
            f'style="padding:6px 16px;font-size:12px;font-weight:700;border:1px solid;'
            f'border-radius:20px;cursor:pointer;{active}">'
            f'{plabel}</button>'
        )

    # Combine all ranking blocks — only momentum visible by default
    all_tab_blocks = ""
    for rk_method, rklabel, rktag, rkcolour in ranking_configs:
        is_first = rktag == ranking_configs[0][2]
        all_tab_blocks += f"""
      <div class="bt-rk-panel" id="bt-rk-panel-{rktag}" style="{'display:block' if is_first else 'display:none'}">
        {rank_tab_blocks[rktag]}
      </div>"""

    # ── Build Weight Optimizer section ────────────────────────────────────────
    opt_section_html = _build_optimizer_section(top5_strategies or [])

    return f"""
    <span class="section-anchor" id="backtest"></span>
    <div class="section">
      <div class="profile-badge" style="background:#1f232811;border-color:#1f232844;color:#1f2328">
        BT &nbsp; Backtest
      </div>
      <div class="section-title">Walk-Forward Backtest — Non-Overlapping Simulation {_BT_RANGE}</div>
      <div class="section-sub">
        Simulates investing <strong>$10,000</strong> starting January 2019, rebalancing at each interval.
        Select <strong>ranking method</strong>, <strong>holding period</strong> and <strong>portfolio size</strong> below.
        Compare against the same $10,000 held in the <strong>S&amp;P 500</strong>.
        <span style="color:#d97706;font-weight:700">⚠ Look-ahead bias applies</span> —
        see explanation below.
      </div>

      <!-- How it works explanation box -->
      <details style="margin-bottom:20px">
        <summary style="cursor:pointer;font-size:12px;font-weight:700;color:#3b82d4;
                        padding:8px 12px;background:#eff6ff;border:1px solid #bfdbfe;
                        border-radius:8px;user-select:none;list-style:none">
          &#9654;&nbsp; How does this backtest work? (click to expand)
        </summary>
        <div style="background:#f7f8fa;border:1px solid #e5e7eb;border-top:none;
                    border-radius:0 0 8px 8px;padding:20px 22px;font-size:13px;
                    line-height:1.75;color:#374151">

          <!-- ── 1. Overview ── -->
          <p style="margin:0 0 6px;font-size:15px;font-weight:800;color:#1f2328">What is this?</p>
          <p style="margin:0 0 16px">
            A <strong>walk-forward simulation</strong> that asks: <em>"If I had used this screener's
            top picks every rebalance period starting January 2019, how much would $10,000 be worth
            today — and how does that compare to just buying the S&amp;P 500?"</em><br>
            Every trade uses only price data that was <strong>available at the time of entry</strong>.
            No future prices are peeked at inside the simulation window.
          </p>

          <!-- ── 2. Data sources ── -->
          <p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#1f2328">Data sources</p>
          <ul style="margin:0 0 16px;padding-left:20px;font-size:12px;color:#374151">
            <li style="margin-bottom:5px">
              <strong>Stock prices:</strong> daily adjusted closing prices from
              <strong>Yahoo Finance</strong> (via yfinance), fetched from <strong>January 2020</strong>
              to today, cached locally in DuckDB (<code>ohlcv_cache</code> table).
              "Adjusted" means splits and dividends are already baked in — the price series reflects
              total return, not just price appreciation.
            </li>
            <li style="margin-bottom:5px">
              <strong>S&amp;P 500 benchmark:</strong> <code>^GSPC</code> daily closes from Yahoo
              Finance, same date range.
            </li>
            <li style="margin-bottom:5px">
              <strong>Candidate universe:</strong> the <strong>top ~200 tickers</strong> from today's
              screener run — top 100 from the "Top Overall" ranking + top 100 from the "Top Convictions"
              ranking, deduplicated. These are the stocks the screener <em>currently</em> considers best-valued.
            </li>
            <li>
              <strong>Entry / exit prices:</strong> the adjusted closing price on the <strong>first
              available trading day</strong> of the target month (scanned forward up to 10 calendar
              days to skip weekends and holidays).
            </li>
          </ul>

          <!-- ── 3. Exact algorithm ── -->
          <p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#1f2328">Exact algorithm — step by step</p>
          <ol style="margin:0 0 16px;padding-left:20px;font-size:12px;color:#374151">
            <li style="margin-bottom:8px">
              <strong>Build the trade schedule.</strong> Starting from <strong>January 2019</strong>,
              generate non-overlapping entry dates spaced exactly <em>holding_months</em> apart
              (e.g. Jan → Apr → Jul → Oct for 3M). The simulation ends at <strong>December 2025</strong>
              (or when no exit price is available).
            </li>
            <li style="margin-bottom:8px">
              <strong>Dynamic momentum re-rank.</strong> At each entry date, take the candidate universe
              (the 40 screener tickers) and sort them by their <strong>12-month trailing price return</strong>
              calculated using only data up to that entry date — specifically:
              <code>momentum = price_on_entry_date / price_12_months_earlier − 1</code>.
              Tickers with no historical price data fall to the bottom. This ranking changes every period —
              the 5 picks in Jan 2019 may be completely different from the 5 picks in Jul 2023.
            </li>
            <li style="margin-bottom:8px">
              <strong>Pick top 5.</strong> Select the first 5 tickers from the momentum-ranked list
              that have a valid closing price on both the entry date <em>and</em> the exit date.
              Each gets an equal <strong>20% of total portfolio capital</strong> (equal-weighted).
            </li>
            <li style="margin-bottom:8px">
              <strong>Calculate trade return.</strong>
              For each pick: <code>stock_return = exit_price / entry_price − 1</code>.
              Portfolio return = simple average of the 5 individual returns (equal weight).
            </li>
            <li style="margin-bottom:8px">
              <strong>Compound capital.</strong> New capital = previous capital × (1 + portfolio_return).
              Starting capital: <strong>$10,000</strong>. 100% is reinvested every trade — no cash drag.
            </li>
            <li style="margin-bottom:8px">
              <strong>Benchmark comparison.</strong> The S&amp;P 500 return is measured over the
              <em>exact same entry–exit window</em> as each trade, and compounded separately the same way.
              The annual bar chart uses <strong>true calendar-year returns</strong> (Jan 1 → Jan 1)
              for the S&amp;P 500, independent of holding period.
            </li>
            <li style="margin-bottom:0">
              <strong>Aggregate.</strong> CAGR, Sharpe, max drawdown, and win rate are computed across
              all trades in the simulation. A trade is a <em>Win</em> if the portfolio return exceeded
              the S&amp;P 500 return over that exact same window.
            </li>
          </ol>

          <!-- ── 4. Concrete example ── -->
          <p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#1f2328">
            Concrete example — 3M tab (4 trades/year, 20 total over 5 years)
          </p>
          <div style="overflow-x:auto;margin-bottom:6px">
            <table style="width:100%;border-collapse:collapse;font-size:12px">
              <thead>
                <tr style="background:#1f2328;color:#fff">
                  <th style="padding:8px 10px;text-align:left">#</th>
                  <th style="padding:8px 10px;text-align:left">Entry</th>
                  <th style="padding:8px 10px;text-align:left">Exit</th>
                  <th style="padding:8px 10px;text-align:left">Top 5 picks (by 12M momentum at entry)</th>
                  <th style="padding:8px 10px;text-align:left">Portfolio return</th>
                  <th style="padding:8px 10px;text-align:left">S&amp;P 500 same window</th>
                  <th style="padding:8px 10px;text-align:left">Capital after exit</th>
                </tr>
              </thead>
              <tbody>
                <tr style="background:#fff">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">2 Jan 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1 Apr 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;font-size:11px;color:#57606a">Tickers A, B, C, D, E — highest 12M momentum on 2 Jan 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;color:#16a34a">e.g. +9.6%</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">e.g. +5.8%</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>$10,960</strong></td>
                </tr>
                <tr style="background:#f7f8fa">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">2</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1 Apr 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1 Jul 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;font-size:11px;color:#57606a">Re-ranked on 1 Apr 2019 — likely different 5 tickers</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;color:#16a34a">e.g. +3.6%</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">e.g. +8.5%</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>$11,355</strong></td>
                </tr>
                <tr style="background:#fff">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">3</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1 Jul 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1 Oct 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;font-size:11px;color:#57606a">Re-ranked on 1 Jul 2019</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;color:#e11d48">e.g. −1.2%</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">e.g. +0.2%</td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>$11,219</strong></td>
                </tr>
                <tr style="background:#f7f8fa">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5" colspan="6" style="font-style:italic;color:#57606a;padding:7px 10px">… continues every 3 months through 2025 (20 trades total) …</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p style="margin:0 0 16px;font-size:11px;color:#57606a">
            Every new trade re-ranks the universe from scratch using momentum only up to that exact entry date.
            Capital always equals 100% of the previous exit value — no cash held between trades.
          </p>

          <!-- ── 5. Metrics defined ── -->
          <p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#1f2328">Metrics — exact definitions</p>
          <div style="overflow-x:auto;margin-bottom:16px">
            <table style="width:100%;border-collapse:collapse;font-size:12px">
              <thead>
                <tr style="background:#f0f2f5">
                  <th style="padding:7px 10px;text-align:left;font-weight:700;width:22%">Metric</th>
                  <th style="padding:7px 10px;text-align:left;font-weight:700">How it is calculated</th>
                </tr>
              </thead>
              <tbody>
                <tr style="background:#fff">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>CAGR</strong></td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">
                    Compound annual growth of the $10,000 equity curve.
                    Formula: <code>(final_capital / 10000) ^ (1 / n_years) − 1</code>, where
                    <em>n_years = n_trades × holding_months / 12</em>.
                  </td>
                </tr>
                <tr style="background:#f7f8fa">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>vs S&amp;P 500</strong></td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">
                    CAGR of the portfolio minus CAGR of the S&amp;P 500 computed over the same
                    non-overlapping trade windows. A positive number means the strategy outperformed.
                  </td>
                </tr>
                <tr style="background:#fff">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>Sharpe ratio</strong></td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">
                    Mean per-trade return divided by the standard deviation of per-trade returns,
                    then annualised: <code>sharpe = (avg_return / std_return) × √(trades_per_year)</code>.
                    Higher = better risk-adjusted return. No risk-free rate subtracted (conservative).
                  </td>
                </tr>
                <tr style="background:#f7f8fa">
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>Max drawdown</strong></td>
                  <td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">
                    Largest peak-to-trough drop in the compounded equity curve across all trades.
                    Shown as a negative percentage. E.g. −25% means at worst the portfolio fell
                    25% from its peak before recovering.
                  </td>
                </tr>
                <tr style="background:#fff">
                  <td style="padding:7px 10px"><strong>Win rate</strong></td>
                  <td style="padding:7px 10px">
                    % of individual stock picks (across all trades) whose return in that exact
                    entry–exit window exceeded the S&amp;P 500's return over the same window.
                    50% = matched the index on average. Above 50% = more picks beat the market
                    than lost to it.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- ── 6. Holding period tabs ── -->
          <p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#1f2328">Holding period tabs — what changes</p>
          <div style="overflow-x:auto;margin-bottom:16px">
            <table style="width:100%;border-collapse:collapse;font-size:12px">
              <thead>
                <tr style="background:#f0f2f5">
                  <th style="padding:7px 10px;text-align:left;font-weight:700">Tab</th>
                  <th style="padding:7px 10px;text-align:left;font-weight:700">Hold</th>
                  <th style="padding:7px 10px;text-align:left;font-weight:700">Trades/yr</th>
                  <th style="padding:7px 10px;text-align:left;font-weight:700">Total trades (5 yrs)</th>
                  <th style="padding:7px 10px;text-align:left;font-weight:700">Entry months</th>
                  <th style="padding:7px 10px;text-align:left;font-weight:700">Notes</th>
                </tr>
              </thead>
              <tbody>
                <tr style="background:#fff"><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>1M</strong></td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">1 month</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">12</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">~60</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">Every month</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;color:#57606a;font-size:11px">Most trades; highest churn; most sensitive to short-term noise</td></tr>
                <tr style="background:#f7f8fa"><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>3M</strong></td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">3 months</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">4</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">~20</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">Jan, Apr, Jul, Oct</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;color:#57606a;font-size:11px">Default view; quarterly rebalance; aligns with earnings cycles</td></tr>
                <tr style="background:#fff"><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5"><strong>6M</strong></td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">6 months</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">2</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">~10</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5">Jan, Jul</td><td style="padding:7px 10px;border-bottom:1px solid #f0f2f5;color:#57606a;font-size:11px">Semi-annual; fewer data points; less transaction friction</td></tr>
                <tr style="background:#f7f8fa"><td style="padding:7px 10px"><strong>1Y</strong></td><td style="padding:7px 10px">12 months</td><td style="padding:7px 10px">1</td><td style="padding:7px 10px">5</td><td style="padding:7px 10px">Jan only</td><td style="padding:7px 10px;color:#57606a;font-size:11px">Annual; only 5 trades total — very low statistical significance</td></tr>
              </tbody>
            </table>
          </div>

          <!-- ── 7. Biases and limitations ── -->
          <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:14px 16px;margin-bottom:0">
            <p style="margin:0 0 8px;font-weight:700;color:#92400e;font-size:13px">
              ⚠ Biases &amp; limitations — read before drawing conclusions
            </p>
            <ul style="margin:0;padding-left:18px;font-size:12px;color:#78350f;line-height:1.8">
              <li>
                <strong>Look-ahead bias (candidate pool):</strong> the ~200 stocks in the universe are
                today's screener picks (top 100 Overall + top 100 Convictions, deduplicated).
                A company scoring highly <em>now</em> may have had weak fundamentals in 2019,
                but it's still included in every historical trade.
                This makes results <em>optimistic</em>. The within-simulation ranking (momentum) is
                clean — it uses only historical prices available at each entry date.
              </li>
              <li>
                <strong>Survivorship bias:</strong> only companies that are still listed and
                accessible through Yahoo Finance today are included. Companies that went bankrupt,
                were delisted, or were acquired since 2019 are absent. Real performance would
                include some of those losses.
              </li>
              <li>
                <strong>No transaction costs:</strong> no commissions, bid-ask spreads, market
                impact, or taxes are modelled. Real returns would be lower, especially for 1M
                (12 trades/year) which involves the most trading.
              </li>
              <li>
                <strong>Equal weighting:</strong> $2,000 per stock regardless of liquidity or
                position sizing logic. In practice you might weight differently.
              </li>
              <li>
                <strong>Small sample (especially 1Y tab):</strong> 5 annual trades is not
                statistically meaningful. Even random stock picks could outperform over 5 years
                by chance. Do not treat the 1Y results as proof of anything.
              </li>
              <li>
                <strong>Interpretation:</strong> treat this as a <em>directional plausibility check</em>
                — does this screener tend to identify companies that do better than average? — not
                as a reliable forecast of future returns.
              </li>
            </ul>
          </div>

        </div>
      </details>

      {opt_section_html}

      <!-- Ranking method tabs -->
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;align-items:center">
        <span style="font-size:11px;color:#57606a;font-weight:700;align-self:center;margin-right:4px;text-transform:uppercase;letter-spacing:.05em">Ranking:</span>
        {rank_btns}
      </div>
      <div style="font-size:11px;color:#9ca3af;margin-bottom:14px;padding-left:2px">
        <strong>Momentum</strong> = at each rebalance, picks the tickers with the highest 12-month trailing return. &nbsp;|&nbsp;
        <strong>Fundamental</strong> = at each rebalance, picks by cross-profile ProfileFit score (quality + valuation, no price momentum).
      </div>

      <!-- Holding period tabs -->
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        {hold_btns}
      </div>

      <!-- Portfolio size tabs -->
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px">
        <span style="font-size:11px;color:#57606a;font-weight:600;align-self:center;margin-right:4px">Portfolio size:</span>
        {port_btns}
      </div>

      {all_tab_blocks}

      <div class="limit-box">
        <strong>Quick reminder — simulation limitations:</strong>
        (1) <strong>Look-ahead bias</strong> — current rankings applied to all past years (optimistic).
        (2) <strong>Survivorship bias</strong> — delisted/failed companies excluded.
        (3) <strong>No costs</strong> — commissions, taxes, slippage not modelled.
        Treat results as <em>directional signal quality indicators</em> only.
      </div>
    </div>"""


# ── Top Convictions section ───────────────────────────────────────────────────

_PROFILE_LABEL_SHORT = {
    "deep_value":       ("DV",  "#3b82d4", "Deep Value"),
    "buffett_quality":  ("BQ",  "#7c3aed", "Buffett Quality"),
    "high_fcf_yield":   ("FCF", "#059669", "High FCF Yield"),
    "quality_value":    ("QV",  "#d97706", "Quality Value"),
    "dividend_growth":  ("DIV", "#0891b2", "Dividend Growth"),
    "net_net":          ("NN",  "#b45309", "Net-Net (NCAV)"),
    "momentum_quality": ("MQ",  "#ea580c", "Momentum+Quality"),
    "contrarian":       ("CON", "#dc2626", "Short Contrarian"),
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

    cards_html = ""
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

        # Profile badges with full label tooltip
        badge_html = " ".join(
            f'<span title="{_PROFILE_LABEL_SHORT[p][2]}" style="display:inline-block;padding:2px 7px;border-radius:4px;'
            f'font-size:11px;font-weight:700;background:{_PROFILE_LABEL_SHORT[p][1]}18;'
            f'color:{_PROFILE_LABEL_SHORT[p][1]};border:1px solid {_PROFILE_LABEL_SHORT[p][1]}44;cursor:default">'
            f'{_PROFILE_LABEL_SHORT[p][0]}</span>'
            for p in profiles
        )

        pos_bar = (
            f'<div class="gauge-wrap"><div class="gauge-track">'
            f'<div class="gauge-fill" style="width:{min(pos_v,100):.1f}%;background:{pc}"></div>'
            f'</div><div class="gauge-pct" style="color:{pc}">{pos_v:.0f}%</div></div>'
            if pos_v is not None else '<span style="color:#9ca3af">—</span>'
        )
        mos_bar = (
            f'<div class="gauge-wrap"><div class="gauge-track">'
            f'<div class="gauge-fill" style="width:{min(mos_v,100):.1f}%;background:{mc}"></div>'
            f'</div><div class="gauge-pct" style="color:{mc}">{mos_v:.0f}%</div></div>'
        )

        _, panel_html, sc_v, sc_c = _why_buy(row, profiles=profiles,
                                              ohlc=_PRICE_DATA.get(tkr) if _PRICE_DATA else None)
        sc_c = sc_c or "#9ca3af"
        why_btn_html = _why_btn(tkr, sc_v, sc_c, ns="conv") if panel_html else ""
        why_panel    = f'<div style="margin-top:10px">{why_btn_html}</div>' if panel_html else ""
        why_exp_div  = (f'<div class="why-row" id="why-{tkr.replace(".", "-")}-conv" style="display:none;margin-top:8px">{panel_html}</div>'
                        if panel_html else "")

        cards_html += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-left:4px solid {conv_colour};
                    border-radius:10px;padding:16px 18px;margin-bottom:12px">
          <!-- header row -->
          <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
            <div>
              <div style="font-weight:800;font-size:15px">{tkr} {_index_badge(tkr)}</div>
              <div style="font-size:12px;color:#57606a">{row.get('Company','')}</div>
              <div style="font-size:11px;color:#57606a;margin-top:2px">{row.get('Sector','') or '—'}</div>
            </div>
            <div style="text-align:right">
              <span style="font-weight:700;font-size:11px;color:{conv_colour};
                           background:{conv_colour}12;border:1px solid {conv_colour}33;
                           border-radius:4px;padding:3px 8px;display:inline-block">{conv_label}</span>
            </div>
          </div>
          <!-- metrics grid -->
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-bottom:10px">
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Price</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('Price',''),2,prefix='$')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Intrinsic Value (DCF)</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('DCF Avg',''),2,prefix='$')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Margin of Safety</div>
              {mos_bar}
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">52-Week Position</div>
              {pos_bar}
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">P/E Ratio</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('P/E',''),1,suffix='x')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">P/FCF Ratio</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('P/FCF',''),1,suffix='x')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Piotroski Score</div>
              <div>{_quality_badge(row.get('Piotroski',''),'piotroski')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">ROIC</div>
              <div>{_quality_badge(row.get('ROIC%',''),'roic')}</div>
            </div>
          </div>
          <!-- profiles row -->
          <div style="margin-bottom:6px">
            <span style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-right:6px">Profiles passed:</span>
            {badge_html}
          </div>
          {why_panel}{why_exp_div}
        </div>"""

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
        <div>{cards_html}</div>
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
    All 8 profiles contribute: Deep Value ×1.3, Net-Net ×1.25, Buffett Quality ×1.2,
    Quality Value ×1.1, Dividend Growth ×1.05, FCF Yield ×1.0,
    Momentum+Quality ×0.9, Contrarian ×0.85.

    Shows top-N with Why-Buy reasoning for each.
    """
    # Profile weights — stricter profiles carry more signal
    # New screens get lower weight: they're complementary, not replacements
    weights = {
        "deep_value":       1.30,
        "buffett_quality":  1.20,
        "quality_value":    1.10,
        "dividend_growth":  1.05,
        "high_fcf_yield":   1.00,
        "net_net":          1.25,  # NCAV is extremely strict — high signal when it passes
        "momentum_quality": 0.90,  # momentum is noisier than pure value
        "contrarian":       0.85,  # contrarian adds colour but lower conviction
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

    cards_html = ""
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
            if pos_v is not None else '<span style="color:#9ca3af">—</span>'
        )

        badge_html = ""
        for pk in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value", "dividend_growth",
                   "net_net", "momentum_quality", "contrarian"):
            if pk in ticker_raw_fits.get(tkr, {}):
                info = _PROFILE_LABEL_SHORT[pk]
                is_p = pk in passes_in
                bg   = info[1] if is_p else "#94a3b8"
                badge_html += (
                    f'<span title="{info[2]}" style="display:inline-block;padding:2px 6px;border-radius:4px;'
                    f'font-size:10px;font-weight:700;background:{bg}18;color:{bg};'
                    f'border:1px solid {bg}44;margin:1px;cursor:default">{info[0]}</span>'
                )

        _, panel_html, _sc_v, _sc_c = _why_buy(row, profiles=passes_in if passes_in else None,
                                                overall_score=score,
                                                ohlc=_PRICE_DATA.get(tkr) if _PRICE_DATA else None,
                                                profile_fits=ticker_raw_fits.get(tkr))
        _sc_c = _sc_c or "#9ca3af"
        why_btn_html = _why_btn(tkr, _sc_v if _sc_v is not None else score, _sc_c, ns="overall") if panel_html else ""
        why_panel    = f'<div style="margin-top:10px">{why_btn_html}</div>' if panel_html else ""
        why_exp_div  = (f'<div class="why-row" id="why-{tkr.replace(".", "-")}-overall" style="display:none;margin-top:8px">{panel_html}</div>'
                        if panel_html else "")

        rank_colour = "#d97706" if i == 0 else ("#3b82d4" if i < 3 else "#57606a")
        border_colour = rank_colour if i < 3 else "#3b82d4"

        cards_html += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-left:4px solid {border_colour};
                    border-radius:10px;padding:16px 18px;margin-bottom:12px">
          <!-- header row -->
          <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
            <div>
              <div style="font-weight:800;font-size:15px">
                <span style="color:{rank_colour};font-weight:900;margin-right:6px">#{i+1}</span>{tkr} {_index_badge(tkr)}
              </div>
              <div style="font-size:12px;color:#57606a">{row.get('Company','')}</div>
              <div style="font-size:11px;color:#57606a;margin-top:2px">{row.get('Sector','') or '—'}</div>
            </div>
            <div style="text-align:right">
              <div style="font-size:11px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">Overall Score</div>
              <div><span style="font-size:28px;font-weight:900;color:{sc}">{score:.0f}</span><span style="font-size:12px;color:#9ca3af"> / 100</span></div>
              <div style="font-size:12px;font-weight:700;color:{mc};margin-top:2px">{grade} <span style="font-size:10px;color:#8d96a0;font-weight:400">{glabel}</span></div>
            </div>
          </div>
          <!-- profiles badges -->
          <div style="margin-bottom:10px">
            <span style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-right:6px">Profiles (hover for name):</span>
            {badge_html}
          </div>
          <!-- metrics grid -->
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-bottom:10px">
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Price</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('Price',''),2,prefix='$')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Intrinsic Value (DCF)</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('DCF Avg',''),2,prefix='$')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Margin of Safety</div>
              {mos_bar}
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">52-Week Position</div>
              {pos_bar}
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">P/E Ratio</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('P/E',''),1,suffix='x')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">P/FCF Ratio</div>
              <div style="font-weight:700;font-size:14px">{_fmt(row.get('P/FCF',''),1,suffix='x')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">Piotroski Score</div>
              <div>{_quality_badge(row.get('Piotroski',''),'piotroski')}</div>
            </div>
            <div style="background:#f7f8fa;border-radius:6px;padding:8px 10px">
              <div style="font-size:10px;color:#57606a;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">ROIC</div>
              <div>{_quality_badge(row.get('ROIC%',''),'roic')}</div>
            </div>
          </div>
          {why_panel}{why_exp_div}
        </div>"""

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
          weighted average across all 8 profiles:
          Deep Value &times;1.3 &nbsp;·&nbsp; Net-Net &times;1.25 &nbsp;·&nbsp; Buffett Quality &times;1.2 &nbsp;·&nbsp;
          Quality Value &times;1.1 &nbsp;·&nbsp; Dividend Growth &times;1.05 &nbsp;·&nbsp; FCF Yield &times;1.0 &nbsp;·&nbsp;
          Momentum+Quality &times;0.9 &nbsp;·&nbsp; Contrarian &times;0.85.
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
        <div>{cards_html}</div>
      </div>
    </details>"""


# ── Full report builder ───────────────────────────────────────────────────────

def build_full_report(out_path: Path) -> None:
    global _PRICE_DATA
    now = datetime.now().strftime("%d %B %Y, %H:%M")

    # Load index membership sets (SPX / DOW / NDQ) for badges
    _load_index_sets()

    # Load most recent CSV for each profile
    all_profile_rows: dict[str, list[dict]] = {}
    profile_sections: list[str] = []
    n_pass_per_profile: dict[str, int] = {}

    for key in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value", "dividend_growth",
                "net_net", "momentum_quality", "contrarian"):
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
    # Collect unique tickers that will appear in Why-Buy panels:
    #   • top 10 (detailed rows) per profile  — always shown expanded
    #   • all strict-PASS tickers per profile  — shown with Why-Buy button
    # Capped at 120 unique tickers total to keep build time reasonable.
    chart_tickers: set[str] = set()
    for rows in all_profile_rows.values():
        sorted_rows = sorted(rows, key=lambda r: _fv(r.get("ProfileFit","")) or 0, reverse=True)
        # Always include top 10 (detailed view) + all strict passes
        for r in sorted_rows[:10]:
            tkr = r.get("Ticker","").strip()
            if tkr:
                chart_tickers.add(tkr)
        for r in sorted_rows:
            if str(r.get("Passes","")).strip().lower() in ("true","1","yes"):
                tkr = r.get("Ticker","").strip()
                if tkr:
                    chart_tickers.add(tkr)
            if len(chart_tickers) >= 120:
                break

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

    # (legacy backtest CSV no longer used — simulation is built inline)

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

    # ── Overall Top (cross-profile) ───────────────────────────────────────────
    overall_top_section = _build_overall_top(all_profile_rows, top_n=30)

    # ── Top Convictions ───────────────────────────────────────────────────────
    convictions_section = _build_convictions_section(all_profile_rows)

    # ── New backtest: monthly simulation for Top Overall + Top Convictions ────
    # Derive ranked ticker lists from the already-computed sections
    _bt_weights = {
        "deep_value":       1.30,
        "buffett_quality":  1.20,
        "quality_value":    1.10,
        "dividend_growth":  1.05,
        "high_fcf_yield":   1.00,
        "net_net":          1.25,
        "momentum_quality": 0.90,
        "contrarian":       0.85,
    }
    def _overall_score(tkr: str, raw_fits: dict[str, dict[str, float]]) -> float:
        fits  = raw_fits.get(tkr, {})
        w_sum = sum(_bt_weights.get(k, 1.0) for k in fits)
        w_fit = sum(fits[k] * _bt_weights.get(k, 1.0) for k in fits)
        return round(w_fit / w_sum, 1) if w_sum > 0 else 0.0

    # Rebuild ticker → profile fits map (mirrors _build_overall_top logic)
    _bt_raw_fits: dict[str, dict[str, float]] = {}
    _bt_passes:   dict[str, list[str]]        = {}
    for key, rows in all_profile_rows.items():
        for row in rows:
            tkr = row.get("Ticker", "").strip()
            if not tkr: continue
            fit_v = _fv(row.get("ProfileFit", ""))
            if fit_v is None: continue
            if tkr not in _bt_raw_fits:
                _bt_raw_fits[tkr] = {}
                _bt_passes[tkr]   = []
            _bt_raw_fits[tkr][key] = fit_v
            is_pass = str(row.get("Passes", "")).strip().lower() in ("true", "1", "yes")
            if is_pass and key not in _bt_passes[tkr]:
                _bt_passes[tkr].append(key)

    overall_ranked = sorted(
        _bt_raw_fits.keys(),
        key=lambda t: _overall_score(t, _bt_raw_fits),
        reverse=True,
    )
    conviction_ranked = sorted(
        [t for t, ps in _bt_passes.items() if len(ps) >= 2],
        key=lambda t: (-len(_bt_passes[t]), -_overall_score(t, _bt_raw_fits)),
    )

    # Fetch historical prices for all backtest tickers + SPX
    _bt_all_tickers = list(dict.fromkeys(overall_ranked[:100] + conviction_ranked[:100]))
    print(f"  Fetching 5y price history for {len(_bt_all_tickers)} backtest tickers…")
    _bt_prices = _fetch_bt_prices(_bt_all_tickers + ["^GSPC"])
    _bt_spx    = _bt_prices.pop("^GSPC", {})
    print(f"  Backtest prices fetched: {len(_bt_prices)} tickers, {len(_bt_spx)} SPX days.")

    # ── Weight Optimizer — run 100 weight combos, pick top-5 by CAGR ─────────
    print("  Running weight optimizer (120 combos, 4-dimensional search)…")
    top5_opt = _run_weight_optimizer(
        overall_ranked, conviction_ranked,
        _bt_prices, _bt_spx, _bt_raw_fits,
        n_combos=120,
    )
    print(f"  Optimizer done — top CAGR: {top5_opt[0]['cagr']:+.1f}% (Strategy 1)" if top5_opt else "  Optimizer: no results.")

    bt_inner = _build_backtest_section(
        overall_ranked, conviction_ranked, _bt_prices, _bt_spx,
        raw_fits=_bt_raw_fits, top5_strategies=top5_opt,
    )
    if bt_inner:
        bt_section = f"""
        <span class="section-anchor" id="backtest"></span>
        <details class="sec-wrap">
          <summary>
            <div class="sec-hdr">
              <span class="sec-arrow">&#9654;</span>
              <span class="sec-badge" style="background:#1f232818;color:#1f2328">BT</span>
              <span class="sec-title">Backtest vs S&amp;P 500 &mdash; Monthly Simulation {_BT_RANGE}</span>
              <span class="sec-meta">Top Overall &amp; Top Convictions · 1M / 3M / 6M / 1Y hold</span>
            </div>
          </summary>
          <div class="sec-body">{bt_inner}</div>
        </details>"""
    else:
        bt_section = ""

    # ── Magic Formula section ─────────────────────────────────────────────────
    magic_formula_section = _build_magic_formula_section(mf_rows, mf_ts)

    # ── TOC ───────────────────────────────────────────────────────────────────
    toc_links  = '<a href="#overall_top">&#9650; Top Overall</a>'
    toc_links += '<a href="#convictions">&#9733; Top Convictions</a>'
    toc_links += "".join(
        f'<a href="#{k}">{_PROFILE_META[k]["label"]}</a>'
        for k in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value", "dividend_growth",
                  "net_net", "momentum_quality", "contrarian")
    )
    toc_links += f'<a href="#magic_formula">{_PROFILE_META["magic_formula"]["label"]}</a>'
    if dow_rows:   toc_links += '<a href="#dow30">Dow 30 Ranking</a>'
    if bt_section: toc_links += '<a href="#backtest">Backtest vs S&amp;P 500</a>'
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
                Seven-pillar quality-adjusted score (max 100 pts):<br>
                &nbsp;&bull; <strong>Margin of Safety %</strong> &mdash; up to 28 pts &mdash; DCF discount vs current price<br>
                &nbsp;&bull; <strong>FCF Yield</strong> &mdash; up to 24 pts &mdash; free cash flow / market cap<br>
                &nbsp;&bull; <strong>Piotroski F-Score</strong> &mdash; up to 24 pts &mdash; accounting health (0&ndash;9)<br>
                &nbsp;&bull; <strong>ROIC %</strong> &mdash; up to 10 pts &mdash; return on invested capital<br>
                &nbsp;&bull; <strong>Operating Margin %</strong> &mdash; up to 8 pts &mdash; operational efficiency<br>
                &nbsp;&bull; <strong>FCF Growth 3yr CAGR</strong> &mdash; up to 4 pts &mdash; free cash flow growth trend<br>
                &nbsp;&bull; <strong>Dilution penalty</strong> &mdash; up to &minus;4 pts &mdash; penalises &gt;5%/yr share dilution<br>
                Used internally for ranking and as the 30% quality component of ProfileFit.
                All three input fields (<code>Op.Margin%</code>, <code>FCF Growth 3yr%</code>, <code>Dilution%</code>)
                are visible as columns in every profile CSV and in the Why-Buy panel.
              </td>
              <td>CSV columns: <code>Op.Margin%</code>, <code>FCF Growth 3yr%</code>, <code>Dilution%</code>, <code>Score</code></td>
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
            using a weighted average across all 8 profiles
            (Deep Value &times;1.3, Net-Net &times;1.25, Buffett Quality &times;1.2,
            Quality Value &times;1.1, Dividend Growth &times;1.05, FCF Yield &times;1.0,
            Momentum+Quality &times;0.9, Contrarian &times;0.85).
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
<script defer>
/* --- Why-Buy row toggle --- */
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
</script>
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
      <span style="font-size:20px">&#11088;</span>
      <span class="watchlist-title">My Watchlist</span>
      <span class="watchlist-subtitle" id="wl-count">0 companies saved</span>
      <button class="wl-export-btn" onclick="exportWatchlistCSV()" style="margin-left:12px">⬇ Export CSV</button>
    </div>
    <div id="watchlist-body">
      <div class="watchlist-empty">No companies starred yet. Click &#9734; on any row to add.</div>
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
/* --- Why-Buy row toggle --- */
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

/* --- Live Filter Bar (ST12) --- */
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

/* --- Watchlist + localStorage (ST13) --- */
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
    btn.textContent = '\u2606';
    btn.classList.remove('starred');
    btn.title = 'Add to watchlist';
  }} else {{
    wl.push(rowData);
    btn.textContent = '\u2B50';
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
    if (body) body.innerHTML = '<div class="watchlist-empty">No companies starred yet. Click \u2606 on any row to add.</div>';
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
      + ' onclick="removeFromWatchlist(this.dataset.ticker)">\u2B50</button></td>'
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
    btn.textContent = '\u2606';
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
  var csv = header.join(',') + '\\n' + rows.join('\\n');
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
      btn.textContent = '\u2B50';
      btn.classList.add('starred');
      btn.title = 'Remove from watchlist';
    }}
  }});
  renderWatchlist();
}}

/* --- Backtest tab switchers --- */
var _btCurrentHold = '3M';
var _btCurrentPt   = 'P5';
var _btCurrentRank = 'RM';

function _btShowPanel() {{
  /* hide all panels inside the active ranking block only */
  var rkPanel = document.getElementById('bt-rk-panel-' + _btCurrentRank);
  if (!rkPanel) return;
  rkPanel.querySelectorAll('.bt-tab-panel').forEach(function(p) {{
    p.style.display = 'none';
  }});
  var panel = document.getElementById('bt-panel-' + _btCurrentRank + '-' + _btCurrentHold + '-' + _btCurrentPt);
  if (panel) panel.style.display = 'block';
}}

function btSwitchRank(rktag) {{
  document.querySelectorAll('.bt-rk-panel').forEach(function(p) {{
    p.style.display = 'none';
  }});
  var rkPanel = document.getElementById('bt-rk-panel-' + rktag);
  if (rkPanel) rkPanel.style.display = 'block';
  _btCurrentRank = rktag;
  _btShowPanel();
  document.querySelectorAll('.bt-rk-btn').forEach(function(b) {{
    var active = b.getAttribute('data-rk') === rktag;
    var isMom  = rktag === 'RM';
    var ac     = isMom ? '#3b82d4' : '#7c3aed';
    b.style.background  = active ? ac     : '#fff';
    b.style.color       = active ? '#fff' : '#374151';
    b.style.borderColor = active ? ac     : '#e5e7eb';
  }});
}}

function btSwitchHold(htag) {{
  _btCurrentHold = htag;
  _btShowPanel();
  document.querySelectorAll('.bt-hold-btn').forEach(function(b) {{
    var active = b.getAttribute('data-hold') === htag;
    b.style.background  = active ? '#3b82d4' : '#fff';
    b.style.color       = active ? '#fff'    : '#374151';
    b.style.borderColor = active ? '#3b82d4' : '#e5e7eb';
  }});
}}

function btSwitchPt(ptag) {{
  _btCurrentPt = ptag;
  _btShowPanel();
  document.querySelectorAll('.bt-pt-btn').forEach(function(b) {{
    var active = b.getAttribute('data-pt') === ptag;
    b.style.background  = active ? '#059669' : '#fff';
    b.style.color       = active ? '#fff'    : '#374151';
    b.style.borderColor = active ? '#059669' : '#e5e7eb';
  }});
}}

function btSwitchStrat(panelId, stratId) {{
  /* hide all strat panels inside this holding×portfolio panel */
  var parent = document.getElementById(panelId);
  if (!parent) return;
  parent.querySelectorAll('.bt-strat-panel').forEach(function(p) {{
    p.style.display = 'none';
  }});
  var sp = document.getElementById(stratId);
  if (sp) sp.style.display = 'block';
  /* update strat buttons that belong to this panel */
  parent.querySelectorAll('.bt-strat-btn').forEach(function(b) {{
    var active = b.getAttribute('data-strat') === stratId;
    var colour = b.querySelector('span') ? b.querySelector('span').style.background : '#3b82d4';
    b.style.background  = active ? colour : '#fff';
    b.style.color       = active ? '#fff'  : '#374151';
    b.style.borderColor = active ? colour : '#e5e7eb';
  }});
}}

function optSwitchStrat(idx) {{
  /* Show the selected optimizer strategy panel, hide others */
  document.querySelectorAll('.opt-panel').forEach(function(p) {{
    p.style.display = 'none';
  }});
  var panel = document.getElementById('opt-strat-' + idx);
  if (panel) panel.style.display = 'block';

  /* Update button styles */
  var colours = ['#059669','#3b82d4','#7c3aed','#d97706','#dc2626'];
  document.querySelectorAll('.opt-strat-btn').forEach(function(b) {{
    var i = parseInt(b.getAttribute('data-optid'), 10);
    var active = (i === idx);
    var c = colours[i] || '#3b82d4';
    b.style.background  = active ? c    : '#fff';
    b.style.color       = active ? '#fff' : '#374151';
    b.style.borderColor = active ? c    : '#e5e7eb';
  }});
}}

function btToggleTrade(rowId) {{
  var tr = document.getElementById(rowId);
  if (!tr) return;
  tr.style.display = tr.style.display === 'none' ? '' : 'none';
}}

/* --- Initialise on DOMContentLoaded --- */
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
