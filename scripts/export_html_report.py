"""
export_html_report.py — Generate a self-contained HTML executive summary
from a screener CSV output (deep_value, buffett_quality, high_fcf_yield, or dow30_ranking).

Auto-detects the report type from the filename suffix.

Usage:
    python scripts/export_html_report.py
    python scripts/export_html_report.py --csv data/reports/20260818_200708_deep_value.csv
    python scripts/export_html_report.py --csv data/reports/20260818_200220_dow30_ranking.csv
    python scripts/export_html_report.py --csv path/to/file.csv --out my_report.html
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(val: str, decimals: int = 2, suffix: str = "", prefix: str = "") -> str:
    """Format a numeric CSV string nicely; return '—' if empty/NaN."""
    if not val or val.strip() in ("", "nan", "inf", "-inf"):
        return "—"
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return "—"
        return f"{prefix}{f:,.{decimals}f}{suffix}"
    except ValueError:
        return val  # return as-is for text values


def _fv(val: str) -> float | None:
    """Parse float or return None."""
    if not val or val.strip() in ("", "nan", "inf", "-inf"):
        return None
    try:
        f = float(val)
        return None if math.isnan(f) or math.isinf(f) else f
    except ValueError:
        return None


def _mos_grade(mos: float) -> tuple[str, str, str]:
    """Return (grade, colour, label) for a Margin of Safety value."""
    if mos >= 60:
        return "A+", "#16a34a", "Exceptional"
    if mos >= 45:
        return "A",  "#22c55e", "Strong Buy"
    if mos >= 30:
        return "B+", "#84cc16", "Buy"
    if mos >= 20:
        return "B",  "#eab308", "Moderate Buy"
    return "C", "#f97316", "Watch"


def _pos_colour(pos: float) -> str:
    """Green = near 52w low, red = near 52w high."""
    if pos < 33:
        return "#16a34a"
    if pos < 66:
        return "#eab308"
    return "#e11d48"


def _load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _detect_failed(csv_path: Path) -> list[str]:
    failed_path = csv_path.parent / f"{csv_path.stem}_failed.txt"
    if failed_path.exists():
        return [l.strip() for l in failed_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return []


def _is_dow30(csv_path: Path) -> bool:
    return "dow30" in csv_path.stem.lower()


def _parse_ts(csv_path: Path) -> str:
    try:
        ts = datetime.strptime(csv_path.stem[:15], "%Y%m%d_%H%M%S")
        return ts.strftime("%d %B %Y at %H:%M")
    except Exception:
        return "unknown"


# ── Shared CSS ────────────────────────────────────────────────────────────────

_CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
         font-size: 14px; line-height: 1.6; background: #f0f2f5; color: #1f2328; }
  a { color: #3b82d4; }
  .page { max-width: 980px; margin: 0 auto; padding: 32px 20px 60px; }

  .report-header { background: #1f2328; color: #fff; border-radius: 12px;
                   padding: 32px 36px; margin-bottom: 28px; }
  .report-header h1 { font-size: 26px; font-weight: 800; margin-bottom: 6px; }
  .report-header .subtitle { color: #8d96a0; font-size: 13px; }
  .header-meta { display:flex; gap:28px; margin-top:20px; flex-wrap:wrap; }
  .header-meta-item .meta-label { font-size:11px; color:#8d96a0; text-transform:uppercase;
                                   letter-spacing:.06em; }
  .header-meta-item .meta-value { font-size:15px; font-weight:700; color:#fff; }

  .disclaimer { background:#fffbeb; border:1px solid #fde68a; border-radius:8px;
                padding:14px 18px; margin-bottom:24px; font-size:12px; color:#92400e;
                line-height:1.5; }
  .section-title { font-size:17px; font-weight:700; margin-bottom:16px; padding-bottom:8px;
                   border-bottom:2px solid #e5e7eb; color:#1f2328; }
  .info-box { border-radius:8px; padding:14px 18px; margin-bottom:20px;
              font-size:13px; line-height:1.6; }
  .info-box.blue  { background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af; }
  .info-box.green { background:#f0fdf4; border:1px solid #bbf7d0; color:#14532d; }
  .info-box.warn  { background:#fffbeb; border:1px solid #fde68a; color:#92400e; }

  .stats-bar { display:flex; gap:16px; margin-bottom:28px; flex-wrap:wrap; }
  .stat-pill { flex:1; min-width:130px; background:#fff; border-radius:10px;
               padding:16px 20px; border:1px solid #e5e7eb; text-align:center; }
  .stat-pill .sp-value { font-size:28px; font-weight:800; }
  .stat-pill .sp-label { font-size:11px; color:#57606a; text-transform:uppercase;
                          letter-spacing:.06em; margin-top:2px; }

  .hero-row { display:flex; gap:16px; margin-bottom:32px; flex-wrap:wrap; }
  .hero-card { flex:1; min-width:200px; background:#fff; border-radius:10px;
               padding:20px; border:1px solid #e5e7eb; }

  /* ── standard screener cards ── */
  .card { background:#fff; border-radius:12px; padding:24px 28px;
          margin-bottom:20px; border:1px solid #e5e7eb; border-left:4px solid #e5e7eb; }
  .top-card { border-left-color:#3b82d4; }
  .card-header { display:flex; align-items:flex-start; gap:16px; margin-bottom:16px; }
  .rank-badge { width:36px; height:36px; border-radius:50%; color:#fff;
                font-weight:800; font-size:13px; display:flex; align-items:center;
                justify-content:center; flex-shrink:0; }
  .card-title-block { flex:1; }
  .card-ticker { font-size:20px; font-weight:800; display:flex; align-items:center; gap:8px; }
  .card-company { font-size:13px; color:#57606a; margin-top:1px; }
  .card-meta { font-size:12px; color:#8d96a0; margin-top:3px; }
  .top-badge { font-size:9px; background:#3b82d4; color:#fff; border-radius:4px;
               padding:2px 6px; letter-spacing:.06em; font-weight:700;
               text-transform:uppercase; vertical-align:middle; }
  .grade-block { border:2px solid; border-radius:8px; padding:6px 14px;
                 text-align:center; flex-shrink:0; }
  .grade-letter { font-size:22px; font-weight:800; line-height:1; }
  .grade-label  { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; }

  .mos-bar-wrap { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
  .mos-bar-label { font-size:12px; color:#57606a; width:120px; flex-shrink:0; }
  .mos-bar-track { flex:1; height:8px; background:#f0f2f5; border-radius:4px; overflow:hidden; }
  .mos-bar-fill  { height:100%; border-radius:4px; }
  .mos-bar-pct   { font-size:14px; font-weight:700; width:56px; text-align:right; flex-shrink:0; }

  .metrics-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr));
                  gap:10px; margin-bottom:18px; }
  .metric-cell  { background:#f7f8fa; border-radius:8px; padding:10px 12px; cursor:help; }
  .metric-label { font-size:10px; color:#8d96a0; text-transform:uppercase;
                  letter-spacing:.05em; font-weight:600; }
  .metric-value { font-size:16px; font-weight:700; margin-top:2px; }

  .conviction-block { background:#f7f8fa; border-radius:8px; padding:14px 16px;
                      border-left:3px solid #3b82d4; }
  .conviction-title { font-size:11px; font-weight:700; color:#3b82d4; text-transform:uppercase;
                      letter-spacing:.06em; margin-bottom:6px; }
  .conviction-text  { font-size:13px; line-height:1.65; color:#374151; }

  /* ── Dow 30 ranking table ── */
  .rank-table { width:100%; border-collapse:collapse; background:#fff;
                border-radius:12px; overflow:hidden; border:1px solid #e5e7eb;
                margin-bottom:24px; }
  .rank-table th { background:#1f2328; color:#fff; font-size:11px; text-transform:uppercase;
                   letter-spacing:.05em; padding:11px 14px; text-align:left;
                   white-space:nowrap; }
  .rank-table th.r { text-align:right; }
  .rank-table td { padding:10px 14px; border-bottom:1px solid #f0f2f5; font-size:13px;
                   vertical-align:middle; }
  .rank-table tr:last-child td { border-bottom:none; }
  .rank-table tr:hover td { background:#f7f8fa; }
  .rank-table td.r { text-align:right; }
  .rank-num { font-weight:800; color:#3b82d4; font-size:15px; }
  .ticker-cell { font-weight:800; font-size:15px; color:#1f2328; }
  .pos-gauge-wrap { display:flex; align-items:center; gap:8px; }
  .pos-gauge-track { flex:1; min-width:80px; height:6px; background:#e5e7eb;
                     border-radius:3px; overflow:hidden; }
  .pos-gauge-fill { height:100%; border-radius:3px; }
  .pos-pct { font-size:12px; font-weight:700; width:42px; text-align:right; flex-shrink:0; }

  /* ── explainer ── */
  .explainer { background:#fff; border-radius:12px; padding:28px 32px;
               margin-bottom:24px; border:1px solid #e5e7eb; }
  .explainer h2 { font-size:18px; font-weight:800; margin-bottom:4px; }
  .explainer .sub { font-size:13px; color:#57606a; margin-bottom:20px; }
  .explainer h3 { font-size:14px; font-weight:700; margin:20px 0 6px; }
  .explainer p  { font-size:13px; color:#374151; line-height:1.7; margin-bottom:8px; }
  .explainer ul { padding-left:20px; }
  .explainer ul li { font-size:13px; color:#374151; line-height:1.7; margin-bottom:4px; }
  .param-table { width:100%; border-collapse:collapse; margin-top:10px; }
  .param-table th { background:#f7f8fa; font-size:11px; text-transform:uppercase;
                    letter-spacing:.05em; color:#57606a; padding:8px 12px;
                    text-align:left; border-bottom:2px solid #e5e7eb; }
  .param-table td { padding:9px 12px; border-bottom:1px solid #e5e7eb;
                    font-size:13px; vertical-align:top; }
  .param-table tr:last-child td { border-bottom:none; }
  .tag       { display:inline-block; background:#eff6ff; color:#1e40af; border-radius:4px;
               padding:1px 6px; font-size:11px; font-weight:600; }
  .tag.red   { background:#fff1f2; color:#be123c; }
  .tag.green { background:#f0fdf4; color:#15803d; }
  .flow-steps { display:flex; flex-wrap:wrap; margin:16px 0; gap:4px; }
  .flow-step  { flex:1; min-width:140px; background:#f7f8fa; border-radius:8px;
                padding:14px 16px; text-align:center; border-top:3px solid #3b82d4; }
  .flow-step .step-num   { font-size:10px; font-weight:700; color:#3b82d4;
                            text-transform:uppercase; letter-spacing:.06em; }
  .flow-step .step-title { font-size:13px; font-weight:700; margin:4px 0 3px; }
  .flow-step .step-desc  { font-size:11px; color:#57606a; line-height:1.4; }
  .sector-table { width:100%; border-collapse:collapse; }
  .sector-table th { background:#f7f8fa; font-size:11px; text-transform:uppercase;
                     letter-spacing:.05em; color:#57606a; padding:8px 12px;
                     text-align:left; border-bottom:2px solid #e5e7eb; }
  .sector-table td { padding:8px 12px; border-bottom:1px solid #f0f2f5; font-size:13px; }
  .footer { text-align:center; font-size:11px; color:#8d96a0;
            border-top:1px solid #e5e7eb; padding-top:20px; margin-top:40px; }
  @media(max-width:640px) {
    .header-meta, .hero-row, .stats-bar { flex-direction:column; }
    .card-header { flex-wrap:wrap; }
    .rank-table { font-size:12px; }
    .rank-table td, .rank-table th { padding:7px 8px; }
    .pos-gauge-track { min-width:50px; }
  }
"""


