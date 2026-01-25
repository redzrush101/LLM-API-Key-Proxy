# Advanced Configuration

Configuration is managed via `.env` file or environment variables. This guide covers all advanced settings available in the proxy.

## 📋 Core Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `PROXY_API_KEY` | **Required.** Auth key for the proxy itself (protects the proxy). | - |
| `HOST` | Bind address for the server. | `0.0.0.0` |
| `PORT` | Listen port for the server. | `8000` |
| `GLOBAL_TIMEOUT` | Global request timeout in seconds. | `30` |
| `SKIP_OAUTH_INIT_CHECK` | Skip OAuth initialization check on startup (faster boot). | `false` |
| `OVERRIDE_TEMPERATURE_ZERO` | Handling for `temperature=0` (which can cause issues with some models). Values: `remove` (delete key), `set` (set to 1.0), `false` (do nothing). | `false` |
| `USE_EMBEDDING_BATCHER` | Enable server-side batching for high-throughput embedding requests. | `false` |

## 🔑 Provider Credentials

The proxy supports both API Key and OAuth credentials.

### API Keys
Format: `PROVIDER_API_KEY` (single) or `PROVIDER_API_KEY_N` (multiple).

```env
# Single key
OPENAI_API_KEY="sk-..."

# Multiple keys (round-robin/rotation)
ANTHROPIC_API_KEY_1="sk-ant-1..."
ANTHROPIC_API_KEY_2="sk-ant-2..."
```

### OAuth Credentials (File-based)
Managed via the built-in credential tool. Credentials are stored in standard locations (e.g., `~/.gemini`, `~/.antigravity`).

### OAuth Credentials (Environment-based)
For stateless deployments (like Docker/Render), you can pass OAuth tokens via environment variables.

```env
# Single credential
ANTIGRAVITY_ACCESS_TOKEN="ya29..."
ANTIGRAVITY_REFRESH_TOKEN="1//..."

# Multiple credentials
ANTIGRAVITY_1_ACCESS_TOKEN="..."
ANTIGRAVITY_1_REFRESH_TOKEN="..."
ANTIGRAVITY_2_ACCESS_TOKEN="..."
ANTIGRAVITY_2_REFRESH_TOKEN="..."
```

The proxy creates virtual paths (e.g., `env://antigravity/1`) for these credentials.

