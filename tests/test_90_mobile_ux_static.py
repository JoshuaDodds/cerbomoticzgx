from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "frontend" / "templates" / "index.html"
APP_JS = ROOT / "frontend" / "static" / "js" / "app.js"
APP_CSS = ROOT / "frontend" / "static" / "css" / "app.css"
MOBILE_CSS = ROOT / "frontend" / "static" / "css" / "app.mobile.css"
HVAC_JS = ROOT / "frontend" / "static" / "js" / "hvac.js"
POWERFLOW_JS = ROOT / "frontend" / "static" / "js" / "powerflow.js"
LIVE_PY = ROOT / "frontend" / "live.py"
EVENT_HANDLER_PY = ROOT / "lib" / "event_handler.py"
EV_CONTROLLER_PY = ROOT / "lib" / "ev_charge_controller.py"


def test_mobile_stylesheet_loads_after_desktop_stylesheet():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'content="width=device-width, initial-scale=1, viewport-fit=cover"' in html
    assert "css/app.mobile.css" in html
    assert html.index("css/app.css") < html.index("css/app.mobile.css")


def test_desktop_overview_gives_solar_forecast_more_room_without_changing_mobile():
    css = APP_CSS.read_text(encoding="utf-8")

    assert "@media (min-width: 901px)" in css
    assert (
        "grid-template-columns: minmax(320px, .95fr) minmax(0, 2.05fr);"
        in css
    )
    # The existing phone override remains present and is not replaced by the
    # desktop-only adjustment.
    assert "@media (max-width: 720px) { .overview-row { grid-template-columns: 1fr; } }" in css


def test_powerflow_ev_card_uses_per_phase_current_like_vehicle_tab():
    powerflow = POWERFLOW_JS.read_text(encoding="utf-8")
    live = LIVE_PY.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")

    for phase in (1, 2, 3):
        assert f'"ev_l{phase}_a": f"N/{{sid}}/evcharger/42/Ac/L{phase}/Current"' in live
        assert f'out["ev_l{phase}_a"] = _num("ev_l{phase}_a")' in live
    assert "const evMeterPhaseAmps" in powerflow
    assert "live.ev_l1_a, live.ev_l2_a, live.ev_l3_a" in powerflow
    assert "num(live.veh_amps)" in powerflow
    assert "evMeterPhaseAmps.length" in powerflow
    assert "/ evMeterPhaseAmps.length" in powerflow
    assert "evTotalAmps" not in powerflow
    assert "ev <= EV_IDLE_POWER_W" in powerflow
    assert "evPhaseAmps * evPhases" not in powerflow
    assert 'card("Charge current", amps(L.veh_amps))' in app


def test_mobile_powerflow_battery_card_budgets_height_for_all_bms_rows():
    powerflow = POWERFLOW_JS.read_text(encoding="utf-8")
    mobile_css = MOBILE_CSS.read_text(encoding="utf-8")

    assert "const ch = 104, batth = 220" in powerflow
    assert "const batteryRowBudget" in powerflow
    assert "rows.length + 2.25" in powerflow
    assert "Math.min(rowF, batteryRowBudget)" in powerflow
    assert "const detailRowBudget" in powerflow
    assert "rows.length + 0.25" in powerflow
    assert "Math.min(rowF, detailRowBudget)" in powerflow
    assert "const battCardH = Math.min(batth * sc, 190)" in powerflow
    assert "y: battTop + battCardH / 2" in powerflow
    assert "height: clamp(560px, 80vh, 650px)" in mobile_css


def test_powerflow_cards_surface_grid_accounting_and_house_only_day_energy():
    powerflow = POWERFLOW_JS.read_text(encoding="utf-8")
    live = LIVE_PY.read_text(encoding="utf-8")

    assert '"day_energy_last_update": "Tibber/home/energy/day/last_update"' in live
    assert (
        '"load_actual_today_wh": '
        '"Cerbomoticzgx/GlobalState/consumption_total_cumulative"'
    ) in live
    assert (
        '"ev_actual_today_kwh": "Cerbomoticzgx/GlobalState/ev_today_kwh"'
    ) in live
    for field in (
        "pf-grid-import",
        "pf-grid-export",
        "pf-grid-updated",
        "pf-house-today",
    ):
        assert field in powerflow
    assert '["Import", "pf-grid-import-m"]' in powerflow
    assert '["Export", "pf-grid-export-m"]' in powerflow
    assert '["Updated", "pf-grid-updated-m"]' in powerflow
    assert r"(\d{2}:\d{2}:\d{2})" in powerflow
    assert '["Today", "pf-ev-today"]' in powerflow
    assert 'V["pf-ev-today"]' in powerflow
    assert "live.ev_actual_today_kwh" in powerflow
    assert 'grid:  [["L1", "pf-grid-l1"]' not in powerflow
    assert "const houseCardH = 128" in powerflow
    assert "height: clamp(560px, 80vh, 650px)" in MOBILE_CSS.read_text(
        encoding="utf-8"
    )


