"use strict";

// Canonical control action (what we COMMAND): IDLE / RETAIN / BUY / SELL.
// One label everywhere so the console, UI and history agree.
const CONTROL_CLASS = { IDLE: "mode-idle", RETAIN: "mode-retain", BUY: "mode-buy", SELL: "mode-sell" };
const CONTROL_COLORVAR = { IDLE: "idle", RETAIN: "retain", BUY: "buy", SELL: "sell" };
const CONTROL_BATTERY = {
  IDLE: "Victron-managed (self-consume / charge surplus PV / export when full)",
  RETAIN: "held — house load covered from the grid",
  BUY: "charging from the grid",
  SELL: "discharging to the grid",
};
// Control action of a plan slot / current object (defaults to IDLE).
const caOf = (o) => (o && o.control_action) ? String(o.control_action).toUpperCase() : "IDLE";
const isIdle = (o) => caOf(o) === "IDLE";  // IDLE flow is projected, not committed
const slotColorVar = (s) => CONTROL_COLORVAR[caOf(s)] || "idle";
const chipFor = (cact) => `<span class="chip ${CONTROL_CLASS[cact] || "mode-idle"}">${cact}</span>`;

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};
const eur = (v) => (v == null ? "—" : "€" + Number(v).toFixed(2));
const kwh = (v) => (v == null ? "—" : Number(v).toFixed(2) + " kWh");
const netHtml = (net) => {
  if (net == null) return "—";
  const profit = net < 0;
  return `<span class="${profit ? "profit" : "cost"}">${eur(Math.abs(net))} ${profit ? "profit" : "cost"}</span>`;
};
// Signed € for the header chips: a +/green profit or −/red loss, no "profit"/"cost"
// word (the colour and sign carry it). `profit` is profit-positive.
const netSigned = (profit) => {
  if (profit == null) return "—";
  const v = Number(profit);
  const cls = v >= 0 ? "profit" : "cost";
  return `<span class="${cls}">${v >= 0 ? "+" : "−"}€${Math.abs(v).toFixed(2)}</span>`;
};
// Signed grid flow (+ import / − export), plain. Production/consumption are
// always positive; show "—" (muted) when ~0.
const fmtGrid = (v) => {
  if (v == null) return "—";
  v = Number(v);
  if (Math.abs(v) < 0.005) return '<span class="muted">0.00</span>';
  return `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}`;
};
const prodCell = (v) => (v == null || Math.abs(Number(v)) < 0.005) ? '<span class="muted">—</span>' : Number(v).toFixed(2);
const consCell = (v) => (v == null || Math.abs(Number(v)) < 0.005) ? '<span class="muted">—</span>' : Number(v).toFixed(2);
const socPair = (a, b) => (a == null || b == null)
  ? '<span class="muted">—</span>'
  : `${Math.round(a)}→${Math.round(b)}%`;
// Consistent power formatting: watts under 1 kW, kW at or above.
const fmtPower = (w) => {
  if (w == null) return "—";
  const a = Math.abs(w);
  if (a < 50) return "0 W";
  return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
};

let lastPlan = null;
let lastLive = null;
let expandedHours = new Set();   // hour keys the user has expanded (survive refreshes)
let lastHoursGen = null;          // generated_at of the last tree we built
let lastCurrentHourKey = null;
const MOBILE_MQ = window.matchMedia("(max-width: 680px)");
const isMobileLayout = () => MOBILE_MQ.matches;

const liveOn = () => !!(lastLive && lastLive.connected);
// Prefer a live MQTT value when the feed is connected and the value is present.
function pick(planVal, liveVal) {
  return (liveOn() && liveVal != null) ? liveVal : planVal;
}
function gridNowText() {
  if (!liveOn() || lastLive.grid_w == null) return "—";
  const g = lastLive.grid_w;
  if (g > 50) return `importing ${fmtPower(g)}`;
  if (g < -50) return `exporting ${fmtPower(-g)}`;
  return "idle (~0)";
}

// ---- Tabs ----
function activateTab(tabName) {
  const panel = $("#tab-" + tabName);
  if (!panel) return;
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
  const tab = document.querySelector(`.tab[data-tab="${tabName}"]`);
  if (tab) tab.classList.add("active");
  panel.classList.add("active");
  syncMobileNavState();
}

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => activateTab(t.dataset.tab));
});

let firstRender = true;

// ---- Top-level app views ----
const APP_VIEWS = ["ess", "battery", "live"];
function setAppView(viewName) {
  const view = APP_VIEWS.includes(viewName) ? viewName : "ess";
  document.querySelectorAll(".app-view").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".app-nav-link").forEach((x) => x.classList.remove("active"));
  const panel = $(`#${view}-view`);
  const link = document.querySelector(`.app-nav-link[data-app-view="${view}"]`);
  if (panel) panel.classList.add("active");
  if (link) link.classList.add("active");
  syncMobileNavState();
}

function appViewFromHash() {
  const v = (window.location.hash || "").replace("#", "");
  return APP_VIEWS.includes(v) ? v : "ess";
}

document.querySelectorAll(".app-nav-link").forEach((link) => {
  link.addEventListener("click", () => setAppView(link.dataset.appView));
});
window.addEventListener("hashchange", () => setAppView(appViewFromHash()));
setAppView(appViewFromHash());

// ---- Mobile chrome (guarded; hidden/no-op on desktop) ----
function currentAppViewName() {
  const active = document.querySelector(".app-view.active");
  return active ? active.id.replace(/-view$/, "") : "ess";
}

function currentTabName() {
  const active = document.querySelector(".tab-panel.active");
  return active ? active.id.replace(/^tab-/, "") : "schedule";
}

function closeMobileMenu() {
  const menu = $("#mobile-menu");
  const toggle = document.querySelector("[data-mobile-menu-toggle]");
  if (menu) menu.hidden = true;
  if (toggle) toggle.setAttribute("aria-expanded", "false");
  syncMobileNavState();
}

