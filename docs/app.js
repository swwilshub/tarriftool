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

// ---------- Tariff rate editor ----------

const FIELD_LABELS = {
  standard: "Standard (p/kWh)",
  cheap: "Cheap window (p/kWh)",
  peak: "Peak (p/kWh)",
  standing: "Standing charge (p/day)",
  export: "Export (p/kWh)",
  mean: "Agile mean (p/kWh)",
  amp: "Agile peak amplitude (p/kWh)",
  winterPremium: "Agile winter premium (p/kWh)",
};

const TARIFFS_LS_KEY = "tarriftool_tariffs_v1";

function saveTariffOverrides() {
  const obj = {};
  for (const [k, v] of Object.entries(Tarriftool.TARIFFS)) {
    obj[k] = { ...v.rates };
  }
  try { localStorage.setItem(TARIFFS_LS_KEY, JSON.stringify(obj)); } catch (e) {}
}

function loadTariffOverrides() {
  let raw;
  try { raw = localStorage.getItem(TARIFFS_LS_KEY); } catch (e) { return; }
  if (!raw) return;
  try {
    const obj = JSON.parse(raw);
    for (const [k, rates] of Object.entries(obj)) {
      if (Tarriftool.TARIFFS[k]) {
        Object.assign(Tarriftool.TARIFFS[k].rates, rates);
      }
    }
  } catch (e) {}
}

function buildTariffEditor() {
  const wrap = document.getElementById("tariffEditor");
  wrap.innerHTML = "";
  for (const [key, info] of Object.entries(Tarriftool.TARIFFS)) {
    const block = document.createElement("div");
    block.className = "tariff-editor-block";
    block.style.borderLeftColor = info.color;
    const title = document.createElement("div");
    title.className = "tariff-editor-title";
    title.innerHTML = `<span class="swatch" style="background:${info.color}"></span>${info.label}`;
    block.appendChild(title);
    for (const field of info.editable) {
      const row = document.createElement("div");
      row.className = "tariff-editor-row";
      const lbl = document.createElement("label");
      lbl.textContent = FIELD_LABELS[field] || field;
      const inp = document.createElement("input");
      inp.type = "number";
      inp.step = "0.01";
      inp.value = info.rates[field];
      inp.dataset.tariff = key;
      inp.dataset.field = field;
      inp.addEventListener("input", onTariffRateInput);
      row.appendChild(lbl);
      row.appendChild(inp);
      block.appendChild(row);
    }
    wrap.appendChild(block);
  }
}

function onTariffRateInput(e) {
  const v = parseFloat(e.target.value);
  if (isNaN(v)) return;
  const { tariff, field } = e.target.dataset;
  Tarriftool.TARIFFS[tariff].rates[field] = v;
  saveTariffOverrides();
  scheduleRefresh();
}

function onResetTariffs() {
  Tarriftool.resetTariffsToDefaults();
  try { localStorage.removeItem(TARIFFS_LS_KEY); } catch (e) {}
  buildTariffEditor();  // re-render inputs with default values
  scheduleRefresh();
}

// ---------- Charts ----------

let annualChart, monthlyChart, tornadoChart, cumChart;
let lastResults = null;
let lastTariffNames = [];
let currentDay = 0;
let playing = false;
let playRaf = null;
let playPrevTs = 0;

// `DAYS` is declared in model.js (shared global on the page).
const MONTH_DAYS = [31,28,31,30,31,30,31,31,30,31,30,31];

