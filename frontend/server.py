"""Flask web server for the cerbomoticzGx dashboard.

Runs standalone (own process / container sidecar) via ``python -m frontend`` or
can be started as a daemon thread from the main service via ``run_in_thread()``.
"""
import os
import json
import logging
import threading

from flask import Flask, jsonify, render_template, request, Response, redirect, url_for

from frontend import data
from frontend.live import live
from lib.helpers import publish_message

app = Flask(__name__, static_folder="static", template_folder="templates")
# Don't let browsers cache the dashboard's JS/CSS — it's edited often and served
# on a trusted LAN, so always revalidate (avoids stale powerflow.js/charts.js
# after an update without needing a hard refresh).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Also re-read templates/index.html on each request (otherwise Jinja caches it at
# startup and template edits — new tabs, the logo — need a full restart to show).
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="img/logo.svg"), code=302)


@app.route("/api/plan")
def api_plan():
    return jsonify(data.get_plan())


@app.route("/api/config")
def api_config():
    return jsonify({"groups": data.get_config()})


@app.route("/api/history/month")
def api_history_month():
    """Per-day net totals for the current month (Trends monthly chart)."""
    return jsonify({"days": data.monthly_history()})


@app.route("/api/history/accuracy")
def api_history_accuracy():
    """Recent actual-vs-forecast PV/load settlements for the Trends overlay."""
    try:
        days = max(1, min(14, int(request.args.get("days", 3))))
    except (TypeError, ValueError):
        days = 3
    return jsonify(data.forecast_accuracy(days))


@app.route("/api/weather")
def api_weather():
    """Cached weather forecast and shadow adjustment summary."""
    return jsonify(data.weather_dashboard())


@app.route("/api/tesla/usage")
def api_tesla_usage():
    """Today's Tesla Fleet API spend (counts + cost per category + total)."""
    return jsonify(data.tesla_usage())


@app.route("/api/history/day")
def api_history_day():
    """Settled hour-tree for a prior day (default yesterday), lazy-loaded when the
    user expands the previous-day row beneath the schedule."""
    try:
        days_back = max(1, int(request.args.get("days_back", 1)))
    except (TypeError, ValueError):
        days_back = 1
    return jsonify(data.previous_day_schedule(days_back))


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """Persist a single allow-listed setting to .env (applies on next cycle)."""
    body = request.get_json(silent=True) or {}
    key, value = body.get("key"), body.get("value")
    if key is None or value is None:
        return jsonify({"ok": False, "error": "key and value are required"}), 400
    try:
        saved = data.update_env_setting(key, value)
        return jsonify({"ok": True, **saved})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"}), 500


@app.route("/api/live")
def api_live():
    return jsonify(live.snapshot())


@app.route("/api/live/stream")
def api_live_stream():
    """Server-Sent Events: push the live snapshot the instant a new MQTT value
    arrives (no browser polling). Falls back gracefully — the browser also keeps a
    slow poll in case the stream drops or is buffered by a proxy."""
    def gen():
        yield f"data: {json.dumps(live.snapshot())}\n\n"
        while True:
            live.wait_for_change(timeout=15)        # wakes instantly on a new value; 15s = keepalive
            yield f"data: {json.dumps(live.snapshot())}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/api/replan", methods=["POST"])
