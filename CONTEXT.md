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
  - `session_id`
  - `adapter_name`
  - `adapter_state_json`
  - `workspace_path`
  - `status`
  - `created_at`
  - `updated_at`
  - `last_used_at`
- slack_threads:
  - `team_id`
  - `channel_id`
  - `thread_ts`
  - `user_id`
  - `session_id`
  - `created_at`
  - `updated_at`
- slack_user_defaults:
  - `team_id`
  - `user_id`
  - `agent_id`
  - `model_id`
  - `created_at`
  - `updated_at`

## Implementation Details

- **Native CLI Session Persistence**: Gateway maintains a mapping between its own sessions and the underlying native CLI sessions via the `adapter_bindings` table.
- **Concurrency Control**: Per-session `asyncio.Lock` is used to prevent concurrent streaming requests. Subsequent stream requests to an active session return a `409 Conflict`.
- **Session Response**: The `has_native_binding` field indicates if a persistent CLI link exists.
- **Recovery**: Startup recovery logic identifies and manages stale or orphaned native sessions.
- **Slack DM Mapping**: 하나의 Slack DM thread는 하나의 Margaret session에 고정 매핑됩니다.
- **Slack User Defaults**: Slack 사용자는 `default <agent> <model>` 형식으로 사용자별 기본 agent/model을 저장할 수 있습니다.
- **Slack Thread Bootstrap**: 새 DM thread에서만 `@bot <agent> <model> [prompt...]` 형식으로 agent/model을 선택할 수 있고, 기존 thread에서는 변경할 수 없습니다.
- **Slack Channel Mentions**: channel에서 `app_mention`으로 들어온 요청도 원본 message `ts`를 기준으로 thread session을 생성합니다.
- **Slack Native Streaming**: Slack client가 전달되면 `chat.startStream` / `chat.appendStream` / `chat.stopStream`으로 thread 안에 streaming 답변을 남깁니다.
- **Slack Thread Limitation**: Slack은 thread reply 아래 nested thread를 지원하지 않습니다. reply message의 `ts`를 `thread_ts`로 보내도 root thread로 보정되므로, thread 내부 질문은 같은 root thread에 답합니다.

## 현재 상태

- `~/project/margaret`에 Gateway MVP가 구현되어 있습니다.
- 실제 adapter가 구현되어 있습니다:
  - `echo`
  - `codex`
  - `opencode`
  - `claude-code`
  - `copilot`
- Persistent CLI session resume가 구현되어 있습니다.
- Embedded Slack Socket Mode DM MVP가 구현되어 있습니다.
- Slack DM/channel mention 응답은 root thread에 native streaming 방식으로 작성됩니다.
- Slack DM에서는 다음이 가능합니다:
  - 사용자별 default 저장: `@bot default <agent> <model>`
  - 새 thread에서 agent/model 지정: `@bot <agent> <model> [prompt...]`
- 현재 운영 상태:
  - `nana:/home/ggugi/project/margaret-dev` → `DEV` 브랜치, 포트 `38091`, Slack 활성화, 기본 agent `codex`
  - `nana:/home/ggugi/project/margaret` → production은 현재 중지 상태

## Slack 운영 메모

- Slack app manifest에는 DM thread를 막는 옵션이 없습니다.
- DM/thread reply는 `chat.postMessage` 또는 `chat.startStream` 호출 시 `channel`과 `thread_ts`로 결정됩니다.
- DM에서는 이벤트 payload의 `channel` 값인 `D...` channel id를 사용해야 합니다.
- 앱 설정 변경 후에는 반드시 workspace reinstall이 필요합니다.
- 권장 bot scopes/events:
  - scopes: `chat:write`, `app_mentions:read`, `im:history`, `im:read`, `im:write`, `assistant:write`
  - events: `message.im`, `app_mention`, `assistant_thread_started`, `assistant_thread_context_changed`
  - Socket Mode app token: `connections:write`

## 아직 하지 않는 것

- agent/model switching within a session: 세션 생성 시 지정된 agent/model은 변경할 수 없으며, 전환 시 새 세션을 생성해야 함
- summary-based handoff: 서로 다른 agent 간의 요약 기반 맥락 전달 기능은 구현되지 않음
- child-session navigation API: 하위 세션 탐색 및 관리 API 미지원
- subprocess pooling / daemonization
- SDK 기반 adapter 구현
- Web UI
- workspace allowlist enforcement
- approval/HITL flow
- multi-user 권한 모델
