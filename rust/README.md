# HWHarness (Rust 포팅)

Python 하네스의 코어를 Rust 로 옮긴 것. **lib(엔진) + bin(CLI)** 구조라, 나중에 TUI 는
이 lib 를 그대로 쓰는 또 다른 프런트엔드로 붙이면 된다.

## 빌드 / 실행

```bash
cd rust
cp ../.env .env          # 또는 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN export
cargo run                # 대화형 CLI
```

> 공식 Rust SDK 가 없어 `reqwest` 로 Messages API 를 직접 호출한다(원시 HTTP).
> MSRV: Rust 1.65+ (let-else, `std::thread::scope` 사용).

## 구조

| 파일 | 역할 |
|---|---|
| `src/lib.rs` | 상수 · `AgentEvent` · `Emit`/`Approve` 타입 · 모듈 묶음 |
| `src/config.rs` | `.env`/환경변수 → 프록시 설정 |
| `src/client.rs` | Messages API 호출(reqwest) + `cache_messages`(멀티턴 캐싱) + `join_text` |
| `src/tools.rs` | 툴 8종 + 디스패치 + 스키마 + `make_tool_result`(인젝션 래핑) |
| `src/schedule.rs` | 의존성 스케줄링(자원 충돌 → stage 분리) |
| `src/agent.rs` | 에이전트 루프 + 병렬/의존 툴 실행 + final_wrapup |
| `src/context.rs` | 컨텍스트 관리(stripping → compaction, 클린 헤드 경계) |
| `src/session.rs` | 세션 JSON + progress |
| `src/skills.rs` | 시스템 프롬프트 빌더 + 키워드 스킬 로더 |
| `src/main.rs` | CLI(이벤트 출력 + 승인 게이트) |

## 이식된 기능

- 멀티턴 루프(`stop_reason`: end_turn/tool_use/pause_turn) + `MAX_TURNS` wrapup
- 툴 8종(read/write/edit/bash/grep/glob/web_fetch/web_search)
- 멀티턴 메시지 캐싱(`cache_control`) + system 캐싱 토글
- 병렬 tool_use(`std::thread::scope`) + **의존성 인식 스케줄링**(자원 충돌만 순차)
- 인젝션 방어(`<tool_output>` 래핑) + 승인 게이트(bash)
- 세션 영속 · 컨텍스트 관리(stripping/compaction) · 키워드 스킬 주입
- `end_turn` 텍스트 = 모든 text 블록 이어붙임(서버사이드 검색 다중블록 대응)

## TUI 로 확장하려면

`run_agent(...)` 의 `emit: &Emit` 자리에 **채널 전송 클로저**를 넘기면 된다:

```rust
let (tx, rx) = std::sync::mpsc::channel();
let emit = move |e| { tx.send(e).ok(); };
// 백그라운드 스레드에서 run_agent 실행, ratatui 렌더 루프는 rx 로 이벤트 수신
```

엔진(lib)은 한 줄도 고치지 않는다.

## 미이식 / 한계

- 웹 서버(server.py) — TUI 가 UI 레이어를 대체하므로 제외.
- 토큰 스트리밍(SSE delta) — 현재 비스트리밍. 이벤트 채널은 이미 있으니 추후 추가 용이.
- `run_bash` 타임아웃은 try_wait 폴링 + kill 방식(스레드로 파이프 드레인해 데드락 회피).
- **컴파일 검증 미완** — 작성 환경에 Rust 미설치. 첫 `cargo build` 에서 나는 에러는 함께 수정 필요.
