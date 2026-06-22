/* Tariff-independent demand + tariff-dependent dispatch — JS port. */
"use strict";

const HH_PER_DAY = 48;
const DAYS = 365;
const N = HH_PER_DAY * DAYS;  // 17520

// ---------- Demand ----------

function buildIndex() {
  // hour-of-day (0..23.5), day-of-year (1..365), month (1..12)
  const hod = new Float32Array(N);
  const doy = new Int16Array(N);
  const month = new Int8Array(N);
  const slotOfDay = new Int8Array(N);
  // cumulative days per month (non-leap)
  const dim = [31,28,31,30,31,30,31,31,30,31,30,31];
  let m = 0, dayInMonth = 1;
  let acc = 0;
  for (let d = 0; d < DAYS; d++) {
    if (dayInMonth > dim[m]) { m++; dayInMonth = 1; }
    for (let s = 0; s < HH_PER_DAY; s++) {
      const i = d * HH_PER_DAY + s;
      hod[i] = s * 0.5;
      doy[i] = d + 1;
      month[i] = m + 1;
      slotOfDay[i] = s;
    }
    dayInMonth++;
  }
  return { hod, doy, month, slotOfDay };
}

function outdoorTemp(idx) {
  // Annual sine + diurnal sine, UK-ish.
  const out = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const seasonal = 11.0 - 7.0 * Math.cos(2 * Math.PI * (idx.doy[i] - 15) / 365);
    const diurnal = -4.0 * Math.cos(2 * Math.PI * (idx.hod[i] - 14.0) / 24);
    out[i] = seasonal + diurnal;
  }
  return out;
}

function baseLoadShape() {
  const sh = new Float32Array(HH_PER_DAY);
  for (let s = 0; s < HH_PER_DAY; s++) {
    const h = s * 0.5;
    const morn = Math.exp(-Math.pow(h - 7.5, 2) / (2 * 1.5 * 1.5));
    const even = 1.3 * Math.exp(-Math.pow(h - 19.0, 2) / (2 * 2.0 * 2.0));
    sh[s] = 0.25 + morn + even;
  }
  const sum = sh.reduce((a, b) => a + b, 0);
  for (let s = 0; s < HH_PER_DAY; s++) sh[s] /= sum;
  return sh;
}

function hpShape() {
  const sh = new Float32Array(HH_PER_DAY);
  for (let s = 0; s < HH_PER_DAY; s++) {
    const h = s * 0.5;
    sh[s] = 0.4
      + Math.exp(-Math.pow(h - 7.0, 2) / (2 * 2.0 * 2.0))
      + 1.2 * Math.exp(-Math.pow(h - 18.0, 2) / (2 * 2.5 * 2.5));
  }
  const sum = sh.reduce((a, b) => a + b, 0);
  for (let s = 0; s < HH_PER_DAY; s++) sh[s] /= sum;
  return sh;
}

function buildDemand(idx, temp, cfg) {
  const baseShape = baseLoadShape();
  const baseDaily = cfg.baseAnnualKwh / 365;
  const base = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    base[i] = baseShape[idx.slotOfDay[i]] * baseDaily;
  }

  // HP per day: HDD * gain / SCOP, distributed per shape, rate-capped.
  const hpSh = hpShape();
  const hpCap = cfg.hpRatedKw * 0.5; // kWh per HH
  const hp = new Float32Array(N);
  for (let d = 0; d < DAYS; d++) {
    let tmean = 0;
    const off = d * HH_PER_DAY;
    for (let s = 0; s < HH_PER_DAY; s++) tmean += temp[off + s];
    tmean /= HH_PER_DAY;
    const hdd = Math.max(0, cfg.hpBaseTempC - tmean);
    const dailyElec = hdd * cfg.hpGain / cfg.scop;
    for (let s = 0; s < HH_PER_DAY; s++) {
      hp[off + s] = Math.min(dailyElec * hpSh[s], hpCap);
    }
  }

  // Solar: clear-sky cosine envelope, UK seasonal scaling, normalised to kwp * yield.
  const lat = 51.5;
  const solar = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const decl = (23.44 * Math.PI / 180) * Math.sin(2 * Math.PI * (idx.doy[i] - 81) / 365);
    const latR = lat * Math.PI / 180;
    const cosOmega = Math.max(-1, Math.min(1, -Math.tan(latR) * Math.tan(decl)));
    const halfDay = Math.acos(cosOmega) * 180 / Math.PI / 15;
    const dh = idx.hod[i] - 12;
    let inst = 0;
    if (Math.abs(dh) <= halfDay && halfDay > 0) {
      inst = Math.cos(Math.PI * dh / (2 * halfDay));
      if (inst < 0) inst = 0;
    }
    const seasonalEnv = 0.25 + 0.75 * Math.max(0, Math.sin(2 * Math.PI * (idx.doy[i] - 80) / 365));
    solar[i] = inst * seasonalEnv;
  }
  let total = 0;
  for (let i = 0; i < N; i++) total += solar[i] * 0.5;
  if (total > 0) {
    const scale = (cfg.solarKwp * cfg.solarYield) / total;
    for (let i = 0; i < N; i++) solar[i] = solar[i] * 0.5 * scale;
  }

  const evDailyKwh = (cfg.evWeeklyMiles / cfg.evMiPerKwh) / 7;
  return { base, hp, solar, evDailyKwh };
}

