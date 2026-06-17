"use strict";
// Live power-flow diagram: Solar / Grid / Battery / House (+ optional EV) with
// curved connectors and animated flow dots. Dots are coloured by the SOURCE of
// the energy (e.g. solar feeding the house shows yellow dots) and move in the
// real power direction. Dependency-free inline SVG (no CDN).
// window.renderPowerFlow(id, live, plan). Called in try/catch by app.js.
(function () {
  const VB_W = 760, VB_H = 620;
  const HUB = { x: 380, y: 310 };
  const HUB_R = 34;            // hub icon radius; connectors stop at its edge
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

  const fmtW = (w) => {
    if (w == null || !isFinite(w)) return "—";
    const a = Math.abs(w);
    if (a < 1) return "0 W";                 // show real small draws (e.g. EV 4 W)
    return a < 1000 ? Math.round(w) + " W" : (w / 1000).toFixed(2) + " kW";
  };
  const kwh = (v) => (v == null ? null : Number(v).toFixed(2) + " kWh");

  // Smooth single-curve path from a node's edge to the hub dot (so it visibly
  // connects). Path runs node (t=0) -> hub (t=1).
  function edgePath(key) {
    const n = NODES[key], r = n.r || R;
    const dx = HUB.x - n.x, dy = HUB.y - n.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const sx = n.x + ux * r, sy = n.y + uy * r;
    const ex = HUB.x - ux * HUB_R, ey = HUB.y - uy * HUB_R;
    const bow = 16;                                  // gentle perpendicular bow
    const mx = (sx + ex) / 2 - uy * bow, my = (sy + ey) / 2 + ux * bow;
    return `M${sx.toFixed(1)},${sy.toFixed(1)} Q${mx.toFixed(1)},${my.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`;
  }

  // base line (node-coloured, faint) + flow dots (source-coloured) moving in the
  // real direction. reverse=true means flow goes hub -> node.
  function connector(key, active, reverse, mag, dotColor) {
    const n = NODES[key];
    const d = edgePath(key);
    const base = `<path d="${d}" fill="none" stroke="${active ? n.color : "var(--line)"}" stroke-width="3" stroke-linecap="round" opacity="${active ? 0.4 : 0.45}"/>`;
    if (!active) return base;
    const dur = Math.max(1.8, 4.2 - Math.min(mag, 12000) / 3500).toFixed(2);  // slower overall
    const kp = reverse ? "1;0" : "0;1";
    let dots = "";
    for (let i = 0; i < 2; i++) {                    // fewer dots
      dots += `<circle r="6" fill="${dotColor || n.color}">
        <animateMotion dur="${dur}s" begin="${(-i * dur / 2).toFixed(2)}s" repeatCount="indefinite"
          calcMode="linear" keyPoints="${kp}" keyTimes="0;1" path="${d}"/></circle>`;
    }
    return base + dots;
  }

  function nodeSvg(key, opts) {
    const n = NODES[key], r = n.r || R;
    const ring = opts.active ? n.color : "var(--line)";
    const lines = (opts.lines || []).filter(Boolean);
    let texts = `<text x="${n.x}" y="${n.y - r - 8}" text-anchor="middle" font-size="13" fill="var(--muted)">${n.label}</text>`;
    texts += `<g transform="translate(${n.x},${n.y - 22})" color="${opts.active ? n.color : "var(--muted)"}">${ICON[key]}</g>`;
    if (opts.big) texts += `<text x="${n.x}" y="${n.y + 24}" text-anchor="middle" font-size="22" font-weight="700" fill="var(--text)">${opts.big}</text>`;
    lines.forEach((ln, i) => {
      texts += `<text x="${n.x}" y="${n.y + 46 + i * 17}" text-anchor="middle" font-size="12.5" fill="var(--muted)">${ln}</text>`;
    });
    return `<circle cx="${n.x}" cy="${n.y}" r="${r}" fill="var(--panel-2)" stroke="${ring}" stroke-width="3"/>${texts}`;
  }

  window.renderPowerFlow = function (containerId, live, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (!live || !live.connected) {
      box.innerHTML = '<span class="muted">live feed offline — connect to see real-time power flow.</span>';
      return;
    }
    const today = (plan && plan.today) || {};
    const pv = Number(live.pv_w), grid = Number(live.grid_w);
    const batt = Number(live.batt_w), load = Number(live.load_w);
    const ev = (live.ev_w != null && isFinite(Number(live.ev_w))) ? Number(live.ev_w) : null;
    const soc = live.soc;
    // "active" (animate flow) when there's a real draw — low threshold so small
    // constant loads like the EV's ~4 W standby still show dots flowing to them.
    const A = (w) => isFinite(w) && Math.abs(w) >= 2;

    // Dominant SOURCE colour right now (max of solar / grid-import / battery-
    // discharge), used to colour the dots on consuming edges so you can see, e.g.,
    // solar feeding the house and battery as yellow.
    const inflow = [];
    if (A(pv) && pv > 0) inflow.push(["solar", pv]);
    if (A(grid) && grid > 0) inflow.push(["grid", grid]);
    if (A(batt) && batt < 0) inflow.push(["batt", -batt]);
    inflow.sort((a, b) => b[1] - a[1]);
    const srcColor = inflow.length ? NODES[inflow[0][0]].color : NODES.house.color;

    const gImp = today.grid_import_kwh, gExp = today.grid_export_kwh;

    // reverse=true => flow runs hub -> node.
    const connectors = [
      connector("solar", A(pv), false, Math.abs(pv), NODES.solar.color),         // solar -> hub (source)
      connector("grid", A(grid), grid < 0, Math.abs(grid),                        // import: grid->hub (grid); export: hub->grid (source)
                grid > 0 ? NODES.grid.color : srcColor),
      connector("batt", A(batt), batt > 0, Math.abs(batt),                        // discharge: batt->hub (batt); charge: hub->batt (source)
                batt < 0 ? NODES.batt.color : srcColor),
      connector("house", A(load), true, Math.abs(load), srcColor),               // hub -> house (consumes; coloured by source)
    ];
    if (ev != null) connectors.push(connector("ev", A(ev), true, Math.abs(ev), srcColor)); // hub -> EV

    const nodes = [
      nodeSvg("solar", { active: A(pv), big: fmtW(pv), lines: [kwh(today.solar_kwh)] }),
      nodeSvg("grid", { active: A(grid), big: fmtW(grid),
        lines: [gImp != null ? `⇢ ${Number(gImp).toFixed(2)} kWh` : null,
                gExp != null ? `⇠ ${Number(gExp).toFixed(2)} kWh` : null] }),
      nodeSvg("house", { active: A(load), big: fmtW(load), lines: [kwh(today.consumption_kwh)] }),
      nodeSvg("batt", { active: A(batt), big: (soc != null ? Number(soc).toFixed(0) + "%" : "—"), lines: [fmtW(batt)] }),
    ];
    if (ev != null) nodes.push(nodeSvg("ev", { active: A(ev), big: fmtW(ev), lines: [kwh(today.ev_kwh)] }));

    // Gas is a consumption stat (m³, from Domoticz), not an electrical flow — show
    // it as a node statically linked to the house when a value is present.
    const gasM3 = today.gas_m3;
    let gasLink = "";
    if (gasM3 != null) {
      const h = NODES.house, gn = NODES.gas, gr = gn.r || R;
      const ddx = gn.x - h.x, ddy = gn.y - h.y, dl = Math.hypot(ddx, ddy) || 1;
      const ux = ddx / dl, uy = ddy / dl;
      gasLink = `<line x1="${(h.x + ux * R).toFixed(1)}" y1="${(h.y + uy * R).toFixed(1)}" x2="${(gn.x - ux * gr).toFixed(1)}" y2="${(gn.y - uy * gr).toFixed(1)}" stroke="${gn.color}" stroke-width="3" opacity="0.4" stroke-linecap="round"/>`;
      nodes.push(nodeSvg("gas", { active: gasM3 > 0, big: `${Number(gasM3).toFixed(2)} m³`, lines: [] }));
    }

    box.innerHTML = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="live power flow">
        ${gasLink}
        ${connectors.join("")}
        ${nodes.join("")}
        <g>
          <circle cx="${HUB.x}" cy="${HUB.y}" r="${HUB_R}" fill="var(--panel-2)" stroke="var(--accent)" stroke-width="3"/>
          <path transform="translate(${HUB.x},${HUB.y})" d="M-3,-15 L-11,3 L-2,3 L-5,15 L11,-4 L1,-4 Z"
            fill="#eab308" stroke="#eab308" stroke-width="1" stroke-linejoin="round"/>
        </g>
      </svg>`;
  };
})();
