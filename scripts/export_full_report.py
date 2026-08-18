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


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
       font-size: 14px; line-height: 1.6; background: #f0f2f5; color: #1f2328; }
.page { max-width: 1020px; margin: 0 auto; padding: 32px 20px 60px; }

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

/* screener table */
.stbl { width:100%; border-collapse:collapse; font-size:13px; }
.stbl thead th { background:#1f2328; color:#fff; padding:10px 12px;
                 font-size:11px; text-transform:uppercase; letter-spacing:.05em;
                 text-align:left; white-space:nowrap; }
.stbl thead th.r { text-align:right; }
.stbl tbody td { padding:10px 12px; border-bottom:1px solid #f0f2f5;
                 vertical-align:middle; }
.stbl tbody tr:last-child td { border-bottom:none; }
.stbl tbody tr:hover td { background:#f7f8fa; }
.stbl td.r { text-align:right; }
.ticker-lbl { font-weight:800; font-size:14px; }
.company-lbl { font-size:12px; color:#57606a; }

/* gauge bar */
.gauge-wrap { display:flex; align-items:center; gap:8px; }
.gauge-track { flex:1; min-width:60px; height:7px; background:#e5e7eb;
               border-radius:4px; overflow:hidden; }
.gauge-fill  { height:100%; border-radius:4px; }
.gauge-pct   { font-size:12px; font-weight:700; width:42px; text-align:right; flex-shrink:0; }

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

/* footer */
.footer { text-align:center; font-size:11px; color:#8d96a0;
          border-top:1px solid #e5e7eb; padding-top:20px; margin-top:40px; }

@media(max-width:640px){
  .header-meta, .bt-header, .stats-bar { flex-direction:column; }
  .stbl { font-size:12px; }
  .stbl th, .stbl td { padding:7px 8px; }
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


def _build_screener_section(profile_key: str, rows: list[dict], run_ts: str) -> str:
    meta = _PROFILE_META.get(profile_key, {
        "label": profile_key.replace("_", " ").title(),
        "icon": profile_key[:2].upper(),
        "desc": "",
        "colour": "#3b82d4",
    })
    colour = meta["colour"]
    n = len(rows)
    if n == 0:
        return f"""
        <span class="section-anchor" id="{profile_key}"></span>
        <div class="section">
          <div class="profile-badge" style="background:{colour}11;border-color:{colour}44;color:{colour}">
            {meta['icon']} &nbsp; {meta['label']}
          </div>
          <div class="section-title">{meta['label']} Screen</div>
          <div class="section-sub">No companies passed this profile in the most recent run.</div>
        </div>"""

    best_mos = _fv(rows[0].get("MoS%", "")) or 0.0
    avg_mos  = sum((_fv(r.get("MoS%","")) or 0) for r in rows) / n

    # ── KPI pills ─────────────────────────────────────────────────────────────
    pills = f"""
    <div class="stats-bar">
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{n}</div>
        <div class="sp-label">Passed</div>
      </div>
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{best_mos:.0f}%</div>
        <div class="sp-label">Best MoS</div>
      </div>
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{avg_mos:.0f}%</div>
        <div class="sp-label">Avg MoS</div>
      </div>
      <div class="stat-pill">
        <div class="sp-value" style="color:{colour}">{run_ts}</div>
        <div class="sp-label">Data Run</div>
      </div>
    </div>"""

    # ── Table ─────────────────────────────────────────────────────────────────
    rows_html = ""
    for i, row in enumerate(rows):
        mos_v  = _fv(row.get("MoS%","")) or 0.0
        mc     = _mos_colour(mos_v)
        grade, glabel = _mos_grade(mos_v)
        pos_v  = _fv(row.get("52w Position%",""))
        pc     = _pos_colour(pos_v) if pos_v is not None else "#8d96a0"
        pos_bar = (
            f'<div class="gauge-wrap">'
            f'<div class="gauge-track"><div class="gauge-fill" style="width:{min(pos_v,100):.1f}%;background:{pc}"></div></div>'
            f'<div class="gauge-pct" style="color:{pc}">{pos_v:.0f}%</div></div>'
            if pos_v is not None else "—"
        )
        mos_bar = (
            f'<div class="gauge-wrap">'
            f'<div class="gauge-track"><div class="gauge-fill" style="width:{min(mos_v,100):.1f}%;background:{mc}"></div></div>'
            f'<div class="gauge-pct" style="color:{mc}">{mos_v:.0f}%</div></div>'
        )
        dcf_model = row.get("DCF Model","").strip() or "—"
        rows_html += f"""<tr>
          <td><span style="font-weight:800;color:#3b82d4;font-size:14px">#{i+1}</span></td>
          <td>
            <div class="ticker-lbl">{row.get('Ticker','')}</div>
            <div class="company-lbl">{row.get('Company','')}</div>
          </td>
          <td style="color:#57606a;font-size:12px">{row.get('Sector','') or '—'}</td>
          <td class="r">{_fmt(row.get('Price',''),2,prefix='$')}</td>
          <td class="r">{_fmt(row.get('DCF Avg',''),2,prefix='$')}</td>
          <td style="min-width:120px">{mos_bar}</td>
          <td style="min-width:120px">{pos_bar}</td>
          <td class="r">{_fmt(row.get('P/E',''),1,suffix='x')}</td>
          <td class="r">{_fmt(row.get('P/B',''),2,suffix='x')}</td>
          <td class="r">{_fmt(row.get('EV/EBITDA',''),1,suffix='x')}</td>
          <td class="r">{_fmt(row.get('P/FCF',''),1,suffix='x')}</td>
          <td class="r">{_fmt(row.get('NetDebt/EBITDA',''),2,suffix='x')}</td>
          <td style="text-align:center">{_quality_badge(row.get('Piotroski',''), 'piotroski')}</td>
          <td class="r">{_quality_badge(row.get('ROIC%',''), 'roic')}</td>
          <td style="text-align:center">
            <span style="font-size:11px;background:#f0f2f5;padding:2px 7px;border-radius:4px;font-weight:600">{dcf_model}</span>
          </td>
          <td class="r" style="font-weight:800">
            <span style="color:{mc}">{grade}</span>
            <div style="font-size:10px;color:#8d96a0;font-weight:400">{glabel}</div>
          </td>
        </tr>"""

    return f"""
    <span class="section-anchor" id="{profile_key}"></span>
    <div class="section">
      <div class="profile-badge" style="background:{colour}11;border-color:{colour}44;color:{colour}">
        {meta['icon']} &nbsp; {meta['label']}
      </div>
      <div class="section-title">{meta['label']} Screen</div>
      <div class="section-sub">{meta['desc']}</div>
      {pills}
      <div style="overflow-x:auto">
        <table class="stbl">
          <thead><tr>
            <th>#</th>
            <th>Ticker / Company</th>
            <th>Sector</th>
            <th class="r">Price</th>
            <th class="r">Intrinsic Value</th>
            <th>Margin of Safety</th>
            <th>52w Position</th>
            <th class="r">P/E</th>
            <th class="r">P/B</th>
            <th class="r">EV/EBITDA</th>
            <th class="r">P/FCF</th>
            <th class="r">Net Debt/EBITDA</th>
            <th style="text-align:center">Piotroski</th>
            <th class="r">ROIC</th>
            <th style="text-align:center">DCF Model</th>
            <th class="r">Grade</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
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
      <div style="overflow-x:auto">
        <table class="bt-tbl">
          <thead><tr>
            <th>Year</th>
            <th class="r">Portfolio Return</th>
            <th class="r">S&amp;P 500 Return</th>
            <th class="r">Excess vs SPX</th>
            <th class="r">Wins / Picks</th>
            <th class="r">Win Rate</th>
            <th>Selected Tickers</th>
          </tr></thead>
          <tbody>{tbl_rows}</tbody>
        </table>
      </div>

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


# ── Full report builder ───────────────────────────────────────────────────────

def build_full_report(out_path: Path) -> None:
    now = datetime.now().strftime("%d %B %Y, %H:%M")

    # Load most recent CSV for each profile
    profile_sections = []
    total_passed = 0
    for key in ("deep_value", "buffett_quality", "high_fcf_yield", "quality_value"):
        p = _most_recent(f"*_{key}.csv", exclude_backtest=True)
        if p is None:
            rows, ts = [], "—"
        else:
            rows = _load_csv(p)
            ts   = p.stem[:15]
            try: ts = datetime.strptime(ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
            except Exception: pass
        total_passed += len(rows)
        profile_sections.append(_build_screener_section(key, rows, ts))

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
          <div style="overflow-x:auto">
            <table class="stbl">
              <thead><tr>
                <th>#</th><th>Ticker</th><th>Company</th><th>Sector</th>
                <th class="r">Price</th><th>52w Position (lower = better)</th>
                <th class="r">P/E</th><th class="r">P/B</th><th class="r">MoS%</th>
              </tr></thead>
              <tbody>{trows}</tbody>
            </table>
          </div>
        </div>"""

    bt_section = _build_backtest_section(bt_rows, bt_ts) if bt_rows else ""

    # ── TOC ───────────────────────────────────────────────────────────────────
    toc_links = "".join(
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
      <div class="section-title">Methodology & V2 Feature Summary</div>
      <div class="section-sub">Every analytical capability implemented in this engine</div>

      <div class="ib blue">
        <strong>Data Pipeline:</strong> S&amp;P 500 universe scraped live from Wikipedia (503 tickers).
        Financials fetched via <code>yfinance</code> with 3-retry exponential backoff, 16-field extraction,
        and full DuckDB caching. Re-runs from cache complete in ~8 seconds.
      </div>

      <table style="width:100%;border-collapse:collapse;font-size:13px;margin:16px 0">
        <thead><tr style="background:#f7f8fa">
          <th style="padding:9px 12px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a">Feature</th>
          <th style="padding:9px 12px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a">Description</th>
          <th style="padding:9px 12px;text-align:left;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#57606a">Phase</th>
        </tr></thead>
        <tbody>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>DCF — Gordon Growth Model</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">3-5yr avg FCF, 10yr projection at 5% growth, 10% WACC, terminal value</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#3b82d4">Phase 1</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>DCF — Exit Multiple</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">EBITDA projected 10yr, 12x exit multiple, net debt adjusted, discounted</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#3b82d4">Phase 1</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>DDM — Dividend Discount Model</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Fallback for Financial sector (banks/insurers) where FCF-DCF is invalid</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#3b82d4">Phase 1</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Piotroski F-Score</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">9-point accounting quality score (profitability, leverage, efficiency). ≥7 = strong.</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#7c3aed">Phase 2</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Altman Z-Score</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Bankruptcy probability model. &lt;1.0 = real distress (excluded). 1.0-2.99 = grey zone.</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#7c3aed">Phase 2</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>ROIC</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Return on Invested Capital = NOPAT / (Equity + Debt - Cash). ≥10% = wide moat.</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#7c3aed">Phase 2</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Composite Score (0–100)</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Weighted blend: MoS 40%, Piotroski 25%, ROIC 25%, 52w Position 10%</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#7c3aed">Phase 2</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Dynamic WACC</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Per-company WACC = Ke×(E/V) + Kd×(1-t)×(D/V). Beta from yfinance, Kd from interest/debt.</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#059669">Phase 3</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>US 10Y Yield (Risk-Free Rate)</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Live fetch of ^TNX via yfinance, cached in DuckDB macro_data table (4hr TTL)</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#059669">Phase 3</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Sustainable Growth Rate</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">g = ROE × Retention Ratio. Used as g in GGM if positive and &lt; WACC.</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#059669">Phase 3</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Walk-Forward Backtest</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Annual portfolio simulation vs S&amp;P 500. Nearest-trading-day resolution for holidays/weekends.</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#d97706">Phase 4</td></tr>
          <tr><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5"><strong>Sharpe / Sortino / MaxDD / Win Rate</strong></td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5">Full risk-adjusted performance metrics computed from annual returns series</td><td style="padding:9px 12px;border-bottom:1px solid #f0f2f5;color:#d97706">Phase 4</td></tr>
          <tr><td style="padding:9px 12px"><strong>Streamlit Dashboard</strong></td><td style="padding:9px 12px">Interactive web UI: sliders, profile switching, DCF sensitivity matrix 3×3</td><td style="padding:9px 12px;color:#dc2626">Phase 5</td></tr>
        </tbody>
      </table>

      <div class="ib green">
        <strong>Value Trap Guard:</strong> A company is automatically excluded if
        Net Debt/EBITDA &gt; 3.5× <em>or</em> all available FCF years are negative.
        The Altman Z-Score adds a second layer: Z &lt; 1.0 triggers exclusion when the
        <code>exclude_altman_distress</code> flag is enabled (active in quality_value and buffett_quality profiles).
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
      <div class="hm-item"><div class="hm-label">Total Passed</div><div class="hm-value">{total_passed} companies</div></div>
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
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    print(f"Full report saved: {out_path}")
    print(f"  Profiles: deep_value={len([])}, etc.")


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
