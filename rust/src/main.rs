//! HWHarness CLI (Rust) — lib 엔진을 쓰는 얇은 프런트엔드.
//! 나중에 TUI 는 이 자리에 ratatui 렌더 루프를 놓고, 같은 run_agent 를 채널 emit 으로 호출하면 된다.

use anyhow::Result;
use hwharness::client::Client;
use hwharness::config::Config;
use hwharness::{agent, skills, AgentEvent, Approve, Emit};
use rustyline::error::ReadlineError;
use rustyline::DefaultEditor;
use serde_json::Value;
use std::io::{self, Write};

const SESSION_ID: &str = "cli";
const SESSION_DIR: &str = "sessions";
const SKILLS_DIR: &str = "skills";

fn truncate(s: &str, n: usize) -> String {
    let one_line = s.replace('\n', " ");
    if one_line.chars().count() > n {
        one_line.chars().take(n).collect::<String>() + " …"
    } else {
        one_line
    }
}

fn main() -> Result<()> {
    let cfg = Config::from_env()?;
    let client = Client::new(cfg)?;

    let environment = format!(
        "작업 디렉토리: {}\nOS: {}\n사용 가능한 툴: read_file, write_file, edit_file, bash, grep, glob, web_fetch, web_search",
        std::env::current_dir().unwrap_or_default().display(),
        std::env::consts::OS,
    );
    let progress = hwharness::session::read_progress(SESSION_DIR, SESSION_ID);
    let base_system = skills::build_system_prompt(
        skills::DEFAULT_ROLE,
        &environment,
        &progress,
        skills::DEFAULT_RULES,
        skills::DEFAULT_OUTPUT_FORMAT,
        "",
    );

    let mut messages: Vec<Value> = hwharness::session::load(SESSION_DIR, SESSION_ID);

    // 이벤트 싱크 (TUI 에선 이 클로저가 채널 전송이 된다)
    let emit_closure = |e: AgentEvent| match e {
        // 스트리밍 토큰 — 그대로 inline 출력 (TUI 에선 채널로 보내 렌더)
        AgentEvent::TextDelta(s) => {
            print!("{s}");
            io::stdout().flush().ok();
        }
        AgentEvent::Text(_) => println!(), // 스트리밍 끝 → 줄바꿈
        // 아래 이벤트들은 스트리밍 텍스트와 섞이지 않게 새 줄에서 출력
        AgentEvent::Tool { name, input, ok, output } => {
            println!("\n[tool:{}] {}({}) -> {}", if ok { "ok" } else { "실패" }, name, truncate(&input, 80), truncate(&output, 200));
        }
        AgentEvent::Schedule { total, stages } => {
            let shape = stages.iter().map(|n| n.to_string()).collect::<Vec<_>>().join("+");
            println!("\n[schedule] tool_use {total}개 → {}단계 ({shape})", stages.len());
        }
        AgentEvent::ServerTool(s) => println!("\n[server_tool] {s}"),
        AgentEvent::ContextManaged(n) => println!("\n[context] 관리 수행 (메시지 {n}개)"),
        AgentEvent::Notice(s) => println!("\n[notice] {s}"),
    };

    // 승인 게이트 (bash) — 메인 스레드 stdin
    let approve_closure = |name: &str, input: &Value| -> bool {
        println!("\n[승인 요청] 툴 '{name}' 실행:\n  {input}");
        print!("  실행할까요? [y/N] ");
        io::stdout().flush().ok();
        let mut line = String::new();
        io::stdin().read_line(&mut line).ok();
        matches!(line.trim().to_lowercase().as_str(), "y" | "yes")
    };
    let emit: &Emit = &emit_closure;
    let approve: &Approve = &approve_closure;

    println!("============================================================");
    println!("  HWHarness (Rust) — 대화형");
    println!("  - 툴: read_file / write_file / edit_file / bash / grep / glob / web_fetch / web_search");
    println!("  - '/exit' 종료, '/skills' 스킬 목록");
    println!("============================================================");

    // rustyline: 멀티바이트(한글) 백스페이스·커서 이동·히스토리를 올바로 처리한다.
    let mut rl = DefaultEditor::new()?;
    loop {
        let line = match rl.readline("\n나 > ") {
            Ok(l) => l,
            Err(ReadlineError::Interrupted) => continue, // Ctrl-C → 입력 취소
            Err(ReadlineError::Eof) => break,            // Ctrl-D → 종료
            Err(e) => {
                eprintln!("입력 오류: {e}");
                break;
            }
        };
        let task = line.trim();
        if task.is_empty() {
            continue;
        }
        rl.add_history_entry(task).ok();
        if task == "/exit" {
            break;
        }
        if task == "/skills" {
            println!("스킬: {}", skills::list_skills(SKILLS_DIR).join(", "));
            continue;
        }

        let skills_text = skills::load_relevant_skills(task, SKILLS_DIR);
        let system = if skills_text.is_empty() {
            base_system.clone()
        } else {
            format!("{base_system}\n\n[SKILLS]\n{skills_text}")
        };

        print!("\nAI > ");
        io::stdout().flush().ok();
        match agent::run_agent(&client, task, &mut messages, Some(system.as_str()), Some(approve), emit) {
            Ok(_) => {} // 스트리밍(TextDelta)으로 이미 출력됨
            Err(e) => println!("\n[오류] {e}"),
        }
        hwharness::session::save(SESSION_DIR, SESSION_ID, &messages).ok();
        hwharness::session::append_progress(SESSION_DIR, SESSION_ID, task);
    }
    Ok(())
}
