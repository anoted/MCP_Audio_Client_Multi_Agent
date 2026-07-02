"""Central configuration loaded from environment / .env file."""
import os

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

    @property
    def speech_configured(self) -> bool:
        return bool(self.nvidia_api_key) or "nvcf.nvidia.com" not in self.asr_server


settings = Settings()
