from app.config import Settings


def test_settings_parse_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/mem")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.embedding_dim == 1536