// ---------- Tariffs ----------

function tariffPrices(name, idx) {
  // returns Float32Array of length N with p/kWh
  const p = new Float32Array(N);
  if (name === "intelligent_go") {
    for (let i = 0; i < N; i++) {
      const h = idx.hod[i];
      p[i] = (h >= 23.5 || h < 5.5) ? 7.0 : 25.0;
    }
  } else if (name === "go") {
    for (let i = 0; i < N; i++) {
      const h = idx.hod[i];
      p[i] = (h >= 0.5 && h < 5.5) ? 8.5 : 28.0;
    }
  } else if (name === "cosy") {
    for (let i = 0; i < N; i++) {
      const h = idx.hod[i];
      let v = 25.0;
      if ((h >= 4 && h < 7) || (h >= 13 && h < 16) || (h >= 22 && h < 24)) v = 12.0;
      else if (h >= 16 && h < 19) v = 39.0;
      p[i] = v;
    }
  } else if (name === "agile") {
    for (let i = 0; i < N; i++) {
      const base = 22.0 + 14.0 * Math.cos(2 * Math.PI * (idx.hod[i] - 17.5) / 24);
      const seasonal = 4.0 * Math.cos(2 * Math.PI * (idx.doy[i] - 15) / 365);
      p[i] = base + seasonal;
    }
  } else if (name === "flat") {
    p.fill(24.0);
  }
  return p;
}

const TARIFFS = {
  intelligent_go: { label: "Intelligent Octopus Go", standing: 50.0, export: 15.0, color: "#4f9" },
  go:             { label: "Octopus Go",             standing: 50.0, export: 15.0, color: "#9c4" },
  cosy:           { label: "Cosy Octopus",           standing: 50.0, export: 15.0, color: "#fa3" },
  agile:          { label: "Octopus Agile (synth.)", standing: 50.0, export: 15.0, color: "#5af" },
  flat:           { label: "Flat 24p baseline",      standing: 50.0, export: 15.0, color: "#aaa" },
};

// ---------- Dispatch ----------

function percentile(arr, p) {
  const s = Float32Array.from(arr).sort();
  const k = (p / 100) * (s.length - 1);
  const lo = Math.floor(k), hi = Math.ceil(k);
  if (lo === hi) return s[lo];
  return s[lo] + (s[hi] - s[lo]) * (k - lo);
}

