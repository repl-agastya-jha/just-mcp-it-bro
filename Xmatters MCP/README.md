# xMatters MCP Server (read-only)

A read-only [MCP](https://modelcontextprotocol.io) server for xMatters, built with
FastMCP v2. It lets Claude Code / Claude Desktop see who is on call, list on-call
groups, and read recent notification events — without any write access to xMatters.

It follows the same engine pattern as the CloudOps KB MCP server (streamable HTTP
transport + optional bearer-token middleware + truststore for corporate Zscaler TLS)
and joins the CloudOps KB MCP (port 8765), Salesforce MCP (port 8766), and Jira MCP
(port 8767) side-by-side on the same host in production, listening on **port 8768**.

## Getting xMatters API credentials

The server authenticates to the xMatters REST API (`/api/xm/1`) using HTTP Basic
auth — a username and a password (or a REST/API token used as the password).

1. In xMatters, create (or reuse) a **dedicated REST / service account** rather than
   a personal login, so access survives people leaving and can be scoped and audited.
2. Give that account a **read-only role** — it only needs to read on-call schedules,
   groups, people, and events. This server never writes to xMatters.
3. Put the account's login in `XMATTERS_USERNAME` and its password/token in
   `XMATTERS_PASSWORD`, and set `XMATTERS_BASE_URL` to your company's xMatters host
   (e.g. `https://replicon.xmatters.com`).

## Setup

```bat
cd C:\Users\agastya.jha\Desktop\xmatters-mcp
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
:: edit .env — fill in XMATTERS_BASE_URL, XMATTERS_USERNAME, XMATTERS_PASSWORD
::             (and MCP_TOKEN if exposing beyond localhost)
run_server.bat
```

The server listens at `http://127.0.0.1:8768/mcp`.

Verify with the smoke test (works even before credentials are configured — it
asserts the graceful not-configured error, which proves transport + auth):

```bat
.venv\Scripts\python scripts\smoke_test.py
```

Register with Claude Code (use forward slashes in paths):

```bash
claude mcp add --transport http xmatters http://127.0.0.1:8768/mcp
```

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `XMATTERS_BASE_URL` | (empty) | xMatters host, e.g. `https://replicon.xmatters.com` |
| `XMATTERS_USERNAME` | (empty) | REST/service account login |
| `XMATTERS_PASSWORD` | (empty) | Password or API token for that account |
| `MCP_TOKEN` | (empty) | Bearer token required by the MCP server itself; empty = auth disabled |
| `PORT` | `8768` | Listen port |

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `who_is_on_call` | `group` | Current on-call members for a group: `{group, member, role, start, end}` per member |
| `list_groups` | `search=''`, `max_results=50` | On-call groups: `{targetName, id, description, status}` |
| `get_events` | `status=''`, `from_=''`, `to=''`, `max_results=25` | Recent notification events, newest first: `{id, eventId, created, terminated, status, priority, summary}` |
| `get_event` | `event_id` | One event's detail: `{eventId, status, priority, created, terminated, summary, submitter, recipients_count}` |
| `person_on_call_history` | `person`, `max_results=25` | Events a person was targeted in, newest first (resolves the person via people search) |

Notes:

- All tools are **read-only** — the server only issues GET requests to xMatters and
  never POSTs, PUTs, or DELETEs.
- With missing credentials every tool returns
  `{"error": "XMATTERS_BASE_URL / XMATTERS_USERNAME / XMATTERS_PASSWORD not configured — see README"}`
  instead of crashing; HTTP failures come back as `{"error": "<status> <reason>"}`.
- Corporate TLS interception (Zscaler) is handled via `truststore`, which trusts
  the OS certificate store.
