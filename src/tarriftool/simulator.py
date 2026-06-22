"""High-level glue: build demand, run dispatch for one or many tariffs."""
from __future__ import annotations

import pandas as pd

from .config import HomeConfig
from .demand import build_demand
from .dispatch import DispatchResult, dispatch_year
from .tariffs import Tariff, builtin_tariffs


def simulate(cfg: HomeConfig, tariff: Tariff) -> DispatchResult:
    demand, ev_daily, _ = build_demand(cfg)
    return dispatch_year(demand, ev_daily, cfg.battery, cfg.ev, tariff)


def simulate_all(cfg: HomeConfig, tariffs: dict[str, Tariff] | None = None) -> dict[str, DispatchResult]:
    """Run dispatch for every tariff against the SAME demand profile."""
    if tariffs is None:
        tariffs = builtin_tariffs()
    demand, ev_daily, _ = build_demand(cfg)
    results: dict[str, DispatchResult] = {}
    for key, t in tariffs.items():
        results[key] = dispatch_year(demand, ev_daily, cfg.battery, cfg.ev, t)
    return results


def annual_costs(results: dict[str, DispatchResult]) -> pd.Series:
    return pd.Series({k: r.annual_cost_gbp for k, r in results.items()}).sort_values()


def monthly_costs(results: dict[str, DispatchResult]) -> pd.DataFrame:
    """One column per tariff, rows are month starts (£/month)."""
    frames = []
    for k, r in results.items():
        m = r.by_month()[["cost_gbp"]].rename(columns={"cost_gbp": k})
        frames.append(m)
    return pd.concat(frames, axis=1)


def spend_breakdown(results: dict[str, DispatchResult]) -> pd.DataFrame:
    rows = []
    for k, r in results.items():
        b = r.breakdown
        rows.append({
            "tariff": k,
            "annual_cost_gbp": b["annual_cost_gbp"],
            "import_cost_gbp": b["import_cost_gbp"],
            "export_revenue_gbp": b["export_revenue_gbp"],
            "standing_gbp": b["standing_gbp"],
            "spend_hp_gbp": b["spend_hp_gbp"],
            "spend_base_gbp": b["spend_base_gbp"],
            "spend_ev_gbp": b["spend_ev_gbp"],
            "battery_loss_kwh": b["battery_loss_kwh"],
            "import_kwh": b["import_kwh"],
            "export_kwh": b["export_kwh"],
        })
    return pd.DataFrame(rows).set_index("tariff").sort_values("annual_cost_gbp")