function openMobileMenu() {
  if (!isMobileLayout()) return;
  const menu = $("#mobile-menu");
  const toggle = document.querySelector("[data-mobile-menu-toggle]");
  if (menu) menu.hidden = false;
  if (toggle) toggle.setAttribute("aria-expanded", "true");
  syncMobileNavState();
}

function syncMobileNavState() {
  const activeTab = currentTabName();
  const activeView = currentAppViewName();
  const menu = $("#mobile-menu");
  const menuOpen = !!(menu && !menu.hidden);
  document.body.dataset.mobileTab = activeTab;
  document.body.dataset.mobileAppView = activeView;
  document.querySelectorAll(".mobile-nav-item[data-mobile-tab]").forEach((btn) => {
    btn.classList.toggle("active", activeView === "ess" && btn.dataset.mobileTab === activeTab);
  });
  const menuToggle = document.querySelector("[data-mobile-menu-toggle]");
  if (menuToggle) {
    const menuOwnsCurrent = activeView !== "ess" || activeTab === "victron" || activeTab === "config";
    menuToggle.classList.toggle("active", menuOpen || menuOwnsCurrent);
  }
  document.querySelectorAll("[data-mobile-tab]").forEach((btn) => {
    btn.classList.toggle("active", activeView === "ess" && btn.dataset.mobileTab === activeTab);
  });
  document.querySelectorAll("[data-mobile-app-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mobileAppView === activeView);
  });
}

function applyMobileChrome() {
  const on = isMobileLayout();
  const bottomNav = document.querySelector(".mobile-bottom-nav");
  const keyStat = $("#mobile-key-stat");
  if (bottomNav) bottomNav.hidden = !on;
  if (keyStat) keyStat.hidden = !on;
  if (!on) closeMobileMenu();
  if (on && lastPlan) {
    renderStatus(lastPlan);
    loadConfig();
  }
  syncMobileNavState();
}

