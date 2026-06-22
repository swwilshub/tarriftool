"""
Tariff-DEPENDENT dispatch engine.

Given a tariff-independent demand profile, an asset stack, and a tariff,
this module decides WHEN to draw energy from the grid: it shifts the EV
charging into the cheapest slots of each day, cycles the battery into cheap
windows and out during expensive windows, and consumes solar first.

Algorithm — greedy with a per-day price ranking. For each day:

  1. Rank the day's half-hourly slots by import price (ascending).
  2. Build an EV plan: fill the cheapest slots, up to the home charger rate,
     until that day's required EV kWh is met. If the day has insufficient
     cheap slots, spill into the next-cheapest slots — never "fake" the kWh.
  3. Build a battery grid-charge plan: estimate how much energy the battery
     will need to deliver during the day's expensive slots; reserve that
     much charge in the day's cheapest non-expensive slots (subject to
     capacity and charge-rate).
  4. Chronological execution pass:
       a. Solar serves house load directly.
       b. Surplus solar charges the EV (if EV-charging slot) then battery.
       c. Battery discharges during expensive slots to cover residual load.
       d. Anything left = grid import at slot price.
       e. Solar that finds no home for itself is exported.

Round-trip losses are split evenly across charge and discharge
(sqrt(rte) each side).

This is deliberately heuristic — NOT a MILP — but it's transparent and fast.
It successfully reproduces the seasonal flip (Cosy-winter / Go-summer) by
letting battery SoC genuinely run out on high-demand days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config import BatterySpec, EVSpec
from .tariffs import Tariff


HH_PER_HOUR = 2
HOURS_PER_SLOT = 0.5


@dataclass
class DispatchResult:
    df: pd.DataFrame
    annual_cost_gbp: float
    breakdown: dict[str, Any] = field(default_factory=dict)

    def by_month(self) -> pd.DataFrame:
        d = self.df.copy()
        d["cost_gbp"] = (d["grid_import_kwh"] * d["import_price_p"]
                         - d["grid_export_kwh"] * d["export_price_p"]) / 100.0
        m = d.resample("MS").agg({
            "grid_import_kwh": "sum",
            "grid_export_kwh": "sum",
            "cost_gbp": "sum",
        })
        # Add standing charges per month so the months sum back to the annual
        # cost (without this, seasonal_switch saving has a phantom standing
        # gap baked in).
        standing_p_per_day = float(self.breakdown.get("standing_p_per_day", 0.0))
        days_in_month = d["grid_import_kwh"].resample("D").sum().resample("MS").count()
        m["standing_gbp"] = days_in_month * standing_p_per_day / 100.0
        m["cost_gbp"] = m["cost_gbp"] + m["standing_gbp"]
        m["month"] = m.index.month
        return m

    def by_season(self) -> pd.DataFrame:
        from .tariffs import season_of
        m = self.by_month()
        m["season"] = m["month"].map(season_of)
        return m.groupby("season")[["grid_import_kwh", "grid_export_kwh", "cost_gbp"]].sum()


def dispatch_year(
    demand: pd.DataFrame,
    ev_daily_kwh: pd.Series,
    battery: BatterySpec,
    ev: EVSpec,
    tariff: Tariff,
) -> DispatchResult:
    idx = demand.index
    n = len(idx)
    base = demand["base_load_kwh"].to_numpy()
    hp = demand["hp_kwh"].to_numpy()
    solar = demand["solar_kwh"].to_numpy()
    load = base + hp

    price = tariff.import_price_series(idx)
    export_p = np.full(n, tariff.export_p_per_kwh, dtype=float)

    cap = battery.usable_kwh
    soc = battery.usable_kwh * battery.initial_soc_frac
    rte = battery.round_trip_efficiency
    eff = rte ** 0.5  # one-way efficiency on each side
    ch_per_slot = battery.max_charge_kw * HOURS_PER_SLOT
    dc_per_slot = battery.max_discharge_kw * HOURS_PER_SLOT
    ev_ch_per_slot = ev.home_charge_kw * HOURS_PER_SLOT

    # Outputs
    grid_import = np.zeros(n)
    grid_export = np.zeros(n)
    batt_charge_in = np.zeros(n)   # energy delivered to battery terminal (before charge loss)
    batt_discharge_out = np.zeros(n)  # energy delivered to load after discharge loss
    ev_charge = np.zeros(n)
    soc_track = np.zeros(n)

    # Group by calendar day
    day_arr = idx.normalize()
    unique_days = pd.DatetimeIndex(pd.Series(day_arr).unique())
    # Pre-compute slot indices per day
    day_to_slots: dict[pd.Timestamp, np.ndarray] = {}
    day_codes = np.searchsorted(unique_days, day_arr)
    for i, d in enumerate(unique_days):
        day_to_slots[d] = np.flatnonzero(day_codes == i)

    for day in unique_days:
        slots = day_to_slots[day]
        if len(slots) == 0:
            continue
        p_today = price[slots]
        load_today = load[slots]
        solar_today = solar[slots]

        # Cheap-tier threshold per day (tariff-relative). Any slot strictly
        # below this is treated as "cheap": battery grid-charges, EV charges.
        # Slots at or above this are treated as "non-cheap": battery
        # discharges to cover residual load.
        cheap_thresh = float(np.percentile(p_today, 35))

        order_cheap = slots[np.argsort(p_today, kind="stable")]

        # --- EV plan: cheapest slots until day's need met ---
        ev_need = float(ev_daily_kwh.get(day, 0.0))
        ev_plan: dict[int, float] = {}
        rem = ev_need
        for s in order_cheap:
            if rem <= 1e-9:
                break
            take = min(ev_ch_per_slot, rem)
            ev_plan[int(s)] = take
            rem -= take

        # --- Battery grid-charge plan ---
        # Strategy: in every cheap-tier slot, plan to charge at max rate.
        # The chronological pass clips to remaining capacity room. This means
        # the battery is opportunistically refilled to capacity at the start
        # of every cheap window — which is exactly what makes Cosy's three
        # windows worth more than Go's single one when daily load is large.
        batt_plan: dict[int, float] = {}
        for s in slots:
            if float(price[s]) < cheap_thresh:
                batt_plan[int(s)] = ch_per_slot

        # --- Chronological execution ---
        for s in slots:
            s = int(s)
            slot_price = float(price[s])
            slot_export_p = float(export_p[s])
            slot_load = float(load[s])
            slot_solar = float(solar[s])

            # 1. Solar serves load.
            solar_used = min(slot_solar, slot_load)
            residual_load = slot_load - solar_used
            solar_surplus = slot_solar - solar_used

            # 2. EV planned charge — prefer solar surplus, else grid.
            ev_take = ev_plan.get(s, 0.0)
            ev_from_solar = min(ev_take, solar_surplus)
            solar_surplus -= ev_from_solar
            ev_from_grid = ev_take - ev_from_solar

            # 3. Battery charging.
            #    Always try to soak up remaining solar surplus (free energy);
            #    then add planned grid-charge.
            room_terminal = (cap - soc) / max(eff, 1e-9)  # kWh at terminal to fill
            room_terminal = min(room_terminal, ch_per_slot)
            batt_from_solar = min(solar_surplus, room_terminal)
            solar_surplus -= batt_from_solar
            room_terminal -= batt_from_solar
            batt_from_grid = min(batt_plan.get(s, 0.0), room_terminal)
            batt_in_total = batt_from_solar + batt_from_grid

            # 4. Battery discharge in non-cheap slots covering residual load.
            #    The break-even is (charge_price / rte) < discharge_price; for
            #    UK time-of-use tariffs this almost always holds outside the
            #    cheap window.
            slot_discharge_out = 0.0
            if slot_price >= cheap_thresh and residual_load > 0:
                avail_out = soc * eff  # what battery can deliver after loss
                avail_out = min(avail_out, dc_per_slot)
                slot_discharge_out = min(avail_out, residual_load)
                residual_load -= slot_discharge_out

            # 5. Anything residual = grid.
            slot_grid_import = residual_load + ev_from_grid + batt_from_grid

            # 6. Export remaining solar.
            slot_export = solar_surplus

            # SoC update: in_stored = terminal_in * eff, out_stored = delivered / eff
            soc = soc + batt_in_total * eff - slot_discharge_out / max(eff, 1e-9)
            soc = max(0.0, min(cap, soc))

            grid_import[s] = slot_grid_import
            grid_export[s] = slot_export
            batt_charge_in[s] = batt_in_total
            batt_discharge_out[s] = slot_discharge_out
            ev_charge[s] = ev_take
            soc_track[s] = soc

    # Costs
    standing_days = len(unique_days)
    standing_p = standing_days * tariff.standing_p_per_day
    import_cost_p = float(np.sum(grid_import * price))
    export_rev_p = float(np.sum(grid_export * export_p))
    annual_cost = (import_cost_p + standing_p - export_rev_p) / 100.0

    # Attribution by load category (post-hoc, by share of physical demand).
    total_load_kwh = float(load.sum()) + float(ev_charge.sum())
    if total_load_kwh > 0:
        share_hp = float(hp.sum()) / total_load_kwh
        share_base = float(base.sum()) / total_load_kwh
        share_ev = float(ev_charge.sum()) / total_load_kwh
    else:
        share_hp = share_base = share_ev = 0.0
    batt_loss_kwh = float(batt_charge_in.sum() - batt_discharge_out.sum())

    breakdown = {
        "import_kwh": float(grid_import.sum()),
        "export_kwh": float(grid_export.sum()),
        "import_cost_gbp": import_cost_p / 100.0,
        "export_revenue_gbp": export_rev_p / 100.0,
        "standing_gbp": standing_p / 100.0,
        "annual_cost_gbp": annual_cost,
        "battery_loss_kwh": batt_loss_kwh,
        # Approximate spend by category (import_cost share-weighted).
        "spend_hp_gbp": (import_cost_p / 100.0) * share_hp,
        "spend_base_gbp": (import_cost_p / 100.0) * share_base,
        "spend_ev_gbp": (import_cost_p / 100.0) * share_ev,
        "standing_p_per_day": tariff.standing_p_per_day,
    }

    df_out = pd.DataFrame({
        "base_kwh": base,
        "hp_kwh": hp,
        "solar_kwh": solar,
        "ev_kwh": ev_charge,
        "batt_charge_kwh": batt_charge_in,
        "batt_discharge_kwh": batt_discharge_out,
        "soc_kwh": soc_track,
        "grid_import_kwh": grid_import,
        "grid_export_kwh": grid_export,
        "import_price_p": price,
        "export_price_p": export_p,
    }, index=idx)

    return DispatchResult(df=df_out, annual_cost_gbp=annual_cost, breakdown=breakdown)