# ── Deep Value report ─────────────────────────────────────────────────────────

def _conviction(row: dict) -> str:
    ticker  = row.get("Ticker", "")
    company = row.get("Company", ticker)
    mos_v   = _fv(row.get("MoS%", ""))
    pe      = row.get("P/E", "")
    pb      = row.get("P/B", "")
    pfcf    = row.get("P/FCF", "")
    sector  = row.get("Sector") or "—"
    dcf     = row.get("DCF Avg", "")
    pos_v   = _fv(row.get("52w Position%", ""))
    low_v   = _fv(row.get("52w Low", ""))
    high_v  = _fv(row.get("52w High", ""))

    mos = mos_v if mos_v is not None else 0.0
    parts = []
    parts.append(
        f"Our model estimates that <strong>{company} ({ticker})</strong> "
        f"is currently trading at a <strong>{mos:.0f}% discount</strong> to its "
        f"calculated intrinsic value"
    )
    dcf_v = _fv(dcf)
    if dcf_v is not None:
        parts[-1] += f" of <strong>${dcf_v:,.2f} per share</strong>"
    parts[-1] += "."

    pe_v = _fv(pe)
    if pe_v is not None:
        parts.append(
            f"At a P/E of <strong>{pe_v:.1f}×</strong>, the market is pricing the company "
            f"as if its earnings will shrink — but our cash-flow analysis suggests otherwise."
        )
    pb_v = _fv(pb)
    if pb_v is not None and pb_v < 1.5:
        parts.append(
            f"A Price-to-Book of <strong>{pb_v:.2f}×</strong> means you are buying the "
            f"company's assets for less than their stated accounting value — a rare opportunity."
        )
    pfcf_v = _fv(pfcf)
    if pfcf_v is not None:
        parts.append(
            f"The P/FCF ratio of <strong>{pfcf_v:.1f}×</strong> confirms the business "
            f"generates real cash — not just accounting profits."
        )
    # 52w commentary
    if pos_v is not None and low_v is not None and high_v is not None:
        if pos_v < 33:
            parts.append(
                f"The stock is trading at just <strong>{pos_v:.0f}%</strong> of its "
                f"52-week range (low ${low_v:,.2f} / high ${high_v:,.2f}), "
                f"offering an additional margin of safety against near-term volatility."
            )
        elif pos_v > 70:
            parts.append(
                f"Note: at <strong>{pos_v:.0f}%</strong> of its 52-week range, the stock "
                f"is closer to its annual high — consider waiting for a pullback."
            )

    parts.append(
        f"The company operates in the <strong>{sector}</strong> sector and passed all "
        f"six Deep Value quality filters simultaneously."
    )
    return " ".join(parts)


