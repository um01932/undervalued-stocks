"""
screener.py — Filtering & ranking presets.

Applies named ScreenerProfile filters to a list of ValuationResult objects
and returns a ranked DataFrame ready for display and export.

Built-in presets:
  deep_value       — classic Graham-style value (tight multiples, high MoS)
  buffett_quality  — quality at reasonable price (looser multiples, clean balance sheet)
  high_fcf_yield   — free-cash-flow focused screener

Profiles can be overridden at runtime via config/screener_profiles.yaml.
Only specified fields are merged; unspecified fields keep built-in defaults.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
from pydantic import BaseModel

from src.engine import ValuationResult

__all__ = [
    "ScreenerProfile",
    "BUILTIN_PROFILES",
    "load_profiles",
    "apply_profile",
    "rank_all",
    "apply_dow30_ranking",
    "compute_composite_score",
    "DOW30_OUTPUT_COLUMNS",
]

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_PROFILES_YAML = _CONFIG_DIR / "screener_profiles.yaml"


# ── Profile model ─────────────────────────────────────────────────────────────


class ScreenerProfile(BaseModel):
    """Filter thresholds and sorting preferences for a screener preset."""

    name: str
    max_pe: Optional[float] = None
    max_pb: Optional[float] = None
    max_ev_ebitda: Optional[float] = None
    max_p_fcf: Optional[float] = None
    max_net_debt_ebitda: Optional[float] = None
    min_margin_of_safety_pct: Optional[float] = None
    sort_by: str = "margin_of_safety_pct"
    include_value_traps: bool = False

    # Phase 2 — quality filters
    min_piotroski: Optional[int] = None            # None = no filter
    min_roic: Optional[float] = None               # % threshold (e.g. 8.0 for 8%)
    exclude_altman_distress: bool = False           # exclude Z < 1.0 when True (real distress zone; 1.81 is Grey Zone)

    # Sub-Task 1 — extended quality filters
    min_roe: Optional[float] = None                # % threshold e.g. 15.0
    min_gross_margin: Optional[float] = None       # % threshold e.g. 30.0

    # Sub-Task 4 — Beneish M-Score
    exclude_beneish_risk: bool = False             # exclude M > -1.78 (potential earnings manipulator)


# ── Built-in presets ──────────────────────────────────────────────────────────

BUILTIN_PROFILES: dict[str, ScreenerProfile] = {
    "deep_value": ScreenerProfile(
        name="deep_value",
        max_pe=15.0,
        max_pb=1.5,
        max_ev_ebitda=8.0,
        max_p_fcf=15.0,
        max_net_debt_ebitda=2.5,
        min_margin_of_safety_pct=20.0,
        min_piotroski=4,          # conservative: F6/F7 missing → max 7 points
        exclude_altman_distress=True,
        # No ROIC filter — combined with 5 tight multiples + Piotroski already demanding enough
    ),
    "buffett_quality": ScreenerProfile(
        name="buffett_quality",
        max_pe=25.0,
        max_pb=4.0,
        max_ev_ebitda=15.0,
        max_p_fcf=25.0,
        max_net_debt_ebitda=1.5,
        min_margin_of_safety_pct=15.0,
        min_piotroski=5,
        min_roic=10.0,
        min_roe=15.0,
        exclude_beneish_risk=True,
    ),
    "high_fcf_yield": ScreenerProfile(
        name="high_fcf_yield",
        max_pe=30.0,
        max_pb=5.0,
        max_ev_ebitda=20.0,
        max_p_fcf=12.0,
        max_net_debt_ebitda=3.0,
        min_margin_of_safety_pct=10.0,
        # no Piotroski / ROIC filter — keep the screen broad
    ),
    "quality_value": ScreenerProfile(
        name="quality_value",
        max_pe=25.0,
        max_pb=4.0,
        max_ev_ebitda=15.0,
        max_p_fcf=20.0,
        max_net_debt_ebitda=2.5,
        min_margin_of_safety_pct=15.0,
        min_piotroski=5,
        min_roic=10.0,
        exclude_altman_distress=True,
        exclude_beneish_risk=True,
        sort_by="Score",
    ),
}


# ── Profile loader ────────────────────────────────────────────────────────────


def load_profiles(yaml_path: Optional[str] = None) -> dict[str, ScreenerProfile]:
    """
    Load screener profiles, merging optional YAML overrides into built-in defaults.

    Args:
        yaml_path: Path to a YAML override file. Defaults to
                   config/screener_profiles.yaml if None.

    Returns:
        Dict mapping profile name → ScreenerProfile.
        YAML values override built-in fields; unspecified fields keep defaults.
    """
    path = Path(yaml_path) if yaml_path else _PROFILES_YAML

    # Start from deep copies of built-in profiles
    profiles: dict[str, ScreenerProfile] = {
        k: v.model_copy() for k, v in BUILTIN_PROFILES.items()
    }

    if not path.exists():
        logger.debug("No YAML override file found at %s; using built-in profiles.", path)
        return profiles

    try:
        with path.open(encoding="utf-8") as f:
            overrides = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Could not parse %s: %s. Using built-in profiles.", path, exc)
        return profiles

    if not isinstance(overrides, dict):
        logger.warning("YAML override file %s must be a mapping. Ignoring.", path)
        return profiles

    for profile_name, fields in overrides.items():
        if not isinstance(fields, dict):
            continue
        if profile_name in profiles:
            # Merge: copy existing, update with overrides
            current = profiles[profile_name].model_dump()
            current.update(fields)
            current["name"] = profile_name  # ensure name is preserved
            profiles[profile_name] = ScreenerProfile(**current)
            logger.debug("Merged YAML overrides for profile %r.", profile_name)
        else:
            # New custom profile
            fields.setdefault("name", profile_name)
            try:
                profiles[profile_name] = ScreenerProfile(**fields)
                logger.debug("Loaded new custom profile %r from YAML.", profile_name)
            except Exception as exc:
                logger.warning("Could not load custom profile %r: %s", profile_name, exc)

    return profiles


# ── Composite score ───────────────────────────────────────────────────────────


def compute_composite_score(result: "ValuationResult") -> Optional[float]:
    """
    Compute a 0–100 composite rank score from four weighted pillars.

    Pillar weights:
      Valuation     (30 pts) — Margin of Safety %
      Quality/Moat  (25 pts) — ROIC (20% = full marks)
      Financial     (25 pts) — Piotroski score (7 = full marks, F6/F7 skipped)
      Momentum      (20 pts) — 52-week position (lower = more upside)

    Neutral (12.5 / 10) awarded when a pillar's data is unavailable so that
    missing-data companies are not unfairly penalised or rewarded.
    """
    # Valuation pillar (30 pts): MoS%
    mos = result.margin_of_safety_pct
    val_score = min(max(mos or 0, 0), 100) * 0.30

    # Quality / Moat pillar (25 pts): ROIC
    if result.roic is not None:
        quality_score = min(result.roic * 100 / 20, 1.0) * 25   # 20% ROIC → full marks
    else:
        quality_score = 12.5  # neutral

    # Financial Health pillar (25 pts): Piotroski
    if result.piotroski_score is not None:
        health_score = (result.piotroski_score / 7) * 25  # 7 = max achievable without F6/F7
    else:
        health_score = 12.5  # neutral

    # Price Momentum / Mean Reversion pillar (20 pts): 52w position (lower = better)
    pos = result.price_vs_52w_low_pct
    if pos is not None:
        momentum_score = (1 - pos / 100) * 20
    else:
        momentum_score = 10  # neutral

    total = val_score + quality_score + health_score + momentum_score
    return round(total, 1)


# ── Filter & rank ─────────────────────────────────────────────────────────────

# Columns included in the standard output DataFrame
_OUTPUT_COLUMNS = [
    "Ticker", "Company", "Sector", "Industry", "Price",
    "52w Low", "52w High", "52w Position%",
    "MoS%", "P/E", "P/B", "EV/EBITDA", "P/FCF", "NetDebt/EBITDA",
    "DCF GGM", "DCF Exit", "Graham", "DCF Avg", "DCF Model",
    "Piotroski", "ROIC%", "ROE%", "ROA%", "Beta", "Gross Margin%", "Score",
    "Beneish M", "Manip.Risk",
]

# Columns for the Dow 30 ranking report (no MoS filter, ranked by 52w position)
DOW30_OUTPUT_COLUMNS = [
    "Rank", "Ticker", "Company", "Sector",
    "Price", "52w Low", "52w High", "52w Position%",
    "Market Cap ($B)", "P/E", "P/B", "MoS%",
]


def _passes_filter(result: ValuationResult, profile: ScreenerProfile) -> bool:
    """Return True if result meets all non-None thresholds in the profile."""

    def _check(
        value: Optional[float],
        max_val: Optional[float],
        allow_negative: bool = False,
    ) -> bool:
        """Pass if threshold is None OR value is None OR value <= threshold.

        When allow_negative is False (default), negative values are rejected
        regardless of the threshold — a negative P/B or P/E is not a bargain,
        it signals negative equity / earnings and should be excluded.
        """
        if max_val is None:
            return True
        if value is None:
            return True  # missing data does not disqualify
        if not allow_negative and value < 0:
            return False  # reject negative multiples (e.g. HPQ P/B = -190)
        return value <= max_val

    if not _check(result.pe_ratio, profile.max_pe):
        return False
    if not _check(result.pb_ratio, profile.max_pb):
        return False
    if not _check(result.ev_ebitda, profile.max_ev_ebitda):
        return False
    if not _check(result.p_fcf, profile.max_p_fcf):
        return False
    # net_debt_ebitda: allow negative (net cash position is a positive signal)
    if not _check(result.net_debt_ebitda, profile.max_net_debt_ebitda, allow_negative=True):
        return False

    # Minimum MoS — requires an actual value
    if profile.min_margin_of_safety_pct is not None:
        if result.margin_of_safety_pct is None:
            return False
        if result.margin_of_safety_pct < profile.min_margin_of_safety_pct:
            return False

    # Piotroski — None means data unavailable → pass (don't penalise missing data)
    if profile.min_piotroski is not None and result.piotroski_score is not None:
        if result.piotroski_score < profile.min_piotroski:
            return False

    # Altman — only exclude Distress Zone when configured
    if profile.exclude_altman_distress and result.altman_z is not None:
        if result.altman_z < 1.0:   # < 1.0 = real financial distress; 1.0-1.81 = grey zone (acceptable)
            return False

    # ROIC — None passes (missing data); threshold stored in %, result stored as decimal
    if profile.min_roic is not None and result.roic is not None:
        if result.roic * 100 < profile.min_roic:
            return False

    # ROE — None passes (missing data); threshold stored in %, result stored as decimal
    if profile.min_roe is not None and result.roe is not None:
        if result.roe * 100 < profile.min_roe:
            return False

    # Gross Margin — None passes (missing data); threshold stored in %, result stored as decimal
    if profile.min_gross_margin is not None and result.gross_margin is not None:
        if result.gross_margin * 100 < profile.min_gross_margin:
            return False

    # Beneish — exclude potential earnings manipulators when configured
    if profile.exclude_beneish_risk and result.beneish_flag:
        return False

    return True


def apply_profile(
    results: list[ValuationResult],
    profile: ScreenerProfile,
) -> pd.DataFrame:
    """
    Filter and rank ValuationResult objects by the given ScreenerProfile.

    Companies with status INSUFFICIENT_DATA are always excluded.
    Companies with status VALUE_TRAP are excluded unless profile.include_value_traps is True.

    Args:
        results: List of ValuationResult objects from engine.evaluate().
        profile: Screener profile defining filter thresholds.

    Returns:
        Sorted DataFrame with columns defined in _OUTPUT_COLUMNS.
        Empty DataFrame if no companies pass the filters.
    """
    rows: list[dict] = []
    for r in results:
        # Status exclusions
        if r.status == "INSUFFICIENT_DATA":
            continue
        if r.status == "VALUE_TRAP" and not profile.include_value_traps:
            continue

        if not _passes_filter(r, profile):
            continue

        rows.append({
            "Ticker":           r.ticker,
            "Company":          r.company_name or "",
            "Sector":           r.sector or "",
            "Industry":         r.industry or "",
            "Price":            r.current_price,
            "52w Low":          r.week52_low,
            "52w High":         r.week52_high,
            "52w Position%":    r.price_vs_52w_low_pct,
            "MoS%":             r.margin_of_safety_pct,
            "P/E":              r.pe_ratio,
            "P/B":              r.pb_ratio,
            "EV/EBITDA":        r.ev_ebitda,
            "P/FCF":            r.p_fcf,
            "NetDebt/EBITDA":   r.net_debt_ebitda,
            "DCF GGM":          r.dcf_ggm_intrinsic,
            "DCF Exit":         r.dcf_exit_intrinsic,
            "Graham":           r.graham_number,
            "DCF Avg":          r.dcf_intrinsic_value,
            "DCF Model":        r.dcf_model_used or "—",
            "Piotroski":        r.piotroski_score,
            "ROIC%":            (r.roic * 100) if r.roic is not None else None,
            "ROE%":             (r.roe * 100) if r.roe is not None else None,
            "ROA%":             (r.roa * 100) if r.roa is not None else None,
            "Beta":             r.beta,
            "Gross Margin%":    (r.gross_margin * 100) if r.gross_margin is not None else None,
            "Score":            r.composite_score,
            "Beneish M":        r.beneish_m,
            "Manip.Risk":       "YES" if r.beneish_flag else "NO",
        })

    if not rows:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    df = pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)

    # Sort: default descending by MoS%, but respect profile.sort_by
    sort_col = profile.sort_by if profile.sort_by in df.columns else "MoS%"
    df = df.sort_values(sort_col, ascending=False, na_position="last").reset_index(drop=True)

    return df


def rank_all(
    results: list[ValuationResult],
    profile: ScreenerProfile,
) -> pd.DataFrame:
    """
    Rank ALL companies by how well they fit a screener profile — no company excluded.

    Instead of a hard pass/fail filter, each company receives a
    **profile fit score** (0–100) measuring how close it is to satisfying
    every criterion in the profile.  Companies that PASS all criteria score
    their composite_score directly; companies that miss one or more criteria
    are penalised proportionally.

    Columns added beyond _OUTPUT_COLUMNS:
      - ``Passes``     : bool — True if the company would pass apply_profile()
      - ``ProfileFit`` : float 0–100 — proximity score for this profile
      - ``Status``     : OK / VALUE_TRAP / INSUFFICIENT_DATA

    INSUFFICIENT_DATA companies are included at the bottom (fit = 0).

    Args:
        results:  All ValuationResult objects from engine.evaluate().
        profile:  The screener profile to score against.

    Returns:
        DataFrame sorted descending by ProfileFit, all companies present.
    """
    rows: list[dict] = []

    for r in results:
        passes = _passes_filter(r, profile) and r.status != "INSUFFICIENT_DATA" and (
            r.status != "VALUE_TRAP" or profile.include_value_traps
        )

        # ── Compute a proximity score (0–100) for each criterion ─────────────
        # Each criterion contributes equally; partial credit for being close.
        criteria_scores: list[float] = []

        def _criterion_score(
            value: Optional[float],
            threshold: Optional[float],
            higher_is_better: bool = False,
            allow_negative: bool = False,
        ) -> float:
            """0.0 = very far from passing, 1.0 = at or better than threshold."""
            if threshold is None:
                return 1.0  # no criterion → full marks
            if value is None:
                return 0.7  # missing data: neutral-ish (don't punish too hard)
            if not allow_negative and value < 0:
                return 0.0  # negative multiple = bad
            if higher_is_better:
                # e.g. MoS ≥ min_threshold
                if value >= threshold:
                    return 1.0
                # partial credit: how far are we as % of threshold?
                return max(0.0, min(value / threshold, 1.0))
            else:
                # e.g. P/E ≤ max_threshold
                if value <= threshold:
                    return 1.0
                # partial credit: ratio threshold/value (closer = higher score)
                return max(0.0, min(threshold / value, 1.0))

        # Multiples criteria
        criteria_scores.append(_criterion_score(r.pe_ratio, profile.max_pe))
        criteria_scores.append(_criterion_score(r.pb_ratio, profile.max_pb))
        criteria_scores.append(_criterion_score(r.ev_ebitda, profile.max_ev_ebitda))
        criteria_scores.append(_criterion_score(r.p_fcf, profile.max_p_fcf))
        criteria_scores.append(_criterion_score(
            r.net_debt_ebitda, profile.max_net_debt_ebitda, allow_negative=True
        ))
        # MoS — higher is better
        criteria_scores.append(_criterion_score(
            r.margin_of_safety_pct, profile.min_margin_of_safety_pct,
            higher_is_better=True
        ))
        # Piotroski — higher is better
        if profile.min_piotroski is not None:
            pio_val = float(r.piotroski_score) if r.piotroski_score is not None else None
            criteria_scores.append(_criterion_score(
                pio_val, float(profile.min_piotroski), higher_is_better=True
            ))
        # ROIC — higher is better (convert to %)
        if profile.min_roic is not None:
            roic_pct = (r.roic * 100) if r.roic is not None else None
            criteria_scores.append(_criterion_score(
                roic_pct, profile.min_roic, higher_is_better=True
            ))
        # Altman distress — penalise Z < 1.0 when flag is set
        if profile.exclude_altman_distress and r.altman_z is not None:
            az_score = 0.0 if r.altman_z < 1.0 else 1.0
            criteria_scores.append(az_score)

        if r.status == "INSUFFICIENT_DATA":
            profile_fit = 0.0
        else:
            # Weighted: average of criteria scores × composite_score blend
            criteria_avg = (sum(criteria_scores) / len(criteria_scores)) if criteria_scores else 0.5
            comp = (r.composite_score or 0) / 100.0
            # 70% criteria proximity + 30% composite quality score
            profile_fit = round((criteria_avg * 0.70 + comp * 0.30) * 100, 1)

        rows.append({
            "Ticker":           r.ticker,
            "Company":          r.company_name or "",
            "Sector":           r.sector or "",
            "Industry":         r.industry or "",
            "Price":            r.current_price,
            "52w Low":          r.week52_low,
            "52w High":         r.week52_high,
            "52w Position%":    r.price_vs_52w_low_pct,
            "MoS%":             r.margin_of_safety_pct,
            "P/E":              r.pe_ratio,
            "P/B":              r.pb_ratio,
            "EV/EBITDA":        r.ev_ebitda,
            "P/FCF":            r.p_fcf,
            "NetDebt/EBITDA":   r.net_debt_ebitda,
            "DCF GGM":          r.dcf_ggm_intrinsic,
            "DCF Exit":         r.dcf_exit_intrinsic,
            "Graham":           r.graham_number,
            "DCF Avg":          r.dcf_intrinsic_value,
            "DCF Model":        r.dcf_model_used or "—",
            "Piotroski":        r.piotroski_score,
            "ROIC%":            (r.roic * 100) if r.roic is not None else None,
            "ROE%":             (r.roe * 100) if r.roe is not None else None,
            "ROA%":             (r.roa * 100) if r.roa is not None else None,
            "Beta":             r.beta,
            "Gross Margin%":    (r.gross_margin * 100) if r.gross_margin is not None else None,
            "Score":            r.composite_score,
            "Beneish M":        r.beneish_m,
            "Manip.Risk":       "YES" if r.beneish_flag else "NO",
            "Passes":           passes,
            "ProfileFit":       profile_fit,
            "Status":           r.status,
        })

    if not rows:
        cols = _OUTPUT_COLUMNS + ["Passes", "ProfileFit", "Status"]
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows)
    df = df.sort_values("ProfileFit", ascending=False, na_position="last").reset_index(drop=True)
    return df


def apply_dow30_ranking(results: list[ValuationResult]) -> pd.DataFrame:
    """
    Produce a pure ranking of Dow Jones 30 companies — no MoS filter applied.

    All companies (OK + VALUE_TRAP) are included; INSUFFICIENT_DATA excluded.
    Sorted ascending by '52w Position%' so the company trading closest to its
    52-week low appears first (= most potential upside, highest safety margin
    against further downside per Ibm Mihai's strategy).

    A secondary sort by Market Cap (desc) breaks ties.

    Args:
        results: ValuationResult list from engine.evaluate().

    Returns:
        DataFrame with DOW30_OUTPUT_COLUMNS, one row per company.
    """
    rows: list[dict] = []
    for r in results:
        if r.status == "INSUFFICIENT_DATA":
            continue
        market_cap_b = (r.market_cap / 1e9) if r.market_cap else None
        rows.append({
            "Ticker":           r.ticker,
            "Company":          r.company_name or "",
            "Sector":           r.sector or "",
            "Price":            r.current_price,
            "52w Low":          r.week52_low,
            "52w High":         r.week52_high,
            "52w Position%":    r.price_vs_52w_low_pct,
            "Market Cap ($B)":  market_cap_b,
            "P/E":              r.pe_ratio,
            "P/B":              r.pb_ratio,
            "MoS%":             r.margin_of_safety_pct,
        })

    if not rows:
        return pd.DataFrame(columns=DOW30_OUTPUT_COLUMNS)

    df = pd.DataFrame(rows)
    # Primary sort: lowest 52w position first (closest to 52-week low)
    df = df.sort_values(
        ["52w Position%", "Market Cap ($B)"],
        ascending=[True, False],
        na_position="last",
    ).reset_index(drop=True)

    df.insert(0, "Rank", range(1, len(df) + 1))
    return df[DOW30_OUTPUT_COLUMNS]