def test_desktop_grid_and_house_phase_rows_match_solar_spacing():
    powerflow = POWERFLOW_JS.read_text(encoding="utf-8")

    assert "const desktopPhaseStep = r.h * 0.062" in powerflow
    assert (
        "const y = y0 + r.h * (desktopDetailLayout ? 0.51 : 0.47) + i * desktopPhaseStep"
        in powerflow
    )
    assert (
        "const y = y0 + r.h * 0.68 + i * desktopPhaseStep"
        in powerflow
    )
    assert (
        "const y = y0 + r.h * (desktopDetailLayout ? 0.70 : 0.66) + i * desktopPhaseStep"
        in powerflow
    )


def test_desktop_powerflow_reserves_extra_svg_header_space_in_every_browser():
    powerflow = POWERFLOW_JS.read_text(encoding="utf-8")

    assert "const desktopDetailLayout = !mobile;" in powerflow
    assert "const rowH = 0.45 * H;" in powerflow
    assert "desktopDetailLayout ? 0.37 : 0.31" in powerflow
    assert "desktopDetailLayout ? 0.40 : 0.34" in powerflow
    assert "desktopDetailLayout ? 0.37 : 0.33" in powerflow
    assert "desktopDetailLayout ? 0.36 : 0.26" in powerflow
    assert "IS_FIREFOX" not in powerflow


def test_hvac_dashboard_uses_capability_driven_compact_controls():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = HVAC_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")
    mobile_css = MOBILE_CSS.read_text(encoding="utf-8")

    assert 'id="hvac-dashboard"' in html
    assert "hvac-energy-rail" in js
    assert "hvac-ring" in js
    assert "hvac-segments" in js
    assert "Reduces humidity while preserving room temperature" in js
    assert "Circulates room air without heating or cooling" in js
    assert "Automatically chooses heating or cooling" in js
    assert 'title="${escapeHtml(help[value])}"' in js
    assert "unit.cloud_connected === false" in js
    assert 'status = "Offline"' in js
    assert 'status = "Error"' in js
    assert 'status = "Warning"' in js
    assert "controlEnabled && unit.cloud_connected !== false" in js
    assert "data-hvac-temp-step" in js
    assert "temperatureSpec(unit)" in js
    assert "spec.speed_modes" in js
    assert 'spec[`${direction}_modes`]' in js
    assert 'airflowControl(unit, unitControlEnabled, "horizontal")' in js
    assert 'airflowControl(unit, unitControlEnabled, "vertical")' in js
    assert 'send(card.dataset.hvacUnit, "temperature", next, null)' in js
    assert "}, 700)" in js
    assert 'fetch("/api/hvac", {cache: "no-store"})' in js
    assert "/api/hvac/units/" in js
    assert 'hour12: false' in js
    assert 'API calls' in js
    assert 'limit_day' in js
    assert 'remaining_day' in js
    assert ".hvac-unit.is-off:hover" in css
    assert ".hvac-control-grid" in css
    assert ".hvac-control-grid { grid-template-columns: 1fr;" in mobile_css


def test_vehicle_tab_warns_only_when_disconnected_during_apparent_charging():
    app = APP_JS.read_text(encoding="utf-8")
    live = LIVE_PY.read_text(encoding="utf-8")

    assert '"veh_telemetry_status": "Tesla/vehicle0/telemetry_status"' in live
    assert "telemetryActivityExpected" in app
    assert "telemetryDisconnected && telemetryActivityExpected" in app
    assert "Vehicle telemetry disconnected during apparent charging" in app
    assert "VEHICLE_TELEMETRY_FRESH_SECONDS" not in app


