# DEVELOPMENT - Margaret Gateway 개발 현황

## 현재 구현 완료

- FastAPI app 생성
- `uv` 기반 Python project 설정
- SQLite `Store` 구현
- REST+SSE API 구현
- `echo` development adapter 구현
- `codex` adapter 구현
- `opencode` adapter 구현
- `claude-code` adapter 구현
- `copilot` adapter 구현
- Persistent native CLI session resume 구현
- `adapter_bindings` 기반 native session persistence 구현
- `has_native_binding` session metadata 구현
- per-session concurrency lock 및 stale session recovery 구현
- Embedded Slack Socket Mode DM MVP 구현
- Slack thread ↔ session mapping 구현
- Slack user default (`default <agent> <model>`) 구현
- Slack 새 thread bootstrap (`<agent> <model> [prompt...]`) 구현
- optional bearer token auth 구현
- pytest 기반 Gateway test 작성
- agent/model selection 계약 반영
- session에 `model_id` 저장
- adapter 호출 시 `model_id` 전달

## 파일 구조

```text
margaret/
├── app/
│   ├── adapters.py
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   ├── store.py
│   └── slack/
│       ├── __init__.py
│       ├── handlers.py
│       ├── models.py
│       └── service.py
├── tests/
│   ├── test_adapters.py
│   ├── test_concurrency.py
│   ├── test_gateway.py
│   └── test_slack.py
├── AGENTS.md
├── CONTEXT.md
├── DEVELOPMENT.md
├── README.md
├── pyproject.toml
└── .env.example
```

## 주요 환경 변수

```env
PORT=8787
MARGARET_DB_PATH=~/.margaret/gateway.sqlite3
MARGARET_GATEWAY_TOKEN=
MARGARET_DEFAULT_AGENT=echo
SLACK_ENABLED=false
SLACK_APP_TOKEN=
SLACK_BOT_TOKEN=
```

`MARGARET_GATEWAY_TOKEN`이 설정되면 요청에 다음 header가 필요합니다.

```text
Authorization: Bearer <token>
```

## 실행

```bash
cd ~/project/margaret
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```

현재 개발 서버 기본 URL:

```text
http://127.0.0.1:8787
```

## 검증

```bash
cd ~/project/margaret
uv run pytest
```

현재 확인된 결과:

```text
35 passed
```

## 운영 상태

- 로컬 GitLab 동기화 완료
- 원격 `nana` 배포 상태:
  - `~/project/margaret-dev` → `DEV` 브랜치, 포트 `38091`
  - `~/project/margaret` → production은 현재 중지 상태
- DEV는 Slack 활성화 + 기본 agent `codex`
- production은 릴리즈 명령이 있을 때만 반영하도록 운영 중

## Smoke Test

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/agents
```

session 생성:

```bash
curl -s -X POST http://127.0.0.1:8787/sessions \
  -H 'content-type: application/json' \
  -d '{"agent_id":"echo","model_id":"echo/default","client":"smoke","title":"Model Smoke"}'
```

SSE stream:

```bash
curl -s -N -X POST "http://127.0.0.1:8787/sessions/<session_id>/messages/stream" \
  -H 'content-type: application/json' \
  -d '{"text":"hello margaret"}'
```

## 다음 개발 단계

### 1. Lifespan 전환

- FastAPI `@app.on_event("startup"/"shutdown")`를 lifespan으로 전환
- 현재 pytest 경고 제거

### 2. Slack 운영 안정화

- Slack app 설정(`message.im`, scopes, reinstall`) 확인 절차 문서화
- Slack DM 실패 원인 로깅 강화
- reply 실패 시 `say()` 예외 로깅 추가

### 3. Voice 연동 UX 정교화

- `has_native_binding` 활용
- busy(409) 처리 UX 정리
- session continuation 흐름 정리

### 4. Session 전환 전략

- child session / handoff 설계
- agent/model 전환 시 새 session 생성 UX 정리

## 설계 결정 기록

- Gateway는 SDK-only 또는 CLI-only가 아니라 app별 최선 adapter 전략을 사용합니다.
- SDK가 있으면 SDK 우선입니다.
- SDK가 없거나 불안정하면 headless CLI를 사용합니다.
- interactive CLI/PTY 제어는 마지막 선택입니다.
- `model_id`는 session의 1급 필드입니다.
- `GET /agents`에서 model 목록과 `requires_model`을 제공해 client가 사전에 선택할 수 있게 합니다.
