"""Typed config loader for the home / asset / demand parameters."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass
class BatterySpec:
    usable_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    round_trip_efficiency: float = 0.90
    initial_soc_frac: float = 0.5


@dataclass
class EVSpec:
    battery_kwh: float
    home_charge_kw: float
    weekly_miles: float
    efficiency_mi_per_kwh: float

    @property
    def daily_kwh(self) -> float:
        return (self.weekly_miles / self.efficiency_mi_per_kwh) / 7.0


@dataclass
class HeatPumpSpec:
    rated_input_kw: float
    scop: float
    base_temp_c: float = 15.5
    gain_kwh_per_hdd: float = 5.5
    diurnal_shape: str = "morning_evening"


@dataclass
class BaseLoadSpec:
    annual_kwh: float
    diurnal_shape: str = "uk_residential"


@dataclass
class SolarSpec:
    kwp: float
    yield_kwh_per_kwp_year: float = 950.0
    azimuth_deg: float = 180.0
    tilt_deg: float = 35.0


@dataclass
class LocationSpec:
    latitude: float = 51.5
    longitude: float = -0.1
    timezone: str = "Europe/London"


@dataclass
class SimulationSpec:
    year: int = 2024


@dataclass
class HomeConfig:
    battery: BatterySpec
    ev: EVSpec
    heat_pump: HeatPumpSpec
    base_load: BaseLoadSpec
    solar: SolarSpec
    location: LocationSpec = field(default_factory=LocationSpec)
    simulation: SimulationSpec = field(default_factory=SimulationSpec)

    @classmethod
    def load(cls, path: str | Path) -> "HomeConfig":
        with open(path, "r") as f:
            d = yaml.safe_load(f) or {}
        return cls(
            battery=BatterySpec(**d["battery"]),
            ev=EVSpec(**d["ev"]),
            heat_pump=HeatPumpSpec(**d["heat_pump"]),
            base_load=BaseLoadSpec(**d["base_load"]),
            solar=SolarSpec(**d["solar"]),
            location=LocationSpec(**d.get("location", {})),
            simulation=SimulationSpec(**d.get("simulation", {})),
        )

    def with_overrides(self, **overrides: Any) -> "HomeConfig":
        """Return a copy with dot-keyed overrides applied.

        Example:
            cfg.with_overrides(**{"battery.usable_kwh": 20.0})
        """
        new = HomeConfig(
            battery=replace(self.battery),
            ev=replace(self.ev),
            heat_pump=replace(self.heat_pump),
            base_load=replace(self.base_load),
            solar=replace(self.solar),
            location=replace(self.location),
            simulation=replace(self.simulation),
        )
        for k, v in overrides.items():
            section, _, attr = k.partition(".")
            target = getattr(new, section)
            setattr(target, attr, v)
        return new
