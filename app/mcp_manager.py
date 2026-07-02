"""MCP client layer: server registration, persistent connections, tool routing.

Each registered MCP server gets a dedicated asyncio task that owns the whole
client context (transport + session). This matters because anyio cancel scopes
must be entered and exited in the same task; funnelling the lifecycle through
one task avoids cross-task teardown errors. `call_tool` on the live session is
safe to await from other tasks.
"""
import asyncio
import contextlib
import json
import re
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import settings

MAX_RESULT_CHARS = 8000
CONNECT_TIMEOUT_S = 30

_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    return _SAFE.sub("_", name)


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"  # stdio | http | sse
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", self.name):
            raise ValueError(
                "Server name must be 1-32 chars of letters, digits, '_' or '-'."
            )
        if self.transport not in ("stdio", "http", "sse"):
            raise ValueError("Transport must be one of: stdio, http, sse.")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires a command.")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(f"{self.transport} transport requires a URL.")


class MCPConnection:
    """One live connection, owned by a single background task."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.session: ClientSession | None = None
        self.tools: list[types.Tool] = []
        self.error: str | None = None
        self.connected = False
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.config.name}")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), CONNECT_TIMEOUT_S)
        if not self._ready.is_set():
            self.error = f"Connection timed out after {CONNECT_TIMEOUT_S}s"
            await self.close()

    async def _run(self) -> None:
        cfg = self.config
        try:
            async with AsyncExitStack() as stack:
                if cfg.transport == "stdio":
                    env = get_default_environment()
                    env.update(cfg.env)
                    params = StdioServerParameters(
                        command=cfg.command, args=cfg.args, env=env
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif cfg.transport == "sse":
                    read, write = await stack.enter_async_context(
                        sse_client(cfg.url, headers=cfg.headers or None)
                    )
                else:  # streamable http
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(cfg.url, headers=cfg.headers or None)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self.session = session
                self.tools = listed.tools
                self.connected = True
                self.error = None
                self._ready.set()
                await self._stop.wait()
        except Exception as exc:  # noqa: BLE001 — surface anything to the UI
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.connected = False
            self.session = None
            self._ready.set()

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 10)
            if not self._task.done():
                self._task.cancel()


class MCPManager:
    """Registry of MCP servers + aggregated tool surface for the LLM."""

    def __init__(self, registry_path: str | None = None):
        self.registry_path = Path(registry_path or settings.mcp_registry_path)
        self.configs: dict[str, MCPServerConfig] = {}
        self.connections: dict[str, MCPConnection] = {}
        self._route: dict[str, tuple[str, str]] = {}  # api name -> (server, tool)

    # -- lifecycle ----------------------------------------------------------

    async def startup(self) -> None:
        self._load()
        await asyncio.gather(*(self._connect(n) for n in self.configs))

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(c.close() for c in self.connections.values()), return_exceptions=True
        )
        self.connections.clear()

    async def _connect(self, name: str) -> None:
        old = self.connections.pop(name, None)
        if old:
            await old.close()
        conn = MCPConnection(self.configs[name])
        self.connections[name] = conn
        await conn.start()

    # -- registration API ---------------------------------------------------

    async def add(self, config: MCPServerConfig) -> dict:
        config.validate()
        if config.name in self.configs:
            raise ValueError(f"A server named '{config.name}' is already registered.")
        self.configs[config.name] = config
        self._persist()
        await self._connect(config.name)
        return self.describe_one(config.name)

    async def remove(self, name: str) -> None:
        if name not in self.configs:
            raise KeyError(name)
        conn = self.connections.pop(name, None)
        if conn:
            await conn.close()
        del self.configs[name]
        self._persist()

    async def reconnect(self, name: str) -> dict:
        if name not in self.configs:
            raise KeyError(name)
        await self._connect(name)
        return self.describe_one(name)

    def describe(self) -> list[dict]:
        return [self.describe_one(name) for name in sorted(self.configs)]

    def describe_one(self, name: str) -> dict:
        cfg = self.configs[name]
        conn = self.connections.get(name)
        return {
            "name": cfg.name,
            "transport": cfg.transport,
            "command": (" ".join([cfg.command, *cfg.args])).strip(),
            "url": cfg.url,
            "connected": bool(conn and conn.connected),
            "error": conn.error if conn else None,
            "tools": [
                {"name": t.name, "description": (t.description or "")[:200]}
                for t in (conn.tools if conn else [])
            ],
        }

    # -- tool surface for the LLM --------------------------------------------

    def openai_tools(self) -> list[dict]:
        """OpenAI function-calling specs for every connected server's tools."""
        self._route.clear()
        specs = []
        for name, conn in self.connections.items():
            if not conn.connected:
                continue
            for tool in conn.tools:
                api_name = f"{_sanitize(name)}__{_sanitize(tool.name)}"[:64]
                base, n = api_name, 2
                while api_name in self._route:
                    api_name = f"{base[:60]}_{n}"
                    n += 1
                self._route[api_name] = (name, tool.name)
                specs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": api_name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema
                            or {"type": "object", "properties": {}},
                        },
                    }
                )
        return specs

    def resolve(self, api_name: str) -> tuple[str, str] | None:
        return self._route.get(api_name)

    async def call(self, api_name: str, arguments: dict[str, Any]) -> dict:
        """Execute a tool call; returns {"ok": bool, "server", "tool", "result"}."""
        target = self.resolve(api_name)
        if target is None:
            return {
                "ok": False,
                "server": "?",
                "tool": api_name,
                "result": f"Unknown tool '{api_name}'.",
            }
        server, tool = target
        conn = self.connections.get(server)
        if conn is None or not conn.connected or conn.session is None:
            return {
                "ok": False,
                "server": server,
                "tool": tool,
                "result": f"MCP server '{server}' is not connected.",
            }
        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool, arguments=arguments),
                settings.tool_timeout_s,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "server": server,
                "tool": tool,
                "result": f"Tool call timed out after {settings.tool_timeout_s}s.",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "server": server,
                "tool": tool,
                "result": f"{type(exc).__name__}: {exc}",
            }

        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            elif isinstance(block, types.ImageContent):
                parts.append(f"[image: {block.mimeType}]")
            else:
                parts.append(f"[{type(block).__name__}]")
        if not parts and result.structuredContent:
            parts.append(json.dumps(result.structuredContent))
        text = "\n".join(parts).strip() or "(empty result)"
        return {
            "ok": not result.isError,
            "server": server,
            "tool": tool,
            "result": text[:MAX_RESULT_CHARS],
        }

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        if not self.registry_path.exists():
            return
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            for entry in raw.get("servers", []):
                cfg = MCPServerConfig(**entry)
                cfg.validate()
                self.configs[cfg.name] = cfg
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"[mcp] ignoring invalid {self.registry_path}: {exc}")

    def _persist(self) -> None:
        data = {"servers": [asdict(c) for c in self.configs.values()]}
        self.registry_path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
