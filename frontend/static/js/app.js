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
const SERVER_OFFLINE_AFTER_MS = 35000; // > two 15-second SSE heartbeats
let lastServerDataAt = 0;
let serverHasResponded = false;
const MOBILE_MQ = window.matchMedia("(max-width: 680px)");
const isMobileLayout = () => MOBILE_MQ.matches;

function setServerOffline(offline) {
  const banner = document.getElementById("server-offline-banner");
  if (banner) banner.hidden = !offline;
  document.body.classList.toggle("server-offline", offline);
  if (lastPlan) renderMeta(lastPlan);
}

function noteServerData() {
  lastServerDataAt = Date.now();
  serverHasResponded = true;
  setServerOffline(false);
}

function noteServerFailure() {
  // A cold-start failure is definitive. After a successful response, tolerate a
  // short fetch/SSE reconnect gap and retain the last good dashboard values.
  if (!serverHasResponded || Date.now() - lastServerDataAt >= SERVER_OFFLINE_AFTER_MS) {
    setServerOffline(true);
  }
}

// Logs tab state — declared here (not down near the Logs functions below) because
// activateTab() calls disconnectLogsStream() on EVERY tab switch, including the very first
// one fired by setAppView(appViewFromHash()) during initial script execution. A `let` further
// down the file is hoisted but stays in the temporal dead zone until its declaration line
// actually runs, so referencing it from a call this early threw "Cannot access before
// initialization" on cold page load (activateTab -> disconnectLogsStream -> _logsES) — this
// never showed up in dev testing because those runs opened the page with a `#ess` URL hash,
// which skips the `view === "overview"` branch that calls activateTab("live") on load.
let _logsES = null;
let _logsLines = [];
let _logsFilterTerm = "";
let _logsFilterDebounce = null;

const liveOn = () => !!(lastLive && lastLive.connected);
const truthy = (v) => v === true || v === 1 || String(v || "").trim().toLowerCase() === "true" ||
  String(v || "").trim() === "1" || String(v || "").trim().toLowerCase() === "on";
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
  if (tabName === "logs") connectLogsStream(); else disconnectLogsStream();
}

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => activateTab(t.dataset.tab));
});

let firstRender = true;

// ---- Top-level app views ----
const APP_VIEWS = ["overview", "ess", "battery", "live"];
function defaultAppViewName() {
  return "overview";
}
function setAppView(viewName) {
  const view = APP_VIEWS.includes(viewName) ? viewName : defaultAppViewName();
  const panelView = view === "overview" ? "ess" : view;
  document.body.dataset.appView = view;
  document.querySelectorAll(".app-view").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".app-nav-link").forEach((x) => x.classList.remove("active"));
  const panel = $(`#${panelView}-view`);
  const link = document.querySelector(`.app-nav-link[data-app-view="${view}"]`);
  if (panel) panel.classList.add("active");
  if (link) link.classList.add("active");
  if (view === "overview" && !isMobileLayout()) activateTab("live");
  syncMobileNavState();
}

function appViewFromHash() {
  const v = (window.location.hash || "").replace("#", "");
  return APP_VIEWS.includes(v) ? v : defaultAppViewName();
}

document.querySelectorAll(".app-nav-link").forEach((link) => {
  link.addEventListener("click", () => setAppView(link.dataset.appView));
});
window.addEventListener("hashchange", () => setAppView(appViewFromHash()));
setAppView(appViewFromHash());

// ---- Mobile chrome (guarded; hidden/no-op on desktop) ----
function currentAppViewName() {
  const view = document.body.dataset.appView;
  if (APP_VIEWS.includes(view)) return view;
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
    const menuOwnsCurrent =
      (activeView !== "ess" && activeView !== "overview") ||
      activeTab === "victron" ||
      activeTab === "config";
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
  setAppView("overview");
  if (!isMobileLayout()) activateTab("live");
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
      if (tabBtn.dataset.mobileTab === "schedule") scrollToCurrentScheduleSlot();
      else jumpToMobileViewTop();
      closeMobileMenu();
      return;
    }

    const appViewBtn = e.target.closest("button[data-mobile-app-view]");
    if (appViewBtn) {
      e.preventDefault();
      setAppView(appViewBtn.dataset.mobileAppView);
      jumpToMobileViewTop();
      closeMobileMenu();
      return;
    }

    if (e.target.closest("button[data-replan]")) {
      jumpToMobileViewTop();
      closeMobileMenu();
      return;
    }

    if (e.target.closest("button[data-restart]")) {
      jumpToMobileViewTop();
      closeMobileMenu();
      return;
    }

    if (e.target.closest("button[data-ai-override]") || e.target.closest("button[data-grid-assist]")) {
      jumpToMobileViewTop();
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

function todayRunningNet(plan) {
  const actual = liveTodayActuals();
  if (actual) return Number(actual.import_cost || 0) - Number(actual.export_rev || 0);
  const days = plan && plan.day_summary && plan.day_summary.days;
  const today = days && days.find((d) => d.is_today);
  if (today && today.actual) {
    return Number(today.actual.import_cost || 0) - Number(today.actual.export_rev || 0);
  }
  return null;
}

function todayForecastedNet(plan) {
  // Same figure as the header "Today" chip: full-day = settled-so-far + forward forecast.
  // Apply live actuals first so the elapsed portion uses the realized MQTT day counters
  // (closest to reality) instead of the plan JSON's lagging today_actuals snapshot —
  // otherwise this row and the header disagree.
  plan = planWithLiveActuals(plan);
  const days = plan && plan.day_summary && plan.day_summary.days;
  const today = days && days.find((d) => d.is_today);
  return today ? today.net : null;
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

  const keys = ["import_kwh", "import_cost", "export_kwh", "export_rev"];
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
    };
  });
  if (!changed) return plan;

  const total = keys.reduce((acc, k) => ({ ...acc, [k]: 0 }), {});
  days.forEach((d) => {
    const combined = d.combined || {};
    keys.forEach((k) => { total[k] += Number(combined[k] || 0); });
  });
  total.net = total.import_cost - total.export_rev;

  return { ...plan, day_summary: { ...plan.day_summary, days, total } };
}

