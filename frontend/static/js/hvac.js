(function () {
  "use strict";

  const tempTimers = new Map();
  const localTargets = new Map();
  const modeOrder = ["cooling", "heating", "dry", "fanOnly", "auto"];
  const modeLabels = {
    cooling: "Cool", heating: "Heat", dry: "Dry", fanOnly: "Fan", auto: "Auto",
  };
  const modeHelp = {
    cooling: "Lowers the room temperature to the selected target.",
    heating: "Raises the room temperature to the selected target.",
    dry: "Reduces humidity while preserving room temperature as much as possible; temperature and fan speed are automatic.",
    fanOnly: "Circulates room air without heating or cooling.",
    auto: "Automatically chooses heating or cooling to maintain the selected target.",
  };
  const escapeHtml = (value) => String(value == null ? "" : value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const number = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const fmt = (value, digits = 1) => number(value) == null
    ? "—" : Number(value).toFixed(digits);
  const title = (value) => String(value || "unknown")
    .replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ")
    .replace(/^./, (char) => char.toUpperCase());
  const attr = (enabled) => enabled ? "" : " disabled";

  function setAvailable(available) {
    const desktop = document.getElementById("hvac-nav-link");
    const mobile = document.getElementById("hvac-mobile-link");
    if (desktop) desktop.hidden = !available;
    if (mobile) mobile.hidden = !available;
  }

  function commandPending(command) {
    return command && ["accepted", "confirming", "accepted_unconfirmed"].includes(command.status);
  }

  function temperatureSpec(unit) {
    return ((((unit.controls || {}).temperature || {}).modes || {})[unit.operation_mode]) || null;
  }

  function temperatureRing(unit, shownPower) {
    const spec = temperatureSpec(unit) || {};
    const room = number(unit.room_temperature_c);
    const target = number(localTargets.get(unit.unit) ?? unit.target_temperature_c);
    const minimum = number(spec.min) ?? 16;
    const maximum = number(spec.max) ?? 32;
    const fraction = target == null ? 0 : Math.max(0, Math.min(1, (target - minimum) / (maximum - minimum)));
    const circumference = 226.2;
    const sweep = circumference * 0.75;
    const filled = shownPower === "on" ? sweep * fraction : 0;
    return `<div class="hvac-ring">
      <svg viewBox="0 0 90 90" role="img"
        aria-label="${escapeHtml(unit.display_name)} room ${fmt(room)} degrees, target ${fmt(target)} degrees">
        <circle class="hvac-ring-track" cx="45" cy="45" r="36" fill="none" stroke-width="7"
          stroke-dasharray="${sweep} ${circumference}" stroke-linecap="round"
          transform="rotate(135 45 45)"></circle>
        <circle class="hvac-ring-arc" cx="45" cy="45" r="36" fill="none" stroke-width="7"
          stroke-dasharray="${filled} ${circumference}" stroke-linecap="round"
          transform="rotate(135 45 45)"></circle>
        <text class="hvac-ring-room" x="45" y="43" text-anchor="middle">${fmt(room)}°</text>
        <text class="hvac-ring-goal" x="45" y="57" text-anchor="middle">${
          shownPower === "on" && target != null ? `→ ${fmt(target)}°` : "idle"
        }</text>
      </svg>
    </div>`;
  }

  function segmented(
    values,
    current,
    command,
    enabled,
    labeler = title,
    help = {},
  ) {
    return `<div class="hvac-segments" role="group">${values.map((value) =>
      `<button type="button" data-hvac-command="${escapeHtml(command)}"
        data-hvac-value="${escapeHtml(value)}" aria-pressed="${String(value) === String(current)}"
        ${help[value] ? `title="${escapeHtml(help[value])}" aria-label="${escapeHtml(`${labeler(value)}: ${help[value]}`)}"` : ""}
        ${attr(enabled)}>${escapeHtml(labeler(value))}</button>`
    ).join("")}</div>`;
  }

  function modeControls(unit, enabled) {
    const spec = (unit.controls || {}).operationMode;
    if (!spec || !spec.settable) return "";
    const values = [...(spec.values || [])].sort(
      (a, b) => {
        const aIndex = modeOrder.indexOf(a);
        const bIndex = modeOrder.indexOf(b);
        return (aIndex < 0 ? modeOrder.length : aIndex)
          - (bIndex < 0 ? modeOrder.length : bIndex);
      }
    );
    return segmented(
      values,
      unit.operation_mode,
      "mode",
      enabled,
      (value) => modeLabels[value] || title(value),
      modeHelp,
    );
  }

  function fanControls(unit, enabled) {
    const spec = ((((unit.controls || {}).fan || {}).modes || {})[unit.operation_mode]) || {};
    const choices = [];
    for (const mode of spec.speed_modes || []) {
      if (mode !== "fixed") choices.push({value: mode, command: "fan_mode", label: title(mode)});
    }
    if (spec.fixed) {
      const minimum = Number(spec.fixed.min);
      const maximum = Number(spec.fixed.max);
      for (let level = minimum; level <= maximum; level += Number(spec.fixed.step || 1)) {
        choices.push({value: level, command: "fan_level", label: String(level)});
      }
    }
    if (!choices.length) return "";
    return `<div class="hvac-segments hvac-fan-segments" role="group">${
      choices.map((choice) => {
        const active = choice.command === "fan_level"
          ? (unit.fan || {}).mode === "fixed" && Number((unit.fan || {}).level) === Number(choice.value)
          : (unit.fan || {}).mode === choice.value;
        return `<button type="button" data-hvac-command="${choice.command}"
          data-hvac-value="${escapeHtml(choice.value)}" aria-pressed="${active}"
          ${attr(enabled)}>${escapeHtml(choice.label)}</button>`;
      }).join("")
    }</div>`;
  }

  function airflowControl(unit, enabled, direction) {
    const spec = ((((unit.controls || {}).fan || {}).modes || {})[unit.operation_mode]) || {};
    const values = spec[`${direction}_modes`] || [];
    if (!values.length) return "";
    const current = (unit.fan || {})[direction];
    const currentIndex = Math.max(0, values.indexOf(current));
    const next = values[(currentIndex + 1) % values.length];
    const active = !["stop", "off", "fixed", null, undefined].includes(current);
    return `<button type="button" class="hvac-chip" data-hvac-command="${direction}_swing"
      data-hvac-value="${escapeHtml(next)}" aria-pressed="${active}" ${attr(enabled)}>
      <span aria-hidden="true">${direction === "horizontal" ? "↔" : "↕"}</span>
      ${escapeHtml(active ? title(current) : "Off")}
    </button>`;
  }

  function temperatureControl(unit, enabled) {
    const spec = temperatureSpec(unit);
    if (!spec) return "";
    const value = number(localTargets.get(unit.unit) ?? unit.target_temperature_c ?? spec.value);
    return `<div class="hvac-stepper" data-hvac-temperature
      data-min="${escapeHtml(spec.min)}" data-max="${escapeHtml(spec.max)}"
      data-step="${escapeHtml(spec.step || 0.5)}">
      <button type="button" data-hvac-temp-step="-1" aria-label="Lower target" ${attr(enabled)}>−</button>
      <span class="hvac-stepper-value" aria-live="polite">${fmt(value)}°C</span>
      <button type="button" data-hvac-temp-step="1" aria-label="Raise target" ${attr(enabled)}>+</button>
    </div>`;
  }

  function unitCard(unit, controlEnabled, command) {
    const pending = commandPending(command);
    const requestedPower = pending && command.command === "power" ? command.desired : null;
    const shownPower = requestedPower || unit.power;
    const running = shownPower === "on";
    const energy = ((unit.energy || {}).today || {}).total_kwh;
    const controls = unit.controls || {};
    const powerful = controls.powerfulMode;
    const health = unit.health || {};
    const unitControlEnabled = controlEnabled && unit.cloud_connected !== false;
    let statusClass = "";
    let status = running ? "Running" : "Off";
    if (pending) {
      status = `${shownPower === "on" ? "On" : "Off"} requested`;
    } else if (unit.cloud_connected === false) {
      status = "Offline";
      statusClass = "is-alert";
    } else if (health.error) {
      status = "Error";
      statusClass = "is-error";
    } else if (health.warning || health.caution) {
      status = "Warning";
      statusClass = "is-alert";
    }
    const fan = (unit.fan || {}).mode === "fixed"
      ? `fan ${(unit.fan || {}).level || "fixed"}`
      : `fan ${title((unit.fan || {}).mode)}`;
    const modeClass = unit.operation_mode === "heating" ? "mode-heating" : "mode-cooling";
    const fanControl = fanControls(unit, unitControlEnabled);
    const horizontalAirflow = airflowControl(unit, unitControlEnabled, "horizontal");
    const verticalAirflow = airflowControl(unit, unitControlEnabled, "vertical");

    return `<article class="hvac-unit ${modeClass} ${running ? "is-running" : "is-off"}"
      data-hvac-unit="${escapeHtml(unit.unit)}">
      <div class="hvac-unit-head">
        ${temperatureRing(unit, shownPower)}
        <div class="hvac-unit-identity">
          <div class="hvac-unit-title">
            <span>${escapeHtml(unit.display_name || "HVAC unit")}</span>
            <span class="hvac-status-pill ${statusClass}">${escapeHtml(status)}</span>
          </div>
          <div class="hvac-unit-meta">${escapeHtml(title(unit.operation_mode))} · ${escapeHtml(fan)} · ${fmt(energy)} kWh today</div>
          <div class="hvac-mode-row">${modeControls(unit, unitControlEnabled)}</div>
        </div>
        <button type="button" class="hvac-master-switch" data-hvac-command="power"
          data-hvac-value="${running ? "off" : "on"}" aria-pressed="${running}"
          aria-label="${running ? "Turn off" : "Turn on"} ${escapeHtml(unit.display_name)}"
          ${attr(unitControlEnabled)}><span></span></button>
      </div>
      <div class="hvac-divider"></div>
      ${Object.keys(controls).length ? `<div class="hvac-control-grid">
        ${temperatureSpec(unit) ? `<div class="hvac-field"><div class="hvac-field-label">Target</div>${temperatureControl(unit, unitControlEnabled)}</div>` : ""}
        ${fanControl ? `<div class="hvac-field"><div class="hvac-field-label">Fan speed</div>${fanControl}</div>` : ""}
        ${(horizontalAirflow || verticalAirflow) ? `
          <div class="hvac-field"><div class="hvac-field-label">Airflow</div><div class="hvac-chip-row">
            ${horizontalAirflow}
            ${verticalAirflow}
          </div></div>` : ""}
        ${powerful ? `<div class="hvac-field"><div class="hvac-field-label">Powerful</div>
          <div class="hvac-switch-row">
            <button type="button" class="hvac-small-switch" data-hvac-command="powerful"
              data-hvac-value="${unit.powerful_mode ? "false" : "true"}"
              aria-pressed="${unit.powerful_mode}" ${attr(unitControlEnabled)}><span></span></button>
            <span>${unit.powerful_mode ? "Boost on" : "Boost off"}</span>
          </div></div>` : ""}
      </div>` : `<p class="muted">Control capabilities will appear after the next ONECTA refresh.</p>`}
      ${command && command.status ? `<div class="hvac-command-status">${escapeHtml(title(command.status))}${
        command.message ? ` · ${escapeHtml(command.message)}` : ""
      }</div>` : ""}
    </article>`;
  }

  function summaryRail(data) {
    const summary = data.summary || {};
    const cooling = number(summary.today_cooling_kwh) || 0;
    const heating = number(summary.today_heating_kwh) || 0;
    const total = number(summary.today_total_kwh) || 0;
    const coolingPercent = total > 0 ? (cooling / total) * 100 : 0;
    const temperatures = (data.units || []).map((unit) => number(unit.outdoor_temperature_c)).filter((value) => value != null);
    const outdoor = temperatures.length
      ? temperatures.reduce((sum, value) => sum + value, 0) / temperatures.length : null;
    return `<section class="hvac-energy-rail" aria-label="HVAC energy summary">
      <div class="hvac-rail-total"><span>Used today</span><b>${fmt(total)} <small>kWh</small></b></div>
      <div class="hvac-rail-split">
        <div class="hvac-split-bar" aria-label="Cooling and heating share">
          <span class="cool" style="width:${coolingPercent}%"></span>
          <span class="heat" style="width:${total > 0 ? 100 - coolingPercent : 0}%"></span>
        </div>
        <div class="hvac-split-legend">
          <span><i class="cool"></i>Cooling ${fmt(cooling)}</span>
          <span><i class="heat"></i>Heating ${fmt(heating)}</span>
          <span>Outdoor avg ${fmt(outdoor)}°</span>
        </div>
      </div>
    </section>`;
  }

  function render(data) {
    const root = document.getElementById("hvac-dashboard");
    if (!root) return;
    const available = !!(data && data.enabled && data.available && (data.units || []).length);
    setAvailable(available);
    if (!available) {
      root.innerHTML = `<div class="card"><h2>HVAC</h2><p class="muted">${
        data && data.enabled ? "Waiting for the first valid ONECTA snapshot." : "Daikin ONECTA monitoring is disabled."
      }</p></div>`;
      return;
    }
    const summary = data.summary || {};
    const stamp = new Date(data.fetched_at).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    const apiLimit = number((data.rate_limits || {}).limit_day);
    const apiRemaining = number((data.rate_limits || {}).remaining_day);
    const apiUsage = apiLimit != null && apiRemaining != null
      ? ` · ${Math.max(0, Math.round(apiLimit - apiRemaining))} of ${Math.round(apiLimit)} API calls`
      : "";
    root.innerHTML = `<div class="hvac-page-head">
        <div><h1>HVAC</h1><p>Daikin ONECTA · ${summary.device_count || data.units.length} units · ${summary.powered_units || 0} running</p></div>
        <span>Updated ${escapeHtml(stamp)}${escapeHtml(apiUsage)}</span>
      </div>
      ${data.control_enabled ? "" : `<div class="hvac-readonly-note"><strong>Read-only mode.</strong> Controls are disabled in Configuration; monitoring and energy history remain active.</div>`}
      ${summaryRail(data)}
      <div class="hvac-units">${data.units.map((unit) =>
        unitCard(unit, data.control_enabled, (data.commands || {})[unit.unit])
      ).join("")}</div>`;
  }

  async function refresh() {
    try {
      const response = await fetch("/api/hvac", {cache: "no-store"});
      if (!response.ok) throw new Error("HVAC state unavailable");
      const data = await response.json();
      render(data);
      return data;
    } catch (_) {
      setAvailable(false);
      return null;
    }
  }

  async function send(unit, command, value, element) {
    if (element) element.disabled = true;
    let keepDisabled = false;
    try {
      const response = await fetch(`/api/hvac/units/${encodeURIComponent(unit)}/control`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({command, value}),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "HVAC command failed");
      keepDisabled = !!(result.result && result.result.status === "accepted");
      await refresh();
      if (keepDisabled) {
        [6000, 12000, 32000].forEach((delay) => window.setTimeout(refresh, delay));
      }
    } catch (error) {
      window.alert(error.message);
    } finally {
      if (element && !keepDisabled) element.disabled = false;
    }
  }

  function stepTemperature(button) {
    const card = button.closest("[data-hvac-unit]");
    const stepper = button.closest("[data-hvac-temperature]");
    if (!card || !stepper) return;
    const readout = stepper.querySelector(".hvac-stepper-value");
    const current = number(localTargets.get(card.dataset.hvacUnit) ?? String(readout.textContent).replace(/[^\d.-]/g, ""));
    const minimum = Number(stepper.dataset.min);
    const maximum = Number(stepper.dataset.max);
    const step = Number(stepper.dataset.step);
    const next = Math.max(minimum, Math.min(maximum, current + Number(button.dataset.hvacTempStep) * step));
    localTargets.set(card.dataset.hvacUnit, next);
    readout.textContent = `${fmt(next)}°C`;
    clearTimeout(tempTimers.get(card.dataset.hvacUnit));
    tempTimers.set(card.dataset.hvacUnit, window.setTimeout(() => {
      send(card.dataset.hvacUnit, "temperature", next, null);
      localTargets.delete(card.dataset.hvacUnit);
    }, 700));
  }

  document.addEventListener("click", (event) => {
    const tempButton = event.target.closest("[data-hvac-temp-step]");
    if (tempButton) {
      stepTemperature(tempButton);
      return;
    }
    const button = event.target.closest("button[data-hvac-command]");
    if (!button) return;
    const card = button.closest("[data-hvac-unit]");
    if (!card) return;
    send(card.dataset.hvacUnit, button.dataset.hvacCommand, button.dataset.hvacValue, button);
  });

  window.refreshHvac = refresh;
  refresh();
  window.setInterval(refresh, 60000);
}());
