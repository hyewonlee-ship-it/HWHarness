//! 에이전트 루프 — stop_reason 분기 + 병렬/의존성 툴 실행 + 멀티턴.

use crate::client::{join_text, Client};
use crate::{
    context, schedule, tools, AgentEvent, Approve, Emit, DEPENDENCY_AWARE, EXTERNAL_TOOLS,
    MAX_PARALLEL_TOOLS, MAX_TURNS, PARALLEL_TOOLS, REQUIRES_APPROVAL,
};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::collections::HashMap;

/// 한 작업을 끝까지 수행한다. `messages` 히스토리를 in-place 로 누적한다.
pub fn run_agent(
    client: &Client,
    user_input: &str,
    messages: &mut Vec<Value>,
    system: Option<&str>,
    approve: Option<&Approve>,
    emit: &Emit,
) -> Result<String> {
    messages.push(json!({ "role": "user", "content": user_input }));
    let tools = tools::tools_spec();
    let mut turns = 0u32;
    loop {
        turns += 1;
        if turns > MAX_TURNS {
            return final_wrapup(client, messages, system, emit);
        }
        if let Some(n) = context::manage(messages, client) {
            emit(AgentEvent::ContextManaged(n));
        }

        // 스트리밍: text_delta 마다 TextDelta 이벤트로 흘려보낸다 (TUI/CLI 실시간 렌더).
        let on_text = |t: &str| emit(AgentEvent::TextDelta(t.to_string()));
        let resp = client.call_stream(system, &tools, messages, None, &on_text)?;
        messages.push(json!({ "role": "assistant", "content": resp.content.clone() }));

        // 서버사이드 툴(web_search) 활동 가시화 — tool_use 로 안 돌아오므로 로깅만.
        for b in &resp.content {
            match b["type"].as_str() {
                Some("server_tool_use") if b["name"] == "web_search" => {
                    emit(AgentEvent::ServerTool(format!("web_search query={}", b["input"]["query"])));
                }
                Some("web_search_tool_result") => {
                    let n = b["content"].as_array().map(|a| a.len()).unwrap_or(0);
                    emit(AgentEvent::ServerTool(format!("web_search 결과 {n}건 (서버사이드)")));
                }
                _ => {}
            }
        }

        match resp.stop_reason.as_str() {
            "end_turn" => {
                let t = join_text(&resp.content);
                emit(AgentEvent::Text(t.clone()));
                return Ok(t);
            }
            "pause_turn" => continue,
            "tool_use" => {
                let blocks: Vec<Value> =
                    resp.content.iter().filter(|b| b["type"] == "tool_use").cloned().collect();
                let (results, stages) = run_tool_batch(&blocks, approve);
                if blocks.len() > 1 {
                    emit(AgentEvent::Schedule {
                        total: blocks.len(),
                        stages: stages.iter().map(|s| s.len()).collect(),
                    });
                }
                let mut tool_results = Vec::new();
                for b in &blocks {
                    let id = b["id"].as_str().unwrap_or("");
                    let name = b["name"].as_str().unwrap_or("");
                    let result = results.get(id).cloned().unwrap_or_default();
                    let untrusted = EXTERNAL_TOOLS.contains(&name);
                    let tr = tools::make_tool_result(id, &result, untrusted);
                    let ok = tr.get("is_error").is_none();
                    emit(AgentEvent::Tool {
                        name: name.to_string(),
                        input: b["input"].to_string(),
                        ok,
                        output: result,
                    });
                    tool_results.push(tr);
                }
                messages.push(json!({ "role": "user", "content": tool_results }));
                continue;
            }
            other => bail!("예상치 못한 stop_reason: {other}"),
        }
    }
}

/// 턴 상한 도달 시 툴 없이 한 번 더 호출해 모델이 마무리하게 한다.
fn final_wrapup(client: &Client, messages: &mut Vec<Value>, system: Option<&str>, emit: &Emit) -> Result<String> {
    messages.push(json!({
        "role": "user",
        "content": "툴 사용 한도에 도달했습니다. 더 이상 툴을 호출하지 말고, 지금까지 확인한 내용만으로 최종 답변을 정리해 주세요."
    }));
    let on_text = |t: &str| emit(AgentEvent::TextDelta(t.to_string()));
    let resp = client.call_stream(system, &json!([]), messages, None, &on_text)?; // 빈 tools = 툴 없이
    messages.push(json!({ "role": "assistant", "content": resp.content.clone() }));
    let t = join_text(&resp.content);
    let t = if t.is_empty() { format!("[최대 턴({MAX_TURNS}) 초과 — 마무리 응답 없음]") } else { t };
    emit(AgentEvent::Text(t.clone()));
    Ok(t)
}

/// 자원 충돌을 감지해 충돌만 순차(stage)·독립은 병렬로 실행 → ({id: result}, stages).
fn run_tool_batch(blocks: &[Value], approve: Option<&Approve>) -> (HashMap<String, String>, Vec<Vec<usize>>) {
    if !(DEPENDENCY_AWARE && PARALLEL_TOOLS) || blocks.len() <= 1 {
        let r = execute_tool_blocks(blocks, approve);
        return (r, vec![(0..blocks.len()).collect()]);
    }
    let stages = schedule::schedule_stages(blocks);
    let mut results = HashMap::new();
    for stage in &stages {
        let subset: Vec<Value> = stage.iter().map(|&i| blocks[i].clone()).collect();
        results.extend(execute_tool_blocks(&subset, approve));
    }
    (results, stages)
}

/// 승인 게이트(메인 스레드 순차) 후, 승인된 툴을 (가능하면) 병렬 실행.
fn execute_tool_blocks(blocks: &[Value], approve: Option<&Approve>) -> HashMap<String, String> {
    let mut results: HashMap<String, String> = HashMap::new();
    let mut runnable: Vec<&Value> = Vec::new();
    for b in blocks {
        let name = b["name"].as_str().unwrap_or("");
        let id = b["id"].as_str().unwrap_or("").to_string();
        if REQUIRES_APPROVAL.contains(&name) {
            if let Some(ap) = approve {
                if !ap(name, &b["input"]) {
                    results.insert(id, "Error: 사용자가 실행을 거부했습니다.".to_string());
                    continue;
                }
            }
        }
        runnable.push(b);
    }

    if PARALLEL_TOOLS && runnable.len() > 1 {
        for chunk in runnable.chunks(MAX_PARALLEL_TOOLS) {
            std::thread::scope(|scope| {
                let handles: Vec<_> = chunk
                    .iter()
                    .map(|b| {
                        let name = b["name"].as_str().unwrap_or("").to_string();
                        let input = b["input"].clone();
                        let id = b["id"].as_str().unwrap_or("").to_string();
                        scope.spawn(move || (id, tools::run_tool_safe(&name, &input)))
                    })
                    .collect();
                for h in handles {
                    if let Ok((id, res)) = h.join() {
                        results.insert(id, res);
                    }
                }
            });
        }
    } else {
        for b in runnable {
            let id = b["id"].as_str().unwrap_or("").to_string();
            let name = b["name"].as_str().unwrap_or("");
            results.insert(id, tools::run_tool_safe(name, &b["input"]));
        }
    }
    results
}
