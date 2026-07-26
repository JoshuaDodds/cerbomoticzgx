from pathlib import Path


def test_startup_clears_retained_shutdown_flag_after_detecting_restart():
    text = Path("main.py").read_text()

    detect = 'retrieve_message("Cerbomoticzgx/system/shutdown")'
    manual = 'Cerbomoticzgx/system/manual_restart", message="True", retain=True'
    clear = 'Cerbomoticzgx/system/shutdown", message="False", retain=True'

    assert detect in text
    assert manual in text
    assert clear in text
    assert text.index(detect) < text.index(manual) < text.index(clear)


def test_ev_controller_is_backgrounded_and_telemetry_starts_first():
    text = Path("main.py").read_text()

    assert 'name="ev-charge-controller"' in text
    assert 'name="tesla-telemetry-bridge"' in text
    assert "daemon=True" in text
    assert "thread.start()" in text
    telemetry = text.index("_start_tesla_telemetry_bridge()", text.index("def main():"))
    services = text.index("sync_tasks_start()", text.index("def main():"))
    assert telemetry < services
