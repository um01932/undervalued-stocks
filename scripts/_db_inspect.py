import duckdb
con = duckdb.connect("data/cache.duckdb", read_only=True)

print("=== PRICE HISTORY ===")
r = con.execute("SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(date), MAX(date) FROM price_history").fetchone()
print(f"  rows: {r[0]:,}   tickers: {r[1]}   date range: {r[2]} -> {r[3]}")
rows = con.execute("SELECT ticker, date, close FROM price_history ORDER BY ticker LIMIT 8").fetchall()
for row in rows:
    print(f"    {row[0]:<8}  {row[1]}  close=${row[2]:.2f}")

print("\n=== MACRO DATA (rate dobanda, risk-free rate) ===")
rows = con.execute("SELECT key, value, fetched_at FROM macro_data").fetchall()
for row in rows:
    print(f"  {row[0]:<25} = {row[1]:.4f}   fetched: {row[2]}")

print("\n=== REZUMAT COMPLET ===")
print("Tabela              Randuri    Tickers   Descriere")
print("-" * 70)
r = con.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM ticker_info").fetchone()
print(f"  ticker_info       {r[0]:>6,}     {r[1]:>4}    Preturi, multipli, FCF, debt, beta, ROE...")
for tbl, desc in [("financials","Venituri, profit net (5 ani)"), ("balance_sheet","Active, pasive, equity (5 ani)"), ("cashflow","FCF, capex, dividende (5 ani)")]:
    r = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT ticker) FROM {tbl}").fetchone()
    print(f"  {tbl:<18}{r[0]:>6,}     {r[1]:>4}    {desc}")
r = con.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM price_history").fetchone()
print(f"  price_history     {r[0]:>6,}     {r[1]:>4}    Preturi istorice (backtest)")
r = con.execute("SELECT COUNT(*) FROM macro_data").fetchone()
print(f"  macro_data        {r[0]:>6,}        —    US 10Y yield, risk-free rate")

con.close()
