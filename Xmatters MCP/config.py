from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_BASE_URL = ""
DEFAULT_PORT = 8768


@dataclass(frozen=True)
class Settings:
    xmatters_base_url: str
    xmatters_username: str
    xmatters_password: str
    mcp_token: str
    port: int

    @property
    def credentials_present(self) -> bool:
        return bool(
            self.xmatters_base_url
            and self.xmatters_username
            and self.xmatters_password
        )


def enable_system_certs() -> None:
    # Corporate TLS interception (Zscaler) replaces upstream certs; trust the
    # OS certificate store instead of certifi so outbound HTTPS still verifies.
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass


def load_settings() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env")
    return Settings(
        xmatters_base_url=os.environ.get(
            "XMATTERS_BASE_URL", DEFAULT_BASE_URL
        ).rstrip("/"),
        xmatters_username=os.environ.get("XMATTERS_USERNAME", ""),
        xmatters_password=os.environ.get("XMATTERS_PASSWORD", ""),
        mcp_token=os.environ.get("MCP_TOKEN", ""),
        port=int(os.environ.get("PORT", str(DEFAULT_PORT))),
    )
