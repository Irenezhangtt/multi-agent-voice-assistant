from dataclasses import dataclass
from pathlib import Path
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    mode: str = "mock"
    architecture: str = "multi-agent"
    agent_max_rounds: int = 5
    model: str = "claude-sonnet-4-6"
    timezone: str = "America/Los_Angeles"
    data_dir: Path = Path("data")
    version: str = "0.1.0"
    prompt_version: str = "parallel-prompts-v6"
    workflow_version: str = "parallel-streaming-v6"
    tool_version: str = "fenced-mcp-v3"
    insurance_kb_path: Path = Path(__file__).parent / "knowledge" / "insurance-demo.json"
    speech_language: str = "en"
    speech_voice: str = "aura-2-thalia-en"
    stt_model: str = "nova-3"
    speech_endpointing_ms: int = 650

    def __post_init__(self):
        if self.mode not in {"mock", "claude"}:
            raise ValueError("AGENT_MODE must be mock or claude")
        if self.architecture not in {"multi-agent", "workflow"}:
            raise ValueError("AGENT_ARCHITECTURE must be multi-agent or workflow")
        if not 1 <= self.agent_max_rounds <= 8:
            raise ValueError("AGENT_MAX_ROUNDS must be 1..8")
        ZoneInfo(self.timezone)
        if self.mode == "claude" and not os.getenv("ANTHROPIC_API_KEY", "").strip():
            raise ValueError("Claude mode requires ANTHROPIC_API_KEY in your local .env")

    @classmethod
    def from_env(cls):
        load_dotenv()
        return cls(
            mode=os.getenv("AGENT_MODE", "mock"),
            architecture=os.getenv("AGENT_ARCHITECTURE", "multi-agent"),
            agent_max_rounds=int(os.getenv("AGENT_MAX_ROUNDS", "5")),
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            timezone=os.getenv("CLINIC_TIMEZONE", "America/Los_Angeles"),
            data_dir=Path(os.getenv("AGENT_DATA_DIR", "./data")),
            insurance_kb_path=Path(os.getenv("INSURANCE_KB_PATH", str(Path(__file__).parent / "knowledge" / "insurance-demo.json"))),
            speech_language=os.getenv("SPEECH_LANGUAGE", "en"),
            speech_voice=os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en"),
            stt_model=os.getenv("DEEPGRAM_STT_MODEL", "nova-3"),
            speech_endpointing_ms=int(os.getenv("SPEECH_ENDPOINTING_MS", "650")),
        )

    def speech_status(self):
        return {
            "provider": "deepgram", "configured": bool(os.getenv("DEEPGRAM_API_KEY", "").strip()),
            "language": self.speech_language, "voice": self.speech_voice,
            "stt_model": self.stt_model, "endpointing_ms": self.speech_endpointing_ms,
            "input_format": "pcm_s16le", "sample_rate": 16000, "channels": 1,
        }

    def versions(self):
        return {
            "agent": self.version,
            "architecture": self.architecture if self.mode == "claude" else "workflow-baseline",
            "prompt": self.prompt_version,
            "workflow": self.workflow_version,
            "tools": self.tool_version,
            "model": self.model if self.mode == "claude" else "deterministic-mock-v1",
            "mode": self.mode,
        }