function goHome(e) {
  if (e && e.target.closest(".app-nav-link")) return;
  if (e) {
    e.preventDefault();
    e.stopPropagation();
  }
  setAppView("ess");
  activateTab("schedule");
  closeMobileMenu();
  if (window.location.hash) {
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function initMobileChrome() {
  const home = document.querySelector("[data-home]");
  if (home) home.addEventListener("click", goHome);

  document.addEventListener("click", (e) => {
    if (!isMobileLayout()) return;
    const menu = $("#mobile-menu");

    if (e.target.closest("[data-mobile-menu-toggle]")) {
      e.preventDefault();
      if (menu && !menu.hidden) closeMobileMenu();
      else openMobileMenu();
      return;
    }

    if (e.target.closest("[data-mobile-menu-close]") || e.target === menu) {
      e.preventDefault();
      closeMobileMenu();
      return;
    }

    const tabBtn = e.target.closest("button[data-mobile-tab]");
    if (tabBtn) {
      e.preventDefault();
      setAppView("ess");
      activateTab(tabBtn.dataset.mobileTab);
      closeMobileMenu();
      return;
    }

    const appViewBtn = e.target.closest("button[data-mobile-app-view]");
    if (appViewBtn) {
      e.preventDefault();
      setAppView(appViewBtn.dataset.mobileAppView);
      closeMobileMenu();
      return;
    }

    if (e.target.closest("[data-mobile-home]")) {
      goHome(e);
    }
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMobileMenu();
  });
  MOBILE_MQ.addEventListener("change", applyMobileChrome);
  applyMobileChrome();
}

function todayNet(plan) {
  plan = planWithLiveActuals(plan);
  if (!plan) return null;
  const days = plan.day_summary && plan.day_summary.days;
  if (!days) return null;
  const t = days.find((d) => d.is_today);
  return t ? t.net : null;
}

function liveTodayActuals() {
  if (!liveOn()) return null;
  const vals = {
    import_kwh: lastLive.day_import_kwh,
    import_cost: lastLive.day_import_cost,
    export_kwh: lastLive.day_export_kwh,
    export_rev: lastLive.day_export_reward,
  };
  return Object.values(vals).every((v) => v != null) ? vals : null;
}

function planWithLiveActuals(plan) {
  const actual = liveTodayActuals();
  if (!plan || !plan.available || !actual || !plan.day_summary) return plan;

  const keys = ["import_kwh", "import_cost", "export_kwh", "export_rev", "idle_imp_cost", "idle_exp_rev"];
  let changed = false;
  const days = (plan.day_summary.days || []).map((d) => {
    if (!d.is_today) return d;
    changed = true;
    const cleanActual = {
      import_kwh: Number(actual.import_kwh || 0),
      import_cost: Number(actual.import_cost || 0),
      export_kwh: Number(actual.export_kwh || 0),
      export_rev: Number(actual.export_rev || 0),
    };
    const forecast = d.forecast || {};
    const combined = {};
    keys.forEach((k) => {
      combined[k] = Number(forecast[k] || 0) + Number(cleanActual[k] || 0);
    });
    return {
      ...d,
      actual: cleanActual,
      combined,
      net: combined.import_cost - combined.export_rev,
      projected_idle_net: combined.idle_exp_rev - combined.idle_imp_cost,
    };
  });
  if (!changed) return plan;

  const total = keys.reduce((acc, k) => ({ ...acc, [k]: 0 }), {});
  days.forEach((d) => {
    const combined = d.combined || {};
    keys.forEach((k) => { total[k] += Number(combined[k] || 0); });
  });
  total.net = total.import_cost - total.export_rev;
  total.projected_idle_net = total.idle_exp_rev - total.idle_imp_cost;

  return { ...plan, day_summary: { ...plan.day_summary, days, total } };
}

function nextSell(plan) {
  if (!plan.hours) return null;
  for (const h of plan.hours) {
    for (const s of h.slots) {
      if (caOf(s) === "SELL" && !s.is_current) return s;
    }
  }
  return null;
}

// Current control action: prefer the live feed when connected, else the plan.
const currentCA = (c) =>
  (liveOn() && lastLive.control_action) ? String(lastLive.control_action).toUpperCase() : caOf(c);

function updateMobileKeyStat(cact, soc, message) {
  const stat = $("#mobile-key-stat");
  if (!stat) return;
  if (message) {
    stat.textContent = message;
    return;
  }
  const socText = soc != null ? `${Number(soc).toFixed(1)}%` : "SoC --";
  stat.innerHTML = `${chipFor(cact || "IDLE")}<span>${socText}</span>`;
}

// ---- Render: overview (status, metrics, solar, decision) ----
function renderStatus(plan) {
  plan = planWithLiveActuals(plan);
  const strip = $("#status-strip");
  strip.innerHTML = "";
  if (!plan.available) {
    updateMobileKeyStat(null, null, plan.message || "No plan yet");
    strip.appendChild(el("span", "muted", plan.message || "No plan yet"));
    return;
  }
  const c = plan.current || {};
  const tNet = todayNet(plan);
  const soc = pick(plan.battery_soc, lastLive && lastLive.soc);
  // Price comes from the 15-min plan (the live MQTT topic is hourly), so the
  // header matches the Now card and the slot table.
  const price = c.price;
  const kv = (b, s, cls) => {
    const d = el("div", ["kv", cls].filter(Boolean).join(" "));
    d.innerHTML = `<b>${b}</b><small>${s}</small>`;
    return d;
  };
  updateMobileKeyStat(currentCA(c), soc);
  strip.appendChild(kv(chipFor(currentCA(c)), "action"));
  strip.appendChild(kv((soc != null ? Number(soc).toFixed(1) : "—") + "%", "battery SoC"));
  strip.appendChild(kv("€" + Number(price || 0).toFixed(3), "price /kWh", "status-price"));
  // Today + Month header chips: signed €, green (+) profit / red (−) loss, no word.
  if (tNet != null) strip.appendChild(kv(netSigned(-tNet), "Today"));   // tNet is cost-positive
  const mtd = plan.mtd_net;
  if (mtd && mtd.net != null) {
    const chip = kv(netSigned(mtd.net), "Month");
    if (mtd.export_reward != null && mtd.import_cost != null) {
      chip.title = `Month-to-date · export €${Number(mtd.export_reward).toFixed(2)} − ` +
        `import €${Number(mtd.import_cost).toFixed(2)} = €${Number(mtd.net).toFixed(2)} over ${mtd.days} days`;
    }
    strip.appendChild(chip);
  }
}

function renderMetrics(plan) {
  plan = planWithLiveActuals(plan);
  const box = $("#metrics");
  box.innerHTML = "";
  if (!plan.available) return;
  const c = plan.current || {};
  const total = plan.day_summary && plan.day_summary.total;
  const ns = nextSell(plan);
  const card = (label, value) => { const d = el("div", "metric"); d.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`; return d; };
  const tNet = todayNet(plan);
  const soc = pick(plan.battery_soc, lastLive && lastLive.soc);
  const price = c.price;  // 15-min plan price (live topic is hourly)
  box.appendChild(card("Current action", chipFor(currentCA(c))));
  box.appendChild(card("Battery SoC", (soc != null ? Number(soc).toFixed(1) : "—") + "<small> %</small>"));
  box.appendChild(card("Price now", "€" + Number(price || 0).toFixed(3) + "<small> /kWh</small>"));
  if (tNet != null) box.appendChild(card("Today net", netHtml(tNet)));
  if (total) box.appendChild(card("Horizon net (2 Day)", netHtml(total.net)));
  if (ns) box.appendChild(card("Next SELL", ns.time.slice(11, 16) + " <small>€" + Number(ns.price).toFixed(3) + "</small>"));
}

function renderSolar(plan) {
  const box = $("#solar");
  if (!plan.available) { box.innerHTML = `<div class="label">Solar forecast</div><div class="big">—</div>`; return; }
  // "Remaining today" can go negative when actual production already exceeded
  // the day's forecast — clamp to 0 (forecast met).
  const today = plan.pv_remaining_wh != null ? Math.max(0, plan.pv_remaining_wh / 1000).toFixed(1) : "—";
  const totalToday = plan.pv_today_total_kwh != null ? Number(plan.pv_today_total_kwh).toFixed(1) : null;
  const tom = plan.pv_tomorrow_wh != null ? (plan.pv_tomorrow_wh / 1000).toFixed(1) : "—";
  const pvnow = (liveOn() && lastLive.pv_w != null) ? fmtPower(lastLive.pv_w) : null;

  // Today / tomorrow lowest & highest buy price, from the retained Tibber MQTT
  // topics via the live feed. Tomorrow appears once Tibber publishes it (~13:00);
  // until then its cost reads non-numeric and the row is hidden.
  const L = lastLive || {};
  const eur3 = (v) => "€" + Number(v).toFixed(3);
  const hhmm = (s) => (typeof s === "string" && s.length >= 5) ? s.slice(0, 5) : "";
  const priceRow = (label, lo, loAt, hi, hiAt) => (lo != null && hi != null)
    ? `<div class="pl-row"><span class="pl">${label}</span> low <b>${eur3(lo)}</b>`
      + `${loAt ? ` <span class="plt">${hhmm(loAt)}</span>` : ""} &nbsp;·&nbsp; high <b>${eur3(hi)}</b>`
      + `${hiAt ? ` <span class="plt">${hhmm(hiAt)}</span>` : ""}</div>`
    : "";
  let pricesHtml = priceRow("Today", L.price_today_low, L.price_today_low_at, L.price_today_high, L.price_today_high_at)
    + priceRow("Tomorrow", L.price_tom_low, L.price_tom_low_at, L.price_tom_high, L.price_tom_high_at);

  box.innerHTML = `<div class="label">Solar forecast</div>
    <div class="big">${today}<small style="font-size:13px;color:var(--muted)"> kWh${totalToday ? ` (of ${totalToday})` : ""} remaining.</small></div>
    <div class="sub">${tom} kWh forecast tomorrow${pvnow ? ` &nbsp;·&nbsp; producing ${pvnow} now` : ""}</div>
    ${pricesHtml ? `<div class="solar-prices">${pricesHtml}</div>` : ""}`;
}

function renderDecision(plan) {
  const box = $("#decision");
  if (!plan.available) { box.innerHTML = `<div class="banner">${plan.message || "No plan published yet."}</div>`; return; }
  const c = plan.current || {};
  const cact = currentCA(c);
  const reason = pick(c.reason, lastLive && lastLive.reason);
  const price = c.price;  // 15-min plan price
  const sp = Number(pick(c.applied_setpoint, lastLive && lastLive.setpoint_w) || 0);

  let control;
  if (cact === "BUY" && Math.abs(sp) < 50) control = "Charge schedule active";
  else if (sp < -50) control = `export ${fmtPower(-sp)}`;
  else if (sp > 50) control = `import ${fmtPower(sp)}`;
  else control = "idle (0 W)";

  const feedIn = (liveOn() && lastLive.feed_in_state)
    ? (String(lastLive.feed_in_state).startsWith("limited") ? "ON (0 W)" : "off")
    : (c.limit_feed_in ? "ON (0 W)" : "off");

  const dot = liveOn()
    ? '<span class="live-dot live-on" title="live MQTT connected"></span>'
    : '<span class="live-dot live-off" title="live feed offline — showing plan values"></span>';

  box.innerHTML = "";
  box.appendChild(el("h2", null, `Now: ${chipFor(cact)} ${dot}`));
  if (reason) box.appendChild(el("div", "reason", reason));
  // Economic / control row (power flows live below).
  // In IDLE the setpoint is neutral, so describe what the battery is ACTUALLY
  // doing from the live feed rather than asserting one outcome.
  let batteryState;
  if (cact === "IDLE" && liveOn() && lastLive.batt_w != null) {
    const bw = Number(lastLive.batt_w);
    batteryState = bw > 50 ? "charging (Victron-managed)"
                 : bw < -50 ? "powering loads / exporting"
                 : "idle (Victron-managed)";
  } else {
    batteryState = CONTROL_BATTERY[cact] || "—";
  }
  // Live Victron AC setpoint straight from MQTT (negative = export, positive =
  // import). Lets you watch the actual commanded setpoint update in real time.
  const liveSetpoint = (liveOn() && lastLive.setpoint_w != null) ? fmtPower(lastLive.setpoint_w) : "—";
  const item = (lbl, val) => `<div class="kv"><b>${val}</b><small>${lbl}</small></div>`;
  const row = el("div", "row");
  row.innerHTML =
    item("Price", "€" + Number(price || 0).toFixed(4)) +
    item("Battery", batteryState) +
    item("Scheduled Charging", control) +
    item("PV Cap", feedIn) +
    item("Grid Setpoint", liveSetpoint);
  box.appendChild(row);

  // Live power flow — signed values: grid −=export/+=import, battery −=discharge/+=charge.
  if (liveOn()) {
    const f = (lbl, val) => `<div class="f"><b>${val}</b><small>${lbl}</small></div>`;
    const flow = el("div", "flow");
    flow.innerHTML =
      f("Grid Use", fmtPower(lastLive.grid_w)) +
      f("PV Generation", fmtPower(lastLive.pv_w)) +
      f("Battery use", fmtPower(lastLive.batt_w)) +
      f("AC Loads", fmtPower(lastLive.load_w));
    box.appendChild(flow);
  }
}

function renderDaySummary(plan) {
  plan = planWithLiveActuals(plan);
  const box = $("#day-summary");
  box.innerHTML = "<h3 style='margin:2px 0 12px'>Day cost summary (actuals + forecast)</h3>";
  if (!plan.available || !plan.day_summary) { box.innerHTML += "<span class='muted'>—</span>"; return; }
  // Four aligned columns: label | import | export | net. The day-row and day-sub
  // rows share the same grid template so the numbers line up vertically.
  const cells = (lbl, impKwh, impC, expKwh, expC, netCell) =>
    `<span class="lbl">${lbl}</span>` +
    `<span class="num"><span class="t">import</span> <b>${impKwh}</b> <small>${impC}</small></span>` +
    `<span class="num"><span class="t">export</span> <b>${expKwh}</b> <small>${expC}</small></span>` +
    `<span class="net">${netCell}</span>`;
  plan.day_summary.days.forEach((d) => {
    const r = el("div", "day-row");
    r.innerHTML = cells(`${d.label}${d.is_today ? " (today)" : ""}`,
      kwh(d.combined.import_kwh), eur(d.combined.import_cost),
      kwh(d.combined.export_kwh), eur(d.combined.export_rev), netHtml(d.net));
    box.appendChild(r);
    if (d.actual) {
      const actNet = d.actual.import_cost - d.actual.export_rev;
      const fcNet = d.forecast.import_cost - d.forecast.export_rev;
      const a = el("div", "day-sub");
      a.innerHTML = cells("actual so far",
        kwh(d.actual.import_kwh), eur(d.actual.import_cost),
        kwh(d.actual.export_kwh), eur(d.actual.export_rev), netHtml(actNet));
      box.appendChild(a);
      const f = el("div", "day-sub");
      f.innerHTML = cells("forecast rest",
        kwh(d.forecast.import_kwh), eur(d.forecast.import_cost),
        kwh(d.forecast.export_kwh), eur(d.forecast.export_rev), netHtml(fcNet));
      box.appendChild(f);
    }
  });
  const t = plan.day_summary.total;
  const tr = el("div", "day-row day-total");
  tr.innerHTML = cells("TOTAL",
    kwh(t.import_kwh), eur(t.import_cost),
    kwh(t.export_kwh), eur(t.export_rev), netHtml(t.net));
  box.appendChild(tr);
}

// ---- Render: hours tree ----
function timelineBar(hour) {
  const bar = el("div", "bar");
  const w = (100 / hour.slots.length).toFixed(2) + "%";
  hour.slots.forEach((s) => {
    const seg = el("span", "", "");
    seg.style.width = w;
    seg.style.background = `var(--${slotColorVar(s)})`;
    seg.title = `${s.time.slice(11, 16)} ${caOf(s)}`;
    bar.appendChild(seg);
  });
  return bar;
}

function slotDetail(s) {
  const d = el("div", "slot-detail");
  d.innerHTML = `<div><b>${chipFor(caOf(s))}</b> &nbsp; ${s.reason || ""}</div>
    <div class="grid">
      <div><small>buy / sell</small>€${Number(s.price || 0).toFixed(4)} / €${Number(s.sell || s.price || 0).toFixed(4)}</div>
      <div><small>SoC</small>${socPair(s.soc_start, s.soc_end)}</div>
      <div><small>grid (+imp/−exp)</small>${fmtGrid(s.grid_energy)} kWh</div>
      <div><small>production</small>${s.pv != null ? Number(s.pv).toFixed(2) + " kWh" : "—"}</div>
      <div><small>consumption</small>${s.load != null ? Number(s.load).toFixed(2) + " kWh" : "—"}</div>
      <div><small>reason code</small>${s.reason_code || "—"}</div>
    </div>`;
  return d;
}

// Reusable builders shared by today's tree and the previous-day tree.
function dominantHourAction(h) {
  const slots = (h && h.slots) || [];
  if (!slots.length) return "IDLE";
  const current = slots.find((s) => s.is_current);
  if (current) return caOf(current);
  const counts = { SELL: 0, BUY: 0, RETAIN: 0, IDLE: 0 };
  slots.forEach((s) => {
    const action = caOf(s);
    counts[action] = (counts[action] || 0) + 1;
  });
  return Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0] || "IDLE";
}

function decorateHourRowForMobile(row, h) {
  row.setAttribute("data-mobile-action", dominantHourAction(h));
}

function hourRowInner(h) {
  const nowTag = h.is_current ? '<span class="now-tag">NOW</span>' : "";
  return (
    `<span class="col-time"><span class="caret">▸</span>${h.label}${nowTag}</span>` +
    `<span class="col-bar"></span>` +
    `<span class="col-num">€${h.avg_price.toFixed(3)}</span>` +
    `<span class="col-num">${fmtGrid(h.grid_kwh)}</span>` +
    `<span class="col-num">${prodCell(h.production_kwh)}</span>` +
    `<span class="col-num">${consCell(h.consumption_kwh)}</span>` +
    `<span class="col-num">${socPair(h.soc_start, h.soc_end)}</span>` +
    `<span class="col-num">${netHtml(h.net_cost)}</span>`
  );
}

function makeSlotRow(s) {
  const sr = el("div", "slot-row" + (s.is_current ? " current" : ""));
  const g = Number(s.grid_energy);
  const sell = Number(s.sell != null ? s.sell : s.price);
  const imp = g > 0 ? g : 0;
  const exp = g < 0 ? -g : 0;
  const idle = isIdle(s);          // IDLE flow is projected, not committed
  const settled = !!s.settled;
  const slotNet = settled
    ? Number(s.actual_cost || 0) - Number(s.actual_reward || 0)
    : imp * Number(s.price) - exp * sell;
  const muted = (v) => `<span class='muted'>${v}</span>`;
  const gridStr = fmtGrid(g);
  sr.innerHTML =
    `<span><span class="slot-dot" style="background:var(--${slotColorVar(s)})"></span>${s.time.slice(11, 16)}</span>` +
    `<span>${caOf(s)}</span>` +
    `<span class="col-num">€${Number(s.price || 0).toFixed(3)}</span>` +
    `<span class="col-num">${idle && !settled ? muted(gridStr) : gridStr}</span>` +
    `<span class="col-num">${prodCell(s.pv)}</span>` +
    `<span class="col-num">${consCell(s.load)}</span>` +
    `<span class="col-num">${socPair(s.soc_start, s.soc_end)}</span>` +
    `<span class="col-num">${idle && !settled ? muted("projected") : netHtml(slotNet)}</span>`;
  const detail = slotDetail(s);
  detail.style.display = "none";
  sr.addEventListener("click", (e) => {
    e.stopPropagation();
    detail.style.display = detail.style.display === "none" ? "block" : "none";
  });
  return [sr, detail];
}

function renderHours(plan) {
  const box = $("#hours");
  box.innerHTML = "";
  if (!plan.available) return;
  let currentRow = null;
  const currentHour = (plan.hours || []).find((h) => h.is_current);
  const currentKey = currentHour && currentHour.key;
  if ((firstRender || currentKey !== lastCurrentHourKey) && currentKey) {
    if (lastCurrentHourKey) expandedHours.delete(lastCurrentHourKey);
    expandedHours.add(currentKey);
    lastCurrentHourKey = currentKey;
  }
  plan.hours.forEach((h) => {
    const row = el("div", "hour-row" + (h.is_current ? " current" : ""));
    if (h.is_current) currentRow = row;
    decorateHourRowForMobile(row, h);
    row.innerHTML = hourRowInner(h);
    row.querySelector(".col-bar").appendChild(timelineBar(h));

    const slotsWrap = el("div", "slots");
    slotsWrap.style.display = "none";
    h.slots.forEach((s) => {
      const [sr, detail] = makeSlotRow(s);
      slotsWrap.appendChild(sr);
      slotsWrap.appendChild(detail);
    });

    row.addEventListener("click", () => {
      const open = slotsWrap.style.display !== "none";
      slotsWrap.style.display = open ? "none" : "block";
      row.classList.toggle("open", !open);
      row.querySelector(".caret").textContent = open ? "▸" : "▾";
      if (open) expandedHours.delete(h.key); else expandedHours.add(h.key);
    });

    // On first ever render, expand the current hour so "now" is visible.
    if (h.is_current && firstRender) expandedHours.add(h.key);

    // Restore expansion the user (or first-render) chose.
    if (expandedHours.has(h.key)) {
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

// ---- Render: previous-day settled schedule (collapsed, lazy-loaded) ----
let _prevDayLoaded = false;
function renderPrevDay() {
  const box = $("#prev-day");
  if (!box || box.dataset.ready) return;     // build the collapsed header once
  box.dataset.ready = "1";
  box.innerHTML = "";

  const head = el("div", "hour-row prev-day-head");
  head.innerHTML =
    `<span class="col-time"><span class="caret">▸</span>Previous day</span>` +
    `<span class="col-bar"><span class="muted">settled actuals — click to expand</span></span>` +
    `<span class="col-num"></span><span class="col-num"></span><span class="col-num"></span>` +
    `<span class="col-num"></span><span class="col-num"></span><span class="col-num"></span>`;
  const body = el("div", "prev-day-body");
  body.style.display = "none";

  head.addEventListener("click", async () => {
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "block";
    head.classList.toggle("open", !open);
    head.querySelector(".caret").textContent = open ? "▸" : "▾";
    if (!_prevDayLoaded) {
      _prevDayLoaded = true;
      body.innerHTML = `<div class="muted prev-day-msg">Loading…</div>`;
      try {
        const d = await fetch("/api/history/day").then((r) => r.json());
        renderSettledDay(body, d);
      } catch (e) {
        _prevDayLoaded = false;               // allow a retry on next expand
        body.innerHTML = `<div class="muted prev-day-msg">Couldn't load the previous day.</div>`;
      }
    }
  });

  box.appendChild(head);
  box.appendChild(body);
}

