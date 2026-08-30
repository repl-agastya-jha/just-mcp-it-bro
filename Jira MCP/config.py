from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_BASE_URL = "https://replicon.atlassian.net"
DEFAULT_PORT = 8767


@dataclass(frozen=True)
class Settings:
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    mcp_token: str
    port: int

    @property
    def credentials_present(self) -> bool:
        return bool(self.jira_email and self.jira_api_token)


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
        jira_base_url=os.environ.get("JIRA_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        jira_email=os.environ.get("JIRA_EMAIL", ""),
        jira_api_token=os.environ.get("JIRA_API_TOKEN", ""),
        mcp_token=os.environ.get("MCP_TOKEN", ""),
        port=int(os.environ.get("PORT", str(DEFAULT_PORT))),
    )
