from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    ncbi_email: str | None = None
    ncbi_api_key: str | None = None
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.25
    ncbi_tool: str = "VetEvidenceAI"

    @property
    def user_agent(self) -> str:
        contact = self.ncbi_email or "contact-not-configured"
        return f"{self.ncbi_tool}/0.1 ({contact})"


def load_settings() -> Settings:
    """Load non-secret runtime configuration from environment variables."""
    return Settings(
        ncbi_email=os.getenv("NCBI_EMAIL") or None,
        ncbi_api_key=os.getenv("NCBI_API_KEY") or None,
    )
