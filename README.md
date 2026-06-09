# HWHarness

회사 AI 프록시(pass-through) 기반의 **Claude Code 스타일 에이전트 하네스**.
날것의 Messages API 위에 에이전트 루프 · 툴 8종 · 세션 · 컨텍스트 관리 · 스킬 · SSE 스트리밍 · 프롬프트 캐싱을 직접 구현했다.

## 빠른 시작

```bash
pip install anthropic            # 유일한 의존성
cp .env.example .env             # ANTHROPIC_AUTH_TOKEN 에 회사 AI 프록시 토큰 입력
python agent.py                  # 데모 1회 실행 (세션 기반)
python chat.py                   # 대화형 CLI
python server.py                 # 웹 챗 UI (127.0.0.1, SSE 스트리밍)
python demo.py                   # 전체 기능 시연 (보고/데모용)
python -m pytest tests/          # 단위 테스트 36개 (네트워크 불필요)
python bench_caching.py          # 프롬프트 캐싱 실측 벤치 (실제 프록시 호출)
```

### 전역 `HWHarness` 명령 (선택)

어디서든 `HWHarness` 를 치면 대화형 CLI 가 뜨게 하려면 PATH 에 런처를 링크한다 (`~/.local/bin` 이 PATH 에 있어야 함):

```bash
ln -sf "$PWD/bin/HWHarness" ~/.local/bin/HWHarness
HWHarness   # 이제 어느 디렉토리에서든 실행
```

런처는 자기 위치를 따라 프로젝트 루트를 자동으로 찾으므로 경로 수정이 필요 없다.

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
| `agent.py` | 에이전트 루프 + 툴 8종 + 세션/컨텍스트/스킬/캐싱 통합. 진입점. |
| `session.py` | 세션 저장/로드/이어받기(JSON) + `progress.txt` |
| `context.py` | 컨텍스트 관리 — Compaction(요약) + Stripping(툴 결과 제거) |
| `skills.py` | 구조화 시스템 프롬프트 빌더 + 키워드 기반 스킬 로더 |
| `skills/*.md` | 스킬 문서 (키워드로 검색되어 시스템 프롬프트에 주입) |
| `chat.py` | 대화형 터미널 CLI (승인 게이트 · 슬래시 명령) |
| `server.py` | 웹 챗 UI (127.0.0.1 바인딩, SSE 토큰 스트리밍) |
| `bench_caching.py` | 프롬프트 캐싱 토큰/비용 실측 벤치 |
| `tests/` | 단위 테스트 36개 (mock client, 네트워크 불필요) |

### 에이전트 루프 (멀티턴)

`messages` 히스토리를 누적하며 `stop_reason` 으로 분기한다: `end_turn` → 종료, `tool_use` → 툴 실행 후 결과 반환, `pause_turn` → 재전송. 한 작업이 여러 모델 호출(턴)로 진행되며 `MAX_TURNS`(25) 초과 시 우아하게 마무리한다. 매 호출 전 컨텍스트 관리를 수행한다. `end_turn` 의 최종 텍스트는 **모든 text 블록을 이어붙여** 반환한다(서버사이드 검색처럼 응답이 여러 블록으로 쪼개져 와도 누락 방지).

### 툴 (8종)

`TOOLS`(스키마)와 `execute_tool`(디스패치) 두 곳에 정의한다.

| 툴 | 설명 |
|---|---|
| `read_file` | 파일 읽기 |
| `write_file` | 파일 전체 쓰기 (상위 디렉토리 자동 생성) |
| `edit_file` | **부분 교체** (`old_string`→`new_string`) + **stale content 감지** |
| `bash` | 셸 실행 (30s 타임아웃 + 위험 커맨드 차단 + 승인 게이트) |
| `grep` | 파일 내용 정규식 검색 (재귀) |
| `glob` | 파일명 패턴 (`**` 재귀) |
| `web_fetch` | **특정 URL 본문을 로컬에서** 가져옴 (http/https, HTML→텍스트) |
| `web_search` | **서버사이드** 웹검색 (`web_search_20250305`, Anthropic 서버가 수행) |

### 세션 관리

`run_session(task, session_id=...)` — 같은 `session_id` 로 재실행하면 이전 히스토리·progress 를 이어받는다. assistant 의 SDK 블록 객체는 dict 로 직렬화해 저장하며, dict 형태는 그대로 API 에 재전송 가능하다.

### 컨텍스트 관리 (클라이언트사이드)

