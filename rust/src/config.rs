//! 회사 프록시 pass-through 설정 — 환경변수/.env 에서 주입 (코드 하드코딩 금지).

use anyhow::{bail, Result};
use std::fs;

#[derive(Debug, Clone)]
pub struct Config {
    /// `https://<company-proxy>/anthropic` — SDK 가 붙이듯 우리는 `/v1/messages` 를 덧붙여 호출한다.
    pub base_url: String,
    /// 프록시 토큰(`aiproxy_...`) — `Authorization: Bearer` 로 전송.
    pub token: String,
}

impl Config {
    /// `.env`(있으면) 로드 후 환경변수에서 구성. 셸 export 가 .env 보다 우선.
    pub fn from_env() -> Result<Config> {
        load_dotenv(".env");
        let base_url = std::env::var("ANTHROPIC_BASE_URL").unwrap_or_default();
        let token = std::env::var("ANTHROPIC_AUTH_TOKEN").unwrap_or_default();
        if base_url.is_empty() || token.is_empty() {
            bail!(
                "회사 프록시 연동에는 다음 환경변수가 필요합니다:\n  \
                 export ANTHROPIC_BASE_URL=\"https://<회사-프록시>/anthropic\"\n  \
                 export ANTHROPIC_AUTH_TOKEN=\"<프록시 토큰>\""
            );
        }
        Ok(Config { base_url, token })
    }
}

/// 의존성 없이 .env 를 읽어 환경변수로 로드한다 (이미 설정된 값은 유지).
fn load_dotenv(path: &str) {
    let Ok(text) = fs::read_to_string(path) else { return };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else { continue };
        let k = k.trim();
        let v = v.trim().trim_matches('"').trim_matches('\'');
        if std::env::var(k).is_err() {
            std::env::set_var(k, v);
        }
    }
}
