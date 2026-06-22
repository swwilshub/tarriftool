"""
Calibrate the synthetic demand model to your real Octopus / Enphase data.

THIS IS THE CONTAMINATION-FREE PATH. Historic Octopus consumption was shaped
by whatever tariff you were on (battery + EV charging are concentrated in
cheap windows). We do NOT plug that profile straight into the dispatch
engine for a different tariff. Instead we use it to BACK OUT the
tariff-independent physical demand parameters:

    annual_base_kwh       — fitted to summer-night/no-solar consumption
    hp_gain_kwh_per_hdd   — fitted to winter consumption minus base/EV/PV,
                            regressed on heating-degree-days
    annual_ev_kwh         — fitted to driver-supplied weekly miles, or
                            to the long-overnight cheap-window energy bump
    solar_annual_kwh      — annual export + self-consumed PV (if Enphase
                            data available)

Then those parameters are written back into the home.yaml config and the
synthetic generator reproduces a clean half-hourly profile that matches
your real annual energy balance.

This module's first cut implements the simplest of these: take a real
half-hourly consumption series + a temperature series and a known EV daily
kWh, and back out (annual_base_kwh, hp_gain_kwh_per_hdd) by linear
regression of daily consumption on HDD.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class CalibrationResult:
    annual_base_kwh: float
    hp_gain_kwh_per_hdd: float
    daily_ev_kwh: float
    fit_r2: float
    notes: list[str]


def estimate_demand_params(
    consumption_hh: pd.Series,
    outdoor_temp_hh: pd.Series,
    daily_ev_kwh: float,
    scop: float,
    base_temp_c: float = 15.5,
) -> CalibrationResult:
    """Fit daily kWh = c0 + c1 * HDD,  then attribute c0 → (base + EV),
    c1 → HP gain.

    Returns a CalibrationResult. This is a calibration *of the model
    parameters*, NOT the load that's then fed to dispatch.
    """
    if not consumption_hh.index.equals(outdoor_temp_hh.index):
        outdoor_temp_hh = outdoor_temp_hh.reindex(consumption_hh.index, method="nearest")
    daily_kwh = consumption_hh.resample("D").sum()
    daily_t = outdoor_temp_hh.resample("D").mean()
    hdd = (base_temp_c - daily_t).clip(lower=0)

    # Linear fit: daily_kwh = a + b * hdd
    mask = daily_kwh.notna() & hdd.notna()
    y = daily_kwh[mask].to_numpy()
    x = hdd[mask].to_numpy()
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    yhat = X @ coef
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1.0
    r2 = 1 - ss_res / ss_tot

    # a = daily (base + EV); peel off the EV that we know.
    base_daily = max(0.0, a - daily_ev_kwh)
    annual_base = base_daily * 365.0
    # b = daily HP elec per HDD = (gain) / SCOP → gain = b * SCOP.
    hp_gain = max(0.0, b * scop)

    notes = []
    if r2 < 0.5:
        notes.append(f"R^2={r2:.2f} — low. Historic data may be tariff-shifted or noisy.")
    if a < daily_ev_kwh:
        notes.append("Intercept below stated EV demand — EV may not be charging at home, or weekly miles is overstated.")

    return CalibrationResult(
        annual_base_kwh=annual_base,
        hp_gain_kwh_per_hdd=hp_gain,
        daily_ev_kwh=daily_ev_kwh,
        fit_r2=r2,
        notes=notes,
    )


def validation_gate(
    real_bill_gbp: float,
    simulated_annual_gbp: float,
    tolerance_pct: float = 5.0,
) -> tuple[bool, str]:
    """Sanity-check: does the reconstructed demand + dispatch under the OLD
    tariff reproduce the user's real annual cost within tolerance?

    If False, you can NOT trust comparisons against other tariffs — the
    demand reconstruction is wrong somewhere.
    """
    diff_pct = 100.0 * (simulated_annual_gbp - real_bill_gbp) / max(real_bill_gbp, 1e-6)
    ok = abs(diff_pct) <= tolerance_pct
    msg = (f"simulated £{simulated_annual_gbp:,.2f} vs real £{real_bill_gbp:,.2f} "
           f"({diff_pct:+.1f}%, tol ±{tolerance_pct:.1f}%)")
    return ok, msg
