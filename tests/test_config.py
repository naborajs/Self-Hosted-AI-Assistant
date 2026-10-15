import importlib


def test_settings_load(monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")

    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$fakehashvalue")
    monkeypatch.setenv("WHATSAPP_BRIDGE_SECRET", "bridge-secret-123")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:8000")

    import config
    importlib.reload(config)
    settings = config.Settings()

    assert settings.whatsapp_phone == "+1234567890"
    assert settings.telegram_bot_token == "token"
    assert settings.ollama_model == "qwen3:8b"
    assert settings.admin_password_hash.get_secret_value() == "$2b$12$fakehashvalue"
    assert settings.whatsapp_bridge_secret.get_secret_value() == "bridge-secret-123"
    assert "http://localhost:3000" in settings.cors_origins_list
    assert "http://127.0.0.1:8000" in settings.cors_origins_list
