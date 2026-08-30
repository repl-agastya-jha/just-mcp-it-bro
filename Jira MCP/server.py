"""Read-only Jira Cloud MCP server.

Exposes a small set of read-only tools over Jira Cloud REST API v3 via
FastMCP's streamable-HTTP transport, protected by an optional bearer token
(same pattern as the CloudOps KB MCP server).
"""

from __future__ import annotations

import hmac

import httpx
import uvicorn
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import Settings, load_settings

mcp = FastMCP("jira")

NOT_CONFIGURED_ERROR = {
    "error": "JIRA_EMAIL / JIRA_API_TOKEN not configured — see README"
}

_ISSUE_FIELDS = [
    "summary",
    "status",
    "priority",
    "assignee",
    "reporter",
    "created",
    "resolutiondate",
    "description",
    "labels",
    "issuelinks",
]

_SEARCH_DESCRIPTION_LIMIT = 400
_HTTP_TIMEOUT = 30.0

_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


# --------------------------------------------------------------------------
# ADF (Atlassian Document Format) -> plain text
# --------------------------------------------------------------------------

_BLOCK_NODE_TYPES = {
    "paragraph",
    "heading",
    "blockquote",
    "codeBlock",
    "listItem",
    "tableRow",
    "rule",
}


def adf_to_text(node: object) -> str:
    """Recursively flatten an Atlassian Document Format tree to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(child) for child in node)
    if not isinstance(node, dict):
        return str(node)
    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type in ("mention", "status", "date"):
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("timestamp") or "")
    if node_type == "emoji":
        return (node.get("attrs") or {}).get("shortName", "")
    if node_type in ("inlineCard", "blockCard", "embedCard"):
        return str((node.get("attrs") or {}).get("url", ""))
    inner = adf_to_text(node.get("content"))
    if node_type in _BLOCK_NODE_TYPES:
        return inner + "\n"
    return inner


# --------------------------------------------------------------------------
# Jira REST helpers
# --------------------------------------------------------------------------


def _jira_request(
    method: str, path: str, *, params: dict | None = None, json_body: dict | None = None
) -> tuple[dict | None, dict | None]:
    """Call the Jira REST API. Returns (data, error) — exactly one is None."""
    settings = get_settings()
    if not settings.credentials_present:
        return None, NOT_CONFIGURED_ERROR
    url = f"{settings.jira_base_url}{path}"
    auth = (settings.jira_email, settings.jira_api_token)
    try:
        with httpx.Client(auth=auth, timeout=_HTTP_TIMEOUT) as client:
            response = client.request(method, url, params=params, json=json_body)
    except httpx.HTTPError as exc:
        return None, {"error": f"request to Jira failed: {exc}"}
    if response.status_code >= 400:
        return None, {"error": f"{response.status_code} {response.reason_phrase}"}
    try:
        return response.json(), None
    except ValueError:
        return None, {"error": f"non-JSON response from Jira ({response.status_code})"}


def _shape_link(link: dict) -> dict | None:
    link_type = link.get("type") or {}
    if "outwardIssue" in link:
        other, relation = link["outwardIssue"], link_type.get("outward", "relates to")
    elif "inwardIssue" in link:
        other, relation = link["inwardIssue"], link_type.get("inward", "relates to")
    else:
        return None
    other_fields = other.get("fields") or {}
    return {
        "relation": relation,
        "key": other.get("key"),
        "summary": other_fields.get("summary"),
        "status": (other_fields.get("status") or {}).get("name"),
    }


def _shape_issue(issue: dict, *, compact: bool = False) -> dict:
    fields = issue.get("fields") or {}
    description = adf_to_text(fields.get("description")).strip()
    if compact and len(description) > _SEARCH_DESCRIPTION_LIMIT:
        description = description[:_SEARCH_DESCRIPTION_LIMIT] + "..."
    links = [
        shaped
        for shaped in (_shape_link(link) for link in fields.get("issuelinks") or [])
        if shaped is not None
    ]
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "created": fields.get("created"),
        "resolved": fields.get("resolutiondate"),
        "description": description,
        "labels": fields.get("labels") or [],
        "links": links,
    }


# --------------------------------------------------------------------------
# MCP tools (all read-only)
# --------------------------------------------------------------------------


@mcp.tool
def get_issue(key: str) -> dict:
    """Fetch one Jira issue by key (e.g. 'IMR-1234').

    Returns key, summary, status, priority, assignee, reporter, created,
    resolved, description (rich text flattened to plain text), labels and
    linked issues (relation + key + summary + status).
    """
    data, error = _jira_request("GET", f"/rest/api/3/issue/{key}", params={
        "fields": ",".join(_ISSUE_FIELDS)
    })
    if error:
        return error
    return _shape_issue(data)


@mcp.tool
def search_issues(jql: str, max_results: int = 20) -> list[dict] | dict:
    """Search Jira with a JQL query and return compact issue summaries.

    Example JQL: "project = IMR AND status != Done ORDER BY created DESC".
    Each result has the same shape as get_issue, with the description
    truncated for compactness. Returns at most max_results issues.
    """
    data, error = _jira_request(
        "POST",
        "/rest/api/3/search/jql",
        json_body={
            "jql": jql,
            "maxResults": max(1, min(int(max_results), 100)),
            "fields": _ISSUE_FIELDS,
        },
    )
    if error:
        return error
    issues = data.get("issues") or []
    return [_shape_issue(issue, compact=True) for issue in issues]


@mcp.tool
def get_comments(key: str, max_comments: int = 20) -> list[dict] | dict:
    """Fetch the newest comments on a Jira issue (newest first).

    Returns a list of {author, created, body} where body is the comment's
    rich text flattened to plain text.
    """
    data, error = _jira_request(
        "GET",
        f"/rest/api/3/issue/{key}/comment",
        params={
            "maxResults": max(1, min(int(max_comments), 100)),
            "orderBy": "-created",
        },
    )
    if error:
        return error
    return [
        {
            "author": (comment.get("author") or {}).get("displayName"),
            "created": comment.get("created"),
            "body": adf_to_text(comment.get("body")).strip(),
        }
        for comment in data.get("comments") or []
    ]


@mcp.tool
def recent_incidents(
    project_keys: str = "IMR,CII,SM", days: int = 30, max_results: int = 20
) -> list[dict] | dict:
    """Recent issues across the incident projects, newest first.

    Convenience wrapper: builds a JQL query over the given comma-separated
    project keys (default 'IMR,CII,SM') for issues created in the last
    `days` days, ordered newest first, and returns compact issue summaries.
    """
    keys = [part.strip() for part in project_keys.split(",") if part.strip()]
    if not keys:
        return {"error": "project_keys must contain at least one project key"}
    jql = (
        f"project in ({', '.join(keys)}) "
        f"AND created >= -{max(1, int(days))}d ORDER BY created DESC"
    )
    return search_issues(jql, max_results=max_results)


# --------------------------------------------------------------------------
# HTTP app with bearer auth (same pattern as the CloudOps KB MCP server)
# --------------------------------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if self.token:
            expected = f"Bearer {self.token}"
            provided = request.headers.get("authorization") or ""
            if not hmac.compare_digest(provided, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main() -> None:
    global _settings
    from config import enable_system_certs

    enable_system_certs()
    _settings = load_settings()
    app = mcp.http_app()
    app.add_middleware(BearerAuthMiddleware, token=_settings.mcp_token)
    uvicorn.run(app, host="127.0.0.1", port=_settings.port)


if __name__ == "__main__":
    main()
