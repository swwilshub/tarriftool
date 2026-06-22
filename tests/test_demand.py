import numpy as np

from tarriftool.config import HomeConfig
from tarriftool.demand import build_demand, half_hourly_index, synthetic_outdoor_temp


def _cfg(tmp_path):
    cfg = """
battery: {usable_kwh: 15, max_charge_kw: 6, max_discharge_kw: 6, round_trip_efficiency: 0.9, initial_soc_frac: 0.5}
ev: {battery_kwh: 100, home_charge_kw: 7, weekly_miles: 70, efficiency_mi_per_kwh: 2.2}
heat_pump: {rated_input_kw: 9, scop: 4, base_temp_c: 15.5, gain_kwh_per_hdd: 5.5}
base_load: {annual_kwh: 3000}
solar: {kwp: 9, yield_kwh_per_kwp_year: 950}
simulation: {year: 2023}
"""
    p = tmp_path / "h.yaml"
    p.write_text(cfg)
    return HomeConfig.load(p)


def test_index_has_full_year(tmp_path):
    idx = half_hourly_index(2023, "Europe/London")
    # 365 days x 48 HH = 17520, ±1 for DST
    assert 17518 <= len(idx) <= 17522


def test_outdoor_temp_seasonal(tmp_path):
    idx = half_hourly_index(2023, "Europe/London")
    t = synthetic_outdoor_temp(idx)
    # July warmer than January
    july = t[(idx.month == 7)].mean()
    jan = t[(idx.month == 1)].mean()
    assert july - jan > 10


def test_annual_totals_track_config(tmp_path):
    cfg = _cfg(tmp_path)
    df, ev_daily, _ = build_demand(cfg)
    # base load should match annual_kwh within 1%
    assert abs(df["base_load_kwh"].sum() - 3000) / 3000 < 0.01
    # solar should match kWp * yield within 1%
    assert abs(df["solar_kwh"].sum() - 9 * 950) / (9 * 950) < 0.01
    # EV total = weekly * 52 = (70/2.2) * 52 / 7 * 7 days
    expected_ev = (70 / 2.2) / 7 * len(ev_daily)
    assert abs(ev_daily.sum() - expected_ev) / expected_ev < 0.01
    # HP should be nonzero and concentrated in winter
    monthly_hp = df["hp_kwh"].groupby(df.index.month).sum()
    assert monthly_hp[1] > monthly_hp[7]
