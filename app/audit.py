"""Structured audit log: one JSONL file per session + live feed to the UI.

Every consequential event in a session is recorded: user input, agent
switches, tool calls (with the server they routed to), tool results (length +
flags, not full payloads), sub-agent runs, plan/review/verify outcomes,
approval checkpoints, injection flags, MCP-app actions, interruptions and
errors. Values that reach the log have already been through the privacy
filter, so the files are safe to share when privacy mode is on.

The logger also keeps an in-memory tail and pushes each entry to an optional
listener (the WebSocket session) so the UI Activity panel updates live.
"""
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from .config import settings

_TAIL = 400  # in-memory entries kept per session


class AuditLog:
    def __init__(self, listener: Callable[[dict], None] | None = None):
        self.session_id = uuid.uuid4().hex[:8]
        self.listener = listener
        self.tail: deque[dict] = deque(maxlen=_TAIL)
        self._path: Path | None = None
        if settings.audit_enabled:
            log_dir = Path(settings.logs_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._path = log_dir / f"session-{stamp}-{self.session_id}.jsonl"

    def event(self, kind: str, **fields) -> dict:
        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "t": round(time.time(), 3),
            "kind": kind,
            **fields,
        }
        self.tail.append(entry)
        if self._path is not None:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass  # never let logging break the pipeline
        if self.listener:
            try:
                self.listener(entry)
            except Exception:  # noqa: BLE001
                pass
        return entry


def list_log_files() -> list[dict]:
    log_dir = Path(settings.logs_dir)
    if not log_dir.exists():
        return []
    out = []
    for path in sorted(log_dir.glob("session-*.jsonl"), reverse=True)[:50]:
        stat = path.stat()
        out.append({
            "name": path.name,
            "size": stat.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return out


def read_log_tail(name: str, limit: int = 300) -> list[dict]:
    # File names come from list_log_files; still normalize to prevent traversal.
    safe = Path(name).name
    path = Path(settings.logs_dir) / safe
    if not path.exists() or path.suffix != ".jsonl":
        raise FileNotFoundError(name)
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
