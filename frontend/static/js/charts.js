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
  const displayStyle = (on) => on ? "" : ` style="display:none"`;
  const offClass = (on) => on ? "" : " is-off";

  window.renderHorizonChart = function (containerId, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (!plan || !plan.available) { box.innerHTML = '<span class="muted">no plan yet…</span>'; return; }
    const pts = series(plan);
    if (pts.length < 2) { box.innerHTML = '<span class="muted">not enough plan data to chart.</span>'; return; }
    const showSoc = box.dataset.showSoc !== "0";
    const showPrice = box.dataset.showPrice !== "0";
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
        <path class="horizon-soc-area" d="${socArea}" fill="url(#socFill)" stroke="none"${displayStyle(showSoc)}/>
        <path class="horizon-price-line" d="${priceLine}" fill="none" stroke="var(--buy)" stroke-width="1.8" opacity="0.9"${displayStyle(showPrice)}/>
        <path class="horizon-soc-line" d="${socLine}" fill="none" stroke="var(--sell)" stroke-width="2.6"${displayStyle(showSoc)}/>
        <g id="hc-hover" style="display:none; pointer-events:none">
          <line id="hc-hline" x1="0" x2="0" y1="${M.t}" y2="${(M.t + PH).toFixed(1)}" stroke="var(--buy)" stroke-width="1" stroke-dasharray="3 3" opacity="0.7"/>
          <circle id="hc-hdot" r="4.5" fill="var(--buy)" stroke="#0b0f14" stroke-width="1.5"${displayStyle(showPrice)}/>
          <circle id="hc-sdot" r="4.5" fill="var(--sell)" stroke="#0b0f14" stroke-width="1.5"${displayStyle(showSoc)}/>
        </g>
        <rect x="${M.l}" y="${M.t}" width="${PW}" height="${PH}" fill="transparent" id="hc-hit"/>
      </svg>
      <div class="chart-legend muted">
        <span class="legend-toggle${offClass(showSoc)}" data-horizon-toggle="soc"><span class="swatch" style="background:var(--sell)"></span> Battery SoC (%)</span>
        <span class="legend-toggle${offClass(showPrice)}" data-horizon-toggle="price"><span class="swatch" style="background:var(--buy)"></span> Buy price (€/kWh)</span>
      </div>`;

    // ---- hover tooltip: nearest slot's buy price (+ SoC) ----
    const svg = box.querySelector("svg");
    if (!svg) return;
    box.style.position = "relative";
    const tip = document.createElement("div");
    tip.className = "chart-tip rich-tip";
    tip.style.display = "none";
    box.appendChild(tip);
    const hover = svg.querySelector("#hc-hover");
    const hline = svg.querySelector("#hc-hline");
    const hdot = svg.querySelector("#hc-hdot");
    const sdot = svg.querySelector("#hc-sdot");

    const dayLabel = (iso) => {
      const d = new Date(iso), now = new Date();
      const key = (x) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
      const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
      if (key(d) === key(now)) return "Today";
      if (key(d) === key(tmr)) return "Tomorrow";
      return isNaN(d) ? "" : d.toLocaleDateString(undefined, { weekday: "short" });
    };
    const idxFromEvent = (e) => {
      const ctm = svg.getScreenCTM();
      if (!ctm) return -1;
      const sp = svg.createSVGPoint();
      sp.x = e.clientX; sp.y = e.clientY;
      const u = sp.matrixTransform(ctm.inverse());
      if (u.x < M.l - 6 || u.x > M.l + PW + 6) return -1;
      const i = Math.round((u.x - M.l) / (PW / (n - 1)));
      return Math.max(0, Math.min(n - 1, i));
    };
    const onMove = (e) => {
      const i = idxFromEvent(e);
      if (i < 0) { hover.style.display = "none"; tip.style.display = "none"; return; }
      const p = pts[i], x = X(i, n), yPrice = Yp(p.price);
      hline.setAttribute("x1", x.toFixed(1)); hline.setAttribute("x2", x.toFixed(1));
      hdot.setAttribute("cx", x.toFixed(1)); hdot.setAttribute("cy", yPrice.toFixed(1));
      sdot.setAttribute("cx", x.toFixed(1)); sdot.setAttribute("cy", Ysoc(p.soc).toFixed(1));
      hover.style.display = "";
      tip.innerHTML = `<b>${dayLabel(p.t)} ${p.t.slice(11, 16)}</b>`
        + (showPrice ? `<span>€${p.price.toFixed(3)} /kWh</span>` : "")
        + (showSoc ? `<span>${Math.round(p.soc)}% SoC</span>` : "");
      tip.style.display = "block";
      const br = box.getBoundingClientRect();
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let tx = e.clientX - br.left + 14, ty = e.clientY - br.top - th - 10;
      if (tx + tw > br.width) tx = e.clientX - br.left - tw - 14;
      if (ty < 0) ty = e.clientY - br.top + 16;
      tip.style.left = `${Math.max(0, tx)}px`;
      tip.style.top = `${ty}px`;
    };
    svg.addEventListener("mousemove", onMove);
    svg.addEventListener("mouseleave", () => { hover.style.display = "none"; tip.style.display = "none"; });
    box.querySelectorAll("[data-horizon-toggle]").forEach((item) => {
      item.addEventListener("click", () => toggleHorizonSeries(containerId, item.dataset.horizonToggle, plan));
    });
  };

  function toggleHorizonSeries(containerId, which, plan) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (which === "soc") box.dataset.showSoc = box.dataset.showSoc === "0" ? "1" : "0";
    if (which === "price") box.dataset.showPrice = box.dataset.showPrice === "0" ? "1" : "0";
    window.renderHorizonChart(containerId, plan);
  }
  window.toggleHorizonSeries = toggleHorizonSeries;

  // ---------- Forecast accuracy overlay ----------
  window.renderForecastAccuracyChart = function (containerId, payload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    const pts = ((payload && payload.slots) || []).filter((p) => p && p.time);
    if (!pts.length) {
      box.innerHTML = '<span class="muted">no settled forecast accuracy yet…</span>';
      return;
    }

    const W = 940, H = 300, m = { l: 48, r: 18, t: 16, b: 34 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const showLoad = box.dataset.showLoad !== "0";
    const showPv = box.dataset.showPv !== "0";
    const nums = [];
    pts.forEach((p) => {
      ["predicted_load_kwh", "actual_load_kwh", "predicted_pv_kwh", "actual_pv_kwh"].forEach((k) => {
        const v = Number(p[k]);
        if (isFinite(v)) nums.push(v);
      });
    });
    let hi = Math.max(0.1, ...nums), lo = 0;
    if (hi <= lo) hi = lo + 1;
    const span = hi - lo, n = pts.length;
    const X = (i) => m.l + (n <= 1 ? pw / 2 : (pw * i) / (n - 1));
    const Y = (v) => m.t + ph - (ph * (v - lo)) / span;
    const parsedTimes = pts.map((p) => new Date(p.time)).map((d) => isNaN(d) ? null : d.getTime());
    const val = (p, k) => {
      const v = Number(p[k]);
      return isFinite(v) ? v : null;
    };
    const path = (k) => {
      let d = "", open = false;
      pts.forEach((p, i) => {
        const v = val(p, k);
        if (v == null) { open = false; return; }
        d += `${open ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)} `;
        open = true;
      });
      return d.trim();
    };

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const v = lo + (span * i) / 4, yy = Y(v).toFixed(1);
      grid += `<line x1="${m.l}" y1="${yy}" x2="${m.l + pw}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;
      grid += `<text x="${m.l - 7}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--muted)">${v.toFixed(1)}</text>`;
    }
    let xt = "", lastDay = "";
    const step = Math.max(1, Math.ceil(n / 8));
    pts.forEach((p, i) => {
      const d = new Date(p.time);
      const label = isNaN(d) ? (p.label || "") : d.toLocaleDateString(undefined, { weekday: "short" });
      if (i % step === 0 || i === n - 1 || label !== lastDay) {
        const time = isNaN(d) ? (p.label || "").slice(-5) : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
        xt += `<text x="${X(i).toFixed(1)}" y="${m.t + ph + 16}" text-anchor="middle" font-size="11" fill="var(--muted)">${label} ${time}</text>`;
        lastDay = label;
      }
    });

    let nowMark = "";
    const validTimes = parsedTimes.filter((t) => t != null);
    if (validTimes.length >= 2) {
      const minT = Math.min(...validTimes), maxT = Math.max(...validTimes);
      const slotMs = Math.max(1, (maxT - minT) / Math.max(1, validTimes.length - 1));
      const nowT = Date.now();
      if (nowT >= minT && nowT <= maxT + slotMs) {
        const clampedT = Math.max(minT, Math.min(maxT, nowT));
        const ratio = (clampedT - minT) / Math.max(1, maxT - minT);
        const nx = m.l + pw * ratio;
        nowMark = `<g class="forecast-now">
          <line class="forecast-now-line" x1="${nx.toFixed(1)}" x2="${nx.toFixed(1)}" y1="${m.t}" y2="${(m.t + ph).toFixed(1)}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3"/>
          <text class="forecast-now-label" x="${nx.toFixed(1)}" y="${m.t + 11}" text-anchor="middle" font-size="11" fill="var(--accent)">now</text>
        </g>`;
      }
    }

    const s = (payload && payload.summary) || {};
    const fmt = (v) => v == null ? "—" : `${Number(v).toFixed(2)} kWh`;
    const metricStyle = (on) => on ? "" : ` style="display:none"`;
    box.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Forecast accuracy, actual versus predicted load and PV">
        ${grid}${xt}${nowMark}
        <path class="forecast-load forecast-load-predicted" d="${path("predicted_load_kwh")}" fill="none" stroke="var(--buy)" stroke-width="1.8" stroke-dasharray="6 4" opacity="0.72"${metricStyle(showLoad)}/>
        <path class="forecast-load forecast-load-actual" d="${path("actual_load_kwh")}" fill="none" stroke="var(--buy)" stroke-width="2.8"${metricStyle(showLoad)}/>
        <path class="forecast-pv forecast-pv-predicted" d="${path("predicted_pv_kwh")}" fill="none" stroke="var(--sell)" stroke-width="1.8" stroke-dasharray="6 4" opacity="0.72"${metricStyle(showPv)}/>
        <path class="forecast-pv forecast-pv-actual" d="${path("actual_pv_kwh")}" fill="none" stroke="var(--sell)" stroke-width="2.8"${metricStyle(showPv)}/>
        <g class="forecast-accuracy-hover" style="display:none; pointer-events:none">
          <line class="forecast-hover-line" x1="0" x2="0" y1="${m.t}" y2="${(m.t + ph).toFixed(1)}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0.75"/>
          <circle class="forecast-load-actual-dot" r="4.5" fill="var(--buy)" stroke="#0b0f14" stroke-width="1.4"${metricStyle(showLoad)}/>
          <circle class="forecast-load-predicted-dot" r="3.8" fill="var(--buy)" stroke="#0b0f14" stroke-width="1.3" opacity="0.55"${metricStyle(showLoad)}/>
          <circle class="forecast-pv-actual-dot" r="4.5" fill="var(--sell)" stroke="#0b0f14" stroke-width="1.4"${metricStyle(showPv)}/>
          <circle class="forecast-pv-predicted-dot" r="3.8" fill="var(--sell)" stroke="#0b0f14" stroke-width="1.3" opacity="0.55"${metricStyle(showPv)}/>
        </g>
        <rect x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/>
      </svg>
      <div class="chart-legend muted">
        <span class="legend-toggle ${showLoad ? "" : "is-off"}" data-acc-toggle="load"><span class="swatch" style="background:var(--buy)"></span> Load actual / dashed forecast</span>
        <span class="legend-toggle ${showPv ? "" : "is-off"}" data-acc-toggle="pv"><span class="swatch" style="background:var(--sell)"></span> PV actual / dashed forecast</span>
        <span>${s.slots || pts.length} slots · Mean absolute error: load ${fmt(s.load_mae_kwh)} · PV ${fmt(s.pv_mae_kwh)}</span>
      </div>`;

    const svg = box.querySelector("svg");
    if (!svg) return;
    const hover = svg.querySelector(".forecast-accuracy-hover");
    const hoverLine = svg.querySelector(".forecast-hover-line");
    const dots = {
      loadActual: svg.querySelector(".forecast-load-actual-dot"),
      loadPred: svg.querySelector(".forecast-load-predicted-dot"),
      pvActual: svg.querySelector(".forecast-pv-actual-dot"),
      pvPred: svg.querySelector(".forecast-pv-predicted-dot"),
    };
    const idxFromEvent = (point) => {
      const ctm = svg.getScreenCTM();
      if (!ctm) return -1;
      const sp = svg.createSVGPoint();
      sp.x = point.clientX; sp.y = point.clientY;
      const u = sp.matrixTransform(ctm.inverse());
      if (u.x < m.l - 8 || u.x > m.l + pw + 8) return -1;
      return Math.max(0, Math.min(n - 1, Math.round((u.x - m.l) / (pw / Math.max(1, n - 1)))));
    };
    const setDot = (dot, x, value) => {
      if (!dot) return;
      if (value == null) { dot.style.display = "none"; return; }
      dot.style.display = "";
      dot.setAttribute("cx", x.toFixed(1));
      dot.setAttribute("cy", Y(value).toFixed(1));
    };
    const labelTime = (iso) => {
      const d = new Date(iso);
      return isNaN(d) ? String(iso || "") : d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    };
    installWeatherTooltip(
      box,
      svg,
      idxFromEvent,
      (i) => {
        const p = pts[i];
        const loadActual = val(p, "actual_load_kwh"), loadPred = val(p, "predicted_load_kwh");
        const pvActual = val(p, "actual_pv_kwh"), pvPred = val(p, "predicted_pv_kwh");
        const loadErr = loadActual != null && loadPred != null ? Math.abs(loadActual - loadPred) : null;
        const pvErr = pvActual != null && pvPred != null ? Math.abs(pvActual - pvPred) : null;
        let html = `<b>Forecast vs actual · ${labelTime(p.time)}</b>`;
        if (showLoad) {
          html += `<span>Load actual ${fmt(loadActual)}</span>`
            + `<span>Load forecast ${fmt(loadPred)}</span>`
            + `<span>Load absolute error ${fmt(loadErr)}</span>`;
        }
        if (showPv) {
          html += `<span>PV actual ${fmt(pvActual)}</span>`
            + `<span>PV forecast ${fmt(pvPred)}</span>`
            + `<span>PV absolute error ${fmt(pvErr)}</span>`;
        }
        return html;
      },
      (i) => {
        const p = pts[i], x = X(i);
        hoverLine.setAttribute("x1", x.toFixed(1)); hoverLine.setAttribute("x2", x.toFixed(1));
        setDot(dots.loadActual, x, showLoad ? val(p, "actual_load_kwh") : null);
        setDot(dots.loadPred, x, showLoad ? val(p, "predicted_load_kwh") : null);
        setDot(dots.pvActual, x, showPv ? val(p, "actual_pv_kwh") : null);
        setDot(dots.pvPred, x, showPv ? val(p, "predicted_pv_kwh") : null);
        hover.style.display = "";
      },
      () => { hover.style.display = "none"; },
      "weather-tip"
    );

    const toggleForecastAccuracySeries = (metric) => {
      if (metric === "load") box.dataset.showLoad = showLoad ? "0" : "1";
      if (metric === "pv") box.dataset.showPv = showPv ? "0" : "1";
      window.renderForecastAccuracyChart(containerId, payload);
    };
    box.querySelectorAll("[data-acc-toggle]").forEach((el) => {
      el.addEventListener("click", () => toggleForecastAccuracySeries(el.getAttribute("data-acc-toggle")));
    });
  };

  function weatherLabel(value, options) {
    const d = new Date(value);
    return isNaN(d) ? String(value || "") : d.toLocaleString(undefined, { hour12: false, ...options });
  }

  function weatherNum(value, digits, suffix) {
    const n = Number(value);
    return isFinite(n) ? `${n.toFixed(digits)}${suffix || ""}` : "—";
  }

  // Wind speed (km/h) → Beaufort force (0–12).
  function beaufort(kmh) {
    const v = Number(kmh);
    if (!isFinite(v)) return "—";
    const lo = [1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118];   // lower km/h bound of Bft 1..12
    let n = 0;
    while (n < lo.length && v >= lo[n]) n++;
    return String(n);
  }

  function installWeatherTooltip(box, svg, idxFromEvent, renderHtml, onShow, onHide, tipClass) {
    if (!box || !svg) return;
    box.style.position = "relative";
    const tip = document.createElement("div");
    tip.className = `chart-tip rich-tip${tipClass ? " " + tipClass : ""}`;
    tip.style.display = "none";
    box.appendChild(tip);

    const hide = () => {
      tip.style.display = "none";
      if (onHide) onHide();
    };
    const place = (point) => {
      const br = box.getBoundingClientRect();
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let tx = point.clientX - br.left + 14;
      let ty = point.clientY - br.top - th - 10;
      if (tx + tw > br.width) tx = point.clientX - br.left - tw - 14;
      if (ty < 0) ty = point.clientY - br.top + 16;
      tip.style.left = `${Math.max(0, tx)}px`;
      tip.style.top = `${Math.max(0, ty)}px`;
    };
    const show = (point) => {
      const idx = idxFromEvent(point);
      if (idx < 0) { hide(); return; }
      tip.innerHTML = renderHtml(idx);
      tip.style.display = "block";
      place(point);
      if (onShow) onShow(idx);
    };

    svg.addEventListener("mousemove", show);
    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("touchstart", (e) => { if (e.touches && e.touches[0]) show(e.touches[0]); }, { passive: true });
    svg.addEventListener("touchmove", (e) => { if (e.touches && e.touches[0]) show(e.touches[0]); }, { passive: true });
  }

  // ---------- Weather forecast ----------
  window.renderWeatherChart = function (containerId, payload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    const pts = ((payload && payload.hours) || []).filter((p) => p && p.time);
    if (!pts.length) { box.innerHTML = '<span class="muted">no weather forecast yet…</span>'; return; }
    const showTemp = box.dataset.showTemp !== "0";
    const showCloud = box.dataset.showCloud !== "0";
    const showGti = box.dataset.showGti !== "0";
    const showRain = box.dataset.showRain !== "0";
    const showWind = box.dataset.showWind !== "0";

    const W = 940, H = 300, m = { l: 50, r: 42, t: 16, b: 34 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const temps = pts.map((p) => Number(p.temp_c)).filter(isFinite);
    const clouds = pts.map((p) => Number(p.cloud_pct)).filter(isFinite);
    let tLo = Math.min(...temps, 0), tHi = Math.max(...temps, 30);
    if (tLo === tHi) tHi = tLo + 1;
    const X = (i) => m.l + (pts.length <= 1 ? pw / 2 : (pw * i) / (pts.length - 1));
    const Yt = (v) => m.t + ph - (ph * (v - tLo)) / (tHi - tLo);
    const Yc = (v) => m.t + ph - (ph * Math.max(0, Math.min(100, v))) / 100;
    // Irradiance (W/m²), rain (mm) and wind (km/h) each auto-scale to their own max
    // over the plot height — like temp/cloud, exact values are read from the tooltip.
    const seriesMax = (k) => Math.max(1, ...pts.map((p) => Number(p[k])).filter(isFinite));
    const gtiMax = seriesMax("gti_wm2"), rainMax = seriesMax("precip_mm"), windMax = seriesMax("wind_kmh");
    const Yg = (v) => m.t + ph - (ph * Math.max(0, v)) / gtiMax;
    const Yr = (v) => m.t + ph - (ph * Math.max(0, v)) / rainMax;
    const Yw = (v) => m.t + ph - (ph * Math.max(0, v)) / windMax;
    const path = (k, yfn) => pts.map((p, i) => {
      const v = Number(p[k]);
      return isFinite(v) ? `${i ? "L" : "M"}${X(i).toFixed(1)},${yfn(v).toFixed(1)}` : "";
    }).filter(Boolean).join(" ");

    let grid = "";
    [tLo, (tLo + tHi) / 2, tHi].forEach((v) => {
      const yy = Yt(v).toFixed(1);
      grid += `<line x1="${m.l}" y1="${yy}" x2="${m.l + pw}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;
      grid += `<text x="${m.l - 7}" y="${(Yt(v) + 3).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--muted)">${v.toFixed(0)}°C</text>`;
    });
    [0, 50, 100].forEach((v) => {
      grid += `<text x="${m.l + pw + 7}" y="${(Yc(v) + 3).toFixed(1)}" text-anchor="start" font-size="11" fill="var(--muted)">${v}%</text>`;
    });
    let xt = "", step = Math.max(1, Math.ceil(pts.length / 8));
    pts.forEach((p, i) => {
      if (i % step === 0 || i === pts.length - 1) {
        const d = new Date(p.time);
        const label = isNaN(d) ? p.time.slice(11, 16) : d.toLocaleDateString(undefined, { weekday: "short" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", hour12: false });
        xt += `<text x="${X(i).toFixed(1)}" y="${m.t + ph + 16}" text-anchor="middle" font-size="11" fill="var(--muted)">${label}</text>`;
      }
    });
    let nowMark = "";
    const times = pts.map((p) => {
      const d = new Date(p.time);
      return isNaN(d) ? null : d.getTime();
    }).filter((t) => t != null);
    if (times.length >= 2) {
      const minT = Math.min(...times), maxT = Math.max(...times);
      const nowT = Date.now();
      if (nowT >= minT && nowT <= maxT) {
        const nx = m.l + pw * ((nowT - minT) / Math.max(1, maxT - minT));
        nowMark = `<g class="weather-now">
          <line class="weather-now-line" x1="${nx.toFixed(1)}" x2="${nx.toFixed(1)}" y1="${m.t}" y2="${(m.t + ph).toFixed(1)}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3"/>
          <text class="weather-now-label" x="${nx.toFixed(1)}" y="${m.t + 11}" text-anchor="middle" font-size="11" fill="var(--accent)">now</text>
        </g>`;
      }
    }

    const s = (payload && payload.summary) || {};
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Weather forecast">
        ${grid}${xt}${nowMark}
        <path class="weather-gti-line" d="${path("gti_wm2", Yg)}" fill="none" stroke="#facc15" stroke-width="2" opacity="0.7"${displayStyle(showGti)}/>
        <path class="weather-rain-line" d="${path("precip_mm", Yr)}" fill="none" stroke="#2dd4bf" stroke-width="2" opacity="0.8" stroke-dasharray="2 3"${displayStyle(showRain)}/>
        <path class="weather-wind-line" d="${path("wind_kmh", Yw)}" fill="none" stroke="#a78bfa" stroke-width="2" opacity="0.8"${displayStyle(showWind)}/>
        <path class="weather-cloud-line" d="${path("cloud_pct", Yc)}" fill="none" stroke="var(--buy)" stroke-width="2" opacity="0.75" stroke-dasharray="5 4"${displayStyle(showCloud)}/>
        <path class="weather-temp-line" d="${path("temp_c", Yt)}" fill="none" stroke="var(--retain)" stroke-width="2.8"${displayStyle(showTemp)}/>
        <g class="weather-hover" style="display:none; pointer-events:none">
          <line class="weather-hover-line" x1="0" x2="0" y1="${m.t}" y2="${(m.t + ph).toFixed(1)}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0.75"/>
          <circle class="weather-temp-dot" r="4.5" fill="var(--retain)" stroke="#0b0f14" stroke-width="1.5"${displayStyle(showTemp)}/>
          <circle class="weather-cloud-dot" r="3.8" fill="var(--buy)" stroke="#0b0f14" stroke-width="1.3" opacity="0.9"${displayStyle(showCloud)}/>
          <circle class="weather-gti-dot" r="3.5" fill="#facc15" stroke="#0b0f14" stroke-width="1.3"${displayStyle(showGti)}/>
          <circle class="weather-rain-dot" r="3.5" fill="#2dd4bf" stroke="#0b0f14" stroke-width="1.3"${displayStyle(showRain)}/>
          <circle class="weather-wind-dot" r="3.5" fill="#a78bfa" stroke="#0b0f14" stroke-width="1.3"${displayStyle(showWind)}/>
        </g>
        <rect x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/>
      </svg>
      <div class="chart-legend muted">
        <span class="legend-toggle${offClass(showTemp)}" data-weather-toggle="temp"><span class="swatch" style="background:var(--retain)"></span> Temperature</span>
        <span class="legend-toggle${offClass(showCloud)}" data-weather-toggle="cloud"><span class="swatch" style="background:var(--buy)"></span> Cloud cover</span>
        <span class="legend-toggle${offClass(showGti)}" data-weather-toggle="gti"><span class="swatch" style="background:#facc15"></span> Irradiance</span>
        <span class="legend-toggle${offClass(showRain)}" data-weather-toggle="rain"><span class="swatch" style="background:#2dd4bf"></span> Rain</span>
        <span class="legend-toggle${offClass(showWind)}" data-weather-toggle="wind"><span class="swatch" style="background:#a78bfa"></span> Wind</span>
        <span>${s.days || 0} days · max ${s.max_temp_c == null ? "—" : Number(s.max_temp_c).toFixed(1) + "°C"}</span>
      </div>`;

    const svg = box.querySelector("svg");
    if (!svg) return;
    const hover = svg.querySelector(".weather-hover");
    const line = svg.querySelector(".weather-hover-line");
    const tempDot = svg.querySelector(".weather-temp-dot");
    const cloudDot = svg.querySelector(".weather-cloud-dot");
    const gtiDot = svg.querySelector(".weather-gti-dot");
    const rainDot = svg.querySelector(".weather-rain-dot");
    const windDot = svg.querySelector(".weather-wind-dot");
    const idxFromEvent = (point) => {
      const ctm = svg.getScreenCTM();
      if (!ctm) return -1;
      const sp = svg.createSVGPoint();
      sp.x = point.clientX; sp.y = point.clientY;
      const u = sp.matrixTransform(ctm.inverse());
      if (u.x < m.l - 8 || u.x > m.l + pw + 8) return -1;
      return Math.max(0, Math.min(pts.length - 1, Math.round((u.x - m.l) / (pw / Math.max(1, pts.length - 1)))));
    };
    installWeatherTooltip(
      box,
      svg,
      idxFromEvent,
      (i) => {
        const p = pts[i];
        return `<b>Weather forecast · ${weatherLabel(p.time, { weekday: "short", hour: "2-digit", minute: "2-digit" })}</b>`
          + (showTemp ? `<span>Temperature ${weatherNum(p.temp_c, 1, "°C")}</span>` : "")
          + `<span>Feels like ${weatherNum(p.apparent_temp_c, 1, "°C")}</span>`
          + (showCloud ? `<span>Cloud cover ${weatherNum(p.cloud_pct, 0, "%")}</span>` : "")
          + `<span>Rain ${weatherNum(p.precip_mm, 1, " mm")}</span>`
          + `<span>Wind ${beaufort(p.wind_kmh)} Bft</span>`
          + `<span>GTI irradiance ${weatherNum(p.gti_wm2, 0, " W/m²")}</span>`;
      },
      (i) => {
        const p = pts[i], x = X(i);
        const tc = Number(p.temp_c), cc = Number(p.cloud_pct);
        const gv = Number(p.gti_wm2), rv = Number(p.precip_mm), wv = Number(p.wind_kmh);
        line.setAttribute("x1", x.toFixed(1)); line.setAttribute("x2", x.toFixed(1));
        if (isFinite(tc)) {
          tempDot.setAttribute("cx", x.toFixed(1));
          tempDot.setAttribute("cy", Yt(tc).toFixed(1));
        }
        if (isFinite(cc)) {
          cloudDot.setAttribute("cx", x.toFixed(1));
          cloudDot.setAttribute("cy", Yc(cc).toFixed(1));
        }
        if (isFinite(gv)) { gtiDot.setAttribute("cx", x.toFixed(1)); gtiDot.setAttribute("cy", Yg(gv).toFixed(1)); }
        if (isFinite(rv)) { rainDot.setAttribute("cx", x.toFixed(1)); rainDot.setAttribute("cy", Yr(rv).toFixed(1)); }
        if (isFinite(wv)) { windDot.setAttribute("cx", x.toFixed(1)); windDot.setAttribute("cy", Yw(wv).toFixed(1)); }
        hover.style.display = "";
      },
      () => { hover.style.display = "none"; },
      "weather-tip"
    );
    box.querySelectorAll("[data-weather-toggle]").forEach((item) => {
      item.addEventListener("click", () => toggleWeatherSeries(containerId, item.dataset.weatherToggle, payload));
    });
  };

  function toggleWeatherSeries(containerId, which, payload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (which === "temp") box.dataset.showTemp = box.dataset.showTemp === "0" ? "1" : "0";
    if (which === "cloud") box.dataset.showCloud = box.dataset.showCloud === "0" ? "1" : "0";
    if (which === "gti") box.dataset.showGti = box.dataset.showGti === "0" ? "1" : "0";
    if (which === "rain") box.dataset.showRain = box.dataset.showRain === "0" ? "1" : "0";
    if (which === "wind") box.dataset.showWind = box.dataset.showWind === "0" ? "1" : "0";
    window.renderWeatherChart(containerId, payload);
  }
  window.toggleWeatherSeries = toggleWeatherSeries;

  window.renderWeatherImpactChart = function (containerId, payload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    const pts = ((payload && payload.days) || []).filter((d) => d && d.date);
    if (!pts.length) { box.innerHTML = '<span class="muted">no weather impact summary yet…</span>'; return; }
    const showLoad = box.dataset.showLoad !== "0";
    const showGti = box.dataset.showGti !== "0";
    const W = 940, H = 260, m = { l: 50, r: 22, t: 16, b: 30 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const vals = pts.flatMap((p) => [Number(p.weather_load_adj_kwh || 0), Number(p.gti_kwh_m2 || 0)]);
    const hi = Math.max(1, ...vals);
    const X = (i) => m.l + (pts.length <= 1 ? pw / 2 : (pw * i) / (pts.length - 1));
    const Y = (v) => m.t + ph - (ph * Math.max(0, v)) / hi;
    let grid = "";
    [0, hi / 2, hi].forEach((v) => {
      const yy = Y(v).toFixed(1);
      grid += `<line x1="${m.l}" y1="${yy}" x2="${m.l + pw}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;
      grid += `<text x="${m.l - 7}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--muted)">${v.toFixed(1)}</text>`;
    });
    let bars = "", labels = "";
    const bw = Math.max(14, Math.min(42, pw / Math.max(1, pts.length) / 3));
    pts.forEach((p, i) => {
      const x = X(i), load = Number(p.weather_load_adj_kwh || 0), gti = Number(p.gti_kwh_m2 || 0);
      bars += `<rect class="weather-impact-load-bar" x="${(x - bw - 2).toFixed(1)}" y="${Y(load).toFixed(1)}" width="${bw}" height="${(m.t + ph - Y(load)).toFixed(1)}" fill="var(--retain)" rx="3"${displayStyle(showLoad)}/>`;
      bars += `<rect class="weather-impact-gti-bar" x="${(x + 2).toFixed(1)}" y="${Y(gti).toFixed(1)}" width="${bw}" height="${(m.t + ph - Y(gti)).toFixed(1)}" fill="var(--sell)" rx="3" opacity="0.85"${displayStyle(showGti)}/>`;
      const d = new Date(p.date);
      labels += `<text x="${x.toFixed(1)}" y="${m.t + ph + 16}" text-anchor="middle" font-size="11" fill="var(--muted)">${isNaN(d) ? p.date.slice(5) : d.toLocaleDateString(undefined, { weekday: "short" })}</text>`;
    });
    const now = new Date();
    const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    const todayIndex = pts.findIndex((p) => p.date === todayKey);
    let todayMark = "";
    if (todayIndex >= 0) {
      const tx = X(todayIndex);
      todayMark = `<g class="weather-impact-today">
        <line class="weather-impact-today-line" x1="${tx.toFixed(1)}" x2="${tx.toFixed(1)}" y1="${m.t}" y2="${(m.t + ph).toFixed(1)}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3"/>
        <text class="weather-impact-today-label" x="${tx.toFixed(1)}" y="${m.t + 11}" text-anchor="middle" font-size="11" fill="var(--accent)">today</text>
      </g>`;
    }
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Weather forecast impact">
        ${grid}${bars}${labels}${todayMark}
        <g class="weather-impact-hover" style="display:none; pointer-events:none">
          <line class="weather-impact-line" x1="0" x2="0" y1="${m.t}" y2="${(m.t + ph).toFixed(1)}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0.75"/>
          <circle class="weather-impact-load-dot" r="4.5" fill="var(--retain)" stroke="#0b0f14" stroke-width="1.4"${displayStyle(showLoad)}/>
          <circle class="weather-impact-gti-dot" r="4.5" fill="var(--sell)" stroke="#0b0f14" stroke-width="1.4"${displayStyle(showGti)}/>
        </g>
        <rect x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/>
      </svg>
      <div class="chart-legend muted">
        <span class="legend-toggle${offClass(showLoad)}" data-weather-impact-toggle="load"><span class="swatch" style="background:var(--retain)"></span> HVAC load forecast kWh</span>
        <span class="legend-toggle${offClass(showGti)}" data-weather-impact-toggle="gti"><span class="swatch" style="background:var(--sell)"></span> GTI Irradiance kWh/m²</span>
      </div>`;

    const svg = box.querySelector("svg");
    if (!svg) return;
    const hover = svg.querySelector(".weather-impact-hover");
    const line = svg.querySelector(".weather-impact-line");
    const loadDot = svg.querySelector(".weather-impact-load-dot");
    const gtiDot = svg.querySelector(".weather-impact-gti-dot");
    const idxFromEvent = (point) => {
      const ctm = svg.getScreenCTM();
      if (!ctm) return -1;
      const sp = svg.createSVGPoint();
      sp.x = point.clientX; sp.y = point.clientY;
      const u = sp.matrixTransform(ctm.inverse());
      if (u.x < m.l - 8 || u.x > m.l + pw + 8) return -1;
      return Math.max(0, Math.min(pts.length - 1, Math.round((u.x - m.l) / (pw / Math.max(1, pts.length - 1)))));
    };
    installWeatherTooltip(
      box,
      svg,
      idxFromEvent,
      (i) => {
        const p = pts[i];
        return `<b>${weatherLabel(p.date, { weekday: "short", day: "numeric", month: "short" })}</b>`
          + (showLoad ? `<span>HVAC Load ${weatherNum(p.weather_load_adj_kwh, 2, " kWh")}</span>` : "")
          + `<span>Cooling degree-days ${weatherNum(p.cdd, 2, "")}</span>`
          + `<span>Heating degree-days ${weatherNum(p.hdd, 2, "")}</span>`
          + (showGti ? `<span>GTI irradiance ${weatherNum(p.gti_kwh_m2, 2, " kWh/m²")}</span>` : "")
          + `<span>Cloud average ${weatherNum(p.cloud_avg_pct, 0, "%")}</span>`
          + `<span>Temperature range ${weatherNum(p.temp_min_c, 1, "°C")}–${weatherNum(p.temp_max_c, 1, "°C")}</span>`
          + `<span>Shadow mode until apply flags are enabled</span>`;
      },
      (i) => {
        const p = pts[i], x = X(i);
        const load = Number(p.weather_load_adj_kwh || 0), gti = Number(p.gti_kwh_m2 || 0);
        line.setAttribute("x1", x.toFixed(1)); line.setAttribute("x2", x.toFixed(1));
        loadDot.setAttribute("cx", (x - bw / 2 - 2).toFixed(1));
        loadDot.setAttribute("cy", Y(load).toFixed(1));
        gtiDot.setAttribute("cx", (x + bw / 2 + 2).toFixed(1));
        gtiDot.setAttribute("cy", Y(gti).toFixed(1));
        hover.style.display = "";
      },
      () => { hover.style.display = "none"; },
      "weather-tip"
    );
    box.querySelectorAll("[data-weather-impact-toggle]").forEach((item) => {
      item.addEventListener("click", () => toggleWeatherImpactSeries(containerId, item.dataset.weatherImpactToggle, payload));
    });
  };

  function toggleWeatherImpactSeries(containerId, which, payload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (which === "load") box.dataset.showLoad = box.dataset.showLoad === "0" ? "1" : "0";
    if (which === "gti") box.dataset.showGti = box.dataset.showGti === "0" ? "1" : "0";
    window.renderWeatherImpactChart(containerId, payload);
  }
  window.toggleWeatherImpactSeries = toggleWeatherImpactSeries;

  // ---------- monthly daily-net chart ----------
  window.renderMonthlyChart = function (containerId, days) {
    const box = document.getElementById(containerId);
    if (!box) return;
    const pts = (days || []).filter((d) => d.net_eur != null);
    if (!pts.length) { box.innerHTML = '<span class="muted">no history this month yet…</span>'; return; }
    const W = 940, H = 280, m = { l: 50, r: 20, t: 16, b: 28 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const forecastValues = (p) => [p.forecast_low_eur, p.forecast_high_eur,
      p.forecast_open_eur, p.forecast_close_eur].filter((v) => Number.isFinite(Number(v))).map(Number);
    const nets = pts.flatMap((p) => [Number(p.net_eur), ...forecastValues(p)]);
    let lo = Math.min(0, ...nets), hi = Math.max(0, ...nets);
    if (lo === hi) hi = lo + 1;
    const pad = Math.max(0.15, (hi - lo) * 0.08);
    lo -= pad; hi += pad;
    const span = (hi - lo) || 1, n = pts.length;
    const band = pw / Math.max(1, n);
    const X = (i) => m.l + band * (i + 0.5);
    const Y = (v) => m.t + ph - (ph * (v - lo)) / span;

    let grid = "";
    for (let k = 0; k <= 4; k++) {
      const v = lo + (span * k) / 4, yy = Y(v).toFixed(1);
      grid += `<line x1="${m.l}" y1="${yy}" x2="${m.l + pw}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;
      grid += `<text x="${m.l - 7}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--muted)">€${v.toFixed(1)}</text>`;
    }
    grid += `<line x1="${m.l}" y1="${Y(0).toFixed(1)}" x2="${m.l + pw}" y2="${Y(0).toFixed(1)}" stroke="var(--muted)" stroke-width="1.2" stroke-dasharray="2 2"/>`;
    let xt = "", step = Math.max(1, Math.ceil(n / 12));
    pts.forEach((p, i) => {
      if (i % step === 0 || i === n - 1) xt += `<text x="${X(i).toFixed(1)}" y="${m.t + ph + 16}" text-anchor="middle" font-size="11" fill="var(--muted)">${p.day}</text>`;
    });
    const candleWidth = Math.max(4, Math.min(18, band * 0.42));
    let marks = "";
    pts.forEach((p, i) => {
      const vals = forecastValues(p);
      if (vals.length === 4) {
        const open = Number(p.forecast_open_eur), close = Number(p.forecast_close_eur);
        const low = Number(p.forecast_low_eur), high = Number(p.forecast_high_eur);
        const col = close >= open ? "var(--sell)" : "#f87171";
        const top = Math.min(Y(open), Y(close));
        const bodyHeight = Math.max(3, Math.abs(Y(open) - Y(close)));
        marks += `<g class="forecast-candle">
          <line x1="${X(i).toFixed(1)}" y1="${Y(high).toFixed(1)}" x2="${X(i).toFixed(1)}" y2="${Y(low).toFixed(1)}" stroke="${col}" stroke-width="2" opacity=".9"/>
          <rect x="${(X(i) - candleWidth / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${candleWidth.toFixed(1)}" height="${bodyHeight.toFixed(1)}" fill="${col}" fill-opacity=".28" stroke="${col}" stroke-width="1.7" rx="1"/>
        </g>`;
      }
      const actual = Number(p.net_eur);
      const actualCol = actual >= 0 ? "var(--sell)" : "#f87171";
      const fill = p.settled ? actualCol : "var(--panel)";
      marks += `<circle class="actual-net-dot" cx="${X(i).toFixed(1)}" cy="${Y(actual).toFixed(1)}" r="${p.is_today ? 5.5 : 4.5}" fill="${fill}" stroke="${actualCol}" stroke-width="2"/>`;
    });
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="daily final-net forecast ranges and settled actuals, month so far">
        ${grid}${xt}
        ${marks}
        <rect x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/>
      </svg>
      <div class="chart-legend muted">
        <span><i class="legend-candle"></i>Forecast range (first → latest)</span>
        <span><i class="legend-dot"></i>Settled actual</span>
        <span>↑ profit · ↓ net cost — hollow dot is today so far</span>
      </div>`;

    const svg = box.querySelector("svg");
    if (!svg) return;
    box.style.position = "relative";
    const tip = document.createElement("div");
    tip.className = "chart-tip rich-tip monthly-tip"; tip.style.display = "none";
    box.appendChild(tip);
    const dname = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }); };
    const idxFromEvent = (e) => {
      const ctm = svg.getScreenCTM(); if (!ctm) return -1;
      const sp = svg.createSVGPoint(); sp.x = e.clientX; sp.y = e.clientY;
      const u = sp.matrixTransform(ctm.inverse());
      if (u.x < m.l - 8 || u.x > m.l + pw + 8) return -1;
      return Math.max(0, Math.min(n - 1, Math.floor((u.x - m.l) / band)));
    };
    svg.addEventListener("mousemove", (e) => {
      const i = idxFromEvent(e);
      if (i < 0) { tip.style.display = "none"; return; }
      const p = pts[i], profit = p.net_eur >= 0;
      const hasForecast = forecastValues(p).length === 4;
      const actualLabel = p.settled ? "Settled actual" : "Actual so far";
      tip.innerHTML = `<b>${dname(p.date)}${p.is_today ? " (today)" : ""}</b>`
        + `<span>${actualLabel}: ${profit ? "€" + p.net_eur.toFixed(2) + " profit" : "€" + Math.abs(p.net_eur).toFixed(2) + " cost"}</span>`
        + `${hasForecast ? `<span>Forecast range: €${Number(p.forecast_low_eur).toFixed(2)} to €${Number(p.forecast_high_eur).toFixed(2)}</span>` : ""}`
        + `${hasForecast ? `<span>First → latest: €${Number(p.forecast_open_eur).toFixed(2)} → €${Number(p.forecast_close_eur).toFixed(2)} (${p.forecast_samples || "?"} snapshots)</span>` : '<span>Forecast snapshots start with the new data format.</span>'}`
        + `<span>Import ${p.import_kwh != null ? p.import_kwh.toFixed(1) : "?"} kWh</span>`
        + `<span>Export ${p.export_kwh != null ? p.export_kwh.toFixed(1) : "?"} kWh</span>`;
      tip.style.display = "block";
      const br = box.getBoundingClientRect(), tw = tip.offsetWidth, th = tip.offsetHeight;
      let tx = e.clientX - br.left + 14, ty = e.clientY - br.top - th - 10;
      if (tx + tw > br.width) tx = e.clientX - br.left - tw - 14;
      if (ty < 0) ty = e.clientY - br.top + 16;
      tip.style.left = `${Math.max(0, tx)}px`; tip.style.top = `${ty}px`;
    });
    svg.addEventListener("mouseleave", () => { tip.style.display = "none"; });
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
