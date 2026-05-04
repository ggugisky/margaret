# AGENTS.md - Margaret Gateway 작업 가이드

이 파일은 agent가 `~/project/margaret`에서 작업할 때 따르는 운영 지침입니다.

## 프로젝트 원칙

- Margaret Gateway는 `margaret-voice`, Slack, Web UI, local CLI가 공통으로 사용하는 control plane입니다.
- API 계약을 먼저 안정화하고, client는 Gateway 계약에 맞춥니다.
- 초기 transport는 REST+SSE를 기본으로 합니다.
- CLI/SDK 차이는 adapter 계층 안에 숨깁니다.
- agent/model/session은 Gateway의 1급 개념으로 유지합니다.
- destructive action은 adapter에서 즉시 실행하지 않고 승인 흐름을 붙일 수 있게 설계합니다.

## Adapter 전략

- SDK가 안정적으로 제공되는 LLM app은 SDK 우선으로 구현합니다.
- SDK가 없거나 불안정한 LLM app은 headless/non-interactive CLI를 fallback으로 사용합니다.
- interactive CLI/PTY 직접 제어는 마지막 선택지로 둡니다.
- 모든 adapter는 Gateway 내부 공통 인터페이스를 구현해야 합니다.
- OpenCode처럼 model 선택이 필수인 app은 `requires_model=true`와 model 목록을 명시해야 합니다.

## 현재 구현 범위

- FastAPI app
- SQLite session/event 저장
- REST API:
  - `GET /health`
  - `GET /agents`
  - `POST /sessions`
  - `GET /sessions`
  - `GET /sessions/{session_id}/history`
  - `POST /sessions/{session_id}/messages/stream`
- SSE streaming
- `echo` development adapter
- optional bearer token auth
- agent별 `models`, `default_model`, `requires_model`
- session별 `model_id` 저장 및 adapter 전달

## 아직 하지 않는 것

- 실제 Codex/OpenCode/Claude Code/Copilot adapter
- subprocess lifecycle 관리
- SDK 기반 adapter 구현
- Slack App 연동
- Web UI
- workspace allowlist enforcement
- approval/HITL flow
- multi-user 권한 모델

## 작업 언어

- 사용자가 한국어로 요청하면 한국어로 응답합니다.
- 코드, API path, env var, schema field는 영어를 사용합니다.
- 변경 결과는 파일, 의도, 검증 여부 중심으로 간결하게 보고합니다.

## 검증

기본 검증 명령:

```bash
cd ~/project/margaret
uv run pytest
```

개발 서버 실행:

```bash
cd ~/project/margaret
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```