def test_vehicle_refresh_is_a_primary_action():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert (
        '<button id="vehicle-refresh" type="button" class="btn" '
        "data-vehicle-refresh"
    ) in html


def test_run_now_is_a_primary_action_with_explicit_style():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert (
        'class="btn-primary" data-ev-smart-action="run_now">Run Now</button>'
        in html
    )
    assert ".btn-primary { background: var(--accent);" in css


def test_abb_event_path_is_only_shared_current_topic_publisher():
    event_handler = EVENT_HANDLER_PY.read_text(encoding="utf-8")
    controller = EV_CONTROLLER_PY.read_text(encoding="utf-8")
    event_aggregate = event_handler.split("    def update_charging_amp_totals", 1)[1].split(
        "    def set_surplus_amps", 1
    )[0]
    controller_aggregate = controller.split("    def update_charging_amp_totals", 1)[1].split(
        "    @staticmethod", 1
    )[0]

    assert 'publish_message("Tesla/vehicle0/charging_amps"' in event_aggregate
    assert "TESLA_TELEMETRY_ENABLED" not in event_aggregate
    assert 'publish_message("Tesla/vehicle0/charging_amps"' not in controller_aggregate


def test_mobile_navigation_markup_is_hidden_by_default():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<nav class="mobile-bottom-nav"' in html
    assert '<nav class="mobile-bottom-nav" hidden' in html
    assert html.index("data-mobile-menu-toggle") < html.index('data-mobile-tab="live"')
    assert html.index('data-mobile-tab="live"') < html.index('data-mobile-tab="schedule"')
    assert html.index('data-mobile-tab="schedule"') < html.index('data-mobile-tab="trends"')
    assert html.index('data-mobile-tab="trends"') < html.index('data-mobile-tab="advisor"')
    assert 'data-mobile-tab="live"' in html
    assert 'data-mobile-tab="schedule"' in html
    assert 'data-mobile-tab="trends"' in html
    assert 'data-mobile-tab="advisor"' in html
    assert "data-mobile-menu-toggle" in html
    assert "data-mobile-app-view=\"battery\"" in html
    assert "data-mobile-app-view=\"live\"" in html
    assert 'id="mobile-replan"' in html
    assert "data-replan" in html
    assert "id=\"mobile-key-stat\"" in html
    assert "Victron Schedule" in html
    assert ">Battery</button>" in html
    assert "Battery view" not in html
    assert "ESS dashboard" not in html
    assert "Victron" in html


def test_favicon_uses_existing_brand_asset():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'rel="icon"' in html
    assert "img/logo.svg" in html


def test_venus_iframe_uses_https_endpoint():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'src="https://venus.hs.mfis.net/gui-v2/"' in html
    assert "http://192.168.1.163/app/" not in html


def test_overview_entry_precedes_ess_and_desktop_uses_power_flow_default():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'data-app-view="overview">Overview</a>' in html
    assert html.index('data-app-view="overview"') < html.index('data-app-view="ess"')
    assert 'const APP_VIEWS = ["overview", "ess", "battery", "hvac", "live"]' in js
    assert 'return "overview"' in js
    assert 'if (view === "overview" && !isMobileLayout()) activateTab("live")' in js
    assert 'body[data-app-view="ess"] .overview' in css


def test_mobile_logo_has_home_action_hook():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "data-mobile-home" in html
    assert "data-home" in html


def test_import_schedule_tab_has_clear_schedule_action():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<button class="tab" data-tab="victron">Victron Schedule</button>' in html
    assert 'id="clear-import-schedule"' in html
    assert html.index('id="tab-victron"') < html.index('id="clear-import-schedule"')
    assert html.index('id="clear-import-schedule"') < html.index('id="victron-schedules"')


