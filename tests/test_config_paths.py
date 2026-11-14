from pathlib import Path


def _reset_env_cache(config_retrieval):
    for name in ("_revision", "_values"):
        if hasattr(config_retrieval._env_values, name):
            delattr(config_retrieval._env_values, name)


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
    _reset_env_cache(config_retrieval)
    if hasattr(config_retrieval.retrieve_setting, "_secrets"):
        delattr(config_retrieval.retrieve_setting, "_secrets")

    assert config_retrieval.retrieve_setting("SOME_SETTING") == "from-mounted-env"


def test_retrieve_setting_parses_one_env_revision_once(monkeypatch, tmp_path):
    import lib.config_retrieval as config_retrieval

    env_file = tmp_path / "runtime.env"
    secrets_file = tmp_path / "secrets"
    env_file.write_text("FIRST=one\nSECOND=two\n")
    secrets_file.write_text("")
    real_dotenv_values = config_retrieval.dotenv_values
    calls = []

    def counted_dotenv_values(path):
        calls.append(str(path))
        return real_dotenv_values(path)

    monkeypatch.setenv("APP_ENV_PATH", str(env_file))
    monkeypatch.setattr(config_retrieval.STATE, "get", lambda key: None)
    monkeypatch.setattr(config_retrieval, "publish_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_retrieval, "dotenv_values", counted_dotenv_values)
    monkeypatch.setattr(config_retrieval, "secrets_path", lambda: str(secrets_file))
    monkeypatch.setattr(config_retrieval.retrieve_setting, "_secrets", {})
    monkeypatch.setattr(
        config_retrieval.retrieve_setting, "_secrets_path", str(secrets_file))
    _reset_env_cache(config_retrieval)

    assert config_retrieval.retrieve_setting("FIRST") == "one"
    assert config_retrieval.retrieve_setting("SECOND") == "two"
    assert calls == [str(env_file)]


def test_retrieve_setting_detects_atomic_env_replacement(monkeypatch, tmp_path):
    import os

    import lib.config_retrieval as config_retrieval

    env_file = tmp_path / "runtime.env"
    replacement = tmp_path / "replacement.env"
    secrets_file = tmp_path / "secrets"
    env_file.write_text("VALUE=before\n")
    secrets_file.write_text("")

    monkeypatch.setenv("APP_ENV_PATH", str(env_file))
    monkeypatch.setattr(config_retrieval.STATE, "get", lambda key: None)
    monkeypatch.setattr(config_retrieval, "publish_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_retrieval, "secrets_path", lambda: str(secrets_file))
    monkeypatch.setattr(config_retrieval.retrieve_setting, "_secrets", {})
    monkeypatch.setattr(
        config_retrieval.retrieve_setting, "_secrets_path", str(secrets_file))
    _reset_env_cache(config_retrieval)

    assert config_retrieval.retrieve_setting("VALUE") == "before"
    replacement.write_text("VALUE=after\n")
    os.replace(replacement, env_file)
    assert config_retrieval.retrieve_setting("VALUE") == "after"


def test_default_env_path_is_repo_local(monkeypatch):
    from lib.config_paths import env_path

    monkeypatch.delenv("APP_ENV_PATH", raising=False)

    assert Path(env_path()).as_posix() == ".env"