def api_replan():
    """Re-run the optimizer now (the dashboard 'Replan' button) — the very same
    function the 15-minute scheduler calls. Works when the dashboard runs
    in-process (FRONTEND_ENABLED=True); a deliberate, explicit action. It runs
    synchronously and republishes the plan, so the caller can reload immediately."""
    try:
        from lib.energy_broker import run_ai_optimizer
        ran = run_ai_optimizer()
        if ran is False:
            return jsonify({
                "ok": False,
                "skipped": True,
                "message": "optimizer already running",
            }), 409
        return jsonify({"ok": True})
    except Exception as e:
        logging.warning("Replan failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def _clear_import_schedule():
    from lib.helpers import clear_victron_schedules
    clear_victron_schedules()


def _request_service_restart():
    publish_message("Cerbomoticzgx/system/shutdown", message="True", retain=True)


def _boolish(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _set_ai_ess_override(enabled: bool):
    """Persist the runtime AI ESS override and idle Victron once when enabling.

    The optimizer reads this state and stands down while it is on; it does not keep
    writing setpoints, so external/Victron changes remain undisturbed afterwards.
    """
    from lib.global_state import GlobalStateClient
    from lib.victron_integration import ac_power_setpoint

    state = GlobalStateClient()
    state.set("ai_ess_override_enabled", bool(enabled))
    publish_message("Cerbomoticzgx/system/ai_ess_override_enabled", message="True" if enabled else "False", retain=True)
    if enabled:
        state.set("ai_grid_assist", "off")
        ac_power_setpoint(watts="0.0", override_ess_net_mettering=False, silent=False)


def _set_grid_assist_toggle(enabled: bool):
    """Reuse the existing manual grid-charge/retain toggle."""
    from lib.global_state import GlobalStateClient
    from lib.energy_broker import _apply_grid_assist_setpoint
    from lib.victron_integration import ac_power_setpoint

    state = GlobalStateClient()
    state.set("grid_charging_enabled", bool(enabled))
    publish_message("Cerbomoticzgx/system/grid_charging_enabled", message="True" if enabled else "False", retain=True)
    if enabled:
        load_watts = state.get("ac_out_power")
        state.set("ai_grid_assist", "on")
        _apply_grid_assist_setpoint(
            load_watts=load_watts,
            cover_all_load=True,
        )
        try:
            load_label = f"{int(round(float(load_watts or 0)))}W"
        except (TypeError, ValueError):
            load_label = "unknown load"
        logging.info("Grid assist enabled: matching grid setpoint to current AC load %s.", load_label)
    else:
        state.set("ai_grid_assist", "off")
        ac_power_setpoint(watts="0.0", override_ess_net_mettering=False, silent=False)
        logging.info("Grid assist disabled: returned AC setpoint to 0W.")


@app.route("/api/victron/clear-schedule", methods=["POST"])
def api_victron_clear_schedule():
    """Clear all five Victron scheduled-charge slots."""
    try:
        _clear_import_schedule()
        return jsonify({"ok": True})
    except Exception as e:
        logging.exception("Clear Victron import schedule failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """Request the existing supervised restart path via MQTT.

    The service already subscribes to Cerbomoticzgx/system/shutdown, kills its own
    process when it sees True, and the outer loop handles the restart. Do not add a
    second restart mechanism here.
    """
    try:
        _request_service_restart()
        return jsonify({"ok": True})
    except Exception as e:
        logging.warning("Restart request failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/control/ai-override", methods=["POST"])
def api_control_ai_override():
    body = request.get_json(silent=True) or {}
    enabled = _boolish(body.get("enabled"), False)
    try:
        _set_ai_ess_override(enabled)
        return jsonify({"ok": True, "enabled": enabled})
    except Exception as e:
        logging.warning("AI ESS override request failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/control/grid-assist", methods=["POST"])
def api_control_grid_assist():
    body = request.get_json(silent=True) or {}
    enabled = _boolish(body.get("enabled"), False)
    try:
        _set_grid_assist_toggle(enabled)
        return jsonify({"ok": True, "enabled": enabled})
    except Exception as e:
        logging.warning("Grid assist request failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def _set_ev_charge_requested(enabled: bool):
    """Manual EV Start/Stop. Sets the DEDICATED ev_charge_requested intent flag the EV controller
    reads (fully decoupled from grid-assist). The controller then starts/stops the car with its
    full safety logic — home+plugged+non-supercharging checks, wake escalation, and local-meter
    stop verification. A direct one-shot command would just be undone by the controller's next
    tick, so we drive its intent flag instead. Publishing the retained control topic keeps it in
    sync + survives a restart via the state restore."""
    from lib.global_state import GlobalStateClient
    GlobalStateClient().set("ev_charge_requested", bool(enabled))
    publish_message("Tesla/vehicle0/control/charge_requested",
                    message="True" if enabled else "False", retain=True)
    logging.info("Manual EV charge %s.", "START requested" if enabled else "STOP requested")


@app.route("/api/control/ev-charge", methods=["POST"])
def api_control_ev_charge():
    body = request.get_json(silent=True) or {}
    enabled = _boolish(body.get("enabled"), False)
    try:
        _set_ev_charge_requested(enabled)
        return jsonify({"ok": True, "enabled": enabled})
    except Exception as e:
        logging.warning("EV charge request failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/advisor", methods=["POST"])
def api_advisor():
    """Run the read-only AI advisor — a default daily review, or answer an open
    question (e.g. "Why did we sell at 15:00 yesterday?"). Never writes config or
    control; only allow-listed tunables + performance data are sent to the API.
    Blocking (the model call takes a few seconds); threaded=True keeps the UI free."""
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip() or None
    try:
        from frontend import advisor
        return jsonify(advisor.run(question))
    except Exception as e:
        logging.warning("Advisor failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/advisor/stream")
def api_advisor_stream():
    """Server-Sent Events stream of the advisor run — progress stages, live CLI log
    lines, and the model's output as it arrives — so the UI is transparent instead
    of hanging silently. The lock + any subprocess are released when the client
    disconnects (the generator is closed)."""
    question = (request.args.get("question") or "").strip() or None

    def gen():
        from frontend import advisor
        try:
            for ev in advisor.run_stream(question):
                yield f"data: {json.dumps(ev)}\n\n"
        except GeneratorExit:                    # client disconnected
            raise
        except Exception as e:                   # never break the stream silently
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/api/advisor/latest")
def api_advisor_latest():
    """Return the persisted advisor chat session, if one exists."""
    from frontend import advisor
    return jsonify(advisor.latest_report())


@app.route("/api/advisor/clear", methods=["POST"])
def api_advisor_clear():
    """Clear the persisted advisor chat session."""
    from frontend import advisor
    return jsonify(advisor.clear_chat())


@app.route("/api/advisor/delete-exchange", methods=["POST"])
def api_advisor_delete_exchange():
    """Delete one persisted advisor exchange by message index."""
    from frontend import advisor
    body = request.get_json(silent=True) or {}
    try:
        index = int(body.get("index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "message index is required"}), 400
    try:
        return jsonify(advisor.delete_exchange(index))
    except IndexError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        logging.warning("Advisor exchange delete failed: %s", e)
        return jsonify({"ok": False, "error": f"delete failed: {e}"}), 500


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


def _host_port():
    env = data._env()
    host = env.get("FRONTEND_HOST") or os.environ.get("FRONTEND_HOST") or "0.0.0.0"
    try:
        port = int(env.get("FRONTEND_PORT") or os.environ.get("FRONTEND_PORT") or 8080)
    except (TypeError, ValueError):
        port = 8080
    return host, port


def _debug_enabled() -> bool:
    env = data._env()
    raw = env.get("FRONTEND_DEBUG") or os.environ.get("FRONTEND_DEBUG") or ""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def run():
    """Run the server in the foreground (blocking)."""
    # Per-request HTTP logging (werkzeug) is noisy and, when the dashboard runs
    # in-process, pollutes the main service log — silence it unless FRONTEND_DEBUG
    # is on. Errors still surface (level ERROR).
    if not _debug_enabled():
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
    live.start()  # begin caching live MQTT values
    host, port = _host_port()
    # threaded=True so concurrent requests don't block each other; the process
    # itself is independent of the main service threads.
    app.run(host=host, port=port, threaded=True, use_reloader=False)


def run_in_thread() -> threading.Thread:
    """Start the server in a daemon thread (optional in-process mode)."""
    t = threading.Thread(target=run, name="frontend-dashboard", daemon=True)
    t.start()
    return t


def main():
    run()


if __name__ == "__main__":
    main()