def _build_deep_value_html(rows: list[dict], csv_path: Path, failed: list[str]) -> str:
    run_date  = datetime.now().strftime("%d %B %Y, %H:%M")
    data_date = _parse_ts(csv_path)
    profile_label = csv_path.stem.split("_", 2)[-1].replace("_", " ").title() if "_" in csv_path.stem else "Screener"
    n = len(rows)

    # ── hero row (top 3) ──────────────────────────────────────────────────────
    hero_html = ""
    for i, row in enumerate(rows[:3]):
        mos_v = _fv(row.get("MoS%", ""))
        if mos_v is None:
            continue
        _, colour, label = _mos_grade(mos_v)
        hero_html += f"""
        <div class="hero-card" style="border-top:4px solid {colour}">
          <div style="font-size:11px;font-weight:700;color:{colour};letter-spacing:.08em;text-transform:uppercase">#{i+1} Top Pick</div>
          <div style="font-size:22px;font-weight:800;margin:4px 0 2px">{row['Ticker']}</div>
          <div style="font-size:12px;color:#57606a;margin-bottom:8px">{row.get('Company','')}</div>
          <div style="font-size:28px;font-weight:800;color:{colour}">{mos_v:.0f}%</div>
          <div style="font-size:11px;color:#57606a">Margin of Safety</div>
          <div style="margin-top:8px;font-size:13px">Price: <strong>{_fmt(row.get('Price',''), 2, prefix='$')}</strong></div>
          <div style="font-size:13px">Intrinsic: <strong>{_fmt(row.get('DCF Avg',''), 2, prefix='$')}</strong></div>
        </div>"""

    # ── cards ─────────────────────────────────────────────────────────────────
    cards_html = ""
    for i, row in enumerate(rows):
        mos_v = _fv(row.get("MoS%", ""))
        if mos_v is None:
            mos_v = 0.0
        grade, colour, label = _mos_grade(mos_v)
        rank = i + 1
        bar_pct = min(mos_v, 100)

        pos_v  = _fv(row.get("52w Position%", ""))
        low_v  = _fv(row.get("52w Low", ""))
        high_v = _fv(row.get("52w High", ""))
        pos_colour_val = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"

        metrics = [
            ("Price",             _fmt(row.get("Price",""), 2, prefix="$"),          "Current market price per share"),
            ("Intrinsic Value",   _fmt(row.get("DCF Avg",""), 2, prefix="$"),         "Average GGM + Exit Multiple"),
            ("Margin of Safety",  f"{_fmt(row.get('MoS%',''), 1)}%",                  "Discount to intrinsic value"),
            ("52w Low",           _fmt(row.get("52w Low",""), 2, prefix="$"),          "52-week lowest price"),
            ("52w High",          _fmt(row.get("52w High",""), 2, prefix="$"),         "52-week highest price"),
            ("52w Position",      f"{_fmt(row.get('52w Position%',''), 1)}%",          "0% = at annual low, 100% = at annual high"),
            ("P/E Ratio",         f"{_fmt(row.get('P/E',''), 1)}×",                    "Price ÷ earnings per share"),
            ("P/B Ratio",         f"{_fmt(row.get('P/B',''), 2)}×",                    "Price ÷ book value per share"),
            ("EV/EBITDA",         f"{_fmt(row.get('EV/EBITDA',''), 1)}×",              "Enterprise value ÷ EBITDA"),
            ("P/FCF",             f"{_fmt(row.get('P/FCF',''), 1)}×",                  "Price ÷ free cash flow per share"),
            ("Net Debt/EBITDA",   f"{_fmt(row.get('NetDebt/EBITDA',''), 2)}×",         "Leverage ratio"),
            ("DCF (GGM)",         _fmt(row.get("DCF GGM",""), 2, prefix="$"),          "Gordon Growth Model"),
            ("DCF (Exit Mult)",   _fmt(row.get("DCF Exit",""), 2, prefix="$"),         "Exit Multiple method"),
        ]
        metrics_html = "".join(
            f'<div class="metric-cell" title="{desc}"><div class="metric-label">{name}</div>'
            f'<div class="metric-value">{val}</div></div>'
            for name, val, desc in metrics
        )

        # 52w bar inside card header area
        pos_bar = ""
        if pos_v is not None:
            pos_bar = f"""
          <div class="mos-bar-wrap" style="margin-bottom:8px">
            <div class="mos-bar-label" style="font-size:11px;color:#57606a;width:100px">52w Position</div>
            <div class="mos-bar-track"><div class="mos-bar-fill" style="width:{min(pos_v,100):.1f}%;background:{pos_colour_val}"></div></div>
            <div class="mos-bar-pct" style="color:{pos_colour_val};font-size:12px">{pos_v:.0f}%</div>
          </div>"""

        top_badge = '<span class="top-badge">TOP PICK</span>' if rank <= 3 else ""
        cards_html += f"""
        <div class="card {'top-card' if rank <= 3 else ''}">
          <div class="card-header">
            <div class="rank-badge" style="background:{colour}">#{rank}</div>
            <div class="card-title-block">
              <div class="card-ticker">{row['Ticker']} {top_badge}</div>
              <div class="card-company">{row.get('Company','')}</div>
              <div class="card-meta">{row.get('Sector','') or '—'} &nbsp;|&nbsp; {row.get('Industry','') or '—'}</div>
            </div>
            <div class="grade-block" style="border-color:{colour};color:{colour}">
              <div class="grade-letter">{grade}</div>
              <div class="grade-label">{label}</div>
            </div>
          </div>
          <div class="mos-bar-wrap">
            <div class="mos-bar-label">Margin of Safety</div>
            <div class="mos-bar-track"><div class="mos-bar-fill" style="width:{bar_pct:.1f}%;background:{colour}"></div></div>
            <div class="mos-bar-pct" style="color:{colour}">{mos_v:.1f}%</div>
          </div>
          {pos_bar}
          <div class="metrics-grid">{metrics_html}</div>
          <div class="conviction-block">
            <div class="conviction-title">Why buy {row['Ticker']}?</div>
            <div class="conviction-text">{_conviction(row)}</div>
          </div>
        </div>"""

    # ── sector breakdown ───────────────────────────────────────────────────────
    sector_counts: dict[str, int] = {}
    for row in rows:
        s = row.get("Sector") or "Unknown"
        sector_counts[s] = sector_counts.get(s, 0) + 1
    sector_rows = "".join(
        f"<tr><td>{s}</td><td style='text-align:center'>{c}</td>"
        f"<td><div style='height:10px;background:#3b82d4;border-radius:4px;width:{c/n*100:.0f}%'></div></td></tr>"
        for s, c in sorted(sector_counts.items(), key=lambda x: -x[1])
    )

    failed_html = ""
    if failed:
        failed_html = f"""<div class="info-box warn">
          <strong>{len(failed)} tickers returned no usable data</strong> (delisted / API gap):<br>
          <span style="font-family:monospace;font-size:12px">{", ".join(failed)}</span>
        </div>"""

    best_mos = _fv(rows[0].get("MoS%", "")) if rows else None
    best_mos_str = f"{best_mos:.0f}%" if best_mos else "—"

    return _HTML_SHELL.format(
        title="Deep Value — Executive Report",
        run_date=run_date,
        data_date=data_date,
        profile_label=profile_label,
        css=_CSS,
        body=f"""
  <div class="report-header">
    <div style="font-size:11px;color:#8d96a0;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">
      Stock Screener &amp; Intrinsic Value Engine
    </div>
    <h1>Deep Value Executive Report</h1>
    <div class="subtitle">S&amp;P 500 Universe &nbsp;·&nbsp; {profile_label} Profile &nbsp;·&nbsp; Run {data_date}</div>
    <div class="header-meta">
      <div class="header-meta-item"><div class="meta-label">Generated</div><div class="meta-value">{run_date}</div></div>
      <div class="header-meta-item"><div class="meta-label">Candidates Passed</div><div class="meta-value">{n}</div></div>
      <div class="header-meta-item"><div class="meta-label">Best MoS</div><div class="meta-value">{best_mos_str}</div></div>
      <div class="header-meta-item"><div class="meta-label">Data Source</div><div class="meta-value">Yahoo Finance</div></div>
    </div>
  </div>

  <div class="disclaimer">
    <strong>Important Disclaimer:</strong> Generated by an automated quantitative algorithm for
    <strong>informational and educational purposes only</strong>. This does <strong>not</strong>
    constitute financial advice. Always conduct your own due diligence.
  </div>

  <div class="section-title">Top 3 Recommendations at a Glance</div>
  <div class="hero-row">{hero_html}</div>

  <div class="info-box green">
    <strong>How to read this report:</strong> Each company below passed <em>all six</em> Deep Value
    filters simultaneously. Ranked by <strong>Margin of Safety</strong> (highest first). The new
    <strong>52w Position%</strong> column shows where each stock sits in its annual price range —
    0% means at the 52-week low, 100% means at the 52-week high. Lower is better.
  </div>

  <div class="section-title">All {n} Candidates — Ranked by Margin of Safety</div>
  {cards_html}
  {failed_html}

  <div class="explainer">
    <h2>Sector Distribution</h2>
    <div class="sub">How the {n} passing companies are distributed across market sectors</div>
    <table class="sector-table">
      <thead><tr><th>Sector</th><th style="text-align:center">Count</th><th style="width:50%">Share</th></tr></thead>
      <tbody>{sector_rows}</tbody>
    </table>
  </div>

  {_DEEP_VALUE_EXPLAINERS}
"""
    )


