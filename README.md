# Margaret Gateway

> **One gateway. Every AI assistant.**

Control Codex, OpenCode, Claude Code, and Copilot through a **single REST+SSE API** —
from your phone, Slack, browser, or terminal.

![Architecture](docs/architecture.svg)

---

## The Problem

The AI coding assistant landscape is fragmented. You might use Claude Code for complex
reasoning, Codex for agentic tasks, and OpenCode for interactive sessions — each with
its own interface, no shared context, and no way to reach them from your phone or Slack.

## The Solution

Margaret Gateway sits in front of all your AI tools and exposes one clean, unified API.
Any client talks the same protocol. The gateway routes to whichever AI backend you need,
maintains session state, and streams responses back.

| Without Margaret Gateway | With Margaret Gateway |
|---|---|
| N different CLIs, N different interfaces | **One REST + SSE API** for everything |
| Context lost when switching tools | Persistent native session binding |
| No mobile or Slack access | Voice, Slack, Web, CLI — all share sessions |
| Each client needs its own integration | One adapter contract for every backend |

## Key Features

- 🔌 **Universal adapter layer** — SDK-first, CLI-fallback strategy for every backend
- 📡 **Multi-client** — Voice app, Slack DM, Web UI, and local CLI share the same sessions
- 🧵 **Persistent sessions** — Native CLI session binding preserves full conversation context
- 💬 **Embedded Slack** — Socket Mode DM integration in-process, no extra server needed
- 🧠 **Semantic memory** — LightRAG long-term memory injected into agent context
- 🔐 **Optional auth** — Bearer token for self-hosted deployments
- 📦 **Minimal stack** — FastAPI + SQLite + pure Python, runs anywhere

---

## Quick Start

```bash
git clone https://github.com/ggugisky/margaret.git
cd margaret
cp .env.example .env
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Verify:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/agents
```

---

## API

```text
GET  /health
GET  /agents
POST /sessions
GET  /sessions?days=7
GET  /sessions/{session_id}/history?limit=10&before_ts=...
POST /sessions/{session_id}/messages/stream
WS   /ws
```

### Agent and Model Contract

`GET /agents` returns each backend's model list and capabilities:

```json
{
  "agents": [
    {
      "id": "echo",
      "name": "Echo",
      "description": "Development adapter",
      "models": [
        {
          "id": "echo/default",
          "name": "Echo Default",
          "description": "Deterministic development model."
        }
      ],
      "default_model": "echo/default",
      "requires_model": false
    }
  ]
}
```

`POST /sessions` accepts both `agent_id` and `model_id`:

```json
{
  "agent_id": "echo",
  "model_id": "echo/default",
  "client": "margaret-voice",
  "title": "Voice session"
}
```

Backends that require model selection (e.g. OpenCode) declare `requires_model: true`,
so clients can prompt the user before creating a session.

---

## Session Model

Each gateway session binds to exactly one native CLI session.

- **Continuity** — Conversation context is preserved across messages via native CLI resume
- **Immutability** — `agent_id`, `model_id`, and `workspace_path` are fixed at creation
- **Switching** — Create a new session to use a different agent or model
- **Native binding** — `has_native_binding: true` confirms the session is linked to a live process

---

## Phone WebSocket Bridge

`WS /ws` is a text-only bridge for the `margaret-voice` phone protocol.

| Gateway event | Phone event |
|---|---|
| `delta` | `text_delta` |
| `done` | `tts_done`, `done` |
| `error` | `error` |

When the final response links to a Markdown file inside the session workspace,
the bridge also sends `markdown_document` events so the phone app can render the
document without local filesystem access.

---

## Slack Integration

Margaret Gateway can run an embedded Slack Socket Mode client in the same process.

```bash
export SLACK_ENABLED=true
export SLACK_APP_TOKEN=xapp-your-app-level-token
export SLACK_BOT_TOKEN=xoxb-your-bot-token
export MARGARET_WORKSPACE_ROOT=./workspace
```

DM command syntax:

```
default <agent> <model>          — save per-user defaults for new threads
<agent> <model>                  — start a new session in this thread
<agent> <model> <prompt...>      — start a session and run the prompt immediately
```

Thread mapping is immutable: once a DM thread is mapped to a session, start a new
thread to switch agents or models.

Required Slack app scopes: `chat:write`, `app_mentions:read`, `im:history`, `im:read`,
`im:write`, `assistant:write`

Required events: `message.im`, `app_mention`, `assistant_thread_started`

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8787` | HTTP port |
| `MARGARET_DB_PATH` | `~/.margaret/gateway.sqlite3` | SQLite database path |
| `MARGARET_GATEWAY_TOKEN` | *(unset)* | Bearer token for auth |
| `MARGARET_DEFAULT_AGENT` | `echo` | Default agent ID |
| `MARGARET_WORKSPACE_ROOT` | `./workspace` | Per-user workspace root |
| `SLACK_ENABLED` | `false` | Enable embedded Slack client |
| `SLACK_APP_TOKEN` | *(unset)* | Slack app-level token |
| `SLACK_BOT_TOKEN` | *(unset)* | Slack bot token |

When `MARGARET_GATEWAY_TOKEN` is set, all requests must include:

```text
Authorization: Bearer <token>
```

---

## Development

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8787
uv run pytest
```

---

## Adapter Strategy

Margaret Gateway uses a tiered adapter strategy for each AI backend:

1. **SDK adapter** — preferred when a stable SDK is available
2. **Headless CLI** — fallback when no SDK exists or the SDK is unstable
3. **Interactive CLI / PTY** — last resort only

All adapters implement the same internal interface, so clients never need to know
which strategy is active.

---

## License

[MIT](LICENSE)