function dispatch(demand, idx, cfg, tariffName) {
  const prices = tariffPrices(tariffName, idx);
  const tInfo = TARIFFS[tariffName];

  const cap = cfg.batteryKwh;
  const eff = Math.sqrt(cfg.batteryRte);
  const chPerSlot = cfg.batteryKw * 0.5;
  const dcPerSlot = cfg.batteryKw * 0.5;
  const evChPerSlot = cfg.evChargeKw * 0.5;
  let soc = cap * 0.5;

  const gridImport = new Float32Array(N);
  const gridExport = new Float32Array(N);
  const evCharge = new Float32Array(N);
  const battIn = new Float32Array(N);
  const battOut = new Float32Array(N);

  const evDaily = demand.evDailyKwh;
  const dayPriceBuf = new Float32Array(HH_PER_DAY);
  const orderBuf = new Int16Array(HH_PER_DAY);

  for (let d = 0; d < DAYS; d++) {
    const off = d * HH_PER_DAY;
    for (let s = 0; s < HH_PER_DAY; s++) dayPriceBuf[s] = prices[off + s];

    // Per-day cheap threshold = 35th percentile.
    const cheap = percentile(dayPriceBuf, 35);

    // Order slots ascending by today's price.
    for (let s = 0; s < HH_PER_DAY; s++) orderBuf[s] = s;
    // simple insertion sort (small array, fast)
    for (let i = 1; i < HH_PER_DAY; i++) {
      const v = orderBuf[i];
      const vp = dayPriceBuf[v];
      let j = i - 1;
      while (j >= 0 && dayPriceBuf[orderBuf[j]] > vp) { orderBuf[j + 1] = orderBuf[j]; j--; }
      orderBuf[j + 1] = v;
    }

    // EV plan: cheapest slots until day's need met.
    const evPlan = new Float32Array(HH_PER_DAY);
    let evRem = evDaily;
    for (let k = 0; k < HH_PER_DAY && evRem > 1e-9; k++) {
      const s = orderBuf[k];
      const take = Math.min(evChPerSlot, evRem);
      evPlan[s] = take;
      evRem -= take;
    }

    // Battery grid-charge plan: max rate in every cheap-tier slot.
    const battPlan = new Float32Array(HH_PER_DAY);
    for (let s = 0; s < HH_PER_DAY; s++) {
      if (dayPriceBuf[s] < cheap) battPlan[s] = chPerSlot;
    }

    // Chronological execution.
    for (let s = 0; s < HH_PER_DAY; s++) {
      const i = off + s;
      const slotPrice = dayPriceBuf[s];
      const slotLoad = demand.base[i] + demand.hp[i];
      const slotSolar = demand.solar[i];

      let solarUsed = Math.min(slotSolar, slotLoad);
      let residual = slotLoad - solarUsed;
      let surplus = slotSolar - solarUsed;

      const evTake = evPlan[s];
      const evFromSolar = Math.min(evTake, surplus);
      surplus -= evFromSolar;
      const evFromGrid = evTake - evFromSolar;

      let room = (cap - soc) / eff;
      if (room > chPerSlot) room = chPerSlot;
      const battFromSolar = Math.min(surplus, room);
      surplus -= battFromSolar;
      room -= battFromSolar;
      const battFromGrid = Math.min(battPlan[s], room);

      let discharge = 0;
      if (slotPrice >= cheap && residual > 0) {
        let avail = soc * eff;
        if (avail > dcPerSlot) avail = dcPerSlot;
        discharge = Math.min(avail, residual);
        residual -= discharge;
      }

      gridImport[i] = residual + evFromGrid + battFromGrid;
      gridExport[i] = surplus;
      evCharge[i] = evTake;
      battIn[i] = battFromSolar + battFromGrid;
      battOut[i] = discharge;

      soc += (battFromSolar + battFromGrid) * eff - discharge / eff;
      if (soc < 0) soc = 0; else if (soc > cap) soc = cap;
    }
  }

  // Costs
  let importCostP = 0, exportRevP = 0, importKwh = 0, exportKwh = 0;
  for (let i = 0; i < N; i++) {
    importCostP += gridImport[i] * prices[i];
    exportRevP += gridExport[i] * tInfo.export;
    importKwh += gridImport[i];
    exportKwh += gridExport[i];
  }
  const standingP = DAYS * tInfo.standing;
  const annualCost = (importCostP + standingP - exportRevP) / 100;

  // Monthly aggregates: cost incl. pro-rata standing.
  const monthly = new Float64Array(12);
  const monthDays = [31,28,31,30,31,30,31,31,30,31,30,31];
  for (let i = 0; i < N; i++) {
    const m = idx.month[i] - 1;
    monthly[m] += (gridImport[i] * prices[i] - gridExport[i] * tInfo.export) / 100;
  }
  for (let m = 0; m < 12; m++) {
    monthly[m] += monthDays[m] * tInfo.standing / 100;
  }

  // Spend by category — share-weighted attribution of import cost.
  let totalBase = 0, totalHp = 0, totalEv = 0;
  for (let i = 0; i < N; i++) {
    totalBase += demand.base[i];
    totalHp += demand.hp[i];
    totalEv += evCharge[i];
  }
  const totalLoad = totalBase + totalHp + totalEv || 1;
  const importCostGbp = importCostP / 100;

  let battLoss = 0;
  for (let i = 0; i < N; i++) battLoss += battIn[i] - battOut[i];

  return {
    annualCost,
    importCostGbp,
    exportRevGbp: exportRevP / 100,
    standingGbp: standingP / 100,
    importKwh,
    exportKwh,
    monthly,
    spendBase: importCostGbp * totalBase / totalLoad,
    spendHp: importCostGbp * totalHp / totalLoad,
    spendEv: importCostGbp * totalEv / totalLoad,
    batteryLossKwh: battLoss,
  };
}

