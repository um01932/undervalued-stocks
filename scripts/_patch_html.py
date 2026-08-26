"""
Patch docs/index.html in-place to fix Top Overall and Top Convictions card layout.

Changes applied:
1. Top Overall: merge #-rank into Ticker cell header, wrap value cells in <span> for flex layout
2. Top Convictions: merge Conviction badge into Ticker cell header, wrap value cells in <span>
"""
import re
from pathlib import Path

src = Path("docs/index.html")
content = src.read_text(encoding="utf-8")
original_len = len(content)

# ── Fix 1: Top Overall rows ───────────────────────────────────────────────────
# Pattern: the rank cell + ticker cell in Overall rows
# Find and fix the Overall section table rows

# The old pattern had a separate <td data-label="#"> before <td data-label="Ticker">
# In the Overall section (has "overall_top" anchor)

# Strategy: use regex to find the pattern in the overall section rows
# Each row looks like:
#   <td data-label="#" ...><span ...>#N</span></td>
#   <td data-label="Ticker" ...>
#     <div ...>TICKER badge</div> ...
#   </td>

# We'll do a targeted replace using regex
# Match the # cell + ticker cell in overall context

# Pattern for overall row rank cell
rank_td_pattern = re.compile(
    r'<td data-label="#" style="width:4%;text-align:center">\s*'
    r'<span style="font-weight:800;color:(#[a-f0-9]{6});font-size:15px">(#\d+)</span>\s*'
    r'</td>\s*'
    r'(<td data-label="Ticker" style="width:13%">)\s*'
    r'(<div style="font-weight:800;font-size:14px">)(.*?)(</div>)',
    re.DOTALL
)

def merge_rank_into_ticker(m):
    rank_colour = m.group(1)
    rank_text   = m.group(2)
    ticker_td   = m.group(3)
    div_open    = m.group(4)
    div_content = m.group(5)
    div_close   = m.group(6)
    return (
        f'{ticker_td}\n'
        f'            {div_open}\n'
        f'              <span style="font-weight:800;color:{rank_colour};font-size:13px;margin-right:6px">{rank_text}</span>{div_content.strip()}{div_close}'
    )

new_content, n_overall = re.subn(rank_td_pattern, merge_rank_into_ticker, content)
print(f"Overall rank+ticker merges: {n_overall}")
content = new_content

# Fix the Overall thead - remove the # column
content = content.replace(
    '<th style="width:4%;text-align:center">#</th>\n            <th style="width:13%">Ticker / Company</th>',
    '<th style="width:17%"># &nbsp; Ticker / Company</th>'
)

# ── Fix 2: Top Convictions rows ───────────────────────────────────────────────
# Old: separate Conviction td + Ticker td
# New: merge Conviction badge into Ticker td header

conv_td_pattern = re.compile(
    r'<td data-label="Conviction" style="width:11%">\s*'
    r'<div style="font-weight:700;font-size:10px;color:(#[a-f0-9]{6});\s*'
    r'background:[^;]+;border:[^;]+;\s*'
    r'border-radius:4px;padding:2px 6px;display:inline-block">([^<]+)</div>\s*'
    r'</td>\s*'
    r'(<td data-label="Ticker" style="width:13%">)\s*'
    r'(<div style="font-weight:800;font-size:13px">)',
    re.DOTALL
)

def merge_conv_into_ticker(m):
    conv_colour = m.group(1)
    conv_label  = m.group(2)
    # Replace with merged ticker cell (width 24%)
    return (
        f'<td data-label="Ticker" style="width:24%">\n'
        f'            {m.group(4)}'
    )

new_content, n_conv_start = re.subn(conv_td_pattern, merge_conv_into_ticker, content)
print(f"Convictions ticker merges (start): {n_conv_start}")
content = new_content

# Now insert the conviction badge after Company name div, before why_btn
# Find the pattern: company div followed immediately by why_btn (in convictions context)
# We need to add the conviction badge - but we don't have easy access to the colour/label here
# A simpler approach: the conviction badge already comes from the matched text, 
# but we lost it above. Let's redo with a single-pass replacement.

