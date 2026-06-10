//! HWHarness TUI (ratatui) — lib 엔진을 백그라운드 스레드에서 돌리고, 이벤트를 채널로 받아 렌더.
//!
//! 구조: [엔진 스레드] client+messages 소유, task 채널 수신 → run_agent → UiMsg 채널 송신.
//!       [UI 스레드]   ratatui 렌더 + 키 입력, task 송신 / UiMsg 수신(스트리밍·툴·승인).

use hwharness::client::Client;
use hwharness::config::Config;
use hwharness::{agent, session, skills, AgentEvent, MODEL};

use ratatui::crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Clear, Paragraph, Wrap};
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

use anyhow::Result;
use serde_json::Value;
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::Duration;

/// 세션 ID 결정: HWHARNESS_SESSION 이 있으면 그 세션 이어받기, 없으면 켤 때마다 새 세션.
fn session_id() -> String {
    std::env::var("HWHARNESS_SESSION").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        format!("tui-{ts}-{}", std::process::id()) // 같은 초에 여러 번 켜도 안 겹치게 pid 추가
    })
}

/// 엔진 → UI 메시지.
enum UiMsg {
    Event(AgentEvent),
    /// bash 승인 요청 — UI 가 y/n 받아 reply 로 회신.
    Approval { prompt: String, reply: Sender<bool> },
    /// 한 작업(턴) 종료.
    TurnDone,
}

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    User,
    Assistant,
    Tool,
    Event,
}

struct Entry {
    kind: Kind,
    text: String,
}

struct App {
    transcript: Vec<Entry>,
    input: String,
    working: bool,
    scroll_back: u16,                       // 위로 스크롤한 정도(0=맨 아래 따라감)
    open_assistant: Option<usize>,          // 스트리밍 중인 assistant entry 인덱스
    modal: Option<(String, Sender<bool>)>,  // 승인 대기
    should_quit: bool,
}

impl App {
    fn new() -> App {
        App {
            transcript: vec![Entry {
                kind: Kind::Event,
                text: "HWHarness TUI — 입력 후 Enter, Esc 종료, PgUp/PgDn 스크롤. bash 는 승인(y/n).".into(),
            }],
            input: String::new(),
            working: false,
            scroll_back: 0,
            open_assistant: None,
            modal: None,
            should_quit: false,
        }
    }

    fn push(&mut self, kind: Kind, text: String) {
        self.transcript.push(Entry { kind, text });
    }

    fn apply(&mut self, msg: UiMsg) {
        match msg {
            UiMsg::Event(AgentEvent::TextDelta(s)) => {
                // 스트리밍 토큰 → 열린 assistant 말풍선에 append (없으면 새로 연다)
                match self.open_assistant {
                    Some(i) => self.transcript[i].text.push_str(&s),
                    None => {
                        self.transcript.push(Entry { kind: Kind::Assistant, text: s });
                        self.open_assistant = Some(self.transcript.len() - 1);
                    }
                }
            }
            UiMsg::Event(AgentEvent::Text(_)) => {
                self.open_assistant = None; // 말풍선 마감
            }
            UiMsg::Event(AgentEvent::Tool { name, input, ok, output }) => {
                self.open_assistant = None;
                let mark = if ok { "ok" } else { "실패" };
                self.push(Kind::Tool, format!("[{mark}] {name}({}) → {}", trunc(&input, 60), trunc(&output, 160)));
            }
            UiMsg::Event(AgentEvent::Schedule { total, stages }) => {
                self.open_assistant = None;
                let shape = stages.iter().map(|n| n.to_string()).collect::<Vec<_>>().join("+");
                self.push(Kind::Event, format!("schedule: tool_use {total}개 → {}단계 ({shape})", stages.len()));
            }
            UiMsg::Event(AgentEvent::ServerTool(s)) => {
                self.open_assistant = None;
                self.push(Kind::Event, format!("server_tool: {s}"));
            }
            UiMsg::Event(AgentEvent::ContextManaged(n)) => {
                self.push(Kind::Event, format!("context: 관리 수행 (메시지 {n}개)"));
            }
            UiMsg::Event(AgentEvent::Notice(s)) => {
                self.open_assistant = None;
                self.push(Kind::Event, s);
            }
            UiMsg::Approval { prompt, reply } => {
                self.modal = Some((prompt, reply));
            }
            UiMsg::TurnDone => {
                self.working = false;
                self.open_assistant = None;
            }
        }
        self.scroll_back = 0; // 새 내용 → 맨 아래로 따라감
    }

