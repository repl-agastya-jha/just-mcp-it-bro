# Jira MCP Server (read-only)

A read-only [MCP](https://modelcontextprotocol.io) server for Jira Cloud, built with
FastMCP v2. It lets Claude Code / Claude Desktop look up issues, run JQL searches,
read comments, and pull recent incidents from the `IMR` / `CII` / `SM` projects —
without any write access to Jira.

It follows the same engine pattern as the CloudOps KB MCP server (streamable HTTP
transport + optional bearer-token middleware + truststore for corporate Zscaler TLS)
and joins the KB MCP (port 8765) and Salesforce MCP on the same host in production,
listening on **port 8767**.

## Getting a Jira API token

1. Go to <https://id.atlassian.com> → **Security** → **API tokens**.
2. **Create API token**, give it a label (e.g. `jira-mcp`), copy the value.
3. Put your Atlassian account email in `JIRA_EMAIL` and the token in `JIRA_API_TOKEN`.

> For a team rollout, create the token under a dedicated **service account** rather
> than a personal account, so access survives people leaving and can be scoped/audited.

## Setup

```bat
cd C:\Users\agastya.jha\Desktop\jira-mcp
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
:: edit .env — fill in JIRA_EMAIL and JIRA_API_TOKEN (and MCP_TOKEN if exposing beyond localhost)
run_server.bat
```

The server listens at `http://127.0.0.1:8767/mcp`.

Verify with the smoke test (works even before credentials are configured — it
asserts the graceful not-configured error, which proves transport + auth):

```bat
.venv\Scripts\python scripts\smoke_test.py
```

Register with Claude Code (use forward slashes in paths):

```bash
claude mcp add --transport http jira http://127.0.0.1:8767/mcp
```

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `JIRA_BASE_URL` | `https://replicon.atlassian.net` | Jira Cloud site |
| `JIRA_EMAIL` | (empty) | Atlassian account email |
| `JIRA_API_TOKEN` | (empty) | API token from id.atlassian.com |
| `MCP_TOKEN` | (empty) | Bearer token required by the MCP server itself; empty = auth disabled |
| `PORT` | `8767` | Listen port |

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `get_issue` | `key` | One issue: key, summary, status, priority, assignee, reporter, created, resolved, description (plain text), labels, links |
| `search_issues` | `jql`, `max_results=20` | Compact list of issues (same shape, truncated description) for any JQL query |
| `get_comments` | `key`, `max_comments=20` | Newest-first comments: `{author, created, body}` (plain text) |
| `recent_incidents` | `project_keys='IMR,CII,SM'`, `days=30`, `max_results=20` | Recent issues in the incident projects, newest first |

Notes:

- All tools are **read-only** — the server never POSTs changes to Jira (the only
  POST used is Jira's JQL search endpoint).
- Atlassian rich text (ADF) in descriptions/comments is flattened to plain text.
- With missing credentials every tool returns
  `{"error": "JIRA_EMAIL / JIRA_API_TOKEN not configured — see README"}` instead
  of crashing; HTTP failures come back as `{"error": "<status> <reason>"}`.
- Corporate TLS interception (Zscaler) is handled via `truststore`, which trusts
  the OS certificate store.
