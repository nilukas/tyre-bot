"""Application settings, read once from environment variables.

No secrets live here: Azure services are reached with Managed Identity
(DefaultAzureCredential) and the Entra ID login uses a federated
credential backed by that same identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


@dataclass(frozen=True)
class Settings:
    app_env: str  # "development" | "production"
    public_base_url: str  # e.g. https://tyres.example.com

    # --- Claude on Microsoft Foundry -------------------------------------
    foundry_resource: str
    claude_deployment: str  # Foundry deployment name (defaults to model id)

    # --- Entra ID (BFF / confidential client, no client secret) ----------
    entra_authority: str  # https://login.microsoftonline.com/<tenant-id> or External ID authority
    entra_client_id: str
    # Client ID of the user-assigned Managed Identity that is configured as a
    # Federated Identity Credential on the app registration.
    managed_identity_client_id: str
    admin_app_role: str  # App role required to manage the reference library

    # --- Switches -----------------------------------------------------------
    # DATA_BACKEND: "azure_blob" (production) | "sqlserver" (local SQL Server) | "local_files"
    data_backend: str
    # AI_BACKEND: "foundry" (Claude on Microsoft Foundry) | "mock" (offline fake, no cost)
    ai_backend: str

    # --- Storage ------------------------------------------------------------
    storage_account_url: str  # https://<account>.blob.core.windows.net
    reference_container: str
    upload_container: str
    local_data_dir: str
    sqlserver_server: str  # e.g. localhost\SQLEXPRESS
    sqlserver_database: str
    sqlserver_trust_server_certificate: bool

    # --- Tyre physics defaults ---------------------------------------------
    legal_min_tread_mm: float
    recommended_min_tread_mm: float
    default_new_tread_mm: float
    default_km_per_mm: float

    # --- Limits -------------------------------------------------------------
    max_upload_bytes: int = 10 * 1024 * 1024
    max_reference_examples: int = 6
    session_ttl_seconds: int = 8 * 3600
    analyses_per_hour_per_user: int = 30

    dev_auth_bypass: bool = field(default=False)

    @property
    def is_production(self) -> bool:
        return self.app_env != "development"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/callback"


DATA_BACKENDS = {"azure_blob", "sqlserver", "local_files"}
AI_BACKENDS = {"foundry", "mock"}
DEV_ONLY_BACKENDS = {"sqlserver", "local_files", "mock"}


@lru_cache
def get_settings() -> Settings:
    app_env = _env("APP_ENV", "production")
    is_dev = app_env == "development"
    dev_bypass = _env("DEV_AUTH_BYPASS", "false").lower() == "true"

    storage_url = _env("STORAGE_ACCOUNT_URL", "")
    data_backend = _env("DATA_BACKEND", "azure_blob" if storage_url else "local_files").lower()
    ai_backend = _env("AI_BACKEND", "foundry").lower()
    if data_backend not in DATA_BACKENDS:
        raise RuntimeError(f"DATA_BACKEND must be one of {sorted(DATA_BACKENDS)}")
    if ai_backend not in AI_BACKENDS:
        raise RuntimeError(f"AI_BACKEND must be one of {sorted(AI_BACKENDS)}")

    if not is_dev:
        if dev_bypass:
            raise RuntimeError("DEV_AUTH_BYPASS is only allowed when APP_ENV=development")
        for name, value in (("DATA_BACKEND", data_backend), ("AI_BACKEND", ai_backend)):
            if value in DEV_ONLY_BACKENDS:
                raise RuntimeError(f"{name}={value} is only allowed when APP_ENV=development")

    entra_required = not dev_bypass
    return Settings(
        app_env=app_env,
        public_base_url=_env("PUBLIC_BASE_URL", "http://localhost:8000"),
        data_backend=data_backend,
        ai_backend=ai_backend,
        foundry_resource=_env("FOUNDRY_RESOURCE", required=ai_backend == "foundry"),
        claude_deployment=_env("CLAUDE_DEPLOYMENT", "claude-opus-5"),
        entra_authority=_env("ENTRA_AUTHORITY", required=entra_required),
        entra_client_id=_env("ENTRA_CLIENT_ID", required=entra_required),
        managed_identity_client_id=_env("MANAGED_IDENTITY_CLIENT_ID", required=entra_required),
        admin_app_role=_env("ADMIN_APP_ROLE", "Tyre.Admin"),
        storage_account_url=_env("STORAGE_ACCOUNT_URL", "", required=data_backend == "azure_blob"),
        reference_container=_env("REFERENCE_CONTAINER", "tyre-references"),
        upload_container=_env("UPLOAD_CONTAINER", "tyre-uploads"),
        local_data_dir=_env("LOCAL_DATA_DIR", "./data"),
        sqlserver_server=_env("SQLSERVER_SERVER", r"localhost\SQLEXPRESS"),
        sqlserver_database=_env("SQLSERVER_DATABASE", "TyreAdvisor"),
        sqlserver_trust_server_certificate=_env("SQLSERVER_TRUST_SERVER_CERTIFICATE", "true").lower() == "true",
        legal_min_tread_mm=float(_env("LEGAL_MIN_TREAD_MM", "1.6")),
        recommended_min_tread_mm=float(_env("RECOMMENDED_MIN_TREAD_MM", "3.0")),
        default_new_tread_mm=float(_env("DEFAULT_NEW_TREAD_MM", "8.0")),
        default_km_per_mm=float(_env("DEFAULT_KM_PER_MM", "8000")),
        dev_auth_bypass=dev_bypass,
    )
