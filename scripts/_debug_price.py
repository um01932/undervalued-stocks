import duckdb
con = duckdb.connect('data/cache.duckdb')

tables = con.execute('SHOW TABLES').fetchall()
print('All tables:', [t[0] for t in tables])

# Check financials for LULU
fin = con.execute("SELECT * FROM financials WHERE ticker='LULU' LIMIT 1").fetchdf()
print('\nfinancials cols:', list(fin.columns))

# Check ticker_info for LULU
ti = con.execute("SELECT * FROM ticker_info WHERE ticker='LULU' LIMIT 1").fetchdf()
print('\nticker_info cols:', list(ti.columns))
if len(ti):
    print(ti.iloc[0].to_dict())
