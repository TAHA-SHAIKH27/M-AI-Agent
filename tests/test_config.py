"""Tests for configuration module."""
import pytest
import tempfile
from pathlib import Path

from m_core.config import Settings


class TestSettings:
    """Test settings configuration."""
    
    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            yield tmp
    
    def test_default_values(self):
        settings = Settings(debug=True)
        assert settings.app_name == "M"
        assert settings.debug is True
        assert settings.nvidia_base_url == "https://integrate.api.nvidia.com/v1"
        assert settings.nvidia_model == "nvidia/nemotron-3-ultra-550b-a55b"
        assert settings.ollama_base_url == "http://localhost:11434"
        assert settings.ollama_model == "llama3.2:3b"
    
    def test_data_dir_creation(self, temp_dir):
        data_dir = Path(temp_dir) / "test_data"
        settings = Settings(debug=True, data_dir=data_dir)
        
        assert settings.data_dir.exists()
        assert (settings.data_dir / "memory" / "stm").exists()
        assert (settings.data_dir / "memory" / "semantic").exists()
        assert (settings.data_dir / "memory" / "procedural").exists()
        assert (settings.data_dir / "models").exists()
        assert (settings.data_dir / "voices").exists()
        assert (settings.data_dir / "logs").exists()
        assert (settings.data_dir / "temp").exists()
    
    def test_whisper_device_validation(self):
        settings = Settings(debug=True, whisper_device="cuda")
        assert settings.whisper_device == "cuda"
        
        settings = Settings(debug=True, whisper_device="cpu")
        assert settings.whisper_device == "cpu"
        
        with pytest.raises(ValueError):
            Settings(debug=True, whisper_device="invalid")
    
    def test_compute_type_validation(self):
        settings = Settings(debug=True, whisper_compute_type="float16")
        assert settings.whisper_compute_type == "float16"
        
        with pytest.raises(ValueError):
            Settings(debug=True, whisper_compute_type="invalid")
    
    def test_get_episodic_db_path(self):
        settings = Settings(debug=True, memory_episodic_db="sqlite:///custom/path.db")
        path = settings.get_episodic_db_path()
        assert path.name == "path.db"
    
    def test_get_semantic_path(self, temp_dir):
        path = Path(temp_dir) / "semantic"
        settings = Settings(debug=True, memory_semantic_path=str(path))
        result = settings.get_semantic_path()
        assert result == path.expanduser().resolve()
    
    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("M_DEBUG", "false")
        monkeypatch.setenv("M_APP_NAME", "TestApp")
        monkeypatch.setenv("M_NVIDIA_MODEL", "custom-model")
        
        settings = Settings()
        assert settings.debug is False
        assert settings.app_name == "TestApp"
        assert settings.nvidia_model == "custom-model"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])