def test_mobile_css_is_scoped_to_phone_breakpoint():
    css = MOBILE_CSS.read_text(encoding="utf-8")
    css_without_comments = css
    while "/*" in css_without_comments:
        start = css_without_comments.index("/*")
        end = css_without_comments.index("*/", start) + 2
        css_without_comments = css_without_comments[:start] + css_without_comments[end:]

    assert css_without_comments.strip().startswith("@media (max-width: 680px)")
    assert ".mobile-bottom-nav" in css
    assert ".mobile-menu" in css
    assert ".status-strip" in css
    assert ".status-price" in css
    assert "order: 99" in css
    assert ".hour-row" in css
    assert ".cfg-info-toggle" in css
    assert 'body[data-app-view="overview"] main' in css
    assert 'body[data-app-view="ess"] .overview' in css
    assert 'body[data-mobile-tab="live"] .overview' not in css
    assert 'body[data-mobile-tab="trends"] .overview' not in css
    assert 'body[data-mobile-tab="advisor"] .overview' not in css
    assert 'body[data-mobile-tab="victron"] .overview' not in css
    assert 'body[data-mobile-tab="config"] .overview' not in css
    assert ".foot #replan" in css
    assert "display: none" in css
    assert ".battery-frame-card" in css
    assert ".live-frame-card" in css
    assert "--battery-frame-scale: 0.9" in css
    assert "--battery-frame-fit: 111.111%" in css
    assert "transform: scale(var(--battery-frame-scale))" in css
    assert "--victron-frame-scale: 0.48" in css
    assert "transform: scale(var(--victron-frame-scale))" in css
    assert "env(safe-area-inset-bottom)" in css


def test_mobile_js_is_guarded_by_phone_media_query():
    js = APP_JS.read_text(encoding="utf-8")

    assert 'matchMedia("(max-width: 680px)")' in js
    assert "function initMobileChrome()" in js
    assert "function activateTab(tabName)" in js
    assert "data-mobile-tab" in js
    assert "data-mobile-menu-toggle" in js
    assert "mobile-key-stat" in js
    assert "data-mobile-action" in js
    assert "data-mobile-home" in js
    assert "dataset.mobileTab" in js
    assert "dataset.appView" in js
    assert 'closest("button[data-mobile-tab]")' in js
    assert 'closest("button[data-mobile-app-view]")' in js
    assert "activeView !== \"overview\"" in js


def test_mobile_home_uses_overview_without_mobile_power_flow_default():
    js = APP_JS.read_text(encoding="utf-8")
    css = MOBILE_CSS.read_text(encoding="utf-8")

    assert "function goHome(" in js
    assert 'setAppView("overview")' in js
    assert 'if (view === "overview" && !isMobileLayout()) activateTab("live")' in js
    assert 'body[data-app-view="overview"] main' in css
    assert "body[data-app-view=\"overview\"] #ess-view > .foot" in css


def test_mobile_replan_uses_shared_action_hook_from_menu():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert 'id="replan"' in html
    assert 'id="mobile-replan"' in html
    assert html.index('id="mobile-replan"') > html.index('id="mobile-menu"')
    mobile_app_section = html.index('aria-label="Application sections"', html.index('id="mobile-menu"'))
    dashboard_section = html.index('aria-label="Dashboard tabs"', html.index('id="mobile-menu"'))
    assert mobile_app_section < dashboard_section
    assert html.index('data-mobile-app-view="battery"') < html.index('data-mobile-app-view="live"')
    assert html.index('data-mobile-app-view="live"') < html.index('data-mobile-tab="victron"')
    assert html.index('data-mobile-tab="config"') < html.index('id="mobile-replan"')
    assert html.count("data-replan") == 2
    assert 'document.querySelectorAll("[data-replan]")' in js
    assert "currentTarget" in js


def test_restart_action_exists_beside_replan_and_in_mobile_menu():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert 'id="restart"' in html
    assert 'id="mobile-restart"' in html
    assert 'data-restart' in html
    assert html.index('id="replan"') < html.index('id="restart"')
    assert html.index('id="mobile-replan"') < html.index('id="mobile-restart"')
    assert html.count("data-restart") == 2
    assert 'document.querySelectorAll("[data-restart]")' in js
    assert 'fetch("/api/restart", { method: "POST" })' in js
    assert 'e.target.closest("button[data-restart]")' in js


def test_override_and_grid_assist_controls_exist_after_restart():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert 'id="override"' in html
    assert 'id="grid-assist"' in html
    assert 'id="mobile-override"' in html
    assert 'id="mobile-grid-assist"' in html
    assert html.index('id="restart"') < html.index('id="override"') < html.index('id="grid-assist"')
    assert html.index('id="mobile-restart"') < html.index('id="mobile-override"') < html.index('id="mobile-grid-assist"')
    assert html.count("data-ai-override") == 2
    assert html.count("data-grid-assist") == 2
    assert '"/api/control/ai-override"' in js
    assert '"/api/control/grid-assist"' in js
    assert "function toggleControl(" in js
    assert 'document.querySelectorAll("[data-ai-override]")' in js
    assert 'document.querySelectorAll("[data-grid-assist]")' in js