function nextSell(plan) {
  if (!plan.hours) return null;
  // plan.hours is the merged timeline (settled history + forward schedule), so we must
  // skip settled/past slots — otherwise an old iteration's predicted SELL (priced at its
  // buy price and frozen in history) shows as the "next" sell and never updates.
  const now = Date.now();
  for (const h of plan.hours) {
    for (const s of h.slots) {
      if (caOf(s) !== "SELL" || s.settled || s.is_current) continue;
      const t = Date.parse(s.time);
      if (isFinite(t) && t < now) continue;   // already elapsed
      return s;
    }
  }
  return null;
}

// 24-hour HH:MM. ISO strings take the fast path; anything else goes through Date with
// hour12:false so it never renders AM/PM regardless of the browser locale.
function hm24(t) {
  if (typeof t === "string" && t.length >= 16 && t[10] === "T") return t.slice(11, 16);
  const d = new Date(t);
  return isNaN(d) ? String(t || "") : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
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

function setToggleButtons(selector, active, baseLabel) {
  document.querySelectorAll(selector).forEach((btn) => {
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.textContent = active ? `${baseLabel} on` : baseLabel;
  });
}

function updateControlButtons() {
  const overrideOn = truthy(lastLive && lastLive.ai_ess_override_enabled);
  const gridAssistOn = truthy(lastLive && lastLive.grid_charging_enabled);
  setToggleButtons("[data-ai-override]", overrideOn, "Override");
  setToggleButtons("[data-grid-assist]", gridAssistOn, "Grid assist");
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
    d.innerHTML = `<b>${b}</b>` + (s ? `<small>${s}</small>` : "");
    return d;
  };
  updateMobileKeyStat(currentCA(c), soc);
  strip.appendChild(kv(chipFor(currentCA(c)), "", "status-action"));   // action chip is self-explanatory — no label
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
  const days = (plan.day_summary && plan.day_summary.days) || [];
  const tomorrow = days.find((d) => !d.is_today);   // null until tomorrow's prices publish
  const ns = nextSell(plan);
  const card = (label, value, cls) => {
    const d = el("div", ["metric", cls].filter(Boolean).join(" "));
    d.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
    return d;
  };
  const tNet = todayNet(plan);
  const soc = pick(plan.battery_soc, lastLive && lastLive.soc);
  const price = c.price;  // 15-min plan price (live topic is hourly)
  box.appendChild(card("Current action", chipFor(currentCA(c)), "metric-current-action"));
  box.appendChild(card("Battery SoC", (soc != null ? Number(soc).toFixed(1) : "—") + "<small> %</small>"));
  box.appendChild(card("Price now", "€" + Number(price || 0).toFixed(3) + "<small> /kWh</small>"));
  if (tNet != null) box.appendChild(card("Today net", netHtml(tNet)));
  box.appendChild(card("Tomorrow", tomorrow ? netHtml(tomorrow.net) : "<span class='muted'>Pending</span>"));
  if (ns) box.appendChild(card("Next SELL", hm24(ns.time) + " <small>€" + Number(ns.price).toFixed(3) + "</small>"));
}

function renderSolar(plan) {
  const box = $("#solar");
  if (!plan.available) { box.innerHTML = `<div class="label">Solar forecast</div><div class="big">—</div>`; return; }
  // Show the optimizer-adjusted remaining PV as the main value, while preserving
  // the original VRM forecast as explicit source context underneath.
  const adjustedWh = plan.pv_adjusted_remaining_wh;
  const rawWh = plan.pv_remaining_raw_wh != null ? plan.pv_remaining_raw_wh : plan.pv_remaining_wh;
  const mainWh = adjustedWh != null ? adjustedWh : rawWh;
  const today = mainWh != null ? Math.max(0, mainWh / 1000).toFixed(1) : "—";
  const rawToday = rawWh != null ? Math.max(0, rawWh / 1000).toFixed(1) : "—";
  const rawSource = plan.pv_remaining_raw_source || "VRM forecast";
  const adjSource = plan.pv_adjusted_remaining_source || "optimizer forecast";
  const adjustment = plan.pv_adjustment_kwh != null ? Number(plan.pv_adjustment_kwh) : null;
  const adjustmentText = adjustment != null && Math.abs(adjustment) >= 0.05
    ? ` &nbsp;·&nbsp; ${adjSource} ${adjustment >= 0 ? "+" : "−"}${Math.abs(adjustment).toFixed(1)} kWh`
    : "";
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
    <div class="big">${today}<small style="font-size:13px;color:var(--muted)"> kWh adjusted remaining.</small></div>
    <div class="sub">${rawSource}: ${rawToday} kWh remaining${totalToday ? ` (${totalToday} kWh day)` : ""}${adjustmentText}</div>
    <div class="sub">VRM forecast tomorrow: ${tom} kWh${pvnow ? ` &nbsp;·&nbsp; producing ${pvnow} now` : ""}</div>
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
  box.innerHTML = "<h3 style='margin:2px 0 12px'>P/L summary (actuals + forecast)</h3>";
  if (!plan.available || !plan.day_summary) { box.innerHTML += "<span class='muted'>—</span>"; return; }
  const strategy = el("div", "pl-strategy");
  strategy.textContent = planStrategySummary(plan);
  box.appendChild(strategy);
  // Four aligned columns: label | import | export | net. The day-row and day-sub
  // rows share the same grid template so the numbers line up vertically.
  const cells = (lbl, impKwh, impC, expKwh, expC, netCell) =>
    `<span class="lbl">${lbl}</span>` +
    `<span class="num"><span class="t">import</span> <b>${impKwh}</b> <small>${impC}</small></span>` +
    `<span class="num"><span class="t">export</span> <b>${expKwh}</b> <small>${expC}</small></span>` +
    `<span class="net">${netCell}</span>`;
  const days = plan.day_summary.days || [];
  const head = (title) => { const h = el("div", "day-head"); h.textContent = title; box.appendChild(h); };
  const sub = (lbl, vals, net) => {
    const r = el("div", "day-sub");
    r.innerHTML = cells(lbl, kwh(vals.import_kwh), eur(vals.import_cost),
      kwh(vals.export_kwh), eur(vals.export_rev), netHtml(net));
    box.appendChild(r);
  };
  const dayTotal = (d) => {
    const r = el("div", "day-row day-total");
    r.innerHTML = cells("Day total", kwh(d.combined.import_kwh), eur(d.combined.import_cost),
      kwh(d.combined.export_kwh), eur(d.combined.export_rev), netHtml(d.net));
    box.appendChild(r);
  };

  // Today — actuals so far + forecast for the rest, then today's own total.
  const today = days.find((d) => d.is_today);
  if (today) {
    head(`Today · ${today.label}`);
    if (today.actual) {
      sub("Actual", today.actual, today.actual.import_cost - today.actual.export_rev);
      sub("Forecasted", today.forecast, today.forecast.import_cost - today.forecast.export_rev);
    }
    dayTotal(today);
  }

  // Tomorrow — its own separate total (forecast), or Pending until prices publish.
  // The two days are never tallied together.
  const tomorrow = days.find((d) => !d.is_today);
  head("Tomorrow" + (tomorrow ? ` · ${tomorrow.label}` : ""));
  if (tomorrow) {
    dayTotal(tomorrow);
  } else {
    const p = el("div", "muted");
    p.style.cssText = "padding: 4px 2px 2px 16px; font-size: 13.5px;";
    p.textContent = "Pending — tomorrow's prices not published yet.";
    box.appendChild(p);
  }
}