# ── Dow 30 report ─────────────────────────────────────────────────────────────

def _build_dow30_html(rows: list[dict], csv_path: Path, failed: list[str]) -> str:
    run_date  = datetime.now().strftime("%d %B %Y, %H:%M")
    data_date = _parse_ts(csv_path)
    n = len(rows)

    # ── ranked table ──────────────────────────────────────────────────────────
    table_rows = ""
    for row in rows:
        rank   = row.get("Rank", "")
        ticker = row.get("Ticker", "")
        co     = row.get("Company", "")
        sector = row.get("Sector", "")
        price  = _fmt(row.get("Price",""), 2, prefix="$")
        low    = _fmt(row.get("52w Low",""), 2, prefix="$")
        high   = _fmt(row.get("52w High",""), 2, prefix="$")
        pos_v  = _fv(row.get("52w Position%",""))
        mcap   = _fmt(row.get("Market Cap ($B)",""), 0, suffix="B", prefix="$")
        pe     = _fmt(row.get("P/E",""), 1, suffix="×")
        pb     = _fmt(row.get("P/B",""), 2, suffix="×")
        mos_v  = _fv(row.get("MoS%",""))

        if pos_v is not None:
            pc = _pos_colour(pos_v)
            gauge = (
                f'<div class="pos-gauge-wrap">'
                f'<div class="pos-gauge-track"><div class="pos-gauge-fill" '
                f'style="width:{min(pos_v,100):.1f}%;background:{pc}"></div></div>'
                f'<div class="pos-pct" style="color:{pc}">{pos_v:.1f}%</div></div>'
            )
        else:
            gauge = "—"

        mos_str = f"{mos_v:.1f}%" if mos_v is not None else "—"
        if mos_v is not None:
            _, mc, _ = _mos_grade(mos_v)
            mos_str = f'<span style="color:{mc};font-weight:700">{mos_str}</span>'

        table_rows += f"""<tr>
          <td class="r"><span class="rank-num">{rank}</span></td>
          <td><span class="ticker-cell">{ticker}</span></td>
          <td style="max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{co}</td>
          <td style="color:#57606a">{sector}</td>
          <td class="r">{price}</td>
          <td class="r" style="color:#57606a">{low}</td>
          <td class="r" style="color:#57606a">{high}</td>
          <td style="min-width:160px">{gauge}</td>
          <td class="r">{mcap}</td>
          <td class="r">{pe}</td>
          <td class="r">{pb}</td>
          <td class="r">{mos_str}</td>
        </tr>"""

    # ── top 3 hero ─────────────────────────────────────────────────────────────
    hero_html = ""
    for i, row in enumerate(rows[:3]):
        pos_v = _fv(row.get("52w Position%",""))
        if pos_v is None:
            continue
        pc = _pos_colour(pos_v)
        hero_html += f"""
        <div class="hero-card" style="border-top:4px solid {pc}">
          <div style="font-size:11px;font-weight:700;color:{pc};letter-spacing:.08em;text-transform:uppercase">#{i+1} Best Opportunity</div>
          <div style="font-size:22px;font-weight:800;margin:4px 0 2px">{row['Ticker']}</div>
          <div style="font-size:12px;color:#57606a;margin-bottom:8px">{row.get('Company','')}</div>
          <div style="font-size:28px;font-weight:800;color:{pc}">{pos_v:.1f}%</div>
          <div style="font-size:11px;color:#57606a">of 52w Range</div>
          <div style="margin-top:8px;font-size:13px">Price: <strong>{_fmt(row.get('Price',''), 2, prefix='$')}</strong></div>
          <div style="font-size:13px">52w Low: <strong>{_fmt(row.get('52w Low',''), 2, prefix='$')}</strong> &nbsp;|&nbsp; High: <strong>{_fmt(row.get('52w High',''), 2, prefix='$')}</strong></div>
        </div>"""

    # ── sector breakdown ───────────────────────────────────────────────────────
    sector_counts: dict[str, int] = {}
    for row in rows:
        s = row.get("Sector") or "Unknown"
        sector_counts[s] = sector_counts.get(s, 0) + 1
    sector_rows_html = "".join(
        f"<tr><td>{s}</td><td style='text-align:center'>{c}</td>"
        f"<td><div style='height:10px;background:#3b82d4;border-radius:4px;width:{c/n*100:.0f}%'></div></td></tr>"
        for s, c in sorted(sector_counts.items(), key=lambda x: -x[1])
    )

    best_row = rows[0] if rows else {}
    best_pos  = _fv(best_row.get("52w Position%",""))
    best_ticker = best_row.get("Ticker","—")

    return _HTML_SHELL.format(
        title="Dow Jones 30 — 52-Week Ranking Report",
        run_date=run_date,
        data_date=data_date,
        profile_label="Dow Jones 30 / 52-Week Ranking",
        css=_CSS,
        body=f"""
  <div class="report-header">
    <div style="font-size:11px;color:#8d96a0;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">
      Stock Screener &amp; Intrinsic Value Engine
    </div>
    <h1>Dow Jones 30 — 52-Week Ranking Report</h1>
    <div class="subtitle">Blue-Chip Universe &nbsp;·&nbsp; Ranked by 52-Week Price Position &nbsp;·&nbsp; {data_date}</div>
    <div class="header-meta">
      <div class="header-meta-item"><div class="meta-label">Generated</div><div class="meta-value">{run_date}</div></div>
      <div class="header-meta-item"><div class="meta-label">Companies Ranked</div><div class="meta-value">{n}</div></div>
      <div class="header-meta-item"><div class="meta-label">Best Opportunity</div><div class="meta-value">{best_ticker} ({best_pos:.1f}% of range)</div></div>
      <div class="header-meta-item"><div class="meta-label">Data Source</div><div class="meta-value">Yahoo Finance</div></div>
    </div>
  </div>

  <div class="disclaimer">
    <strong>Important Disclaimer:</strong> Generated by an automated quantitative algorithm for
    <strong>informational and educational purposes only</strong>. This does <strong>not</strong>
    constitute financial advice. Always conduct your own due diligence.
  </div>

  <div class="info-box blue">
    <strong>How to read this ranking:</strong> Companies are sorted by <strong>52-week Position %</strong>
    — the percentage of their annual price range they currently occupy.
    <strong style="color:#16a34a">Green (0–33%)</strong> = trading near the annual low — maximum near-term upside potential.
    <strong style="color:#eab308">Yellow (33–66%)</strong> = mid-range.
    <strong style="color:#e11d48">Red (66–100%)</strong> = trading near the annual high.
    All 30 Dow companies are shown — no MoS filter applied. The MoS% column is informational only.
  </div>

  <div class="section-title">Top 3 Best Opportunities</div>
  <div class="hero-row">{hero_html}</div>

  <div class="section-title">All {n} Dow Jones Companies — Ranked by 52-Week Position</div>
  <table class="rank-table">
    <thead>
      <tr>
        <th class="r">#</th>
        <th>Ticker</th>
        <th>Company</th>
        <th>Sector</th>
        <th class="r">Price</th>
        <th class="r">52w Low</th>
        <th class="r">52w High</th>
        <th>52w Position (lower = better)</th>
        <th class="r">Mkt Cap</th>
        <th class="r">P/E</th>
        <th class="r">P/B</th>
        <th class="r">MoS%</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>

  <div class="explainer">
    <h2>Sector Distribution</h2>
    <div class="sub">How the {n} Dow Jones companies are distributed across sectors</div>
    <table class="sector-table">
      <thead><tr><th>Sector</th><th style="text-align:center">Count</th><th style="width:50%">Share</th></tr></thead>
      <tbody>{sector_rows_html}</tbody>
    </table>
  </div>

  {_DOW30_EXPLAINER}
"""
    )


