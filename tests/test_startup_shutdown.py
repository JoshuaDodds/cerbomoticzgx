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
