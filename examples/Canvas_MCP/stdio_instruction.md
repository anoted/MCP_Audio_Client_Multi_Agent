# Running the Canvas MCP Server over stdio

The server's default transport is **streamable HTTP with bearer-token auth**
(see `README.md`). For clients that spawn the server themselves, stdio is still
available — no code changes needed:

```
python canvas_mcp_server.py --stdio
```

or set `MCP_TRANSPORT=stdio` in the environment / `.env`.

Over stdio there is **no HTTP endpoint and no bearer token**: the transport is a
private pipe between the client and the process it spawned, which is why the MCP
spec does not apply HTTP authorization to stdio servers. Credentials
(`CANVAS_BASE_URL`, `CANVAS_API_TOKEN`) still come from the environment or
`.env`.

Do not write regular log messages to stdout when using stdio. MCP clients use
stdout for protocol messages, so plain `print()` output can break the MCP
handshake. If you need startup warnings or logs, send them to stderr instead —
everything in `canvas_mcp_server.py` already does.

Example MCP client configuration (process-spawning client):

```json
{
  "mcpServers": {
    "canvas": {
      "command": "conda",
      "args": [
        "run", "--no-capture-output", "-n", "mcpagents",
        "python", "canvas_mcp_server.py", "--stdio"
      ],
      "env": {
        "CANVAS_BASE_URL": "https://yourschool.instructure.com",
        "CANVAS_API_TOKEN": "your_canvas_token_here"
      }
    }
  }
}
```

If your client launches commands from a different working directory, use the
absolute path to `canvas_mcp_server.py` in the `args` list.
