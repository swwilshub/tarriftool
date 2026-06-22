"use strict";

const MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

const FIELDS = [
  "batteryKwh", "batteryKw", "batteryRte",
  "hpRatedKw", "scop", "hpGain", "hpBaseTempC",
  "baseAnnualKwh",
  "solarKwp", "solarYield",
  "evWeeklyMiles", "evMiPerKwh", "evChargeKw",
];

function readCfg() {
  const cfg = {};
  for (const f of FIELDS) {
    const el = document.getElementById(f);
    cfg[f] = parseFloat(el.value);
    const vEl = document.getElementById("v-" + f);
    if (vEl) {
      let v = cfg[f];
      if (f === "batteryRte") v = Math.round(v * 100);
      else if (f === "hpGain" || f === "scop" || f === "evMiPerKwh") v = v.toFixed(1);
      else if (f === "hpBaseTempC") v = v.toFixed(1);
      vEl.textContent = v;
    }
  }
  return cfg;
}

function fmtGbp(v) {
  const sign = v < 0 ? "−" : "";
  return sign + "£" + Math.abs(v).toLocaleString("en-GB", { maximumFractionDigits: 0 });
}

function fmtKwh(v) {
  return Math.round(v).toLocaleString("en-GB") + " kWh";
}

// ---------- Tariff toggles ----------

const enabledTariffs = new Set(Object.keys(Tarriftool.TARIFFS));

function buildTariffToggles() {
  const wrap = document.getElementById("tariffToggles");
  wrap.innerHTML = "";
  for (const [key, info] of Object.entries(Tarriftool.TARIFFS)) {
    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = enabledTariffs.has(key);
    cb.addEventListener("change", () => {
      if (cb.checked) enabledTariffs.add(key); else enabledTariffs.delete(key);
      scheduleRefresh();
    });
    lbl.appendChild(cb);
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = info.color;
    lbl.appendChild(sw);
    lbl.appendChild(document.createTextNode(info.label));
    wrap.appendChild(lbl);
  }
}

// ---------- Charts ----------

let annualChart, monthlyChart, tornadoChart;

