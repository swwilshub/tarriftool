"""
Octopus Energy API adapter.

Purpose (per the design principle):
  1. Pull LIVE half-hourly tariff rates and standing charges so cost figures
     reflect actual published prices.
  2. Pull half-hourly consumption + export so the synthetic demand model can
     be CALIBRATED to this specific home.

CRITICAL: historic consumption is NOT used directly as the demand profile
for tariff comparisons — it bakes in whichever tariff was in force at the
time. See `calibrate.estimate_demand_params()` for the reconstruction path.

Auth: API key as basic-auth username (password blank).
Docs: https://developer.octopus.energy/docs/api/
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import pandas as pd
import requests

BASE = "https://api.octopus.energy/v1"


class OctopusError(RuntimeError):
    pass


class OctopusClient:
    def __init__(
        self,
        api_key: str | None = None,
        mpan: str | None = None,
        serial: str | None = None,
        export_mpan: str | None = None,
        export_serial: str | None = None,
        account_number: str | None = None,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key or os.environ.get("OCTOPUS_API_KEY", "")
        self.mpan = mpan or os.environ.get("OCTOPUS_MPAN", "")
        self.serial = serial or os.environ.get("OCTOPUS_SERIAL", "")
        self.export_mpan = export_mpan or os.environ.get("OCTOPUS_EXPORT_MPAN", "")
        self.export_serial = export_serial or os.environ.get("OCTOPUS_EXPORT_SERIAL", "")
        self.account_number = account_number or os.environ.get("OCTOPUS_ACCOUNT_NUMBER", "")
        if not self.api_key:
            raise OctopusError("OCTOPUS_API_KEY not set in environment")
        self.session = session or requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(BASE + path, auth=(self.api_key, ""), params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    # ----- Consumption -----

    def consumption(
        self,
        period_from: datetime,
        period_to: datetime,
        export: bool = False,
    ) -> pd.DataFrame:
        mpan = self.export_mpan if export else self.mpan
        serial = self.export_serial if export else self.serial
        if not mpan or not serial:
            raise OctopusError("MPAN / serial not configured")
        path = f"/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
        params = {
            "period_from": _iso(period_from),
            "period_to": _iso(period_to),
            "page_size": 25000,
            "order_by": "period",
        }
        rows: list[dict] = []
        url = path
        while True:
            data = self._get(url, params=params)
            rows.extend(data.get("results", []))
            nxt = data.get("next")
            if not nxt:
                break
            # `next` is a full URL; strip BASE if present.
            url = nxt.replace(BASE, "")
            params = None
        if not rows:
            return pd.DataFrame(columns=["consumption_kwh"])
        df = pd.DataFrame(rows)
        df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True)
        df = df.set_index("interval_start").sort_index()
        return df[["consumption"]].rename(columns={"consumption": "consumption_kwh"})

    # ----- Tariff rates -----

    def standard_unit_rates(
        self,
        product_code: str,
        tariff_code: str,
        period_from: datetime,
        period_to: datetime,
    ) -> pd.DataFrame:
        path = f"/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        data = self._get(path, params={
            "period_from": _iso(period_from),
            "period_to": _iso(period_to),
            "page_size": 1500,
        })
        rows = data.get("results", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["valid_from"] = pd.to_datetime(df["valid_from"], utc=True)
        df["valid_to"] = pd.to_datetime(df["valid_to"], utc=True)
        return df.sort_values("valid_from").reset_index(drop=True)

    def standing_charges(
        self,
        product_code: str,
        tariff_code: str,
        period_from: datetime,
        period_to: datetime,
    ) -> pd.DataFrame:
        path = f"/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"
        data = self._get(path, params={
            "period_from": _iso(period_from),
            "period_to": _iso(period_to),
        })
        rows = data.get("results", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["valid_from"] = pd.to_datetime(df["valid_from"], utc=True)
        return df.sort_values("valid_from").reset_index(drop=True)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def rates_to_half_hourly(rates: pd.DataFrame, idx: pd.DatetimeIndex) -> np.ndarray:
    """Given a `standard_unit_rates` DataFrame from Octopus, return a price
    array aligned to `idx` (p/kWh).

    Octopus returns step-function rate periods; we re-sample to half-hourly
    by forward-filling within each period.
    """
    if rates.empty:
        return np.zeros(len(idx))
    s = pd.Series(rates["value_inc_vat"].to_numpy(), index=rates["valid_from"]).sort_index()
    # Ensure tz-aware to match idx
    if s.index.tz is None:
        s.index = s.index.tz_localize("UTC")
    target = idx.tz_convert("UTC") if idx.tz is not None else idx
    return s.reindex(target, method="ffill").bfill().to_numpy()
