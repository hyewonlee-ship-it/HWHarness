# 에이전트 전체 루프 (작동 흐름)

`run_session()` 한 번 호출이 실제로 어떻게 도는지 코드 기준으로 정리한 문서.
모든 단계는 회사 AI 프록시 pass-through 를 통해 실제 모델과 통신한다.

## 전체 구조 한눈에

```
사용자 작업(task)
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│ run_session(task, session_id)            [agent.py]          │
│                                                              │
│  1. SessionManager.resume_or_new()  ── 세션 이어받기/생성    │
│  2. build_system_prompt(...)        ── 구조화 시스템 프롬프트 │
│       ├ TASK CONTEXT ← read_progress()  (이전 progress)      │
│       └ SKILLS       ← load_relevant_skills(task) (키워드)   │
│  3. run_agent(task, messages=session.messages, system, ...)  │
│  4. SessionManager.save() + append_progress()                │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ run_agent — while 루프            [agent.py]                  │
│                                                              │
│  messages += {user: task}                                    │
│  while True:                                                 │
│     ① manage_context(messages)   ── 70% 초과 시 압축         │
│     ② response = client.messages.create(...)  ── 프록시 호출 │
│     ③ messages += {assistant: response.content}              │
│     ④ stop_reason 분기:                                      │
│         end_turn  → 최종 텍스트 return  ✅                    │
│         tool_use  → 툴 실행 → 결과 append → 계속  ↻           │
│         pause_turn→ 그대로 재전송 ↻                           │
└───────────────────────────┬──────────────────────────────────┘
                            │ tool_use
                            ▼
        execute_tool(name, input)  ── read_file/write_file/bash/grep/glob
```

## 단계별 상세

### 0. 진입 — `python agent.py` 또는 `run_session(task, session_id)`

- 모듈 로드 시 `_load_dotenv()` 가 `.env` 를 읽어 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 을 환경변수로 올린다.
- `client = anthropic.Anthropic(base_url=PROXY_URL, auth_token=PROXY_TOKEN)` → SDK 가 `Authorization: Bearer <토큰>` 으로 프록시에 인증. 둘 중 하나라도 없으면 즉시 종료.

### 1. 세션 준비 (`session.py`)

- `resume_or_new(session_id)`: `sessions/<id>.json` 이 있으면 **이전 메시지 히스토리를 로드**해 이어받고, 없으면 새 세션 생성.
- 이전 작업 기록은 `sessions/<id>.progress.txt` 에 누적돼 있고, `read_progress()` 로 읽어 다음 단계에서 주입한다.

### 2. 시스템 프롬프트 조립 (`skills.py`)

`build_system_prompt()` 가 6개 섹션을 만든다:

| 섹션 | 내용 |
|---|---|
| `[ROLE & IDENTITY]` | 에이전트 역할 |
| `[ENVIRONMENT]` | 작업 디렉토리 + 사용 가능한 툴 목록 |
| `[TASK CONTEXT]` | 이전 세션 progress (이어받기) |
| `[RULES]` | 툴 사용 규칙 / 금지 사항 |
| `[OUTPUT FORMAT]` | 응답 형식 |
| `[SKILLS]` | `load_relevant_skills(task)` — task 키워드와 겹치는 `skills/*.md` 주입 |

> 시스템 프롬프트는 **매 API 호출마다 재전송**되므로 컨텍스트 압축에 날아가지 않는다. 그래서 "압축 후에도 살아남아야 하는" progress 를 여기(TASK CONTEXT)에 둔다.

### 3. 에이전트 루프 (`run_agent`, `agent.py`)

`messages` 에 user 작업을 넣고 `while True` 진입. 매 반복:

