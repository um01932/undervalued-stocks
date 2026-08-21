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
from datetime import datetime
from pathlib import Path

REPORTS_DIR = Path(__file__).parent.parent / "data" / "reports"

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
}


def _why_buy(row: dict, profile_key: str | None = None, profiles: list[str] | None = None) -> str:
    """
    Generate a plain-English 'Why buy X?' paragraph with real numbers injected.
    Works for both single-profile rows and multi-profile conviction rows.
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

    sentences: list[str] = []

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

    if not sentences:
        return ""

    body = '  '.join(sentences)
    return f"""
    <details class="why">
      <summary>
        <span class="why-arrow">&#9654;</span>
        Why buy {ticker}?
      </summary>
      <div class="why-body">
        <div style="font-size:13px;line-height:1.75;color:#374151">{body}</div>
      </div>
    </details>"""


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

/* Why-buy collapsible ─────────────────────────────────────────────────────── */
details.why { position:static; margin-top:6px; }
details.why summary {
  cursor:pointer; display:inline-flex; align-items:center; gap:6px;
  font-size:11px; font-weight:700; color:#3b82d4; text-transform:uppercase;
  letter-spacing:.07em; list-style:none; user-select:none;
  padding:4px 10px; background:#eff6ff; border:1px solid #bfdbfe;
  border-radius:20px; transition:background .15s; white-space:nowrap;
}
details.why summary::-webkit-details-marker { display:none; }
details.why summary:hover { background:#dbeafe; }
.why-arrow { display:inline-block; transition:transform .2s ease; flex-shrink:0; }
details.why[open] summary .why-arrow { transform:rotate(90deg); }

/* The expanded panel — breaks out of the narrow <td> using absolute positioning */
details.why { position:relative; }
.why-body {
  display:none;
  position:absolute;
  /* align to the left edge of the section card (closest positioned ancestor) */
  left:-160px;          /* rough offset to reach column #1 — overridden by JS below */
  top:calc(100% + 4px);
  width:700px;
  max-width:90vw;
  z-index:50;
  background:#f7f8fa;
  border-left:3px solid #3b82d4;
  border-radius:0 8px 8px 0;
  padding:16px 20px;
  box-shadow:0 4px 24px rgba(0,0,0,.12);
}
details.why[open] .why-body { display:block; }

/* footer */
.footer { text-align:center; font-size:11px; color:#8d96a0;
          border-top:1px solid #e5e7eb; padding-top:20px; margin-top:40px; }

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
    """Render a single company row for any screener table."""
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
    if show_fit and fit_v is not None:
        fc = "#16a34a" if fit_v >= 70 else ("#eab308" if fit_v >= 40 else "#e11d48")
        fit_cell = f'<td class="r" style="width:6%"><span style="font-weight:800;color:{fc}">{fit_v:.0f}</span></td>'

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

    rank_html = f'<span style="font-weight:800;color:{colour};font-size:13px">#{rank}</span>'

    return f"""<tr>
      <td style="width:3%">{rank_html}</td>
      <td style="width:12%">
        <div class="ticker-lbl">{row.get('Ticker','')}</div>
        <div class="company-lbl">{row.get('Company','')}</div>
        <div style="margin-top:2px">{badge}</div>
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
      <td class="r" style="width:6%">{_fmt(row.get('NetDebt/EBITDA',''),2,suffix='x')}</td>
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

    return f"""<tr>
      <td style="color:#6b7280;font-size:12px">{rank}</td>
      <td>
        <span style="font-weight:700;font-size:12px">{row.get('Ticker','')}</span>
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
                             top_n: int = 5) -> str:
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
            <table class="stbl" style="font-size:11.5px">
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
    <div class="section">
      <div class="profile-badge" style="background:{colour}11;border-color:{colour}44;color:{colour}">
        {meta['icon']} &nbsp; {meta['label']}
      </div>
      <div class="section-title">{meta['label']} Screen</div>
      <div class="section-sub">{meta['desc']}</div>
      <div class="ib blue" style="margin-bottom:16px">
        <strong>Fit Score explained:</strong> 0–100, calculated as
        70% proximity to all profile thresholds + 30% composite quality score.
        <strong style="color:#16a34a">PASS</strong> = meets ALL strict criteria.
        <strong style="color:#9ca3af">NEAR</strong> = misses one or more criteria but still ranked.
        <strong style="color:#dc2626">TRAP</strong> = value trap flag (high debt / negative FCF).
        No company is hidden — all {n} ranked companies visible below.
      </div>
      {pills}
      <div style="font-size:13px;font-weight:700;margin-bottom:8px;color:#1f2328">
        Top {min(top_n, n)} — Detailed View
      </div>
      <table class="stbl">
        {_table_header(show_fit=True)}
        <tbody>{top_html}</tbody>
      </table>
      {rest_html}
    </div>"""


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

        # conviction level: 4 profiles = gold, 3 = strong, 2 = moderate
        if n_prof == 4:
            conv_colour, conv_label = "#d97706", "GOLD — 4/4 profiles"
        elif n_prof == 3:
            conv_colour, conv_label = "#16a34a", "HIGH — 3/4 profiles"
        else:
            conv_colour, conv_label = "#3b82d4", "MODERATE — 2/4 profiles"

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

        why = _why_buy(row, profiles=profiles)
        rows_html += f"""<tr>
          <td style="width:11%">
            <div style="font-weight:700;font-size:10px;color:{conv_colour};
                        background:{conv_colour}12;border:1px solid {conv_colour}33;
                        border-radius:4px;padding:2px 6px;display:inline-block">{conv_label}</div>
          </td>
          <td style="width:13%">
            <div style="font-weight:800;font-size:13px">{tkr}</div>
            <div style="font-size:11px;color:#57606a">{row.get('Company','')}</div>
            {why}
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
        </tr>"""

    n_conv = len(ranked)
    top_ticker = ranked[0][0] if ranked else "—"
    top_n      = len(ranked[0][1]) if ranked else 0

    return f"""
    <span class="section-anchor" id="convictions"></span>
    <div class="section" style="border-left:4px solid #d97706">
      <div class="profile-badge" style="background:#d9770611;border-color:#d9770644;color:#d97706">
        &#9733; &nbsp; Top Convictions
      </div>
      <div class="section-title">Top Convictions — Multi-Profile Overlap</div>
      <div class="section-sub">
        Companies that passed <strong>2 or more screener profiles simultaneously</strong>.
        The more profiles a company passes, the stronger the quantitative signal —
        each profile uses a different set of thresholds and a different investment philosophy,
        so overlap is a robust, multi-dimensional buy signal.
      </div>

      <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
                  padding:14px 18px;margin-bottom:18px;font-size:13px;color:#92400e">
        <strong>How to read this table:</strong>
        <strong style="color:#d97706">GOLD (4/4)</strong> = strongest possible signal — passes every single screen.
        <strong style="color:#16a34a">HIGH (3/4)</strong> = passes 3 different philosophical filters.
        <strong style="color:#3b82d4">MODERATE (2/4)</strong> = confirmed by 2 independent approaches.
        Sorted by conviction level, then by Margin of Safety.
      </div>

      <div class="stats-bar">
        <div class="stat-pill">
          <div class="sp-value" style="color:#d97706">{n_conv}</div>
          <div class="sp-label">Multi-Profile Companies</div>
        </div>
        <div class="stat-pill">
          <div class="sp-value" style="color:#d97706">{len([t for t,ps in ranked if len(ps)==3])}</div>
          <div class="sp-label">High Conviction (3+)</div>
        </div>
        <div class="stat-pill">
          <div class="sp-value" style="color:#d97706">{top_ticker}</div>
          <div class="sp-label">Strongest Signal ({top_n} profiles)</div>
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
    </div>"""


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

        why = _why_buy(row, profiles=passes_in if passes_in else None)
        rank_colour = "#d97706" if i == 0 else ("#3b82d4" if i < 3 else "#57606a")

        rows_html += f"""<tr>
          <td style="width:4%;text-align:center">
            <span style="font-weight:800;color:{rank_colour};font-size:15px">#{i+1}</span>
          </td>
          <td style="width:13%">
            <div style="font-weight:800;font-size:14px">{tkr}</div>
            <div style="font-size:11px;color:#57606a">{row.get('Company','')}</div>
            {why}
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
        </tr>"""

    return f"""
    <span class="section-anchor" id="overall_top"></span>
    <div class="section" style="border-left:4px solid #3b82d4">
      <div class="profile-badge" style="background:#3b82d411;border-color:#3b82d444;color:#3b82d4">
        &#9650;&nbsp; Top Overall
      </div>
      <div class="section-title">Top Overall &mdash; Cross-Profile Ranking</div>
      <div class="section-sub">
        Weighted average of ProfileFit scores across all 4 screener profiles.
        Deep Value carries the highest weight (1.3&times;) as the strictest screen.
        Every company in the S&amp;P 500 universe is scored and ranked regardless
        of whether it strictly passes any single profile.
      </div>

      <div class="ib blue" style="margin-bottom:18px">
        <strong>Overall Score (0&ndash;100)</strong> =
        weighted average of
        (Deep Value &times;1.3 + Buffett Quality &times;1.2 + Quality Value &times;1.1 + FCF Yield &times;1.0)
        normalised by the profiles the company appears in.
        &nbsp;Profile badges:
        <strong>filled colour</strong> = strict PASS &nbsp;|&nbsp;
        <strong style="color:#94a3b8">grey</strong> = ranked but did not strictly pass.
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
    </div>"""


