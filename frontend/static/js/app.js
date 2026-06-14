"use strict";

const MODE_LABEL = { buy: "BUY", sell: "SELL", hold: "HOLD", self_supply: "SELF-SUPPLY" };
const BATTERY = {
  buy: "charging", sell: "discharging to grid",
  hold: "held (idle)", self_supply: "powering house loads",
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};
const eur = (v) => (v == null ? "—" : "€" + Number(v).toFixed(2));
const kwh = (v) => (v == null ? "—" : Number(v).toFixed(2) + " kWh");
const modeChip = (m) => `<span class="chip mode-${m}">${MODE_LABEL[m] || m}</span>`;
const netHtml = (net) => {
  if (net == null) return "—";
  const profit = net < 0;
  return `<span class="${profit ? "profit" : "cost"}">${eur(Math.abs(net))} ${profit ? "profit" : "cost"}</span>`;
};
const gridFlow = (sp, mode) => {
  sp = Number(sp || 0);
  if (sp > 0) return `import ${sp.toFixed(0)} W`;
  if (sp < 0) return `export ${Math.abs(sp).toFixed(0)} W`;
  return mode === "hold" ? "idle (PV covering load)" : "idle";
};

// ---- Tabs ----
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("#tab-" + t.dataset.tab).classList.add("active");
  });
});

// ---- Render: status + decision ----
function renderStatus(plan) {
  const strip = $("#status-strip");
  strip.innerHTML = "";
  if (!plan.available) {
    strip.appendChild(el("span", "muted", plan.message || "No plan yet"));
    return;
  }
  const c = plan.current || {};
  const total = plan.day_summary && plan.day_summary.total;
  const kv = (b, s) => {
    const d = el("div", "kv");
    d.innerHTML = `<b>${b}</b><small>${s}</small>`;
    return d;
  };
  strip.appendChild(kv(modeChip(c.mode), "mode"));
  strip.appendChild(kv((plan.battery_soc != null ? plan.battery_soc.toFixed(1) : "—") + "%", "battery SoC"));
  strip.appendChild(kv("€" + Number(c.price || 0).toFixed(3), "price /kWh"));
  if (total) strip.appendChild(kv(netHtml(total.net), "horizon net"));
}

function todayNet(plan) {
  const days = plan.day_summary && plan.day_summary.days;
  if (!days) return null;
  const t = days.find((d) => d.is_today);
  return t ? t.net : null;
}

let firstRender = true;

function nextSell(plan) {
  if (!plan.hours) return null;
  for (const h of plan.hours) {
    for (const s of h.slots) {
      if (s.mode === "sell" && !s.is_current) return s;
    }
  }
  return null;
}

function renderMetrics(plan) {
  const box = $("#metrics");
  box.innerHTML = "";
  if (!plan.available) return;
  const c = plan.current || {};
  const total = plan.day_summary && plan.day_summary.total;
  const ns = nextSell(plan);
  const card = (label, value) => {
    const d = el("div", "metric");
    d.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
    return d;
  };
  const tNet = todayNet(plan);
  const pvToday = plan.pv_remaining_wh != null ? (plan.pv_remaining_wh / 1000).toFixed(1) : "—";
  const pvTomorrow = plan.pv_tomorrow_wh != null ? (plan.pv_tomorrow_wh / 1000).toFixed(1) : "—";
  box.appendChild(card("Current mode", modeChip(c.mode)));
  box.appendChild(card("Battery SoC", (plan.battery_soc != null ? plan.battery_soc.toFixed(1) : "—") + "<small> %</small>"));
  box.appendChild(card("Price now", "€" + Number(c.price || 0).toFixed(3) + "<small> /kWh</small>"));
  if (tNet != null) box.appendChild(card("Today net", netHtml(tNet)));
  if (total) box.appendChild(card("Horizon net <small>(today+tom)</small>", netHtml(total.net)));
  if (ns) box.appendChild(card("Next SELL", ns.time.slice(11, 16) + " <small>€" + Number(ns.price).toFixed(3) + "</small>"));
  box.appendChild(card("Solar fcst", pvToday + "<small> kWh today · </small>" + pvTomorrow + "<small> kWh tom</small>"));
}

function renderDecision(plan) {
  const box = $("#decision");
  if (!plan.available) {
    box.innerHTML = `<div class="banner">${plan.message || "No plan published yet."}</div>`;
    return;
  }
  const c = plan.current || {};
  box.innerHTML = "";
  box.appendChild(el("h2", null, `Now: ${modeChip(c.mode)}`));
  if (c.reason) box.appendChild(el("div", "reason", c.reason));
  const row = el("div", "row");
  const item = (lbl, val) => `<div class="kv"><b>${val}</b><small>${lbl}</small></div>`;
  row.innerHTML =
    item("price", "€" + Number(c.price || 0).toFixed(4)) +
    item("battery", BATTERY[c.mode] || "—") +
    item("grid now", gridFlow(c.applied_setpoint, c.mode)) +
    item("setpoint", Number(c.applied_setpoint || 0).toFixed(0) + " W") +
    item("feed-in cap", c.limit_feed_in ? "ON (0 W)" : "off");
  box.appendChild(row);
}

