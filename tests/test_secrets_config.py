import os

import yaml

from nexus.core import secrets
from nexus.core.config import Config


def test_env_file_parsing(tmp_path, monkeypatch):
    env = tmp_path / "custom.env"
    env.write_text('# comment\nFOO_A=plain\nFOO_B="quoted value"\nexport FOO_C=x # trailing\n', encoding="utf-8")
    monkeypatch.setenv("AUTOCRAT_ENV_FILE", str(env))
    for k in ("FOO_A", "FOO_B", "FOO_C"):
        monkeypatch.delenv(k, raising=False)
    secrets.reset_for_tests()
    assert secrets.get_secret("FOO_A") == "plain"
    assert secrets.get_secret("FOO_B") == "quoted value"
    assert secrets.get_secret("FOO_C") == "x"


def test_secret_paths_come_from_env_not_yaml(tmp_path, monkeypatch):
    (tmp_path / "nexus_config.yaml").write_text(yaml.safe_dump({
        "auth": {"token": "LEAKED-IN-YAML"},
        "ai": {"gemini_api_key": "yaml-key", "local_model": "m"},
        "telegram": {"bot_token": "yaml-bot"},
    }), encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    Config.reset_for_tests()
    cfg = Config()
    assert cfg.get("auth", "token") == os.environ["AUTOCRAT_AUTH_TOKEN"]
    assert cfg.get("ai", "gemini_api_key") == "env-key"
    assert cfg.get("telegram", "bot_token") in ("", None)
    assert cfg.get("ai", "local_model") == "m"


def test_save_never_writes_secrets(tmp_path):
    Config.reset_for_tests()
    cfg = Config()
    cfg.data.setdefault("auth", {})["token"] = "should-not-persist"
    cfg.data.setdefault("ai", {})["gemini_api_key"] = "nope"
    cfg.save()
    text = (tmp_path / "nexus_config.yaml").read_text(encoding="utf-8")
    assert "should-not-persist" not in text and "nope" not in text


def test_loading_config_does_not_rewrite_file(tmp_path):
    path = tmp_path / "nexus_config.yaml"
    path.write_text("nexus:\n  name: Autocrat\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    Config.reset_for_tests()
    Config()
    assert path.read_text(encoding="utf-8") == before


def test_auth_token_generated_into_env_file_when_missing(tmp_path, monkeypatch):
    env = tmp_path / "gen.env"
    monkeypatch.setenv("AUTOCRAT_ENV_FILE", str(env))
    monkeypatch.delenv("AUTOCRAT_AUTH_TOKEN")
    secrets.reset_for_tests()
    token = secrets.auth_token()
    assert len(token) >= 40
    assert f"AUTOCRAT_AUTH_TOKEN={token}" in env.read_text(encoding="utf-8")
    assert secrets.mask(token).endswith("chars]") and token not in secrets.mask(token)
