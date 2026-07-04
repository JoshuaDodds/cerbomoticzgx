from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "frontend" / "templates" / "index.html"
APP_JS = ROOT / "frontend" / "static" / "js" / "app.js"
APP_CSS = ROOT / "frontend" / "static" / "css" / "app.css"
MOBILE_CSS = ROOT / "frontend" / "static" / "css" / "app.mobile.css"


def test_mobile_stylesheet_loads_after_desktop_stylesheet():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'content="width=device-width, initial-scale=1, viewport-fit=cover"' in html
    assert "css/app.mobile.css" in html
    assert html.index("css/app.css") < html.index("css/app.mobile.css")


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
    assert 'const APP_VIEWS = ["overview", "ess", "battery", "live"]' in js
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
    assert "--mobile-frame-scale: 0.9" in css
    assert "--mobile-frame-fit: 111.111%" in css
    assert "transform: scale(var(--mobile-frame-scale))" in css
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

    assert 'strip.appendChild(kv(chipFor(currentCA(c)), "action", "status-action"))' in js
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


def test_external_frames_hide_scrollbars_in_desktop_and_mobile():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")
    mobile_css = MOBILE_CSS.read_text(encoding="utf-8")

    assert 'scrolling="no"' not in html
    assert "--frame-scrollbar-mask: 48px" in css
    assert "width: calc(100% + var(--frame-scrollbar-mask))" in css
    assert "margin-right: calc(-1 * var(--frame-scrollbar-mask))" in css
    assert "scrollbar-width: none" not in css
    assert "-ms-overflow-style: none" not in css
    assert ".battery-frame::-webkit-scrollbar" not in css
    assert "--frame-scrollbar-mask: 48px" in mobile_css
    assert "width: calc(var(--mobile-frame-fit) + var(--frame-scrollbar-mask))" in mobile_css
    assert "overscroll-behavior: none" in mobile_css


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


def test_monthly_chart_renders_projected_today_marker():
    charts = (ROOT / "frontend" / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert "projected_net_eur" in charts
    assert "projected-today" in charts
    assert "Projected full day" in charts
    assert 'tip.className = "chart-tip rich-tip monthly-tip";' in charts
    assert "Import " in charts
    assert "Export " in charts


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