function renderDaySummary(plan) {
  const box = $("#day-summary");
  box.innerHTML = "<h3 style='margin:2px 0 8px'>Day cost summary (actuals + forecast)</h3>";
  if (!plan.available || !plan.day_summary) { box.innerHTML += "<span class='muted'>—</span>"; return; }
  plan.day_summary.days.forEach((d) => {
    const r = el("div", "day-row");
    let sub = "";
    if (d.actual) {
      sub = `<div class="sub">actual so far: ${kwh(d.actual.import_kwh)} ${eur(d.actual.import_cost)} in / ${kwh(d.actual.export_kwh)} ${eur(d.actual.export_rev)} out &nbsp;•&nbsp; forecast rest: ${kwh(d.forecast.import_kwh)} ${eur(d.forecast.import_cost)} in / ${kwh(d.forecast.export_kwh)} ${eur(d.forecast.export_rev)} out</div>`;
    }
    r.innerHTML = `<span class="lbl">${d.label}${d.is_today ? " (today)" : ""}</span>
      <span>import ${kwh(d.combined.import_kwh)} (${eur(d.combined.import_cost)})</span>
      <span>export ${kwh(d.combined.export_kwh)} (${eur(d.combined.export_rev)})</span>
      <span>${netHtml(d.net)}</span>${sub}`;
    box.appendChild(r);
  });
  const t = plan.day_summary.total;
  const tr = el("div", "day-row");
  tr.innerHTML = `<span class="lbl">TOTAL</span>
    <span>import ${kwh(t.import_kwh)} (${eur(t.import_cost)})</span>
    <span>export ${kwh(t.export_kwh)} (${eur(t.export_rev)})</span>
    <span>${netHtml(t.net)}</span>`;
  box.appendChild(tr);
}

// ---- Render: hours tree ----
function timelineBar(hour) {
  const bar = el("div", "bar");
  const w = (100 / hour.slots.length).toFixed(2) + "%";
  hour.slots.forEach((s) => {
    const seg = el("span", "", "");
    seg.style.width = w;
    seg.style.background = `var(--${s.mode === "self_supply" ? "self" : s.mode})`;
    seg.title = `${s.time.slice(11, 16)} ${MODE_LABEL[s.mode] || s.mode}`;
    bar.appendChild(seg);
  });
  return bar;
}

function slotDetail(s) {
  const d = el("div", "slot-detail");
  d.innerHTML = `<div><b>${modeChip(s.mode)}</b> &nbsp; ${s.reason || ""}</div>
    <div class="grid">
      <div><small>buy / sell</small>€${Number(s.price).toFixed(4)} / €${Number(s.sell).toFixed(4)}</div>
      <div><small>SoC</small>${Number(s.soc_start).toFixed(0)}% → ${Number(s.soc_end).toFixed(0)}%</div>
      <div><small>grid energy</small>${Number(s.grid_energy).toFixed(2)} kWh</div>
      <div><small>reason code</small>${s.reason_code || "—"}</div>
    </div>`;
  return d;
}

function renderHours(plan) {
  const box = $("#hours");
  box.innerHTML = "";
  if (!plan.available) return;
  let currentRow = null;
  plan.hours.forEach((h) => {
    const row = el("div", "hour-row" + (h.is_current ? " current" : ""));
    if (h.is_current) currentRow = row;
    const nowTag = h.is_current ? '<span class="now-tag">NOW</span>' : "";
    row.innerHTML =
      `<span class="col-time"><span class="caret">▸</span>${h.label}${nowTag}</span>` +
      `<span class="col-bar"></span>` +
      `<span class="col-num">€${h.avg_price.toFixed(3)}</span>` +
      `<span class="col-num">${h.import_kwh.toFixed(2)}</span>` +
      `<span class="col-num">${h.export_kwh.toFixed(2)}</span>` +
      `<span class="col-num">${Math.round(h.soc_start)}→${Math.round(h.soc_end)}%</span>` +
      `<span class="col-num">${netHtml(h.net_cost)}</span>`;
    row.querySelector(".col-bar").appendChild(timelineBar(h));

    const slotsWrap = el("div", "slots");
    slotsWrap.style.display = "none";
    h.slots.forEach((s) => {
      const sr = el("div", "slot-row" + (s.is_current ? " current" : ""));
      const g = Number(s.grid_energy);
      const sell = Number(s.sell != null ? s.sell : s.price);
      const imp = g > 0 ? g : 0;
      const exp = g < 0 ? -g : 0;
      const slotNet = imp * Number(s.price) - exp * sell;
      sr.innerHTML =
        `<span><span class="slot-dot" style="background:var(--${s.mode === "self_supply" ? "self" : s.mode})"></span>${s.time.slice(11, 16)}</span>` +
        `<span>${MODE_LABEL[s.mode] || s.mode}</span>` +
        `<span class="col-num">€${Number(s.price).toFixed(3)}</span>` +
        `<span class="col-num">${imp > 0 ? imp.toFixed(2) : "—"}</span>` +
        `<span class="col-num">${exp > 0 ? exp.toFixed(2) : "—"}</span>` +
        `<span class="col-num">${Math.round(s.soc_start)}→${Math.round(s.soc_end)}%</span>` +
        `<span class="col-num">${netHtml(slotNet)}</span>`;
      const detail = slotDetail(s);
      detail.style.display = "none";
      sr.addEventListener("click", (e) => {
        e.stopPropagation();
        detail.style.display = detail.style.display === "none" ? "block" : "none";
      });
      slotsWrap.appendChild(sr);
      slotsWrap.appendChild(detail);
    });

    row.addEventListener("click", () => {
      const open = slotsWrap.style.display !== "none";
      slotsWrap.style.display = open ? "none" : "block";
      row.classList.toggle("open", !open);
      row.querySelector(".caret").textContent = open ? "▸" : "▾";
    });

    // Auto-expand the current hour so "now" is visible on open.
    if (h.is_current) {
      slotsWrap.style.display = "block";
      row.classList.add("open");
      row.querySelector(".caret").textContent = "▾";
    }

    box.appendChild(row);
    box.appendChild(slotsWrap);
  });

  // On first load, jump to the current slot.
  if (firstRender && currentRow) {
    setTimeout(() => currentRow.scrollIntoView({ block: "center", behavior: "smooth" }), 80);
  }
  firstRender = false;
}

