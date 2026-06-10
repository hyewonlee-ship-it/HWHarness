//! 클라이언트사이드 컨텍스트 관리 — 70% 초과 시 Stripping → Compaction(요약).
//! 안전 경계: 압축 후 꼬리는 "문자열 user 턴"에서 시작해 고아 tool_result(400)를 막는다.

use crate::client::Client;
use serde_json::{json, Value};

const WINDOW: usize = 200_000;
const THRESHOLD_RATIO: f64 = 0.70;

pub fn estimate_tokens(messages: &[Value]) -> usize {
    messages.iter().map(|m| m.to_string().chars().count()).sum::<usize>() / 4
}

fn should_compact(messages: &[Value]) -> bool {
    estimate_tokens(messages) > (WINDOW as f64 * THRESHOLD_RATIO) as usize
}

/// 매 모델 호출 전 호출. 관리 수행 시 Some(현재 메시지 수) 반환.
pub fn manage(messages: &mut Vec<Value>, client: &Client) -> Option<usize> {
    if !should_compact(messages) {
        return None;
    }
    strip_old_tool_results(messages);
    if should_compact(messages) {
        compact(messages, client);
    }
    Some(messages.len())
}

/// 오래된 tool_result 의 내용만 비우고 블록·tool_use_id 는 보존(페어링 유지).
fn strip_old_tool_results(messages: &mut [Value]) {
    let n = messages.len();
    let keep_tail = 6;
    for i in 0..n {
        if i + keep_tail >= n {
            continue;
        }
        let content = messages[i].get("content").cloned();
        if let Some(Value::Array(arr)) = content {
            if arr.iter().any(|b| b["type"] == "tool_result") {
                let stripped: Vec<Value> = arr
                    .iter()
                    .map(|b| {
                        if b["type"] == "tool_result" {
                            json!({
                                "type": "tool_result",
                                "tool_use_id": b["tool_use_id"].clone(),
                                "content": "[이전 결과 생략됨]"
                            })
                        } else {
                            b.clone()
                        }
                    })
                    .collect();
                messages[i]["content"] = Value::Array(stripped);
            }
        }
    }
}

/// from 이후 첫 "user + 문자열 content"(클린 헤드) 인덱스.
fn clean_head_index(messages: &[Value], from: usize) -> Option<usize> {
    (from..messages.len()).find(|&i| messages[i]["role"] == "user" && messages[i]["content"].is_string())
}

fn compact(messages: &mut Vec<Value>, client: &Client) {
    let n = messages.len();
    if n < 4 {
        return;
    }
    let split = n / 2;
    let tail_start = clean_head_index(messages, split).unwrap_or_else(|| n.saturating_sub(2));
    let older_text = messages[..tail_start]
        .iter()
        .map(|m| m.to_string())
        .collect::<Vec<_>>()
        .join("\n");
    let prompt = format!(
        "다음 대화를 압축 요약하세요.\n보존: 아키텍처/설계 결정, 미완성 작업, 에러 상태, 중요한 파일 경로.\n\
         단, 사용자가 명시한 보안·금지 제약은 압축 후에도 적용되도록 반드시 그대로(verbatim) 보존하라.\n\n{older_text}"
    );
    let summary = client.complete_text(&prompt).unwrap_or_else(|_| "[요약 실패]".to_string());
    let mut newmsgs = vec![json!({ "role": "user", "content": format!("[이전 대화 요약]\n{summary}") })];
    newmsgs.extend(messages[tail_start..].iter().cloned());
    *messages = newmsgs;
}
