//! HWHarness 엔진 (Rust 포팅).
//!
//! Python 하네스(agent.py 등)의 코어를 라이브러리로 옮긴 것. 프런트엔드(CLI/TUI)는
//! 이 lib 를 쓰고, 상태 변화는 `AgentEvent` 를 받는 `Emit` 클로저로 전달받는다.
//! (CLI 는 바로 출력, TUI 는 채널로 감싸 렌더 루프에 넘기면 된다.)

pub mod config;
pub mod client;
pub mod tools;
pub mod schedule;
pub mod agent;
pub mod context;
pub mod session;
pub mod skills;

use serde_json::Value;

// ── 상수 (Python 의 MODEL/MAX_TOKENS 등에 대응) ──────────────────────────────
pub const DEFAULT_MODEL: &str = "claude-haiku-4-5";

/// 시작 시 모델: HWHARNESS_MODEL 환경변수(있으면) 또는 기본 haiku.
pub fn default_model() -> String {
    std::env::var("HWHARNESS_MODEL")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_MODEL.to_string())
}

/// 짧은 별칭(haiku/sonnet/opus)을 정식 모델 ID 로. 그 외는 입력 그대로 사용.
pub fn resolve_model(name: &str) -> String {
    match name.trim() {
        "haiku" => "claude-haiku-4-5".to_string(),
        "sonnet" => "claude-sonnet-4-6".to_string(),
        "opus" => "claude-opus-4-8".to_string(),
        other => other.to_string(),
    }
}
pub const MAX_TOKENS: u32 = 16000;
pub const MAX_TURNS: u32 = 25;
pub const CACHE_PROMPT: bool = true; // system 블록 cache_control (3,913<4,096 이라 현재 no-op)
pub const CACHE_MESSAGES: bool = true; // 마지막 메시지 cache_control (멀티턴 캐싱, 실효 레버)
pub const PARALLEL_TOOLS: bool = true; // 한 응답의 여러 tool_use 병렬 실행
pub const MAX_PARALLEL_TOOLS: usize = 8;
pub const DEPENDENCY_AWARE: bool = true; // 자원 충돌만 순차, 독립은 병렬
pub const WEB_SEARCH_SERVER_SIDE: bool = true;
pub const WEB_SEARCH_MAX_USES: u32 = 5;
pub const WEB_FETCH_MAX_CHARS: usize = 20000;

/// 외부 입력을 반환하는 툴 — 결과를 `<tool_output>` 로 감싼다 (인젝션 방어).
pub const EXTERNAL_TOOLS: &[&str] = &["read_file", "grep", "glob", "bash", "web_search", "web_fetch"];
/// 실행 전 사용자 승인이 필요한 툴.
pub const REQUIRES_APPROVAL: &[&str] = &["bash"];

/// 엔진이 프런트엔드로 흘려보내는 이벤트.
#[derive(Debug, Clone)]
pub enum AgentEvent {
    Notice(String),
    /// 여러 tool_use 의 실행 스케줄 (예: total=3, stages=[2,1] → 2개 병렬 후 1개).
    Schedule { total: usize, stages: Vec<usize> },
    /// 개별 툴 실행 결과.
    Tool { name: String, input: String, ok: bool, output: String },
    /// 서버사이드 툴(web_search) 활동 (클라이언트 미실행, 로깅만).
    ServerTool(String),
    /// 컨텍스트 관리(stripping/compaction) 수행됨 — 현재 메시지 수.
    ContextManaged(usize),
    /// 스트리밍 텍스트 조각(토큰) — TUI/CLI 가 실시간 렌더.
    TextDelta(String),
    /// 최종(또는 마무리) 어시스턴트 텍스트(스트리밍이면 델타 합본).
    Text(String),
}

/// 프런트엔드가 넘기는 이벤트 싱크. CLI=즉시 출력, TUI=채널 전송.
pub type Emit<'a> = dyn Fn(AgentEvent) + 'a;
/// 위험 툴 승인 콜백. true=실행 허용.
pub type Approve<'a> = dyn Fn(&str, &Value) -> bool + 'a;