def test_mobile_schedule_button_scrolls_to_current_slot():
    js = APP_JS.read_text(encoding="utf-8")

    assert "function scrollToCurrentScheduleSlot" in js
    assert 'document.querySelector("#hours .slot-row.current")' in js
    assert 'document.querySelector("#hours .hour-row.current")' in js
    assert 'tabBtn.dataset.mobileTab === "schedule"' in js
    assert "scrollToCurrentScheduleSlot()" in js


def test_vehicle_tab_contains_smart_charge_job_form_and_readable_daily_plan():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = MOBILE_CSS.read_text(encoding="utf-8")
    desktop_css = APP_CSS.read_text(encoding="utf-8")

    assert 'id="ev-smart-charge-form"' in html
    assert 'id="ev-smart-target-soc"' in html
    assert 'id="ev-smart-ready-date"' in html
    assert 'id="ev-smart-ready-time"' in html
    assert 'aria-label="Ready time, 24-hour format"' in html
    assert 'id="ev-smart-plan"' in html
    assert 'fetch("/api/ev/smart-charge"' in js
    assert "function renderEvSmartCharge" in js
    assert "function evSmartDailyPlan" in js
    assert "function evSmartPopulateTimeOptions" in js
    assert "the exact Tesla app schedule is shown below" in js
    assert "Matches the visible charging block" in js
    assert "Tesla deadline safety fallback" in js
    assert "Solar surplus is used when it costs less than the energy it replaces" in js
    assert 'source === "pending" ? "Source to be chosen"' in js
    assert "ev-charge-day" in js
    assert 'hour: "2-digit", minute: "2-digit", hour12: false' in js
    assert 'source[name] == null' in js
    assert 'about €${provisionalCost.toFixed(2)}' in js
    assert "function escapeHtml" in js
    assert "function requestEvSmartReplan" in js
    assert 'fetch("/api/replan", {method: "POST"})' in js
    assert ".ev-smart-form" in css
    assert ".ev-smart-actions[hidden]" in desktop_css


def test_daily_schedule_has_compact_ev_annotation_hooks():
    js = APP_JS.read_text(encoding="utf-8")

    assert "planned_ev_kwh" in js
    assert "ev_target_kw" in js
    assert "ev-slot-tag" in js
    assert "EV battery SoC" in js
    assert "EV charge rate" in js
    assert "energy_shortfall_kwh" in js
    assert "charge_cutoff" in js


def test_mobile_non_schedule_navigation_jumps_to_top():
    js = APP_JS.read_text(encoding="utf-8")

    assert "function jumpToMobileViewTop" in js
    assert 'window.scrollTo({ top: 0, behavior })' in js
    assert 'if (tabBtn.dataset.mobileTab === "schedule") scrollToCurrentScheduleSlot();' in js
    assert 'else jumpToMobileViewTop();' in js
    assert "jumpToMobileViewTop();" in js
    assert 'e.target.closest("button[data-replan]")' in js


def test_mobile_overview_hides_redundant_current_action_card():
    js = APP_JS.read_text(encoding="utf-8")
    css = MOBILE_CSS.read_text(encoding="utf-8")

    # The action chip carries no text label (removed the redundant "action" caption);
    # kv() is passed an empty label so it renders the chip alone.
    assert 'strip.appendChild(kv(chipFor(currentCA(c)), "", "status-action"))' in js
    assert "updateMobileKeyStat(currentCA(c), soc)" in js
    assert 'card("Current action", chipFor(currentCA(c)), "metric-current-action")' in js
    assert "  .status-strip .status-action,\n" in css
    assert ".status-strip .status-action small" not in css
    assert 'body[data-app-view="overview"] .metric-current-action' in css
    assert 'body[data-app-view="overview"] #decision' in css


def test_solar_card_shows_adjusted_remaining_and_vrm_source():
    js = APP_JS.read_text(encoding="utf-8")

    assert "pv_adjusted_remaining_wh" in js
    assert "pv_remaining_raw_wh" in js
    assert "VRM forecast" in js
    assert "adjusted remaining" in js


