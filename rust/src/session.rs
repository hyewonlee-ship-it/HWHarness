//! 세션 영속 — sessions/<id>.json (히스토리) + sessions/<id>.progress.txt.

use anyhow::Result;
use serde_json::Value;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

fn file(dir: &str, id: &str, ext: &str) -> PathBuf {
    PathBuf::from(dir).join(format!("{id}.{ext}"))
}

/// 세션 히스토리 로드 (없으면 빈 벡터).
pub fn load(dir: &str, id: &str) -> Vec<Value> {
    std::fs::read_to_string(file(dir, id, "json"))
        .ok()
        .and_then(|s| serde_json::from_str::<Vec<Value>>(&s).ok())
        .unwrap_or_default()
}

/// 세션 히스토리 저장.
pub fn save(dir: &str, id: &str, messages: &[Value]) -> Result<()> {
    std::fs::create_dir_all(dir)?;
    std::fs::write(file(dir, id, "json"), serde_json::to_string_pretty(messages)?)?;
    Ok(())
}

/// progress 한 줄 추가 (타임스탬프 epoch 초 + 작업 요약).
pub fn append_progress(dir: &str, id: &str, task: &str) {
    let _ = std::fs::create_dir_all(dir);
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let line = format!("[{ts}] {task}\n");
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(file(dir, id, "progress.txt")) {
        let _ = f.write_all(line.as_bytes());
    }
}

/// 이전 세션 progress 읽기 (시스템 프롬프트의 TASK CONTEXT 주입용).
pub fn read_progress(dir: &str, id: &str) -> String {
    std::fs::read_to_string(file(dir, id, "progress.txt")).unwrap_or_default()
}
