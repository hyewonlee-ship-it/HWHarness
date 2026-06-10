//! Anthropic Messages API 클라이언트 (회사 프록시 pass-through, 원시 HTTP).
//!
//! Rust 공식 SDK 가 없어 reqwest 로 직접 호출한다. content 블록은 종류가 다양하고
//! 그대로 히스토리에 재전송해야 하므로 `serde_json::Value` 로 다룬다(충실한 round-trip).

use crate::config::Config;
use crate::{CACHE_MESSAGES, CACHE_PROMPT, MAX_TOKENS, MODEL};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};

pub struct Client {
    cfg: Config,
    http: reqwest::blocking::Client,
}

/// 모델 응답에서 우리가 쓰는 것만 추린 형태.
pub struct ApiResponse {
    pub content: Vec<Value>, // 텍스트 + tool_use + server_tool_use 등 블록 전체 (그대로 재전송)
    pub stop_reason: String,
}

impl Client {
    pub fn new(cfg: Config) -> Result<Client> {
        let http = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .build()?;
        Ok(Client { cfg, http })
    }

    /// 한 번의 모델 호출. tools 는 JSON 배열, messages 는 메시지 객체 배열.
    pub fn call(
        &self,
        system: Option<&str>,
        tools: &Value,
        messages: &[Value],
        tool_choice: Option<&Value>,
    ) -> Result<ApiResponse> {
        let mut body = json!({
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            // 멀티턴 캐싱: 마지막 메시지에 브레이크포인트를 단 복사본을 전송 (원본 불변)
            "messages": cache_messages(messages),
        });
        // 빈 tools 배열은 API 가 거부할 수 있어 생략 (final_wrapup 의 "툴 없이" 호출 대비)
        if tools.as_array().map(|a| !a.is_empty()).unwrap_or(false) {
            body["tools"] = tools.clone();
        }
        if let Some(sys) = system {
            body["system"] = if CACHE_PROMPT {
                json!([{ "type": "text", "text": sys, "cache_control": {"type": "ephemeral"} }])
            } else {
                json!(sys)
            };
        }
        if let Some(tc) = tool_choice {
            body["tool_choice"] = tc.clone();
        }
        self.send(&body)
    }

    /// 스트리밍 호출. text_delta 마다 `on_text` 를 부르고, 최종 메시지(content+stop_reason)를 조립해 반환.
    /// (Anthropic SSE 의 content_block_* / message_delta 이벤트를 누적해 비스트리밍과 같은 형태로 만든다.)
    pub fn call_stream(
        &self,
        system: Option<&str>,
        tools: &Value,
        messages: &[Value],
        tool_choice: Option<&Value>,
        on_text: &dyn Fn(&str),
    ) -> Result<ApiResponse> {
        let mut body = json!({
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": cache_messages(messages),
            "stream": true,
        });
        if tools.as_array().map(|a| !a.is_empty()).unwrap_or(false) {
            body["tools"] = tools.clone();
        }
        if let Some(sys) = system {
            body["system"] = if CACHE_PROMPT {
                json!([{ "type": "text", "text": sys, "cache_control": {"type": "ephemeral"} }])
            } else {
                json!(sys)
            };
        }
        if let Some(tc) = tool_choice {
            body["tool_choice"] = tc.clone();
        }

        let url = format!("{}/v1/messages", self.cfg.base_url);
        let resp = self
            .http
            .post(&url)
            .header("authorization", format!("Bearer {}", self.cfg.token))
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json")
            .json(&body)
            .send()?;
        let status = resp.status();
        if !status.is_success() {
            let v: Value = resp.json().unwrap_or(Value::Null);
            bail!("API 오류 (HTTP {}): {}", status.as_u16(), v);
        }

        let mut blocks: Vec<Value> = Vec::new();
        let mut json_acc: Vec<String> = Vec::new(); // tool_use input 의 partial_json 누적
        let mut stop_reason = String::new();
        for line in BufReader::new(resp).lines() {
            let line = line?;
            let line = line.trim_start();
            let Some(data) = line.strip_prefix("data:") else { continue };
            let data = data.trim();
            if data.is_empty() || data == "[DONE]" {
                continue;
            }
            let Ok(ev) = serde_json::from_str::<Value>(data) else { continue };
            match ev["type"].as_str().unwrap_or("") {
                "content_block_start" => {
                    let idx = ev["index"].as_u64().unwrap_or(0) as usize;
                    ensure_len(&mut blocks, idx + 1, Value::Null);
                    ensure_len(&mut json_acc, idx + 1, String::new());
                    blocks[idx] = ev["content_block"].clone();
                }
                "content_block_delta" => {
                    let idx = ev["index"].as_u64().unwrap_or(0) as usize;
                    let d = &ev["delta"];
                    match d["type"].as_str().unwrap_or("") {
                        "text_delta" => {
                            let t = d["text"].as_str().unwrap_or("");
                            on_text(t);
                            if idx < blocks.len() {
                                let cur = blocks[idx]["text"].as_str().unwrap_or("").to_string();
                                blocks[idx]["text"] = json!(format!("{cur}{t}"));
                            }
                        }
                        "input_json_delta" => {
                            if idx < json_acc.len() {
                                json_acc[idx].push_str(d["partial_json"].as_str().unwrap_or(""));
                            }
                        }
                        _ => {}
                    }
                }
                "content_block_stop" => {
                    let idx = ev["index"].as_u64().unwrap_or(0) as usize;
                    if idx < blocks.len() && idx < json_acc.len() && !json_acc[idx].is_empty() {
                        if let Ok(parsed) = serde_json::from_str::<Value>(&json_acc[idx]) {
                            blocks[idx]["input"] = parsed;
                        }
                    }
                }
                "message_delta" => {
                    if let Some(sr) = ev["delta"]["stop_reason"].as_str() {
                        if !sr.is_empty() {
                            stop_reason = sr.to_string();
                        }
                    }
                }
                _ => {}
            }
        }
        Ok(ApiResponse {
            content: blocks.into_iter().filter(|b| !b.is_null()).collect(),
            stop_reason,
        })
    }

