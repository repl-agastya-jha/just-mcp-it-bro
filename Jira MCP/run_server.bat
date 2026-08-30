@echo off
cd /d "%~dp0"
echo Starting Jira MCP server at http://127.0.0.1:8767/mcp
echo Keep this window open - closing it stops the server.
.venv\Scripts\python.exe -m server
pause