    /// 트랜스크립트를 스타일된 줄로.
    fn lines(&self) -> Vec<Line<'static>> {
        let mut out = Vec::new();
        for e in &self.transcript {
            let (label, style) = match e.kind {
                Kind::User => ("나 ▶ ", Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                Kind::Assistant => ("AI ◀ ", Style::new().fg(Color::Green)),
                Kind::Tool => ("  ⚙ ", Style::new().fg(Color::Magenta)),
                Kind::Event => ("  · ", Style::new().fg(Color::DarkGray)),
            };
            for (i, raw) in e.text.split('\n').enumerate() {
                let prefix = if i == 0 { label } else { "    " };
                out.push(Line::from(vec![
                    Span::styled(prefix.to_string(), style),
                    Span::styled(raw.to_string(), style),
                ]));
            }
        }
        out
    }
}

fn trunc(s: &str, n: usize) -> String {
    let one = s.replace('\n', " ");
    if one.chars().count() > n {
        one.chars().take(n).collect::<String>() + "…"
    } else {
        one
    }
}

fn centered_rect(width: u16, height: u16, area: Rect) -> Rect {
    let x = area.x + (area.width.saturating_sub(width)) / 2;
    let y = area.y + (area.height.saturating_sub(height)) / 2;
    Rect { x, y, width: width.min(area.width), height: height.min(area.height) }
}

fn ui(f: &mut Frame, app: &App) {
    let chunks = Layout::vertical([Constraint::Min(1), Constraint::Length(3), Constraint::Length(1)]).split(f.area());

    // 트랜스크립트 (자동 하단 추적 + 스크롤)
    let lines = app.lines();
    let inner_w = chunks[0].width.saturating_sub(2); // 테두리 제외 폭
    let inner_h = chunks[0].height.saturating_sub(2); // 테두리 제외 높이
    let p = Paragraph::new(Text::from(lines))
        .block(Block::bordered().title(" HWHarness (Rust TUI) — ↑/↓·PgUp/PgDn 스크롤 "))
        .wrap(Wrap { trim: false });
    // 줄바꿈(wrap)까지 반영한 실제 렌더 줄 수로 스크롤을 계산해야 끝까지·옛 대화까지 보인다.
    let total = p.line_count(inner_w) as u16;
    let max_scroll = total.saturating_sub(inner_h);
    let scroll = max_scroll.saturating_sub(app.scroll_back.min(max_scroll)); // 0=맨아래, 위로 갈수록 옛 대화
    f.render_widget(p.scroll((scroll, 0)), chunks[0]);

    // 입력
    let title = if app.working { " 입력 (작업 중…) " } else { " 입력 (Enter 전송 · Esc 종료) " };
    let input = Paragraph::new(app.input.as_str()).block(Block::bordered().title(title));
    f.render_widget(input, chunks[1]);
    // 입력 커서 — 표시 폭(한글=2칸) 기준이라야 IME 조합 글자가 어긋나지 않는다.
    let cx = chunks[1].x + 1 + app.input.width() as u16;
    f.set_cursor_position((cx.min(chunks[1].x + chunks[1].width.saturating_sub(1)), chunks[1].y + 1));

    // 상태줄
    let status = format!(" 모델 {MODEL} · {} ", if app.working { "● 작업중" } else { "○ 대기" });
    f.render_widget(Paragraph::new(status).style(Style::new().fg(Color::DarkGray)), chunks[2]);

    // 승인 모달
    if let Some((prompt, _)) = &app.modal {
        let area = centered_rect(70, 8, f.area());
        f.render_widget(Clear, area);
        let p = Paragraph::new(format!("bash 실행을 승인할까요?\n\n{}\n\n[y] 허용    [n] 거부", trunc(prompt, 200)))
            .block(Block::bordered().title(" 승인 요청 ").border_style(Style::new().fg(Color::Yellow)))
            .wrap(Wrap { trim: false });
        f.render_widget(p, area);
    }
}

fn handle_key(app: &mut App, code: KeyCode, mods: KeyModifiers, task_tx: &Sender<String>) {
    // 모달이 떠 있으면 y/n 만 처리 (다른 키는 모달 유지)
    if app.modal.is_some() {
        let decision = match code {
            KeyCode::Char('y') | KeyCode::Char('Y') => Some(true),
            KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => Some(false),
            _ => None,
        };
        if let Some(ok) = decision {
            if let Some((_, reply)) = app.modal.take() {
                let _ = reply.send(ok);
            }
        }
        return;
    }

    match code {
        KeyCode::Esc => app.should_quit = true,
        KeyCode::Char('c') if mods.contains(KeyModifiers::CONTROL) => app.should_quit = true,
        KeyCode::Enter => {
            let task = app.input.trim().to_string();
            app.input.clear();
            if task.is_empty() {
                return;
            }
            if task == "/exit" {
                app.should_quit = true;
                return;
            }
            app.push(Kind::User, task.clone());
            app.working = true;
            app.scroll_back = 0;
            let _ = task_tx.send(task);
        }
        KeyCode::Backspace => {
            app.input.pop(); // char 단위 — 한글도 정상
        }
        KeyCode::Char(c) => app.input.push(c),
        KeyCode::Up => app.scroll_back = app.scroll_back.saturating_add(1),
        KeyCode::Down => app.scroll_back = app.scroll_back.saturating_sub(1),
        KeyCode::PageUp => app.scroll_back = app.scroll_back.saturating_add(10),
        KeyCode::PageDown => app.scroll_back = app.scroll_back.saturating_sub(10),
        _ => {}
    }
}

