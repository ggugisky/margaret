# Margaret Gateway

Margaret Gateway is a local REST+SSE gateway for controlling CLI agent sessions from clients such as Margaret Voice, Slack, Web UI, and local CLI tools.

## MVP API

```text
GET  /health
GET  /agents
POST /sessions
GET  /sessions?days=7
GET  /sessions/{session_id}/history?limit=10&before_ts=...
POST /sessions/{session_id}/messages/stream
WS   /ws
```

## Agent And Model Contract

`GET /agents` returns selectable agents and their models:

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

The response includes a `has_native_binding` field:

```json
{
  "session_id": "...",
  "has_native_binding": true,
  "...": "..."
}
```

Adapters such as OpenCode can set `requires_model=true` so clients must choose a model before creating a session.

## Phone WebSocket Bridge

`WS /ws` is a text-only compatibility bridge for the existing `margaret-voice`
phone protocol. It creates or resumes Gateway sessions and maps Gateway stream
events to phone events:

- `delta` -> `text_delta`
- `done` -> `tts_done`, `done`
- `error` -> `error`

When `MARGARET_GATEWAY_TOKEN` is set, WebSocket clients can authenticate with
either `Authorization: Bearer <token>` or `/ws?token=<token>`.

## Session Persistence

Each Gateway session binds to exactly one native CLI session (e.g., a specific Codex thread or OpenCode session).

- **Continuity**: Conversation context is preserved across messages via native CLI resume capabilities.
- **Immutability**: `agent_id`, `model_id`, and `workspace_path` are immutable once a session is created.
- **Switching**: To use a different agent or model, you must create a new session.
- **Native Binding**: The `has_native_binding` flag in the session response indicates whether the session is successfully linked to a persistent native CLI process.
- **Handoff**: Summary-based handoff between different agents is not implemented in the current version.

## Development

```bash
cd ~/project/margaret
uv sync
uv run uvicorn app.main:app --reload --port 8787
```

Optional auth:

```bash
export MARGARET_GATEWAY_TOKEN=change-me
```

When `MARGARET_GATEWAY_TOKEN` is set, requests must include:

```text
Authorization: Bearer change-me
```

## Embedded Slack Socket Mode (DM MVP)

Margaret Gateway can run an embedded Slack Socket Mode client in the same process.

- Scope: DM/text-only MVP (ignores non-DM events and bot/self messages)
- Routing: one Slack DM thread maps to one Margaret session
- Execution: Slack bridge uses in-process adapter registry/store (no HTTP loopback)

DM command syntax (plain text, no slash commands):

- `@bot default <agent> <model>` (or mentionless `default <agent> <model>`):
  save per-user defaults for future **new threads** only.
- `@bot <agent> <model>` (or mentionless `<agent> <model>`):
  in a **new thread**, create a session pinned to that agent/model.
- `@bot <agent> <model> <prompt...>`:
  in a **new thread**, create a session and run `<prompt...>` immediately.

Thread mapping is immutable: once a DM thread is mapped to a session, agent/model
cannot be switched in that thread. Start a new thread to switch.

Environment variables:

```bash
export SLACK_ENABLED=true
export SLACK_APP_TOKEN=xapp-your-app-level-token
export SLACK_BOT_TOKEN=xoxb-your-bot-token
```

When `SLACK_ENABLED` is `false` (default), startup behavior remains unchanged.
