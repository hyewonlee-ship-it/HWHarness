"""웹 채팅 UI — 브라우저에서 에이전트를 대화형으로 사용한다.

stdlib http.server 만 사용 (추가 의존성 없음). 보안상 127.0.0.1 에만 바인딩한다
(에이전트는 bash 등을 실행하므로 네트워크 노출 금지).

실행: python server.py   -> 브라우저가 자동으로 열린다.
"""

import contextlib
import io
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

SESSION = "web"
LOCK = threading.Lock()  # 세션 히스토리 동시 변경 방지 (한 번에 한 요청)

PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HWHarness Chat</title>
<style>
  * { box-sizing: border-box; }
  :root { --bg:#f7f7f8; --panel:#fff; --line:#e5e5e8; --user:#2563eb; --txt:#1f2329; --muted:#8a8f98; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Segoe UI",sans-serif;
         background:var(--bg); color:var(--txt); height:100vh; display:flex; flex-direction:column; }
  header { padding:12px 18px; background:var(--panel); border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:10px; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .tag { font-size:11px; color:var(--muted); border:1px solid var(--line); border-radius:10px; padding:2px 8px; }
  header button { margin-left:auto; font-size:13px; border:1px solid var(--line); background:var(--panel);
                  border-radius:8px; padding:6px 12px; cursor:pointer; }
  header button:hover { background:var(--bg); }
  #chat { flex:1; overflow-y:auto; padding:24px 0; }
  .wrap { max-width:760px; margin:0 auto; padding:0 18px; }
  .msg { display:flex; gap:12px; margin:18px 0; }
  .msg .av { width:28px; height:28px; border-radius:6px; flex:none; font-size:13px;
             display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }
  .msg.user .av { background:var(--user); }
  .msg.bot .av { background:#10a37f; }
  .msg .body { white-space:pre-wrap; line-height:1.6; padding-top:3px; word-break:break-word; }
  .tools { margin:8px 0 0; display:flex; flex-direction:column; gap:4px; }
  .toolrow { font-size:12px; padding:5px 9px; border-radius:7px; border:1px solid var(--line);
             background:var(--panel); white-space:pre-wrap; word-break:break-word; color:#444; }
  .toolrow.ok  { border-left:3px solid #10a37f; }
  .toolrow.err { border-left:3px solid #e5484d; background:#fdf0f0; color:#9b1c1c; }
  .inbar select { border:1px solid var(--line); border-radius:10px; padding:0 8px; font-size:12px; background:var(--panel); }
  .typing { color:var(--muted); font-style:italic; }
  footer { background:var(--panel); border-top:1px solid var(--line); padding:14px 0; }
  .inbar { max-width:760px; margin:0 auto; padding:0 18px; display:flex; gap:10px; }
  textarea { flex:1; resize:none; border:1px solid var(--line); border-radius:12px; padding:12px 14px;
             font:inherit; font-size:15px; max-height:160px; outline:none; }
  textarea:focus { border-color:var(--user); }
  #send { border:none; background:var(--user); color:#fff; border-radius:12px; padding:0 20px; font-size:15px; cursor:pointer; }
  #send:disabled { opacity:.5; cursor:default; }
  .hint { max-width:760px; margin:6px auto 0; padding:0 18px; font-size:11px; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>HWHarness</h1>
  <span class="tag">claude-haiku-4-5 · 회사 프록시</span>
  <button onclick="newChat()">+ 새 대화</button>
</header>
<div id="chat"><div class="wrap" id="list"></div></div>
<footer>
  <div class="inbar">
    <select id="force" title="첫 턴 툴 강제 (tool_choice)">
      <option value="">강제 안 함</option>
      <option value="web_search">web_search 강제</option>
      <option value="any">아무 툴 강제</option>
    </select>
    <textarea id="box" rows="1" placeholder="작업을 입력하세요 (예: 없는파일.txt 읽어줘 / 오늘 날씨 검색해줘)"></textarea>
    <button id="send" onclick="send()">전송</button>
  </div>
  <div class="hint">툴: read_file · write_file · bash · grep · glob · web_search &nbsp;|&nbsp; 실패=빨강 배지(is_error) · 드롭다운=tool_choice 강제 · 승인 게이트는 터미널 창에서</div>
</footer>
<script>
const list = document.getElementById('list');
const box = document.getElementById('box');
const sendBtn = document.getElementById('send');

function esc(s){ return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function addMsg(role, text){
  const m = document.createElement('div');
  m.className = 'msg ' + (role==='user'?'user':'bot');
  m.innerHTML = `<div class="av">${role==='user'?'나':'AI'}</div><div class="content"><div class="body">${esc(text)}</div></div>`;
  list.appendChild(m);
  scroll();
  return m;
}
function scroll(){ const c=document.getElementById('chat'); c.scrollTop=c.scrollHeight; }

box.addEventListener('keydown', e => {
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }
});
box.addEventListener('input', () => { box.style.height='auto'; box.style.height=Math.min(box.scrollHeight,160)+'px'; });

async function send(){
  const text = box.value.trim();
  if(!text) return;
  box.value=''; box.style.height='auto';
  addMsg('user', text);
  sendBtn.disabled=true;
  const bot = addMsg('bot', '');
  const body = bot.querySelector('.body');
  body.innerHTML = '<span class="typing">생각 중…</span>';
  try{
    const force = document.getElementById('force').value;
    const res = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:text, force})});
    const data = await res.json();
    let toolsHtml = '';
    if(data.tools && data.tools.length){
      const rows = data.tools.map(t => {
        const err = t.indexOf('[tool:실패]') >= 0;
        return `<div class="toolrow ${err?'err':'ok'}">${esc(t)}</div>`;
      }).join('');
      toolsHtml = `<div class="tools">${rows}</div>`;
    }
    body.innerHTML = esc(data.answer || '(빈 응답)');
    if(toolsHtml) bot.querySelector('.content').insertAdjacentHTML('beforeend', toolsHtml);
  }catch(err){
    body.innerHTML = '<span style="color:#c00">[오류] '+esc(String(err))+'</span>';
  }
  sendBtn.disabled=false; box.focus(); scroll();
}

async function newChat(){
  await fetch('/api/new', {method:'POST'});
  list.innerHTML=''; box.focus();
}
box.focus();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html")
        elif self.path == "/health":
            self._send(200, json.dumps({"status": "ok"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}

        if self.path == "/api/new":
            for suffix in (".json", ".progress.txt"):
                p = os.path.join("sessions", SESSION + suffix)
                if os.path.exists(p):
                    os.remove(p)
            self._send(200, json.dumps({"status": "reset"}))
            return

        if self.path == "/api/chat":
            message = (payload.get("message") or "").strip()
            if not message:
                self._send(400, json.dumps({"error": "empty message"}))
                return
            # 드롭다운 선택 -> tool_choice (첫 턴 툴 강제)
            force = (payload.get("force") or "").strip()
            if force == "any":
                tool_choice = {"type": "any"}
            elif force:
                tool_choice = {"type": "tool", "name": force}
            else:
                tool_choice = None
            buf = io.StringIO()
            try:
                with LOCK:  # 한 번에 한 작업만 (세션 히스토리 보호)
                    with contextlib.redirect_stdout(buf):
                        _, answer = agent.run_session(message, session_id=SESSION, tool_choice=tool_choice)
            except Exception as exc:  # noqa: BLE001
                self._send(200, json.dumps({"answer": f"[오류] {type(exc).__name__}: {exc}", "tools": []}))
                return
            log = buf.getvalue().splitlines()
            # 새 로그 prefix '[tool:ok]' / '[tool:실패]' 와 '[context]' 캡처
            tools = [ln for ln in log if ln.startswith("[tool") or ln.startswith("[context]")]
            self._send(200, json.dumps({"answer": answer, "tools": tools}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):
        pass  # 콘솔 잡음 억제


def main():
    host = "127.0.0.1"
    port = None
    for candidate in range(8765, 8786):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        raise SystemExit("사용 가능한 포트(8765-8785)를 찾지 못했습니다.")

    url = f"http://{host}:{port}/"
    print(f"HWHarness 챗 UI: {url}  (Ctrl-C 로 종료)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
