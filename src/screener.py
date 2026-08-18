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
    "apply_dow30_ranking",
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
    ),
    "buffett_quality": ScreenerProfile(
        name="buffett_quality",
        max_pe=25.0,
        max_pb=4.0,
        max_ev_ebitda=15.0,
        max_p_fcf=25.0,
        max_net_debt_ebitda=1.5,
        min_margin_of_safety_pct=15.0,
    ),
    "high_fcf_yield": ScreenerProfile(
        name="high_fcf_yield",
        max_pe=30.0,
        max_pb=5.0,
        max_ev_ebitda=20.0,
        max_p_fcf=12.0,
        max_net_debt_ebitda=3.0,
        min_margin_of_safety_pct=10.0,
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


# ── Filter & rank ─────────────────────────────────────────────────────────────

# Columns included in the standard output DataFrame
_OUTPUT_COLUMNS = [
    "Ticker", "Company", "Sector", "Industry", "Price",
    "52w Low", "52w High", "52w Position%",
    "MoS%", "P/E", "P/B", "EV/EBITDA", "P/FCF", "NetDebt/EBITDA",
    "DCF GGM", "DCF Exit", "DCF Avg",
]

# Columns for the Dow 30 ranking report (no MoS filter, ranked by 52w position)
DOW30_OUTPUT_COLUMNS = [
    "Rank", "Ticker", "Company", "Sector",
    "Price", "52w Low", "52w High", "52w Position%",
    "Market Cap ($B)", "P/E", "P/B", "MoS%",
]


def _passes_filter(result: ValuationResult, profile: ScreenerProfile) -> bool:
    """Return True if result meets all non-None thresholds in the profile."""

    def _check(value: Optional[float], max_val: Optional[float]) -> bool:
        """Pass if threshold is None OR value is None OR value <= threshold."""
        if max_val is None:
            return True
        if value is None:
            return True  # missing data does not disqualify
        return value <= max_val

    if not _check(result.pe_ratio, profile.max_pe):
        return False
    if not _check(result.pb_ratio, profile.max_pb):
        return False
    if not _check(result.ev_ebitda, profile.max_ev_ebitda):
        return False
    if not _check(result.p_fcf, profile.max_p_fcf):
        return False
    if not _check(result.net_debt_ebitda, profile.max_net_debt_ebitda):
        return False

    # Minimum MoS — requires an actual value
    if profile.min_margin_of_safety_pct is not None:
        if result.margin_of_safety_pct is None:
            return False
        if result.margin_of_safety_pct < profile.min_margin_of_safety_pct:
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
            "DCF Avg":          r.dcf_intrinsic_value,
        })

    if not rows:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    df = pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)

    # Sort: default descending by MoS%, but respect profile.sort_by
    sort_col = profile.sort_by if profile.sort_by in df.columns else "MoS%"
    df = df.sort_values(sort_col, ascending=False, na_position="last").reset_index(drop=True)

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
