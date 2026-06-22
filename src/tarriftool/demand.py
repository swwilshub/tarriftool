"""
Tariff-INDEPENDENT demand generation.

This module produces the physical load and generation profile of the home.
It NEVER references prices, tariffs, or windows: the same profile is fed into
every tariff comparison so that comparisons are honest.

Outputs a half-hourly pandas DataFrame with columns:
    base_load_kwh   non-dispatchable house load (appliances, lighting, HW)
    hp_kwh          heat-pump electrical demand (shape fixed; battery offsets it)
    solar_kwh       PV generation (fixed)
and a per-day Series of EV kWh required (dispatchable in time).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import HomeConfig

HH_PER_DAY = 48


# ---------------------------------------------------------------------------
# Index + weather

def half_hourly_index(year: int, tz: str = "Europe/London") -> pd.DatetimeIndex:
    start = pd.Timestamp(f"{year}-01-01", tz=tz)
    end = pd.Timestamp(f"{year + 1}-01-01", tz=tz)
    return pd.date_range(start, end, freq="30min", inclusive="left")


def synthetic_outdoor_temp(idx: pd.DatetimeIndex) -> np.ndarray:
    """A plausible UK outdoor temperature: annual sine + diurnal sine.

    Annual mean ~11 C, peak ~18 C in July, ~4 C in January.
    Diurnal swing ~8 C peak-to-peak with afternoon peak.
    """
    doy = idx.dayofyear.to_numpy() + idx.hour.to_numpy() / 24.0
    hod = idx.hour.to_numpy() + idx.minute.to_numpy() / 60.0
    seasonal = 11.0 - 7.0 * np.cos(2 * np.pi * (doy - 15) / 365.0)
    diurnal = -4.0 * np.cos(2 * np.pi * (hod - 14.0) / 24.0)
    return seasonal + diurnal


# ---------------------------------------------------------------------------
# Base load

def _diurnal_base_shape(shape: str) -> np.ndarray:
    hod = np.arange(HH_PER_DAY) / 2.0
    if shape == "flat":
        return np.ones(HH_PER_DAY)
    # UK residential: small morning peak ~7:30, larger evening peak ~19:00,
    # overnight floor ~0.25 (fridge, standby).
    morn = np.exp(-((hod - 7.5) ** 2) / (2 * 1.5 ** 2))
    even = 1.3 * np.exp(-((hod - 19.0) ** 2) / (2 * 2.0 ** 2))
    return 0.25 + morn + even


def base_load_kwh(idx: pd.DatetimeIndex, annual_kwh: float, shape: str = "uk_residential") -> np.ndarray:
    shape_arr = _diurnal_base_shape(shape)
    daily_target = annual_kwh / 365.0
    shape_norm = shape_arr / shape_arr.sum()  # sums to 1 per day
    out = np.zeros(len(idx))
    # broadcast per-day
    hod_idx = (idx.hour * 2 + idx.minute // 30).to_numpy()
    out = shape_norm[hod_idx] * daily_target
    return out


# ---------------------------------------------------------------------------
# Heat pump

def _diurnal_hp_shape(shape: str) -> np.ndarray:
    hod = np.arange(HH_PER_DAY) / 2.0
    if shape == "flat":
        return np.ones(HH_PER_DAY) / HH_PER_DAY
    s = (
        0.4
        + 1.0 * np.exp(-((hod - 7.0) ** 2) / (2 * 2.0 ** 2))
        + 1.2 * np.exp(-((hod - 18.0) ** 2) / (2 * 2.5 ** 2))
    )
    return s / s.sum()


def heat_pump_kwh(
    idx: pd.DatetimeIndex,
    outdoor_temp_c: np.ndarray,
    base_temp_c: float,
    gain_kwh_per_hdd: float,
    scop: float,
    rated_input_kw: float,
    diurnal_shape: str = "morning_evening",
) -> np.ndarray:
    """Heat-pump *electrical* demand, half-hourly.

    Daily heat demand = HDD * gain.  HDD uses daily mean outdoor temp.
    Electrical = heat / SCOP. Spread across the day per the diurnal shape.
    Capped by rated_input_kw per half-hour (rate cap).
    """
    df = pd.DataFrame({"t": outdoor_temp_c}, index=idx)
    daily_mean_t = df["t"].resample("D").mean()
    hdd = (base_temp_c - daily_mean_t).clip(lower=0)
    daily_heat = hdd * gain_kwh_per_hdd
    daily_elec = daily_heat / scop

    shape = _diurnal_hp_shape(diurnal_shape)
    out = np.zeros(len(idx))
    dates_arr = idx.normalize()
    cap_per_slot = rated_input_kw * 0.5  # max kWh per half-hour
    for d, elec_today in daily_elec.items():
        if elec_today <= 0:
            continue
        mask = np.asarray(dates_arr == d)
        slots = np.flatnonzero(mask)
        n = len(slots)
        if n == 0:
            continue
        # Trim/pad shape if DST shortened the day (47 or 49 slots).
        sh = shape[:n] if n <= HH_PER_DAY else np.concatenate([shape, np.zeros(n - HH_PER_DAY)])
        sh = sh / sh.sum()
        allocated = elec_today * sh
        out[slots] = np.minimum(allocated, cap_per_slot)
    return out


# ---------------------------------------------------------------------------
# Solar

def solar_kwh(
    idx: pd.DatetimeIndex,
    kwp: float,
    annual_yield_kwh_per_kwp: float,
    latitude: float = 51.5,
) -> np.ndarray:
    """A simple solar generation proxy: clear-sky cosine shape between sunrise
    and sunset, scaled by a UK-shaped seasonal envelope so the annual sum
    matches kwp * yield_kwh_per_kwp.
    """
    doy = idx.dayofyear.to_numpy()
    hod = idx.hour.to_numpy() + idx.minute.to_numpy() / 60.0
    decl_deg = 23.44 * np.sin(np.radians(360.0 * (doy - 81) / 365.0))
    decl = np.radians(decl_deg)
    lat = np.radians(latitude)
    cos_omega0 = np.clip(-np.tan(lat) * np.tan(decl), -1, 1)
    half_day_hours = np.degrees(np.arccos(cos_omega0)) / 15.0
    # solar noon ≈ 12:00 local (ignoring equation of time + DST)
    angle = np.pi * (hod - 12.0) / (2.0 * np.maximum(half_day_hours, 1e-6))
    in_daylight = (np.abs(hod - 12.0) <= half_day_hours).astype(float)
    inst = np.cos(angle) * in_daylight
    inst = np.clip(inst, 0.0, None)

    # Seasonal envelope: UK insolation roughly tracks day length but with
    # heavier winter cloud. Use a clipped sine with min ~0.25 in deep winter.
    seasonal = 0.25 + 0.75 * np.maximum(0.0, np.sin(2 * np.pi * (doy - 80) / 365.0))
    raw = inst * seasonal  # dimensionless instantaneous-power proxy

    # Normalise to annual kWh: each slot is 0.5 h, so kWh ∝ raw * 0.5 * scale.
    total = float(raw.sum()) * 0.5
    if total <= 0:
        return np.zeros(len(idx))
    target_kwh = kwp * annual_yield_kwh_per_kwp
    scale = target_kwh / total
    return raw * 0.5 * scale


# ---------------------------------------------------------------------------
# EV — total daily kWh requirement (dispatch decides timing)

def ev_daily_requirement(idx: pd.DatetimeIndex, weekly_miles: float, mi_per_kwh: float) -> pd.Series:
    daily_kwh = (weekly_miles / mi_per_kwh) / 7.0
    days = pd.DatetimeIndex(pd.Series(idx.normalize()).unique())
    return pd.Series(daily_kwh, index=days, name="ev_daily_kwh")


# ---------------------------------------------------------------------------
# Top-level: build the full tariff-independent demand record

def build_demand(cfg: HomeConfig) -> tuple[pd.DataFrame, pd.Series, np.ndarray]:
    """Return (half-hourly DataFrame, per-day EV kWh need, outdoor temp array)."""
    idx = half_hourly_index(cfg.simulation.year, tz=cfg.location.timezone)
    temp = synthetic_outdoor_temp(idx)
    base = base_load_kwh(idx, cfg.base_load.annual_kwh, cfg.base_load.diurnal_shape)
    hp = heat_pump_kwh(
        idx, temp,
        base_temp_c=cfg.heat_pump.base_temp_c,
        gain_kwh_per_hdd=cfg.heat_pump.gain_kwh_per_hdd,
        scop=cfg.heat_pump.scop,
        rated_input_kw=cfg.heat_pump.rated_input_kw,
        diurnal_shape=cfg.heat_pump.diurnal_shape,
    )
    pv = solar_kwh(
        idx, kwp=cfg.solar.kwp,
        annual_yield_kwh_per_kwp=cfg.solar.yield_kwh_per_kwp_year,
        latitude=cfg.location.latitude,
    )
    df = pd.DataFrame(
        {"base_load_kwh": base, "hp_kwh": hp, "solar_kwh": pv},
        index=idx,
    )
    ev = ev_daily_requirement(idx, cfg.ev.weekly_miles, cfg.ev.efficiency_mi_per_kwh)
    return df, ev, temp