**① 컨텍스트 관리** — `manage_context(messages, _summarize)` (`context.py`)
- `estimate_tokens` 가 200K 의 70% 를 넘으면:
  - 먼저 `strip_old_tool_results()`: 오래된 `tool_result` **내용만** placeholder 로 치환 (블록·`tool_use_id` 는 보존 → 짝 무결성 유지).
  - 그래도 초과면 `compact_context()`: 오래된 부분을 Haiku 로 요약(`_summarize`)하고 `[요약] + 최근 tail` 로 교체. tail 은 항상 "깨끗한 경계"(문자열 user 턴)에서 시작 → 떠도는 `tool_result` 방지.
- 압축이 일어나면 `session.compaction_count += 1`.

**② 모델 호출** — `client.messages.create(model, max_tokens, tools, messages, system)` → 프록시 → Anthropic.

**③ 히스토리 누적** — `messages += {assistant: response.content}`. 텍스트만이 아니라 **`tool_use` 블록을 포함한 content 전체**를 넣는다 (안 그러면 다음 요청이 깨진다).

**④ `stop_reason` 분기**
- `end_turn` → 응답에서 텍스트를 뽑아 **return** (루프 종료).
- `tool_use` → 모든 `tool_use` 블록을 `execute_tool` 로 실행, 각 결과를 `tool_result`(같은 `tool_use_id`)로 묶어 **한 user 메시지**로 append → `continue`.
- `pause_turn` → 서버사이드 툴 이어가기 위해 그대로 재전송 → `continue`.
- 그 외(max_tokens, refusal 등) → 예외.

### 4. 툴 실행 (`execute_tool`, `agent.py`)

이름으로 디스패치:

| 툴 | 동작 |
|---|---|
| `read_file` | 파일 읽기 (없음/디렉토리/바이너리 에러 처리) |
| `write_file` | 파일 쓰기 (상위 디렉토리 자동 생성) |
| `bash` | 셸 실행 (30s 타임아웃, stdout+stderr, 위험 커맨드 차단) |
| `grep` | 정규식 재귀 검색 (`path:줄번호: 내용`) |
| `glob` | 파일명 패턴 검색 (`**` 재귀) |
| `web_search` | 웹 검색 (회사 프록시 Brave 엔드포인트, 제목·URL·요약) |

결과 문자열은 `tool_result` 로 모델에 돌아가고, 루프는 ②로 복귀한다. 모델이 더 호출할 게 없으면 ④에서 `end_turn` 으로 끝난다.

### 5. 세션 저장 (`session.py`)

- `save()`: `messages` 의 SDK 블록 객체를 `model_dump()` 로 dict 직렬화해 `sessions/<id>.json` 에 기록 (dict 형태는 다음 실행 때 그대로 API 재전송 가능).
- `append_progress()`: `[작업]`/`[결과]` 를 타임스탬프와 함께 `progress.txt` 에 누적 → 다음 세션의 TASK CONTEXT 가 됨.

## 실제 실행 예시 (다중 툴 자율 체이닝)

`run_session("현재 디렉토리의 .py 파일 함수 목록을 result.txt 에 저장해줘")`:

```
[tool] glob({"pattern": "**/*.py"})            → 6개 .py 발견
[tool] grep({"path":"./agent.py","pattern":"^def "})  → 함수 추출 (파일마다 반복)
[tool] write_file({"path":"result.txt", ...})  → 저장
→ stop_reason=end_turn → "총 43개 함수를 result.txt 에 저장했습니다"
```

한 번의 지시로 `glob → grep → write_file` 을 모델이 스스로 순서대로 호출하고, 매 호출마다 위 ①~④ 루프를 돈다. 같은 `session_id` 로 다시 부르면 1번에서 히스토리를 이어받아 "방금 몇 개였지?" 같은 후속 질문에 답한다.

## 관련 파일

| 파일 | 역할 |
|---|---|
| `agent.py` | 루프(`run_agent`) · 5개 툴 · `run_session` · `_summarize` |
| `session.py` | 세션 저장/로드/이어받기 + progress |
| `context.py` | 토큰 추정 · stripping · compaction |
| `skills.py` | 구조화 시스템 프롬프트 + 스킬 로더 |
| `skills/*.md` | 스킬 문서 (키워드 검색 대상) |
