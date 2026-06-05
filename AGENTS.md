# AGENTS.md

Guidelines for AI agents working in this repository.

## Project Principles

- Margaret Gateway is the shared control plane for `margaret-voice`, Slack, Web UI, and local CLI clients.
- Stabilize the API contract first; clients adapt to the gateway contract, not the other way around.
- Default transport is REST + SSE.
- Differences between CLI and SDK backends are hidden inside the adapter layer.
- `agent`, `model`, and `session` are first-class concepts in the gateway.
- Destructive actions must be designed to support an approval flow rather than executing immediately in the adapter.

## Adapter Strategy

- Prefer SDK-based adapters when a stable SDK is available for the target LLM app.
- Fall back to headless/non-interactive CLI when no stable SDK exists.
- Interactive CLI / PTY control is the last resort.
- Every adapter must implement the gateway's common internal interface.
- Apps that require model selection (e.g., OpenCode) must declare `requires_model=true` and provide a model list.

## Current Scope

- FastAPI application
- SQLite-backed session and event storage
- REST API:
  - `GET /health`
  - `GET /agents`
  - `POST /sessions`
  - `GET /sessions`
  - `GET /sessions/{session_id}/history`
  - `POST /sessions/{session_id}/messages/stream`
- SSE streaming
- `echo` development adapter
- Optional bearer token auth
- Per-agent `models`, `default_model`, `requires_model`
- Per-session `model_id` stored and forwarded to adapter

## Not Yet Implemented

- Real adapters for Codex / OpenCode / Claude Code / Copilot
- Subprocess lifecycle management
- SDK-based adapter implementations
- Slack App integration
- Web UI
- Workspace allowlist enforcement
- Approval / HITL flow
- Multi-user permission model

## Validation

Run tests:

```bash
uv run pytest
```

Start the development server:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```
