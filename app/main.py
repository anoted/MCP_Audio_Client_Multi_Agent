"""FastAPI app: static frontend, MCP registration REST API, voice WebSocket."""
import json
import shlex
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .mcp_manager import MCPManager, MCPServerConfig
from .session import VoiceSession

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

mcp_manager = MCPManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await mcp_manager.startup()
    yield
    await mcp_manager.shutdown()


app = FastAPI(title="NIM Audio Client", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config() -> dict:
    return {
        "model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "voice": settings.tts_voice,
        "tts_sample_rate": settings.tts_sample_rate,
        "speech_enabled": settings.speech_configured,
    }


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
        return await mcp_manager.add(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/mcp/servers/{name}")
async def remove_server(name: str) -> dict:
    try:
        await mcp_manager.remove(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No server named '{name}'.") from exc
    return {"ok": True}


@app.post("/api/mcp/servers/{name}/reconnect")
async def reconnect_server(name: str) -> dict:
    try:
        return await mcp_manager.reconnect(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No server named '{name}'.") from exc


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
