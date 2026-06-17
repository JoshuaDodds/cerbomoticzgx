"""Flask web server for the cerbomoticzGx dashboard (read-only v1).

Runs standalone (own process / container sidecar) via ``python -m frontend`` or
can be started as a daemon thread from the main service via ``run_in_thread()``.
"""
import os
import logging
import threading

from flask import Flask, jsonify, render_template, request

from frontend import data
from frontend.live import live

app = Flask(__name__, static_folder="static", template_folder="templates")
# Don't let browsers cache the dashboard's JS/CSS — it's edited often and served
# on a trusted LAN, so always revalidate (avoids stale powerflow.js/charts.js
# after an update without needing a hard refresh).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/plan")
def api_plan():
    return jsonify(data.get_plan())


@app.route("/api/config")
def api_config():
    return jsonify({"groups": data.get_config()})


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