function renderSettledDay(box, d) {
  box.innerHTML = "";
  if (!d || !d.available || !(d.hours || []).length) {
    box.innerHTML = `<div class="muted prev-day-msg">No settled history for ${(d && d.label) || "the previous day"}.</div>`;
    return;
  }
  const sum = d.summary || {};
  const cap = el("div", "prev-day-cap");
  cap.innerHTML =
    `<b>${d.label}</b> &nbsp; net ${netHtml(sum.net)} ` +
    `<span class="muted">· imported ${kwh(sum.import_kwh)} @ ${eur(sum.import_cost)} · ` +
    `exported ${kwh(sum.export_kwh)} @ ${eur(sum.export_rev)}</span>`;
  box.appendChild(cap);

  d.hours.forEach((h) => {
    const row = el("div", "hour-row");
    decorateHourRowForMobile(row, h);
    row.innerHTML = hourRowInner(h);
    row.querySelector(".col-bar").appendChild(timelineBar(h));
    const slotsWrap = el("div", "slots");
    slotsWrap.style.display = "none";
    h.slots.forEach((s) => {
      const [sr, detail] = makeSlotRow(s);
      slotsWrap.appendChild(sr);
      slotsWrap.appendChild(detail);
    });
    row.addEventListener("click", () => {
      const isOpen = slotsWrap.style.display !== "none";
      slotsWrap.style.display = isOpen ? "none" : "block";
      row.classList.toggle("open", !isOpen);
      row.querySelector(".caret").textContent = isOpen ? "▸" : "▾";
    });
    box.appendChild(row);
    box.appendChild(slotsWrap);
  });
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
  const dEl = item.querySelector(".d");
  if (dEl) dEl.style.visibility = "hidden";  // free the row so Save/Cancel are clickable
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
      if (isMobileLayout()) {
        item.innerHTML = `<span>${s.label}</span><span class="v" title="click to edit">${val}</span>` +
          `<button type="button" class="cfg-info-toggle" aria-expanded="false" aria-label="Toggle description">i</button>` +
          `<span class="d" hidden>${s.desc || ""}</span>`;
        const info = item.querySelector(".cfg-info-toggle");
        const desc = item.querySelector(".d");
        info.addEventListener("click", () => {
          const open = desc.hidden;
          desc.hidden = !open;
          item.classList.toggle("info-open", open);
          info.setAttribute("aria-expanded", String(open));
        });
      } else {
        item.innerHTML = `<span>${s.label}</span><span class="v" title="click to edit">${val}</span><span class="d">${s.desc || ""}</span>`;
      }
      item.querySelector(".v").addEventListener("click", () => startEdit(item, s));
      grp.appendChild(item);
    });
    box.appendChild(grp);
  });
}

