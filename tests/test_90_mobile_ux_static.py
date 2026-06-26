from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "frontend" / "templates" / "index.html"
APP_JS = ROOT / "frontend" / "static" / "js" / "app.js"
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
    assert "id=\"mobile-key-stat\"" in html
    assert "Import Schedule" in html
    assert "ESS dashboard" not in html
    assert "Venus" in html


def test_mobile_logo_has_home_action_hook():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "data-mobile-home" in html
    assert "data-home" in html


def test_import_schedule_tab_has_clear_schedule_action():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<button class="tab" data-tab="victron">Import Schedule</button>' in html
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
    assert 'body[data-mobile-tab="live"] .overview' in css
    assert 'body[data-mobile-tab="trends"] .overview' in css
    assert 'body[data-mobile-tab="advisor"] .overview' in css
    assert 'body[data-mobile-tab="victron"] .overview' in css
    assert 'body[data-mobile-tab="config"] .overview' in css
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
    assert 'closest("button[data-mobile-tab]")' in js
    assert 'closest("button[data-mobile-app-view]")' in js


def test_desktop_logo_and_clear_schedule_js_hooks_exist():
    js = APP_JS.read_text(encoding="utf-8")

    assert "function goHome(" in js
    assert "setAppView(\"ess\")" in js
    assert "activateTab(\"schedule\")" in js
    assert "history.replaceState" in js
    assert "function clearImportSchedule(" in js
    assert 'fetch("/api/victron/clear-schedule", { method: "POST" })' in js
    assert "Clear Import Schedule?" in js


def test_advisor_latest_report_loads_on_browser_startup():
    js = APP_JS.read_text(encoding="utf-8")

    assert "function renderAdvisorRecord(" in js
    assert "function loadAdvisorLatest(" in js
    assert 'fetch("/api/advisor/latest")' in js
    assert "loadAdvisorLatest();" in js
    assert "Generated " in js
    assert "dateStyle" in js
    assert "timeStyle" in js
