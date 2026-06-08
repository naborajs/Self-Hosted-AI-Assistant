import importlib


def test_settings_load(monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")

    import config
    importlib.reload(config)
    settings = config.Settings()

    assert settings.whatsapp_phone == "+1234567890"
    assert settings.telegram_bot_token == "token"
    assert settings.ollama_model == "qwen3:8b"
