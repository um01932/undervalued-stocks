# Stock Screener & Intrinsic Value Engine — v3

> **Ce face acest proiect în două propoziții:**
> Descarcă automat datele financiare reale ale tuturor companiilor din S&P 500, NASDAQ-100,
> Russell 2000, Euro Stoxx 50 sau BET România, calculează valoarea lor intrinsecă și îți arată
> care sunt **subevaluate** — cu un raport HTML complet, interactiv și 100% local, în mai
> puțin de 5 minute, fără abonamente plătite.

---

## Cuprins

1. [Ce problemă rezolvă](#ce-problemă-rezolvă)
2. [Noutăți v3 — cele 13 sub-task-uri](#noutăți-v3--cele-13-sub-task-uri)
3. [Cum funcționează — pas cu pas](#cum-funcționează--pas-cu-pas)
4. [Profile de screening](#profile-de-screening)
5. [Metricile calculate](#metricile-calculate)
6. [Modele de evaluare](#modele-de-evaluare)
7. [Scoruri de calitate și detectare manipulare](#scoruri-de-calitate-și-detectare-manipulare)
8. [Raportul HTML — funcționalități interactive](#raportul-html--funcționalități-interactive)
9. [Backtesting vs S&P 500](#backtesting-vs-sp-500)
10. [Instalare și rulare rapidă](#instalare-și-rulare-rapidă)
11. [Toate comenzile CLI](#toate-comenzile-cli)
12. [Structura bazei de date](#structura-bazei-de-date)
13. [Structura proiectului](#structura-proiectului)
14. [Limitări cunoscute](#limitări-cunoscute)
15. [Rularea testelor](#rularea-testelor)
16. [Tehnologii folosite](#tehnologii-folosite)

---

## Ce problemă rezolvă

Găsirea acțiunilor subevaluate manual înseamnă să analizezi câte o companie pe rând —
bilanț, cont de profit, cashflow, comparație cu concurența. Pentru 500+ companii ar dura
**luni întregi**. Acest sistem face totul automat în ~5 minute.

**Principiul de bază:** dacă prețul de piață al unei companii este semnificativ mai mic
decât valoarea ei calculată (intrinsecă), există o **Marjă de Siguranță** — un buffer
care protejează investitorul dacă modelul greșește puțin.

```
Valoare intrinsecă: $100
Preț de piață:       $40
─────────────────────────
Marjă de Siguranță:  60%  ← cumperi $1 de valoare cu $0.40
```

---

## Noutăți v3 — cele 13 sub-task-uri

Versiunea 3 adaugă **13 îmbunătățiri majore** față de v2, organizate în 5 faze:

### Faza 1 — Date suplimentare expuse (ST1–ST2)

| Sub-task | Descriere | Impact |
|---|---|---|
| **ST1** | ROE, ROA, Beta, Gross Margin, Operating Margin | Afișate în Why-Buy și scoruri de calitate |
| **ST2** | **Graham Number** — al 3-lea model de valoare intrinsecă | `√(22.5 × EPS × Book Value per share)` |

**Graham Number** este o valoare conservatoare calculată direct din EPS și Book Value,
independentă de DCF. Comparat cu prețul curent dă o perspectivă extra-conservatoare.

### Faza 2 — Îmbogățire motor de evaluare (ST3–ST5)

| Sub-task | Descriere | Impact |
|---|---|---|
| **ST3** | **DCF Sensitivity Matrix 3×3** | Bear/Base/Bull × 3 rate de creștere în raportul HTML |
| **ST4** | **Beneish M-Score** (detecție manipulare contabilă) | Flaghează companiile cu risc de fraud earnings |
| **ST5** | **SBC/Share Dilution Tracking** | SBC% din FCF, ajustare FCF real, diluare acțiuni YoY |

**Beneish M-Score** analizează 8 indici contabili (DSRI, GMI, AQI, SGI, DEPI, SGAI, LVGI, TATA).
M-Score > -1.78 → risc ridicat de manipulare → flag `MANIPULATION_RISK` în raport.

**SBC (Stock-Based Compensation)** reduce FCF-ul real. Un SBC/FCF > 30% semnalează că
profitabilitatea aparentă e parțial o iluzie contabilă.

### Faza 3 — Algoritmi de ranking (ST6–ST9)

| Sub-task | Descriere | Impact |
|---|---|---|
| **ST6** | **Dividend Growth Profile** | Screen dedicat: yield ≥ 2.5%, FCF payout ≤ 70%, Net Debt/EBITDA ≤ 2.0 |
| **ST7** | **Magic Formula Greenblatt** | Ranking combinat Earnings Yield + ROIC pe tot universul |
| **ST8** | **Percentile sectoriale** | P/E, P/FCF, EV/EBITDA față de media sectorului (nu față de tot S&P 500) |
| **ST9** | **Score History + Sparklines** | Evoluția scorului compozit în timp, vizualizare SVG inline |

**Magic Formula** (Joel Greenblatt): sortează toate companiile după `rang(EarningsYield) + rang(ROIC)`.
Companiile cu cel mai bun raport calitate/preț simultan → Top 30 afișate.

**Percentile sectoriale** rezolvă problema "comparare mere cu portocale": o companie Tech
cu P/E 20× poate fi ieftină față de sectorul ei (median 35×), chiar dacă pare scumpă absolut.

### Faza 4 — Extindere univers (ST10–ST11)

| Sub-task | Descriere | Tickers |
|---|---|---|
| **ST10** | **Russell 2000** (small-cap SUA) | ~2000 tickers (Wikipedia scrape + 50 fallback) |
| **ST11** | **Euro Stoxx 50** + **BET România** | 50 blue-chips europene + 18 tickers BET cu sufix `.RO` |

```bash
# Russell 2000 — oportunități small-cap
python src/main.py --universe russell2000 --profile deep_value --workers 10

# Euro Stoxx 50 — piețe europene
python src/main.py --universe eurostoxx50 --profile buffett_quality --workers 8

# BET România — piața locală
python src/main.py --universe bet --profile dividend_growth --workers 4
```

> **Notă yfinance:** tickerele europene folosesc sufixe standard:
> `.PA` (Paris), `.AS` (Amsterdam), `.DE` (Xetra), `.MI` (Milano), `.RO` (București)

### Faza 5 — UX / Interactivitate (ST12–ST13)

| Sub-task | Descriere | Fără server |
|---|---|---|
| **ST12** | **Live Frontend Filtering** | Sector dropdown + Min MoS% + Max P/E + Max P/FCF + Min Piotroski |
| **ST13** | **Watchlist + localStorage** | Star ⭐ per companie, persistent, export CSV |

**Filtrare live (ST12):** fiecare tabel din raportul HTML are o bară de filtre. Schimbi
un criteriu → rândurile se ascund/afișează instant, contorul "Showing N of M" se actualizează.
100% client-side JavaScript, zero server, funcționează offline.

**Watchlist (ST13):** click pe ☆ lângă orice ticker → salvat în `localStorage` al browserului.
La reîncărcarea paginii, stelele sunt restaurate. Secțiunea "My Watchlist" apare în top cu
toate companiile starred. Butonul "⬇ Export CSV" descarcă un CSV cu companiile favorite.

---

## Cum funcționează — pas cu pas

```
┌─────────────────────────────────────────────────────────────────┐
│  PASUL 1 — Universe                                             │
│  Descarcă lista live a companiilor din sursele selectate:        │
│  S&P 500 / NASDAQ-100 / Dow 30 / Russell 2000 / Euro Stoxx 50  │
│  / BET Romania / World / Custom CSV                             │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 2 — Fetch & Cache                                        │
│  Descarcă date financiare via Yahoo Finance (yfinance),         │
│  8+ firme simultan. Salvează în DuckDB local.                   │
│  Durată: ~3–5 min (dependent de universum)                      │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 3 — Valuation Engine (v3)                                │
│  • 6 multipli de piață + percentile sectoriale                  │
│  • 3 modele DCF (GGM + Exit Multiple + Graham Number)           │
│  • Marjă de Siguranță % + DCF Sensitivity Matrix 3×3            │
│  • ROE, ROA, Beta, Gross/Operating Margin                       │
│  • Piotroski F-Score, Altman Z, ROIC                            │
│  • Beneish M-Score (detecție manipulare)                        │
│  • SBC/FCF ratio + Share Dilution tracking                      │
│  • Dividend yield, payout ratio FCF                             │
│  • Score History (evoluție în timp)                             │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 4 — Screen & Rank                                        │
│  5 profile predefinite + Magic Formula Greenblatt               │
│  + Sector-relative percentiles                                  │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 5 — Export                                               │
│  • CSV/Excel cu toate metricile (30+ coloane)                   │
│  • Raport HTML interactiv cu filtrare live + watchlist          │
│  • Auto-deploy GitHub Pages                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Profile de screening

Fiecare profil este **complet independent** — aceleași date, filosofii diferite.

### 🔵 Deep Value — `--profile deep_value`
> *Inspirat din Benjamin Graham — "The Intelligent Investor" (1949)*

| Criteriu | Prag |
|---|---|
| P/E | ≤ 15× |
| P/B | ≤ 1.5× |
| EV/EBITDA | ≤ 8× |
| P/FCF | ≤ 15× |
| Net Debt/EBITDA | ≤ 2.5× |
| Margin of Safety | ≥ 20% |
| Piotroski | ≥ 4/9 |

---

### 🟣 Buffett Quality — `--profile buffett_quality`
> *"Calitate la un preț rezonabil"*

| Criteriu | Prag |
|---|---|
| ROIC | ≥ 10% |
| ROE | ≥ 12% |
| Piotroski | ≥ 6/9 |
| Gross Margin | ≥ 30% |
| P/FCF | ≤ 20× |
| EV/EBITDA | ≤ 12× |
| Margin of Safety | ≥ 15% |
| Altman Z | ≥ 1.0 |

---

### 🟢 High FCF Yield — `--profile high_fcf_yield`

| Criteriu | Prag |
|---|---|
| P/FCF | ≤ 12× |
| Margin of Safety | ≥ 30% |

---

### 🟡 Quality Value — `--profile quality_value`

| Criteriu | Prag |
|---|---|
| EV/EBITDA | ≤ 10× |
| P/E | ≤ 18× |
| ROIC | ≥ 8% |
| Piotroski | ≥ 6/9 |
| Altman Z | ≥ 1.0 |
| Margin of Safety | ≥ 20% |

---

### 🔵 Dividend Growth — `--profile dividend_growth` *(NOU v3)*
> *Income investing sustenabil*

| Criteriu | Prag |
|---|---|
| Dividend Yield | ≥ 2.5% |
| FCF Payout Ratio | ≤ 70% |
| Net Debt/EBITDA | ≤ 2.0× |
| Piotroski | ≥ 5/9 |
| Beneish Flag | Fără MANIPULATION_RISK |

---

### 🔮 Magic Formula Greenblatt *(NOU v3)*

Ranking combinat pe tot universul (fără filtre stricte):

```
Magic Rank = rank(Earnings Yield = E/P) + rank(ROIC)
```

Top 30 companii cu cel mai bun rang combinat. Exclude financiale și utilități.

---

## Metricile calculate

### Multipli de piață (relativi)

| Metric | Ce măsoară | Bun dacă |
|---|---|---|
| **P/E** | Ani de profit ca să recuperezi prețul | ≤ 15× |
| **P/B** | Prețul față de activele nete contabile | ≤ 1.5× |
| **EV/EBITDA** | Costul total de achiziție / profit operațional | ≤ 8× |
| **P/FCF** | Prețul față de cashflow real generat | ≤ 12× |
| **Net Debt/EBITDA** | Ani necesari să plătești datoriile | ≤ 2.5× |
| **PEG Ratio** | P/E ajustat la creștere | ≤ 1.0 |

### Percentile sectoriale *(NOU v3)*

| Metric | Descriere |
|---|---|
| `sector_pe_percentile` | P/E al companiei față de distribuția P/E în sectorul ei |
| `sector_pfcf_percentile` | P/FCF față de sector |
| `sector_ev_percentile` | EV/EBITDA față de sector |

**Interpretare:** percentila 20 = mai ieftină decât 80% din companiile din același sector.

### Indicatori de profitabilitate *(NOU v3)*

| Metric | Descriere |
|---|---|
| **ROE** | Return on Equity — eficiența capitalului propriu |
| **ROA** | Return on Assets — eficiența activelor totale |
| **Gross Margin** | Marja brută (Gross Profit / Revenue) |
| **Operating Margin** | Marja operațională (EBIT / Revenue) |
| **Beta** | Volatilitatea față de piață (1.0 = piața) |

### SBC & Diluare *(NOU v3)*

| Metric | Descriere |
|---|---|
| **SBC / FCF %** | Stock-Based Compensation ca % din Free Cash Flow |
| **SBC-Adjusted FCF** | FCF real după scăderea SBC |
| **Shares Dilution %** | Variația numărului de acțiuni YoY |

> **Atenție:** SBC/FCF > 30% → profitabilitatea aparentă e parțial contabilă.
> SBC-Adjusted FCF e mai conservator și mai relevant pentru evaluare.

### Dividend metrics *(NOU v3)*

| Metric | Descriere |
|---|---|
| **Dividend Yield** | Dividend anual / Preț curent |
| **FCF Payout Ratio** | Dividend total / Free Cash Flow |

---

## Modele de evaluare

Motorul calculează **trei modele independente** și face media ponderată.

### Model 1 — Gordon Growth Model (GGM)

1. Extrage Free Cash Flow din ultimii 3–5 ani
2. Cere minim **3 ani de FCF pozitiv** (altfel: `INSUFFICIENT_DATA`)
3. Proiectează 10 ani la rata de creștere configurabilă (default 5%)
4. Valoare terminală: `TV = FCF × (1+g) / (r−g)`
5. Actualizează cu WACC dinamic per companie

### Model 2 — Exit Multiple

1. Folosește EBITDA curent ca bază
2. Proiectează 10 ani la 5% creștere
3. Valoare terminală = `EBITDA_10 × 12×` (multiplul median S&P 500)
4. Scade datoriile nete → valoare equity

### Model 3 — Graham Number *(NOU v3)*

```
Graham Number = √(22.5 × EPS × Book Value per share)
```

Valoarea maximă la care Graham ar considera o companie ieftină:
- **≥ 40% discount** față de preț → semnal puternic
- Bazat strict pe date contabile fundamentale

### DCF Sensitivity Matrix *(NOU v3)*

Pentru fiecare companie, raportul HTML afișează o matrice 3×3:

```
              Rate de creștere
         Low (−2%)  Base (0%)  High (+2%)
Bear   [   $X.XX  |  $X.XX  |  $X.XX  ]
Base   [   $X.XX  |  $X.XX  |  $X.XX  ]   ← valoarea centrală = DCF Avg
Bull   [   $X.XX  |  $X.XX  |  $X.XX  ]
```

Permite înțelegerea intervalului de incertitudine al modelului.

### Parametri DCF (configurabili)

| Parametru | Default | Rațiune |
|---|---|---|
| `growth_rate` | 5% | Media creșterii economice pe termen lung |
| `discount_rate` | 10% | Randamentul istoric mediu S&P 500 |
| `terminal_growth` | 2.5% | Inflație + creștere nominală |
| `projection_years` | 10 | Orizontul standard DCF |
| `exit_multiple` | 12× | Multiplul median EV/EBITDA S&P 500 |

---

## Scoruri de calitate și detectare manipulare

### Piotroski F-Score (0–9)

| Grup | Criterii |
|---|---|
| **Profitabilitate** (4 pts) | ROA > 0, CFO > 0, ROA crescut YoY, Accruals < 0 |
| **Leverage** (3 pts) | Leverage scăzut YoY, Lichiditate crescută YoY, Fără diluare |
| **Eficiență** (2 pts) | Marjă brută crescută YoY, Rotație active crescută YoY |

**Interpretare:** ≥ 7 = fundamentele se îmbunătățesc activ. 4–6 = stabil. < 4 = deteriorare.

### Altman Z-Score

```
Z = 1.2×X1 + 1.4×X2 + 3.3×X3 + 0.6×X4 + 1.0×X5
```

- **Z < 1.0** → zonă de risc (exclus din quality_value și buffett_quality)
- **Z 1.0–2.99** → zonă gri
- **Z ≥ 3.0** → sănătos financiar

### ROIC — Return on Invested Capital

```
ROIC = NOPAT / (Equity + Debt − Cash)
```

- **≥ 15%** → avantaj competitiv clar (moat)
- **≥ 10%** → depășește costul tipic al capitalului
- **< 5%** → eficiență slabă

### Beneish M-Score *(NOU v3)*

Detectează probabilitatea de **manipulare a câștigurilor**:

| Indice | Ce detectează |
|---|---|
| **DSRI** | Days Sales Receivables Index — recunoaștere prematură venituri |
| **GMI** | Gross Margin Index — deteriorare marje |
| **AQI** | Asset Quality Index — capitalizare costuri nepotrivită |
| **SGI** | Sales Growth Index — creștere sustenabilă vs artificială |
| **LVGI** | Leverage Index — creștere rapidă a îndatorării |
| **TATA** | Total Accruals to Total Assets — accruals vs cashflow real |

**M-Score > -1.78 → flag MANIPULATION_RISK** în raportul HTML.

> *Notă: DEPI și SGAI setate la 1.0 (neutral) — date de depreciere granulare nu sunt disponibile în yfinance.*

### Score History & Sparklines *(NOU v3)*

La fiecare rulare, scorul compozit 0–100 per companie este salvat în tabela `score_history`
din DuckDB. Raportul HTML afișează un sparkline SVG inline care arată evoluția scorului
în timp (ultimele N rulări).

---

## Raportul HTML — funcționalități interactive

```bash
# Generează raportul CONSOLIDAT cu toate profilurile
python scripts/export_full_report.py
# → salvat automat în docs/index.html + push GitHub Pages
```

### Structura raportului

1. **★ Top Convictions** — companiile care trec prin 2+ profile simultan
2. **Watchlist ⭐** *(NOU v3)* — companiile starred din localStorage
3. **Deep Value Screen** — cu filter bar live
4. **Buffett Quality Screen** — cu filter bar live
5. **High FCF Yield Screen** — cu filter bar live
6. **Quality Value Screen** — cu filter bar live
7. **Dividend Growth Screen** *(NOU v3)* — cu filter bar live
8. **Magic Formula Top 30** *(NOU v3)* — ranking Greenblatt
9. **Dow Jones 30 Ranking** — gauge 52w position
10. **Walk-Forward Backtest** — vs ^GSPC, grafic dual per an
11. **Methodology** — explicații complete

### Filter Bar Live *(NOU v3)*

Fiecare secțiune de screening are o bară de filtre deasupra tabelului:

```
[Sector ▼] [Min MoS% ___] [Max P/E ___] [Max P/FCF ___] [Min Pio ___] [Reset] Showing 4 of 10
```

- **Sector dropdown** — populat automat din datele tabelului
- **Filtre numerice** — Min Margin of Safety %, Max P/E, Max P/FCF, Min Piotroski
- **Reset** — șterge toate filtrele
- **Showing N of M** — contor live actualizat la fiecare schimbare
- Funcționează și pe rândurile din "Show all N remaining companies"

### Watchlist + localStorage *(NOU v3)*

- Click **☆** lângă orice ticker → adăugat la watchlist (☆ → ⭐)
- Click **⭐** → eliminat din watchlist
- **Persistent** — se salvează în `localStorage["uv_watchlist"]` al browserului
- La reîncărcare pagină → stelele sunt restaurate automat
- Secțiunea "My Watchlist" apare în top cu tabelul companiilor favorite
- **⬇ Export CSV** → descarcă `watchlist.csv` cu: ticker, company, sector, price, MoS%, P/E, P/FCF, Piotroski, Fit

### Why Buy — raționament per companie

Fiecare companie are un buton **"Why Buy →"** care extinde un panou cu:
- Raționament narativ generat automat cu cifrele reale
- Grafic OHLCV (candlestick 1 an) cu prețul curent vs valoarea intrinsecă
- DCF Sensitivity Matrix 3×3 (Bear/Base/Bull)
- Score sparkline (evoluție în timp)
- Beneish M-Score cu flaguri per indice

---

## Backtesting vs S&P 500

```bash
python src/main.py --universe sp500 --profile deep_value \
  --backtest --backtest-start 2021 --backtest-end 2024 \
  --workers 6 --export csv
```

Walk-forward anual: top-N companii din screen, ținute 12 luni, vs ^GSPC.

**Limitări importante:**
- **Look-ahead bias** — folosește fundamentalele *curente* pentru toți anii istorici
- **Survivorship bias** — include doar companiile *actuale* din index
- **Fără costuri de tranzacție** — nu include comisioane sau spread bid-ask

---

## Instalare și rulare rapidă

### Cerințe

- Python 3.11+
- `pip`
- Conexiune internet (pentru descărcarea datelor)

### Instalare

```bash
# 1. Clonează proiectul
git clone https://github.com/um01932/undervalued-stocks.git
cd undervalued-stocks

# 2. Creează virtual environment (recomandat)
python -m venv .venv
.venv\Scripts\activate        # Windows PowerShell
# source .venv/bin/activate   # macOS / Linux

# 3. Instalează dependențele
pip install -r requirements.txt
```

### Prima rulare (S&P 500, Deep Value)

```bash
# Descarcă 503 companii, calculează, exportă CSV + raport HTML
python src/main.py --universe sp500 --profile deep_value --workers 6 --export csv
python scripts/export_full_report.py
# → deschide data/reports/full_report.html în browser
# → sau vizualizează live: https://um01932.github.io/undervalued-stocks/
```

**Durată:** ~3–5 minute.

---

## Toate comenzile CLI

```bash
# ── S&P 500 ───────────────────────────────────────────────────────────────────
python src/main.py --universe sp500 --profile deep_value --workers 6 --export csv
python src/main.py --universe sp500 --profile buffett_quality --workers 6 --export csv
python src/main.py --universe sp500 --profile high_fcf_yield --workers 6 --export csv
python src/main.py --universe sp500 --profile quality_value --workers 6 --export csv
python src/main.py --universe sp500 --profile dividend_growth --workers 6 --export csv

# ── Alte universuri ────────────────────────────────────────────────────────────
python src/main.py --universe nasdaq100 --profile buffett_quality --workers 8
python src/main.py --universe dow30 --workers 6 --export csv
python src/main.py --universe russell2000 --profile deep_value --workers 10    # NOU v3
python src/main.py --universe eurostoxx50 --profile buffett_quality --workers 8  # NOU v3
python src/main.py --universe bet --profile dividend_growth --workers 4          # NOU v3

# ── Custom CSV ─────────────────────────────────────────────────────────────────
python src/main.py --universe custom --csv-path my_tickers.csv --profile deep_value

# ── Rapoarte HTML ─────────────────────────────────────────────────────────────
python scripts/export_full_report.py                          # consolidat + auto-push GitHub Pages
python scripts/export_html_report.py                          # individual (ultimul CSV)
python scripts/export_html_report.py --csv data/reports/<ts>_deep_value.csv

# ── Backtesting ───────────────────────────────────────────────────────────────
python src/main.py --universe sp500 --profile deep_value \
  --backtest --backtest-start 2021 --backtest-end 2024 \
  --backtest-top-n 10 --workers 6 --export csv

# ── Parametri DCF personalizați ───────────────────────────────────────────────
python src/main.py --universe sp500 --profile deep_value \
  --dcf-growth 0.04 \
  --dcf-discount 0.09 \
  --dcf-terminal 0.02 \
  --dcf-years 10 \
  --dcf-exit-multiple 10.0
```

### Referință completă flags

```
Universe & Date
  --universe      sp500 | nasdaq100 | dow30 | russell2000 | eurostoxx50 | bet | world | custom
  --csv-path      CSV cu coloana 'ticker'; necesar când --universe custom
  --workers       Fire de execuție paralele                     (default: 8)
  --rps           Requests per secundă Yahoo Finance            (default: 2.0)

Screener
  --profile       deep_value | buffett_quality | high_fcf_yield | quality_value | dividend_growth

Export
  --export        csv | excel | both | none                     (default: csv)

Backtest
  --backtest                  Activează modul backtest
  --backtest-start YEAR       Primul an (default: 2018)
  --backtest-end   YEAR       Ultimul an (default: 2024)
  --backtest-top-n N          Câte companii în portofoliu       (default: 10)
  --backtest-benchmark TICKER Benchmark (default: ^GSPC)

DCF (toți cu prefix --dcf-)
  --dcf-growth     Rata creștere FCF (default: 0.05)
  --dcf-discount   Rata de discount / WACC (default: 0.10)
  --dcf-terminal   Rata creștere terminală (default: 0.025)
  --dcf-years      Ani proiecție (default: 10)
  --dcf-exit-multiple  Multiplu EV/EBITDA terminal (default: 12.0)
```

---

## Structura bazei de date

Datele sunt stocate în `data/cache.duckdb` (gitignored, creat automat).

```
Tabelă              Ce conține
────────────────────────────────────────────────────────────────────
ticker_info         Snapshot curent per companie:
                    preț, P/E, P/B, FCF, datorii, beta, ROE, ROA,
                    gross_margin, operating_margin, dividende,
                    52w low/high, sector, industrie

financials          Cont de profit: 5 ani anuali
                    (venituri, profit brut, EBIT, profit net)

balance_sheet       Bilanț: 5 ani anuali
                    (active totale, pasive, datorii, cash, equity)

cashflow            Cashflow: 5 ani anuali
                    (CFO, CapEx, FCF, stock_based_compensation)

price_history       Prețuri zilnice de închidere (backtest)

ohlcv_cache         OHLCV zilnic (Why-Buy charts, TTL 1 zi)

macro_data          us_10y_yield (rata risk-free pentru WACC/DCF)

score_history       Evoluție scor compozit per ticker în timp
                    (alimentat la fiecare rulare main.py)
```

### Politica de refresh

```
ticker_info     → TTL: 0  (mereu fresh)
financials      → TTL: 0  (mereu fresh)
balance_sheet   → TTL: 0  (mereu fresh)
cashflow        → TTL: 0  (mereu fresh)
ohlcv_cache     → TTL: 1 zi
price_history   → TTL: 1 zi (closes istorice nu se schimbă)
macro_data      → TTL: 1 zi
score_history   → append-only (nu se șterg înregistrările vechi)
```

---

## Structura proiectului

```
UndervaluedStocks/
│
├── src/
│   ├── universe.py       Universuri: S&P 500, NASDAQ-100, Dow 30,
│   │                     Russell 2000, Euro Stoxx 50, BET Romania,
│   │                     World, Custom CSV; scraping Wikipedia + fallback
│   │
│   ├── fetcher.py        Pipeline paralel: DuckDB cache thread-safe,
│   │                     throttle, retry exponential, auto-migration,
│   │                     score_history append, SBC în cashflow schema
│   │
│   ├── engine.py         Motor evaluare: multipli, GGM DCF, Exit Multiple,
│   │                     Graham Number, DDM fallback, Piotroski, Altman Z,
│   │                     ROIC, WACC dinamic, Beneish M-Score, SBC/FCF,
│   │                     share dilution, dividend metrics, sector percentiles,
│   │                     ROE, ROA, Beta, Gross/Operating Margin
│   │
│   ├── screener.py       5 profile predefinite + compute_sector_percentiles
│   │                     + apply_magic_formula; 30+ coloane output
│   │
│   ├── backtester.py     Walk-forward backtest anual vs benchmark
│   │
│   └── main.py           CLI: 8 universuri, 5 profile, DCF params
│
├── scripts/
│   ├── export_full_report.py   Raport HTML consolidat:
│   │                           • Filter bars live (ST12)
│   │                           • Watchlist + localStorage (ST13)
│   │                           • DCF Sensitivity Matrix (ST3)
│   │                           • Beneish badges (ST4)
│   │                           • Score sparklines (ST9)
│   │                           • Magic Formula section (ST7)
│   │                           • Auto-push GitHub Pages
│   │
│   ├── export_html_report.py   Raport HTML individual
│   └── gen_global_tickers.py   Regenerează data/global_tickers.csv
│
├── tests/
│   ├── unit/             339 teste unitare (mocked, fără internet)
│   │   ├── test_universe.py    incl. Russell2000, EuroStoxx50, BET (ST10, ST11)
│   │   ├── test_fetcher.py     incl. TestScoreHistory (ST9)
│   │   ├── test_engine.py      incl. Graham Number (ST2), Beneish (ST4),
│   │   │                             SBC/dilution (ST5), dividend metrics (ST6),
│   │   │                             ROE/ROA/Beta/GrossMargin (ST1)
│   │   ├── test_screener.py    incl. ROE filter, Magic Formula (ST7),
│   │   │                             Sector percentiles (ST8), DividendGrowth (ST6)
│   │   ├── test_backtester.py
│   │   └── test_dashboard_imports.py
│   │
│   └── integration/      Teste reale (necesită internet): pytest -m integration
│
├── data/
│   ├── global_tickers.csv    ~552 tickere internaționale (în git)
│   ├── cache.duckdb          Cache DuckDB local (gitignored)
│   └── reports/              CSV / Excel / HTML (gitignored)
│
├── docs/
│   └── index.html            GitHub Pages (auto-generat de export_full_report.py)
│
├── config/
│   └── screener_profiles.yaml   Override YAML opțional pentru praguri
│
├── v3-improvements-plan.md   Plan complet v3 (toate 13 sub-task-uri)
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## Limitări cunoscute

| Limitare | Detaliu |
|---|---|
| **Date actuale, nu istorice** | Screener-ul folosește fundamentalele *curente*. |
| **Fără analiză calitativă** | Managementul, moat-ul, poziția competitivă — invizibile. |
| **DCF sensibil la ipoteze** | O schimbare de 2% în rata de discount mișcă valoarea cu 20–40%. |
| **Sectorul financiar** | Băncile/asigurătorii rutați automat spre DDM, MoS mai puțin precis. |
| **Backtesting look-ahead** | Backtestul folosește datele curente pentru toți anii istorici. |
| **Survivorship bias** | Universul conține doar companiile *actuale* din index. |
| **Beneish DEPI/SGAI** | Setate neutral (1.0) — datele granulare de depreciere nu sunt în yfinance. |
| **Piețe europene** | yfinance acoperire variabilă pentru `.RO`; unele BET tickers pot lipsi. |
| **Russell 2000** | Wikipedia nu listează toți 2000 constituenți — se folosește fallback 50 tickers. |

---

## Rularea testelor

```bash
# Toate testele unitare — rapide, mocked, fără internet (339 teste)
pytest tests/unit/ -q

# Verbose cu coverage
pytest tests/unit/ -v

# Teste de integrare — necesită internet (~60 sec)
pytest -m integration

# Test specific
pytest tests/unit/test_universe.py -v -k "russell2000"
```

---

## Tehnologii folosite

| Librărie | Rol |
|---|---|
| `yfinance` | Descărcare date financiare Yahoo Finance |
| `duckdb` | Baza de date locală columnar (cache, query rapid) |
| `pandas` | Procesare DataFrames situații financiare |
| `pydantic` v2 | Validare și tipizare date (TickerData, ValuationResult) |
| `rich` | Output CLI colorat, tabele, progress bar |
| `tqdm` | Progress bar fetch |
| `streamlit` | Dashboard web interactiv |
| `plotly` | Grafice interactive în dashboard |
| `pytest` | Framework teste unitare + integrare |

---

## Licență

MIT — liber de utilizat, modificat și distribuit. **Nu constituie sfat financiar.**

> *"The stock market is a device for transferring money from the impatient to the patient."*
> — Warren Buffett

> *"Price is what you pay. Value is what you get."*
> — Warren Buffett

---

**GitHub Pages (live report):** https://um01932.github.io/undervalued-stocks/
