# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-14

### Features

- **Gateway application** — FastAPI-based local REST+SSE control plane for CLI agent sessions
- **Session management** — SQLite-backed session and event persistence with full history API
- **Agent contract** — `GET /agents` returns per-agent model list, `default_model`, and `requires_model` flag
- **Echo adapter** — Deterministic development adapter for local testing without real LLM backends
- **Slack integration** — Embedded socket-mode client with threading, workspace-per-user routing, slash commands (`/agents`, preferences), and channel mention routing
- **Voice gateway** — Voice client routes persisted in SQLite; markdown documents forwarded to voice clients
- **RAG memory** — LightRAG-based semantic long-term memory with injection into agent context and explicit `/memory search` command
- **Agent discovery** — Automatic discovery of user-installed CLI agents with model enumeration
- **Stream resilience** — Typing indicators, agent status events, and canceled stream recording

### Bug Fixes

- Align advertised agent models with actual adapter capabilities
- Fall back gracefully when default agent is unavailable
- Handle large CLI JSON stream lines without truncation
- Keep RAG initialization failures non-fatal
- Restrict gateway documentation endpoint to private host
- Strip leading newline from first stream delta
- Record canceled gateway streams in session history

### Chores

- uv project scaffolding with `pyproject.toml`
- Runtime artifacts (`sessions.db`, `logs/`, `workspace/`) excluded from version control

[0.1.0]: https://github.com/ggugisky/margaret/releases/tag/v0.1.0
