"use strict";
// Live power-flow v2 — VRM-style rich info CARDS + HASS-style curved, source-
// coloured connectors. A mash-up of the two references the operator liked:
//   • Home Assistant "Energy distribution" — smooth pathing: a connector leaves a
//     node perpendicular, makes ONE quarter-turn Bézier, and runs straight into
//     the target; every flow dot keeps its SOURCE colour end-to-end.
//   • Victron GUI-v2 power-flow — each node is a rounded-rectangle card packed
//     with live telemetry (grid/loads per-phase, battery temp·V·A·SoC·time-to-go,
//     EV session) plus a top-centre inverter/charger state pill.
// There is NO central hub: energy flows along DIRECT source→sink paths computed
// from a flow decomposition (PV→house/battery/grid, battery→house/grid, grid→
// house/battery, house→EV). window.renderPowerFlow(containerId, live, plan).
//
// PERF: the SVG structure (cards, static labels/icons, connector paths and their
// <animateMotion> dots) is built ONCE, then every frame only mutates attributes —
// text content, card/edge colour, dot opacity, dot speed. The structure is rebuilt
// only when the OPTIONAL EV node appears/disappears, so <animateMotion> is never
// recreated and the dots never freeze.
(function () {
  const VB_W = 980, VB_H = 620;

  // Node cards: centre (x,y) + size (w,h). `color` is the SOURCE colour used for
  // that node's outgoing flow dots and its "active" border.
  const NODES = {
    grid:  { x: 168, y: 150, w: 252, h: 150, color: "#0ea5e9", label: "Grid" },
    house: { x: 812, y: 150, w: 252, h: 150, color: "#ec4899", label: "AC Loads" },
    solar: { x: 150, y: 470, w: 212, h: 96,  color: "#eab308", label: "Solar" },
    batt:  { x: 490, y: 330, w: 272, h: 168, color: "#22c55e", label: "Battery" },
    ev:    { x: 820, y: 470, w: 212, h: 132, color: "#6b8e57", label: "EV" },
  };

  // Inverter/charger system-state code -> word (mirrors lib/constants.py SystemState).
  const SYS_STATE = {
    0: "Off", 1: "Low Power", 2: "Fault", 3: "Bulk Charging", 4: "Absorption",
    5: "Float", 6: "Storage", 7: "Equalising", 8: "Pass-through", 9: "Inverting",
    10: "Assisting", 252: "External Control", 256: "Discharging", 257: "Sustain",
    258: "Recharge", 259: "Scheduled Charge",
  };

  // Origin-centred glyphs (the group's translate sets the visual centre).
  const ICON = {
    solar: '<g transform="translate(-16,-16) scale(1.33)"><circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="5" y1="5" x2="7" y2="7"/><line x1="17" y1="17" x2="19" y2="19"/><line x1="19" y1="5" x2="17" y2="7"/><line x1="7" y1="17" x2="5" y2="19"/></g></g>',
    grid: '<g transform="translate(-15,-16) scale(1.3)"><path d="M6 2 L18 2 L15 22 L9 22 Z" fill="none" stroke="currentColor" stroke-width="1.5"/><line x1="6" y1="8" x2="18" y2="8" stroke="currentColor" stroke-width="1.3"/><line x1="9" y1="2" x2="15" y2="22" stroke="currentColor" stroke-width="1.1"/><line x1="15" y1="2" x2="9" y2="22" stroke="currentColor" stroke-width="1.1"/></g>',
    house: '<g transform="translate(-16,-15) scale(1.33)"><path d="M3 11 L12 3 L21 11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M5 10 V21 H19 V10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></g>',
    batt: '<g transform="translate(-12,-17) scale(1.4)"><rect x="5" y="3" width="10" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="8.5" y="1.5" width="3" height="2" fill="currentColor"/></g>',
    ev: '<g transform="translate(-15,-11) scale(1.25)"><path d="M3 13 L5 8 H19 L21 13 V18 H3 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><circle cx="7.5" cy="18" r="1.8" fill="currentColor"/><circle cx="16.5" cy="18" r="1.8" fill="currentColor"/></g>',
  };

  // Directed flow edges (source -> sink). Colour follows the SOURCE node. Each edge
  // declares its attach PORTS as [side, offset-along-side] on both cards, so the
  // connectors fan out cleanly instead of all leaving one point.
  const FLOW_EDGES = [
    { key: "s_house", a: "solar", b: "house", p: ["top",    46], q: ["left",   36] },
    { key: "s_batt",  a: "solar", b: "batt",  p: ["right", -16], q: ["left",   38] },
    { key: "s_grid",  a: "solar", b: "grid",  p: ["top",   -44], q: ["bottom", -46] },
    { key: "g_house", a: "grid",  b: "house", p: ["right", -30], q: ["left",  -30] },
    { key: "g_batt",  a: "grid",  b: "batt",  p: ["right",  32], q: ["left",  -38] },
    { key: "b_house", a: "batt",  b: "house", p: ["right", -38], q: ["bottom", -40] },
    { key: "b_grid",  a: "batt",  b: "grid",  p: ["top",   -70], q: ["bottom",  48] },
    { key: "h_ev",    a: "house", b: "ev",    p: ["bottom", 44], q: ["top",     0] },
  ];

  const A = (w) => isFinite(w) && Math.abs(w) > 15;          // active threshold (W)
  const f = (v) => (isFinite(v) ? v.toFixed(1) : "0");

  const fmtW = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const a = Math.abs(w);
    if (a < 1) return "0 W";
    return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
  };
  // Signed per-phase / battery power (keeps the +/- the BMS reports).
  const fmtWs = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const a = Math.abs(w);
    if (a < 1) return "0 W";
    return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
  };
  const kwh = (v) => (v == null || !isFinite(Number(v)) ? "" : Number(v).toFixed(2) + " kWh");
  const fmtEnergy = (k) => {
    if (k == null || !isFinite(k)) return "";
    return k >= 1000 ? (k / 1000).toFixed(2) + " MWh" : Math.round(k) + " kWh";
  };
  const fmtHM = (s) => {
    s = Math.max(0, Math.round(Number(s)));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h + "h " + String(m).padStart(2, "0") + "m";
  };
  // Grid headline: ◄ import / ► export + magnitude.
  const gridBig = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const ar = w > 15 ? "◄ " : (w < -15 ? "► " : "");
    return ar + fmtW(Math.abs(w));
  };

  function durFor(mag) {
    mag = Math.abs(mag);
    if (mag < 300) return "3.6";
    if (mag < 1500) return "3.0";
    if (mag < 5000) return "2.4";
    return "1.8";
  }

  // ---- connector geometry ----------------------------------------------------
  function port(nodeKey, side, off) {
    const n = NODES[nodeKey], hw = n.w / 2, hh = n.h / 2;
    off = off || 0;
    switch (side) {
      case "right":  return { x: n.x + hw, y: n.y + off, dx: 1,  dy: 0 };
      case "left":   return { x: n.x - hw, y: n.y + off, dx: -1, dy: 0 };
      case "top":    return { x: n.x + off, y: n.y - hh, dx: 0,  dy: -1 };
      default:       return { x: n.x + off, y: n.y + hh, dx: 0,  dy: 1 }; // bottom
    }
  }
  // Perpendicular lead-out, one cubic Bézier, perpendicular lead-in (HASS style).
  function ends(e) {
    const s = port(e.a, e.p[0], e.p[1]), t = port(e.b, e.q[0], e.q[1]);
    const lead = 24;
    const s1 = { x: s.x + s.dx * lead, y: s.y + s.dy * lead };
    const t1 = { x: t.x + t.dx * lead, y: t.y + t.dy * lead };
    const k = Math.max(46, Math.hypot(t1.x - s1.x, t1.y - s1.y) * 0.42);
    const c1 = { x: s1.x + s.dx * k, y: s1.y + s.dy * k };
    const c2 = { x: t1.x + t.dx * k, y: t1.y + t.dy * k };
    return { s, t, s1, t1, c1, c2 };
  }
  function flowPath(e) {
    const g = ends(e);
    return `M${f(g.s.x)},${f(g.s.y)} L${f(g.s1.x)},${f(g.s1.y)} `
         + `C${f(g.c1.x)},${f(g.c1.y)} ${f(g.c2.x)},${f(g.c2.y)} ${f(g.t1.x)},${f(g.t1.y)} `
         + `L${f(g.t.x)},${f(g.t.y)}`;
  }
  function bezierMid(e) {                       // cubic at t=0.5, for the watt label
    const g = ends(e);
    return {
      x: 0.125 * g.s1.x + 0.375 * g.c1.x + 0.375 * g.c2.x + 0.125 * g.t1.x,
      y: 0.125 * g.s1.y + 0.375 * g.c1.y + 0.375 * g.c2.y + 0.125 * g.t1.y,
    };
  }

  // ---- static markup builders (run once per structure) -----------------------
  function txt(id, x, y, o, content) {
    o = o || {};
    const idAttr = id ? ` id="${id}"` : "";
    const weight = o.weight ? ` font-weight="${o.weight}"` : "";
    return `<text${idAttr} x="${x}" y="${y}" text-anchor="${o.anchor || "start"}" `
         + `font-size="${o.size || 14}"${weight} fill="${o.fill || "var(--text)"}">${content || ""}</text>`;
  }

  function edgeSvg(e, dur) {
    const col = NODES[e.a].color, d = flowPath(e), m = bezierMid(e);
    // Thin source-coloured ribbon under the dots (shown only while active).
    let s = `<path id="pf-base-${e.key}" d="${d}" fill="none" stroke="${col}" stroke-width="6" stroke-linecap="round" opacity="0"/>`;
    for (let i = 0; i < 2; i++) {
      const begin = (-i * parseFloat(dur) / 2).toFixed(2);
      s += `<circle id="pf-dot-${e.key}-${i}" r="5" fill="${col}" opacity="0">`
         + `<animateMotion id="pf-anim-${e.key}-${i}" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"`
         + ` calcMode="linear" keyPoints="0;1" keyTimes="0;1" path="${d}"/></circle>`;
    }
    s += `<text id="pf-elabel-${e.key}" x="${m.x.toFixed(1)}" y="${m.y.toFixed(1)}" `
       + `text-anchor="middle" dominant-baseline="middle" font-size="12" font-weight="600" `
       + `fill="var(--text)" opacity="0" style="paint-order:stroke;stroke:var(--panel-2);stroke-width:3px;">—</text>`;
    return s;
  }

  // The card frame (rect + centred icon + title) shared by all nodes.
  function cardFrame(key) {
    const n = NODES[key], x0 = n.x - n.w / 2, y0 = n.y - n.h / 2;
    let s = `<rect id="pf-card-${key}" x="${x0}" y="${y0}" width="${n.w}" height="${n.h}" rx="14" `
          + `fill="var(--panel-2)" stroke="var(--line)" stroke-width="2.5"/>`;
    s += `<g id="pf-icon-${key}" transform="translate(${x0 + 24},${y0 + 22}) scale(0.66)" color="var(--muted)">${ICON[key]}</g>`;
    s += txt(null, x0 + 44, y0 + 27, { size: 14, fill: "var(--muted)" }, n.label);
    return s;
  }

  function cardBody(key) {
    const n = NODES[key], x0 = n.x - n.w / 2, y0 = n.y - n.h / 2;
    const L = x0 + 18, Rr = x0 + n.w - 16;        // left label col / right value col
    if (key === "grid" || key === "house") {
      let s = txt(`pf-${key}-big`, L, y0 + 66, { size: 30, weight: 700 }, "—");
      ["L1", "L2", "L3"].forEach((lab, i) => {
        const y = y0 + 96 + i * 22;
        s += txt(null, L, y, { size: 13, fill: "var(--muted)" }, lab);
        s += txt(`pf-${key}-l${i + 1}`, Rr, y, { size: 14, anchor: "end" }, "—");
      });
      return s;
    }
    if (key === "solar") {
      return txt(`pf-solar-big`, L, y0 + 64, { size: 30, weight: 700 }, "—")
           + txt(`pf-solar-sub`, L, y0 + 86, { size: 13, fill: "var(--muted)" }, "");
    }
    if (key === "batt") {
      return txt(`pf-batt-temp`, Rr, y0 + 27, { size: 14, fill: "var(--muted)", anchor: "end" }, "")
           + txt(`pf-batt-big`, L, y0 + 72, { size: 34, weight: 700 }, "—")
           + txt(`pf-batt-state`, L, y0 + 102, { size: 15 }, "")
           + txt(`pf-batt-vaw`, L, y0 + 128, { size: 14, fill: "var(--muted)" }, "");
    }
    if (key === "ev") {
      return txt(`pf-ev-big`, L, y0 + 62, { size: 28, weight: 700 }, "—")
           + txt(`pf-ev-energy`, L, y0 + 90, { size: 13, fill: "var(--muted)" }, "")
           + txt(`pf-ev-time`, Rr, y0 + 90, { size: 13, fill: "var(--muted)", anchor: "end" }, "");
    }
    return "";
  }

  function buildCard(key) { return cardFrame(key) + cardBody(key); }

  // Top-centre inverter/charger state pill (status only — not a flow node).
  function buildStatePill() {
    const cx = 490, cy = 64, w = 230, h = 38, x0 = cx - w / 2, y0 = cy - h / 2;
    let s = txt(null, cx, 36, { size: 11, anchor: "middle", fill: "var(--muted)" }, "INVERTER / CHARGER");
    s += `<rect x="${x0}" y="${y0}" width="${w}" height="${h}" rx="19" fill="var(--panel-2)" stroke="var(--line)" stroke-width="2"/>`;
    s += `<circle id="pf-state-dot" cx="${x0 + 22}" cy="${cy}" r="6" fill="var(--muted)"/>`;
    s += txt("pf-state-text", x0 + 40, cy + 5, { size: 15, weight: 600 }, "—");
    return s;
  }

  // ---- per-frame attribute application (used by both build + fast paths) ------
  let _struct = null;             // which optional nodes exist (signature)
  const _edgeDur = {};            // key -> last applied dur (avoid needless restarts)

  function applyEdges(box, edges, flow) {
    edges.forEach((e) => {
      const mag = flow[e.key] || 0, on = A(mag), dur = durFor(mag);
      const base = box.querySelector("#pf-base-" + e.key);
      if (base) base.setAttribute("opacity", on ? 0.32 : 0);
      for (let i = 0; i < 2; i++) {
        const dot = box.querySelector("#pf-dot-" + e.key + "-" + i);
        if (dot) dot.setAttribute("opacity", on ? 1 : 0);
      }
      const lbl = box.querySelector("#pf-elabel-" + e.key);
      if (lbl) { lbl.setAttribute("opacity", on ? 1 : 0); if (on) lbl.textContent = fmtW(mag); }
      if (on && _edgeDur[e.key] !== dur) {
        for (let i = 0; i < 2; i++) {
          const an = box.querySelector("#pf-anim-" + e.key + "-" + i);
          if (an) an.setAttribute("dur", dur + "s");
        }
        _edgeDur[e.key] = dur;
      }
    });
  }
  function applyNodes(box, nodeActive) {
    for (const k in nodeActive) {
      const card = box.querySelector("#pf-card-" + k);
      if (card) card.setAttribute("stroke", nodeActive[k] ? NODES[k].color : "var(--line)");
      const icon = box.querySelector("#pf-icon-" + k);
      if (icon) icon.setAttribute("color", nodeActive[k] ? NODES[k].color : "var(--muted)");
    }
  }
  function applyTexts(box, V, dotCol) {
    for (const id in V) {
      const el = box.querySelector("#" + id);
      if (el) el.textContent = V[id];
    }
    const dot = box.querySelector("#pf-state-dot");
    if (dot) dot.setAttribute("fill", dotCol);
  }

  // Decompose net node powers into directed source→sink flows (W). PV serves the
  // house first, then charges the battery, then exports; the house is topped up
  // from the battery, then the grid; battery charging is topped up from the grid.
  function decompose(pv, grid, batt, load, ev) {
    const pvp = Math.max(0, pv);
    const gexp = Math.max(0, -grid);
    const bchg = Math.max(0, batt);
    const bdis = Math.max(0, -batt);
    const evW = ev > 0 ? ev : 0;
    const home = Math.max(0, load) + evW;

    let pvLeft = pvp, homeLeft = home, chgLeft = bchg;
    const s_house = Math.min(pvLeft, homeLeft); pvLeft -= s_house; homeLeft -= s_house;
    const b_house = Math.min(bdis, homeLeft); homeLeft -= b_house;
    const g_house = homeLeft;
    const s_batt = Math.min(pvLeft, chgLeft); pvLeft -= s_batt; chgLeft -= s_batt;
    const g_batt = chgLeft;
    const s_grid = Math.min(pvLeft, gexp); pvLeft -= s_grid;
    const b_grid = Math.max(0, gexp - s_grid);
    return { s_house, s_batt, s_grid, g_house, g_batt, b_house, b_grid, h_ev: evW };
  }

  window.renderPowerFlow = function (containerId, live, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (!live || !live.connected) {
      box.innerHTML = '<span class="muted">live feed offline — connect to see real-time power flow.</span>';
      _struct = null;
      return;
    }

    const today = (plan && plan.today) || {};
    const pv = Number(live.pv_w), grid = Number(live.grid_w);
    const batt = Number(live.batt_w), load = Number(live.load_w);
    const ev = (live.ev_w != null && isFinite(Number(live.ev_w))) ? Number(live.ev_w) : null;
    const soc = live.soc;

    const flow = decompose(pv, grid, batt, load, ev || 0);
    const edges = FLOW_EDGES.filter((e) => e.key !== "h_ev" || ev != null);

    const nodeActive = {
      grid: A(grid), house: A(load), solar: A(pv) && pv > 0, batt: A(batt),
      ev: ev != null && A(ev),
    };

    // Battery detail lines.
    const chargeWord = A(batt) ? (batt > 0 ? "Charging" : "Discharging") : "Idle";
    let battState = chargeWord;
    const ttg = Number(live.batt_ttg);
    if (batt < -15 && isFinite(ttg) && ttg > 0 && ttg < 360000) battState += " · " + fmtHM(ttg);
    const vaw = [
      (live.batt_voltage != null && isFinite(Number(live.batt_voltage))) ? Number(live.batt_voltage).toFixed(2) + " V" : "",
      (live.batt_current != null && isFinite(Number(live.batt_current))) ? Math.round(Number(live.batt_current)) + " A" : "",
      isFinite(batt) ? fmtWs(batt) : "",
    ].filter(Boolean).join("   ·   ");

    // Inverter/charger state pill.
    const code = live.system_state != null ? Math.round(Number(live.system_state)) : null;
    const sysWord = (code != null && SYS_STATE[code]) ? SYS_STATE[code]
                  : (A(batt) ? (batt > 0 ? "Charging" : "Discharging") : "Idle");
    const dotCol = batt > 15 ? "#22c55e" : (batt < -15 ? "#eab308" : "var(--muted)");

    const V = {
      "pf-grid-big": gridBig(grid),
      "pf-grid-l1": fmtWs(live.grid_l1), "pf-grid-l2": fmtWs(live.grid_l2), "pf-grid-l3": fmtWs(live.grid_l3),
      "pf-house-big": fmtW(load),
      "pf-house-l1": fmtWs(live.load_l1), "pf-house-l2": fmtWs(live.load_l2), "pf-house-l3": fmtWs(live.load_l3),
      "pf-solar-big": fmtW(pv),
      "pf-solar-sub": (today.solar_kwh != null ? kwh(today.solar_kwh) + " today" : ""),
      "pf-batt-temp": (live.batt_temp != null && isFinite(Number(live.batt_temp))) ? Math.round(Number(live.batt_temp)) + " °C" : "",
      "pf-batt-big": (soc != null ? Math.round(Number(soc)) + " %" : "—"),
      "pf-batt-state": battState,
      "pf-batt-vaw": vaw,
      "pf-state-text": sysWord,
    };
    if (ev != null) {
      V["pf-ev-big"] = fmtW(ev);
      V["pf-ev-energy"] = fmtEnergy(live.ev_energy_kwh);
      V["pf-ev-time"] = (live.ev_charge_time != null && isFinite(Number(live.ev_charge_time))) ? fmtHM(live.ev_charge_time) : "";
    }

    const structSig = ev != null ? "1" : "0";

    // ---- fast path: structure unchanged -> mutate in place -------------------
    if (structSig === _struct && box.querySelector("svg")) {
      applyEdges(box, edges, flow);
      applyNodes(box, nodeActive);
      applyTexts(box, V, dotCol);
      return;
    }

    // ---- full (re)build: EV node membership changed -------------------------
    _struct = structSig;
    edges.forEach((e) => { _edgeDur[e.key] = durFor(flow[e.key] || 0); });

    const cards = [buildCard("grid"), buildCard("house"), buildCard("solar"), buildCard("batt")];
    if (ev != null) cards.push(buildCard("ev"));

    box.innerHTML = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="live power flow">
        ${edges.map((e) => edgeSvg(e, _edgeDur[e.key])).join("")}
        ${buildStatePill()}
        ${cards.join("")}
      </svg>`;

    applyEdges(box, edges, flow);
    applyNodes(box, nodeActive);
    applyTexts(box, V, dotCol);
  };
})();