    /// tools 없는 단발 호출 (컨텍스트 요약 등).
    pub fn complete_text(&self, prompt: &str) -> Result<String> {
        let body = json!({
            "model": MODEL,
            "max_tokens": 2048,
            "messages": [{ "role": "user", "content": prompt }],
        });
        let resp = self.send(&body)?;
        Ok(join_text(&resp.content))
    }

    fn send(&self, body: &Value) -> Result<ApiResponse> {
        let url = format!("{}/v1/messages", self.cfg.base_url);
        let resp = self
            .http
            .post(&url)
            .header("authorization", format!("Bearer {}", self.cfg.token))
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json")
            .json(body)
            .send()?;
        let status = resp.status();
        let v: Value = resp.json()?;
        if !status.is_success() {
            bail!("API 오류 (HTTP {}): {}", status.as_u16(), v);
        }
        Ok(ApiResponse {
            content: v["content"].as_array().cloned().unwrap_or_default(),
            stop_reason: v["stop_reason"].as_str().unwrap_or("").to_string(),
        })
    }
}

fn ensure_len<T: Clone>(v: &mut Vec<T>, n: usize, fill: T) {
    while v.len() < n {
        v.push(fill.clone());
    }
}

/// 마지막 메시지의 마지막 블록에 `cache_control` 을 달아 누적 대화 프리픽스를 캐시한다(멀티턴).
/// 원본은 건드리지 않고 요청용 복사본만 마킹한다 (마커 누적/직렬화 오염 방지).
pub fn cache_messages(messages: &[Value]) -> Vec<Value> {
    let mut out: Vec<Value> = messages.to_vec();
    if !CACHE_MESSAGES || out.is_empty() {
        return out;
    }
    let idx = out.len() - 1;
    let content = out[idx].get("content").cloned().unwrap_or(Value::Null);
    let mark = json!({ "type": "ephemeral" });
    if let Some(s) = content.as_str() {
        out[idx]["content"] = json!([{ "type": "text", "text": s, "cache_control": mark }]);
    } else if let Some(arr) = content.as_array() {
        if !arr.is_empty() && arr.last().map(|b| b.is_object()).unwrap_or(false) {
            let mut arr = arr.clone();
            let last = arr.len() - 1;
            arr[last]["cache_control"] = mark;
            out[idx]["content"] = Value::Array(arr);
        }
    }
    out
}

/// 응답 content 의 모든 text 블록을 이어붙인다. 서버사이드 검색은 텍스트가 프리앰블+답변
/// 여러 블록으로 쪼개져 오므로 첫 블록만 집으면 답을 놓친다.
pub fn join_text(content: &[Value]) -> String {
    content
        .iter()
        .filter(|b| b["type"] == "text")
        .filter_map(|b| b["text"].as_str())
        .collect::<Vec<_>>()
        .join("")
}