# ── Shared explainer blocks ───────────────────────────────────────────────────

_DEEP_VALUE_EXPLAINERS = """
  <div class="explainer">
    <h2>How the Algorithm Works</h2>
    <div class="sub">A plain-English explanation of every step — no finance degree required</div>
    <p>The screener works in four sequential stages — a progressively tighter funnel starting from every company in the S&amp;P 500:</p>
    <div class="flow-steps">
      <div class="flow-step"><div class="step-num">Step 1</div><div class="step-title">Universe Assembly</div><div class="step-desc">Download the live S&amp;P 500 list from Wikipedia (503 tickers)</div></div>
      <div class="flow-step"><div class="step-num">Step 2</div><div class="step-title">Data Fetch &amp; Cache</div><div class="step-desc">Pull financials from Yahoo Finance; store locally — re-runs take ~8 seconds</div></div>
      <div class="flow-step"><div class="step-num">Step 3</div><div class="step-title">Valuation Engine</div><div class="step-desc">Compute 6 relative multiples + 2 DCF models + 52-week range metrics</div></div>
      <div class="flow-step"><div class="step-num">Step 4</div><div class="step-title">Deep Value Filter</div><div class="step-desc">Keep only companies passing all 6 thresholds AND with ≥ 20% MoS</div></div>
    </div>
    <h3>DCF Method 1 — Gordon Growth Model (conservative, FCF-based)</h3>
    <p>Requires ≥ 3 years of positive Free Cash Flow. Uses the historical average as baseline, projects 10 years at 5% growth, discounts at 10% WACC, adds terminal value via <code>TV = FCF × (1+g) / (r−g)</code>.</p>
    <h3>DCF Method 2 — Exit Multiple (market-relative, EBITDA-based)</h3>
    <p>Projects EBITDA 10 years forward, applies a 12× exit multiple (S&amp;P 500 median), subtracts net debt, discounts at 10%, divides by shares outstanding. Both methods are averaged.</p>
    <h3>New: 52-Week Position %</h3>
    <p><code>52w Position% = (Price − 52w Low) / (52w High − 52w Low) × 100</code>. This secondary indicator shows how close a stock is to its annual low — a position below 33% provides an additional layer of safety against near-term market volatility, independent of the DCF model.</p>
  </div>

  <div class="explainer">
    <h2>What Every Metric Means</h2>
    <div class="sub">Plain-English definitions for non-technical readers</div>
    <table class="param-table">
      <thead><tr><th>Metric</th><th>What it tells you</th><th>Threshold</th></tr></thead>
      <tbody>
        <tr><td><strong>Margin of Safety %</strong></td><td>(Intrinsic − Price) / Intrinsic × 100. Your buffer against model errors. Benjamin Graham called this the cornerstone of value investing.</td><td><span class="tag green">≥ 20%</span></td></tr>
        <tr><td><strong>52w Position %</strong></td><td>Where the stock sits in its 52-week range. 0% = at the annual low. Lower means more upside relative to recent history.</td><td><span class="tag green">&lt; 33% = green</span></td></tr>
        <tr><td><strong>P/E Ratio</strong></td><td>Dollars paid per dollar of annual earnings. S&amp;P 500 average ≈ 22×.</td><td><span class="tag green">≤ 15×</span></td></tr>
        <tr><td><strong>P/B Ratio</strong></td><td>Price per dollar of net assets. Below 1× = buying assets for less than book value.</td><td><span class="tag green">≤ 1.5×</span></td></tr>
        <tr><td><strong>EV/EBITDA</strong></td><td>Total takeover cost ÷ operating profit. Below 8× is historically cheap.</td><td><span class="tag green">≤ 8×</span></td></tr>
        <tr><td><strong>P/FCF</strong></td><td>Price to real cash generated. Harder to manipulate than earnings.</td><td><span class="tag green">≤ 15×</span></td></tr>
        <tr><td><strong>Net Debt / EBITDA</strong></td><td>Years of operating profit to pay off net debt. Above 3.5× triggers Value Trap exclusion.</td><td><span class="tag green">≤ 2.5×</span></td></tr>
      </tbody>
    </table>
  </div>

  <div class="explainer">
    <h2>Value Traps — Why We Exclude Them</h2>
    <div class="sub">A cheap stock is not always a good investment</div>
    <p>A <strong>Value Trap</strong> looks cheap by simple metrics but is heading toward financial distress. We exclude a company if <em>either</em> condition holds:</p>
    <ul>
      <li><strong>Net Debt / EBITDA &gt; 3.5×</strong> — debt service could overwhelm operations in a downturn.</li>
      <li><strong>All available FCF years ≤ 0</strong> — the business burns cash every year; the "cheap" price may be fully justified.</li>
    </ul>
  </div>

  <div class="explainer">
    <h2>Known Limitations</h2>
    <div class="sub">What the algorithm cannot see</div>
    <ul>
      <li style="margin-bottom:8px"><strong>Backward-looking data</strong> — all inputs are historical. Major structural changes (new competitors, regulation, M&amp;A) are invisible.</li>
      <li style="margin-bottom:8px"><strong>No qualitative analysis</strong> — management quality, moats, and ESG factors are not modelled.</li>
      <li style="margin-bottom:8px"><strong>Single DCF scenario</strong> — a 2% change in discount rate moves intrinsic value by 20–40%.</li>
      <li style="margin-bottom:8px"><strong>Financial sector quirks</strong> — banks and insurers have unusual cashflow structures that reduce DCF accuracy.</li>
    </ul>
    <div class="info-box blue" style="margin-top:14px">
      <strong>Best practice:</strong> Use this report as a <em>starting point for research</em>, not a final decision. Each company here is statistically cheap by multiple independent measures and deserves a thorough qualitative review before capital is committed.
    </div>
  </div>
"""

