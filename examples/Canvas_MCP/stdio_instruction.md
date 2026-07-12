# Running the Canvas MCP Server over stdio

`FastMCP` server can run in stdio form.

In `canvas_mcp_server.py`, change the entrypoint at the end of the file from:

```python
if __name__ == "__main__":
    if not (os.environ.get("CANVAS_BASE_URL") and os.environ.get("CANVAS_API_TOKEN")):
        print("WARNING: CANVAS_BASE_URL / CANVAS_API_TOKEN not set — tool calls will fail "
              "until you configure them in .env and restart.")
    print(f"Canvas MCP server (streamable HTTP, no auth) on http://{MCP_HOST}:{MCP_PORT}/mcp")
    mcp.run(transport="streamable-http")
```

to:

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Do not write regular log messages to stdout when using stdio. MCP clients use stdout
for protocol messages, so plain `print()` output can break the MCP handshake. If you
need startup warnings or logs, send them to stderr instead.

The existing `FastMCP(...)` settings near the top of the file, such as `host`, `port`,
and `streamable_http_path`, are harmless but only relevant when running with the HTTP
transport.

Example MCP client configuration:

```json
{
  "mcpServers": {
    "canvas": {
      "command": "conda",
      "args": ["run", "-n", "py_11", "python", "canvas_mcp_server.py"],
      "env": {
        "CANVAS_BASE_URL": "https://yourschool.instructure.com",
        "CANVAS_API_TOKEN": "your_canvas_token_here"
      }
    }
  }
}
```

If your client launches commands from a different working directory, use the absolute
path to `canvas_mcp_server.py` in the `args` list.
