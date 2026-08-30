"""Read-only xMatters MCP server.

Exposes a small set of read-only tools over the xMatters REST API
(/api/xm/1) via FastMCP's streamable-HTTP transport, protected by an
optional bearer token (same pattern as the CloudOps KB / Jira MCP servers).
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

mcp = FastMCP("xmatters")

NOT_CONFIGURED_ERROR = {
    "error": "XMATTERS_BASE_URL / XMATTERS_USERNAME / XMATTERS_PASSWORD not configured — see README"
}

_API_PREFIX = "/api/xm/1"
_HTTP_TIMEOUT = 30.0

_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


# --------------------------------------------------------------------------
# xMatters REST helpers
# --------------------------------------------------------------------------


def _xmatters_request(
    method: str, path: str, *, params: dict | None = None
) -> tuple[dict | None, dict | None]:
    """Call the xMatters REST API. Returns (data, error) — exactly one is None."""
    settings = get_settings()
    if not settings.credentials_present:
        return None, NOT_CONFIGURED_ERROR
    url = f"{settings.xmatters_base_url}{_API_PREFIX}{path}"
    auth = (settings.xmatters_username, settings.xmatters_password)
    try:
        with httpx.Client(auth=auth, timeout=_HTTP_TIMEOUT) as client:
            response = client.request(method, url, params=params)
    except httpx.HTTPError as exc:
        return None, {"error": f"request to xMatters failed: {exc}"}
    if response.status_code >= 400:
        return None, {"error": f"{response.status_code} {response.reason_phrase}"}
    try:
        return response.json(), None
    except ValueError:
        return None, {"error": f"non-JSON response from xMatters ({response.status_code})"}


def _recipient_name(recipient: object) -> str | None:
    """Pull a human-readable name out of an xMatters recipient object."""
    if not isinstance(recipient, dict):
        return None
    return (
        recipient.get("targetName")
        or recipient.get("firstName")
        or recipient.get("name")
        or recipient.get("id")
    )


def _shape_shift(shift: dict) -> list[dict]:
    """Flatten one on-call shift into per-member rows.

    An on-call entry describes a shift within a group and a list of members
    currently covering it. Returns one row per member so the caller sees a
    flat list of who is on call, in which role, and for what window.
    """
    group = shift.get("group") or {}
    group_name = group.get("targetName") or group.get("name") or _recipient_name(group)
    shift_ref = shift.get("shift") or {}
    start = shift_ref.get("start") or shift.get("start")
    end = shift_ref.get("end") or shift.get("end")
    members_container = shift.get("members") or {}
    if isinstance(members_container, dict):
        member_entries = members_container.get("data") or []
    elif isinstance(members_container, list):
        member_entries = members_container
    else:
        member_entries = []

    rows: list[dict] = []
    for entry in member_entries:
        if not isinstance(entry, dict):
            continue
        member = entry.get("member") or entry.get("recipient") or {}
        role = entry.get("position") or entry.get("role")
        rows.append(
            {
                "group": group_name,
                "member": _recipient_name(member),
                "role": role,
                "start": start,
                "end": end,
            }
        )
    if not rows:
        # A shift with no covering members is still worth surfacing.
        rows.append(
            {
                "group": group_name,
                "member": None,
                "role": None,
                "start": start,
                "end": end,
            }
        )
    return rows


def _shape_group(group: dict) -> dict:
    return {
        "targetName": group.get("targetName"),
        "id": group.get("id"),
        "description": group.get("description"),
        "status": group.get("status"),
    }


def _shape_event(event: dict) -> dict:
    priority = event.get("priority")
    if isinstance(priority, dict):
        priority = priority.get("name") or priority.get("value")
    return {
        "id": event.get("id"),
        "eventId": event.get("eventId"),
        "created": event.get("created"),
        "terminated": event.get("terminated"),
        "status": event.get("status"),
        "priority": priority,
        "summary": event.get("summary") or event.get("name"),
    }


def _clamp(value: object) -> int:
    return max(1, min(int(value), 100))


# --------------------------------------------------------------------------
# MCP tools (all read-only)
# --------------------------------------------------------------------------


@mcp.tool
def who_is_on_call(group: str) -> list[dict] | dict:
    """Who is currently on call for an xMatters group.

    Looks up the live on-call shifts for the given group (by target name) and
    returns one row per covering member: {group, member, role, start, end}.
    Use this to answer "who is on call for <team> right now".
    """
    data, error = _xmatters_request("GET", "/on-call", params={"groups": group})
    if error:
        return error
    shifts = data.get("data") or []
    rows: list[dict] = []
    for shift in shifts:
        if isinstance(shift, dict):
            rows.extend(_shape_shift(shift))
    return rows


@mcp.tool
def list_groups(search: str = "", max_results: int = 50) -> list[dict] | dict:
    """List xMatters on-call groups, optionally filtered by a search term.

    Returns compact group summaries {targetName, id, description, status}.
    Pass `search` to match on group name; returns at most max_results groups.
    """
    params: dict = {"limit": _clamp(max_results)}
    if search:
        params["search"] = search
    data, error = _xmatters_request("GET", "/groups", params=params)
    if error:
        return error
    groups = data.get("data") or []
    return [_shape_group(group) for group in groups if isinstance(group, dict)]


@mcp.tool
def get_events(
    status: str = "", from_: str = "", to: str = "", max_results: int = 25
) -> list[dict] | dict:
    """Recent xMatters notification events, newest first.

    Optional filters: `status` (e.g. 'ACTIVE', 'TERMINATED', 'SUSPENDED'),
    and an ISO-8601 time window via `from_` and `to`. Returns compact event
    summaries {id, eventId, created, terminated, status, priority, summary},
    at most max_results of them.
    """
    params: dict = {
        "sortBy": "START_TIME",
        "sortOrder": "DESCENDING",
        "limit": _clamp(max_results),
    }
    if status:
        params["status"] = status
    if from_:
        params["from"] = from_
    if to:
        params["to"] = to
    data, error = _xmatters_request("GET", "/events", params=params)
    if error:
        return error
    events = data.get("data") or []
    return [_shape_event(event) for event in events if isinstance(event, dict)]


@mcp.tool
def get_event(event_id: str) -> dict:
    """Fetch one xMatters event's detail by its id.

    Returns eventId, status, priority, created, terminated, summary,
    submitter and a count of targeted recipients.
    """
    data, error = _xmatters_request("GET", f"/events/{event_id}")
    if error:
        return error
    priority = data.get("priority")
    if isinstance(priority, dict):
        priority = priority.get("name") or priority.get("value")
    submitter = data.get("submitter") or {}
    recipients = data.get("recipients") or {}
    if isinstance(recipients, dict):
        recipients_count = recipients.get("count")
        if recipients_count is None:
            recipients_count = len(recipients.get("data") or [])
    elif isinstance(recipients, list):
        recipients_count = len(recipients)
    else:
        recipients_count = 0
    return {
        "eventId": data.get("eventId"),
        "status": data.get("status"),
        "priority": priority,
        "created": data.get("created"),
        "terminated": data.get("terminated"),
        "summary": data.get("summary") or data.get("name"),
        "submitter": _recipient_name(submitter),
        "recipients_count": recipients_count,
    }


@mcp.tool
def person_on_call_history(person: str, max_results: int = 25) -> list[dict] | dict:
    """Events an xMatters person was targeted in, newest first (best-effort).

    Resolves `person` to a user via people search, then lists events that
    targeted that user's recipient name. Returns compact event summaries
    (same shape as get_events). If the person cannot be resolved, returns an
    informative error dict rather than crashing.
    """
    people_data, error = _xmatters_request(
        "GET", "/people", params={"search": person, "limit": 10}
    )
    if error:
        return error
    matches = people_data.get("data") or []
    if not matches:
        return {"error": f"no xMatters person matched '{person}'"}
    target_name = None
    for match in matches:
        if isinstance(match, dict):
            target_name = match.get("targetName") or _recipient_name(match)
            if target_name:
                break
    if not target_name:
        return {"error": f"could not resolve a target name for '{person}'"}
    events_data, error = _xmatters_request(
        "GET",
        "/events",
        params={
            "targetedRecipients": target_name,
            "sortBy": "START_TIME",
            "sortOrder": "DESCENDING",
            "limit": _clamp(max_results),
        },
    )
    if error:
        return error
    events = events_data.get("data") or []
    return [_shape_event(event) for event in events if isinstance(event, dict)]


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
