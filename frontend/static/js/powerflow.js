"use strict";
// Live power-flow diagram: Solar / Grid / Battery / House (+ optional EV / Gas).
// HASS-style: NO central hub. Energy flows along DIRECT curved paths between a
// source and a sink, and every flow dot keeps its SOURCE colour the whole way —
// so grid power reads blue into the house, battery green, solar yellow, and you
// can see exactly what is feeding what. window.renderPowerFlow(id, live, plan).
//
// SMOOTHNESS MODEL: every possible edge (solar→house/battery/grid, battery→
// house/grid, grid→house/battery, house→EV) is built ONCE with its dots and
// animates continuously; a flow appearing/disappearing is a pure opacity toggle,
// so <animateMotion> is never recreated and dots never freeze. Each edge's dot
// colour is fixed to its source, so colour never has to change either. Only
// speed (dur) is touched, and only for the edge whose magnitude tier changed.
// The SVG is rebuilt only when which OPTIONAL nodes exist (EV / Gas) changes.
(function () {
  const VB_W = 760, VB_H = 600;
  const R = 84;                       // main node radius (bigger — more use of space)

  const NODES = {
    solar: { x: 380, y: 116, color: "#eab308", label: "Solar" },
    grid:  { x: 116, y: 300, color: "#0ea5e9", label: "Grid" },
    house: { x: 642, y: 300, color: "#ec4899", label: "House" },
    batt:  { x: 380, y: 484, color: "#22c55e", label: "Battery" },
    ev:    { x: 642, y: 116, color: "#6b8e57", label: "EV", r: 58 },
    gas:   { x: 642, y: 470, color: "#dc2626", label: "Gas", r: 54 },
  };

  const ICON = {
    solar: '<g transform="translate(-16,-16) scale(1.33)"><circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="5" y1="5" x2="7" y2="7"/><line x1="17" y1="17" x2="19" y2="19"/><line x1="19" y1="5" x2="17" y2="7"/><line x1="7" y1="17" x2="5" y2="19"/></g></g>',
    grid: '<g transform="translate(-15,-16) scale(1.3)"><path d="M6 2 L18 2 L15 22 L9 22 Z" fill="none" stroke="currentColor" stroke-width="1.5"/><line x1="6" y1="8" x2="18" y2="8" stroke="currentColor" stroke-width="1.3"/><line x1="9" y1="2" x2="15" y2="22" stroke="currentColor" stroke-width="1.1"/><line x1="15" y1="2" x2="9" y2="22" stroke="currentColor" stroke-width="1.1"/></g>',
    house: '<g transform="translate(-16,-15) scale(1.33)"><path d="M3 11 L12 3 L21 11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M5 10 V21 H19 V10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></g>',
    batt: '<g transform="translate(-12,-17) scale(1.4)"><rect x="5" y="3" width="10" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="8.5" y="1.5" width="3" height="2" fill="currentColor"/></g>',
    ev: '<g transform="translate(-15,-11) scale(1.25)"><path d="M3 13 L5 8 H19 L21 13 V18 H3 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><circle cx="7.5" cy="18" r="1.8" fill="currentColor"/><circle cx="16.5" cy="18" r="1.8" fill="currentColor"/></g>',
    gas: '<g transform="translate(-11,-13) scale(1.1)"><path d="M11 2 C13 6 16 7 16 12 a5 5 0 0 1 -10 0 C6 9 8 8 8 6 C9 7 10 7 11 6 C11 4 11 3 11 2 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></g>',
  };

  const LINE_COUNT = { solar: 1, grid: 2, house: 1, batt: 1, ev: 1, gas: 0 };

  // Directed flow edges (source -> sink). Colour is the SOURCE node's colour and
  // never changes; `bow` curves the path so crossing edges separate visually.
  const FLOW_EDGES = [
    { key: "s_house", a: "solar", b: "house", bow: 22 },
    { key: "s_batt",  a: "solar", b: "batt",  bow: 30 },
    { key: "s_grid",  a: "solar", b: "grid",  bow: 22 },
    { key: "g_house", a: "grid",  b: "house", bow: 40 },
    { key: "g_batt",  a: "grid",  b: "batt",  bow: -18 },
    { key: "b_house", a: "batt",  b: "house", bow: -22 },
    { key: "b_grid",  a: "batt",  b: "grid",  bow: 18 },
    { key: "h_ev",    a: "house", b: "ev",    bow: 0 },
  ];

  const fmtW = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const a = Math.abs(w);
    if (a < 1) return "0 W";
    return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
  };
  const kwh = (v) => (v == null ? "" : Number(v).toFixed(2) + " kWh");

  function durFor(mag) {
    mag = Math.abs(mag);
    if (mag < 300) return "3.6";
    if (mag < 1500) return "3.0";
    if (mag < 5000) return "2.4";
    return "1.8";
  }

  // Curved path from the edge of node A to the edge of node B, bowed by `bow`.
  function pairPath(aKey, bKey, bow) {
    const A = NODES[aKey], B = NODES[bKey];
    const rA = A.r || R, rB = B.r || R;
    const dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const sx = A.x + ux * rA, sy = A.y + uy * rA;
    const ex = B.x - ux * rB, ey = B.y - uy * rB;
    const mx = (sx + ex) / 2 - uy * bow, my = (sy + ey) / 2 + ux * bow;
    return `M${sx.toFixed(1)},${sy.toFixed(1)} Q${mx.toFixed(1)},${my.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`;
  }

  // Midpoint of the bowed quadratic path (for placing the watt label).
  function pairMid(aKey, bKey, bow) {
    const A = NODES[aKey], B = NODES[bKey];
    const rA = A.r || R, rB = B.r || R;
    const dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const sx = A.x + ux * rA, sy = A.y + uy * rA;
    const ex = B.x - ux * rB, ey = B.y - uy * rB;
    return { x: (sx + ex) / 2 - uy * bow * 0.5, y: (sy + ey) / 2 + ux * bow * 0.5 };
  }

  // ---- static markup builders (run once per structure) ----------------------
  function edgeSvg(e, dur) {
    const col = NODES[e.a].color, d = pairPath(e.a, e.b, e.bow), m = pairMid(e.a, e.b, e.bow);
    // Thicker "ribbon" connector (Domoticz style) under the flow dots.
    let s = `<path id="pf-base-${e.key}" d="${d}" fill="none" stroke="${col}" stroke-width="7" stroke-linecap="round" opacity="0"/>`;
    for (let i = 0; i < 2; i++) {
      const begin = (-i * parseFloat(dur) / 2).toFixed(2);
      s += `<circle id="pf-dot-${e.key}-${i}" r="5" fill="${col}" opacity="0">`
         + `<animateMotion id="pf-anim-${e.key}-${i}" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"`
         + ` calcMode="linear" keyPoints="0;1" keyTimes="0;1" path="${d}"/></circle>`;
    }
    // Watt label at the connector midpoint, with a panel-coloured halo so it reads
    // over the ribbon. Shown only while the flow is active.
    s += `<text id="pf-elabel-${e.key}" x="${m.x.toFixed(1)}" y="${m.y.toFixed(1)}" `
       + `text-anchor="middle" dominant-baseline="middle" font-size="12" font-weight="600" `
       + `fill="var(--text)" opacity="0" style="paint-order:stroke;stroke:var(--panel-2);stroke-width:3px;">—</text>`;
    return s;
  }

  function nodeSvg(key, active, big, lines) {
    const n = NODES[key], r = n.r || R;
    let t = `<circle id="pf-ring-${key}" cx="${n.x}" cy="${n.y}" r="${r}" fill="var(--panel-2)" stroke="${active ? n.color : "var(--line)"}" stroke-width="3"/>`;
    t += `<text x="${n.x}" y="${n.y - r - 9}" text-anchor="middle" font-size="13" fill="var(--muted)">${n.label}</text>`;
    t += `<g id="pf-icon-${key}" transform="translate(${n.x},${n.y - 26})" color="${active ? n.color : "var(--muted)"}">${ICON[key]}</g>`;
    // Smaller power figure, larger sub-text (easier to read at a glance).
    t += `<text id="pf-${key}-big" x="${n.x}" y="${n.y + 20}" text-anchor="middle" font-size="19" font-weight="700" fill="var(--text)">${big}</text>`;
    const nLines = LINE_COUNT[key] || 0;
    for (let i = 0; i < nLines; i++) {
      const ln = (lines && lines[i]) || "";
      t += `<text id="pf-${key}-l${i}" x="${n.x}" y="${n.y + 42 + i * 18}" text-anchor="middle" font-size="14" fill="var(--muted)">${ln}</text>`;
    }
    return t;
  }

  let _struct = null;            // signature of which optional nodes exist
  const _edgeDur = {};           // key -> last applied dur (avoid needless restarts)

  // Decompose the net node powers into directed source→sink flows (W). Priority:
  // PV serves the house first, then charges the battery, then exports; the house
  // is then topped up from the battery, then the grid; battery charging is topped
  // up from the grid. This mirrors how a real ESS dispatches.
  function decompose(pv, grid, batt, load, ev) {
    const pvp = Math.max(0, pv);
    const gexp = Math.max(0, -grid);            // export to grid
    const bchg = Math.max(0, batt);             // battery charging (sink)
    const bdis = Math.max(0, -batt);            // battery discharging (source)
    const evW = ev > 0 ? ev : 0;
    const home = Math.max(0, load) + evW;       // EV is a house load

    let pvLeft = pvp, homeLeft = home, chgLeft = bchg;
    const s_house = Math.min(pvLeft, homeLeft); pvLeft -= s_house; homeLeft -= s_house;
    const b_house = Math.min(bdis, homeLeft); homeLeft -= b_house;
    const g_house = homeLeft;                                  // remainder from grid
    const s_batt = Math.min(pvLeft, chgLeft); pvLeft -= s_batt; chgLeft -= s_batt;
    const g_batt = chgLeft;                                    // remainder from grid
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
    const gasM3 = today.gas_m3;
    const soc = live.soc;
    const A = (w) => isFinite(w) && Math.abs(w) > 15;     // active threshold

    const flow = decompose(pv, grid, batt, load, ev || 0);

    // Per-edge magnitude + speed this frame.
    const edgeMag = (e) => flow[e.key] || 0;
    const edges = FLOW_EDGES.filter((e) => e.key !== "h_ev" || ev != null);

    // A node is lit when it is producing or consuming power.
    const nodeActive = {
      solar: A(pv) && pv > 0,
      grid: A(grid),
      house: A(load),
      batt: A(batt),
      ev: ev != null && A(ev),
      gas: gasM3 != null && Number(gasM3) > 0,
    };

    const gImp = today.grid_import_kwh, gExp = today.grid_export_kwh;
    const labels = {
      "pf-solar-big": fmtW(pv), "pf-solar-l0": kwh(today.solar_kwh),
      "pf-grid-big": fmtW(grid),
      "pf-grid-l0": gImp != null ? `⇢ ${Number(gImp).toFixed(2)} kWh` : "",
      "pf-grid-l1": gExp != null ? `⇠ ${Number(gExp).toFixed(2)} kWh` : "",
      "pf-house-big": fmtW(load), "pf-house-l0": kwh(today.consumption_kwh),
      "pf-batt-big": fmtW(batt), "pf-batt-l0": (soc != null ? Number(soc).toFixed(0) + "% SoC" : "—"),
    };
    if (ev != null) { labels["pf-ev-big"] = fmtW(ev); labels["pf-ev-l0"] = kwh(today.ev_kwh); }
    if (gasM3 != null) labels["pf-gas-big"] = `${Number(gasM3).toFixed(2)} m³`;

    const structSig = `${ev != null ? 1 : 0}|${gasM3 != null ? 1 : 0}`;

    // ---- fast path: structure unchanged -> mutate attributes in place --------
    if (structSig === _struct && box.querySelector("svg")) {
      edges.forEach((e) => {
        const mag = edgeMag(e), on = A(mag), dur = durFor(mag);
        const base = box.querySelector("#pf-base-" + e.key);
        if (base) base.setAttribute("opacity", on ? 0.3 : 0);
        for (let i = 0; i < 2; i++) {
          const dot = box.querySelector("#pf-dot-" + e.key + "-" + i);
          if (dot) dot.setAttribute("opacity", on ? 1 : 0);
        }
        const lbl = box.querySelector("#pf-elabel-" + e.key);
        if (lbl) { lbl.setAttribute("opacity", on ? 1 : 0); if (on) lbl.textContent = fmtW(mag); }
        if (on && _edgeDur[e.key] !== dur) {
          for (let i = 0; i < 2; i++) {
            const anim = box.querySelector("#pf-anim-" + e.key + "-" + i);
            if (anim) anim.setAttribute("dur", dur + "s");
          }
          _edgeDur[e.key] = dur;
        }
      });
      for (const key in nodeActive) {
        const ring = box.querySelector("#pf-ring-" + key);
        if (ring) ring.setAttribute("stroke", nodeActive[key] ? NODES[key].color : "var(--line)");
        const icon = box.querySelector("#pf-icon-" + key);
        if (icon) icon.setAttribute("color", nodeActive[key] ? NODES[key].color : "var(--muted)");
      }
      for (const id in labels) {
        const el = box.querySelector("#" + id);
        if (el) el.textContent = labels[id];
      }
      return;
    }

    // ---- full (re)build: optional-node membership changed -------------------
    _struct = structSig;
    edges.forEach((e) => { _edgeDur[e.key] = durFor(edgeMag(e)); });

    let gasLink = "";
    const nodes = [
      nodeSvg("solar", nodeActive.solar, labels["pf-solar-big"], [labels["pf-solar-l0"]]),
      nodeSvg("grid", nodeActive.grid, labels["pf-grid-big"], [labels["pf-grid-l0"], labels["pf-grid-l1"]]),
      nodeSvg("house", nodeActive.house, labels["pf-house-big"], [labels["pf-house-l0"]]),
      nodeSvg("batt", nodeActive.batt, labels["pf-batt-big"], [labels["pf-batt-l0"]]),
    ];
    if (ev != null) nodes.push(nodeSvg("ev", nodeActive.ev, labels["pf-ev-big"], [labels["pf-ev-l0"]]));
    if (gasM3 != null) {
      const h = NODES.house, gn = NODES.gas, gr = gn.r || R;
      const ddx = gn.x - h.x, ddy = gn.y - h.y, dl = Math.hypot(ddx, ddy) || 1;
      const ux = ddx / dl, uy = ddy / dl;
      gasLink = `<line id="pf-gaslink" x1="${(h.x + ux * R).toFixed(1)}" y1="${(h.y + uy * R).toFixed(1)}" x2="${(gn.x - ux * gr).toFixed(1)}" y2="${(gn.y - uy * gr).toFixed(1)}" stroke="${gn.color}" stroke-width="3" opacity="0.4" stroke-linecap="round"/>`;
      nodes.push(nodeSvg("gas", nodeActive.gas, labels["pf-gas-big"], []));
    }

    box.innerHTML = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="live power flow">
        ${gasLink}
        ${edges.map((e) => edgeSvg(e, _edgeDur[e.key])).join("")}
        ${nodes.join("")}
      </svg>`;

    // Apply this frame's active/opacity state to the freshly built structure.
    edges.forEach((e) => {
      const mag = edgeMag(e), on = A(mag);
      const base = box.querySelector("#pf-base-" + e.key);
      if (base) base.setAttribute("opacity", on ? 0.3 : 0);
      for (let i = 0; i < 2; i++) {
        const dot = box.querySelector("#pf-dot-" + e.key + "-" + i);
        if (dot) dot.setAttribute("opacity", on ? 1 : 0);
      }
      const lbl = box.querySelector("#pf-elabel-" + e.key);
      if (lbl) { lbl.setAttribute("opacity", on ? 1 : 0); if (on) lbl.textContent = fmtW(mag); }
    });
  };
})();
