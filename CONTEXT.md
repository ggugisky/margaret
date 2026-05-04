# CONTEXT - Margaret Gateway

## 프로젝트 목적

Margaret Gateway는 여러 클라이언트에서 로컬 LLM CLI/SDK agent를 제어하기 위한 개인용 gateway입니다.

핵심 목적은 이동 중에는 `margaret-voice`나 Slack으로 작업을 시작하고, PC에서는 같은 session을 이어서 확인하거나 제어할 수 있게 만드는 것입니다.

## 클라이언트

- `margaret-voice`: 모바일 음성/텍스트 클라이언트
- Slack App: DM/thread 기반 작업 제어
- Web UI: session 목록, log viewer, attach UX
- Local CLI: 개발/디버깅용 직접 제어

## 대상 LLM APP

- Codex CLI
- OpenCode CLI
- Claude Code
- GitHub Copilot CLI

각 LLM app은 SDK 지원 수준과 headless CLI 지원 수준이 다르므로 Gateway는 app별 adapter를 둡니다.

## SDK vs Headless CLI 판단

SDK가 CLI보다 좋은 이유:

- 구조화된 request/response schema를 제공함
- session id, thread id, model id, status를 명시적으로 다룰 수 있음
- `delta`, `tool_call`, `tool_result`, `error`, `done` 같은 event를 안정적으로 받을 수 있음
- cancel, timeout, retry, error handling이 깔끔함
- CLI stdout/stderr 파싱보다 버전 변화에 강함
- 테스트와 mocking이 쉬움

하지만 모든 LLM app에 안정적인 SDK가 있는 것은 아닙니다. 그래서 Margaret의 기본 전략은 다음과 같습니다.

- SDK가 안정적이면 SDK adapter 사용
- SDK가 없거나 기능이 부족하면 headless/non-interactive CLI adapter 사용
- interactive CLI/PTY 제어는 최후순위

## 현재 Gateway 계약

1차 구현은 REST+SSE입니다.

```text
GET  /health
GET  /agents
POST /sessions
GET  /sessions?days=7
GET  /sessions/{session_id}/history?limit=10&before_ts=...
POST /sessions/{session_id}/messages/stream
```

## Agent And Model Contract

`GET /agents`는 agent와 선택 가능한 model 목록을 반환합니다.

```json
{
  "agents": [
    {
      "id": "echo",
      "name": "Echo",
      "description": "Development adapter that streams back the user message.",
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

`POST /sessions`는 `agent_id`와 `model_id`를 받습니다.

```json
{
  "agent_id": "echo",
  "model_id": "echo/default",
  "client": "margaret-voice",
  "title": "Voice session",
  "workspace_path": "/Users/hykwon/project/diary"
}
```

OpenCode처럼 model 선택이 필수인 adapter는 `requires_model=true`로 노출합니다.

## Streaming Contract

`POST /sessions/{session_id}/messages/stream`은 `text/event-stream`을 반환합니다.

예상 event:

- `status`: 실행 상태
- `delta`: assistant streaming text
- `done`: 최종 응답
- `error`: 오류

`margaret-voice`는 `delta`를 기존 WebSocket `text_delta`로 변환하고, `done.text`를 TTS pipeline으로 넘깁니다.

## Persistence

현재 저장소는 SQLite입니다.

- sessions:
  - `session_id`
  - `agent_id`
  - `model_id`
  - `title`
  - `client`
  - `workspace_path`
  - `status`
  - `created_at`
  - `updated_at`
- events:
  - `event_id`
  - `session_id`
  - `role`
  - `content`
  - `created_at`
- adapter_bindings (Persistent CLI Sessions):
  - `session_id`: Gateway session mapping
  - `native_session_id`: Underlying CLI session ID (thread_id, etc.)
  - `last_active_at`: For stale session cleanup

## Implementation Details

- **Native CLI Session Persistence**: Gateway maintains a mapping between its own sessions and the underlying native CLI sessions via the `adapter_bindings` table.
- **Concurrency Control**: Per-session `asyncio.Lock` is used to prevent concurrent streaming requests. Subsequent stream requests to an active session return a `409 Conflict`.
- **Session Response**: The `has_native_binding` field indicates if a persistent CLI link exists.
- **Recovery**: Startup recovery logic identifies and manages stale or orphaned native sessions.

## 현재 상태

- `~/project/margaret`에 Gateway MVP가 구현되어 있습니다.
- 개발용 `echo` adapter만 있습니다.
- 실제 Codex/OpenCode/Claude Code/Copilot adapter는 아직 없습니다.

## 아직 하지 않는 것

- agent/model switching within a session: 세션 생성 시 지정된 agent/model은 변경할 수 없으며, 전환 시 새 세션을 생성해야 함
- summary-based handoff: 서로 다른 agent 간의 요약 기반 맥락 전달 기능은 구현되지 않음
- child-session navigation API: 하위 세션 탐색 및 관리 API 미지원
- subprocess lifecycle 관리
- SDK 기반 adapter 구현
- Slack App 연동
- Web UI
- workspace allowlist enforcement
- approval/HITL flow
- multi-user 권한 모델

