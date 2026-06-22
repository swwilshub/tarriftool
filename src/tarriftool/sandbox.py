"""Scenario sandbox: sweep one parameter while holding everything else fixed.

For each sweep value:
  - rebuild tariff-independent demand with the new config
  - dispatch under each tariff
  - record annual cost per tariff, the tariff winner, and the seasonal
    switch saving

Also provides a tornado-style sensitivity comparison across all sweeps.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .config import HomeConfig
from .seasonal import seasonal_switch_summary
from .simulator import simulate_all
from .tariffs import Tariff, builtin_tariffs


@dataclass
class SweepPoint:
    param: str
    value: float
    costs: dict[str, float]
    winner: str
    seasonal_total_gbp: float
    seasonal_saving_gbp: float


def sweep_parameter(
    cfg: HomeConfig,
    param: str,
    values: Iterable[float],
    tariffs: dict[str, Tariff] | None = None,
) -> pd.DataFrame:
    """`param` is a dot-keyed override like 'battery.usable_kwh' or 'ev.weekly_miles'."""
    rows = []
    for v in values:
        c = cfg.with_overrides(**{param: v})
        results = simulate_all(c, tariffs)
        costs = {k: r.annual_cost_gbp for k, r in results.items()}
        winner = min(costs, key=costs.get)
        seasonal = seasonal_switch_summary(results)
        rows.append({
            "param": param,
            "value": v,
            "winner": winner,
            "seasonal_switch_total_gbp": seasonal["switch_total_gbp"],
            "seasonal_saving_vs_single_gbp": seasonal["saving_gbp"],
            "best_single_tariff": seasonal["best_single_tariff"],
            **{f"cost_{k}_gbp": c for k, c in costs.items()},
        })
    return pd.DataFrame(rows)


def tornado(
    cfg: HomeConfig,
    sweeps: dict[str, tuple[float, float]],
    tariffs: dict[str, Tariff] | None = None,
) -> pd.DataFrame:
    """For each (param: (low, high)) entry, compute the annual cost on each
    extreme using the BEST-tariff-per-scenario, and the swing in £.

    Returns a DataFrame ranked by absolute swing (biggest lever first).
    """
    if tariffs is None:
        tariffs = builtin_tariffs()
    rows = []
    for param, (low, high) in sweeps.items():
        df = sweep_parameter(cfg, param, [low, high], tariffs)
        low_row = df[df["value"] == low].iloc[0]
        high_row = df[df["value"] == high].iloc[0]
        low_best = min(low_row[c] for c in df.columns if c.startswith("cost_"))
        high_best = min(high_row[c] for c in df.columns if c.startswith("cost_"))
        rows.append({
            "param": param,
            "low_value": low,
            "high_value": high,
            "low_best_cost_gbp": low_best,
            "high_best_cost_gbp": high_best,
            "swing_gbp": high_best - low_best,
            "abs_swing_gbp": abs(high_best - low_best),
            "low_winner": low_row["winner"],
            "high_winner": high_row["winner"],
        })
    out = pd.DataFrame(rows).sort_values("abs_swing_gbp", ascending=False).reset_index(drop=True)
    return out


# Default sweeps used by the CLI sandbox command — sensible UK ranges.
DEFAULT_SWEEPS: dict[str, tuple[float, float]] = {
    "battery.usable_kwh": (5.0, 25.0),
    "heat_pump.gain_kwh_per_hdd": (3.0, 8.0),
    "ev.weekly_miles": (50.0, 300.0),
    "solar.kwp": (3.0, 12.0),
}

DEFAULT_RANGES: dict[str, list[float]] = {
    "battery.usable_kwh": [5.0, 10.0, 15.0, 20.0, 25.0],
    "heat_pump.gain_kwh_per_hdd": [3.0, 4.5, 5.5, 6.5, 8.0],
    "ev.weekly_miles": [50.0, 100.0, 150.0, 200.0, 300.0],
    "solar.kwp": [3.0, 5.0, 7.0, 9.0, 12.0],
}
