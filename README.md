# Stock Screener & Intrinsic Value Engine

> **Ce face acest proiect în două propoziții:**
> Descarcă automat datele financiare reale ale tuturor celor 503 companii din S&P 500,
> calculează valoarea lor intrinsecă și îți arată care sunt **subevaluate** — cu un raport
> HTML complet, în mai puțin de 3 minute, 100% local, fără abonamente plătite.

---

## Cuprins

1. [Ce problemă rezolvă](#ce-problemă-rezolvă)
2. [Cum funcționează — pas cu pas](#cum-funcționează--pas-cu-pas)
3. [Cele 4 screen-uri de investiții](#cele-4-screen-uri-de-investiții)
4. [Top Convictions — semnalul cel mai puternic](#top-convictions--semnalul-cel-mai-puternic)
5. [Metricile calculate](#metricile-calculate)
6. [Modele de evaluare DCF](#modele-de-evaluare-dcf)
7. [Scoruri de calitate](#scoruri-de-calitate)
8. [Raportul HTML](#raportul-html)
9. [Backtesting vs S&P 500](#backtesting-vs-sp-500)
10. [Instalare și rulare rapidă](#instalare-și-rulare-rapidă)
11. [Toate comenzile CLI](#toate-comenzile-cli)
12. [Structura bazei de date](#structura-bazei-de-date)
13. [Structura proiectului](#structura-proiectului)
14. [Limitări cunoscute](#limitări-cunoscute)

---

## Ce problemă rezolvă

Găsirea acțiunilor subevaluate manual înseamnă să analizezi câte o companie pe rând —
bilanț, cont de profit, cashflow, comparație cu concurența. Pentru 503 companii ar dura
**luni întregi**. Acest sistem face totul automat în ~3 minute.

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

## Cum funcționează — pas cu pas

```
┌─────────────────────────────────────────────────────────────────┐
│  PASUL 1 — Universe                                             │
│  Descarcă lista live a celor 503 companii S&P 500               │
│  de pe Wikipedia (sau Dow 30, NASDAQ-100, CSV custom)           │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 2 — Fetch & Cache                                        │
│  Descarcă date financiare reale pentru fiecare companie         │
│  via Yahoo Finance (yfinance), 8 firme simultan.                │
│  Salvează totul în DuckDB local (cache.duckdb, ~53 MB).         │
│  La FIECARE rulare datele sunt re-descărcate fresh.             │
│  Durată: ~3 min prima dată, ~3 min și data viitoare             │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 3 — Valuation Engine                                     │
│  Pentru fiecare companie calculează:                            │
│  • 6 multipli de piață (P/E, P/B, EV/EBITDA, P/FCF, etc.)      │
│  • 2 modele DCF independente (GGM + Exit Multiple)              │
│  • Marjă de Siguranță % față de prețul curent                   │
│  • 3 scoruri de calitate (Piotroski, Altman Z, ROIC)            │
│  • Scor composite 0–100                                         │
│  • WACC dinamic per companie                                    │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 4 — Screen & Rank                                        │
│  Aplică 4 filtre independente pe rezultate.                     │
│  Fiecare filtru are o filosofie diferită de investiții.         │
│  503 companii → de obicei 2–15 trec fiecare filtru             │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│  PASUL 5 — Export                                               │
│  • CSV/Excel cu toate rezultatele                               │
│  • Raport HTML complet cu reasoning per companie                │
│  • "Why buy X?" generat automat cu cifrele reale                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Cele 4 screen-uri de investiții

Fiecare screen este **complet independent** — folosesc aceleași date calculate, dar aplică
criterii diferite, bazate pe filosofii diferite de investiții.

> **Analogie:** Vrei să cumperi o casă.
> - **Deep Value** = cel mai mic preț/mp din oraș, indiferent de stare
> - **Buffett Quality** = casă bine întreținută, cartier bun, la preț corect
> - **High FCF Yield** = casa care generează cea mai mare chirie față de preț
> - **Quality Value** = casă solidă structural, fără ipotecă mare, la discount

### 🔵 Deep Value — `--profile deep_value`
> *Inspirat din Benjamin Graham — "The Intelligent Investor" (1949)*

Cel mai strict screen. Compania trebuie să treacă **toate 7 criterii simultan**:

| Criteriu | Prag | Ce înseamnă |
|---|---|---|
| P/E | ≤ 15× | Prețul e sub 15 ani de profit |
| P/B | ≤ 1.5× | Cumperi activele sub valoarea contabilă |
| EV/EBITDA | ≤ 8× | Costul total de achiziție e mic față de profit operațional |
| P/FCF | ≤ 15× | Prețul e sub 15 ani de cashflow liber real |
| Net Debt/EBITDA | ≤ 2.5× | Datoriile pot fi plătite în 2.5 ani |
| Margin of Safety | ≥ 20% | Reducere minimă față de valoarea intrinsecă |
| Piotroski F-Score | ≥ 4/9 | Sănătate financiară de bază |

Din 503 companii S&P 500, trec de obicei **2–5**.

---

### 🟣 Buffett Quality — `--profile buffett_quality`
> *Inspirat din Warren Buffett — "calitate la un preț rezonabil"*

Nu contează că e ieftin absolut — contează că **câștigă mai mult decât costul capitalului**:

| Criteriu | Prag | Ce înseamnă |
|---|---|---|
| ROIC | ≥ 10% | Câștigă mai mult decât costă capitalul investit |
| Piotroski | ≥ 6/9 | Fundamentele contabile sunt solide |
| P/FCF | ≤ 20× | Cashflow la un preț rezonabil |
| EV/EBITDA | ≤ 12× | Nu prea scump față de profit operațional |
| Margin of Safety | ≥ 15% | Discount față de valoarea calculată |
| Altman Z | ≥ 1.0 | Nu e în zona de risc de faliment |

---

### 🟢 High FCF Yield — `--profile high_fcf_yield`
> *Focalizat pe cashflow liber — cel mai greu de manipulat indicator*

Simplu și direct — cashflow maxim la preț minim:

| Criteriu | Prag | Ce înseamnă |
|---|---|---|
| P/FCF | ≤ 12× | Plătești sub 12 ani de cashflow real pentru toată firma |
| Margin of Safety | ≥ 30% | Reducere semnificativă față de valoarea intrinsecă |

> **De ce P/FCF e important?** Spre deosebire de profit net (care poate fi "aranjat"
> contabil), cashflow liber = banii care chiar intră în contul firmei. Mult mai greu
> de manipulat.

---

### 🟡 Quality Value — `--profile quality_value`
> *Echilibru între calitate și valoare — cea mai completă verificare*

| Criteriu | Prag | Ce înseamnă |
|---|---|---|
| EV/EBITDA | ≤ 10× | Preț rezonabil față de profit operațional |
| P/E | ≤ 18× | Sub media S&P 500 (~22×) |
| ROIC | ≥ 8% | Eficiență minimă a capitalului |
| Piotroski | ≥ 6/9 | Fundamentele îmbunătățite |
| Altman Z | ≥ 1.0 | Nu e în dificultate financiară |
| Margin of Safety | ≥ 20% | Discount clar față de valoare |

---

### 🔵 Dow Jones 30 — `--universe dow30`
> *Ranking pur după poziție în range-ul anual — fără filtru MoS*

Toate 30 companiile Dow sunt **listate și sortate** după cât de aproape sunt de minimul
lor anual. Nu e un filtru de calitate — e un indicator tehnic pentru blue-chips:

```
52w Position% = (Preț curent − Minim 52 săpt.) / (Maxim − Minim) × 100
```

- **0%** = la minimul anual — cel mai mult upside potențial
- **100%** = la maximul anual — cel mai puțin upside

Dow 30 e special: companiile slabe sunt eliminate periodic din index, deci o
companie Dow la minimul anual este aproape sigur o oportunitate temporară, nu o problemă structurală.

---

## Top Convictions — semnalul cel mai puternic

Prima secțiune din raportul HTML. Arată companiile care au **trecut prin 2 sau mai
multe screen-uri simultan**.

**Logica:** fiecare screen folosește o filosofie complet diferită. Dacă 3 filosofii
independente ajung la aceeași concluzie despre aceeași companie, semnalul e mult
mai robust.

```
Niveluri de conviction:
  ⭐ GOLD     (4/4 profile) — cel mai puternic semnal posibil
  🟢 HIGH     (3/4 profile) — 3 filosofii diferite, același rezultat
  🔵 MODERATE (2/4 profile) — confirmat de 2 abordări independente
```

**Exemplu real (August 2026):**
- **APA Corporation** — trece Buffett Quality + High FCF Yield + Quality Value
  → nivel HIGH, MoS 64%, ROIC 14.1%, P/FCF 6.9×

---

## Metricile calculate

### Multipli de piață (relativi)

| Metric | Ce măsoară | S&P 500 avg | Bun dacă |
|---|---|---|---|
| **P/E** | Ani de profit ca să recuperezi prețul | ~22× | ≤ 15× |
| **P/B** | Prețul față de activele nete contabile | ~4× | ≤ 1.5× (cumperi sub valoarea activelor) |
| **EV/EBITDA** | Costul total de achiziție / profit operațional | ~14× | ≤ 8× |
| **P/FCF** | Prețul față de cashflow real generat | ~20× | ≤ 12× |
| **Net Debt/EBITDA** | Ani necesari să plătești datoriile din profit | — | ≤ 2.5× (leverage conservator) |

### Poziție 52 săptămâni

```
52w Position% = (Preț curent − Minim 52w) / (Maxim 52w − Minim 52w) × 100
```

- **Verde < 33%** — aproape de minimul anual → upside tehnic maxim
- **Galben 33–66%** — în mijlocul range-ului
- **Roșu > 66%** — aproape de maximul anual → prudență

---

## Modele de evaluare DCF

Motorul calculează **două modele DCF independente** pentru fiecare companie și le
face media pentru a obține valoarea intrinsecă finală.

### Model 1 — Gordon Growth Model (GGM)
> Conservator. Bazat pe cashflow liber real.

1. Extrage Free Cash Flow din ultimii 3–5 ani
2. Cere minim **3 ani de FCF pozitiv** (altfel: `INSUFFICIENT_DATA`)
3. Folosește media FCF ca estimare bază
4. Proiectează 10 ani la o rată de creștere de 5%
5. Adaugă valoarea terminală: `TV = FCF × (1+g) / (r−g)`
6. Actualizează la prezent cu WACC dinamic per companie
7. Împarte la numărul de acțiuni → valoare per acțiune

### Model 2 — Exit Multiple
> Relativ la piață. Bazat pe EBITDA.

1. Folosește EBITDA curent ca bază
2. Proiectează 10 ani la 5% creștere
3. Valoare terminală = `EBITDA_10 × 12×` (multiplul median S&P 500)
4. Scade datoriile nete → valoare equity
5. Actualizează la prezent → valoare per acțiune

### Parametri DCF (configurabili)

| Parametru | Default | Rațiune |
|---|---|---|
| `growth_rate` | 5% | Media creșterii economice pe termen lung (SUA) |
| `discount_rate` | 10% | Randamentul istoric mediu S&P 500 |
| `terminal_growth` | 2.5% | Inflație + creștere nominală pe termen lung |
| `projection_years` | 10 | Orizontul standard DCF |
| `exit_multiple` | 12× | Multiplul median EV/EBITDA S&P 500 |

### Modele speciale

- **DDM (Dividend Discount Model)** — fallback automat pentru bănci și asigurători
  (sectorul Financiar), unde FCF-based DCF nu e valid
- **WACC dinamic** — calculat per companie din beta real, rata dobânzii la datorii,
  structura capitalului (equity vs debt). Nu un WACC fix de 10% pentru toți.
- **Sustainable Growth Rate** — `g = ROE × Retention Ratio`, folosit ca rată de
  creștere în GGM dacă e pozitiv și mai mic decât WACC

---

## Scoruri de calitate

### Piotroski F-Score (0–9)
9 criterii contabile binare — fiecare e 0 sau 1:

| Grup | Criterii |
|---|---|
| **Profitabilitate** (4 pts) | ROA > 0, CFO > 0, ROA crescut YoY, Accruals < 0 |
| **Leverage** (3 pts) | Leverage scăzut YoY, Lichiditate crescută YoY, Fără diluare acțiuni |
| **Eficiență** (2 pts) | Marjă brută crescută YoY, Rotație active crescută YoY |

**Interpretare:** ≥ 7 = companie cu fundamentale în îmbunătățire activă.
4–6 = stabilă. < 4 = deteriorare.

### Altman Z-Score
Model statistic de predicție a falimentului (1968, recalibrat):

```
Z = 1.2×X1 + 1.4×X2 + 3.3×X3 + 0.6×X4 + 1.0×X5
```

- **Z < 1.0** → zonă de risc real (exclus din profilele quality_value și buffett_quality)
- **Z 1.0–2.99** → zonă gri (acceptabil, monitorizat)
- **Z ≥ 3.0** → companie sănătoasă financiar

> **Notă:** Pragul original al lui Altman era 1.81 pentru "distress". Am calibrat
> la 1.0 pentru că media/telecom (ex: CMCSA) are Z structural mai mic fără a fi
> în dificultate reală.

### ROIC — Return on Invested Capital
```
ROIC = NOPAT / (Equity + Debt − Cash)
```
- **≥ 15%** → avantaj competitiv clar (moat)
- **≥ 10%** → depășește costul tipic al capitalului
- **< 5%** → eficiență slabă, cazul investițional se bazează pe discount de preț

---

## Raportul HTML

```bash
# Generează raportul CONSOLIDAT cu toate profilurile + backtest
python scripts/export_full_report.py

# Raport individual (deep_value sau dow30)
python scripts/export_html_report.py
python scripts/export_html_report.py --csv data/reports/<timestamp>_deep_value.csv
```

### Ce conține `full_report.html`

Raportul este un singur fișier HTML de ~115–150 KB, complet self-contained
(fără dependențe externe), **pregătit pentru export PDF** (Ctrl+P → Save as PDF).

**Secțiuni:**

1. **★ Top Convictions** — companiile care au trecut prin 2+ profile, cu:
   - Nivel de conviction (GOLD/HIGH/MODERATE)
   - Badge-uri colorate per profil (DV / BQ / FCF / QV)
   - **"Why buy X?"** — paragraph generat automat cu cifrele reale:
     > *"Our model estimates that APA Corporation (APA) is currently trading at a
     > 64% discount to its calculated intrinsic value of $117.35 per share.
     > In plain terms: for every $1 of estimated value, the market is charging
     > only $0.36 — a rare margin of safety. At a P/E of 8.9× — versus the S&P 500
     > average of ~22× — the market is pricing APA as if earnings will decline
     > sharply..."*

2. **Deep Value Screen** — tabel cu toate metricile: prețul, valoarea intrinsecă,
   MoS%, 52w position, P/E, P/B, EV/EBITDA, P/FCF, Net Debt/EBITDA,
   Piotroski badge, ROIC badge, model DCF folosit, grad (A+/A/B+/B/C)

3. **Buffett Quality Screen** — același format, cu accentul pe ROIC și calitate

4. **High FCF Yield Screen** — 11 companii sortate după cashflow yield

5. **Quality Value Screen** — blend echilibrat

6. **Dow Jones 30 Ranking** — toate 30 companii, gauge vizual 52w position

7. **Backtest vs S&P 500** — grafic vizual dual per an (portfolio vs ^GSPC),
   tabel detaliat cu "Excess vs SPX" colorat verde/roșu, KPI-uri:
   Portfolio CAGR, S&P 500 CAGR, Sharpe, Sortino, Max Drawdown, Win Rate

8. **Methodology** — tabel cu toate cele 13 feature-uri v2, Faze 1–5

---

## Backtesting vs S&P 500

```bash
python src/main.py --universe sp500 --profile deep_value \
  --backtest --backtest-start 2021 --backtest-end 2024 \
  --workers 6 --export csv
```

Simulare walk-forward: la începutul fiecărui an, ia top-N companii din screen,
ține-le 12 luni, măsoară randamentul față de **^GSPC (S&P 500)**.

**Exemplu rezultate reale (Deep Value, 2021–2024, CMCSA + FISV):**

| An | Portfolio | S&P 500 | Excess vs SPX |
|---|---|---|---|
| 2021 | -3.5% | +27.7% | -31.2% |
| 2022 | -16.7% | -20.3% | **+3.6%** |
| 2023 | +28.9% | +24.0% | **+4.8%** |
| 2024 | +21.2% | +23.7% | -2.5% |
| **CAGR** | **+5.87%** | **+11.80%** | -5.93% |

**Limitări importante ale backtestului:**
- **Look-ahead bias** — folosește fundamentalele *curente* pentru toți anii istorici
- **Survivorship bias** — include doar companiile care sunt *acum* în S&P 500
- **Fără costuri de tranzacție** — nu include comisioane sau spread bid-ask
- Rezultatele sunt indicatori direcționali, nu predicții.

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
```

**Durată:** ~3 minute (descărcare fresh) → deschide browserul cu raportul.

---

## Toate comenzile CLI

```bash
# ── Screen-uri principale ─────────────────────────────────────────────────────

# S&P 500, Deep Value (cel mai strict)
python src/main.py --universe sp500 --profile deep_value --workers 6 --export csv

# S&P 500, Buffett Quality
python src/main.py --universe sp500 --profile buffett_quality --workers 6 --export csv

# S&P 500, High FCF Yield
python src/main.py --universe sp500 --profile high_fcf_yield --workers 6 --export csv

# S&P 500, Quality Value
python src/main.py --universe sp500 --profile quality_value --workers 6 --export csv

# Dow Jones 30 — ranking 52w position (fără filtru MoS)
python src/main.py --universe dow30 --workers 6 --export csv

# NASDAQ-100
python src/main.py --universe nasdaq100 --profile buffett_quality --workers 8

# Tickere custom dintr-un CSV
python src/main.py --universe custom --csv-path my_tickers.csv --profile deep_value

# ── Rapoarte HTML ─────────────────────────────────────────────────────────────

# Raport CONSOLIDAT (toate profilurile + backtest) — recomandat
python scripts/export_full_report.py

# Raport individual (auto-detectează cel mai recent CSV)
python scripts/export_html_report.py

# Raport individual din CSV specific
python scripts/export_html_report.py --csv data/reports/<timestamp>_deep_value.csv

# ── Backtesting ───────────────────────────────────────────────────────────────

python src/main.py --universe sp500 --profile deep_value \
  --backtest --backtest-start 2021 --backtest-end 2024 \
  --backtest-top-n 10 --workers 6 --export csv

# ── Dashboard interactiv ──────────────────────────────────────────────────────

streamlit run dashboard/app.py

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
  --universe      sp500 | nasdaq100 | dow30 | world | custom   (default: world)
  --csv-path      CSV cu coloana 'ticker'; necesar când --universe custom
  --workers       Fire de execuție paralele                     (default: 8)
  --rps           Requests per secundă Yahoo Finance            (default: 2.0)

Screener
  --profile       deep_value | buffett_quality | high_fcf_yield | quality_value

Export
  --export        csv | excel | both | none                     (default: csv)

Backtest
  --backtest                  Activează modul backtest
  --backtest-start YEAR       Primul an (default: 2018)
  --backtest-end   YEAR       Ultimul an (default: 2024)
  --backtest-top-n N          Câte companii în portofoliu       (default: 10)
  --backtest-benchmark TICKER Benchmark (default: ^GSPC)
```

---

## Structura bazei de date

Datele sunt stocate în `data/cache.duckdb` (~53 MB, gitignored, creat automat).

```
Tabelă              Rânduri     Tickers   Ce conține
─────────────────────────────────────────────────────────────────────
ticker_info            502        502     Snapshot curent per companie
                                          (preț, P/E, P/B, FCF, datorii,
                                          beta, ROE, ROA, dividende,
                                          52w low/high, sector, industrie)

financials           2,375        502     Cont de profit: 5 ani anuali
                                          (venituri, profit brut, EBIT,
                                          profit net per companie)

balance_sheet        2,456        502     Bilanț: 5 ani anuali
                                          (active totale, pasive, datorii,
                                          cash, equity acționari)

cashflow             2,467        502     Cashflow: 5 ani anuali
                                          (CFO, CapEx, FCF per companie)

price_history      678,754        541     Prețuri zilnice de închidere
                                          (2020–prezent, pentru backtest)

macro_data               1          —     us_10y_yield = 4.706%
                                          (rata risk-free pentru WACC/DCF)
```

### Politica de refresh

**La fiecare rulare, datele sunt re-descărcate fresh** — nu există cache persistent
pentru prețuri și fundamentale. Singura excepție: `price_history` se păstrează 1 zi
(prețurile istorice nu se schimbă niciodată).

```
ticker_info     → TTL: 0  (mereu fresh)
financials      → TTL: 0  (mereu fresh)
balance_sheet   → TTL: 0  (mereu fresh)
cashflow        → TTL: 0  (mereu fresh)
price_history   → TTL: 1 zi (closes istorice nu se schimbă)
macro_data      → TTL: 1 zi (randamentul US 10Y se actualizează zilnic)
```

---

## Structura proiectului

```
UndervaluedStocks/
│
├── src/
│   ├── universe.py       Asamblare universe: S&P 500, NASDAQ-100, Dow 30,
│   │                     world, custom CSV; scraping Wikipedia cu UA fix
│   │
│   ├── fetcher.py        Pipeline paralel de date: DuckDB cache thread-safe
│   │                     (Lock pe toate operațiile), throttle configurabil,
│   │                     retry exponential, auto-migration scheme, TTL=0
│   │
│   ├── engine.py         Motor de evaluare: multipli, GGM DCF, Exit Multiple,
│   │                     DDM fallback, Piotroski, Altman Z, ROIC, WACC dinamic,
│   │                     Sustainable Growth Rate, Composite Score 0–100
│   │
│   ├── screener.py       4 profile predefinite + Dow 30 ranking; gardă multipli
│   │                     negativi (P/B < 0 exclus); filtru Altman Z la 1.0
│   │
│   ├── backtester.py     Walk-forward backtest anual vs benchmark; CAGR,
│   │                     Sharpe, Sortino, MaxDD, Win Rate; rezolvare
│   │                     nearest-trading-day pentru weekend/holiday
│   │
│   └── main.py           CLI (argparse) + wizard interactiv; output ASCII-safe
│                         (Windows cp1252 compatible); toate profilele disponibile
│
├── scripts/
│   ├── export_full_report.py   Raport HTML consolidat: toate profilurile +
│   │                           backtest + Dow 30 + Why buy + Top Convictions;
│   │                           table-layout:fixed, fără scrollbars, PDF-ready
│   │
│   ├── export_html_report.py   Raport HTML individual (deep_value / dow30)
│   └── gen_global_tickers.py   Regenerează data/global_tickers.csv
│
├── dashboard/
│   ├── app.py            Streamlit: slidere DCF live, tabel sortabil,
│   │                     chart composite score, pie sector, matrix 3×3
│   └── run.py            Launcher convenience
│
├── tests/
│   ├── unit/             258 teste unitare (mocked, fără internet)
│   │   ├── test_universe.py
│   │   ├── test_fetcher.py     incl. TestFetchHistoricalPricesNearestDate
│   │   ├── test_engine.py      incl. Piotroski, Altman, ROIC, WACC, DDM
│   │   ├── test_screener.py    incl. negative P/B guard, Altman threshold
│   │   ├── test_backtester.py
│   │   └── test_dashboard_imports.py
│   │
│   └── integration/      Teste reale (necesită internet): --pytest -m integration
│
├── data/
│   ├── global_tickers.csv    ~552 tickere internaționale curate (în git)
│   ├── cache.duckdb          Cache DuckDB local (gitignored, creat automat)
│   └── reports/              CSV / Excel / HTML (gitignored)
│
├── config/
│   └── screener_profiles.yaml   Override YAML opțional pentru praguri
│
├── v2-improvements-plan.md   Plan complet v2 (Faze 1–5, toate implementate)
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## Limitări cunoscute

| Limitare | Detaliu |
|---|---|
| **Date actuale, nu istorice** | Screener-ul folosește fundamentalele *curente*. Nu știe cum arătau situațiile financiare ale companiei în 2021. |
| **Fără analiză calitativă** | Managementul, moat-ul, poziția competitivă, reglementările — invizibile pentru algoritm. |
| **DCF sensibil la ipoteze** | O schimbare de 2% în rata de discount mișcă valoarea intrinsecă cu 20–40%. Folosiți DCF ca orientare, nu ca certitudine. |
| **Sectorul financiar** | Băncile și asigurătorii au structuri de cashflow neobișnuite. Sistemul rutează automat spre DDM pentru aceste companii, dar MoS-ul e mai puțin precis. |
| **Backtesting look-ahead** | Backtestul folosește datele curente pentru toți anii istorici — rezultatele sunt optimiste vs realitate. |
| **Survivorship bias** | Universul conține doar companiile *actuale* din S&P 500 — companiile delistate din 2018 încoace lipsesc. |

---

## Rularea testelor

```bash
# Teste unitare — rapide, mocked, fără internet (258 teste)
pytest

# Teste de integrare — necesită internet (~60 sec)
pytest -m integration

# Toate testele
pytest -m ""
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
