import re
content = open('docs/index.html', encoding='utf-8').read()

# Find $10,000 final values in the backtest section
idx = content.find('bt-panel')
segment = content[idx:idx+300000] if idx >= 0 else ""

# Find final portfolio values shown in the $10,000 box
finals = re.findall(r'\$(\d{1,3}(?:,\d{3})*)', segment)
print(f"Dollar values in BT section: {finals[:30]}")

# Check SPX annual bar values
spx_bars = re.finditer(r'S&amp;P 500 \(Jan.Jan real\).*?(?=S&amp;P 500 \(Jan|</div>\s*</div>\s*</div>)', segment, re.DOTALL)
print("\nVerifying SPX annual values are realistic (expected: ~28%, -20%, 24%, 24%, 17%):")
for m in re.finditer(r'([+-]?\d{1,3}\.\d)%</span></div>\s*</div>\s*<div[^>]*>\s*<div[^>]*>Excess', segment):
    print(f"  Excess: {m.group(1)}%")