// ---- Render: Victron scheduled-charge slots (CerboGX style) ----
function fmtDur(sec) {
  sec = Number(sec) || 0;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return (h ? h + "h " : "") + m + "m";
}
function renderVictron(plan) {
  const box = $("#victron-schedules");
  if (!box) return;
  if (!plan || !plan.available) { box.innerHTML = "<span class='muted'>no plan yet…</span>"; return; }
  const slots = plan.victron_slots || [];
  let html = "";
  for (let i = 0; i < 5; i++) {
    const s = slots[i];
    let right;
    if (s) {
      let day = "";
      try { day = new Date(s.start).toLocaleDateString([], { weekday: "long" }); } catch (_) {}
      const time = (s.start || "").slice(11, 16);
      right = `<span class="vic-on">${day} ${time} <span class="muted">(${fmtDur(s.duration)})</span> &nbsp;→ ${s.target_soc}%</span>`;
    } else {
      right = `<span class="muted">Disabled</span>`;
    }
    html += `<div class="vic-row"><span class="vic-name">Schedule ${i + 1}</span>${right}<span class="vic-chev">›</span></div>`;
  }
  box.innerHTML = html;
}

function renderMeta(plan) {
  const m = $("#meta");
  if (!plan.available) { m.textContent = ""; return; }
  let when = plan.generated_at;
  try { when = new Date(plan.generated_at).toLocaleTimeString([], { hour12: false }); } catch (e) {}
  let txt = "plan generated at " + when;
  if (lastLive) txt += " · live feed " + (lastLive.connected ? "connected" : "offline");
  if (plan.stale) txt += " — STALE (optimizer may not be running)";
  m.textContent = txt;
}

