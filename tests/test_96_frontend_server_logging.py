import logging

from frontend import server


def test_flask_startup_banner_uses_project_logger_and_restores_flask(monkeypatch, caplog):
    original_banner = server.flask_cli.show_server_banner

    monkeypatch.setattr(server.live, "start", lambda: None)
    monkeypatch.setattr(server, "_host_port", lambda: ("127.0.0.1", 7070))
    monkeypatch.setattr(server, "_debug_enabled", lambda: False)

    def fake_run(**kwargs):
        assert kwargs == {
            "host": "127.0.0.1",
            "port": 7070,
            "threaded": True,
            "use_reloader": False,
        }
        server.flask_cli.show_server_banner(False, server.app.name)

    monkeypatch.setattr(server.app, "run", fake_run)

    with caplog.at_level(logging.INFO):
        server.run()

    assert "Serving Flask app 'frontend.server'." in caplog.text
    assert "Flask debug mode: off." in caplog.text
    assert server.flask_cli.show_server_banner is original_banner
