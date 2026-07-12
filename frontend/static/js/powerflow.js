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

  const NODE_LABEL = { grid: "Grid", inv: "MultiPlus-II", house: "AC Loads", solar: "Solar", batt: "Battery", ev: "EV", gas: "Gas" };

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

  // Animated physical links (source-coloured dots). `ap`/`bp` = [side, offsetFrac];
  // when omitted, attach sides are auto-picked from the live layout, so the same
  // edges work for the desktop 3-column AND the narrow 2-column mobile arrangement.
  // EV/Gas keep an explicit bottom-of-AC-Loads attach with a splay offset.
  const FLOW_EDGES = [
    { key: "grid",  a: "grid",  b: "inv",   color: PALETTE.grid },
    { key: "load",  a: "inv",   b: "house", color: PALETTE.house },
    { key: "batt",  a: "inv",   b: "batt",  color: PALETTE.batt },
    { key: "solar", a: "solar", b: "batt",  color: PALETTE.solar },
    { key: "ev",    a: "house", ap: ["bottom", -0.26], b: "ev", bp: ["top", 0], color: PALETTE.ev },
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

  const MOBILE_MAX = 600;   // container width (px) below which the 2-column layout kicks in

  function layout(W, H, hasEV, hasGas) {
    return W < MOBILE_MAX ? layoutMobile(W, H, hasEV, hasGas) : layoutDesktop(W, H, hasEV, hasGas);
  }

  // Desktop / wide: Victron-style proportions — wider central column, EV+Gas small.
  function layoutDesktop(W, H, hasEV, hasGas) {
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

  // Narrow / phone: 2×2 corner cards (Grid/AC-Loads on top, Battery/Solar on the
  // bottom) around a central MP-II hub — echoing the Victron VRM advanced view — with
  // EV + a small Gas card below the fold (Gas bottom-centre). Connectors use the
  // explicit MOBILE_PORTS below so the links radiate cleanly from the hub.
  //     Grid          AC Loads
  //            [ MP-II ]
  //     Battery       Solar
  //     · · · fold · · ·
  //          EV    Gas(small)
  function layoutMobile(W, H, hasEV, hasGas) {
    const xL = 0.225 * W, xR = 0.775 * W, colW = 0.41 * W;   // small side margins, wider cards
    // Battery + Solar carry extra BMS/string detail rows, so they're taller than the
    // Grid/AC-Loads cards; the whole stack is scaled to fill H below.
    const ch = 104, batth = 190, mph = 100, evh = 74, solh = 196, gash = 54;
    // Left column: Grid, Battery. Right column: AC Loads, then EV stacked above a
    // dropped-down-and-right Solar (so its line to Battery can curve). MP-II hub
    // centred; Gas centred at the bottom overflow. Heights are content-fit; the whole
    // stack is scaled to fill H.
    const ny1  = 12 + ch / 2;                               // top row (Grid / AC Loads)
    const nHub = ny1 + ch / 2 + 26 + mph / 2;               // hub
    const nR2  = nHub + mph / 2 + 26 + batth / 2;          // Battery (left, taller), 26 below hub
    const nEv  = nHub + mph / 2 + 26 + evh / 2;             // EV — top level with the hub cards (26 below hub)
    const nSol = nEv + evh / 2 + 20 + solh / 2;             // Solar just below EV
    // Gas is tucked into the right column's AC-Loads→EV gap (placed below), so the
    // stack now ends at Battery (left) / Solar (right); the taller of those scales it.
    const Tnat = Math.max(nR2 + batth / 2, nSol + solh / 2) + 12;
    const sc = H / Tnat;
    const N = {
      grid:  { x: xL, y: ny1 * sc, w: colW, h: ch * sc },
      house: { x: xR, y: ny1 * sc, w: colW, h: ch * sc },
      inv:   { x: 0.5 * W, y: nHub * sc, w: 0.36 * W, h: mph * sc },
      batt:  { x: xL, y: nR2 * sc, w: colW, h: batth * sc },
      solar: { x: xR + 0.015 * W, y: nSol * sc, w: 0.40 * W, h: solh * sc },   // dropped down + right
    };
    if (hasEV)  N.ev  = { x: xR, y: nEv * sc, w: colW, h: evh * sc };          // stacked above Solar
    if (hasGas) {
      // Gas nestled in the AC-Loads→EV gap, right-aligned with that column (right edge
      // at xR + colW/2) so there's a balanced gap to the hub on its left — matching how
      // the other cards sit off the centre hub.
      const nGasY = (ny1 + ch / 2 + (nEv - evh / 2)) / 2;
      const gasW = 0.22 * W;
      N.gas = { x: xR + colW / 2 - gasW / 2, y: nGasY * sc, w: gasW, h: gash * sc };
    }
    return N;
  }

  // Auto-pick attach sides from the two boxes' relative position (horizontal bias so
  // same-row cards connect side-to-side and stacked cards connect top/bottom).
  function autoPorts(ra, rb) {
    const dx = rb.x - ra.x, dy = rb.y - ra.y;
    if (Math.abs(dx) * 1.7 >= Math.abs(dy))
      return dx >= 0 ? [["right", 0], ["left", 0]] : [["left", 0], ["right", 0]];
    return dy >= 0 ? [["bottom", 0], ["top", 0]] : [["top", 0], ["bottom", 0]];
  }

  // Explicit attach ports for the mobile hub layout, so the four links radiate
  // cleanly from the central MP-II instead of being auto-routed across cards.
  // Mobile attach ports: connect at the CENTRE of each card's side for uniform, longer
  // arcs — the only exception is Grid + AC Loads meeting the MP2 hub side-by-side at its
  // top (40% / 60% positions).
  const MOBILE_PORTS = {
    grid:  { ap: ["bottom", 0],  bp: ["top", -0.1] },   // Grid bottom-centre → MP2 top 40%
    load:  { ap: ["top", 0.1],   bp: ["left", 0] },     // MP2 top 60% → AC-Loads left-centre
    batt:  { ap: ["bottom", 0],  bp: ["top", 0] },      // MP2 bottom-centre → Battery top-centre
    solar: { ap: ["left", 0],    bp: ["right", 0] },    // Solar left-centre → Battery right-centre
    ev:    { ap: ["bottom", 0],  bp: ["top", 0] },      // AC-Loads bottom-centre → EV top-centre
  };

  // Width-aware (so narrow cards don't overflow) with an optional scale `k`
  // (mobile passes k<1 to shrink fonts/labels a touch — desktop uses k=1, unchanged).
  function boxFonts(r, k) {
    k = k || 1;
    const fit = (a, b, lo, hi) => clamp(Math.min(a, b) * k, lo, hi);
    return {
      title: fit(r.h * 0.11, r.w * 0.135, 8, 17),
      big: fit(r.h * 0.22, r.w * 0.17, 11, 46),
      row: fit(r.h * 0.085, r.w * 0.11, 7.5, 15),
      state: fit(r.w * 0.115, r.h * 0.18, 12, 34),
      iscale: clamp(Math.min(r.h, r.w) * 0.11 / 17 * k, 0.38, 0.8),
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
  function edgePath(e, N, mobile) {
    let ap = e.ap, bp = e.bp;
    if (mobile && MOBILE_PORTS[e.key]) { ap = MOBILE_PORTS[e.key].ap; bp = MOBILE_PORTS[e.key].bp; }
    if (!ap || !bp) { const a = autoPorts(N[e.a], N[e.b]); ap = ap || a[0]; bp = bp || a[1]; }
    return pathBetween(port(N[e.a], ap), port(N[e.b], bp));
  }

  function txt(id, x, y, o, content) {
    o = o || {};
    const idAttr = id ? ` id="${id}"` : "";
    const weight = o.weight ? ` font-weight="${o.weight}"` : "";
    const sz = typeof o.size === "number" ? o.size.toFixed(1) : (o.size || 14);
    return `<text${idAttr} x="${f(x)}" y="${f(y)}" text-anchor="${o.anchor || "start"}" font-size="${sz}"${weight} fill="${o.fill || "var(--text)"}">${content || ""}</text>`;
  }

  // Source colours for the flow particles (provenance).
  const SRC = { solar: PALETTE.solar, grid: PALETTE.grid, batt: PALETTE.batt };
  const PF_DOTS = 2;   // particles per link (coloured by source to show provenance)

  // Logical source→sink decomposition: where each sink's power actually comes from.
  // PV serves the house first, then charges the battery, then exports; the house is
  // topped up from the battery, then grid; battery charging is topped up from grid.
  function decompose(pv, grid, batt, load) {
    let pvLeft = Math.max(0, pv), homeLeft = Math.max(0, load), chgLeft = Math.max(0, batt);
    const gexp = Math.max(0, -grid), bdis = Math.max(0, -batt);
    const s_house = Math.min(pvLeft, homeLeft); pvLeft -= s_house; homeLeft -= s_house;
    const b_house = Math.min(bdis, homeLeft); homeLeft -= b_house;
    const g_house = homeLeft;
    const s_batt = Math.min(pvLeft, chgLeft); pvLeft -= s_batt; chgLeft -= s_batt;
    const g_batt = chgLeft;
    const s_grid = Math.min(pvLeft, gexp); pvLeft -= s_grid;
    const b_grid = Math.max(0, gexp - s_grid);
    return { s_house, s_batt, s_grid, b_house, b_grid, g_house, g_batt };
  }

  // Colour each of `n` particles on a link by its source mix (proportional segments),
  // so a link fed by two sources shows both colours intermingled.
  function dotColors(sources, n) {
    const list = (sources || []).filter((s) => s.m > 0);
    const total = list.reduce((s, x) => s + x.m, 0);
    if (!total) return new Array(n).fill("var(--muted)");
    const out = [];
    for (let i = 0; i < n; i++) {
      const frac = (i + 0.5) / n;
      let cum = 0, col = list[0].c;
      for (const s of list) { col = s.c; cum += s.m / total; if (frac <= cum) break; }
      out.push(col);
    }
    return out;
  }

  function edgeSvg(e, N, dur, fwd, mobile) {
    const d = edgePath(e, N, mobile), kp = fwd ? "0;1" : "1;0";
    // Visible per-link colour trace (the "wire"); source-coloured particles ride on it.
    let s = `<path id="pf-base-${e.key}" d="${d}" fill="none" stroke="${e.color}" stroke-width="5.5" stroke-linecap="round" opacity="0.14"/>`;
    for (let i = 0; i < PF_DOTS; i++) {
      const begin = (-i * parseFloat(dur) / PF_DOTS).toFixed(2);
      s += `<circle id="pf-dot-${e.key}-${i}" r="5" fill="${e.color}" opacity="0">`
         + `<animateMotion id="pf-anim-${e.key}-${i}" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"`
         + ` calcMode="linear" keyPoints="${kp}" keyTimes="0;1" path="${d}"/></circle>`;
    }
    return s;
  }
  const gasSvg = (N) => `<path id="pf-base-gas" d="${edgePath(GAS_EDGE, N)}" fill="none" stroke="${PALETTE.gas}" stroke-width="5.5" stroke-linecap="round" opacity="0.12"/>`;

  function buildCard(key, N, mobile) {
    const r = N[key]; if (!r) return "";
    const F = boxFonts(r, mobile ? 0.8 : 1), x0 = r.x - r.w / 2, y0 = r.y - r.h / 2;
    const pad = clamp(r.w * (mobile ? 0.05 : 0.06), mobile ? 5 : 8, mobile ? 12 : 20), L = x0 + pad, R = x0 + r.w - pad;
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
      s += txt(`pf-solar-big`, L, y0 + r.h * 0.40, { size: F.big, weight: 700 }, "—");
      s += txt(`pf-solar-sub`, L, y0 + r.h * 0.53, { size: F.row, fill: "var(--muted)" }, "");
      [["Forecast", "pf-solar-forecast"], ["A", "pf-solar-a"], ["B", "pf-solar-b"], ["C", "pf-solar-c"],
       ["Amps", "pf-solar-amps"], ["Surplus", "pf-solar-surplus"]].forEach(([lab, id], i) => {
        const y = y0 + r.h * 0.62 + i * (r.h * 0.062);
        s += txt(null, L, y, { size: F.row, fill: "var(--muted)" }, lab);
        s += txt(id, R, y, { size: F.row, anchor: "end" }, "—");
      });
    } else if (key === "batt") {
      s += txt(`pf-batt-temp`, R, y0 + pad + F.title + 1, { size: F.row, fill: "var(--muted)", anchor: "end" }, "");
      // Hero: SoC% (reduced) with the live charge/discharge power tight underneath —
      // the two headline metrics — then the state word.
      s += txt(`pf-batt-big`, L, y0 + r.h * 0.33, { size: F.big * 0.82, weight: 700 }, "—");
      s += txt(`pf-batt-power`, L, y0 + r.h * 0.46, { size: F.big * 0.44, weight: 700, fill: "var(--text)" }, "");
      s += txt(`pf-batt-state`, L, y0 + r.h * 0.565, { size: F.row, fill: "var(--muted)" }, "");
      // BMS detail rows (label left / value right); min/max cell temps sit top-right.
      [["Voltage", "pf-batt-volt"], ["Current", "pf-batt-curr"], ["Min / Max (V)", "pf-batt-cells"],
       ["Capacity", "pf-batt-cap"], ["Modules Online", "pf-batt-mods"]].forEach(([lab, id], i) => {
        const y = y0 + r.h * 0.665 + i * (r.h * 0.072);
        s += txt(null, L, y, { size: F.row, fill: "var(--muted)" }, lab);
        s += txt(id, R, y, { size: F.row, anchor: "end" }, "—");
      });
    } else if (key === "inv") {
      s += txt(`pf-inv-big`, L, bigY + 4, { size: F.state, weight: 700 }, "—");
    } else if (key === "ev") {
      s += txt(`pf-ev-big`, L, y0 + r.h * 0.26, { size: F.big, weight: 700 }, "—");
      s += txt(`pf-ev-energy`, L, y0 + r.h * 0.40, { size: F.row, fill: "var(--muted)" }, "");
      // Tesla detail: SoC / charge limit / measured amps / ETA-to-limit (label left, value right).
      [["SoC", "pf-ev-soc"], ["Limit", "pf-ev-limit"], ["Amps", "pf-ev-amps"], ["ETA", "pf-ev-eta"]].forEach(([lab, id], i) => {
        const y = y0 + r.h * 0.55 + i * (r.h * 0.10);
        s += txt(null, L, y, { size: F.row, fill: "var(--muted)" }, lab);
        s += txt(id, R, y, { size: F.row, anchor: "end" }, "—");
      });
    } else if (key === "gas") {
      s += txt(`pf-gas-big`, L, y0 + r.h * 0.58, { size: F.big, weight: 700 }, "—");
    }
    return s;
  }

  // Mobile-only VRM-style card: icon+name header → big value (large number, small
  // unit via tspans) → divider → compact labelled detail rows. Uses data we already
  // have. (Desktop keeps buildCard above, untouched.)
  const MOBILE_ROWS = {
    grid:  [["L1", "pf-grid-l1"], ["L2", "pf-grid-l2"], ["L3", "pf-grid-l3"]],
    house: [["L1", "pf-house-l1"], ["L2", "pf-house-l2"], ["L3", "pf-house-l3"]],
    batt:  [["Voltage", "pf-batt-volt"], ["Current", "pf-batt-curr"], ["Cells", "pf-batt-cells"],
            ["Temps", "pf-batt-temps"], ["Capacity", "pf-batt-cap"], ["Modules", "pf-batt-mods"]],
    solar: [["Today", "pf-solar-kwh"], ["Forecast", "pf-solar-forecast"], ["A", "pf-solar-a"],
            ["B", "pf-solar-b"], ["C", "pf-solar-c"], ["Amps", "pf-solar-amps"], ["Surplus", "pf-solar-surplus"]],
    ev:    [["SoC", "pf-ev-soc"], ["Limit", "pf-ev-limit"], ["Amps", "pf-ev-amps"],
            ["ETA", "pf-ev-eta"], ["Total", "pf-ev-energy"]],
  };
  function buildCardMobile(key, N) {
    const r = N[key]; if (!r) return "";
    const x0 = r.x - r.w / 2, y0 = r.y - r.h / 2;
    const pad = clamp(r.w * 0.07, 6, 12), L = x0 + pad, R = x0 + r.w - pad;
    // Fonts scale with the card's (content-fit) height so the rows fill it tightly.
    const nameF = clamp(Math.min(r.h * 0.115, r.w * 0.12), 9, 13.5);
    const bigF = clamp(Math.min(r.h * 0.24, r.w * 0.18), 14, 27);
    const unitF = Math.max(9, bigF * 0.52);
    const rowF = clamp(Math.min(r.h * 0.105, r.w * 0.105), 9, 12.5);
    const iscale = clamp(nameF / 15, 0.42, 0.6);
    let s = `<rect id="pf-card-${key}" x="${f(x0)}" y="${f(y0)}" width="${f(r.w)}" height="${f(r.h)}" rx="12" fill="var(--panel-2)" stroke="var(--line)" stroke-width="2"/>`;

    // Centre hub: centred icon + name + state word.
    if (key === "inv") {
      s += `<g id="pf-icon-inv" transform="translate(${f(r.x)},${f(y0 + r.h * 0.34)}) scale(${(iscale * 1.6).toFixed(2)})" color="var(--muted)">${ICON.inv}</g>`;
      s += txt(null, r.x, y0 + r.h * 0.62, { size: nameF, anchor: "middle", fill: "var(--muted)" }, NODE_LABEL.inv);
      s += `<text id="pf-inv-bignum" x="${f(r.x)}" y="${f(y0 + r.h * 0.85)}" text-anchor="middle" font-size="${(bigF * 0.66).toFixed(1)}" font-weight="700" fill="var(--text)">—</text>`;
      return s;
    }

    // Header: icon + name (Battery's charge state + power now live in the hero below).
    s += `<g id="pf-icon-${key}" transform="translate(${f(x0 + pad + 7)},${f(y0 + pad + 7)}) scale(${iscale.toFixed(2)})" color="var(--muted)">${ICON[key]}</g>`;
    s += txt(null, x0 + pad + 17, y0 + pad + nameF, { size: nameF, fill: "var(--muted)" }, NODE_LABEL[key]);

    // Big value. Battery: SoC% (slightly reduced) with the live charge/discharge power
    // tight underneath — the two headline metrics — then a small state word. Every
    // other card keeps the standard big number + small unit.
    let heroBottom;
    if (key === "batt") {
      const bBig = bigF * 0.86;
      const bY = y0 + pad + nameF + bBig + 1;
      s += `<text x="${f(L)}" y="${f(bY)}" font-size="${bBig.toFixed(1)}" font-weight="700" fill="var(--text)">`
         + `<tspan id="pf-batt-bignum">—</tspan> `
         + `<tspan id="pf-batt-bigunit" font-size="${(bBig * 0.5).toFixed(1)}" font-weight="600" fill="var(--muted)"></tspan></text>`;
      const pY = bY + rowF + 4;
      s += txt(`pf-batt-power`, L, pY, { size: Math.max(rowF + 2, bBig * 0.5), weight: 700, fill: "var(--text)" }, "—");
      const stY = pY + rowF + 1;
      s += txt(`pf-batt-state`, L, stY, { size: rowF, fill: "var(--muted)" }, "");
      heroBottom = stY;
    } else {
      const bigY = y0 + pad + nameF + bigF + 2;
      s += `<text x="${f(L)}" y="${f(bigY)}" font-size="${bigF.toFixed(1)}" font-weight="700" fill="var(--text)">`
         + `<tspan id="pf-${key}-bignum">—</tspan> `
         + `<tspan id="pf-${key}-bigunit" font-size="${unitF.toFixed(1)}" font-weight="600" fill="var(--muted)"></tspan></text>`;
      heroBottom = bigY;
    }

    // Compact labelled detail rows.
    const rows = MOBILE_ROWS[key];
    if (rows && rows.length) {
      const divY = heroBottom + 5;
      s += `<line x1="${f(L)}" y1="${f(divY)}" x2="${f(R)}" y2="${f(divY)}" stroke="var(--line)"/>`;
      const startY = divY + rowF + 4, step = rowF + 3;   // tight line spacing
      rows.forEach(([lab, id], i) => {
        const y = startY + i * step;
        s += txt(null, L, y, { size: rowF, fill: "var(--muted)" }, lab);
        s += txt(id, R, y, { size: rowF, anchor: "end" }, "—");
      });
    }
    return s;
  }

  // ---- per-frame attribute application ---------------------------------------
  const _edgeDur = {}, _edgeDir = {};
  function applyEdges(box, flows) {
    for (const key in flows) {
      const fl = flows[key], on = A(fl.mag), dur = durFor(fl.mag), kp = fl.fwd ? "0;1" : "1;0";
      const base = box.querySelector("#pf-base-" + key);
      if (base) base.setAttribute("opacity", on ? 0.35 : 0.14);
      const cols = dotColors(fl.sources, PF_DOTS);   // per-particle source colours
      for (let i = 0; i < PF_DOTS; i++) {
        const dot = box.querySelector("#pf-dot-" + key + "-" + i);
        if (dot) { dot.setAttribute("opacity", on ? 1 : 0); if (on) dot.setAttribute("fill", cols[i]); }
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

    // Source-flow decomposition → provenance-coloured particles, and a flow-consistent
    // Inverter↔Battery direction: the link follows the net DC-bus flow (pv − batt_w),
    // NOT raw batt_w — so a solar surplus that's exporting correctly shows the DC bus
    // feeding the inverter *upward*, even while the battery itself trickle-charges.
    const D = decompose(pv || 0, grid || 0, batt || 0, load || 0);
    const evW = ev != null ? Math.max(0, ev) : 0;
    const invDc = (pv || 0) - (batt || 0);   // + = DC→inverter (up); − = inverter→DC (down, grid-charging)
    const flows = {
      grid: (grid || 0) >= 0
        ? { mag: Math.abs(grid || 0), fwd: true,  sources: [{ c: SRC.grid, m: D.g_house + D.g_batt }] }
        : { mag: Math.abs(grid || 0), fwd: false, sources: [{ c: SRC.solar, m: D.s_grid }, { c: SRC.batt, m: D.b_grid }] },
      load: { mag: Math.max(0, load || 0), fwd: true,
              sources: [{ c: SRC.solar, m: D.s_house }, { c: SRC.batt, m: D.b_house }, { c: SRC.grid, m: D.g_house }] },
      batt: invDc >= 0
        ? { mag: invDc,  fwd: false, sources: [{ c: SRC.solar, m: D.s_house + D.s_grid }, { c: SRC.batt, m: D.b_house + D.b_grid }] }
        : { mag: -invDc, fwd: true,  sources: [{ c: SRC.grid, m: D.g_batt }] },
      solar: { mag: Math.max(0, pv || 0), fwd: true, sources: [{ c: SRC.solar, m: Math.max(0, pv || 0) }] },
      ev:    { mag: evW, fwd: true, sources: [{ c: PALETTE.ev, m: evW }] },
    };
    const active = {
      grid: A(grid), house: A(load), solar: A(pv) && pv > 0, batt: A(batt),
      ev: ev != null && A(ev),
    };

    const chargeWord = A(batt) ? (batt > 0 ? "Charging" : "Discharging") : "Idle";
    let battState = chargeWord;
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

    const V = {};
    // Combined value (desktop pf-X-big) + split number/unit (mobile VRM tspans).
    const big = (key, str) => {
      V["pf-" + key + "-big"] = str;
      const m = /^(.*\S)\s(\S+)$/.exec(String(str));
      V["pf-" + key + "-bignum"] = m ? m[1] : str;
      V["pf-" + key + "-bigunit"] = m ? m[2] : "";
    };
    big("grid", gridBig(grid));
    V["pf-grid-l1"] = fmtWs(num(live.grid_l1)); V["pf-grid-l2"] = fmtWs(num(live.grid_l2)); V["pf-grid-l3"] = fmtWs(num(live.grid_l3));
    big("house", fmtW(load));
    V["pf-house-l1"] = fmtWs(num(live.load_l1)); V["pf-house-l2"] = fmtWs(num(live.load_l2)); V["pf-house-l3"] = fmtWs(num(live.load_l3));
    big("solar", fmtW(pv));
    V["pf-solar-sub"] = today.solar_kwh != null ? kwh(today.solar_kwh) + " today" : "";
    V["pf-solar-kwh"] = today.solar_kwh != null ? kwh(today.solar_kwh) : "—";
    big("batt", soc != null ? Math.round(soc) + " %" : "—");
    V["pf-batt-temp"] = live.batt_temp != null && isFinite(Number(live.batt_temp)) ? Math.round(Number(live.batt_temp)) + " °C" : "—";
    V["pf-batt-state"] = battState;
    V["pf-batt-charge"] = chargeWord;
    V["pf-batt-vaw"] = vaw;
    V["pf-batt-volt"] = live.batt_voltage != null && isFinite(Number(live.batt_voltage)) ? Number(live.batt_voltage).toFixed(2) + " V" : "—";
    V["pf-batt-curr"] = live.batt_current != null && isFinite(Number(live.batt_current)) ? Number(live.batt_current).toFixed(1) + " A" : "—";
    V["pf-batt-power"] = isFinite(batt) ? fmtW(Math.abs(batt)) : "—";
    // Battery pack detail (BMS service 512): cell V/temp extremes, Ah, modules.
    const _pair = (a, b, dp, unit) => (a != null && isFinite(Number(a)) && b != null && isFinite(Number(b)))
      ? Number(a).toFixed(dp) + " / " + Number(b).toFixed(dp) + " " + unit : "—";
    const cellTemps = _pair(live.batt_min_cell_t, live.batt_max_cell_t, 0, "°C");
    V["pf-batt-cells"] = _pair(live.batt_min_cell_v, live.batt_max_cell_v, 2, "V");
    V["pf-batt-temps"] = cellTemps;
    V["pf-batt-temp"] = cellTemps;   // repurpose the desktop top-right temp to min/max
    V["pf-batt-cap"] = _pair(live.batt_capacity, live.batt_installed_capacity, 0, "Ah");
    V["pf-batt-mods"] = live.batt_modules_online != null && isFinite(Number(live.batt_modules_online))
      ? String(Math.round(Number(live.batt_modules_online))) : "—";
    // Solar detail: per-string V·kW (A/B/C), total DC amps, day forecast, surplus.
    const _strn = (v, p) => {
      const vv = (v != null && isFinite(Number(v))) ? Math.round(Number(v)) + " V" : "—";
      const pp = (p != null && isFinite(Number(p)))
        ? (Math.abs(Number(p)) >= 100 ? (Number(p) / 1000).toFixed(2) + " kW" : Math.round(Number(p)) + " W") : "—";
      return vv + " · " + pp;
    };
    V["pf-solar-a"] = _strn(live.pv_a_v, live.pv_a_p);
    V["pf-solar-b"] = _strn(live.pv_b_v, live.pv_b_p);
    V["pf-solar-c"] = _strn(live.pv_c_v, live.pv_c_p);
    V["pf-solar-amps"] = live.pv_current != null && isFinite(Number(live.pv_current)) ? Number(live.pv_current).toFixed(1) + " A" : "—";
    // pv_projected_today is published in Wh (a known mislabel), so ÷1000 for kWh.
    V["pf-solar-forecast"] = live.pv_forecast_today != null && isFinite(Number(live.pv_forecast_today)) ? (Number(live.pv_forecast_today) / 1000).toFixed(1) + " kWh" : "—";
    V["pf-solar-surplus"] = live.pv_surplus_w != null && isFinite(Number(live.pv_surplus_w)) ? fmtW(Number(live.pv_surplus_w)) : "—";
    V["pf-inv-big"] = sysWord; V["pf-inv-bignum"] = sysWord; V["pf-inv-bigunit"] = "";
    if (ev != null) {
      big("ev", fmtW(ev));
      V["pf-ev-energy"] = fmtEnergy(num(live.ev_energy_kwh));
      // Tesla vehicle detail (from MQTT; no API cost). "—" when a field hasn't published.
      const _pct = (v) => (v != null && isFinite(Number(v))) ? Number(v).toFixed(0) + "%" : "—";
      const _amps = (v) => (v != null && isFinite(Number(v))) ? Number(v).toFixed(0) + " A" : "—";
      const _charging = live.veh_is_charging === true || String(live.veh_is_charging) === "True";
      V["pf-ev-soc"] = _pct(live.veh_soc);
      V["pf-ev-limit"] = _pct(live.veh_soc_limit);
      V["pf-ev-amps"] = _amps(live.veh_amps);
      V["pf-ev-eta"] = (_charging && live.veh_eta && live.veh_eta !== "N/A") ? String(live.veh_eta) : "—";
    }
    if (gasM3 != null) big("gas", gasM3.toFixed(2) + " m³");

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
    const sig = `${W < MOBILE_MAX ? "m" : "d"}${fr.hasEV ? 1 : 0}${fr.hasGas ? 1 : 0}|${Math.round(W / 8)}|${Math.round(H / 8)}`;

    if (sig === _sig && box.querySelector("svg")) {
      applyEdges(box, fr.flows);
      applyNodes(box, fr.active, fr.invCol, fr.gasOn);
      applyTexts(box, fr.V);
      return;
    }

    // ---- full (re)build ------------------------------------------------------
    _sig = sig;
    const mobile = W < MOBILE_MAX;
    const N = layout(W, H, fr.hasEV, fr.hasGas);
    // EV link is drawn when present (mobile shows AC-Loads→EV); Gas stays a card only.
    const edges = FLOW_EDGES.filter((e) => e.key !== "ev" || fr.hasEV);
    edges.forEach((e) => { _edgeDur[e.key] = durFor(fr.flows[e.key].mag); _edgeDir[e.key] = fr.flows[e.key].fwd ? "0;1" : "1;0"; });

    const cardKeys = ["grid", "inv", "house", "solar", "batt"];
    if (fr.hasEV) cardKeys.push("ev");
    if (fr.hasGas) cardKeys.push("gas");

    box.innerHTML = `
      <svg viewBox="0 0 ${f(W)} ${f(H)}" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" style="display:block" role="img" aria-label="live power flow">
        ${edges.map((e) => edgeSvg(e, N, _edgeDur[e.key], fr.flows[e.key].fwd, mobile)).join("")}
        ${!mobile && fr.hasGas ? gasSvg(N) : ""}
        ${cardKeys.map((k) => mobile ? buildCardMobile(k, N) : buildCard(k, N, false)).join("")}
      </svg>`;

    applyEdges(box, fr.flows);
    applyNodes(box, fr.active, fr.invCol, fr.gasOn);
    applyTexts(box, fr.V);
  };
})();
