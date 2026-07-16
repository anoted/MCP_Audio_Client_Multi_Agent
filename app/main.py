"""FastAPI app: static frontend, MCP + agent REST API, voice WebSocket."""
import asyncio
import json
import shlex
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit, conversations
from .agents import describe_agents, initiator
from .config import settings
from .mcp_manager import MCPManager, MCPServerConfig
from .session import VoiceSession
from .skills import registry as skill_registry

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

mcp_manager = MCPManager()
_initiator_task: asyncio.Task | None = None


def _rerun_initiator() -> None:
    """Kick the one-shot initiator whenever the tool inventory changes."""
    global _initiator_task
    _initiator_task = asyncio.create_task(initiator.run(mcp_manager))


@asynccontextmanager
async def lifespan(_: FastAPI):
    skill_registry.load()
    await mcp_manager.startup()
    _rerun_initiator()  # classify tools in the background at start
    yield
    await mcp_manager.shutdown()


app = FastAPI(title="Voice Client", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config() -> dict:
    return settings.public()


class SetModelRequest(BaseModel):
    model: str


@app.post("/api/model")
async def set_model(req: SetModelRequest) -> dict:
    model = req.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model name must not be empty.")
    settings.update({"llm_model": model})
    return {"model": model}


class UpdateSettingsRequest(BaseModel):
    llm_model: str | None = None
    tts_voice: str | None = None
    approval_mode: str | None = None
    privacy_enabled: bool | None = None
    injection_guard_enabled: bool | None = None
    audit_enabled: bool | None = None


@app.put("/api/settings")
async def update_settings(req: UpdateSettingsRequest) -> dict:
    applied = settings.update(req.model_dump(exclude_none=True))
    return {"applied": applied, "config": settings.public()}


@app.get("/api/agents")
async def get_agents() -> dict:
    return {"agents": describe_agents(), "initiator": initiator.describe()}


@app.get("/api/skills")
async def get_skills() -> dict:
    return {"skills": skill_registry.describe()}


@app.get("/api/apps")
async def get_apps() -> dict:
    """Openable MCP apps: argument-free `open_*` tools across servers."""
    apps = []
    for spec in mcp_manager.openai_tools():
        target = mcp_manager.resolve(spec["function"]["name"])
        if not target:
            continue
        server, tool = target
        if tool.startswith("open_"):
            apps.append({
                "server": server,
                "tool": tool,
                "description": (spec["function"].get("description") or "")[:200],
            })
    return {"apps": apps, "resources": mcp_manager.ui_resources()}


@app.get("/api/prompts")
async def get_prompts() -> dict:
    """Reusable prompt templates published by connected MCP servers."""
    return {"prompts": mcp_manager.prompts()}


class RenderPromptRequest(BaseModel):
    server: str
    name: str
    args: dict[str, str] = Field(default_factory=dict)


@app.post("/api/prompts/render")
async def render_prompt(req: RenderPromptRequest) -> dict:
    """Fill a server prompt's arguments and return the rendered text. The
    client sends that text to the active agent as ordinary user input, so
    triage, approval gates, and the audit log all apply unchanged."""
    text = await mcp_manager.get_prompt(req.server, req.name, req.args)
    if text is None:
        raise HTTPException(
            status_code=502,
            detail=f"Could not render prompt '{req.name}' from server "
                   f"'{req.server}' (check required arguments and connection).",
        )
    return {"text": text}


@app.get("/api/logs")
async def get_logs() -> list[dict]:
    return audit.list_log_files()


@app.get("/api/logs/{name}")
async def get_log(name: str) -> list[dict]:
    try:
        return audit.read_log_tail(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No log '{name}'.") from exc


# --- conversations --------------------------------------------------------------


@app.get("/api/conversations")
async def list_conversations() -> list[dict]:
    return conversations.list_all()


@app.delete("/api/conversations/{name}")
async def delete_conversation(name: str) -> dict:
    conversations.delete(name)
    return {"ok": True}


# --- MCP registration API -----------------------------------------------------


class RegisterServerRequest(BaseModel):
    name: str
    transport: str = "stdio"
    command: str = ""  # full command line for stdio, e.g. "python examples/demo_mcp_server.py"
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


def _split_command(command_line: str) -> tuple[str, list[str]]:
    # posix=False keeps Windows backslash paths intact; then strip quotes.
    parts = [p.strip('"') for p in shlex.split(command_line, posix=False)]
    if not parts:
        return "", []
    return parts[0], parts[1:]


@app.get("/api/mcp/servers")
async def list_servers() -> list[dict]:
    return mcp_manager.describe()


@app.post("/api/mcp/servers")
async def register_server(req: RegisterServerRequest) -> dict:
    command, args = _split_command(req.command)
    config = MCPServerConfig(
        name=req.name.strip(),
        transport=req.transport,
        command=command,
        args=args,
        env=req.env,
        url=req.url.strip(),
        headers=req.headers,
    )
    try:
        result = await mcp_manager.add(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _rerun_initiator()
    return result


@app.delete("/api/mcp/servers/{name}")
async def remove_server(name: str) -> dict:
    try:
        await mcp_manager.remove(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No server named '{name}'.") from exc
    _rerun_initiator()
    return {"ok": True}


@app.post("/api/mcp/servers/{name}/reconnect")
async def reconnect_server(name: str) -> dict:
    try:
        result = await mcp_manager.reconnect(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No server named '{name}'.") from exc
    _rerun_initiator()
    return result


# --- Voice WebSocket -----------------------------------------------------------


@app.websocket("/ws")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()
    session = VoiceSession(ws, mcp_manager)
    await session.start()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await session.on_audio(msg["bytes"])
            elif msg.get("text"):
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                await session.on_message(payload)
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