토큰 추정이 200K 의 70% 를 넘으면 → ① Stripping(오래된 `tool_result` 내용만 치환, ID·구조 보존) → ② 여전히 초과면 Compaction(Haiku 로 요약, `[요약] + 최근 tail`). tail 은 항상 "깨끗한 경계"(문자열 user 턴)에서 시작해 `tool_use`/`tool_result` 짝이 끊기지 않게 한다. 요약 시 사용자가 명시한 보안·금지 제약은 그대로(verbatim) 보존한다.

### 시스템 프롬프트 + 스킬

`[ROLE] [ENVIRONMENT] [TASK CONTEXT] [RULES] [OUTPUT FORMAT] [SKILLS]` 구조. 네 요소(환경 주입 · 툴 선택 규율 · 에러 복구 · 인젝션 방어)를 의도적으로 담는다(근거는 `SYSTEM_PROMPT_DESIGN.md`). progress 는 TASK CONTEXT 에(시스템 프롬프트라 압축에 안 날아감), 작업과 키워드가 겹치는 `skills/*.md` 는 SKILLS 에 주입(검색→주입, RAG 아님).

## 심화 구현 (피드백 과제)

기본 6단계 위에 다음을 추가했다. 상세 학습 노트는 `.docs/` 참고.

| # | 항목 | 핵심 |
|---|---|---|
| 1 | **시스템 프롬프트 재설계** | 4요소(환경/툴규율/에러복구/인젝션방어) + 2중 인젝션 방어(프롬프트 규칙 + `<tool_output>` 토큰 래핑) |
| 2 | **SSE 스트리밍** | `messages.stream()` + `on_event` 콜백 → 웹 UI 토큰 실시간 렌더 |
| 3 | **프롬프트 캐싱** | system 캐싱(`CACHE_PROMPT`) + **멀티턴 메시지 캐싱**(`CACHE_MESSAGES`, 마지막 메시지 브레이크포인트). 실측 4턴 약 79% 절감 |
| 4 | **web_search 서버사이드** | 클라이언트 검색 대신 Anthropic 서버가 한 호출 안에서 검색(`server_tool_use`/`web_search_tool_result`) |
| 5 | **web_fetch (로컬)** | 지정 URL 을 우리 루프가 직접 fetch. http/https 만 허용(SSRF 가드), HTML→텍스트, 길이 컷 |
| 6 | **edit_file (부분 수정)** | `old_string`→`new_string` 교체. **stale 감지**: 없거나 여러 곳이면 실패시켜 `read_file` 재확인·복구 유도 |

### 서버 위임 vs 로컬 실행 판단 기준 (#4·#5 학습 포인트)

"무엇을 할지 모델이 판단하고, 공개 자원만 쓰며, 부작용 없는" 도구(검색)는 **서버 위임**. "대상이 확정돼 있고, 우리 머신의 자원/권한이 필요하거나, 부작용·통제·안전이 걸린" 도구(URL fetch, 파일, edit, bash)는 **로컬 실행**. `web_search`(서버) ↔ `web_fetch`(로컬)가 가장 깨끗한 대비 쌍이다.

## 가이드 기본 6단계 매핑

| 단계 | 내용 | 위치 |
|---|---|---|
| 1 | 기본 agent loop | `agent.py: run_agent` |
| 2 | 툴 (현재 8종) | `agent.py: TOOLS / execute_tool` |
| 3 | 세션 관리 | `session.py` |
| 4 | 컨텍스트 관리 | `context.py` |
| 5 | 시스템 프롬 + 스킬 | `skills.py`, `skills/` |
| 6 | 프록시 연동 | `agent.py` 클라이언트 구성 |

## 제약 / 안전장치

- 모델은 `claude-haiku-4-5` 로 고정 (PostToolUse hook `.claude/hooks/check_agent.py` 가 변경 차단).
- `bash` 차단은 정규식 가드레일이지 완전한 샌드박스가 아니다 (신뢰 환경용). 위험 툴은 승인 게이트를 거친다.
- `web_fetch` 는 http/https 만 허용해 `file://` 등 로컬 파일 접근/SSRF 류를 막는다.
- 외부 입력 툴(`read_file`/`grep`/`glob`/`bash`/`web_search`/`web_fetch`) 결과는 `<tool_output>` 으로 감싸 "데이터 vs 지시" 경계를 토큰 레벨로 박는다 (인젝션 방어).
- 웹 서버는 `127.0.0.1` 에만 바인딩한다.
- `.env`, `sessions/`, `.docs/` 는 `.gitignore` 로 커밋에서 제외.