fn main() -> Result<()> {
    let cfg = Config::from_env()?;
    let client = Client::new(cfg)?;

    // 세션/스킬 디렉토리는 시작 시점 cwd 기준 절대경로로 고정 → change_dir 후에도 안 깨짐.
    let home = std::env::current_dir().unwrap_or_default();
    let session_dir = home.join("sessions").to_string_lossy().into_owned();
    let skills_dir = home.join("skills").to_string_lossy().into_owned();

    let environment = format!(
        "작업 디렉토리: {}\nOS: {}\n사용 가능한 툴: read_file, write_file, edit_file, bash, grep, glob, web_fetch, web_search, change_dir",
        home.display(),
        std::env::consts::OS,
    );
    let sid = session_id(); // 새 세션(기본) 또는 HWHARNESS_SESSION 이어받기
    let progress = session::read_progress(&session_dir, &sid);
    let base_system = skills::build_system_prompt(
        skills::DEFAULT_ROLE,
        &environment,
        &progress,
        skills::DEFAULT_RULES,
        skills::DEFAULT_OUTPUT_FORMAT,
        "",
    );
    let mut messages: Vec<Value> = session::load(&session_dir, &sid);
    let prior_len = messages.len(); // messages 는 곧 엔진 스레드로 move 되므로 미리 길이 확보
    let sid_engine = sid.clone(); // 엔진 스레드로 이동시킬 복사본

    let (task_tx, task_rx) = mpsc::channel::<String>();
    let (ui_tx, ui_rx) = mpsc::channel::<UiMsg>();

    // ── 엔진 스레드: client+messages 소유 ──────────────────────────────────
    let ui_tx_emit = ui_tx.clone();
    let ui_tx_appr = ui_tx.clone();
    let engine = thread::spawn(move || {
        let emit = |e: AgentEvent| {
            let _ = ui_tx_emit.send(UiMsg::Event(e));
        };
        let approve = |name: &str, input: &Value| -> bool {
            let (rtx, rrx) = mpsc::channel();
            let _ = ui_tx_appr.send(UiMsg::Approval { prompt: format!("{name}({input})"), reply: rtx });
            rrx.recv().unwrap_or(false) // UI 종료로 sender drop 시 false
        };
        for task in task_rx {
            let skills_text = skills::load_relevant_skills(&task, &skills_dir);
            let sys = if skills_text.is_empty() {
                base_system.clone()
            } else {
                format!("{base_system}\n\n[SKILLS]\n{skills_text}")
            };
            if let Err(e) = agent::run_agent(&client, &task, &mut messages, Some(sys.as_str()), Some(&approve), &emit) {
                let _ = ui_tx.send(UiMsg::Event(AgentEvent::Notice(format!("오류: {e}"))));
            }
            session::save(&session_dir, &sid_engine, &messages).ok();
            session::append_progress(&session_dir, &sid_engine, &task);
            let _ = ui_tx.send(UiMsg::TurnDone);
        }
    });

    // ── UI 스레드: ratatui 렌더 루프 ───────────────────────────────────────
    let mut terminal = ratatui::init();
    let mut app = App::new();
    app.push(
        Kind::Event,
        if prior_len == 0 {
            format!("세션: {sid} (새 대화) · 이어받으려면 HWHARNESS_SESSION={sid} 로 실행")
        } else {
            format!("세션: {sid} (이전 대화 {prior_len} 메시지 이어받음)")
        },
    );
    let res = run_ui(&mut terminal, &mut app, &task_tx, &ui_rx);
    ratatui::restore();

    drop(task_tx); // 엔진 루프 종료 유도
    let _ = engine.join();
    res
}

fn run_ui(
    terminal: &mut ratatui::DefaultTerminal,
    app: &mut App,
    task_tx: &Sender<String>,
    ui_rx: &Receiver<UiMsg>,
) -> Result<()> {
    loop {
        terminal.draw(|f| ui(f, app))?;

        // 키 입력 (타임아웃으로 폴링해 엔진 메시지도 주기적으로 처리)
        if event::poll(Duration::from_millis(50))? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press {
                    handle_key(app, key.code, key.modifiers, task_tx);
                }
            }
        }
        // 엔진 메시지 드레인
        while let Ok(msg) = ui_rx.try_recv() {
            app.apply(msg);
        }
        if app.should_quit {
            return Ok(());
        }
    }
}
