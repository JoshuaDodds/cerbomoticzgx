"use strict";
// Live power-flow v2 — VRM-style info CARDS in the real Victron physical topology,
// wired with smooth HASS-style connectors and source-coloured flow dots.
//
// TOPOLOGY:
//   Grid ── Inverter/Charger ── AC Loads        (AC bus, across the top)
//                  │                    ├── EV   (the EV charger is an AC load)
//               Battery ── Solar        └── Gas  (house gas use, grouped with loads)
//   (Solar is DC-coupled to the Battery; Battery is on the inverter's DC link.)
//
// SIZING: boxes are deliberately NOT a uniform grid — the central column
// (Inverter/Charger + Battery) is wider, and EV + Gas are two smaller boxes tucked
// beneath AC Loads, echoing the Victron GUI-v2 proportions. Font sizes scale per
// box, so the big central cards read large and the little EV/Gas cards stay compact.
//
// RESPONSIVE: the SVG measures its container and re-lays the whole diagram to fill
// the available width AND height (good for embedding on any screen), via a
// ResizeObserver. Built once and mutated in place every frame so dots never freeze.
//   window.renderPowerFlow(containerId, live, plan).
(function () {
  const PALETTE = {
    grid: "#0ea5e9", house: "#ec4899", solar: "#eab308",
    batt: "#22c55e", ev: "#6b8e57", gas: "#dc2626", inv: "#94a3b8",
  };

  const SYS_STATE = {
    0: "Off", 1: "Low Power", 2: "Fault", 3: "Bulk", 4: "Absorption",
    5: "Float", 6: "Storage", 7: "Equalising", 8: "Pass-through", 9: "Inverting",
    10: "Assisting", 252: "Ext. Control", 256: "Discharging", 257: "Sustain",
    258: "Recharge", 259: "Scheduled",
  };

  const NODE_LABEL = { grid: "Grid", inv: "Inverter / Charger", house: "AC Loads", solar: "Solar", batt: "Battery", ev: "EV", gas: "Gas" };

  // Origin-centred glyphs (the group's translate sets the visual centre).
  const ICON = {
    solar: '<g transform="translate(-16,-16) scale(1.33)"><circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="5" y1="5" x2="7" y2="7"/><line x1="17" y1="17" x2="19" y2="19"/><line x1="19" y1="5" x2="17" y2="7"/><line x1="7" y1="17" x2="5" y2="19"/></g></g>',
    grid: '<g transform="translate(-15,-16) scale(1.3)"><path d="M6 2 L18 2 L15 22 L9 22 Z" fill="none" stroke="currentColor" stroke-width="1.5"/><line x1="6" y1="8" x2="18" y2="8" stroke="currentColor" stroke-width="1.3"/><line x1="9" y1="2" x2="15" y2="22" stroke="currentColor" stroke-width="1.1"/><line x1="15" y1="2" x2="9" y2="22" stroke="currentColor" stroke-width="1.1"/></g>',
    house: '<g transform="translate(-16,-15) scale(1.33)"><path d="M3 11 L12 3 L21 11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M5 10 V21 H19 V10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></g>',
    batt: '<g transform="translate(-12,-17) scale(1.4)"><rect x="5" y="3" width="10" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="8.5" y="1.5" width="3" height="2" fill="currentColor"/></g>',
    ev: '<g transform="translate(-15,-11) scale(1.25)"><path d="M3 13 L5 8 H19 L21 13 V18 H3 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><circle cx="7.5" cy="18" r="1.8" fill="currentColor"/><circle cx="16.5" cy="18" r="1.8" fill="currentColor"/></g>',
    gas: '<g transform="translate(-11,-13) scale(1.1)"><path d="M11 2 C13 6 16 7 16 12 a5 5 0 0 1 -10 0 C6 9 8 8 8 6 C9 7 10 7 11 6 C11 4 11 3 11 2 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></g>',
    inv: '<g transform="translate(-13,-13) scale(1.15)"><rect x="2" y="2" width="20" height="20" rx="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 12 q3 -6 6 0 t6 0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></g>',
  };

  // Animated physical links (source-coloured dots). `ap`/`bp` = [side, offsetFrac].
  const FLOW_EDGES = [
    { key: "grid",  a: "grid",  ap: ["right", 0],     b: "inv",   bp: ["left", 0],  color: PALETTE.grid },
    { key: "load",  a: "inv",   ap: ["right", 0],     b: "house", bp: ["left", 0],  color: PALETTE.house },
    { key: "batt",  a: "inv",   ap: ["bottom", 0],    b: "batt",  bp: ["top", 0],   color: PALETTE.batt },
    { key: "solar", a: "solar", ap: ["right", 0],     b: "batt",  bp: ["left", 0],  color: PALETTE.solar },
    { key: "ev",    a: "house", ap: ["bottom", -0.26], b: "ev",   bp: ["top", 0],   color: PALETTE.ev },
  ];
  // Gas is a static link (no instantaneous power to animate), drawn faint to show
  // the connection beneath AC Loads.
  const GAS_EDGE = { key: "gas", a: "house", ap: ["bottom", 0.26], b: "gas", bp: ["top", 0], color: PALETTE.gas };

  const A = (w) => isFinite(w) && Math.abs(w) > 15;
  const f = (v) => (isFinite(v) ? v.toFixed(1) : "0");
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const num = (v) => { const n = Number(v); return isFinite(n) ? n : null; };

  const fmtW = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const a = Math.abs(w);
    if (a < 1) return "0 W";
    return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
  };
  const fmtWs = fmtW;
  const kwh = (v) => (v == null || !isFinite(Number(v)) ? "" : Number(v).toFixed(2) + " kWh");
  const fmtEnergy = (k) => (k == null || !isFinite(k) ? "" : (k >= 1000 ? (k / 1000).toFixed(2) + " MWh" : Math.round(k) + " kWh"));
  const fmtHM = (s) => { s = Math.max(0, Math.round(Number(s))); return Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m"; };
  const gridBig = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const ar = w > 15 ? "► " : (w < -15 ? "◄ " : "");
    return ar + fmtW(Math.abs(w));
  };
  function durFor(mag) {
    mag = Math.abs(mag);
    if (mag < 300) return "3.6";
    if (mag < 1500) return "3.0";
    if (mag < 5000) return "2.4";
    return "1.8";
  }

  // ---- layout: Victron-style proportions (center column wider; EV+Gas small) --
  function layout(W, H, hasEV, hasGas) {
    const xL = 0.145 * W, xC = 0.485 * W, xR = 0.825 * W;
    const wSide = 0.215 * W, wCtr = 0.30 * W;
    const yT = 0.25 * H, yB = 0.72 * H, rowH = 0.36 * H;
    const N = {
      grid:  { x: xL, y: yT, w: wSide, h: rowH },
      inv:   { x: xC, y: yT, w: wCtr,  h: rowH },
      house: { x: xR, y: yT, w: wSide, h: rowH },
      solar: { x: xL, y: yB, w: wSide, h: rowH },
      batt:  { x: xC, y: yB, w: wCtr,  h: rowH },
    };
    const subW = wSide * 0.46, subH = rowH * 0.74, dx = wSide * 0.28;
    if (hasEV && hasGas) {
      N.ev = { x: xR - dx, y: yB, w: subW, h: subH };
      N.gas = { x: xR + dx, y: yB, w: subW, h: subH };
    } else if (hasEV) {
      N.ev = { x: xR, y: yB, w: wSide * 0.7, h: subH };
    } else if (hasGas) {
      N.gas = { x: xR, y: yB, w: wSide * 0.7, h: subH };
    }
    return N;
  }

  function boxFonts(r) {
    return {
      title: clamp(r.h * 0.11, 10, 17),
      big: clamp(Math.min(r.h * 0.22, r.w * 0.17), 13, 46),
      row: clamp(r.h * 0.085, 9, 15),
      state: clamp(r.w * 0.115, 13, 36),
      iscale: clamp(r.h * 0.11 / 17, 0.42, 0.8),
    };
  }

  function port(r, spec) {
    const side = spec[0], off = spec[1] || 0, hw = r.w / 2, hh = r.h / 2;
    switch (side) {
      case "right":  return { x: r.x + hw, y: r.y + off * r.h, dx: 1,  dy: 0 };
      case "left":   return { x: r.x - hw, y: r.y + off * r.h, dx: -1, dy: 0 };
      case "top":    return { x: r.x + off * r.w, y: r.y - hh, dx: 0,  dy: -1 };
      default:       return { x: r.x + off * r.w, y: r.y + hh, dx: 0,  dy: 1 };
    }
  }
  function pathBetween(s, t) {
    const L = Math.max(20, Math.hypot(t.x - s.x, t.y - s.y) * 0.4);
    const c1 = { x: s.x + s.dx * L, y: s.y + s.dy * L };
    const c2 = { x: t.x + t.dx * L, y: t.y + t.dy * L };
    return `M${f(s.x)},${f(s.y)} C${f(c1.x)},${f(c1.y)} ${f(c2.x)},${f(c2.y)} ${f(t.x)},${f(t.y)}`;
  }
  const edgePath = (e, N) => pathBetween(port(N[e.a], e.ap), port(N[e.b], e.bp));

  function txt(id, x, y, o, content) {
    o = o || {};
    const idAttr = id ? ` id="${id}"` : "";
    const weight = o.weight ? ` font-weight="${o.weight}"` : "";
    const sz = typeof o.size === "number" ? o.size.toFixed(1) : (o.size || 14);
    return `<text${idAttr} x="${f(x)}" y="${f(y)}" text-anchor="${o.anchor || "start"}" font-size="${sz}"${weight} fill="${o.fill || "var(--text)"}">${content || ""}</text>`;
  }

  function edgeSvg(e, N, dur, fwd) {
    const d = edgePath(e, N), kp = fwd ? "0;1" : "1;0";
    let s = `<path id="pf-base-${e.key}" d="${d}" fill="none" stroke="${e.color}" stroke-width="5.5" stroke-linecap="round" opacity="0.12"/>`;
    for (let i = 0; i < 2; i++) {
      const begin = (-i * parseFloat(dur) / 2).toFixed(2);
      s += `<circle id="pf-dot-${e.key}-${i}" r="5" fill="${e.color}" opacity="0">`
         + `<animateMotion id="pf-anim-${e.key}-${i}" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"`
         + ` calcMode="linear" keyPoints="${kp}" keyTimes="0;1" path="${d}"/></circle>`;
    }
    return s;
  }
  const gasSvg = (N) => `<path id="pf-base-gas" d="${edgePath(GAS_EDGE, N)}" fill="none" stroke="${PALETTE.gas}" stroke-width="5.5" stroke-linecap="round" opacity="0.12"/>`;

  function buildCard(key, N) {
    const r = N[key]; if (!r) return "";
    const F = boxFonts(r), x0 = r.x - r.w / 2, y0 = r.y - r.h / 2;
    const pad = clamp(r.w * 0.06, 8, 20), L = x0 + pad, R = x0 + r.w - pad;
    let s = `<rect id="pf-card-${key}" x="${f(x0)}" y="${f(y0)}" width="${f(r.w)}" height="${f(r.h)}" rx="14" fill="var(--panel-2)" stroke="var(--line)" stroke-width="2.5"/>`;
    s += `<g id="pf-icon-${key}" transform="translate(${f(x0 + pad + 9)},${f(y0 + pad + 9)}) scale(${F.iscale.toFixed(2)})" color="var(--muted)">${ICON[key]}</g>`;
    s += txt(null, x0 + pad + 22, y0 + pad + F.title + 1, { size: F.title, fill: "var(--muted)" }, NODE_LABEL[key]);
    const bigY = y0 + r.h * 0.45;
    if (key === "grid" || key === "house") {
      s += txt(`pf-${key}-big`, L, bigY, { size: F.big, weight: 700 }, "—");
      ["L1", "L2", "L3"].forEach((lab, i) => {
        const y = y0 + r.h * 0.62 + i * (r.h * 0.135);
        s += txt(null, L, y, { size: F.row, fill: "var(--muted)" }, lab);
        s += txt(`pf-${key}-l${i + 1}`, R, y, { size: F.row, anchor: "end" }, "—");
      });
    } else if (key === "solar") {
      s += txt(`pf-solar-big`, L, bigY, { size: F.big, weight: 700 }, "—");
      s += txt(`pf-solar-sub`, L, y0 + r.h * 0.66, { size: F.row, fill: "var(--muted)" }, "");
    } else if (key === "batt") {
      s += txt(`pf-batt-temp`, R, y0 + pad + F.title + 1, { size: F.row, fill: "var(--muted)", anchor: "end" }, "");
      s += txt(`pf-batt-big`, L, bigY, { size: F.big, weight: 700 }, "—");
      s += txt(`pf-batt-state`, L, y0 + r.h * 0.66, { size: F.row + 1 }, "");
      s += txt(`pf-batt-vaw`, L, y0 + r.h * 0.82, { size: F.row, fill: "var(--muted)" }, "");
    } else if (key === "inv") {
      s += txt(`pf-inv-big`, L, bigY + 4, { size: F.state, weight: 700 }, "—");
    } else if (key === "ev") {
      s += txt(`pf-ev-big`, L, y0 + r.h * 0.55, { size: F.big, weight: 700 }, "—");
      s += txt(`pf-ev-energy`, L, y0 + r.h * 0.82, { size: F.row, fill: "var(--muted)" }, "");
    } else if (key === "gas") {
      s += txt(`pf-gas-big`, L, y0 + r.h * 0.58, { size: F.big, weight: 700 }, "—");
    }
    return s;
  }

  // ---- per-frame attribute application ---------------------------------------
  const _edgeDur = {}, _edgeDir = {};
  function applyEdges(box, flows) {
    for (const key in flows) {
      const { mag, fwd } = flows[key], on = A(mag), dur = durFor(mag), kp = fwd ? "0;1" : "1;0";
      const base = box.querySelector("#pf-base-" + key);
      if (base) base.setAttribute("opacity", on ? 0.34 : 0.12);
      for (let i = 0; i < 2; i++) {
        const dot = box.querySelector("#pf-dot-" + key + "-" + i);
        if (dot) dot.setAttribute("opacity", on ? 1 : 0);
      }
      for (let i = 0; i < 2; i++) {
        const an = box.querySelector("#pf-anim-" + key + "-" + i);
        if (!an) continue;
        if (on && _edgeDur[key] !== dur) an.setAttribute("dur", dur + "s");
        if (_edgeDir[key] !== kp) an.setAttribute("keyPoints", kp);
      }
      if (on) _edgeDur[key] = dur;
      _edgeDir[key] = kp;
    }
  }
  function applyNodes(box, active, invCol, gasOn) {
    for (const k in active) {
      const card = box.querySelector("#pf-card-" + k);
      if (card) card.setAttribute("stroke", active[k] ? PALETTE[k] : "var(--line)");
      const icon = box.querySelector("#pf-icon-" + k);
      if (icon) icon.setAttribute("color", active[k] ? PALETTE[k] : "var(--muted)");
    }
    const ic = box.querySelector("#pf-card-inv"); if (ic) ic.setAttribute("stroke", invCol);
    const ii = box.querySelector("#pf-icon-inv"); if (ii) ii.setAttribute("color", invCol);
    const gc = box.querySelector("#pf-card-gas"); if (gc) gc.setAttribute("stroke", gasOn ? PALETTE.gas : "var(--line)");
    const gi = box.querySelector("#pf-icon-gas"); if (gi) gi.setAttribute("color", gasOn ? PALETTE.gas : "var(--muted)");
    const gw = box.querySelector("#pf-base-gas"); if (gw) gw.setAttribute("opacity", gasOn ? 0.34 : 0.12);
  }
  function applyTexts(box, V) {
    for (const id in V) { const el = box.querySelector("#" + id); if (el) el.textContent = V[id]; }
  }

  // ---- module state for resize-driven re-layout ------------------------------
  let _sig = null, _lastLive = null, _lastPlan = null, _obs = null, _boxId = null;

  function computeFrame(live, plan) {
    const today = (plan && plan.today) || {};
    const grid = num(live.grid_w), batt = num(live.batt_w), pv = num(live.pv_w), load = num(live.load_w);
    const ev = (live.ev_w != null && isFinite(Number(live.ev_w))) ? Number(live.ev_w) : null;
    const gasM3 = today.gas_m3 != null && isFinite(Number(today.gas_m3)) ? Number(today.gas_m3) : null;
    const soc = num(live.soc);

    const flows = {
      grid:  { mag: Math.abs(grid || 0), fwd: (grid || 0) >= 0 },
      load:  { mag: Math.max(0, load || 0), fwd: true },
      batt:  { mag: Math.abs(batt || 0), fwd: (batt || 0) >= 0 },
      solar: { mag: Math.max(0, pv || 0), fwd: true },
      ev:    { mag: ev != null ? Math.max(0, ev) : 0, fwd: true },
    };
    const active = {
      grid: A(grid), house: A(load), solar: A(pv) && pv > 0, batt: A(batt),
      ev: ev != null && A(ev),
    };

    let battState = A(batt) ? (batt > 0 ? "Charging" : "Discharging") : "Idle";
    const ttg = num(live.batt_ttg);
    if (batt < -15 && ttg != null && ttg > 0 && ttg < 360000) battState += " · " + fmtHM(ttg);
    const vaw = [
      live.batt_voltage != null && isFinite(Number(live.batt_voltage)) ? Number(live.batt_voltage).toFixed(2) + " V" : "",
      live.batt_current != null && isFinite(Number(live.batt_current)) ? Math.round(Number(live.batt_current)) + " A" : "",
      batt != null ? fmtWs(batt) : "",
    ].filter(Boolean).join("   ·   ");

    const code = live.system_state != null ? Math.round(Number(live.system_state)) : null;
    const sysWord = (code != null && SYS_STATE[code]) ? SYS_STATE[code]
                  : (A(batt) ? (batt > 0 ? "Charging" : "Discharging") : "Idle");
    const invCol = (batt || 0) > 15 ? PALETTE.batt : ((batt || 0) < -15 ? "#eab308" : "var(--line)");

    const V = {
      "pf-grid-big": gridBig(grid),
      "pf-grid-l1": fmtWs(num(live.grid_l1)), "pf-grid-l2": fmtWs(num(live.grid_l2)), "pf-grid-l3": fmtWs(num(live.grid_l3)),
      "pf-house-big": fmtW(load),
      "pf-house-l1": fmtWs(num(live.load_l1)), "pf-house-l2": fmtWs(num(live.load_l2)), "pf-house-l3": fmtWs(num(live.load_l3)),
      "pf-solar-big": fmtW(pv),
      "pf-solar-sub": today.solar_kwh != null ? kwh(today.solar_kwh) + " today" : "",
      "pf-batt-temp": live.batt_temp != null && isFinite(Number(live.batt_temp)) ? Math.round(Number(live.batt_temp)) + " °C" : "",
      "pf-batt-big": soc != null ? Math.round(soc) + " %" : "—",
      "pf-batt-state": battState,
      "pf-batt-vaw": vaw,
      "pf-inv-big": sysWord,
    };
    if (ev != null) {
      V["pf-ev-big"] = fmtW(ev);
      V["pf-ev-energy"] = fmtEnergy(num(live.ev_energy_kwh));
    }
    if (gasM3 != null) V["pf-gas-big"] = gasM3.toFixed(2) + " m³";

    return { flows, active, invCol, V, hasEV: ev != null, hasGas: gasM3 != null, gasOn: gasM3 != null && gasM3 > 0 };
  }

  function ensureObserver(box) {
    if (_obs || typeof ResizeObserver === "undefined") return;
    let raf = 0;
    _obs = new ResizeObserver(() => {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { if (_lastLive) window.renderPowerFlow(_boxId, _lastLive, _lastPlan); });
    });
    _obs.observe(box);
  }

  window.renderPowerFlow = function (containerId, live, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    _boxId = containerId; _lastLive = live; _lastPlan = plan;
    ensureObserver(box);

    if (!live || !live.connected) {
      box.innerHTML = '<span class="muted">live feed offline — connect to see real-time power flow.</span>';
      _sig = null;
      return;
    }

    let W = Math.round(box.clientWidth) || 0;
    let H = Math.round(box.clientHeight) || 0;
    if (W < 60) return;
    if (H < 120) H = Math.round(W * 0.58);

    const fr = computeFrame(live, plan);
    const sig = `${fr.hasEV ? 1 : 0}${fr.hasGas ? 1 : 0}|${Math.round(W / 8)}|${Math.round(H / 8)}`;

    if (sig === _sig && box.querySelector("svg")) {
      applyEdges(box, fr.flows);
      applyNodes(box, fr.active, fr.invCol, fr.gasOn);
      applyTexts(box, fr.V);
      return;
    }

    // ---- full (re)build ------------------------------------------------------
    _sig = sig;
    const N = layout(W, H, fr.hasEV, fr.hasGas);
    const edges = FLOW_EDGES.filter((e) => e.key !== "ev" || fr.hasEV);
    edges.forEach((e) => { _edgeDur[e.key] = durFor(fr.flows[e.key].mag); _edgeDir[e.key] = fr.flows[e.key].fwd ? "0;1" : "1;0"; });

    const cardKeys = ["grid", "inv", "house", "solar", "batt"];
    if (fr.hasEV) cardKeys.push("ev");
    if (fr.hasGas) cardKeys.push("gas");

    box.innerHTML = `
      <svg viewBox="0 0 ${f(W)} ${f(H)}" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" style="display:block" role="img" aria-label="live power flow">
        ${edges.map((e) => edgeSvg(e, N, _edgeDur[e.key], fr.flows[e.key].fwd)).join("")}
        ${fr.hasGas ? gasSvg(N) : ""}
        ${cardKeys.map((k) => buildCard(k, N)).join("")}
      </svg>`;

    applyEdges(box, fr.flows);
    applyNodes(box, fr.active, fr.invCol, fr.gasOn);
    applyTexts(box, fr.V);
  };
})();