function planStrategySummary(plan) {
  const now = new Date();
  const configuredSlotHours = Number(plan.slot_duration_h);
  const slotHours = Number.isFinite(configuredSlotHours) && configuredSlotHours > 0
    ? configuredSlotHours : 0.25;
  const durationMs = Math.max(0.05, slotHours) * 3600000;
  const sameLocalDay = (d) => d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  const slots = ((plan.hours || []).flatMap((hour) => hour.slots || []))
    .map((slot) => ({ slot, at: new Date(slot.time) }))
    .filter((entry) => !Number.isNaN(entry.at.getTime()) && sameLocalDay(entry.at)
      && entry.at.getTime() + durationMs > now.getTime())
    .sort((a, b) => a.at - b.at);
  if (!slots.length) return "From now until midnight: no further scheduled grid action; use solar locally and avoid unnecessary battery cycling.";

  const groupsFor = (action) => {
    const matches = slots.filter((entry) => caOf(entry.slot) === action);
    const groups = [];
    matches.forEach((entry) => {
      const previous = groups[groups.length - 1];
      if (!previous || entry.at.getTime() - previous.end > durationMs * 1.25) {
        groups.push({ start: entry.at.getTime(), end: entry.at.getTime() + durationMs, entries: [entry] });
      } else {
        previous.end = entry.at.getTime() + durationMs;
        previous.entries.push(entry);
      }
    });
    return groups;
  };
  const time = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const ranges = (groups) => groups.slice(0, 3).map((g) => `${time(g.start)}–${time(g.end)}`).join(", ");
  const buys = groupsFor("BUY");
  const sells = groupsFor("SELL");
  const parts = [];
  if (buys.length) {
    const target = Math.max(...buys.flatMap((g) => g.entries.map((e) => Number(e.slot.soc_end)).filter(Number.isFinite)));
    parts.push(`charge${Number.isFinite(target) ? ` toward ${Math.round(target)}%` : ""} at ${ranges(buys)}`);
  }
  if (sells.length) parts.push(`export during the price peaks at ${ranges(sells)}`);
  if (!parts.length) parts.push("hold the battery for household demand and use available solar locally");

  let strategy = "Avoid unnecessary grid use";
  if (buys.length && sells.length) strategy = "Buy low, then sell at the stronger price peaks";
  else if (buys.length) strategy = "Buy low to cover the planned energy requirement";
  else if (sells.length) strategy = "Sell stored energy only at the strongest remaining prices";
  return `From now until midnight: ${parts.join(", then ")}. ${strategy} to minimise cost and maximise the day's net result.`;
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

function scrollToCurrentScheduleSlot(behavior = "smooth") {
  const currentSlot = document.querySelector("#hours .slot-row.current");
  const currentHour = document.querySelector("#hours .hour-row.current");
  const target = currentSlot && currentSlot.offsetParent !== null ? currentSlot : currentHour;
  if (target) {
    setTimeout(() => target.scrollIntoView({ block: "center", behavior }), 80);
  }
}

function jumpToMobileViewTop(behavior = "smooth") {
  setTimeout(() => window.scrollTo({ top: 0, behavior }), 80);
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

function makeRunningLedgerRow(plan) {
  const net = todayRunningNet(plan);
  const row = el("div", "running-ledger-row");
  row.innerHTML =
    `<span class="col-time">Today so far</span>` +
    `<span class="col-bar">midnight → now</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num">${net == null ? "—" : netHtml(net)}</span>`;
  return row;
}

function makeForecastedLedgerRow(plan) {
  const net = todayForecastedNet(plan);
  const row = el("div", "running-ledger-row forecasted-ledger-row");
  row.innerHTML =
    `<span class="col-time">Forecasted</span>` +
    `<span class="col-bar">full day total</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num muted">—</span>` +
    `<span class="col-num">${net == null ? "—" : netHtml(net)}</span>`;
  return row;
}

function makeSlotRow(s) {
  const sr = el("div", "slot-row" + (s.is_current ? " current" : ""));
  const g = Number(s.grid_energy);
  const sell = Number(s.sell != null ? s.sell : s.price);
  const imp = g > 0 ? g : 0;
  const exp = g < 0 ? -g : 0;
  const settled = !!s.settled;
  const socEnd = Number(s.soc_end);
  // IDLE PV-surplus into a non-full battery charges it (SoC up / cost basis down)
  // rather than exporting, so it books no grid revenue — mirrors _forward_grid_econ
  // on the server. Real feed-in (battery full) and SELL discharges still count.
  const idleStore = !settled && isIdle(s) && g < 0
    && !(Number.isFinite(socEnd) && socEnd >= 99);
  const projExp = idleStore ? 0 : exp;
  const slotNet = settled
    ? Number(s.actual_cost || 0) - Number(s.actual_reward || 0)
    : imp * Number(s.price) - projExp * sell;
  const gridStr = fmtGrid(g);
  const gridCell = idleStore ? `<span class='muted'>${gridStr}</span>` : gridStr;
  sr.innerHTML =
    `<span><span class="slot-dot" style="background:var(--${slotColorVar(s)})"></span>${s.time.slice(11, 16)}</span>` +
    `<span>${caOf(s)}</span>` +
    `<span class="col-num">€${Number(s.price || 0).toFixed(3)}</span>` +
    `<span class="col-num">${gridCell}</span>` +
    `<span class="col-num">${prodCell(s.pv)}</span>` +
    `<span class="col-num">${consCell(s.load)}</span>` +
    `<span class="col-num">${socPair(s.soc_start, s.soc_end)}</span>` +
    `<span class="col-num">${netHtml(slotNet)}</span>`;
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
  const hours = plan.hours || [];
  const hourDayKey = (h) => {
    const start = h && h.hour_start ? String(h.hour_start).slice(0, 10) : "";
    if (start) return start;
    const key = h && h.key ? String(h.key) : "";
    return /^\d{4}-\d{2}-\d{2}/.test(key) ? key.slice(0, 10) : "";
  };
  const currentHour = hours.find((h) => h.is_current);
  const currentKey = currentHour && currentHour.key;
  const currentDayKey = plan.current && plan.current.time
    ? String(plan.current.time).slice(0, 10)
    : hourDayKey(currentHour);
  const firstDayKey = hourDayKey(hours[0]);
  const todayKey = currentDayKey || firstDayKey;
  const todayHours = hours.filter((h) => hourDayKey(h) === todayKey);
  const lastTodayHourKey = todayHours.length ? todayHours[todayHours.length - 1].key : null;
  if ((firstRender || currentKey !== lastCurrentHourKey) && currentKey) {
    if (lastCurrentHourKey) expandedHours.delete(lastCurrentHourKey);
    expandedHours.add(currentKey);
    lastCurrentHourKey = currentKey;
  }
  hours.forEach((h) => {
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

    if (h.is_current) box.appendChild(makeRunningLedgerRow(plan));
    box.appendChild(row);
    box.appendChild(slotsWrap);
    if (h.key === lastTodayHourKey) {
      box.appendChild(makeForecastedLedgerRow(plan));
    }
  });

  // On first load, jump to the current slot.
  if (firstRender && currentRow) {
    scrollToCurrentScheduleSlot();
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
    if (s.min !== undefined) input.min = s.min;
    if (s.max !== undefined) input.max = s.max;
    if (s.type === "float") input.step = "0.01";
    if (s.type === "int") input.step = "1";
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
  if (document.body.classList.contains("server-offline")) {
    txt += " · server offline — showing last data";
  } else if (lastLive) {
    txt += " · live feed " + (lastLive.connected ? "connected" : "offline");
  }
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
    $("#config").innerHTML = '<span class="muted">Configuration unavailable while the server is offline.</span>';
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
    noteServerData();
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
    noteServerFailure();
  }
}

// Vehicle tab — a read-only mirror of the Tesla/vehicle0/* MQTT topics (no API cost).
function renderVehicle() {
  const box = document.getElementById("vehicle");
  if (!box) return;
  const L = lastLive || {};
  const has = (v) => v !== null && v !== undefined && v !== "" && v !== "None";
  const bool = (v) => v === true || String(v) === "True";
  const pct = (v) => has(v) ? Number(v).toFixed(0) + "%" : null;
  const amps = (v) => has(v) ? Number(v).toFixed(0) + " A" : null;
  const yesno = (v) => has(v) ? (bool(v) ? "Yes" : "No") : null;

  const cards = [];
  const card = (label, val) => { if (has(val)) cards.push(`<div class="metric"><div class="label">${label}</div><div class="value">${val}</div></div>`); };

  card("Car SoC", pct(L.veh_soc));
  card("Charge limit", pct(L.veh_soc_limit));
  card("Status", L.veh_charging_status);
  card("Plugged in", L.veh_plugged_status);
  card("Charge current", amps(L.veh_amps));
  card("PV surplus", amps(L.veh_surplus_amps));
  card("ETA to limit", (bool(L.veh_is_charging) && has(L.veh_eta) && L.veh_eta !== "N/A") ? L.veh_eta : null);
  // is_home stays unpublished in telemetry mode until the car streams its first Location
  // (audit M4) — show "Unknown" rather than silently hiding the row once we otherwise have
  // vehicle data, so a no-op manual Start press is explained instead of looking broken.
  card("At home", has(L.veh_is_home) ? yesno(L.veh_is_home) : (has(L.veh_soc) ? "Unknown" : null));
  card("Supercharging", yesno(L.veh_is_supercharging));
  card("Updated", L.veh_last_update);

  box.innerHTML = cards.length
    ? `<div class="metrics-grid">${cards.join("")}</div>`
    : `<span class="muted">waiting for vehicle status…</span>`;
  const title = document.querySelector("#tab-vehicle h3");
  if (title && has(L.veh_name)) title.textContent = L.veh_name;
}

function applyLive(data) {
  noteServerData();
  lastLive = data;
  renderOverview();             // overlay live values onto the plan
  if (lastPlan) renderDaySummary(lastPlan);
  updateControlButtons();
  safeRenderPowerFlow();
  renderVehicle();
  if (lastPlan) renderMeta(lastPlan);
}

async function pollLive() {     // backup path (and the initial fetch)
  try { applyLive(await fetch("/api/live").then((r) => r.json())); }
  catch (e) { noteServerFailure(); /* keep last values on transient errors */ }
}

// Push stream: update the instant a new MQTT value arrives (no polling lag).
let _liveES = null;
function startLiveStream() {
  if (!window.EventSource || _liveES) return;
  try {
    _liveES = new EventSource("/api/live/stream");
    _liveES.onmessage = (e) => { try { applyLive(JSON.parse(e.data)); } catch (_) {} };
    _liveES.onerror = () => noteServerFailure();
    // The browser auto-reconnects; the watchdog preserves values during short gaps.
  } catch (_) { _liveES = null; noteServerFailure(); }
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
async function replan(e) {
  const btn = e && e.currentTarget ? e.currentTarget : $("#replan");
  const buttons = Array.from(document.querySelectorAll("[data-replan]"));
  buttons.forEach((x) => { x.disabled = true; });
  if (btn) btn.textContent = "Replanning…";
  try {
    // Runs the optimizer synchronously server-side and republishes the plan,
    // so by the time this resolves the new plan is ready to load.
    const r = await fetch("/api/replan", { method: "POST" }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "replan failed");
    await refreshPlan();
  } catch (e) {
    buttons.forEach((x) => { x.title = "Replan failed — is the service running?"; });
  } finally {
    buttons.forEach((x) => {
      x.disabled = false;
      x.textContent = "Replan";
    });
  }
}
document.querySelectorAll("[data-replan]").forEach((btn) => btn.addEventListener("click", replan));

async function restartService(e) {
  const btn = e && e.currentTarget ? e.currentTarget : $("#restart");
  const buttons = Array.from(document.querySelectorAll("[data-restart]"));
  buttons.forEach((x) => { x.disabled = true; });
  if (btn) btn.textContent = "Restarting...";
  try {
    const r = await fetch("/api/restart", { method: "POST" }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "restart failed");
    buttons.forEach((x) => { x.textContent = "Restart requested"; });
  } catch (e) {
    buttons.forEach((x) => { x.title = "Restart failed - is the service running?"; });
  } finally {
    setTimeout(() => {
      buttons.forEach((x) => {
        x.disabled = false;
        x.textContent = "Restart";
      });
    }, 3000);
  }
}
document.querySelectorAll("[data-restart]").forEach((btn) => btn.addEventListener("click", restartService));

async function toggleControl(selector, endpoint, currentValue, baseLabel) {
  const buttons = Array.from(document.querySelectorAll(selector));
  const desired = !truthy(currentValue);
  buttons.forEach((x) => { x.disabled = true; });
  try {
    const r = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: desired }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "control failed");
    if (!lastLive) lastLive = {};
    if (selector === "[data-ai-override]") lastLive.ai_ess_override_enabled = desired;
    if (selector === "[data-grid-assist]") lastLive.grid_charging_enabled = desired;
    updateControlButtons();
  } catch (e) {
    buttons.forEach((x) => { x.title = `${baseLabel} failed - is the service running?`; });
  } finally {
    buttons.forEach((x) => { x.disabled = false; });
  }
}

function toggleAiOverride() {
  return toggleControl("[data-ai-override]", "/api/control/ai-override", lastLive && lastLive.ai_ess_override_enabled, "Override");
}
document.querySelectorAll("[data-ai-override]").forEach((btn) => btn.addEventListener("click", toggleAiOverride));

function toggleGridAssist() {
  return toggleControl("[data-grid-assist]", "/api/control/grid-assist", lastLive && lastLive.grid_charging_enabled, "Grid assist");
}
document.querySelectorAll("[data-grid-assist]").forEach((btn) => btn.addEventListener("click", toggleGridAssist));

async function clearImportSchedule() {
  const btn = $("#clear-import-schedule");
  if (!btn || btn.disabled) return;

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
function _isMdTableRow(line) {
  return /^\s*\|.*\|\s*$/.test(line || "");
}
function _splitMdTableRow(line) {
  return String(line || "").trim().replace(/^\|/, "").replace(/\|$/, "")
    .split("|").map((cell) => cell.trim());
}
function _isMdTableSeparator(line) {
  if (!_isMdTableRow(line)) return false;
  const cells = _splitMdTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s/g, "")));
}
function _mdTableAligns(separatorLine) {
  return _splitMdTableRow(separatorLine).map((cell) => {
    const s = cell.replace(/\s/g, "");
    if (s.startsWith(":") && s.endsWith(":")) return "center";
    if (s.endsWith(":")) return "right";
    return "";
  });
}
function _mdTableToHtml(headerLine, separatorLine, bodyLines) {
  const header = _splitMdTableRow(headerLine);
  const aligns = _mdTableAligns(separatorLine);
  const alignAttr = (idx) => aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
  const head = header.map((cell, idx) => `<th${alignAttr(idx)}>${_mdInline(cell)}</th>`).join("");
  const body = bodyLines.map((line) => {
    const cells = _splitMdTableRow(line);
    return `<tr>${cells.map((cell, idx) => `<td${alignAttr(idx)}>${_mdInline(cell)}</td>`).join("")}</tr>`;
  }).join("");
  return `<div class="advisor-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function mdToHtml(md) {
  const lines = _esc(md || "").split(/\r?\n/);
  let html = "", list = null;
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.replace(/\s+$/, "");
    let m;
    if (!line.trim()) { closeList(); continue; }
    if (_isMdTableRow(line)) {
      let sepIdx = i + 1;
      while (sepIdx < lines.length && !lines[sepIdx].trim()) sepIdx++;
      const separator = sepIdx < lines.length ? lines[sepIdx].replace(/\s+$/, "") : "";
      if (_isMdTableSeparator(separator)) {
        const bodyLines = [];
        let rowIdx = sepIdx + 1;
        while (rowIdx < lines.length) {
          const row = lines[rowIdx].replace(/\s+$/, "");
          if (!row.trim()) { rowIdx++; continue; }
          if (!_isMdTableRow(row) || _isMdTableSeparator(row)) break;
          bodyLines.push(row);
          rowIdx++;
        }
        closeList();
        html += _mdTableToHtml(line, separator, bodyLines);
        i = rowIdx - 1;
        continue;
      }
    }
    if (/^\s*-{3,}\s*$/.test(line)) {
      closeList();
      html += "<hr>";
      continue;
    }
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
let _advisorRecord = { ok: false, schema: "advisor_chat_v1", messages: [] };

function advisorMessages(record) {
  return (record && Array.isArray(record.messages)) ? record.messages : [];
}

function advisorWhen(value) {
  if (!value) return "";
  try {
    return new Date(value).toLocaleString([], {
      dateStyle: "medium",
      timeStyle: "medium",
      hour12: false,
    });
  } catch (_) {
    return value;
  }
}

function advisorMetaText(record) {
  const messages = advisorMessages(record);
  if (!messages.length) return "";
  const last = messages[messages.length - 1];
  const when = last && last.created_at ? "Generated " + advisorWhen(last.created_at) : "";
  return [
    "Advisor chat",
    messages.length + " message" + (messages.length === 1 ? "" : "s"),
    when,
  ].filter(Boolean).join(" · ");
}

function renderAdvisorMessage(msg, opts) {
  const role = msg.role === "user" ? "user" : "assistant";
  const label = role === "user" ? "You" : "Advisor";
  const time = msg.created_at ? advisorWhen(msg.created_at) : "";
  const status = msg.elapsed_s != null ? `${msg.elapsed_s}s` : "";
  const meta = [label, msg.model, msg.auth, status, time].filter(Boolean).join(" · ");
  const body = role === "assistant"
    ? (msg.text ? mdToHtml(msg.text) : `<span class="muted">${_esc(msg.error || "Waiting for response...")}</span>`)
    : `<p>${_esc(msg.text || "")}</p>`;
  const id = opts && opts.id ? ` id="${opts.id}"` : "";
  const index = opts && Number.isInteger(opts.index) ? opts.index : null;
  const actions = index === null ? "" : `<div class="advisor-message-actions">
      <button type="button" class="advisor-mini-btn" data-advisor-copy="${index}">Copy</button>
    </div>`;
  return `<article${id} class="advisor-message advisor-role-${role}">
    <div class="advisor-message-head">
      <div class="advisor-message-meta">${_esc(meta)}</div>
      ${actions}
    </div>
    <div class="advisor-message-body">${body}</div>
  </article>`;
}

function advisorTurns(messages) {
  const turns = [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    if (msg.role === "user") {
      const turn = { user: msg, userIndex: i, assistant: null, assistantIndex: null };
      if (messages[i + 1] && messages[i + 1].role === "assistant") {
        turn.assistant = messages[i + 1];
        turn.assistantIndex = i + 1;
        i++;
      }
      turns.push(turn);
    } else {
      turns.push({ user: null, userIndex: null, assistant: msg, assistantIndex: i });
    }
  }
  return turns;
}

function renderAdvisorChat(record, pendingTurn) {
  const report = $("#advisor-report");
  const meta = $("#advisor-meta");
  if (!report) return;
  const messages = advisorMessages(record);
  const latestMessages = messages.slice().reverse();
  void latestMessages;
  const turns = advisorTurns(messages).slice().reverse();
  const pending = pendingTurn
    ? `<section class="advisor-turn advisor-turn-pending">
        ${renderAdvisorMessage(pendingTurn.user)}
        ${renderAdvisorMessage(pendingTurn.assistant, { id: "advisor-streaming-message" })}
        <div class="advisor-log" id="advisor-log"></div>
      </section>`
    : "";
  const saved = turns.map((turn) => {
    const deleteIndex = turn.userIndex !== null ? turn.userIndex : turn.assistantIndex;
    return `<section class="advisor-turn">
      <div class="advisor-turn-actions">
        <button type="button" class="advisor-mini-btn advisor-delete-btn" data-advisor-delete="${deleteIndex}">Delete exchange</button>
      </div>
      ${turn.user ? renderAdvisorMessage(turn.user, { index: turn.userIndex }) : ""}
      ${turn.assistant ? renderAdvisorMessage(turn.assistant, { index: turn.assistantIndex }) : ""}
    </section>`;
  }).join("");
  report.innerHTML = (pending || saved)
    ? `<div class="advisor-chat">${pending}${saved}</div>`
    : '<span class="muted">No advisor chat yet — run the daily review or ask a question.</span>';
  if (meta) meta.textContent = advisorMetaText(record);
}

function renderAdvisorRecord(record) {
  _advisorRecord = record && Array.isArray(record.messages)
    ? record
    : { ok: false, schema: "advisor_chat_v1", messages: [] };
  renderAdvisorChat(_advisorRecord);
}

async function loadAdvisorLatest() {
  if (_advisorBusy) return;
  try {
    const record = await fetch("/api/advisor/latest").then((r) => r.json());
    if (!_advisorBusy) renderAdvisorRecord(record);
  } catch (_) { /* keep the empty advisor placeholder */ }
}

function advisorConfirm(opts) {
  const title = (opts && opts.title) || "Confirm";
  const body = (opts && opts.body) || "";
  const confirmText = (opts && opts.confirmText) || "Confirm";
  const cancelText = opts && Object.prototype.hasOwnProperty.call(opts, "cancelText")
    ? opts.cancelText
    : "Cancel";
  const danger = opts && opts.danger;
  return new Promise((resolve) => {
    const prior = document.querySelector(".advisor-modal-backdrop");
    if (prior) prior.remove();
    const overlay = document.createElement("div");
    overlay.className = "advisor-modal-backdrop";
    overlay.innerHTML = `<div class="advisor-modal" role="dialog" aria-modal="true" aria-labelledby="advisor-modal-title">
      <h3 id="advisor-modal-title">${_esc(title)}</h3>
      <p>${_esc(body)}</p>
      <div class="advisor-modal-actions">
        ${cancelText ? `<button type="button" class="btn-secondary" data-advisor-modal-cancel>${_esc(cancelText)}</button>` : ""}
        <button type="button" class="btn-secondary ${danger ? "btn-danger" : ""}" data-advisor-modal-confirm>${_esc(confirmText)}</button>
      </div>
    </div>`;
    const finish = (value) => {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") finish(false);
    };
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay || e.target.closest("[data-advisor-modal-cancel]")) finish(false);
      else if (e.target.closest("[data-advisor-modal-confirm]")) finish(true);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    const first = overlay.querySelector("[data-advisor-modal-cancel], [data-advisor-modal-confirm]");
    if (first) first.focus();
  });
}

function advisorTextForIndex(index) {
  const messages = advisorMessages(_advisorRecord);
  const msg = messages[index];
  return msg ? (msg.text || msg.error || "") : "";
}

function copyAdvisorFallback(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } finally {
    document.body.removeChild(area);
  }
}

async function copyAdvisorMessage(index, btn) {
  const text = advisorTextForIndex(index);
  if (!text) return;
  const label = btn ? btn.textContent : "";
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      copyAdvisorFallback(text);
    }
    if (btn) {
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = label || "Copy"; }, 1000);
    }
  } catch (_) {
    if (btn) {
      btn.textContent = "Copy failed";
      setTimeout(() => { btn.textContent = label || "Copy"; }, 1400);
    }
  }
}

async function deleteAdvisorExchange(index, btn) {
  if (_advisorBusy) return;
  const confirmed = await advisorConfirm({
    title: "Delete exchange?",
    body: "This removes the saved prompt and its paired advisor reply from this chat.",
    confirmText: "Delete",
    danger: true,
  });
  if (!confirmed) return;
  const label = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Deleting...";
  }
  try {
    const response = await fetch("/api/advisor/delete-exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index }),
    });
    const record = await response.json().catch(() => ({}));
    if (!response.ok) {
      const msg = response.status === 404
        ? "Delete endpoint is not available. Restart the frontend service to load the new route."
        : (record.error || "delete failed");
      throw new Error(msg);
    }
    renderAdvisorRecord(record);
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = e.message && e.message.includes("endpoint") ? "Restart needed" : "Delete failed";
      btn.title = e.message;
      setTimeout(() => { btn.textContent = label || "Delete exchange"; }, 1600);
    }
    advisorConfirm({
      title: "Delete failed",
      body: e.message || "The exchange could not be deleted.",
      confirmText: "OK",
      cancelText: null,
    });
  }
}

// Streams the advisor run over SSE so the user sees live progress (stages, CLI log
// lines, and the model's output as it arrives) instead of a silent hang.
function runAdvisor(question) {
  if (_advisorBusy) return;
  const meta = $("#advisor-meta"), rBtn = $("#advisor-review");
  _advisorBusy = true;
  if (rBtn) rBtn.disabled = true;
  if (meta) meta.textContent = "";
  const now = new Date().toISOString();
  const pendingTurn = {
    user: { role: "user", mode: question ? "question" : "review", text: question || "Run daily review", created_at: now },
    assistant: { role: "assistant", mode: question ? "question" : "review", text: "", created_at: now },
  };
  renderAdvisorChat(_advisorRecord, pendingTurn);
  const logEl = $("#advisor-log"), streamMsg = $("#advisor-streaming-message");
  const outEl = streamMsg && streamMsg.querySelector(".advisor-message-body");
  let acc = "", done = false;
  const addLog = (cls, msg) => {
    if (!logEl) return;
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
    if (meta && metaTxt) meta.textContent = metaTxt;
  };

  let es;
  try {
    es = new EventSource("/api/advisor/stream?question=" + encodeURIComponent(question || ""));
  } catch (e) {
    addLog("alog-err", "✗ could not open the advisor stream.");
    if (outEl) outEl.innerHTML = '<div class="banner">Could not start the advisor.</div>';
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
      if (outEl) outEl.innerHTML = mdToHtml(acc);
    }
    else if (ev.type === "error") {
      addLog("alog-err", "✗ " + ev.error);
      if (!acc && outEl) outEl.innerHTML = `<div class="banner">${_esc(ev.error)}</div>`;
      finish("error");
      loadAdvisorLatest();
    } else if (ev.type === "done") {
      if (outEl) outEl.innerHTML = mdToHtml(acc);
      const when = ev.generated_at ? advisorWhen(ev.generated_at) : "";
      finish([ev.mode === "question" ? "Answer" : "Daily review", ev.model, ev.auth,
              ev.elapsed_s != null ? ev.elapsed_s + "s" : null, when].filter(Boolean).join(" · "));
      loadAdvisorLatest();
    }
  };
  es.onerror = () => {
    if (!done) {
      addLog("alog-err", "✗ stream closed (connection lost or service restarting).");
      if (!acc && outEl) outEl.innerHTML = '<div class="banner">Advisor stream closed — is the service running?</div>';
    }
    finish();
  };
}
const _advReview = $("#advisor-review");
if (_advReview) _advReview.addEventListener("click", () => runAdvisor(null));
async function clearAdvisorChat() {
  if (_advisorBusy) return;
  const confirmed = await advisorConfirm({
    title: "Clear advisor chat?",
    body: "This removes every saved advisor prompt and reply from this session.",
    confirmText: "Clear chat",
    danger: true,
  });
  if (!confirmed) return;
  try {
    const record = await fetch("/api/advisor/clear", { method: "POST" }).then((r) => r.json());
    renderAdvisorRecord(record);
  } catch (_) { /* leave current chat visible */ }
}
const _advClear = $("#advisor-clear");
if (_advClear) _advClear.addEventListener("click", clearAdvisorChat);
const _advReport = $("#advisor-report");
if (_advReport) _advReport.addEventListener("click", (e) => {
  const copyBtn = e.target.closest("[data-advisor-copy]");
  if (copyBtn) {
    const index = Number(copyBtn.dataset.advisorCopy);
    if (Number.isInteger(index)) copyAdvisorMessage(index, copyBtn);
    return;
  }
  const deleteBtn = e.target.closest("[data-advisor-delete]");
  if (deleteBtn) {
    const index = Number(deleteBtn.dataset.advisorDelete);
    if (Number.isInteger(index)) deleteAdvisorExchange(index, deleteBtn);
  }
});
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

async function refreshForecastAccuracy() {
  try {
    const r = await fetch("/api/history/accuracy").then((x) => x.json());
    if (window.renderForecastAccuracyChart) renderForecastAccuracyChart("forecast-accuracy-chart", r);
  } catch (e) { /* leave the placeholder */ }
}

// Tesla API usage table on the Vehicle tab (today's Fleet API spend vs the credit).
async function refreshVehicleUsage() {
  const box = document.getElementById("vehicle-usage");
  if (!box) return;
  try {
    const u = await fetch("/api/tesla/usage").then((x) => x.json());
    const cats = u.categories || {};
    const cur = { EUR: "€", USD: "$" }[u.currency] || "";
    const money = (v) => cur + Number(v || 0).toFixed(2);
    const row = (label, key) => cats[key]
      ? `<div class="usage-row"><span>${label}</span><span>${cats[key].count}</span><span>${money(cats[key].cost)}</span></div>`
      : "";
    // Streaming Signals are pushed by the car (outside the request budget); shown approximately.
    const s = u.streaming;
    const streamRow = s
      ? `<div class="usage-row"><span>Streaming signals <span class="muted" style="font-weight:400">≈</span></span><span>${s.count}</span><span>${money(s.cost)}</span></div>`
      : "";
    box.innerHTML =
      `<div class="usage-table">` +
      row("Commands", "command") +
      row("Data", "data") +
      row("Wakes", "wake") +
      streamRow +
      `<div class="usage-row usage-total"><span>Total this month</span><span></span>` +
      `<span>${money(u.total)} <span class="muted" style="font-weight:400">of ${money(u.monthly_credit)}</span></span></div>` +
      `</div>`;
  } catch (e) { /* leave the placeholder */ }
}

// ---- Logs tab: live tail via SSE, connected lazily while the tab is open ----
// (_logsES/_logsLines/_logsFilterTerm/_logsFilterDebounce are declared near the top of this
// file, not here — see the comment there for why.)
const LOGS_MAX_RENDERED = 2000;

function _escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Finds matches against the RAW line (case-insensitive) and escapes each resulting segment
// independently, THEN wraps matched segments in a highlight span. Escaping only whole,
// un-split segments of the original text — never a match found by re-scanning already-escaped
// output — means an entity like "&amp;" can never get a <mark> boundary spliced into the
// middle of it (a term like "amp" would otherwise match inside "&amp;"'s own escaped form and
// corrupt the entity, even though nothing here is exploitable as XSS: the <mark> wrapper is a
// fixed literal and its content is always pre-escaped).
function _highlightMatches(line, term) {
  if (!term) return _escapeHtml(line);
  const lower = line.toLowerCase();
  const termLower = term.toLowerCase();
  if (!termLower) return _escapeHtml(line);
  let out = "";
  let i = 0;
  while (i < line.length) {
    const idx = lower.indexOf(termLower, i);
    if (idx === -1) {
      out += _escapeHtml(line.slice(i));
      break;
    }
    out += _escapeHtml(line.slice(i, idx));
    out += `<mark class="log-hl">${_escapeHtml(line.slice(idx, idx + term.length))}</mark>`;
    i = idx + term.length;
  }
  return out;
}

function _pushLogLines(lines, replace) {
  if (replace) _logsLines = [];
  if (lines && lines.length) _logsLines = _logsLines.concat(lines);
  if (_logsLines.length > LOGS_MAX_RENDERED) {
    _logsLines = _logsLines.slice(_logsLines.length - LOGS_MAX_RENDERED);
  }
}

function _renderLogsView() {
  const el = $("#logs-output");
  if (!el) return;
  const wasNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  const term = _logsFilterTerm.trim();
  const filtered = term
    ? _logsLines.filter((line) => line.toLowerCase().includes(term.toLowerCase()))
    : _logsLines;
  if (!filtered.length) {
    el.innerHTML = `<span class="muted">${term ? "no matching lines" : "waiting for logs…"}</span>`;
    return;
  }
  const frag = document.createDocumentFragment();
  filtered.forEach((line) => {
    const d = document.createElement("div");
    d.className = "log-line";
    d.innerHTML = _highlightMatches(line, term);
    frag.appendChild(d);
  });
  el.innerHTML = "";
  el.appendChild(frag);
  if (wasNearBottom) el.scrollTop = el.scrollHeight;
}

function connectLogsStream() {
  if (_logsES || !window.EventSource) return;
  const el = $("#logs-output");
  if (el) el.innerHTML = '<span class="muted">waiting for logs…</span>';
  try {
    _logsES = new EventSource("/api/logs/stream");
  } catch (e) {
    if (el) el.innerHTML = '<span class="muted">could not open the log stream.</span>';
    return;
  }
  _logsES.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (!ev.lines) return;
    // "snapshot" replaces the buffer (sent on every new connection, including the server's
    // periodic self-recycle — see LOGS_STREAM_MAX_SECONDS server-side); "append" adds
    // incrementally. Without this distinction a reconnect would duplicate the whole buffer.
    _pushLogLines(ev.lines, ev.type === "snapshot");
    _renderLogsView();
  };
}

function disconnectLogsStream() {
  if (_logsES) { _logsES.close(); _logsES = null; }
}

const logsSearchInput = document.getElementById("logs-search");
if (logsSearchInput) {
  logsSearchInput.addEventListener("input", () => {
    const value = logsSearchInput.value;
    clearTimeout(_logsFilterDebounce);
    // Short debounce so re-filtering 2000 lines on every keystroke of a fast typer doesn't
    // visibly lag; still reads as instant/live filtering to the user.
    _logsFilterDebounce = setTimeout(() => {
      _logsFilterTerm = value;
      _renderLogsView();
    }, 120);
  });
}

async function refreshWeather() {
  try {
    const r = await fetch("/api/weather").then((x) => x.json());
    if (window.renderWeatherChart) renderWeatherChart("weather-chart", r);
    if (window.renderWeatherImpactChart) renderWeatherImpactChart("weather-impact-chart", r);
    // Mobile-only duplicates in the Trends view (Weather tab isn't in the mobile nav).
    // The containers only exist / show on mobile; render defensively when present.
    if (document.getElementById("weather-chart-m") && window.renderWeatherChart) {
      renderWeatherChart("weather-chart-m", r);
    }
    if (document.getElementById("weather-impact-chart-m") && window.renderWeatherImpactChart) {
      renderWeatherImpactChart("weather-impact-chart-m", r);
    }
  } catch (e) { /* leave the placeholder */ }
}

// EV manual Start/Stop charge: sets the dedicated ev_charge_requested intent (independent of
// grid assist); the controller then starts/stops the car with its safety checks.
document.querySelectorAll("[data-ev-charge]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const enabled = btn.getAttribute("data-ev-charge") === "start";
    btn.disabled = true;
    try {
      await fetch("/api/control/ev-charge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } catch (e) { /* ignore; controller acts on its next tick */ }
    setTimeout(() => { btn.disabled = false; }, 1500);
  });
});

// Manual "Refresh data" (Vehicle tab): one-shot flag that wakes the car on the controller's
// next tick (up to ~30s), then normal live updates pick up the fresh state — no separate
// polling needed here.
document.querySelectorAll("[data-vehicle-refresh]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Refreshing…";
    try {
      await fetch("/api/control/refresh-vehicle", { method: "POST" });
    } catch (e) { /* ignore; controller picks it up on its next tick regardless */ }
    setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 4000);
  });
});

initMobileChrome();
load();
loadAdvisorLatest();
refreshForecastAccuracy();
refreshWeather();
refreshMonthly();
refreshVehicleUsage();
renderHeaderClock();
startLiveStream();              // instant live updates via SSE
// Plan refreshes slowly (changes only when the optimizer runs). Live values now
// arrive via the SSE push; keep a slow poll as a fallback if the stream drops.
setInterval(refreshPlan, 30000);
setInterval(pollLive, 20000);
setInterval(noteServerFailure, 5000);
setInterval(renderHeaderClock, 1000);
setInterval(refreshForecastAccuracy, 120000);
setInterval(refreshWeather, 1800000);
setInterval(refreshMonthly, 120000);   // month chart changes slowly
setInterval(refreshVehicleUsage, 60000);   // Tesla API usage tally
