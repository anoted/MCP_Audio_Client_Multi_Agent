"""Save/load conversations as one JSON file each in a folder."""
import json
import re
import time
from pathlib import Path

from .config import settings

_SAFE = re.compile(r"[^\w\- ]+")


def _dir() -> Path:
    path = Path(settings.conversations_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    return _SAFE.sub("", name or "").strip()[:60]


def save(name: str, data: dict) -> str:
    final = _safe_name(name) or time.strftime("conversation %Y-%m-%d %H%M%S")
    data = dict(data)
    data["name"] = final
    data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (_dir() / f"{final}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return final


def load(name: str) -> dict:
    safe = _safe_name(name)
    path = _dir() / f"{safe}.json"
    if not safe or not path.exists():
        raise FileNotFoundError(name)
    return json.loads(path.read_text(encoding="utf-8"))


def delete(name: str) -> None:
    safe = _safe_name(name)
    path = _dir() / f"{safe}.json"
    if safe and path.exists():
        path.unlink()


def list_all() -> list[dict]:
    items = []
    for path in sorted(_dir().glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        preview = ""
        for entry in data.get("transcript", []):
            if entry.get("kind") == "user":
                preview = entry.get("text", "")[:80]
                break
        items.append(
            {
                "name": data.get("name") or path.stem,
                "saved_at": data.get("saved_at", ""),
                "preview": preview,
            }
        )
    return items