// ---- Load / refresh ----
function renderOverview() {
  if (!lastPlan) return;
  renderStatus(lastPlan);
  renderMetrics(lastPlan);
  renderSolar(lastPlan);
  renderDecision(lastPlan);
}

async function loadConfig() {
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    renderConfig(cfg);
  } catch (e) {
    $("#config").innerHTML = `<span class="cost">error loading config: ${e}</span>`;
  }
}

// Optional view modules (powerflow.js / charts.js). Called defensively so a
// failure in a module can never break the core dashboard render.
function safeRenderPowerFlow() {
  try { if (window.renderPowerFlow) window.renderPowerFlow("powerflow", lastLive, lastPlan); }
  catch (e) { /* isolated: module failure must not affect the rest */ }
}
function safeRenderChart() {
  try { if (window.renderHorizonChart) window.renderHorizonChart("horizon-chart", lastPlan); }
  catch (e) { /* isolated */ }
  try { if (window.renderEnergyMetrics) window.renderEnergyMetrics("energy-metrics", lastPlan); }
  catch (e) { /* isolated */ }
}

async function refreshPlan() {
  try {
    lastPlan = await fetch("/api/plan").then((r) => r.json());
    renderOverview();
    renderDaySummary(lastPlan);
    // Only rebuild the schedule tree when the plan actually changed, so a
    // background refresh can't collapse an hour you're inspecting.
    if (lastPlan.available && lastPlan.generated_at !== lastHoursGen) {
      renderHours(lastPlan);
      lastHoursGen = lastPlan.generated_at;
    }
    safeRenderChart();
    renderVictron(lastPlan);
    renderMeta(lastPlan);
  } catch (e) {
    $("#status-strip").innerHTML = `<span class="cost">error loading: ${e}</span>`;
  }
}