_DOW30_EXPLAINER = """
  <div class="explainer">
    <h2>About This Ranking Strategy</h2>
    <div class="sub">Why 52-week position is a powerful secondary indicator for blue-chip stocks</div>
    <p>The Dow Jones Industrial Average contains only 30 companies, changed very rarely (roughly once per year on average vs. 20–30 changes per year in the S&amp;P 500). This extreme stability means that a Dow company trading near its 52-week low is almost certainly experiencing a <strong>temporary setback</strong> — not a structural failure — because weak companies get removed from the index.</p>
    <h3>The Formula</h3>
    <p><code style="background:#f7f8fa;padding:2px 6px;border-radius:4px">52w Position% = (Current Price − 52w Low) / (52w High − 52w Low) × 100</code></p>
    <ul>
      <li><strong>0%</strong> — stock is at exactly its 52-week low: maximum potential upside, maximum downside safety</li>
      <li><strong>50%</strong> — stock is exactly in the middle of its annual range</li>
      <li><strong>100%</strong> — stock is at exactly its 52-week high: minimum near-term upside</li>
    </ul>
    <h3>Why Dow 30 specifically?</h3>
    <p>Benjamin Graham — without explicitly stating this rule — consistently focused on large, established, dividend-paying companies. The Dow 30 is a curated list of exactly those companies. Because every constituent is a sector leader with a long history of profitability, a stock near its annual low is statistically likely to recover — whereas the same signal in a small-cap or growth stock could mean genuine deterioration.</p>
    <h3>How to use this ranking</h3>
    <p>This ranking is a <strong>secondary filter</strong>, not a standalone buy signal. The recommended workflow:</p>
    <ol style="padding-left:20px;font-size:13px;color:#374151;line-height:1.8">
      <li>Identify companies in the <strong>green zone</strong> (52w Position &lt; 33%)</li>
      <li>Check the MoS% column — a positive margin confirms the DCF model also agrees the stock is cheap</li>
      <li>Review the P/E and P/B — they should be below sector averages</li>
      <li>Verify the company still pays dividends and has stable revenue (outside this tool)</li>
      <li>Consider a <strong>staged entry</strong> — buy a portion now, add more if the stock drops further</li>
    </ol>
    <div class="info-box blue" style="margin-top:16px">
      <strong>Real example (August 2026):</strong> NKE (Nike) ranked #1 at 3.7% of its annual range — trading just 3.7% above its 52-week low of $38.86 while maintaining positive FCF and a P/E of 20×. This is precisely the type of setup this ranking is designed to surface.
    </div>
  </div>

  <div class="explainer">
    <h2>Known Limitations</h2>
    <div class="sub">What this ranking cannot capture</div>
    <ul>
      <li style="margin-bottom:8px"><strong>Trend vs. level</strong> — a stock could be at its 52-week low because it fell today (temporary) or because it has been declining for a year (structural). Always check the chart.</li>
      <li style="margin-bottom:8px"><strong>Sector context</strong> — a 52-week low during a sector-wide downturn means something different from an individual company's low.</li>
      <li style="margin-bottom:8px"><strong>Dividend cuts</strong> — not captured here; verify dividend status separately.</li>
      <li style="margin-bottom:8px"><strong>Recent index changes</strong> — a newly added Dow component may have a short 52-week history; treat its position gauge with caution.</li>
    </ul>
  </div>
"""

