"use strict";
// Live power-flow diagram: Solar / Grid / Battery / House (+ optional EV / Gas)
// with curved connectors and animated flow dots coloured by the source mix.
// Dependency-free inline SVG (no CDN). window.renderPowerFlow(id, live, plan).
//
// SMOOTHNESS MODEL: the SVG structure is built ONCE (all edges + their flow dots
// exist from the start and animate continuously, even when an edge is inactive —
// inactive dots are just opacity:0). Every live update then mutates attributes IN
// PLACE (opacity, fill, stroke, text). Crossing the activation threshold is a pure
// opacity toggle, so no <animateMotion> is ever recreated and the dots never
// freeze/jerk. The structure is only rebuilt when which OPTIONAL nodes exist
// (EV / Gas) changes — rare. dur / direction are touched per-edge only when that
// edge's own tier or flow direction actually changes.
(function () {
  const VB_W = 760, VB_H = 620;
  const HUB = { x: 380, y: 310 };
  const HUB_R = 34;
  const R = 78;

  const NODES = {
    solar: { x: 380, y: 120, color: "#eab308", label: "Solar" },
    grid:  { x: 120, y: 310, color: "#0ea5e9", label: "Grid" },
    house: { x: 640, y: 310, color: "#ec4899", label: "House" },
    batt:  { x: 380, y: 500, color: "#22c55e", label: "Battery" },
    ev:    { x: 638, y: 120, color: "#6b8e57", label: "EV", r: 56 },
    gas:   { x: 640, y: 488, color: "#dc2626", label: "Gas", r: 52 },
  };

  const ICON = {
    solar: '<g transform="translate(-16,-16) scale(1.33)"><circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="5" y1="5" x2="7" y2="7"/><line x1="17" y1="17" x2="19" y2="19"/><line x1="19" y1="5" x2="17" y2="7"/><line x1="7" y1="17" x2="5" y2="19"/></g></g>',
    grid: '<g transform="translate(-15,-16) scale(1.3)"><path d="M6 2 L18 2 L15 22 L9 22 Z" fill="none" stroke="currentColor" stroke-width="1.5"/><line x1="6" y1="8" x2="18" y2="8" stroke="currentColor" stroke-width="1.3"/><line x1="9" y1="2" x2="15" y2="22" stroke="currentColor" stroke-width="1.1"/><line x1="15" y1="2" x2="9" y2="22" stroke="currentColor" stroke-width="1.1"/></g>',
    house: '<g transform="translate(-16,-15) scale(1.33)"><path d="M3 11 L12 3 L21 11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M5 10 V21 H19 V10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></g>',
    batt: '<g transform="translate(-12,-17) scale(1.4)"><rect x="5" y="3" width="10" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="8.5" y="1.5" width="3" height="2" fill="currentColor"/></g>',
    ev: '<g transform="translate(-15,-11) scale(1.25)"><path d="M3 13 L5 8 H19 L21 13 V18 H3 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><circle cx="7.5" cy="18" r="1.8" fill="currentColor"/><circle cx="16.5" cy="18" r="1.8" fill="currentColor"/></g>',
    gas: '<g transform="translate(-11,-13) scale(1.1)"><path d="M11 2 C13 6 16 7 16 12 a5 5 0 0 1 -10 0 C6 9 8 8 8 6 C9 7 10 7 11 6 C11 4 11 3 11 2 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></g>',
  };

  // How many muted sub-lines each node reserves (rendered up-front so they can be
  // filled/cleared in place without changing structure).
  const LINE_COUNT = { solar: 1, grid: 2, house: 1, batt: 1, ev: 1, gas: 0 };

  const fmtW = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const a = Math.abs(w);
    if (a < 1) return "0 W";
    return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
  };
  const kwh = (v) => (v == null ? "" : Number(v).toFixed(2) + " kWh");

  // Discrete speed tiers (s per loop) so small magnitude wobble doesn't retune the
  // animation. Higher power -> faster dots.
  function durFor(mag) {
    mag = Math.abs(mag);
    if (mag < 300) return "3.6";
    if (mag < 1500) return "3.0";
    if (mag < 5000) return "2.4";
    return "1.8";
  }

  function edgePath(key) {
    const n = NODES[key], r = n.r || R;
    const dx = HUB.x - n.x, dy = HUB.y - n.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const sx = n.x + ux * r, sy = n.y + uy * r;
    const ex = HUB.x - ux * HUB_R, ey = HUB.y - uy * HUB_R;
    const bow = 16;
    const mx = (sx + ex) / 2 - uy * bow, my = (sy + ey) / 2 + ux * bow;
    return `M${sx.toFixed(1)},${sy.toFixed(1)} Q${mx.toFixed(1)},${my.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`;
  }

  // ---- static markup builders (run once per structure) ----------------------
  function edgeSvg(e) {
    const n = NODES[e.key], d = edgePath(e.key);
    const dir = e.reverse ? "1;0" : "0;1";
    let s = `<path id="pf-base-${e.key}" d="${d}" fill="none" stroke="${e.active ? n.color : "var(--line)"}" stroke-width="3" stroke-linecap="round" opacity="${e.active ? 0.4 : 0.45}"/>`;
    for (let i = 0; i < 2; i++) {
      const col = (e.colors && e.colors[i]) || n.color;
      const begin = (-i * parseFloat(e.dur) / 2).toFixed(2);
      s += `<circle id="pf-dot-${e.key}-${i}" r="6" fill="${col}" opacity="${e.active ? 1 : 0}">`
         + `<animateMotion id="pf-anim-${e.key}-${i}" dur="${e.dur}s" begin="${begin}s" repeatCount="indefinite"`
         + ` calcMode="linear" keyPoints="${dir}" keyTimes="0;1" path="${d}"/></circle>`;
    }
    return s;
  }

  function nodeSvg(key, active, big, lines) {
    const n = NODES[key], r = n.r || R;
    let t = `<circle id="pf-ring-${key}" cx="${n.x}" cy="${n.y}" r="${r}" fill="var(--panel-2)" stroke="${active ? n.color : "var(--line)"}" stroke-width="3"/>`;
    t += `<text x="${n.x}" y="${n.y - r - 8}" text-anchor="middle" font-size="13" fill="var(--muted)">${n.label}</text>`;
    t += `<g id="pf-icon-${key}" transform="translate(${n.x},${n.y - 22})" color="${active ? n.color : "var(--muted)"}">${ICON[key]}</g>`;
    t += `<text id="pf-${key}-big" x="${n.x}" y="${n.y + 24}" text-anchor="middle" font-size="22" font-weight="700" fill="var(--text)">${big}</text>`;
    const nLines = LINE_COUNT[key] || 0;
    for (let i = 0; i < nLines; i++) {
      const ln = (lines && lines[i]) || "";
      t += `<text id="pf-${key}-l${i}" x="${n.x}" y="${n.y + 46 + i * 17}" text-anchor="middle" font-size="12.5" fill="var(--muted)">${ln}</text>`;
    }
    return t;
  }

  let _struct = null;            // signature of which optional nodes exist
  const _edgeState = {};         // key -> { dur, dir } last applied (avoid needless restarts)

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
    // Active (animated flow + coloured ring) only above ±15 W.
    const A = (w) => isFinite(w) && Math.abs(w) > 15;

    // source mix for dot colouring
    const srcs = [];
    if (A(pv) && pv > 0) srcs.push([NODES.solar.color, pv]);
    if (A(grid) && grid > 0) srcs.push([NODES.grid.color, grid]);
    if (A(batt) && batt < 0) srcs.push([NODES.batt.color, -batt]);
    srcs.sort((a, b) => b[1] - a[1]);
    const two = (c) => [c, c];
    function mixDots(n) {
      if (!srcs.length) return Array(n).fill(NODES.house.color);
      const total = srcs.reduce((s, x) => s + x[1], 0) || 1;
      const out = [];
      srcs.forEach(([c, w]) => { for (let k = 0; k < Math.round(n * w / total); k++) out.push(c); });
      while (out.length < n) out.push(srcs[0][0]);
      return out.slice(0, n);
    }
    const cGrid = grid > 0 ? two(NODES.grid.color) : mixDots(2);
    const cBatt = batt < 0 ? two(NODES.batt.color) : mixDots(2);

    // per-edge desired state this frame
    const EDGES = [
      { key: "solar", active: A(pv),   colors: two(NODES.solar.color), dur: durFor(pv),   reverse: false },
      { key: "grid",  active: A(grid),  colors: cGrid,                 dur: durFor(grid), reverse: grid < 0 },
      { key: "batt",  active: A(batt),  colors: cBatt,                 dur: durFor(batt), reverse: batt > 0 },
      { key: "house", active: A(load),  colors: mixDots(2),            dur: durFor(load), reverse: true },
    ];
    if (ev != null) EDGES.push({ key: "ev", active: A(ev), colors: mixDots(2), dur: durFor(ev), reverse: true });

    const gImp = today.grid_import_kwh, gExp = today.grid_export_kwh;
    const labels = {
      "pf-solar-big": fmtW(pv), "pf-solar-l0": kwh(today.solar_kwh),
      "pf-grid-big": fmtW(grid),
      "pf-grid-l0": gImp != null ? `⇢ ${Number(gImp).toFixed(2)} kWh` : "",
      "pf-grid-l1": gExp != null ? `⇠ ${Number(gExp).toFixed(2)} kWh` : "",
      "pf-house-big": fmtW(load), "pf-house-l0": kwh(today.consumption_kwh),
      "pf-batt-big": (soc != null ? Number(soc).toFixed(0) + "%" : "—"), "pf-batt-l0": fmtW(batt),
    };
    if (ev != null) { labels["pf-ev-big"] = fmtW(ev); labels["pf-ev-l0"] = kwh(today.ev_kwh); }
    if (gasM3 != null) labels["pf-gas-big"] = `${Number(gasM3).toFixed(2)} m³`;

    // structure depends only on which OPTIONAL nodes are present
    const structSig = `${ev != null ? 1 : 0}|${gasM3 != null ? 1 : 0}`;

    // ---- fast path: structure unchanged -> mutate attributes in place --------
    if (structSig === _struct && box.querySelector("svg")) {
      EDGES.forEach((e) => {
        const n = NODES[e.key];
        const base = box.querySelector("#pf-base-" + e.key);
        if (base) { base.setAttribute("stroke", e.active ? n.color : "var(--line)"); base.setAttribute("opacity", e.active ? 0.4 : 0.45); }
        const ring = box.querySelector("#pf-ring-" + e.key);
        if (ring) ring.setAttribute("stroke", e.active ? n.color : "var(--line)");
        const icon = box.querySelector("#pf-icon-" + e.key);
        if (icon) icon.setAttribute("color", e.active ? n.color : "var(--muted)");
        const dir = e.reverse ? "1;0" : "0;1";
        const st = _edgeState[e.key] || {};
        for (let i = 0; i < 2; i++) {
          const dot = box.querySelector("#pf-dot-" + e.key + "-" + i);
          if (!dot) continue;
          dot.setAttribute("opacity", e.active ? 1 : 0);
          if (e.active) dot.setAttribute("fill", (e.colors && e.colors[i]) || n.color);
          const anim = box.querySelector("#pf-anim-" + e.key + "-" + i);
          if (anim) {
            // Only touch animation attrs when they truly change for THIS edge —
            // otherwise the dots restart. Most updates skip this entirely.
            if (st.dur !== e.dur) anim.setAttribute("dur", e.dur + "s");
            if (st.dir !== dir) anim.setAttribute("keyPoints", dir);
          }
        }
        _edgeState[e.key] = { dur: e.dur, dir };
      });
      for (const id in labels) {
        const el = box.querySelector("#" + id);
        if (el) el.textContent = labels[id];
      }
      return;
    }

    // ---- full (re)build: optional-node membership changed -------------------
    _struct = structSig;
    EDGES.forEach((e) => { _edgeState[e.key] = { dur: e.dur, dir: e.reverse ? "1;0" : "0;1" }; });

    let gasLink = "";
    const nodes = [
      nodeSvg("solar", A(pv), labels["pf-solar-big"], [labels["pf-solar-l0"]]),
      nodeSvg("grid", A(grid), labels["pf-grid-big"], [labels["pf-grid-l0"], labels["pf-grid-l1"]]),
      nodeSvg("house", A(load), labels["pf-house-big"], [labels["pf-house-l0"]]),
      nodeSvg("batt", A(batt), labels["pf-batt-big"], [labels["pf-batt-l0"]]),
    ];
    if (ev != null) nodes.push(nodeSvg("ev", A(ev), labels["pf-ev-big"], [labels["pf-ev-l0"]]));
    if (gasM3 != null) {
      const h = NODES.house, gn = NODES.gas, gr = gn.r || R;
      const ddx = gn.x - h.x, ddy = gn.y - h.y, dl = Math.hypot(ddx, ddy) || 1;
      const ux = ddx / dl, uy = ddy / dl;
      gasLink = `<line id="pf-gaslink" x1="${(h.x + ux * R).toFixed(1)}" y1="${(h.y + uy * R).toFixed(1)}" x2="${(gn.x - ux * gr).toFixed(1)}" y2="${(gn.y - uy * gr).toFixed(1)}" stroke="${gn.color}" stroke-width="3" opacity="0.4" stroke-linecap="round"/>`;
      nodes.push(nodeSvg("gas", gasM3 > 0, labels["pf-gas-big"], []));
    }

    box.innerHTML = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="live power flow">
        ${gasLink}
        ${EDGES.map(edgeSvg).join("")}
        ${nodes.join("")}
        <g transform="translate(${HUB.x},${HUB.y})">
          <circle r="${HUB_R}" fill="#ffffff"/>
          <path d="M3,-18 L-9,4 L-1,4 L-4,18 L9,-3 L1,-3 Z" fill="#0b0f14"/>
        </g>
      </svg>`;
  };
})();