function applyLive(data) {
  lastLive = data;
  renderOverview();             // overlay live values onto the plan
  if (lastPlan) renderDaySummary(lastPlan);
  safeRenderPowerFlow();
  if (lastPlan) renderMeta(lastPlan);
}

async function pollLive() {     // backup path (and the initial fetch)
  try { applyLive(await fetch("/api/live").then((r) => r.json())); }
  catch (e) { /* keep last values on transient errors */ }
}

// Push stream: update the instant a new MQTT value arrives (no polling lag).
let _liveES = null;
function startLiveStream() {
  if (!window.EventSource || _liveES) return;
  try {
    _liveES = new EventSource("/api/live/stream");
    _liveES.onmessage = (e) => { try { applyLive(JSON.parse(e.data)); } catch (_) {} };
    // On error the browser auto-reconnects; the slow poll below covers any gap.
  } catch (_) { _liveES = null; }
}

// Sticky-header clock + sunrise/sunset (globally useful info). The clock ticks
// every second; sun times come from the plan's `today` block.
const SUN_ICON = {
  rise: '<svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="20" x2="21" y2="20"/><path d="M7 16 a5 5 0 0 1 10 0"/><line x1="12" y1="8" x2="12" y2="4"/><polyline points="9,7 12,4 15,7"/></g></svg>',
  set: '<svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="20" x2="21" y2="20"/><path d="M7 16 a5 5 0 0 1 10 0"/><line x1="12" y1="4" x2="12" y2="8"/><polyline points="9,5 12,8 15,5"/></g></svg>',
};
function renderHeaderClock() {
  const el = $("#header-clock");
  if (!el) return;
  const now = new Date().toLocaleTimeString([], { hour12: false });
  const t = (lastPlan && lastPlan.today) || {};
  const sun = (t.sun_rise && t.sun_set)
    ? `<span class="hc-sun"><span class="ic">${SUN_ICON.rise}</span>${t.sun_rise}<span class="ic">${SUN_ICON.set}</span>${t.sun_set}</span>`
    : "";
  el.innerHTML = `<span class="hc-time">${now}</span>${sun}`;
}

async function load() {
  await refreshPlan();
  renderPrevDay();           // set up the collapsed previous-day row (lazy-loads on expand)
  await loadConfig();
  await pollLive();
}

// Replan: ask the main service to re-run the optimizer now (same as the 15-min
// cycle), then reload the freshly published plan.
async function replan() {
  const btn = $("#replan");
  if (btn) { btn.disabled = true; btn.textContent = "Replanning…"; }
  try {
    // Runs the optimizer synchronously server-side and republishes the plan,
    // so by the time this resolves the new plan is ready to load.
    const r = await fetch("/api/replan", { method: "POST" }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "replan failed");
    await refreshPlan();
  } catch (e) {
    if (btn) btn.title = "Replan failed — is the service running?";
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Replan"; }
  }
}
const _replanBtn = $("#replan");
if (_replanBtn) _replanBtn.addEventListener("click", replan);