# ── HTML shell ────────────────────────────────────────────────────────────────

_HTML_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="page">
{body}
  <div class="footer">
    <p>Generated by <strong>Stock Screener &amp; Intrinsic Value Engine</strong>
    &nbsp;·&nbsp; Python 3.11+ &nbsp;·&nbsp; yfinance &nbsp;·&nbsp; DuckDB
    &nbsp;·&nbsp; 100% local execution</p>
    <p style="margin-top:6px">Report date: {run_date} &nbsp;·&nbsp; Data run: {data_date}</p>
    <p style="margin-top:6px;color:#c0c4cb">For informational purposes only. Not financial advice.</p>
    <p style="margin-top:12px;padding-top:12px;border-top:1px solid #e5e7eb">Made with IBM Bob</p>
  </div>
</div>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate HTML executive report from screener CSV"
    )
    parser.add_argument("--csv",  default=None, help="Path to screener CSV (default: most recent in data/reports/)")
    parser.add_argument("--out",  default=None, help="Output HTML path (default: same folder, .html extension)")
    args = parser.parse_args()

    reports_dir = Path(__file__).parent.parent / "data" / "reports"

    if args.csv:
        csv_path = Path(args.csv)
    else:
        # Auto-detect: prefer most recent CSV of any screener type
        csvs = sorted(
            reports_dir.glob("*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not csvs:
            print("ERROR: No CSV found in data/reports/. Run the screener first.", file=sys.stderr)
            sys.exit(1)
        csv_path = csvs[0]
        print(f"Auto-detected: {csv_path}")

    if not csv_path.exists():
        print(f"ERROR: File not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows   = _load_rows(csv_path)
    failed = _detect_failed(csv_path)

    if not rows:
        print("ERROR: CSV is empty.", file=sys.stderr)
        sys.exit(1)

    if _is_dow30(csv_path):
        html = _build_dow30_html(rows, csv_path, failed)
    else:
        html = _build_deep_value_html(rows, csv_path, failed)

    out_path = Path(args.out) if args.out else csv_path.with_suffix(".html")
    out_path.write_text(html, encoding="utf-8")
    print(f"Report saved to: {out_path}")


if __name__ == "__main__":
    main()
