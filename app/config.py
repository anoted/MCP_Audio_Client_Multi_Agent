"""Central configuration: environment / .env plus runtime overrides.

Secrets and endpoints stay in `.env`. A small set of user-facing options
(approval mode, privacy, model, voice) can be changed from the Settings
overlay at runtime; those are persisted to `app_settings.json` so they
survive restarts without touching the env file.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Your replies are spoken aloud with "
    "text-to-speech, so answer conversationally and keep responses concise "
    "unless the user asks for detail. Avoid markdown formatting, tables, and "
    "code blocks unless explicitly requested. When tools are available, use "
    "them when they help answer the question, then summarize the result in "
    "plain speech."
)


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # --- Text LLM (OpenAI-compatible) ---
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    system_prompt: str = os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
    max_tool_rounds: int = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
    tool_timeout_s: float = float(os.getenv("TOOL_TIMEOUT_S", "60"))

    # --- NVIDIA NIM speech (Riva gRPC) ---
    nvidia_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    riva_use_ssl: bool = _bool("RIVA_USE_SSL", True)

    asr_server: str = os.getenv("RIVA_ASR_SERVER", "grpc.nvcf.nvidia.com:443")
    asr_function_id: str = os.getenv(
        "RIVA_ASR_FUNCTION_ID", "1598d209-5e27-4d3c-8079-4751568b1081"
    )
    asr_language: str = os.getenv("ASR_LANGUAGE", "en-US")
    asr_sample_rate: int = 16000  # what the browser worklet sends

    tts_server: str = os.getenv("RIVA_TTS_SERVER", "grpc.nvcf.nvidia.com:443")
    tts_function_id: str = os.getenv(
        "RIVA_TTS_FUNCTION_ID", "877104f7-e885-42b9-8de8-f6e4c6303969"
    )
    tts_voice: str = os.getenv("TTS_VOICE", "Magpie-Multilingual.EN-US.Aria")
    tts_language: str = os.getenv("TTS_LANGUAGE", "en-US")
    tts_sample_rate: int = int(os.getenv("TTS_SAMPLE_RATE", "22050"))

    mcp_registry_path: str = os.getenv("MCP_REGISTRY_PATH", "mcp_servers.json")
    conversations_dir: str = os.getenv("CONVERSATIONS_DIR", "conversations")

    # --- Workflow safety & governance ---
    # Approval checkpoints for modifying tools: all | high | off
    approval_mode: str = os.getenv("APPROVAL_MODE", "high")
    approval_timeout_s: float = float(os.getenv("APPROVAL_TIMEOUT_S", "600"))
    privacy_enabled: bool = _bool("PRIVACY_ENABLED", True)
    injection_guard_enabled: bool = _bool("INJECTION_GUARD", True)
    audit_enabled: bool = _bool("AUDIT_ENABLED", True)
    logs_dir: str = os.getenv("LOGS_DIR", "logs")
    skills_dir: str = os.getenv("SKILLS_DIR", "skills")

    # Keys the Settings overlay may change at runtime (persisted).
    MUTABLE = (
        "llm_model", "tts_voice", "approval_mode",
        "privacy_enabled", "injection_guard_enabled", "audit_enabled",
    )
    overrides_path = Path(os.getenv("APP_SETTINGS_PATH", "app_settings.json"))

    @property
    def speech_configured(self) -> bool:
        return bool(self.nvidia_api_key) or "nvcf.nvidia.com" not in self.asr_server

    def load_overrides(self) -> None:
        try:
            data = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key in self.MUTABLE:
            if key in data:
                setattr(self, key, data[key])

    def update(self, changes: dict) -> dict:
        """Apply + persist mutable settings; returns what was applied."""
        applied = {}
        for key, val in changes.items():
            if key not in self.MUTABLE or val is None:
                continue
            if key == "approval_mode":
                if val not in ("all", "high", "off"):
                    continue
            elif isinstance(getattr(self, key), bool):
                val = bool(val)
            else:
                val = str(val).strip()
                if not val:
                    continue
            setattr(self, key, val)
            applied[key] = val
        if applied:
            current = {k: getattr(self, k) for k in self.MUTABLE}
            try:
                self.overrides_path.write_text(
                    json.dumps(current, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        return applied

    def public(self) -> dict:
        return {
            "model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "voice": self.tts_voice,
            "tts_sample_rate": self.tts_sample_rate,
            "speech_enabled": self.speech_configured,
            "approval_mode": self.approval_mode,
            "privacy_enabled": self.privacy_enabled,
            "injection_guard_enabled": self.injection_guard_enabled,
            "audit_enabled": self.audit_enabled,
        }


settings = Settings()
settings.load_overrides()