function dayToDate(day0) {
  // day0 is 0-indexed day of year, non-leap
  let d = day0;
  for (let m = 0; m < 12; m++) {
    if (d < MONTH_DAYS[m]) {
      return `${d + 1} ${MONTH_NAMES[m]}`;
    }
    d -= MONTH_DAYS[m];
  }
  return "31 Dec";
}

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

  // Cumulative cost curve — runs across all 365 days.
  cumChart = new Chart(document.getElementById("cumChart"), {
    type: "line",
    data: { labels: Array.from({length: DAYS}, (_, i) => i + 1), datasets: [] },
    options: {
      ...common,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        ...common.plugins,
        tooltip: {
          callbacks: {
            title: (items) => items.length ? `Day ${items[0].label} — ${dayToDate(parseInt(items[0].label,10) - 1)}` : "",
            label: (ctx) => `${ctx.dataset.label}: ${fmtGbp(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: "#8a93a6",
            callback: function(value) {
              const day = parseInt(this.getLabelForValue(value), 10);
              // show first of month
              let acc = 1;
              for (let m = 0; m < 12; m++) {
                if (day === acc) return MONTH_NAMES[m];
                acc += MONTH_DAYS[m];
              }
              return "";
            },
            autoSkip: false, maxRotation: 0,
          },
          grid: { color: "#2a3040" },
        },
        y: { ticks: { color: "#8a93a6", callback: (v) => fmtGbp(v) }, grid: { color: "#2a3040" } },
      },
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

  // Cumulative chart (full curves; visibility window controlled by scrub).
  cumChart.data.datasets = tariffNames.map((k) => ({
    label: Tarriftool.TARIFFS[k].label,
    data: Array.from(results[k].cumulative),
    borderColor: Tarriftool.TARIFFS[k].color,
    backgroundColor: Tarriftool.TARIFFS[k].color + "20",
    tension: 0.1, pointRadius: 0, borderWidth: 2,
  }));
  lastResults = results;
  lastTariffNames = tariffNames;
  applyDayClip(currentDay);

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

// ---------- Year playback ----------

function applyDayClip(day) {
  if (!lastResults || !cumChart) return;
  // Reveal cumulative line up to `day` only — the rest is null so Chart.js
  // doesn't draw it. This is the "year unfolding" effect.
  for (let di = 0; di < cumChart.data.datasets.length; di++) {
    const tariff = lastTariffNames[di];
    const cum = lastResults[tariff].cumulative;
    const arr = new Array(DAYS).fill(null);
    for (let d = 0; d <= day && d < DAYS; d++) arr[d] = cum[d];
    cumChart.data.datasets[di].data = arr;
  }
  cumChart.update("none");
  renderTicker(day);
  document.getElementById("dayLabel").textContent = `Day ${day + 1} — ${dayToDate(day)}`;
  document.getElementById("dayScrub").value = day;
}

function renderTicker(day) {
  if (!lastResults) return;
  // Rank tariffs by cumulative-to-date, then show daily delta.
  const rows = lastTariffNames.map((k) => {
    const r = lastResults[k];
    const total = r.cumulative[day];
    const delta = r.daily[day];
    return { name: k, total, delta, label: Tarriftool.TARIFFS[k].label, color: Tarriftool.TARIFFS[k].color };
  }).sort((a, b) => a.total - b.total);
  const leadName = rows[0].name;
  const wrap = document.getElementById("cumTicker");
  wrap.innerHTML = "";
  for (const r of rows) {
    const div = document.createElement("div");
    div.className = "ticker-row" + (r.name === leadName ? " leading" : "");
    div.style.borderLeftColor = r.color;
    const deltaSign = r.delta >= 0 ? "+" : "−";
    div.innerHTML = `<div class="name">${r.label}</div>
                     <div class="total">${fmtGbp(r.total)}</div>
                     <div class="delta">today ${deltaSign}${fmtGbp(Math.abs(r.delta))}</div>`;
    wrap.appendChild(div);
  }
}

function setPlaying(p) {
  playing = p;
  const btn = document.getElementById("playBtn");
  btn.textContent = p ? "Pause" : "Play";
  btn.classList.toggle("playing", p);
  if (p) {
    playPrevTs = performance.now();
    playRaf = requestAnimationFrame(tickPlayback);
  } else if (playRaf) {
    cancelAnimationFrame(playRaf);
    playRaf = null;
  }
}

function tickPlayback(ts) {
  if (!playing) return;
  const dt = (ts - playPrevTs) / 1000;
  playPrevTs = ts;
  const speed = parseFloat(document.getElementById("speedSel").value);
  currentDay += dt * speed;
  if (currentDay >= DAYS) {
    currentDay = DAYS - 1;
    applyDayClip(Math.floor(currentDay));
    setPlaying(false);
    return;
  }
  applyDayClip(Math.floor(currentDay));
  playRaf = requestAnimationFrame(tickPlayback);
}

function initPlayback() {
  document.getElementById("playBtn").addEventListener("click", () => {
    if (currentDay >= DAYS - 1) currentDay = 0;
    setPlaying(!playing);
  });
  document.getElementById("resetBtn").addEventListener("click", () => {
    setPlaying(false);
    currentDay = 0;
    applyDayClip(0);
  });
  document.getElementById("dayScrub").addEventListener("input", (e) => {
    setPlaying(false);
    currentDay = parseInt(e.target.value, 10);
    applyDayClip(currentDay);
  });
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
  loadTariffOverrides();
  buildTariffToggles();
  buildTariffEditor();
  document.getElementById("resetTariffsBtn").addEventListener("click", onResetTariffs);
  makeCharts();
  initPlayback();
  for (const f of FIELDS) {
    document.getElementById(f).addEventListener("input", scheduleRefresh);
  }
  scheduleRefresh();
  // Start the year at day 0 with the reveal already applied.
  applyDayClip(0);
}

window.addEventListener("DOMContentLoaded", init);
