import re, sys
content = open('docs/index.html', encoding='utf-8').read()

# Find the backtest panel and check for any absurd values (>200% for S&P 500 in one year)
idx = content.find('bt-panel')
if idx < 0:
    print("No bt-panel found!")
    sys.exit(1)

segment = content[idx:idx+200000]

# Find all yearly bar values (the labels inside the bar divs like "+26.3%")
vals = re.findall(r'([+-]?\d{1,4}\.\d)%</span></div>', segment)
print(f"Found {len(vals)} return values in backtest section")

numeric = []
for v in vals:
    try:
        numeric.append(float(v))
    except:
        pass

if numeric:
    print(f"Min: {min(numeric):.1f}%  Max: {max(numeric):.1f}%")
    crazy = [v for v in numeric if abs(v) > 200]
    if crazy:
        print(f"WARNING: {len(crazy)} crazy values (>200%): {crazy[:10]}")
    else:
        print("All values within ±200% — looks sane!")

# Check actual SPX annual returns shown — should be roughly in range of real history
spx_vals = []
# Look for pairs of S&P 500 bar values
for m in re.finditer(r'S&amp;P 500</span>\s*<div[^>]*>.*?([+-]?\d+\.\d)%</span>', segment, re.DOTALL):
    try:
        spx_vals.append(float(m.group(1)))
    except:
        pass
print(f"\nS&P 500 bar values found: {spx_vals[:20]}")
