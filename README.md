# HWHarness

회사 AI 프록시(pass-through) 기반의 **Claude Code 스타일 에이전트 하네스**.
날것의 Messages API 위에 에이전트 루프 · 툴 · 세션 · 컨텍스트 관리 · 스킬을 직접 구현했다.

## 빠른 시작

```bash
pip install anthropic          # 유일한 의존성
cp .env.example .env           # ANTHROPIC_AUTH_TOKEN 에 회사 AI 프록시 토큰 입력
python agent.py                # 데모 1회 실행 (세션 기반)
python demo.py                 # 전체 기능 시연 (보고/데모용)
python tests/test_agent_loop.py  # 단위 테스트 (네트워크 불필요)
```

## 설정 (회사 프록시 pass-through)

인증·엔드포인트는 모두 환경변수로 주입한다 (`.env` 또는 셸 export). 코드에 하드코딩하지 않는다.

| 변수 | 값 |
|---|---|
| `ANTHROPIC_BASE_URL` | `https://<company-proxy>/anthropic` |
| `ANTHROPIC_AUTH_TOKEN` | 회사 AI 프록시 토큰 (`aiproxy_...`) |

`anthropic.Anthropic(base_url=..., auth_token=...)` 로 구성하면 SDK 가 `Authorization: Bearer` 헤더로 프록시에 인증하고, 프록시가 네이티브 Anthropic `/v1/messages` 로 그대로 전달한다.

## 아키텍처

| 파일 | 역할 |
|---|---|
| `agent.py` | 에이전트 루프 + 6개 툴 + 세션/컨텍스트/스킬 통합. 진입점. |
| `session.py` | 세션 저장/로드/이어받기(JSON) + `progress.txt` |
| `context.py` | 컨텍스트 관리 — Compaction(요약) + Stripping(툴 결과 제거) |
| `skills.py` | 구조화 시스템 프롬프트 빌더 + 키워드 기반 스킬 로더 |
| `skills/*.md` | 스킬 문서 (키워드로 검색되어 시스템 프롬프트에 주입) |
| `tests/` | 단위 테스트 (mock client, 네트워크 불필요) |

### 에이전트 루프

`messages` 히스토리를 누적하며 `stop_reason` 으로 분기한다: `end_turn` → 종료, `tool_use` → 툴 실행 후 결과 반환, `pause_turn` → 재전송. 매 호출 전 컨텍스트 관리를 수행한다.

### 툴 (6종)

`read_file` · `write_file`(디렉토리 자동 생성) · `bash`(30s 타임아웃 + 위험 커맨드 차단) · `grep`(정규식, 재귀) · `glob`(`**` 재귀 패턴) · `web_search`(회사 프록시의 Brave 검색 엔드포인트). `TOOLS`(스키마)와 `execute_tool`(디스패치) 두 곳에 정의.

### 세션 관리

`run_session(task, session_id=...)` — 같은 `session_id` 로 재실행하면 이전 히스토리·progress 를 이어받는다. assistant 의 SDK 블록 객체는 dict 로 직렬화해 저장하며, dict 형태는 그대로 API 에 재전송 가능하다.

### 컨텍스트 관리 (클라이언트사이드)

토큰 추정이 200K 의 70% 를 넘으면 → ① Stripping(오래된 `tool_result` 내용만 치환, ID·구조 보존) → ② 여전히 초과면 Compaction(Haiku 로 요약, `[요약] + 최근 tail`). tail 은 항상 "깨끗한 경계"(문자열 user 턴)에서 시작해 `tool_use`/`tool_result` 짝이 끊기지 않게 한다.

### 시스템 프롬프트 + 스킬

`[ROLE] [ENVIRONMENT] [TASK CONTEXT] [RULES] [OUTPUT FORMAT] [SKILLS]` 구조. progress 는 TASK CONTEXT 에(시스템 프롬프트라 압축에 안 날아감), 작업과 키워드가 겹치는 `skills/*.md` 는 SKILLS 에 주입(검색→주입, RAG 아님).

## 가이드 6단계 매핑

| 단계 | 내용 | 위치 |
|---|---|---|
| 1 | 기본 agent loop | `agent.py: run_agent` |
| 2 | 툴 5종 | `agent.py: TOOLS / execute_tool` |
| 3 | 세션 관리 | `session.py` |
| 4 | 컨텍스트 관리 | `context.py` |
| 5 | 시스템 프롬 + 스킬 | `skills.py`, `skills/` |
| 6 | 프록시 연동 | `agent.py` 클라이언트 구성 |

## 보고 핵심 포인트

1. **API 레벨 이해** — 완성된 하네스를 쓰지 않고 Messages API 의 tool_use 루프부터 직접 구현.
2. **프록시 연동** — 회사 AI 프록시 pass-through 에 Bearer 토큰으로 연결 (멀티 프로바이더 게이트웨이의 `/anthropic` 경로).
3. **컨텍스트 전략** — Stripping(저렴) → Compaction(요약) 에스컬레이션, 짝 무결성 보존.
4. **확장 가능성** — 툴/스킬을 파일 추가만으로 확장 → QA 에이전트, 게임 UI 자동화로 연결 가능.

## 제약 / 안전장치

- 모델은 `claude-haiku-4-5` 로 고정 (PostToolUse hook `.claude/hooks/check_agent.py` 가 변경 차단).
- `bash` 차단은 정규식 가드레일이지 완전한 샌드박스가 아니다 (신뢰 환경용).
- `.env`, `sessions/` 는 `.gitignore` 로 커밋에서 제외.