function makeCharts() {
  const common = {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: "#cfd3df" } },
      tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label || ctx.label}: ${fmtGbp(ctx.parsed.y ?? ctx.parsed.x ?? ctx.parsed)}` } },
    },
    scales: {
      x: { ticks: { color: "#8a93a6" }, grid: { color: "#2a3040" } },
      y: { ticks: { color: "#8a93a6", callback: (v) => fmtGbp(v) }, grid: { color: "#2a3040" } },
    },
  };

  annualChart = new Chart(document.getElementById("annualChart"), {
    type: "bar",
    data: { labels: [], datasets: [{ label: "Annual cost", data: [], backgroundColor: [] }] },
    options: { ...common, plugins: { ...common.plugins, legend: { display: false } } },
  });

  monthlyChart = new Chart(document.getElementById("monthlyChart"), {
    type: "line",
    data: { labels: MONTH_NAMES, datasets: [] },
    options: {
      ...common,
      scales: { ...common.scales, y: { ...common.scales.y, ticks: { color: "#8a93a6", callback: (v) => fmtGbp(v) } } },
    },
  });

  tornadoChart = new Chart(document.getElementById("tornadoChart"), {
    type: "bar",
    data: { labels: [], datasets: [{ label: "£ swing (low → high)", data: [], backgroundColor: [] }] },
    options: {
      ...common,
      indexAxis: "y",
      plugins: { ...common.plugins, legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8a93a6", callback: (v) => fmtGbp(v) }, grid: { color: "#2a3040" } },
        y: { ticks: { color: "#cfd3df" }, grid: { color: "#2a3040" } },
      },
    },
  });
}

// ---------- Render ----------

function renderResults(cfg) {
  const tariffNames = Array.from(enabledTariffs);
  if (tariffNames.length === 0) return;

  const t0 = performance.now();
  const results = Tarriftool.simulateAll(cfg, tariffNames);
  const swap = Tarriftool.seasonalSwap(results, tariffNames);
  const tdf = Tarriftool.tornado(cfg, tariffNames);
  const t1 = performance.now();
  console.debug(`simulate+swap+tornado: ${(t1 - t0).toFixed(0)}ms`);

  // Headline
  const bestInfo = Tarriftool.TARIFFS[swap.bestSingle];
  document.getElementById("hd-best").textContent = bestInfo.label;
  document.getElementById("hd-best").className = "value " + (swap.bestSingleCost < 0 ? "positive" : "");
  document.getElementById("hd-bestCost").textContent =
    swap.bestSingleCost < 0 ? `${fmtGbp(swap.bestSingleCost)} (you're a net exporter)` : `${fmtGbp(swap.bestSingleCost)} / year`;

  document.getElementById("hd-switch").textContent = fmtGbp(swap.switchTotal);
  const savingEl = document.getElementById("hd-saving");
  if (swap.saving > 1) {
    savingEl.textContent = `Saves ${fmtGbp(swap.saving)} / yr vs best single tariff`;
    savingEl.style.color = "var(--accent)";
  } else {
    savingEl.textContent = "No benefit — one tariff wins all year";
    savingEl.style.color = "var(--muted)";
  }

  const best = results[swap.bestSingle];
  document.getElementById("hd-import").textContent = fmtKwh(best.importKwh);
  document.getElementById("hd-export").textContent = `Export: ${fmtKwh(best.exportKwh)}`;

  const d = results._demand;
  document.getElementById("hd-demand").innerHTML =
    `Base ${fmtKwh(d.annualBaseKwh)}<br>HP ${fmtKwh(d.annualHpKwh)}<br>EV ${fmtKwh(d.annualEvKwh)}<br>Solar ${fmtKwh(d.annualSolarKwh)}`;

  // Annual bar chart
  const ranked = tariffNames.slice().sort((a, b) => results[a].annualCost - results[b].annualCost);
  annualChart.data.labels = ranked.map((k) => Tarriftool.TARIFFS[k].label);
  annualChart.data.datasets[0].data = ranked.map((k) => results[k].annualCost);
  annualChart.data.datasets[0].backgroundColor = ranked.map((k) => Tarriftool.TARIFFS[k].color);
  annualChart.update("none");

  // Monthly line chart
  monthlyChart.data.datasets = tariffNames.map((k) => ({
    label: Tarriftool.TARIFFS[k].label,
    data: Array.from(results[k].monthly),
    borderColor: Tarriftool.TARIFFS[k].color,
    backgroundColor: Tarriftool.TARIFFS[k].color + "33",
    tension: 0.25, pointRadius: 3,
  }));
  monthlyChart.update("none");

  // Seasonal schedule strip
  const strip = document.getElementById("scheduleStrip");
  strip.innerHTML = "";
  for (let m = 0; m < 12; m++) {
    const div = document.createElement("div");
    div.className = "m";
    const w = swap.winners[m];
    div.innerHTML = `${MONTH_NAMES[m]}<br><span class="name" style="color:${Tarriftool.TARIFFS[w].color}">${Tarriftool.TARIFFS[w].label.split(" ")[0]}</span>`;
    strip.appendChild(div);
  }
  document.getElementById("scheduleNote").textContent =
    swap.saving > 1
      ? `Switching tariff month-by-month saves ${fmtGbp(swap.saving)}/yr versus staying on ${bestInfo.label} all year.`
      : `Per the current parameters, ${bestInfo.label} wins every month — no seasonal swap is worth the hassle.`;

  // Monthly winner table
  const tbl = document.getElementById("monthlyTable");
  let html = "<thead><tr><th>Month</th>";
  for (const k of tariffNames) html += `<th>${Tarriftool.TARIFFS[k].label}</th>`;
  html += "<th>Winner</th></tr></thead><tbody>";
  for (let m = 0; m < 12; m++) {
    html += `<tr><td>${MONTH_NAMES[m]}</td>`;
    let bestCost = Infinity;
    for (const k of tariffNames) bestCost = Math.min(bestCost, results[k].monthly[m]);
    for (const k of tariffNames) {
      const c = results[k].monthly[m];
      const cls = Math.abs(c - bestCost) < 1e-6 ? " class=\"cheapest\"" : "";
      html += `<td${cls}>${fmtGbp(c)}</td>`;
    }
    html += `<td class="winner">${Tarriftool.TARIFFS[swap.winners[m]].label.split(" ")[0]}</td></tr>`;
  }
  html += "</tbody>";
  tbl.innerHTML = html;

  // Tornado
  tornadoChart.data.labels = tdf.map((r) => `${r.label}  (${r.low}→${r.high})`);
  tornadoChart.data.datasets[0].data = tdf.map((r) => r.swing);
  tornadoChart.data.datasets[0].backgroundColor = tdf.map((r) => r.swing < 0 ? "#4f9" : "#fa3");
  tornadoChart.update("none");

  // Breakdown table
  const bt = document.getElementById("breakdownTable");
  let bh = "<thead><tr><th>Tariff</th><th>Annual £</th><th>Import £</th><th>Export £</th><th>Standing £</th><th>Base £</th><th>HP £</th><th>EV £</th><th>Batt losses</th></tr></thead><tbody>";
  for (const k of ranked) {
    const r = results[k];
    bh += `<tr><td>${Tarriftool.TARIFFS[k].label}</td>`;
    bh += `<td>${fmtGbp(r.annualCost)}</td>`;
    bh += `<td>${fmtGbp(r.importCostGbp)}</td>`;
    bh += `<td>${fmtGbp(-r.exportRevGbp)}</td>`;
    bh += `<td>${fmtGbp(r.standingGbp)}</td>`;
    bh += `<td>${fmtGbp(r.spendBase)}</td>`;
    bh += `<td>${fmtGbp(r.spendHp)}</td>`;
    bh += `<td>${fmtGbp(r.spendEv)}</td>`;
    bh += `<td>${fmtKwh(r.batteryLossKwh)}</td></tr>`;
  }
  bh += "</tbody>";
  bt.innerHTML = bh;
}

// ---------- Debounced refresh ----------

let pending = false;
function scheduleRefresh() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    const cfg = readCfg();
    renderResults(cfg);
  });
}

function init() {
  buildTariffToggles();
  makeCharts();
  for (const f of FIELDS) {
    document.getElementById(f).addEventListener("input", scheduleRefresh);
  }
  scheduleRefresh();
}

window.addEventListener("DOMContentLoaded", init);
