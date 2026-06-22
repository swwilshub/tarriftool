"""Seasonal tariff-swap recommender.

For each month of the year, pick the tariff whose dispatched cost over
that month is lowest. Compare the resulting "best monthly switch" total
against the best fixed-all-year tariff.

This makes the seasonal flip explicit: if Cosy wins Nov–Mar and Go wins
Apr–Oct, you get a recommended switch schedule + £ saved.
"""
from __future__ import annotations

import pandas as pd

from .dispatch import DispatchResult


def monthly_winner_table(results: dict[str, DispatchResult]) -> pd.DataFrame:
    """Per-month cost for each tariff plus a 'winner' column."""
    frames = {k: r.by_month()["cost_gbp"] for k, r in results.items()}
    df = pd.DataFrame(frames)
    df["winner"] = df.idxmin(axis=1)
    df["best_cost_gbp"] = df.drop(columns=["winner"]).min(axis=1)
    return df


def seasonal_switch_summary(results: dict[str, DispatchResult]) -> dict:
    """Returns:
        schedule: per-month winning tariff
        switch_total_gbp: sum of the cheapest tariff per month
        best_single_tariff: the cheapest fixed-all-year tariff
        best_single_cost_gbp
        saving_gbp: switch_total minus best single
    """
    mw = monthly_winner_table(results)
    schedule = mw["winner"].tolist()
    switch_total = float(mw["best_cost_gbp"].sum())
    annual = {k: r.annual_cost_gbp for k, r in results.items()}
    best_single = min(annual, key=annual.get)
    best_single_cost = float(annual[best_single])
    return {
        "schedule": dict(zip(mw.index.month.tolist(), schedule)),
        "schedule_compact": _compact_schedule(mw.index.month.tolist(), schedule),
        "switch_total_gbp": switch_total,
        "best_single_tariff": best_single,
        "best_single_cost_gbp": best_single_cost,
        "saving_gbp": best_single_cost - switch_total,
        "monthly_table": mw,
    }


def _compact_schedule(months: list[int], winners: list[str]) -> list[tuple[str, list[int]]]:
    """Group consecutive months sharing a winner into runs."""
    runs: list[tuple[str, list[int]]] = []
    if not months:
        return runs
    cur_name = winners[0]
    cur_months = [months[0]]
    for m, w in zip(months[1:], winners[1:]):
        if w == cur_name:
            cur_months.append(m)
        else:
            runs.append((cur_name, cur_months))
            cur_name = w
            cur_months = [m]
    runs.append((cur_name, cur_months))
    return runs