// ---- Render: config (editable) ----
async function saveSetting(key, value, msgEl) {
  msgEl.textContent = "saving…";
  msgEl.style.color = "var(--muted)";
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "failed");
    msgEl.textContent = "saved ✓ (applies next cycle)";
    msgEl.style.color = "var(--sell)";
    return true;
  } catch (e) {
    msgEl.textContent = "✗ " + e.message;
    msgEl.style.color = "#f87171";
    return false;
  }
}

function startEdit(item, s) {
  const vEl = item.querySelector(".v");
  const editor = el("span", "cfg-edit");
  let input;
  if (s.type === "bool") {
    input = el("select");
    ["True", "False"].forEach((o) => input.appendChild(new Option(o, o, false, String(s.value) === o)));
  } else if (s.options) {
    input = el("select");
    s.options.forEach((o) => input.appendChild(new Option(o, o, false, s.value === o)));
  } else {
    input = el("input");
    input.type = (s.type === "int" || s.type === "float") ? "number" : "text";
    if (s.type === "float") input.step = "0.01";
    input.value = s.value;
    input.style.width = "110px";
  }
  const save = el("button", "save", "Save");
  const cancel = el("button", "cancel", "Cancel");
  const msg = el("span", "cfg-msg");
  editor.append(input, save, cancel, msg);
  vEl.replaceWith(editor);
  input.focus();

  cancel.addEventListener("click", () => loadConfig());
  save.addEventListener("click", async () => {
    // Deliberate confirm step before changing a setting on a live 16kW system.
    if (!confirm(`Set ${s.key} = ${input.value}?\nThis applies on the next optimization cycle.`)) return;
    const ok = await saveSetting(s.key, input.value, msg);
    if (ok) setTimeout(loadConfig, 700);
  });
}

function renderConfig(cfg) {
  const box = $("#config");
  box.innerHTML = "";
  const note = el("p", "cfg-note", "Click a value to edit. Changes are written to .env (the durable source of truth) and picked up by the service on its next optimization cycle.");
  box.appendChild(note);
  cfg.groups.forEach((g) => {
    const grp = el("div", "card cfg-group");
    grp.appendChild(el("h3", null, g.group));
    g.settings.forEach((s) => {
      const item = el("div", "cfg-item");
      const val = s.value === "" ? "—" : s.value;
      item.innerHTML = `<span>${s.label}</span><span class="v" title="click to edit">${val}</span><span class="d">${s.desc || ""}</span>`;
      item.querySelector(".v").addEventListener("click", () => startEdit(item, s));
      grp.appendChild(item);
    });
    box.appendChild(grp);
  });
}

function renderMeta(plan) {
  const m = $("#meta");
  if (!plan.available) { m.textContent = ""; return; }
  let txt = "plan generated " + (plan.age_seconds != null ? plan.age_seconds + "s ago" : plan.generated_at);
  if (plan.stale) txt += " — STALE (optimizer may not be running)";
  m.textContent = txt;
}

// ---- Load ----
async function loadConfig() {
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    renderConfig(cfg);
  } catch (e) {
    $("#config").innerHTML = `<span class="cost">error loading config: ${e}</span>`;
  }
}

async function refreshPlan() {
  try {
    const plan = await fetch("/api/plan").then((r) => r.json());
    renderStatus(plan);
    renderMetrics(plan);
    renderDecision(plan);
    renderDaySummary(plan);
    renderHours(plan);
    renderMeta(plan);
  } catch (e) {
    $("#status-strip").innerHTML = `<span class="cost">error loading: ${e}</span>`;
  }
}

async function load() {
  await refreshPlan();
  await loadConfig();
}

$("#refresh").addEventListener("click", load);
load();
// Auto-refresh the plan only (not config) so it can't interrupt an in-progress edit.
setInterval(refreshPlan, 30000);
