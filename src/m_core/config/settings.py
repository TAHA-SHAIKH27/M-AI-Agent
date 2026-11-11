"""Application settings using Pydantic Settings."""
import os
import pathlib
from typing import Optional, List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="M_",
    )
    
    # Application
    debug: bool = True
    testing: bool = False
    app_name: str = "M"
    app_version: str = "0.1.0-dev"
    data_dir: pathlib.Path = Field(
        default_factory=lambda: pathlib.Path("./data").resolve()
    )
    
    # NVIDIA Nemotron 3 Ultra (Primary)
    nvidia_api_key: Optional[str] = None
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    
    # Fallback Providers
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    
    lm_studio_base_url: str = "http://localhost:1234/v1"
    
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    
    # Vision Providers (for screenshot-based visual intelligence)
    # NVIDIA NIM vision models (NVLM, Nemotron-3V, etc.)
    nvidia_vision_model: str = "nvidia/nv-embed-v1"  # Will use actual vision model when available
    nvidia_vision_base_url: str = "https://integrate.api.nvidia.com/v1"
    
    # Google Gemini API (fallback vision)
    gemini_api_key: Optional[str] = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_vision_model: str = "gemini-1.5-flash"
    
    # Memory
    memory_stm_ttl_seconds: int = 3600
    memory_episodic_db: str = "sqlite:///data/memory/episodic.db"
    memory_semantic_path: str = "data/memory/semantic"
    memory_procedural_path: str = "data/memory/procedural"
    
    # Voice
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    kokoro_voice: str = "af_sky"
    kokoro_speed: float = 1.0
    wake_word_enabled: bool = False
    wake_word_access_key: Optional[str] = None
    
    # Phone / Twilio
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_phone_number: Optional[str] = None
    twilio_webhook_url: Optional[str] = None
    
    # Android Companion
    android_companion_enabled: bool = False
    android_companion_port: int = 8765
    
    # Windows Automation
    uia_timeout_ms: int = 5000
    uia_retry_count: int = 3
    
    # Browser
    playwright_headless: bool = True
    playwright_browser: str = "chromium"
    
    # Security
    encryption_key: Optional[str] = None
    audit_log_enabled: bool = True
    consent_required: bool = True
    
    # API Server
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    ws_port: int = 8081
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 100
    rate_limit_window: int = 60
    
    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, v: str | pathlib.Path) -> pathlib.Path:
        if isinstance(v, str):
            v = pathlib.Path(v)
        return v.expanduser().resolve()
    
    @field_validator("whisper_device")
    @classmethod
    def _validate_whisper_device(cls, v: str) -> str:
        if v not in ("cpu", "cuda", "mps"):
            raise ValueError("whisper_device must be 'cpu', 'cuda', or 'mps'")
        return v
    
    @field_validator("whisper_compute_type")
    @classmethod
    def _validate_compute_type(cls, v: str) -> str:
        valid = ("float16", "float32", "int8", "int8_float16")
        if v not in valid:
            raise ValueError(f"whisper_compute_type must be one of {valid}")
        return v
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ensure_directories()
    
    def _ensure_directories(self) -> None:
        """Create necessary data directories."""
        dirs = [
            self.data_dir,
            self.data_dir / "memory" / "stm",
            self.data_dir / "memory" / "semantic",
            self.data_dir / "memory" / "procedural",
            self.data_dir / "models",
            self.data_dir / "voices",
            self.data_dir / "logs",
            self.data_dir / "temp",
        ]
        try:
            for d in dirs:
                d.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            # Fall back to local ./data in current directory
            local_data = pathlib.Path("./data").resolve()
            self.data_dir = local_data
            for d in [
                local_data,
                local_data / "memory" / "stm",
                local_data / "memory" / "semantic",
                local_data / "memory" / "procedural",
                local_data / "models",
                local_data / "voices",
                local_data / "logs",
                local_data / "temp",
            ]:
                try:
                    d.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
    
    def get_episodic_db_path(self) -> pathlib.Path:
        """Get the SQLite database file path for episodic memory."""
        # Extract path from sqlite:/// URI
        uri = self.memory_episodic_db
        if uri.startswith("sqlite:///"):
            path_str = uri[10:]  # Remove 'sqlite:///'
            return pathlib.Path(path_str).expanduser().resolve()
        return self.data_dir / "memory" / "episodic.db"
    
    def get_semantic_path(self) -> pathlib.Path:
        """Get the ChromaDB path for semantic memory."""
        return pathlib.Path(self.memory_semantic_path).expanduser().resolve()
    
    def get_procedural_path(self) -> pathlib.Path:
        """Get the procedural memory path."""
        return pathlib.Path(self.memory_procedural_path).expanduser().resolve()


# Global settings instance
settings = Settings()