# Redo with a single more complete pattern
# Reset
content = Path("docs/index.html").read_text(encoding="utf-8")
original_len = len(content)

# Single comprehensive fix for convictions
conv_full_pattern = re.compile(
    r'<td data-label="Conviction" style="width:11%">\s*'
    r'<div style="font-weight:700;font-size:10px;color:(#[a-f0-9]{6});\s*'
    r'background:[^;]+;border:[^;]+;\s*'
    r'border-radius:4px;padding:2px 6px;display:inline-block">([^<]+)</div>\s*'
    r'</td>\s*'
    r'<td data-label="Ticker" style="width:13%">\s*'
    r'(<div style="font-weight:800;font-size:13px">.*?</div>)\s*'
    r'(<div style="font-size:11px;color:#57606a">.*?</div>)\s*'
    r'(\s*(?:<button[^>]*>.*?</button>)?)',
    re.DOTALL
)

def replace_conv(m):
    conv_colour = m.group(1)
    conv_label  = m.group(2).strip()
    ticker_div  = m.group(3)
    company_div = m.group(4)
    why_btn     = m.group(5).strip()
    return (
        f'<td data-label="Ticker" style="width:24%">\n'
        f'            {ticker_div}\n'
        f'            {company_div}\n'
        f'            <div style="margin-top:4px">'
        f'<span style="font-weight:700;font-size:10px;color:{conv_colour};'
        f'background:{conv_colour}12;border:1px solid {conv_colour}33;'
        f'border-radius:4px;padding:2px 6px;display:inline-block">{conv_label}</span></div>\n'
        f'            {why_btn}'
    )

new_content, n_conv = re.subn(conv_full_pattern, replace_conv, content)
print(f"Convictions full merges: {n_conv}")
content = new_content

# Fix the Convictions thead
content = content.replace(
    '<th style="width:11%">Conviction</th>\n            <th style="width:13%">Ticker / Company</th>',
    '<th style="width:24%">Ticker / Company &nbsp; Conviction</th>'
)

# ── Fix 3: Overall rows - fix the # rank merge ────────────────────────────────
rank_td_pattern = re.compile(
    r'<td data-label="#" style="width:4%;text-align:center">\s*'
    r'<span style="font-weight:800;color:(#[a-f0-9]{6});font-size:15px">(#\d+)</span>\s*'
    r'</td>\s*'
    r'<td data-label="Ticker" style="width:13%">\s*'
    r'<div style="font-weight:800;font-size:14px">(.*?)</div>',
    re.DOTALL
)

def merge_rank_overall(m):
    rank_colour = m.group(1)
    rank_text   = m.group(2)
    div_content = m.group(3).strip()
    return (
        f'<td data-label="Ticker" style="width:17%">\n'
        f'            <div style="font-weight:800;font-size:14px">\n'
        f'              <span style="font-weight:800;color:{rank_colour};font-size:13px;margin-right:6px">{rank_text}</span>{div_content}\n'
        f'            </div>'
    )

new_content, n_overall = re.subn(rank_td_pattern, merge_rank_overall, content)
print(f"Overall rank+ticker merges: {n_overall}")
content = new_content

# Fix the Overall thead
content = content.replace(
    '<th style="width:4%;text-align:center">#</th>\n            <th style="width:13%">Ticker / Company</th>',
    '<th style="width:17%"># &nbsp; Ticker / Company</th>'
)

# Save
if len(content) != original_len or n_conv > 0 or n_overall > 0:
    src.write_text(content, encoding="utf-8")
    print(f"Saved: {src} ({len(content):,} bytes)")
    # Also sync to data/reports
    dst = Path("data/reports/full_report.html")
    if dst.exists():
        dst.write_text(content, encoding="utf-8")
        print(f"Synced: {dst}")
else:
    print("No changes made.")