# ── Full report builder ───────────────────────────────────────────────────────

def build_full_report(out_path: Path) -> None:
    now = datetime.now().strftime("%d %B %Y, %H:%M")

    # Load most recent CSV for each profile
    all_profile_rows: dict[str, list[dict]] = {}
    profile_sections: list[str] = []
    n_pass_per_profile: dict[str, int] = {}

    for key in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value"):
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

    # Dow 30
    dow_path = _most_recent("*_dow30_ranking.csv")
    dow_rows: list[dict] = []
    dow_ts = "—"
    if dow_path:
        dow_rows = _load_csv(dow_path)
        dow_ts = dow_path.stem[:15]
        try: dow_ts = datetime.strptime(dow_ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
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
        <div class="section">
          <div class="profile-badge" style="background:#0891b211;border-color:#0891b244;color:#0891b2">
            D30 &nbsp; Dow Jones 30
          </div>
          <div class="section-title">Dow Jones 30 — 52-Week Ranking</div>
          <div class="section-sub">All 30 blue-chip companies ranked by proximity to 52-week low.
            Green = near annual low (best opportunity). Red = near annual high. Data run: {dow_ts}.</div>
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
        </div>"""

    bt_section = _build_backtest_section(bt_rows, bt_ts) if bt_rows else ""

    # ── Overall Top (cross-profile) ───────────────────────────────────────────
    overall_top_section = _build_overall_top(all_profile_rows, top_n=10)

    # ── Top Convictions ───────────────────────────────────────────────────────
    convictions_section = _build_convictions_section(all_profile_rows)

    # ── TOC ───────────────────────────────────────────────────────────────────
    toc_links  = '<a href="#overall_top">&#9650; Top Overall</a>'
    toc_links += '<a href="#convictions">&#9733; Top Convictions</a>'
    toc_links += "".join(
        f'<a href="#{k}">{_PROFILE_META[k]["label"]}</a>'
        for k in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value")
    )
    if dow_rows:   toc_links += '<a href="#dow30">Dow 30 Ranking</a>'
    if bt_rows:    toc_links += '<a href="#backtest">Backtest vs S&amp;P 500</a>'
    toc_links += '<a href="#methodology">Methodology</a>'

    # ── Methodology ───────────────────────────────────────────────────────────
    methodology = """
    <span class="section-anchor" id="methodology"></span>
    <div class="section">
      <div class="section-title">Cum funcționează motorul</div>
      <div class="section-sub">
        Descrierea reală, la zi, a tot ce rulează în sistem — fără roadmap-uri sau funcții planificate.
      </div>

      <!-- ── 1. Date ── -->
      <div style="font-size:14px;font-weight:800;color:#1f2328;margin:20px 0 10px">
        1 &nbsp;·&nbsp; Sursa datelor
      </div>
      <div class="ib blue" style="margin-bottom:0">
        Universul S&amp;P 500 (503 tickers) este preluat live prin scraping Wikipedia la fiecare rulare.
        Datele financiare vin din <strong>Yahoo Finance via <code>yfinance</code></strong>:
        prețuri, bilanț, cont de profit&amp;pierdere, cash flow (3–5 ani istorici), beta, dividende.
        Fiecare ticker trece printr-un mecanism de <strong>retry cu backoff exponențial</strong> (3 încercări).
        Tot ce se descarcă se salvează în <strong>DuckDB local</strong> (<code>data/cache.duckdb</code>) —
        TTL = 0 pentru prețuri și financiare (date proaspete la fiecare rulare),
        TTL = 1 zi pentru istoricul de prețuri (datele istorice nu se schimbă).
        O re-rulare completă din cache durează ~8 secunde.
      </div>

      <!-- ── 2. Evaluare ── -->
      <div style="font-size:14px;font-weight:800;color:#1f2328;margin:24px 0 10px">
        2 &nbsp;·&nbsp; Modele de evaluare (Valoare intrinsecă)
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f7f8fa">
          <th style="padding:9px 14px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a;width:22%">Model</th>
          <th style="padding:9px 14px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a">Cum funcționează</th>
        </tr></thead>
        <tbody>
          <tr>
            <td style="padding:10px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">DCF — Gordon Growth Model</td>
            <td style="padding:10px 14px;border-bottom:1px solid #f0f2f5">
              Media FCF pe 3–5 ani istorici, proiectată 10 ani cu o rată de creștere calculată
              (<em>g = ROE × Retention Ratio</em>, capped la WACC−1%). Valoare terminală cu perpetuitate.
              WACC per-companie: Ke×(E/V) + Kd×(1−t)×(D/V), cu rata risk-free din yield-ul US 10Y
              (^TNX, live din Yahoo Finance). Rezultatul e împărțit la numărul de acțiuni.
            </td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">DCF — Exit Multiple</td>
            <td style="padding:10px 14px;border-bottom:1px solid #f0f2f5">
              EBITDA mediu proiectat 10 ani, multiplicat cu 12× la exit, ajustat cu net debt,
              totul actualizat cu același WACC dinamic. Al doilea model independent de GGM.
            </td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">DDM — Dividend Discount</td>
            <td style="padding:10px 14px;border-bottom:1px solid #f0f2f5">
              Activat automat pentru sectorul <strong>Financial</strong> (bănci, asigurători)
              unde FCF-ul din cash flow statement nu reflectă realitatea economică.
              Formula Gordon: P = D₁ / (r − g).
            </td>
          </tr>
          <tr>
            <td style="padding:10px 14px;font-weight:700">Valoare intrinsecă finală</td>
            <td style="padding:10px 14px">
              Media aritmetică GGM + Exit Multiple (sau DDM dacă sector Financial).
              <strong>Marja de Siguranță</strong> = (Intrinsic − Preț) / Intrinsic × 100.
              Valori pozitive = companie potențial subevaluată.
            </td>
          </tr>
        </tbody>
      </table>

      <!-- ── 3. Multipli & scoruri ── -->
      <div style="font-size:14px;font-weight:800;color:#1f2328;margin:24px 0 10px">
        3 &nbsp;·&nbsp; Multipli de piață și scoruri de calitate
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f7f8fa">
          <th style="padding:9px 14px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a;width:22%">Metric</th>
          <th style="padding:9px 14px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a">Interpretare</th>
        </tr></thead>
        <tbody>
          <tr><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">P/E, P/B, EV/EBITDA, P/FCF</td><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5">Multipli relativi calculați din prețul curent și datele financiare. P/FCF e cel mai important — măsoară cash real, nu profit contabil.</td></tr>
          <tr><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">Net Debt / EBITDA</td><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5">Gradul de îndatorare. &gt;3.5× declanșează automat steagul <em>Value Trap</em> și exclude compania din filtrele stricte.</td></tr>
          <tr><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">Piotroski F-Score (0–9)</td><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5">9 criterii binare: 4 profitabilitate, 3 levier/lichiditate, 2 eficiență operațională. ≥7 = solid, ≤3 = deteriorare.</td></tr>
          <tr><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">Altman Z-Score</td><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5">Model de predicție a falimentului. Prag calibrat la 1.0 (față de 1.81 original) pentru a acoperi sectoarele media/telecom cu active intangibile mari. &lt;1.0 = distress real.</td></tr>
          <tr><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5;font-weight:700">ROIC</td><td style="padding:9px 14px;border-bottom:1px solid #f0f2f5">Return on Invested Capital = NOPAT / (Equity + Debt − Cash). ≥10% = avantaj competitiv real (cost of capital depășit). ≥15% = moat puternic.</td></tr>
          <tr><td style="padding:9px 14px;font-weight:700">Composite Score (0–100)</td><td style="padding:9px 14px">Scor agregat: MoS% 40% + Piotroski 25% + ROIC 25% + Poziție 52w 10%. Folosit intern pentru ranking.</td></tr>
        </tbody>
      </table>

      <!-- ── 4. Screener ── -->
      <div style="font-size:14px;font-weight:800;color:#1f2328;margin:24px 0 10px">
        4 &nbsp;·&nbsp; Screener — ProfileFit și filtrele stricte
      </div>
      <div class="ib blue" style="margin-bottom:0">
        Fiecare companie primește un <strong>ProfileFit Score (0–100)</strong> față de fiecare profil,
        calculat ca 70% proximitate față de toate pragurile profilului + 30% Composite Score.
        <strong>PASS</strong> = îndeplinește <em>toate</em> criteriile strict.
        <strong>NEAR</strong> = ratează unul sau mai multe, dar e rankată.
        <strong>TRAP</strong> = steag Value Trap activ (datorii excesive sau FCF negativ pe toți anii).
        Nicio companie nu e ascunsă din raport — toate 503 sunt rankate per profil.
      </div>

      <!-- ── 5. Backtest ── -->
      <div style="font-size:14px;font-weight:800;color:#1f2328;margin:24px 0 10px">
        5 &nbsp;·&nbsp; Walk-Forward Backtest vs S&amp;P 500
      </div>
      <div class="ib blue" style="margin-bottom:12px">
        Simulare anuală: top-N companii din Deep Value cumpărate la prima zi de tranzacționare
        a anului, vândute la prima zi a anului următor. Benchmark: <strong>^GSPC (S&amp;P 500)</strong>.
        Metrici calculate pe seria de randamente anuale:
        <strong>CAGR, Sharpe Ratio, Sortino Ratio, Max Drawdown, Win Rate pe acțiune</strong>.
        Rezoluție nearest-trading-day pentru sărbători/weekenduri.
      </div>
      <div class="ib" style="background:#fffbeb;border:1px solid #fde68a;color:#92400e;margin-bottom:0">
        <strong>Limitări importante:</strong>
        (1) <strong>Look-ahead bias</strong> — filtrele folosesc datele financiare curente pentru toți anii istorici.
        (2) <strong>Survivorship bias</strong> — universul conține doar constituenții actuali ai S&amp;P 500.
        (3) Fără costuri de tranzacție, slippage sau taxe.
        Rezultatele sunt <em>indicatori direcționali</em>, nu predicții de viitor.
      </div>

    </div>"""

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
      Stock Screener &amp; Intrinsic Value Engine — v2 Full Report
    </div>
    <h1>Executive Summary Report</h1>
    <div class="subtitle">S&amp;P 500 Universe &nbsp;·&nbsp; 4 Screener Profiles &nbsp;·&nbsp;
      Dow Jones 30 Ranking &nbsp;·&nbsp; Walk-Forward Backtest vs S&amp;P 500</div>
    <div class="header-meta">
      <div class="hm-item"><div class="hm-label">Generated</div><div class="hm-value">{now}</div></div>
      <div class="hm-item"><div class="hm-label">Universe</div><div class="hm-value">S&amp;P 500 (503 tickers)</div></div>
      <div class="hm-item"><div class="hm-label">Strict Passed</div><div class="hm-value">{total_passed} (across 4 profiles)</div></div>
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

  {overall_top_section}
  {convictions_section}
  {''.join(profile_sections)}
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
/* Reposition every .why-body panel to span the full .section card width,
   regardless of which narrow <td> column the <details> sits in. */
(function () {{
  function reposition(det) {{
    var panel = det.querySelector('.why-body');
    if (!panel) return;
    var section = det.closest('.section');
    if (!section) return;
    var sR = section.getBoundingClientRect();
    var dR = det.getBoundingClientRect();
    panel.style.left     = (sR.left - dR.left + 16) + 'px';
    panel.style.width    = Math.min(sR.width - 32, 1380) + 'px';
    panel.style.maxWidth = '95vw';
  }}
  /* toggle event fires when open state changes */
  document.addEventListener('toggle', function (e) {{
    var det = e.target;
    if (det.classList && det.classList.contains('why') && det.open) reposition(det);
  }}, true);
  /* fallback: click for browsers that fire toggle after paint */
  document.querySelectorAll('details.why').forEach(function (det) {{
    det.addEventListener('click', function () {{
      setTimeout(function () {{ if (det.open) reposition(det); }}, 0);
    }});
  }});
}})();
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    print(f"Full report saved: {out_path}")
    print(f"  Strict passes — "
          + ", ".join(f"{k}: {n_pass_per_profile.get(k,0)}" for k in n_pass_per_profile))
    print(f"  Total ranked rows: {total_ranked}")
    print(f"  Report size: {out_path.stat().st_size / 1024:.0f} KB")


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
