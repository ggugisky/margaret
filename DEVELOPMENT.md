# DEVELOPMENT - Margaret Gateway 개발 현황

## 현재 구현 완료

- FastAPI app 생성
- `uv` 기반 Python project 설정
- SQLite `Store` 구현
- REST+SSE API 구현
- `echo` development adapter 구현
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
│   └── store.py
├── tests/
│   └── test_gateway.py
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
4 passed
```

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

### Phase 1: Adapter Contract 강화

- `AgentAdapter`에 session lifecycle method 추가
- `create_session`, `resume_session`, `stop`, `get_status` 경계 정의
- streaming event를 단순 text delta에서 structured event로 확장
- approval/HITL event type 초안 추가

### Phase 2: OpenCode Adapter

- OpenCode는 model 선택이 중요하므로 우선순위가 높습니다.
- SDK가 안정적이면 SDK adapter로 구현합니다.
- SDK가 부족하면 headless CLI/server mode fallback을 구현합니다.
- `requires_model=true`로 model 선택을 강제합니다.

### Phase 3: Codex Adapter

- 우선 headless `codex exec` 기반 adapter로 시작합니다.
- SDK/server API가 안정적이면 SDK adapter로 교체할 수 있게 내부 계약을 유지합니다.
- session resume/continue 가능성을 별도로 검증합니다.

### Phase 4: Claude Code Adapter

- Claude Code는 SDK와 headless mode가 모두 있으므로 SDK 우선 후보입니다.
- `stream-json` event를 Gateway SSE event로 mapping합니다.

### Phase 5: margaret-voice 연동

- `margaret-voice` server가 Gateway `/agents`를 읽어 agent/model dropdown을 구성합니다.
- `POST /sessions`로 session을 만들고 `model_id`를 저장합니다.
- `/messages/stream` SSE를 기존 WebSocket `text_delta`, `done`, `error`로 변환합니다.

## 설계 결정 기록

- Gateway는 SDK-only 또는 CLI-only가 아니라 app별 최선 adapter 전략을 사용합니다.
- SDK가 있으면 SDK 우선입니다.
- SDK가 없거나 불안정하면 headless CLI를 사용합니다.
- interactive CLI/PTY 제어는 마지막 선택입니다.
- `model_id`는 session의 1급 필드입니다.
- `GET /agents`에서 model 목록과 `requires_model`을 제공해 client가 사전에 선택할 수 있게 합니다.