def test_external_frames_do_not_crop_desktop_content_or_mobile_scrollbars():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")
    mobile_css = MOBILE_CSS.read_text(encoding="utf-8")

    assert 'scrolling="no"' not in html
    assert "--frame-scrollbar-mask: 0px" in css
    assert "width: calc(100% + var(--frame-scrollbar-mask))" in css
    assert "margin-right: calc(-1 * var(--frame-scrollbar-mask))" in css
    # Desktop frames deliberately expose their whole cross-origin viewport; CSS scrollbar
    # properties cannot control an iframe's own scrollbar and a mask would crop real content.
    frame_rule_start = css.index(".battery-frame, .live-frame")
    frame_rule = css[frame_rule_start:css.index("}", frame_rule_start)]
    assert "scrollbar-width: none" not in frame_rule
    assert "-ms-overflow-style: none" not in frame_rule
    assert ".battery-frame::-webkit-scrollbar" not in css
    assert "--frame-scrollbar-mask: 48px" in mobile_css
    assert "--battery-frame-scale: 0.9" in mobile_css
    assert "--victron-frame-width: 768px" in mobile_css
    assert "--victron-frame-scale: 0.48" in mobile_css
    assert "width: var(--victron-frame-width)" in mobile_css
    assert "aspect-ratio: 4 / 5" in mobile_css
    assert "overscroll-behavior: contain" in mobile_css


def test_mobile_victron_view_keeps_the_parent_document_pinned():
    js = APP_JS.read_text(encoding="utf-8")

    assert "function keepMobileLiveViewPinned()" in js
    assert 'window.addEventListener("scroll", keepMobileLiveViewPinned' in js
    assert 'window.scrollTo({ top: 0, left: 0, behavior: "auto" })' in js


def test_desktop_logo_and_clear_schedule_js_hooks_exist():
    js = APP_JS.read_text(encoding="utf-8")

    assert "function goHome(" in js
    assert "setAppView(\"overview\")" in js
    assert "activateTab(\"live\")" in js
    assert "history.replaceState" in js
    assert "function clearImportSchedule(" in js
    assert 'fetch("/api/victron/clear-schedule", { method: "POST" })' in js


def test_operator_actions_do_not_use_browser_blocking_dialogs():
    js = APP_JS.read_text(encoding="utf-8")

    assert "confirm(" not in js
    assert "alert(" not in js
    assert "prompt(" not in js


def test_advisor_latest_report_loads_on_browser_startup():
    js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "function renderAdvisorRecord(" in js
    assert "function renderAdvisorChat(" in js
    assert "advisor-turn" in js
    assert "messages.slice().reverse()" in js
    assert "function loadAdvisorLatest(" in js
    assert 'fetch("/api/advisor/latest")' in js
    assert "loadAdvisorLatest();" in js
    assert "function clearAdvisorChat(" in js


def test_advisor_exposes_read_only_forecast_and_strategy_tools():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'id="advisor-forecast-validation"' in html
    assert 'id="advisor-ess-strategies"' in html
    assert 'fetch(`/api/advisor/tools/${tool}`' in js
    assert "CURRENT LIVE PLAN" in js
    assert 'fetch("/api/advisor/clear", { method: "POST" })' in js
    assert "function copyAdvisorMessage(" in js
    assert "function deleteAdvisorExchange(" in js
    assert "function advisorConfirm(" in js
    assert 'fetch("/api/advisor/delete-exchange"' in js
    assert "record.ok === false" not in js
    assert 'confirm("Delete this advisor exchange?")' not in js
    assert "Delete endpoint is not available" in js
    assert 'data-advisor-copy="' in js
    assert 'data-advisor-delete="' in js
    assert "Generated " in js
    assert "dateStyle" in js
    assert "timeStyle" in js
    assert 'id="advisor-clear"' in html
    assert ".advisor-turn" in css
    assert ".advisor-message-actions" in css
    assert ".advisor-turn-actions" in css
    assert ".advisor-modal-backdrop" in css
    assert ".advisor-modal" in css
    assert ".advisor-role-user" in css
    assert "background: #f8fafc" in css