async function clearImportSchedule() {
  const btn = $("#clear-import-schedule");
  if (!btn || btn.disabled) return;
  if (!confirm("Clear Import Schedule?\nThis disables all five Victron scheduled-charge slots.")) return;

  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Clearing...";
  btn.title = "";
  try {
    const response = await fetch("/api/victron/clear-schedule", { method: "POST" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || !body.ok) throw new Error(body.error || "clear failed");
    if (lastPlan) {
      lastPlan = { ...lastPlan, victron_slots: [] };
      renderVictron(lastPlan);
    }
    btn.textContent = "Cleared";
    setTimeout(() => { btn.textContent = label; }, 1200);
  } catch (e) {
    btn.title = "Clear schedule failed: " + e.message;
    btn.textContent = "Clear failed";
    setTimeout(() => { btn.textContent = label; }, 1800);
  } finally {
    btn.disabled = false;
  }
}
const _clearImportScheduleBtn = $("#clear-import-schedule");
if (_clearImportScheduleBtn) _clearImportScheduleBtn.addEventListener("click", clearImportSchedule);

// ---- Advisor (read-only AI review) ----
const _esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
function _mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}
function mdToHtml(md) {
  const lines = _esc(md || "").split(/\r?\n/);
  let html = "", list = null;
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    let m;
    if (!line.trim()) { closeList(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList(); const lvl = Math.min(6, m[1].length + 2);
      html += `<h${lvl}>${_mdInline(m[2])}</h${lvl}>`; continue;
    }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += `<li>${_mdInline(m[1])}</li>`; continue;
    }
    if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
      html += `<li>${_mdInline(m[1])}</li>`; continue;
    }
    closeList();
    html += `<p>${_mdInline(line.trim())}</p>`;
  }
  closeList();
  return html;
}

let _advisorBusy = false;
let _advisorES = null;
// Streams the advisor run over SSE so the user sees live progress (stages, CLI log
// lines, and the model's output as it arrives) instead of a silent hang.
function runAdvisor(question) {
  if (_advisorBusy) return;
  const report = $("#advisor-report"), meta = $("#advisor-meta"), rBtn = $("#advisor-review");
  _advisorBusy = true;
  if (rBtn) rBtn.disabled = true;
  meta.textContent = "";
  report.innerHTML = '<div class="advisor-log" id="advisor-log"></div><div class="advisor-out" id="advisor-out"></div>';
  const logEl = $("#advisor-log"), outEl = $("#advisor-out");
  let acc = "", done = false;
  const addLog = (cls, msg) => {
    const d = document.createElement("div");
    d.className = "alog " + cls;
    d.textContent = msg;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
  };
  addLog("alog-stage", question ? `Asking: ${question}` : "Starting daily review…");

  const finish = (metaTxt) => {
    if (done) return;
    done = true;
    if (_advisorES) { _advisorES.close(); _advisorES = null; }   // stop auto-reconnect
    _advisorBusy = false;
    if (rBtn) rBtn.disabled = false;
    if (metaTxt) meta.textContent = metaTxt;
  };

  let es;
  try {
    es = new EventSource("/api/advisor/stream?question=" + encodeURIComponent(question || ""));
  } catch (e) {
    addLog("alog-err", "✗ could not open the advisor stream.");
    outEl.innerHTML = '<div class="banner">Could not start the advisor.</div>';
    finish();
    return;
  }
  _advisorES = es;
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (ev.type === "stage") addLog("alog-stage", "• " + ev.msg);
    else if (ev.type === "log") addLog("alog-line", ev.msg);
    else if (ev.type === "thinking") {
      let th = document.getElementById("alog-think");
      if (!th) { th = document.createElement("div"); th.id = "alog-think"; th.className = "alog alog-think"; logEl.appendChild(th); }
      th.textContent = `🧠 Claude is thinking… (${ev.count})`;
      logEl.scrollTop = logEl.scrollHeight;
    } else if (ev.type === "delta") {
      const th = document.getElementById("alog-think");
      if (th && !th.dataset.done) { th.dataset.done = "1"; th.textContent = "🧠 thinking complete — writing the report…"; }
      acc += ev.text;
      outEl.innerHTML = mdToHtml(acc);
    }
    else if (ev.type === "error") {
      addLog("alog-err", "✗ " + ev.error);
      if (!acc) outEl.innerHTML = `<div class="banner">${_esc(ev.error)}</div>`;
      finish("error");
    } else if (ev.type === "done") {
      outEl.innerHTML = mdToHtml(acc);
      const when = ev.generated_at ? new Date(ev.generated_at).toLocaleTimeString([], { hour12: false }) : "";
      finish([ev.mode === "question" ? "Answer" : "Daily review", ev.model, ev.auth,
              ev.elapsed_s != null ? ev.elapsed_s + "s" : null, when].filter(Boolean).join(" · "));
    }
  };
  es.onerror = () => {
    if (!done) {
      addLog("alog-err", "✗ stream closed (connection lost or service restarting).");
      if (!acc) outEl.innerHTML = '<div class="banner">Advisor stream closed — is the service running?</div>';
    }
    finish();
  };
}
const _advReview = $("#advisor-review");
if (_advReview) _advReview.addEventListener("click", () => runAdvisor(null));
const _advForm = $("#advisor-ask");
if (_advForm) _advForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = ($("#advisor-q").value || "").trim();
  if (q) runAdvisor(q);
});

// Month-so-far daily net chart (Trends). Cheap; refreshed slowly.
async function refreshMonthly() {
  try {
    const r = await fetch("/api/history/month").then((x) => x.json());
    if (window.renderMonthlyChart) renderMonthlyChart("monthly-chart", (r && r.days) || []);
  } catch (e) { /* leave the placeholder */ }
}

initMobileChrome();
load();
refreshMonthly();
renderHeaderClock();
startLiveStream();              // instant live updates via SSE
// Plan refreshes slowly (changes only when the optimizer runs). Live values now
// arrive via the SSE push; keep a slow poll as a fallback if the stream drops.
setInterval(refreshPlan, 30000);
setInterval(pollLive, 20000);
setInterval(renderHeaderClock, 1000);
setInterval(refreshMonthly, 120000);   // month chart changes slowly
