"use strict";
// Trends tab: (a) SoC% + price line chart across the horizon (gradient area),
// and (b) HA-style energy metrics — self-sufficiency, self-consumed solar,
// grid balance. Dependency-free inline SVG (no CDN).
// Exposes window.renderHorizonChart(id, plan) and window.renderEnergyMetrics(id, plan).
// Self-contained; app.js calls each in try/catch so a failure is isolated.
(function () {
  // ---------- horizon chart ----------
  const VB_W = 940, VB_H = 360;
  const M = { l: 46, r: 54, t: 18, b: 36 };
  const PW = VB_W - M.l - M.r, PH = VB_H - M.t - M.b;

  function series(plan) {
    const out = [];
    (plan.hours || []).forEach((h) => (h.slots || []).forEach((s) => {
      const soc = Number(s.soc_end != null ? s.soc_end : s.soc_start);
      const price = Number(s.price);
      if (s.time && isFinite(soc) && isFinite(price)) out.push({ t: s.time, soc, price, current: !!s.is_current });
    }));
    return out;
  }
  const X = (i, n) => M.l + (n <= 1 ? 0 : (PW * i) / (n - 1));
  const Ysoc = (v) => M.t + PH - (PH * Math.max(0, Math.min(100, v))) / 100;

  window.renderHorizonChart = function (containerId, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (!plan || !plan.available) { box.innerHTML = '<span class="muted">no plan yet…</span>'; return; }
    const pts = series(plan);
    if (pts.length < 2) { box.innerHTML = '<span class="muted">not enough plan data to chart.</span>'; return; }
    const n = pts.length;
    const prices = pts.map((p) => p.price);
    const pMin = Math.min(...prices), pMax = Math.max(...prices), pSpan = (pMax - pMin) || 1;
    const Yp = (v) => M.t + PH - (PH * (v - pMin)) / pSpan;

    const socLine = pts.map((p, i) => `${i ? "L" : "M"}${X(i, n).toFixed(1)},${Ysoc(p.soc).toFixed(1)}`).join(" ");
    const socArea = `${socLine} L${X(n - 1, n).toFixed(1)},${(M.t + PH).toFixed(1)} L${X(0, n).toFixed(1)},${(M.t + PH).toFixed(1)} Z`;
    const priceLine = pts.map((p, i) => `${i ? "L" : "M"}${X(i, n).toFixed(1)},${Yp(p.price).toFixed(1)}`).join(" ");

    let grid = "";
    [0, 25, 50, 75, 100].forEach((g) => {
      const yy = Ysoc(g).toFixed(1);
      grid += `<line x1="${M.l}" y1="${yy}" x2="${M.l + PW}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;
      grid += `<text x="${M.l - 7}" y="${(Ysoc(g) + 3).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--muted)">${g}%</text>`;
    });
    [pMin, (pMin + pMax) / 2, pMax].forEach((v) => {
      grid += `<text x="${M.l + PW + 7}" y="${(Yp(v) + 3).toFixed(1)}" text-anchor="start" font-size="11" fill="var(--muted)">€${v.toFixed(2)}</text>`;
    });
    let xticks = "", nowMark = "", lastHour = null;
    pts.forEach((p, i) => {
      const hh = p.t.slice(11, 13);
      if (hh !== lastHour && Number(hh) % 3 === 0) {
        lastHour = hh;
        const xx = X(i, n).toFixed(1);
        xticks += `<text x="${xx}" y="${M.t + PH + 17}" text-anchor="middle" font-size="11" fill="var(--muted)">${p.t.slice(11, 16)}</text>`;
      }
      if (p.current) {
        const xx = X(i, n).toFixed(1);
        nowMark = `<line x1="${xx}" y1="${M.t}" x2="${xx}" y2="${M.t + PH}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3"/>
          <text x="${xx}" y="${M.t + 11}" text-anchor="middle" font-size="11" fill="var(--accent)">now</text>`;
      }
    });

    box.innerHTML = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="SoC and price across the horizon">
        <defs><linearGradient id="socFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--sell)" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="var(--sell)" stop-opacity="0.02"/>
        </linearGradient></defs>
        ${grid}${xticks}${nowMark}
        <path d="${socArea}" fill="url(#socFill)" stroke="none"/>
        <path d="${priceLine}" fill="none" stroke="var(--buy)" stroke-width="1.8" opacity="0.9"/>
        <path d="${socLine}" fill="none" stroke="var(--sell)" stroke-width="2.6"/>
      </svg>
      <div class="chart-legend muted">
        <span><span class="swatch" style="background:var(--sell)"></span> Battery SoC (%)</span>
        <span><span class="swatch" style="background:var(--buy)"></span> Buy price (€/kWh)</span>
      </div>`;
  };

  // ---------- HA-style energy metrics ----------
  function gauge(pct, color) {
    const r = 52, cx = 64, cy = 64;
    const P = (ang) => [cx + r * Math.cos(ang), cy - r * Math.sin(ang)];
    const f = Math.max(0, Math.min(100, pct == null ? 0 : pct)) / 100;
    const [sx, sy] = P(Math.PI), [bx, by] = P(0), [ex, ey] = P(Math.PI * (1 - f));
    return `<svg viewBox="0 0 128 84" width="128" height="84" aria-hidden="true">
      <path d="M${sx} ${sy} A${r} ${r} 0 0 1 ${bx} ${by}" fill="none" stroke="var(--line)" stroke-width="11" stroke-linecap="round"/>
      ${pct != null ? `<path d="M${sx} ${sy} A${r} ${r} 0 0 1 ${ex.toFixed(2)} ${ey.toFixed(2)}" fill="none" stroke="${color}" stroke-width="11" stroke-linecap="round"/>` : ""}
      <text x="${cx}" y="${cy - 2}" text-anchor="middle" font-size="22" font-weight="700" fill="var(--text)">${pct != null ? Math.round(pct) + "%" : "—"}</text>
    </svg>`;
  }

  window.renderEnergyMetrics = function (containerId, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    const t = (plan && plan.today) || {};
    const card = (inner) => `<div class="metric-card">${inner}</div>`;
    let html = "";

    // Self-sufficiency + self-consumed solar gauges.
    html += card(`${gauge(t.self_sufficiency_pct, "var(--retain)")}<div class="metric-label">Self-sufficiency</div>`);
    html += card(`${gauge(t.self_consumed_solar_pct, "var(--sell)")}<div class="metric-label">Self-consumed solar</div>`);

    // Grid balance bar (import blue vs export purple), net label.
    const imp = Number(t.grid_import_kwh || 0), exp = Number(t.grid_export_kwh || 0);
    const tot = (imp + exp) || 1;
    const impPct = (imp / tot * 100).toFixed(1), expPct = (exp / tot * 100).toFixed(1);
    const net = (imp - exp);
    html += card(`
      <div class="metric-label" style="margin-bottom:8px">Grid balance (today)</div>
      <div class="gridbar"><span style="width:${impPct}%;background:var(--buy)"></span><span style="width:${expPct}%;background:#a855f7"></span></div>
      <div class="gridbar-legend muted">
        <span><b style="color:var(--buy)">${imp.toFixed(2)}</b> import</span>
        <span><b style="color:#a855f7">${exp.toFixed(2)}</b> export</span>
        <span>net <b>${net >= 0 ? "+" : ""}${net.toFixed(2)} kWh</b></span>
      </div>`);

    box.innerHTML = html;
  };
})();