### OAuth Callback Ports
Override the local port used during OAuth authentication flows.

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_CLI_OAUTH_PORT` | Port for Gemini CLI OAuth callback. | `8085` |
| `ANTIGRAVITY_OAUTH_PORT` | Port for Antigravity OAuth callback. | `51121` |
| `IFLOW_OAUTH_PORT` | Port for iFlow OAuth callback. | `11451` |

## 🎯 Model Filtering

Control exposed models via allow/deny lists.
*   **Order**: Whitelist → Blacklist → Default Allow.
*   **Wildcards**: Supported (`*`).

```env
# Example: Block all previews, except specific one
IGNORE_MODELS_OPENAI="*-preview*"
WHITELIST_MODELS_OPENAI="gpt-4o-2024-08-06-preview"
```

### Static Model Definitions
Define models strictly via environment variables (overrides dynamic discovery).

| Variable | Description |
|----------|-------------|
| `QWEN_CODE_MODELS` | Comma-separated list of Qwen Code models. |
| `IFLOW_MODELS` | Comma-separated list of iFlow models. |
| `GEMINI_CLI_MODELS` | Comma-separated list of Gemini CLI models (via model definitions). |

## 🔄 Rotation Logic

The proxy's core strength is its intelligent rotation engine.

### Rotation Mode
Control how credentials are selected.

| Variable | Description | Use Case |
|----------|-------------|----------|
| `balanced` | Distributes requests across all keys based on load. | **Best for Rate Limits** (RPM/TPM). Spreads traffic to avoid hitting short-term limits. |
| `sequential` | Uses the first key until it hits a limit, then moves to the next. | **Best for Quotas**. Uses up one account's daily/monthly budget before touching the next. |

**Configuration:**
```env
# Default is balanced
ROTATION_MODE_OPENAI=balanced
ROTATION_MODE_GEMINI=sequential
```

| Variable | Description | Default |
|----------|-------------|---------|
| `ROTATION_TOLERANCE` | Selection randomness. `0.0` (Strictly least used) to `5.0` (High randomness). | `3.0` |

### Fair Cycle Rotation
Ensures each credential exhausts its quota at least once before reuse (useful for tiered quotas).

| Variable | Description | Default |
|----------|-------------|---------|
| `FAIR_CYCLE_<PROVIDER>` | Enable fair cycle rotation (`true`/`false`). | `null` (auto) |
| `FAIR_CYCLE_TRACKING_MODE_<PROVIDER>` | `model_group` (track per model/group) or `credential` (global per key). | `model_group` |
| `FAIR_CYCLE_DURATION_<PROVIDER>` | Cycle duration in seconds before auto-reset. | `604800` (7 days) |
| `FAIR_CYCLE_CROSS_TIER_<PROVIDER>` | If `true`, all credentials must exhaust regardless of priority tier. | `false` |

## ⏱️ Timeouts & Retries

Fine-tune network behavior.

### Timeouts (Seconds)

| Variable | Description | Default |
|----------|-------------|---------|
| `TIMEOUT_CONNECT` | TCP connect timeout. | `30` |
| `TIMEOUT_WRITE` | Request body send timeout. | `30` |
| `TIMEOUT_POOL` | Connection pool acquisition timeout. | `60` |
| `TIMEOUT_READ_STREAMING` | Max time between chunks for streaming requests. | `300` (5 min) |
| `TIMEOUT_READ_NON_STREAMING` | Max total time for non-streaming responses. | `600` (10 min) |

### Cooldown & Backoff (Internal Defaults)
*These are currently internal defaults but good to know for behavior analysis.*
*   **1st Failure**: 10s cooldown
*   **2nd Failure**: 30s cooldown
*   **3rd Failure**: 60s cooldown
*   **Auth Error (401/403)**: 5 minutes cooldown (assumed key revocation)

## 📊 Quota Management

Protect your usage and manage costs.

| Variable | Description | Default |
|----------|-------------|---------|
| `MAX_CONCURRENT_REQUESTS_PER_KEY_<PROVIDER>` | Max simultaneous requests allowed per key. | `1` (Gemini), `∞` (others) |
| `EXHAUSTION_COOLDOWN_THRESHOLD` | Global threshold (seconds). If a key's cooldown exceeds this, it's considered "exhausted". | `300` |
| `EXHAUSTION_COOLDOWN_THRESHOLD_<PROVIDER>` | Provider-specific exhaustion threshold. | - |

## 📝 Logging

Debug and monitor proxy activity.

| Variable | Description | Default |
|----------|-------------|---------|
| `ENABLE_REQUEST_LOGGING` | Enable transaction logging (library-level, correlates requests/responses). | `false` (via flag) |
| `ENABLE_RAW_LOGGING` | Enable raw I/O logging at the proxy boundary (unmodified HTTP data). | `false` (via flag) |
| `LITELLM_LOG` | LiteLLM internal log level. | `ERROR` |

## 🔌 Provider-Specific Configuration

### Antigravity (Gemini 3 + Claude)

**Core Settings**
| Variable | Description | Default |
|----------|-------------|---------|
| `ANTIGRAVITY_INTERLEAVED_THINKING` | Parse "thinking" tags from models that interleave them with text. | `True` |
| `ANTIGRAVITY_SIGNATURE_CACHE_TTL` | Duration (seconds) to cache thought signatures for multi-turn chats. | `3600` |
| `ANTIGRAVITY_GEMINI3_TOOL_FIX` | Applies schema corrections for Gemini 3's tool calling quirks. | `True` |

**Internal Tuning (Modify with caution)**
| Variable | Description | Default |
|----------|-------------|---------|
| `ANTIGRAVITY_EMPTY_RESPONSE_ATTEMPTS` | Max retries for empty responses. | `6` |
| `ANTIGRAVITY_EMPTY_RESPONSE_RETRY_DELAY` | Delay between empty response retries (seconds). | `3` |
| `ANTIGRAVITY_MALFORMED_CALL_RETRIES` | Max retries for malformed tool calls. | `2` |
| `ANTIGRAVITY_PREPEND_INSTRUCTION` | Prepend system instruction to requests. | `True` |
| `ANTIGRAVITY_INJECT_IDENTITY_OVERRIDE` | Inject identity override to prevent self-identification leaks. | `True` |
| `ANTIGRAVITY_GEMINI3_STRICT_SCHEMA` | Enforce strict schema validation for Gemini 3 tools. | `True` |
| `ANTIGRAVITY_CLAMP_THINKING_TO_OUTPUT` | Force thinking content to be part of the output. | `False` |

### Gemini CLI

**Core Settings**
| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_CLI_PROJECT_ID` | **Required (Auto-detected).** Google Cloud Project ID. | - |
| `GEMINI_CLI_QUOTA_REFRESH_INTERVAL` | How often to refresh free/paid tier quota status (seconds). | `300` |
| `QUOTA_GROUPS_GEMINI_CLI_<GROUP>` | Override default model quota groupings (e.g., `pro`, `flash`). | - |

**Internal Tuning**
| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_CLI_SIGNATURE_CACHE_TTL` | Memory TTL for thought signature cache (seconds). | `3600` |
| `GEMINI_CLI_PRESERVE_THOUGHT_SIGNATURES` | Keep thought signatures in client memory. | `True` |
| `GEMINI_CLI_GEMINI3_TOOL_FIX` | Enable specific fixes for Gemini 3 tool calling. | `True` |
| `GEMINI_CLI_GEMINI3_STRICT_SCHEMA` | Enforce strict schema validation for Gemini 3 tools. | `True` |
