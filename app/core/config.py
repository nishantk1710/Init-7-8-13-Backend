"""Environment-driven application settings.

Values are read from the process environment first and from a local ``.env``
file as a fallback, so the same code runs unchanged locally and on Azure App
Service (where settings are supplied as environment variables).

Nothing here is required for startup: every future-integration setting defaults
to empty so the app -- and the health endpoint -- come up with no SAP, database
or identity credentials present.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    app_name: str = "spares-ai-backend"
    app_version: str = "0.1.0"
    api_prefix: str = "/api"
    log_level: str = "INFO"

    # Browser origin(s) allowed to call this API. Comma-separated for multiple.
    frontend_origin: str = "http://localhost:3000"

    # Database connection, as a SQLAlchemy URL. Empty by default so the app --
    # and the liveness endpoint -- still start with no database present.
    #
    # This is the ONLY place the database is named. Local development points it
    # at Postgres; the deployed environment points it at the managed database.
    # Swapping environments is therefore a config change, never a code change:
    # nothing below this line, and nothing in app/core/db.py, knows which
    # engine it is talking to.
    #
    #   local:  postgresql+psycopg://postgres:<password>@localhost:5432/spares_ai
    #
    # NOTE: a Postgres-to-SQL-Server move is NOT config-only -- dialect, driver
    # and several types differ. Keep models on portable SQLAlchemy constructs so
    # that swap stays small. See README, "Database".
    database_url: str = ""

    # Echo every SQL statement to the log. Local debugging only.
    database_echo: bool = False

    # Object storage location. Like database_url, this is the ONLY place storage
    # is named, and the adapter is chosen from the URL scheme -- so moving from a
    # local folder to cloud storage is a config change, not a code change.
    #
    #   local:  D:/vzi-data/extracts        (a plain path is accepted)
    #           file:///D:/vzi-data/extracts
    #   cloud:  abfss://<container>@<account>.dfs.core.windows.net/<path>
    #
    # Empty by default so the app starts with no storage configured.
    storage_url: str = ""

    # --- Reserved for future integrations. Not read by any code yet, and not
    # --- required for startup. See app/core/security.py and app/integrations/.
    sap_base_url: str = ""
    sap_client_id: str = ""
    sap_client_secret: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""

    @property
    def cors_origins(self) -> list[str]:
        """Allowed CORS origins, parsed from ``FRONTEND_ORIGIN``."""
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
