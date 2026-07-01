from pathlib import Path


def test_retrieve_setting_reads_app_env_path(monkeypatch, tmp_path):
    import importlib
    import sys
    sys.modules.pop("lib.config_retrieval", None)
    config_retrieval = importlib.import_module("lib.config_retrieval")

    env_path = tmp_path / "runtime.env"
    env_path.write_text("SOME_SETTING=from-mounted-env\n")

    monkeypatch.setenv("APP_ENV_PATH", str(env_path))
    monkeypatch.setattr(config_retrieval.STATE, "get", lambda key: None)
    monkeypatch.setattr(config_retrieval, "publish_message", lambda *args, **kwargs: None)
    if hasattr(config_retrieval.retrieve_setting, "_secrets"):
        delattr(config_retrieval.retrieve_setting, "_secrets")

    assert config_retrieval.retrieve_setting("SOME_SETTING") == "from-mounted-env"


def test_default_env_path_is_repo_local(monkeypatch):
    from lib.config_paths import env_path

    monkeypatch.delenv("APP_ENV_PATH", raising=False)

    assert Path(env_path()).as_posix() == ".env"
