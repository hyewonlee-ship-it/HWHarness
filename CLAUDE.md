# CLAUDE.md

Claude Code 가 이 저장소에서 작업할 때 참고하는 가이드. **코드로 알 수 있는 건 적지 않고, 모르면 깨지는 것(불변식·게터차)과 규약만** 담는다. 구현 세부는 코드 docstring, 설계 근거는 `SYSTEM_PROMPT_DESIGN.md` 참조.

## 개요

회사 AI 프록시(pass-through) 기반 **Claude Code 스타일 에이전트 하네스**. 진입점 `agent.py`(수동 에이전트 루프 + 툴 8종 + 세션/컨텍스트/스킬/캐싱). 보조: `chat.py`(터미널), `server.py`(웹 SSE UI, 127.0.0.1).

## 실행

```bash
pip install anthropic            # 유일한 의존성
cp .env.example .env             # ANTHROPIC_AUTH_TOKEN 에 프록시 토큰
python agent.py                  # chat.py(CLI) · server.py(웹) · python -m pytest tests/
```

## 프록시 설정 (게터차 주의)

- `ANTHROPIC_BASE_URL=https://<company-proxy>/anthropic` — **`/anthropic` 접미사 필수** (SDK 가 `/v1/messages` 를 덧붙여 `.../anthropic/v1/messages` 가 실제 엔드포인트).
- `ANTHROPIC_AUTH_TOKEN=aiproxy_...` — `anthropic.Anthropic(auth_token=...)` 로 넣어 `Authorization: Bearer` 전송.
- `ANTHROPIC_API_KEY` **설정 금지** (x-api-key 와 두 헤더 충돌 시 거부). 코드에 `base_url` URL 리터럴 하드코딩 금지 — PostToolUse 훅(`.claude/hooks/check_agent.py`)이 차단, 환경변수에서 읽어라.

## 깨지기 쉬운 불변식

- **히스토리**: 매 턴 `response.content` **전체**(text + tool_use 블록)를 append. 텍스트만 추출해 붙이면 다음 요청이 깨진다.
- **tool_result 페어링**: 각 `tool_result.tool_use_id` 는 원래 `tool_use` id 와 일치해야 한다.
- **컨텍스트 압축 경계**: 남기는 꼬리는 `tool_result` 가 아니라 **문자열 user 턴**("클린 헤드")에서 시작해야 고아 `tool_result`(400) 가 안 난다 — `context.py:_clean_head_index`.
- **세션 직렬화**: assistant content(SDK Pydantic 블록)는 저장 전 `serialize_messages()` 가 `.model_dump()` 로 dict 화한다(그래야 재전송 가능).
- **압축 시 보안 제약 보존**: `_summarize` 는 사용자가 명시한 보안/금지 제약을 verbatim 보존하도록 지시돼 있다(압축돼도 인젝션 방어/금지 규칙이 사라지지 않게).

## 알아둘 동작 (코드와 다르게 추측하기 쉬운 것)

- **`web_search` 는 기본 서버사이드** (`WEB_SEARCH_SERVER_SIDE`, `web_search_20250305`): 검색이 한 호출 안에서 서버에서 끝나 `tool_use` 로 돌아오지 않으니 `execute_tool` 로 실행하지 않는다. 토글로 클라이언트사이드(프록시 Brave) 전환 가능.
- **`edit_file` 은 stale 감지**: `old_string` 이 현재 파일에 없거나 여러 곳이면 교체를 거부(`Error:`→`is_error`)하고 재확인을 유도한다. 새 파일/전체 교체는 `write_file`.
- **여러 tool_use 는 의존성 인식 스케줄링**: 자원 충돌(같은 경로 쓰기, bash=전역 장벽)만 순차, 독립은 병렬 (`_run_tool_batch`/`_schedule_stages`). 승인 게이트는 메인 스레드 순차.
- **인젝션 방어**: 외부 입력 툴(`EXTERNAL_TOOLS`) 결과는 `<tool_output>` 로 감싸 데이터/지시 경계를 박는다.
- 시스템 프롬프트 4요소(환경/툴규율/에러복구/인젝션방어)의 근거는 `SYSTEM_PROMPT_DESIGN.md`. 스킬은 키워드 검색→주입(RAG 아님): `skills/*.md` 추가만으로 확장.

## 규약 / 금지

- 모델은 `MODEL` 상수 `claude-haiku-4-5` 로 고정 — PostToolUse 훅이 변경 차단. 바꾸려면 이 규약부터 갱신.
- 토큰·내부 프록시 URL 절대 커밋 금지 (`.env`, `.docs/`, `sessions/` 는 gitignore).
- `MAX_TOKENS` 16000 — 크게 올리려면 스트리밍 전환 먼저. `bash` 차단은 정규식 가드레일이지 샌드박스 아님(신뢰 환경용).
- 한 번에 너무 많은 기능을 구현하지 말 것 — 작게 검증하며 진행.
