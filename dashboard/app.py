"""
dashboard/app.py — Streamlit interactive dashboard for the Stock Screener &
Intrinsic Value Engine.

Run with:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── Streamlit and Plotly are imported at module level (lightweight). ──────────
# Heavy project imports (yfinance, duckdb) are deferred inside the run block.
import pandas as pd
import plotly.express as px
import streamlit as st

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Stock Screener & Intrinsic Value Engine",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
st.sidebar.title("Stock Screener")
st.sidebar.markdown("---")

# Universe
universe: str = st.sidebar.selectbox("Universe", ["sp500", "nasdaq100", "dow30", "world"])

# Profile (disabled for dow30)
if universe != "dow30":
    profile: str = st.sidebar.selectbox(
        "Profile",
        ["deep_value", "buffett_quality", "high_fcf_yield", "quality_value"],
    )
else:
    profile = "dow30_ranking"

st.sidebar.markdown("---")
st.sidebar.subheader("DCF Parameters")

dcf_growth: float = st.sidebar.slider(
    "Annual Growth Rate",
    0.01, 0.20, 0.05, 0.01,
    format="%.0f%%",
    help="Conservative estimate of FCF growth",
)
dcf_discount: float = st.sidebar.slider(
    "Discount Rate (WACC)",
    0.06, 0.20, 0.10, 0.01,
    format="%.0f%%",
    help="Required rate of return. Dynamic WACC overrides this per company.",
)
dcf_terminal: float = st.sidebar.slider(
    "Terminal Growth Rate",
    0.01, 0.05, 0.025, 0.005,
    format="%.1f%%",
)
dcf_exit_mult: float = st.sidebar.slider(
    "Exit Multiple (EV/EBITDA)",
    6.0, 20.0, 12.0, 0.5,
    format="%.1f×",
)
dcf_years: int = st.sidebar.slider("Projection Years", 5, 15, 10, 1)

st.sidebar.markdown("---")
st.sidebar.subheader("Fetch Settings")
workers: int = st.sidebar.slider("Parallel Threads", 2, 16, 6, 1)
rps: float = st.sidebar.slider("Max Requests/sec", 1.0, 10.0, 3.0, 0.5)

st.sidebar.markdown("---")
run_btn: bool = st.sidebar.button(
    "🚀 Run Screener", type="primary", use_container_width=True
)


# ---------------------------------------------------------------------------
# Helper: run the pipeline
# ---------------------------------------------------------------------------
def _run_pipeline(
    universe: str,
    profile: str,
    dcf_growth: float,
    dcf_discount: float,
    dcf_terminal: float,
    dcf_years: int,
    dcf_exit_mult: float,
    workers: int,
    rps: float,
) -> tuple[list, pd.DataFrame, int, int]:
    """Execute the full fetch → evaluate → screen pipeline and return results."""
    # Deferred heavy imports so the module itself stays light.
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from src.universe import UniverseSource, get_universe  # noqa: PLC0415
    from src.fetcher import CacheStore, fetch_universe, fetch_risk_free_rate  # noqa: PLC0415
    from src.engine import DCFParams, evaluate  # noqa: PLC0415
    from src.screener import load_profiles, apply_profile, apply_dow30_ranking  # noqa: PLC0415

    DATA_DIR = Path(__file__).parent.parent / "data"
    CACHE_PATH = DATA_DIR / "cache.duckdb"
    CONFIG_DIR = Path(__file__).parent.parent / "config"

    with st.spinner(f"Fetching {universe.upper()} universe…"):
        source = UniverseSource(universe)
        tickers = get_universe(source)

    cache = CacheStore(str(CACHE_PATH))
    rf_rate = fetch_risk_free_rate(cache)

    progress_bar = st.progress(0, text="Fetching financial data…")

    ticker_data_list, _failed = fetch_universe(
        tickers, cache=cache, max_workers=workers, requests_per_second=rps
    )
    progress_bar.progress(50, text="Evaluating companies…")

    dcf_params = DCFParams(
        growth_rate=dcf_growth,
        discount_rate=dcf_discount,
        terminal_growth=dcf_terminal,
        projection_years=dcf_years,
        exit_multiple=dcf_exit_mult,
    )

    valuation_results = []
    for td in ticker_data_list:
        try:
            valuation_results.append(evaluate(td, dcf_params, rf_rate=rf_rate))
        except Exception:
            pass

    progress_bar.progress(80, text="Applying screener profile…")

    is_dow30 = universe == "dow30"
    if is_dow30:
        df = apply_dow30_ranking(valuation_results)
    else:
        profiles = load_profiles(str(CONFIG_DIR / "screener_profiles.yaml"))
        prof_obj = profiles.get(profile, next(iter(profiles.values())))
        df = apply_profile(valuation_results, prof_obj)

    progress_bar.progress(100, text="Done!")

    ok_count = sum(1 for r in valuation_results if r.status == "OK")
    trap_count = sum(1 for r in valuation_results if r.status == "VALUE_TRAP")

    return tickers, df, ok_count, trap_count


# ---------------------------------------------------------------------------
# Trigger pipeline run and persist in session_state
# ---------------------------------------------------------------------------
if run_btn:
    tickers, df, ok_count, trap_count = _run_pipeline(
        universe, profile, dcf_growth, dcf_discount, dcf_terminal,
        dcf_years, dcf_exit_mult, workers, rps,
    )
    st.session_state["results"]    = df
    st.session_state["tickers"]    = tickers
    st.session_state["ok_count"]   = ok_count
    st.session_state["trap_count"] = trap_count
    # Persist the DCF params used so the sensitivity matrix stays consistent.
    st.session_state["dcf_discount"] = dcf_discount
    st.session_state["dcf_terminal"] = dcf_terminal
    st.rerun()


# ---------------------------------------------------------------------------
# Main panel — landing / instructions
# ---------------------------------------------------------------------------
if "results" not in st.session_state:
    st.title("Stock Screener & Intrinsic Value Engine")
    st.info("Configure parameters in the sidebar and click **Run Screener** to start.")

    with st.expander("How it works"):
        st.markdown(
            """
            1. **Universe Assembly** — Downloads the live constituent list from Wikipedia
            2. **Data Fetch & Cache** — Pulls financials via yfinance; stores locally
               (re-runs take ~8s)
            3. **Valuation Engine** — Computes P/E, P/B, EV/EBITDA, P/FCF, two DCF
               models (GGM + Exit Multiple), dynamic WACC, Piotroski F-Score,
               Altman Z-Score, ROIC
            4. **Screener Filter** — Applies the selected profile thresholds and ranks
               by Composite Score or Margin of Safety
            """
        )

    st.stop()


# ---------------------------------------------------------------------------
# Main panel — results
# ---------------------------------------------------------------------------
df: pd.DataFrame        = st.session_state["results"]
tickers: list           = st.session_state["tickers"]
ok_count: int           = st.session_state["ok_count"]
trap_count: int         = st.session_state["trap_count"]
# Use stored DCF params from the run that produced these results.
_stored_discount: float = st.session_state.get("dcf_discount", dcf_discount)
_stored_terminal: float = st.session_state.get("dcf_terminal", dcf_terminal)

st.title("Screener Results")

# ── Top metric cards ──────────────────────────────────────────────────────────
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Tickers Screened", len(tickers))
col2.metric("Evaluated", ok_count + trap_count)
col3.metric("Passed Filter", len(df))
col4.metric("Value Traps", trap_count)
col5.metric(
    "Best MoS",
    f"{df['MoS%'].max():.1f}%"
    if not df.empty and "MoS%" in df.columns and df["MoS%"].notna().any()
    else "—",
)

# ── Results table ─────────────────────────────────────────────────────────────
st.subheader("Screener Results")

if df.empty:
    st.warning("No companies passed the selected profile filters. Try relaxing the DCF parameters or switching profile.")
else:
    column_config: dict = {
        "MoS%":          st.column_config.NumberColumn("MoS%", format="%.1f%%"),
        "52w Position%": st.column_config.ProgressColumn(
            "52w Pos%", format="%.0f%%", min_value=0, max_value=100
        ),
        "Score":         st.column_config.NumberColumn("Score", format="%.1f"),
        "Piotroski":     st.column_config.NumberColumn("F-Score"),
        "ROIC%":         st.column_config.NumberColumn("ROIC%", format="%.1f%%"),
        "Price":         st.column_config.NumberColumn("Price", format="$%.2f"),
        "DCF Avg":       st.column_config.NumberColumn("DCF Avg", format="$%.2f"),
    }

    st.dataframe(
        df,
        use_container_width=True,
        column_config=column_config,
        height=400,
    )

    # ── Charts ────────────────────────────────────────────────────────────────
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Top Companies by Composite Score")
        if "Score" in df.columns and df["Score"].notna().any():
            top10 = df.dropna(subset=["Score"]).head(10)
            fig = px.bar(
                top10,
                x="Ticker",
                y="Score",
                color="Score",
                color_continuous_scale="RdYlGn",
                title="Composite Score (0–100)",
            )
            st.plotly_chart(fig, use_container_width=True)
        elif "52w Position%" in df.columns:
            # Dow30 mode — no Score column; use 52w Position% instead
            top10 = df.head(10)
            fig = px.bar(
                top10,
                x="Ticker",
                y="52w Position%",
                color="52w Position%",
                color_continuous_scale="RdYlGn_r",
                title="52-Week Position % (lower = cheaper)",
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No score data available for chart.")

    with col_right:
        st.subheader("Sector Distribution")
        if "Sector" in df.columns and df["Sector"].notna().any():
            sector_counts = (
                df["Sector"]
                .replace("", pd.NA)
                .dropna()
                .value_counts()
                .reset_index()
            )
            sector_counts.columns = ["Sector", "Count"]
            fig2 = px.pie(
                sector_counts,
                names="Sector",
                values="Count",
                title="Sector Distribution",
            )
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No sector data available.")

    # ── DCF Sensitivity Matrix ─────────────────────────────────────────────────
    st.subheader("DCF Sensitivity Analysis")
    st.caption(
        "Intrinsic value at different WACC × terminal growth rate combinations. "
        "Values are GGM-scaled approximations based on the run's output."
    )

    top5 = df.head(5) if "DCF GGM" in df.columns or "DCF Avg" in df.columns else pd.DataFrame()

    if top5.empty:
        st.info("DCF sensitivity data not available for the current profile (Dow30 ranking mode).")
    else:
        waccs = [_stored_discount - 0.02, _stored_discount, _stored_discount + 0.02]
        tgs   = [0.015, 0.025, 0.035]

        for _, row in top5.iterrows():
            label = f"{row['Ticker']} — {row.get('Company', '')}"
            with st.expander(label, expanded=False):
                base_dcf = row.get("DCF GGM") or row.get("DCF Avg")
                base_spread = max(_stored_discount - _stored_terminal, 0.01)

                matrix_rows: dict[str, dict[str, str]] = {}
                for tg in tgs:
                    row_data: dict[str, str] = {}
                    for w in waccs:
                        if (
                            base_dcf is not None
                            and not pd.isna(base_dcf)
                            and base_dcf > 0
                        ):
                            scale = (base_spread) / max(w - tg, 0.01)
                            scaled = base_dcf * scale
                            row_data[f"WACC {w:.0%}"] = f"${scaled:,.0f}"
                        else:
                            row_data[f"WACC {w:.0%}"] = "—"
                    matrix_rows[f"TG {tg:.1%}"] = row_data

                matrix_df = pd.DataFrame(matrix_rows).T
                st.dataframe(matrix_df, use_container_width=True)

                current_price = row.get("Price")
                if current_price and not pd.isna(current_price):
                    st.caption(f"Current price: ${current_price:.2f}")
