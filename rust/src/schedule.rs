//! 의존성 인식 스케줄링 — tool_use 간 자원 충돌(데이터 해저드)을 감지해
//! 충돌하는 호출만 순차(stage 분리), 독립 호출은 병렬로 묶는다.

use serde_json::Value;
use std::path::Path;

/// 툴이 건드리는 자원 = (경로 또는 특수키, 모드). 모드 'r'(읽기)/'w'(쓰기).
/// bash 는 불투명해 "\0bash" 전역 장벽, web_* 는 로컬 자원 없음(None).
fn tool_resource(block: &Value) -> (Option<String>, char) {
    let name = block["name"].as_str().unwrap_or("");
    let input = &block["input"];
    let path = |k: &str| input.get(k).and_then(|v| v.as_str()).unwrap_or("");
    match name {
        "write_file" | "edit_file" => (Some(abspath(path("path"))), 'w'),
        "read_file" => (Some(abspath(path("path"))), 'r'),
        "grep" | "glob" => (Some(abspath(if path("path").is_empty() { "." } else { path("path") })), 'r'),
        "bash" => (Some("\0bash".to_string()), 'w'),
        _ => (None, 'r'), // web_search / web_fetch
    }
}

fn abspath(p: &str) -> String {
    let path = Path::new(p);
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    abs.to_string_lossy().trim_end_matches('/').to_string()
}

/// 두 경로가 같거나 한쪽이 다른 쪽의 상위 디렉토리이면 True (서브트리 겹침).
fn paths_overlap(a: &str, b: &str) -> bool {
    if a == "\0bash" || b == "\0bash" {
        return a == b;
    }
    a == b
        || b.starts_with(&format!("{a}/"))
        || a.starts_with(&format!("{b}/"))
}

/// 두 tool_use 가 자원 충돌(순서가 의미 있는 관계)인가.
pub fn tools_conflict(a: &Value, b: &Value) -> bool {
    let (pa, ma) = tool_resource(a);
    let (pb, mb) = tool_resource(b);
    let (Some(pa), Some(pb)) = (pa, pb) else {
        return false; // 네트워크 툴: 로컬 충돌 없음
    };
    if pa == "\0bash" || pb == "\0bash" {
        return true; // bash 전역 장벽 (다른 FS 툴/bash 와 순차)
    }
    if ma == 'r' && mb == 'r' {
        return false; // 읽기-읽기 안전
    }
    paths_overlap(&pa, &pb) // 하나라도 쓰기 + 경로 겹침 → 충돌
}

/// 원래 순서를 보존하며 충돌하지 않는 호출끼리 같은 stage 로 묶는다.
/// 반환: stage 리스트, 각 stage 는 blocks 인덱스 벡터. 같은 stage 는 병렬, stage 간 순차.
pub fn schedule_stages(blocks: &[Value]) -> Vec<Vec<usize>> {
    let mut stage_of = vec![0usize; blocks.len()];
    for i in 0..blocks.len() {
        let mut s = 0;
        for j in 0..i {
            if tools_conflict(&blocks[j], &blocks[i]) {
                s = s.max(stage_of[j] + 1);
            }
        }
        stage_of[i] = s;
    }
    let max_stage = stage_of.iter().copied().max().unwrap_or(0);
    (0..=max_stage)
        .map(|k| (0..blocks.len()).filter(|&i| stage_of[i] == k).collect())
        .collect()
}
