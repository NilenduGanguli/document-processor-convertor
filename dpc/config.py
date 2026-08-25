"""Settings. ``DPC_`` prefix, one flat object, refuse-don't-guess on nonsense."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="dpc_", env_file=".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8300
    #: Optional X-API-Key; empty disables the gate (fine behind a mesh, not on a desk).
    api_key: str = ""

    # ---- Postgres -----------------------------------------------------------
    pg_dsn: str = "postgresql://dpc:dpc@localhost:5438/dpc"

    # ---- S3 / MinIO ---------------------------------------------------------
    s3_endpoint: str = "http://localhost:9004"
    s3_access_key: str = "dpc"
    s3_secret_key: str = "dpc-secret"
    s3_bucket: str = "docmd"
    s3_region: str = "us-east-1"

    # ---- Reading a raw document --------------------------------------------
    #: Max upload size.
    max_bytes: int = 32 * 1024 * 1024
    #: Pages read from one PDF.
    max_pages: int = 500
    #: Below this many alphanumeric characters a page's text layer is treated as absent.
    #: Same floor, same reasoning as DCE's per-page rule.
    min_alnum_chars: int = 40
    #: Azure Document Intelligence endpoint used ONLY when a raw document needs optical
    #: recognition (an image, or a PDF page with no text layer). Empty = scans come back
    #: as a structured refusal rather than a guess. In-network per the deployment.
    azure_di_endpoint: str = ""
    azure_di_key: str = ""
    azure_di_api_version: str = "2024-11-30"
    ocr_timeout_seconds: float = 60.0
    ocr_poll_interval_seconds: float = 0.5
    ocr_max_polls: int = 120

    #: DEBUG-by-default tracing, same posture and same reasoning as DCE: a trace nobody can
    #: find is not a trace, and no log line here carries document text.
    log_level: str = "DEBUG"


@lru_cache
def get_settings() -> Settings:
    return Settings()


__all__ = ["Settings", "get_settings"]
