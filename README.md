# M AI Assistant - Core Backend

The core backend system for M, a powerful personal desktop AI assistant.

## Features

- **Multi-Provider Model Abstraction**: NVIDIA Nemotron 3 Ultra (primary), Ollama, LM Studio, OpenAI, Anthropic
- **Automatic Fallback Chain**: Seamless failover between providers
- **Secure Credential Storage**: Windows Credential Manager (DPAPI) + Fernet encryption
- **Granular Consent System**: Per-capability permissions with TTL and scopes
- **Structured Logging**: JSON/console output with structlog
- **Async Architecture**: Built on asyncio for high performance
- **Comprehensive Testing**: Unit and integration tests

## Quick Start

### Prerequisites

- Python 3.13+
- Windows 11 (primary), Linux, macOS
- NVIDIA GPU with CUDA (optional, for local models)

### Installation

```bash
# Clone and enter directory
cd M

# Install uv (fast package manager)
pip install uv

# Install dependencies
uv sync --all-extras

# Or install core only
uv sync
```

### Configuration

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your API keys
# Required: M_NVIDIA_API_KEY (get from https://build.nvidia.com)
```

### Usage

```bash
# Run CLI
python -m m_core --help

# Check system health
python -m m_core health

# List providers
python -m m_core providers --list --health

# Test NVIDIA Nemotron 3 Ultra
python -m m_core test-model --provider nvidia-nemotron-3-ultra

# Interactive chat
python -m m_core chat

# Manage API keys
python -m m_core vault --list
python -m m_core vault --set --provider nvidia-nemotron-3-ultra

# Manage consents
python -m m_core consent --list
python -m m_core consent --grant --capability model:chat
```

## Architecture

```
m_core/
├── config/          # Pydantic Settings configuration
├── core/            # Core types, events, registry, lifecycle
├── model_providers/ # Provider abstraction + implementations
├── security/        # Vault, consent, encryption, audit
├── api/             # REST, WebSocket, CLI interfaces
└── utils/           # Logging, async, retry, platform utilities
```

## Model Providers

| Provider | ID | Type | Status |
|----------|-----|------|--------|
| NVIDIA Nemotron 3 Ultra | `nvidia-nemotron-3-ultra` | Cloud | Primary |
| Ollama | `ollama` | Local | Fallback |
| LM Studio | `lm-studio` | Local | Fallback |
| OpenAI | `openai` | Cloud | Fallback |
| Anthropic | `anthropic` | Cloud | Fallback |

### NVIDIA Nemotron 3 Ultra

- **Model**: `nvidia/nemotron-3-ultra-550b-a55b`
- **API**: OpenAI-compatible at `https://integrate.api.nvidia.com/v1`
- **Context**: 1M tokens
- **Capabilities**: Chat, streaming, tool calling, reasoning
- **Pricing**: ~$0.50/$2.20 per 1M tokens (input/output)

Get API key: https://build.nvidia.com

## Security

### API Key Vault

Keys are encrypted with Fernet (AES-128) and stored in Windows Credential Manager (DPAPI). The master key is also stored in Credential Manager.

```bash
# Store key
python -m m_core vault --set --provider nvidia-nemotron-3-ultra

# List keys
python -m m_core vault --list

# Retrieve (masked)
python -m m_core vault --get --provider nvidia-nemotron-3-ultra
```

### Consent System

Granular permissions for each capability:

```bash
# List all capabilities
python -m m_core consent --list

# Grant consent for file reading
python -m m_core consent --grant --capability file:read --scope global

# Grant temporary consent (1 hour)
python -m m_core consent --grant --capability phone:outbound --ttl 1
```

Capabilities:
- `model:chat`, `model:embed`, `model:tools`
- `file:read`, `file:write`, `file:delete`, `file:search`
- `app:launch`, `app:control`, `system:admin`
- `web:search`, `web:browse`, `web:automate`
- `voice:stt`, `voice:tts`, `voice:wake-word`
- `phone:outbound`, `phone:inbound`, `phone:contacts`
- `screen:capture`, `screen:analyze`
- `automation:input`, `automation:window`
- `memory:read`, `memory:write`

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=m_core

# Run specific test file
pytest tests/test_model_providers.py -v
```

### Code Quality

```bash
# Format
ruff format .

# Lint
ruff check .

# Type check
mypy src/
```

## Project Structure

```
M/
├── src/m_core/           # Main package
├── tests/                # Test suite
├── scripts/              # Utility scripts
├── data/                 # Runtime data (gitignored)
│   ├── memory/           # Memory tiers
│   ├── models/           # Local model cache
│   ├── voices/           # TTS voices
│   └── logs/             # Log files
├── docs/                 # Documentation
├── pyproject.toml        # Project config
├── .env.example          # Environment template
└── README.md             # This file
```

## License

MIT License - see LICENSE file for details.