// ---------- Top-level ----------

const INDEX = buildIndex();
const TEMP = outdoorTemp(INDEX);

function simulateAll(cfg, tariffNames) {
  const demand = buildDemand(INDEX, TEMP, cfg);
  const out = {};
  for (const name of tariffNames) out[name] = dispatch(demand, INDEX, cfg, name);
  // demand totals for the headline
  let totalBase = 0, totalHp = 0, totalSolar = 0;
  for (let i = 0; i < N; i++) {
    totalBase += demand.base[i];
    totalHp += demand.hp[i];
    totalSolar += demand.solar[i];
  }
  out._demand = {
    annualBaseKwh: totalBase,
    annualHpKwh: totalHp,
    annualSolarKwh: totalSolar,
    annualEvKwh: demand.evDailyKwh * 365,
  };
  return out;
}

function seasonalSwap(results, tariffNames) {
  // For each month pick the cheapest tariff.
  const winners = new Array(12);
  const bestMonth = new Float64Array(12);
  for (let m = 0; m < 12; m++) {
    let best = Infinity, bestName = null;
    for (const name of tariffNames) {
      const c = results[name].monthly[m];
      if (c < best) { best = c; bestName = name; }
    }
    winners[m] = bestName;
    bestMonth[m] = best;
  }
  const switchTotal = bestMonth.reduce((a, b) => a + b, 0);
  let bestSingleName = tariffNames[0], bestSingleCost = Infinity;
  for (const name of tariffNames) {
    if (results[name].annualCost < bestSingleCost) {
      bestSingleCost = results[name].annualCost;
      bestSingleName = name;
    }
  }
  return {
    winners, bestMonth,
    switchTotal,
    bestSingle: bestSingleName,
    bestSingleCost,
    saving: bestSingleCost - switchTotal,
  };
}

function tornado(baseCfg, tariffNames) {
  const sweeps = [
    { key: "solarKwp",         label: "Solar kWp",            low: 3,   high: 12 },
    { key: "evWeeklyMiles",    label: "EV miles / week",      low: 50,  high: 300 },
    { key: "hpGain",           label: "HP gain (kWh / HDD)",  low: 3,   high: 8 },
    { key: "batteryKwh",       label: "Battery usable kWh",   low: 5,   high: 25 },
    { key: "baseAnnualKwh",    label: "Base load kWh / yr",   low: 1500, high: 5000 },
  ];
  const rows = [];
  for (const sw of sweeps) {
    const lowCfg = Object.assign({}, baseCfg); lowCfg[sw.key] = sw.low;
    const highCfg = Object.assign({}, baseCfg); highCfg[sw.key] = sw.high;
    const lowR = simulateAll(lowCfg, tariffNames);
    const highR = simulateAll(highCfg, tariffNames);
    let lowBest = Infinity, lowWin = null, highBest = Infinity, highWin = null;
    for (const t of tariffNames) {
      if (lowR[t].annualCost < lowBest) { lowBest = lowR[t].annualCost; lowWin = t; }
      if (highR[t].annualCost < highBest) { highBest = highR[t].annualCost; highWin = t; }
    }
    rows.push({
      label: sw.label, low: sw.low, high: sw.high,
      lowBest, highBest,
      swing: highBest - lowBest,
      absSwing: Math.abs(highBest - lowBest),
      lowWin, highWin,
    });
  }
  rows.sort((a, b) => b.absSwing - a.absSwing);
  return rows;
}

// Expose
window.Tarriftool = { TARIFFS, simulateAll, seasonalSwap, tornado };
