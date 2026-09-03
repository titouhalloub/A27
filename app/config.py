"""Application settings loaded from environment (pydantic-settings)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="A27_", extra="ignore")

    # --- Database -----------------------------------------------------------
    database_url: str = "sqlite:///./a27.db"

    # --- Anthropic (classification / extraction) ----------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    classification_min_confidence: float = 0.75
    extraction_min_confidence: float = 0.85

    # --- Langfuse observability ---------------------------------------------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # Set to "console" to emit traces without network / credentials in dev.
    telemetry_backend: str = "console"

    # --- Security -----------------------------------------------------------
    # KYC/AML and other PII documents are encrypted at rest with this key
    # (Fernet). Access to those rows is role-scoped at the API layer.
    # Retention: 7 years, matching common money-laundering regulations.
    document_encryption_key: str = ""  # base64 32-byte Fernet key
    pii_retention_days: int = 2557  # 7 years

    # --- API auth -------------------------------------------------------
    # Comma-separated allowlist of API keys. Empty means the API fails
    # closed (503 on every route) rather than silently open -- see
    # app.main.require_api_key.
    api_keys: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()