def test_advisor_submission_clears_draft_and_prevents_duplicate_requests():
    js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="advisor-submit"' in html
    assert 'id="advisor-submit-status"' in html
    assert 'role="status"' in html
    assert "function setAdvisorBusy(" in js
    assert 'form.setAttribute("aria-busy", String(busy))' in js
    assert "function submitAdvisorQuestion(" in js
    assert 'ev.type === "accepted"' in js
    assert "const restoreDraft = () => {" in js
    assert "const clearDraft = () => {" in js
    assert "onAccepted: clearDraft" in js
    assert "onStartFailure: restoreDraft" in js
    assert "if (!started) {" in js
    assert "restoreDraft();" in js
    assert "if (_advisorBusy) return false;" in js
    assert "if (!accepted && onStartFailure) onStartFailure();" in js
    assert 'aria-label="Ask the AI Advisor a question"' in html


def test_advisor_run_details_and_sources_survive_completed_chat_rendering():
    js = APP_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert "function renderAdvisorRunDetails(" in js
    assert "function renderAdvisorSources(" in js
    assert 'class="advisor-run-details"' in js
    assert 'class="advisor-sources"' in js
    assert "Sources used" in js
    assert "run_details" in js
    assert "sources" in js
    assert "<details" in js
    assert ".advisor-run-details" in css
    assert ".advisor-sources" in css
    assert "chain-of-thought" not in js.lower()


def test_advisor_pending_run_details_are_expanded_then_saved_details_collapse():
    js = APP_JS.read_text(encoding="utf-8")

    assert 'opts && opts.pending ? " open" : ""' in js
    assert 'id="advisor-log"' in js
    assert 'id="advisor-run-status"' in js
    assert 'pending: true' in js


def test_config_editor_supports_labeled_model_choices_and_custom_cli_text():
    js = APP_JS.read_text(encoding="utf-8")

    assert "s.ui_options" in js
    assert 's.editor !== "text"' in js
    assert "option.value" in js
    assert "option.label" in js
    assert "knownValues.has(currentValue)" in js
    assert "Current selection" in js
    assert "s.effective_label" in js
    assert "s.editor_help" in js
    assert "_esc(description)" in js
    assert "function makeConfigValueEditable(" in js
    assert 'valueControl.setAttribute("role", "button")' in js
    assert 'valueControl.setAttribute("tabindex", "0")' in js


def test_advisor_markdown_tables_are_rendered_as_tables():
    js = APP_JS.read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "function _isMdTableSeparator" in js
    assert "function _mdTableToHtml" in js
    assert '<div class="advisor-table-wrap"><table>' in js
    assert "<thead><tr>" in js
    assert "<tbody>" in js
    assert "advisor-message-body table" in css
    assert ".advisor-table-wrap" in css


def test_trends_forecast_accuracy_overlay_exists_between_charts():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert 'id="forecast-accuracy-chart"' in html
    assert html.index('id="horizon-chart"') < html.index('id="forecast-accuracy-chart"')
    assert html.index('id="forecast-accuracy-chart"') < html.index('id="monthly-chart"')
    assert 'fetch("/api/history/accuracy")' in js
    assert "refreshForecastAccuracy();" in js
    assert "renderForecastAccuracyChart" in charts
    assert "Forecast accuracy" in charts


def test_forecast_accuracy_chart_has_tooltips_now_marker_and_toggles():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert "forecast-accuracy-hover" in charts
    assert "Forecast vs actual" in charts
    assert "forecast-now-line" in charts
    assert "forecast-now-label" in charts
    assert "data-acc-toggle=\"load\"" in charts
    assert "data-acc-toggle=\"pv\"" in charts
    assert "Mean absolute error" in charts
    assert "toggleForecastAccuracySeries" in charts


def test_horizon_weather_and_weather_impact_legends_are_toggleable():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert 'data-horizon-toggle="soc"' in charts
    assert 'data-horizon-toggle="price"' in charts
    assert "toggleHorizonSeries" in charts
    assert 'data-weather-toggle="temp"' in charts
    assert 'data-weather-toggle="cloud"' in charts
    assert "toggleWeatherSeries" in charts
    assert 'data-weather-impact-toggle="load"' in charts
    assert 'data-weather-impact-toggle="gti"' in charts
    assert "toggleWeatherImpactSeries" in charts


