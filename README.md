# Universal LLM API Proxy & Resilience Library
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/C0C0UZS4P)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Mirrowel/LLM-API-Key-Proxy)

**One proxy. Any LLM provider. Zero code changes.**

A self-hosted proxy providing universal OpenAI and Anthropic endpoints for all your providers. Features intelligent key rotation, failover, and support for specialized providers like Gemini CLI and Antigravity.

---

## 🚀 Quick Start (Docker)

```bash
# 1. Create config files
cp .env.example .env
touch key_usage.json

# 2. Run
docker run -d -p 8000:8000 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/oauth_creds:/app/oauth_creds \
  -v $(pwd)/key_usage.json:/app/key_usage.json \
  ghcr.io/mirrowel/llm-api-key-proxy:latest
```
*(For Windows/Linux/Source installation, see below)*

---

## 💾 Installation

### Pre-built Binaries (Recommended)

**Windows:**
1. Download the latest `proxy_app.exe` from [Releases](https://github.com/Mirrowel/LLM-API-Key-Proxy/releases).
2. Create a folder, place the `.exe` inside.
3. Double-click to run. It will guide you through the setup.

**Linux:**
1. Download `proxy_app` from [Releases](https://github.com/Mirrowel/LLM-API-Key-Proxy/releases).
2. Open a terminal in the download directory.
3. Make executable and run:
   ```bash
   chmod +x proxy_app
   ./proxy_app
   ```

### From Source

**Prerequisites:** Python 3.10+ and Git.

**Windows:**
```powershell
git clone https://github.com/Mirrowel/LLM-API-Key-Proxy.git
cd LLM-API-Key-Proxy
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python src/proxy_app/main.py
```

**Linux:**
```bash
git clone https://github.com/Mirrowel/LLM-API-Key-Proxy.git
cd LLM-API-Key-Proxy
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 src/proxy_app/main.py
```

---

## 🖥️ Interactive TUI (Recommended)
Run the application without any arguments to launch the **Terminal User Interface**.
*   **Easy Setup**: Configure Host, Port, and API Keys visually.
*   **Credential Manager**: Add OAuth (Gemini, Antigravity) and API keys interactively.
*   **Monitoring**: View active providers and quota usage stats.

```bash
# Windows
proxy_app.exe

# Linux / Source
./proxy_app  # or python src/proxy_app/main.py
```

---

## 📡 Connecting Clients

**Base URL**: `http://127.0.0.1:8000/v1`
**Auth**: Bearer Token (Your `PROXY_API_KEY`)
**Model Format**: `provider/model_name` (e.g., `gemini/gemini-2.5-flash`, `openai/gpt-4o`)

### Listing Available Models
Use this command to get a list of all configured models and their providers:
```bash
curl http://localhost:8000/v1/models?enriched=true \
  -H "Authorization: Bearer VerysecretKey"
```
*(Set `?enriched=false` for a faster, minimal list)*

### Example: Python (OpenAI SDK)
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="your-proxy-api-key")
client.chat.completions.create(
    model="gemini/gemini-2.5-flash",
    messages=[{"role": "user", "content": "Hi"}]
)
```

### Example: Claude Code
Edit `settings.json`:
```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "your-proxy-api-key",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "gemini/gemini-3-pro-preview"
  }
}
```

### Example: OpenCode
Edit `config.jsonc` (Requires [@ai-sdk/openai-compatible](https://www.npmjs.com/package/@ai-sdk/openai-compatible)):
```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "llm-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Proxy",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "VerysecretKey",
        "timeout": 300000
      }
    }
  },
  "models": {
    // Add models found via the curl command above
    "gemini-3-pro": {
      "provider": "llm-proxy",
      "modelId": "gemini_cli/gemini-3-pro-preview"
    }
  }
}
```
*[See full OpenCode documentation](https://opencode.ai/docs/models/)*

---

## ⚙️ Configuration
Managed via `.env` file.

| Variable | Description |
|----------|-------------|
| `PROXY_API_KEY` | **Required.** Auth key for the proxy itself. |
| `OPENAI_API_KEY_1` | Example provider key. Add more with `_2`, `_3`. |

---

## 🔧 Advanced Configuration

> **[📖 Click here for the Full Advanced Configuration Guide](docs/ADVANCED_CONFIGURATION.md)**
>
> Deep dive into **Timeouts**, **Model Filtering**, **Rotation Logic**, and **Provider-Specific Settings** (Gemini CLI, Antigravity, etc).

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check. |
| POST | `/v1/chat/completions` | OpenAI-compatible chat. |
| POST | `/v1/messages` | Anthropic-compatible chat. |
| POST | `/v1/embeddings` | Text embeddings (supports batching). |
| GET | `/v1/models` | List models. Add `?enriched=false` for speed. |
| GET | `/v1/quota-stats` | View current quota usage. |

---

## 🛠️ Troubleshooting

| Issue | Solution |
|-------|----------|
| `401 Unauthorized` | Check `PROXY_API_KEY` matches header. |
| `500 Internal Server Error` | Check provider keys. Enable `--enable-request-logging`. |
| All keys on cooldown | Upstream rate limits hit. Check `logs/detailed_logs/`. |
| Model not found | Use `provider/model` format (e.g. `gemini/gemini-1.5-pro`). |

**Logs**: Enable with `--enable-request-logging`. Check `logs/detailed_logs/<req_id>/`.

---

## 📚 Documentation
*   **[Deployment Guide](docs/DEPLOYMENT.md)**: Hosting on Render, Railway, VPS.
*   **[Architecture](docs/ARCHITECTURE.md)**: Internal design and logic.
*   **[Resilience Library](docs/RESILIENCE_LIB.md)**: Using the Python library directly.