def test_desktop_weather_tab_exists_without_mobile_nav_entry():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert '<button class="tab" data-tab="weather">Weather</button>' in html
    assert html.index('data-tab="victron"') < html.index('data-tab="weather"')
    assert html.index('data-tab="weather"') < html.index('data-tab="advisor"')
    assert 'id="tab-weather"' in html
    assert 'id="weather-chart"' in html
    assert 'id="weather-impact-chart"' in html
    assert 'data-mobile-tab="weather"' not in html
    assert 'fetch("/api/weather")' in js
    assert "refreshWeather();" in js
    assert "renderWeatherChart" in charts
    assert "renderWeatherImpactChart" in charts


def test_weather_charts_include_interactive_tooltips():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "installWeatherTooltip" in charts
    assert "weather-tip" in charts
    assert "weather-now-line" in charts
    assert "weather-impact-today-line" in charts
    assert "Weather forecast" in charts
    assert "HVAC Load" in charts
    assert "GTI irradiance" in charts
    assert "mousemove" in charts
    assert "touchstart" in charts
    assert ".chart-tip.weather-tip" in css
    assert "white-space: normal" in css
    assert "min-width: 220px" in css


def test_horizon_weather_and_impact_tooltips_use_large_multiline_style():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'tip.className = "chart-tip rich-tip";' in charts
    assert 'tip.className = `chart-tip rich-tip${tipClass ? " " + tipClass : ""}`;' in charts
    assert ".chart-tip.rich-tip" in css
    assert ".chart-tip.rich-tip span { display: block" in css
    assert "font-size: 14px" in css


def test_monthly_chart_has_rich_daily_net_tooltip():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert 'tip.className = "chart-tip rich-tip monthly-tip";' in charts
    assert "Import " in charts
    assert "Export " in charts


def test_header_has_accessible_server_offline_overlay_and_staleness_watchdog():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'id="server-offline-banner"' in html
    assert 'role="status"' in html and 'aria-live="assertive"' in html
    assert "Server Offline" in html
    assert "SERVER_OFFLINE_AFTER_MS" in js
    assert "noteServerData" in js
    assert "noteServerFailure" in js
    assert "_liveES.onerror" in js
    assert "server offline — showing last data" in js
    assert "error loading:" not in js
    assert ".server-offline-banner" in css


def test_pl_summary_includes_human_readable_remaining_day_strategy():
    js = APP_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert "function planStrategySummary" in js
    assert '"pl-strategy"' in js
    assert "until midnight" in js
    assert "Buy low" in js
    assert ".pl-strategy" in css


def test_pl_summary_explains_winter_household_protection_policy():
    js = APP_JS.read_text(encoding="utf-8")

    assert 'plan.optimizer_mode === "winter"' in js
    assert "winter_policy" in js
    assert "protected household requirement" in js
    assert "An exceptional spread cleared every loss and safety hurdle" in js
    assert "Winter Mode degraded safely" in js


def test_monthly_chart_uses_forecast_spread_and_comparable_actual_markers():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert "forecast_q1_eur" in charts
    assert "forecast_median_eur" in charts
    assert "forecast_q3_eur" in charts
    assert "forecast_range_low_eur" in charts
    assert "forecast_range_high_eur" in charts
    assert "forecast_outliers_eur" not in charts
    assert 'class="forecast-boxplot"' in charts
    assert 'class="forecast-outlier-dot"' not in charts
    assert "actual-net-dot" in charts
    assert "Box: middle 50% of observed forecasts" in charts
    assert "Centre line: median forecast" in charts
    assert "Range: lowest–highest forecast observed" in charts
    assert "Solid dot: settled actual" in charts
    assert "Hollow dot: latest full-day forecast for today" in charts
    assert "Settled so far" in charts
    assert "projected_net_eur" in charts
    assert "one per 15-minute period" in charts
    assert "minimum 8" in charts
    assert "First → latest" not in charts


def test_schedule_timeline_has_running_today_ledger_row():
    js = APP_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert "makeRunningLedgerRow" in js
    assert "makeForecastedLedgerRow" in js
    assert "running-ledger-row" in js
    assert "forecasted-ledger-row" in js
    assert "todayRunningNet" in js
    assert "todayForecastedNet" in js
    assert "box.appendChild(makeRunningLedgerRow(plan));" in js
    assert "const hourDayKey = (h) =>" in js
    assert "key.slice(0, 10)" in js
    assert "lastTodayHourKey" in js
    assert "if (h.key === lastTodayHourKey)" in js
    assert ".running-ledger-row" in css
    assert ".forecasted-ledger-row" in css
