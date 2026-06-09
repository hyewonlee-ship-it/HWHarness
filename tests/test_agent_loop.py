"""agent loop + read_file 툴 테스트 (네트워크 없이 mock client 사용).

tool_use -> end_turn 시나리오를 스크립팅해서 다음을 검증한다:
  - messages 히스토리 누적 (user -> assistant -> tool_result -> assistant)
  - stop_reason 분기 (tool_use면 실행, end_turn이면 종료)
  - tool_use_id 매칭
  - read_file 툴 실제 실행 (정상/에러) 및 최종 텍스트 반환
"""

import json
import os
import sys
import tempfile
from types import SimpleNamespace

# agent.py 모듈 로드 시 프록시 env(BASE_URL/AUTH_TOKEN)를 요구하므로 더미 주입.
# (실제 호출은 client.messages.create 를 monkeypatch 하므로 네트워크로 안 나간다)
os.environ.setdefault("ANTHROPIC_BASE_URL", "https://proxy.test.local/anthropic")
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-dummy-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


class _Block:
    """SDK content 블록 흉내 — 속성 접근 + model_dump 직렬화 둘 다 지원."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, mode="json", exclude_none=True):
        return dict(self.__dict__)


def block(**kw):
    return _Block(**kw)


def _sys_text(system):
    """system 이 문자열이든 cache_control 블록 리스트든 텍스트를 반환 (캐싱 토글 대응)."""
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system)
    return system or ""


def make_scripted_create(captured, target_path):
    """create 호출마다 messages를 기록하고, 호출 순서대로 스크립트된 응답을 반환."""
    responses = [
        # 1턴: read_file 툴 호출
        SimpleNamespace(
            stop_reason="tool_use",
            content=[
                block(type="text", text="파일을 읽어볼게요."),
                block(type="tool_use", id="toolu_test_1", name="read_file",
                      input={"path": target_path}),
            ],
        ),
        # 2턴: 최종 응답
        SimpleNamespace(
            stop_reason="end_turn",
            content=[block(type="text", text="파일을 읽고 요약했습니다.")],
        ),
    ]
    calls = {"n": 0}

    def fake_create(*, model, max_tokens, tools, messages):
        captured.append([dict(m) for m in messages])
        resp = responses[calls["n"]]
        calls["n"] += 1
        return resp

    return fake_create


def test_agent_loop_with_read_file():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write("SENTINEL_CONTENT_12345")
        target_path = tf.name

    try:
        captured = []
        agent.client.messages.create = make_scripted_create(captured, target_path)

        result = agent.run_agent("이 파일 읽어줘")

        # 1) 최종 반환 텍스트
        assert result == "파일을 읽고 요약했습니다.", f"final={result!r}"

        # 2) create 가 정확히 2번 호출됨 (tool_use 1회 + end_turn 1회)
        assert len(captured) == 2, f"calls={len(captured)}"

        # 3) 1번째 호출: user 1개
        first = captured[0]
        assert len(first) == 1 and first[0]["role"] == "user"

        # 4) 2번째 호출: user -> assistant -> user(tool_result), 총 3개
        second = captured[1]
        assert [m["role"] for m in second] == ["user", "assistant", "user"], second

        # 5) tool_result 의 tool_use_id 매칭 + read_file 가 실제 파일을 읽었는지
        tool_result_msg = second[2]["content"]
        assert tool_result_msg[0]["type"] == "tool_result"
        assert tool_result_msg[0]["tool_use_id"] == "toolu_test_1"
        # read_file 은 외부 입력 -> <tool_output> 으로 감싸짐 (인젝션 방어). 내용은 그 안에 포함.
        assert "SENTINEL_CONTENT_12345" in tool_result_msg[0]["content"]
        assert "<tool_output>" in tool_result_msg[0]["content"]

        print("PASS: agent loop + read_file (history 누적 / stop_reason 분기 / tool_use_id 매칭 / 실제 파일 읽기)")
    finally:
        os.unlink(target_path)


def test_read_file():
    # 정상 읽기
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write("hello world")
        path = tf.name
    try:
        assert agent.read_file(path) == "hello world"
    finally:
        os.unlink(path)

    # 파일 없음 -> 에러 메시지
    missing = agent.read_file("/nonexistent/path/nope.txt")
    assert missing.startswith("Error: 파일을 찾을 수 없습니다"), missing

    # 디렉토리 -> 에러 메시지
    assert agent.read_file(os.path.dirname(__file__)).startswith("Error: 디렉토리입니다")

    print("PASS: read_file (정상 / 파일없음 / 디렉토리)")


def test_write_file():
    base = tempfile.mkdtemp()

    # 정상 쓰기 (round-trip)
    p = os.path.join(base, "out.txt")
    msg = agent.write_file(p, "hello")
    assert msg.startswith("OK:"), msg
    assert open(p, encoding="utf-8").read() == "hello"

    # 상위 디렉토리 자동 생성
    nested = os.path.join(base, "a", "b", "c", "deep.txt")
    msg = agent.write_file(nested, "x")
    assert msg.startswith("OK:"), msg
    assert os.path.isfile(nested)

    print("PASS: write_file (정상 쓰기 / 디렉토리 자동 생성)")


def test_edit_file():
    base = tempfile.mkdtemp()
    p = os.path.join(base, "f.txt")
    open(p, "w", encoding="utf-8").write("alpha\nbeta\ngamma\nbeta\n")

    # 정상: 유일한 old_string 교체
    r = agent.edit_file(p, "gamma", "GAMMA")
    assert r.startswith("OK") and "1곳" in r
    assert open(p, encoding="utf-8").read() == "alpha\nbeta\nGAMMA\nbeta\n"

    # stale 감지: old_string 이 없으면 실패 + read_file 재확인 유도, 파일은 안 바뀜
    r = agent.edit_file(p, "zeta", "X")
    assert r.startswith("Error:") and "stale" in r and "read_file" in r
    assert open(p, encoding="utf-8").read() == "alpha\nbeta\nGAMMA\nbeta\n"
    # stale 결과는 is_error 로 모델에 전달돼 복구를 유도한다
    assert agent._make_tool_result("id", r).get("is_error") is True

    # 모호: 여러 곳이면 replace_all 없이는 실패
    r = agent.edit_file(p, "beta", "B")
    assert r.startswith("Error:") and "2곳" in r and "replace_all" in r

    # replace_all: 모두 교체
    r = agent.edit_file(p, "beta", "B", replace_all=True)
    assert r.startswith("OK") and "2곳" in r
    assert open(p, encoding="utf-8").read() == "alpha\nB\nGAMMA\nB\n"

    # old==new: 변화 없음 -> 실패
    assert agent.edit_file(p, "B", "B").startswith("Error:")

    # 없는 파일 -> 실패(write_file 안내)
    assert agent.edit_file(os.path.join(base, "none.txt"), "a", "b").startswith("Error:")

    # execute_tool 디스패치 (replace_all 기본 False)
    assert agent.execute_tool(
        "edit_file", {"path": p, "old_string": "alpha", "new_string": "A"}).startswith("OK")
    assert open(p, encoding="utf-8").read().startswith("A\n")

    # 외부 입력이 아니므로 인젝션 래핑 대상 아님 (write_file 과 일관)
    assert "edit_file" not in agent.EXTERNAL_TOOLS
    print("PASS: edit_file (교체 / stale감지+is_error / 모호 / replace_all / old==new / 없는파일 / 디스패치)")


def test_bash():
    # 정상 실행: stdout 반환 + exit 0
    out = agent.run_bash("echo hello_bash")
    assert "hello_bash" in out and "[exit] 0" in out, out

    # stderr 도 반환
    err = agent.run_bash("ls /nonexistent_dir_xyz")
    assert "[stderr]" in err and "[exit]" in err, err

    # 위험 커맨드 차단 (실제 실행 안 됨)
    for cmd in ["rm -rf /tmp/x", "sudo rm file", "shutdown now", ":(){ :|:& };:"]:
        r = agent.run_bash(cmd)
        assert r.startswith("Error: 위험한 커맨드로 차단됨"), (cmd, r)

    # 타임아웃 처리 (짧은 타임아웃으로 빠르게 검증)
    t = agent.run_bash("sleep 5", timeout=1)
    assert t.startswith("Error: 타임아웃"), t

    print("PASS: bash (정상 / stderr / 위험 차단 / 타임아웃)")


def test_grep():
    base = tempfile.mkdtemp()
    with open(os.path.join(base, "a.py"), "w", encoding="utf-8") as f:
        f.write("def foo():\n    return 1\n")
    with open(os.path.join(base, "b.txt"), "w", encoding="utf-8") as f:
        f.write("hello\nfoo bar\n")

    # 디렉토리 재귀 검색
    out = agent.grep(r"foo", base)
    assert "a.py:1:" in out and "b.txt:2:" in out, out

    # glob 필터 (.py 만)
    out_py = agent.grep(r"foo", base, glob="*.py")
    assert "a.py" in out_py and "b.txt" not in out_py, out_py

    # 매치 없음
    assert agent.grep(r"ZZZ_없음", base) == "(매치 없음)"

    # 잘못된 정규식
    assert agent.grep(r"[unclosed", base).startswith("Error: 잘못된 정규식")

    print("PASS: grep (재귀 검색 / glob 필터 / 매치 없음 / 잘못된 정규식)")


def test_glob():
    base = tempfile.mkdtemp()
    os.makedirs(os.path.join(base, "sub"))
    open(os.path.join(base, "a.py"), "w").close()
    open(os.path.join(base, "sub", "b.py"), "w").close()
    open(os.path.join(base, "c.txt"), "w").close()

    # 재귀 매칭 (**/*.py -> a.py + sub/b.py)
    out = agent.glob_files("**/*.py", base)
    assert "a.py" in out and os.path.join("sub", "b.py") in out, out
    assert "c.txt" not in out

    # 단일 레벨 패턴
    out_txt = agent.glob_files("*.txt", base)
    assert "c.txt" in out_txt and "a.py" not in out_txt

    # 매치 없음
    assert agent.glob_files("*.md", base) == "(매치 없음)"

    print("PASS: glob (재귀 ** / 단일 패턴 / 매치 없음)")


def test_web_search():
    captured = {}

    def fake_get_json(url, headers, timeout):
        captured["url"] = url
        return {"web": {"results": [
            {"title": "제목A", "url": "https://a.example", "description": "설명 &amp; A"},
            {"title": "제목B", "url": "https://b.example", "description": "설명 B"},
        ]}}

    orig = agent._http_get_json
    agent._http_get_json = fake_get_json
    try:
        out = agent.web_search("anthropic claude", count=2)
        assert "제목A" in out and "https://a.example" in out
        assert "설명 & A" in out  # HTML 엔티티 unescape
        assert "q=anthropic" in captured["url"] and "count=2" in captured["url"]

        # 결과 없음
        agent._http_get_json = lambda url, headers, timeout: {"web": {"results": []}}
        assert agent.web_search("zzz") == "(검색 결과 없음)"

        # count 클램프 (>10 -> 10)
        agent._http_get_json = fake_get_json
        agent.web_search("x", count=99)
        assert "count=10" in captured["url"]
    finally:
        agent._http_get_json = orig
    print("PASS: web_search (결과 포맷 / unescape / 결과없음 / count 클램프)")


def test_web_fetch():
    # http/https 외 scheme 차단 (로컬파일 접근/SSRF 방지)
    assert agent.web_fetch("file:///etc/passwd").startswith("Error:")
    assert agent.web_fetch("ftp://h/x").startswith("Error:")

    orig = agent._http_get_text
    try:
        # HTML 본문 -> 텍스트 (script/style 제거, 엔티티 복원, 태그 제거)
        agent._http_get_text = lambda u, t: (
            "<html><body><h1>제목</h1><p>본문 &amp; 끝</p><script>bad()</script></body></html>",
            "text/html; charset=utf-8")
        out = agent.web_fetch("https://e.example/page")
        assert "제목" in out and "본문 & 끝" in out
        assert "bad()" not in out and "<h1>" not in out

        # 긴 본문 잘림
        agent._http_get_text = lambda u, t: ("x" * (agent.WEB_FETCH_MAX_CHARS + 500), "text/plain")
        out2 = agent.web_fetch("https://e.example/big")
        assert "잘림" in out2 and len(out2) < agent.WEB_FETCH_MAX_CHARS + 200

        # 비-HTML(JSON)은 원문 그대로
        agent._http_get_text = lambda u, t: ('{"a": 1}', "application/json")
        assert agent.web_fetch("https://e.example/j") == '{"a": 1}'

        # execute_tool 디스패치
        agent._http_get_text = lambda u, t: ("본문ok", "text/plain")
        assert agent.execute_tool("web_fetch", {"url": "https://e.example"}) == "본문ok"
    finally:
        agent._http_get_text = orig

    # web_fetch 는 외부 입력 -> 인젝션 래핑 대상(EXTERNAL_TOOLS)
    assert "web_fetch" in agent.EXTERNAL_TOOLS
    print("PASS: web_fetch (scheme차단 / HTML텍스트화 / 잘림 / JSON원문 / 디스패치)")


def test_web_fetch_http_error():
    import urllib.error
    orig = agent._http_get_text

    def boom(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    agent._http_get_text = boom
    try:
        out = agent.web_fetch("https://e.example/missing")
    finally:
        agent._http_get_text = orig
    assert out.startswith("Error:") and "404" in out
    print("PASS: web_fetch HTTP 에러 처리")


def test_unknown_tool():
    assert "알 수 없는" in agent.execute_tool("nope", {})
    print("PASS: 미지의 툴 디스패치")


def test_web_search_server_side_tool_declaration():
    # 기본(서버사이드): TOOLS 의 web_search 는 Anthropic 서버툴 타입으로 선언된다
    ws = [t for t in agent.TOOLS if t.get("name") == "web_search"][0]
    if agent.WEB_SEARCH_SERVER_SIDE:
        assert ws["type"] == "web_search_20250305"      # 서버가 검색 수행 (클라이언트 키/HTTP 불요)
        assert "input_schema" not in ws                  # 커스텀 스키마 아님
    # 두 정의의 형태 대비 (학습 포인트): 서버=type 선언 / 클라이언트=input_schema 보유
    assert agent._WEB_SEARCH_SERVER["type"] == "web_search_20250305"
    assert "input_schema" in agent._WEB_SEARCH_CLIENT and "type" not in agent._WEB_SEARCH_CLIENT
    print("PASS: 서버사이드 web_search 툴 선언 (type=web_search_20250305)")


def test_server_tool_use_not_dispatched_locally():
    # 서버사이드 검색 응답(server_tool_use/web_search_tool_result + 최종 text)은 우리 루프가
    # execute_tool 로 실행하지 않고, 한 번의 end_turn 으로 마무리되어야 한다.
    seq = iter([
        SimpleNamespace(stop_reason="end_turn", content=[
            block(type="text", text="검색하겠습니다."),
            block(type="server_tool_use", name="web_search", input={"query": "현재 한국 대통령"}),
            block(type="web_search_tool_result", content=[{"title": "r1"}, {"title": "r2"}]),
            block(type="text", text="답: ..."),
        ]),
    ])
    calls = {"n": 0}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, **kw):
        calls["n"] += 1
        return next(seq)

    orig_create = agent.client.messages.create
    orig_exec = agent.execute_tool
    dispatched = []
    agent.client.messages.create = fake_create
    agent.execute_tool = lambda name, ti: dispatched.append(name) or "x"
    try:
        out = agent.run_agent("현재 한국 대통령 검색해줘")
    finally:
        agent.client.messages.create = orig_create
        agent.execute_tool = orig_exec
    assert calls["n"] == 1                 # 한 번의 호출로 끝남 (서버에서 검색 완결)
    assert dispatched == []                # execute_tool 미호출 (클라이언트 검색 안 함)
    assert out == "검색하겠습니다.답: ..."  # 텍스트 블록 전부 이어붙임 (프리앰블+답변)
    print("PASS: server_tool_use 는 로컬 디스패치 없이 end_turn 으로 마무리")


def test_max_turns_guard():
    # mock: 툴이 주어지면 무조건 tool_use(끝없음), 툴 없이 오면(마무리 호출) end_turn+텍스트
    calls = {"n": 0}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None):
        calls["n"] += 1
        if not tools:  # _final_wrapup 의 마무리 호출 (tools 없이)
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[block(type="text", text="지금까지 결과로 마무리합니다.")])
        return SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id=f"t{calls['n']}", name="read_file",
                  input={"path": "__no_such_file__"}),
        ])

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    try:
        result = agent.run_agent("끝없이 툴을 부르게 해줘")  # 가드 없으면 여기서 영원히 멈춤
    finally:
        agent.client.messages.create = orig

    # 우아한 마무리: 무뚝뚝한 중단이 아니라 모델이 정리한 최종 텍스트가 와야 함
    assert result == "지금까지 결과로 마무리합니다.", result
    # 툴 턴 MAX_TURNS 회 + 마무리 호출 1회
    assert calls["n"] == agent.MAX_TURNS + 1, calls["n"]
    print(f"PASS: max_turns 우아한 마무리 (툴 {agent.MAX_TURNS}턴 + 마무리 1회, 최종 텍스트 반환)")


def test_is_error_propagation():
    # 헬퍼 단위: 에러 규약('Error:') -> is_error=True, 정상 -> 플래그 없음
    assert agent._make_tool_result("id1", "Error: 없음").get("is_error") is True
    assert "is_error" not in agent._make_tool_result("id2", "정상 결과")
    # untrusted=True 면 <tool_output> 으로 감싸고, 에러 판정은 원본 기준 유지
    wrapped = agent._make_tool_result("id3", "Error: x", untrusted=True)
    assert "<tool_output>" in wrapped["content"] and "Error: x" in wrapped["content"]
    assert wrapped.get("is_error") is True
    assert "<tool_output>" not in agent._make_tool_result("id4", "ok")["content"]  # 기본 미래핑

    # 통합: 실패하는 툴(없는 파일 read_file)의 결과가 is_error=True 로 모델에 전달되는지
    seq = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="terr", name="read_file", input={"path": "__nope__"})]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="확인했습니다")]),
    ])

    def fake_create(*, model, max_tokens, messages, tools=None, system=None):
        return next(seq)

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    msgs = []
    try:
        agent.run_agent("없는 파일 읽어줘", messages=msgs)
    finally:
        agent.client.messages.create = orig

    trs = [b for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
           for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(trs) == 1 and trs[0]["is_error"] is True, trs
    assert "Error:" in trs[0]["content"]  # read_file 외부툴 -> <tool_output> 래핑 안에 에러 포함
    print("PASS: is_error 전파 (실패 툴 결과에 is_error=True)")


def test_cache_messages_marks_last_block_only():
    # 문자열 content -> text 블록으로 감싸고 cache_control 부여
    out = agent._cache_messages([{"role": "user", "content": "hi"}])
    assert out[0]["content"][0]["text"] == "hi"
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    # 리스트 content -> 마지막 블록에만 브레이크포인트
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "x"},
            {"type": "tool_result", "tool_use_id": "b", "content": "y"},
        ]},
    ]
    out = agent._cache_messages(msgs)
    last = out[-1]["content"]
    assert "cache_control" not in last[0]               # 앞 블록엔 안 붙음
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(out[0]["content"], str)           # 이전 메시지는 그대로

    # 원본 불변 (마커가 히스토리에 누적되면 4개 한도 초과/직렬화 오염)
    assert msgs[-1]["content"][-1] == {"type": "tool_result", "tool_use_id": "b", "content": "y"}
    assert "cache_control" not in str(msgs)
    assert agent._cache_messages([]) == []              # 빈 히스토리는 그대로
    print("PASS: _cache_messages 마지막 블록만 마킹 + 원본 불변")


def test_cache_messages_wired_into_loop():
    # run_agent 가 create 에 넘기는 messages 의 마지막 블록에 cache_control 이 실려가는지
    captured = {}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, **kw):
        captured["messages"] = messages
        return SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="done")])

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    try:
        assert agent.CACHE_MESSAGES is True
        agent.run_agent("안녕")
    finally:
        agent.client.messages.create = orig
    last = captured["messages"][-1]["content"]
    assert isinstance(last, list) and last[-1]["cache_control"] == {"type": "ephemeral"}
    print("PASS: 멀티턴 캐싱이 run_agent 루프에 연결됨")


def _has_cc(b):
    return isinstance(b, dict) and "cache_control" in b


def test_multiturn_breakpoint_moves_not_accumulates():
    """멀티턴 루프에서 브레이크포인트가 매 턴 '마지막 메시지로 이동'하고 이전 메시지엔
    누적되지 않는지 검증. 이게 안 되면(예: system 위치 고정) write 만 되고 read 로
    회수되지 않는 헛쓰기가 난다 — 그 구조적 원인을 막는지 본다.
    (실제 cache_write/read 토큰 측정은 bench_caching.py 의 멀티턴 시나리오 담당.)
    """
    # 툴 3회 호출 후 종료 → create 4번 호출, 매 턴 tool_result 가 누적
    responses = [
        SimpleNamespace(stop_reason="tool_use",
                        content=[block(type="tool_use", id=f"t{i}", name="read_file",
                                       input={"path": f"__nope{i}__"})])
        for i in range(3)
    ] + [SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="끝")])]
    captured = []
    calls = {"n": 0}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, **kw):
        captured.append(messages)               # _cache_messages 가 적용된 요청 그 자체
        r = responses[calls["n"]]; calls["n"] += 1
        return r

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    history = []
    try:
        agent.run_agent("조사해줘", messages=history)
    finally:
        agent.client.messages.create = orig

    assert len(captured) == 4
    for turn, msgs in enumerate(captured, 1):
        last = msgs[-1]["content"]
        assert isinstance(last, list) and _has_cc(last[-1]), f"turn{turn}: 마지막 블록에 브레이크포인트 없음"
        # 한 요청에 메시지 마커는 정확히 1곳 — 이전 메시지로 누적되면 안 됨(4개 한도/정렬 문제)
        marks = sum(_has_cc(b) for m in msgs for b in (m["content"] if isinstance(m["content"], list) else []))
        assert marks == 1, f"turn{turn}: 메시지 마커 {marks}개 (이동이 아니라 누적됨)"

    # 원본 히스토리(세션에 저장될 것)엔 마커가 단 하나도 남지 않아야 함
    for m in history:
        if isinstance(m["content"], list):
            assert not any(_has_cc(b) for b in m["content"]), "원본 히스토리에 cache_control 오염"
    print("PASS: 멀티턴 브레이크포인트 이동 + 원본 히스토리 무오염")


def test_msg_cache_off_leaves_no_message_breakpoint():
    """CACHE_MESSAGES=False 면 메시지엔 브레이크포인트가 전혀 안 붙는다.

    이 상태에서 system 에만 마커가 있으면(below-minimum) 브레이크포인트가 꼬리를 못 따라가
    write 만 반복하는 헛쓰기가 난다 — A/B 실험으로 확인된 케이스. 그 '메시지 무마킹' 설정을
    구조적으로 대조 고정한다.
    """
    captured = {}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, **kw):
        captured["messages"] = [dict(m) for m in messages]
        return SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="done")])

    orig = agent.client.messages.create
    orig_flag = agent.CACHE_MESSAGES
    agent.client.messages.create = fake_create
    agent.CACHE_MESSAGES = False
    try:
        agent.run_agent("안녕")
    finally:
        agent.client.messages.create = orig
        agent.CACHE_MESSAGES = orig_flag

    for m in captured["messages"]:
        c = m["content"]
        if isinstance(c, list):
            assert not any(_has_cc(b) for b in c)
        else:
            assert isinstance(c, str)  # 문자열 그대로 — text 블록 변환/마킹조차 안 함
    print("PASS: msg캐시 OFF 시 메시지 브레이크포인트 없음 (헛쓰기 설정 대조)")


def test_parallel_tools_run_concurrently():
    # 느린 툴 여러 개가 병렬로 겹쳐 실행되는지 벽시계로 확인 (4*0.3s 순차=1.2s, 병렬≈0.3s)
    import time
    orig = agent.execute_tool
    agent.execute_tool = lambda name, ti: (time.sleep(0.3), f"done:{ti['k']}")[1]
    blocks = [block(type="tool_use", id=f"t{i}", name="read_file", input={"k": i}) for i in range(4)]
    try:
        agent.PARALLEL_TOOLS = True
        t = time.perf_counter()
        res = agent._execute_tool_blocks(blocks, approve=None)
        elapsed = time.perf_counter() - t
    finally:
        agent.execute_tool = orig
    assert set(res) == {f"t{i}" for i in range(4)}
    assert elapsed < 0.9, f"병렬이 아닌 듯: {elapsed:.2f}s"
    print(f"PASS: 병렬 tool 동시 실행 ({elapsed:.2f}s)")


def test_parallel_tools_order_and_approval():
    # 승인 거부된 툴은 실행 안 되고, 승인 불필요 툴은 실행됨 (승인은 메인 스레드 순차)
    executed = []
    orig = agent.execute_tool
    agent.execute_tool = lambda name, ti: (executed.append(ti.get("path") or ti.get("command")),
                                           f"ran:{ti.get('path')}")[1]
    blocks = [
        block(type="tool_use", id="a", name="read_file", input={"path": "A"}),
        block(type="tool_use", id="b", name="bash", input={"command": "rm x"}),  # 승인 대상
        block(type="tool_use", id="c", name="read_file", input={"path": "C"}),
    ]
    try:
        res = agent._execute_tool_blocks(blocks, approve=lambda n, i: False)  # 모두 거부
    finally:
        agent.execute_tool = orig
    assert res["b"].startswith("Error: 사용자가 실행을 거부")  # bash 거부
    assert "rm x" not in executed                              # bash 는 실행 안 됨
    assert res["a"] == "ran:A" and res["c"] == "ran:C"         # read_file 은 실행됨
    print("PASS: 병렬 — 승인 게이트 보존 (거부 툴 미실행)")


def test_run_agent_parallel_tool_results_order():
    # run_agent 가 여러 tool_use 결과를 원래 tool_use 순서대로 조립하는지 (id 매칭 무결성)
    seq = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="id1", name="glob", input={"pattern": "*.nope1"}),
            block(type="tool_use", id="id2", name="glob", input={"pattern": "*.nope2"}),
            block(type="tool_use", id="id3", name="glob", input={"pattern": "*.nope3"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="ok")]),
    ])

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, **kw):
        return next(seq)

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    msgs = []
    try:
        agent.run_agent("세 개 glob 동시에", messages=msgs)
    finally:
        agent.client.messages.create = orig
    trs = [b for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
           for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert [t["tool_use_id"] for t in trs] == ["id1", "id2", "id3"]  # 원래 순서 보존
    print("PASS: 병렬 tool_result 원래 순서로 조립")


def test_dependency_scheduling():
    mk = lambda i, name, **inp: block(type="tool_use", id=f"t{i}", name=name, input=inp)
    shape = lambda bs: [len(s) for s in agent._schedule_stages(bs)]

    # 독립(다른 파일 읽기) → 1단계 병렬
    assert shape([mk(0, "read_file", path="a"), mk(1, "read_file", path="b")]) == [2]
    # 같은 파일 write→read → 충돌, 2단계 순차
    assert shape([mk(0, "write_file", path="x", content=""), mk(1, "read_file", path="x")]) == [1, 1]
    # 같은 파일 edit 두 번 → 순차
    assert shape([mk(0, "edit_file", path="x", old_string="a", new_string="b"),
                  mk(1, "edit_file", path="x", old_string="b", new_string="c")]) == [1, 1]
    # 다른 파일 write → 병렬
    assert shape([mk(0, "write_file", path="a", content=""),
                  mk(1, "write_file", path="b", content="")]) == [2]
    # bash 는 전역 장벽 → 다른 FS 툴과 순차
    assert shape([mk(0, "read_file", path="a"), mk(1, "bash", command="ls"),
                  mk(2, "read_file", path="b")]) == [1, 1, 1]
    # 네트워크 툴은 로컬 자원 없음 → 파일 읽기와도 병렬
    assert shape([mk(0, "web_fetch", url="http://x"), mk(1, "read_file", path="a")]) == [2]
    # 서브트리 겹침: dir/f 쓰기 + dir 검색 → 순차
    assert shape([mk(0, "write_file", path="d/f", content=""), mk(1, "grep", pattern="x", path="d")]) == [1, 1]
    print("PASS: 의존성 스케줄링 (충돌=순차 / 독립=병렬 / bash 장벽 / 서브트리)")


def test_run_agent_dependent_write_then_read():
    # write_file(X) 와 read_file(X) 가 한 응답에 오면, 의존 감지로 write 가 먼저 실행돼
    # read 결과에 방금 쓴 내용이 보여야 한다 (순서 무시 시 read 가 빈/없는 파일을 볼 위험).
    p = os.path.join(tempfile.mkdtemp(), "dep.txt")
    seq = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="w", name="write_file", input={"path": p, "content": "WROTE_IT"}),
            block(type="tool_use", id="r", name="read_file", input={"path": p}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="done")]),
    ])

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, **kw):
        return next(seq)

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    msgs = []
    try:
        agent.run_agent("write 후 read", messages=msgs)
    finally:
        agent.client.messages.create = orig
    trs = {b["tool_use_id"]: b["content"] for m in msgs
           if m["role"] == "user" and isinstance(m["content"], list)
           for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}
    assert "WROTE_IT" in trs["r"], trs["r"]  # write 가 먼저 → read 가 그 내용을 봄
    print("PASS: 의존(write→read) 순차 — read 가 write 결과를 봄")


def test_approval_gate():
    # bash 를 호출하는 응답 -> 그 다음 end_turn
    def make_seq():
        return iter([
            SimpleNamespace(stop_reason="tool_use", content=[
                block(type="tool_use", id="tb", name="bash", input={"command": "echo hi"})]),
            SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="끝")]),
        ])

    def run_with(approve_fn, executed):
        seq = make_seq()

        def fake_create(*, model, max_tokens, messages, tools=None, system=None):
            return next(seq)

        # 실제 bash 실행 여부를 감지하기 위해 run_bash 를 가로챈다
        orig_create, orig_bash = agent.client.messages.create, agent.run_bash
        agent.client.messages.create = fake_create
        agent.run_bash = lambda cmd, timeout=30: executed.append(cmd) or "[exit] 0"
        msgs = []
        try:
            agent.run_agent("echo 해줘", messages=msgs, approve=approve_fn)
        finally:
            agent.client.messages.create = orig_create
            agent.run_bash = orig_bash
        trs = [b for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        return trs[0]

    # 거부: bash 실행 안 됨 + is_error
    executed = []
    tr = run_with(lambda name, inp: False, executed)
    assert executed == [], executed
    assert tr.get("is_error") is True and "거부" in tr["content"]

    # 승인: bash 실행됨
    executed = []
    tr = run_with(lambda name, inp: True, executed)
    assert executed == ["echo hi"], executed
    assert tr.get("is_error") is None

    print("PASS: 승인 게이트 (거부 시 미실행+is_error / 승인 시 실행)")


def test_tool_choice_first_turn_only():
    seen = []  # 각 create 호출에 전달된 tool_choice 기록
    seq = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="t1", name="read_file", input={"path": "__nope__"})]),  # turn1
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="끝")]),       # turn2
    ])

    def fake_create(*, model, max_tokens, tools, messages, system=None, tool_choice=None):
        seen.append(tool_choice)
        return next(seq)

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    try:
        agent.run_agent("검색부터 해", tool_choice={"type": "tool", "name": "web_search"})
    finally:
        agent.client.messages.create = orig

    assert seen[0] == {"type": "tool", "name": "web_search"}, seen  # 첫 턴: 강제
    assert seen[1] is None, seen                                    # 이후 턴: auto (강제 해제)
    print("PASS: tool_choice 강제 (첫 턴만 적용, 이후 auto)")


class _FakeStream:
    """client.messages.stream 컨텍스트매니저 흉내."""
    def __init__(self, deltas, final):
        self._deltas, self._final = deltas, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._deltas)

    def get_final_message(self):
        return self._final


def test_streaming_emits_text_and_tool():
    events = []
    streams = iter([
        # turn1: 텍스트 델타 없이 tool_use (read_file 실패)
        _FakeStream([], SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="ts", name="read_file", input={"path": "__nope__"})])),
        # turn2: 토큰 델타 후 end_turn
        _FakeStream(["안녕", "하세요"],
                    SimpleNamespace(stop_reason="end_turn", content=[TextBlockFake("안녕하세요")])),
    ])

    def fake_stream(**kwargs):
        return next(streams)

    orig = agent.client.messages.stream
    agent.client.messages.stream = fake_stream
    try:
        result = agent.run_agent("hi", on_event=lambda k, d: events.append((k, d)))
    finally:
        agent.client.messages.stream = orig

    assert ("text", "안녕") in events and ("text", "하세요") in events  # 토큰 델타 emit
    assert any(k == "tool" for k, _ in events)                         # 툴 이벤트 emit
    assert result == "안녕하세요"
    print("PASS: 스트리밍 (text 델타 + tool 이벤트 emit)")


# ---- 세션 관리 (3단계) ----------------------------------------------------

import session as sessmod  # noqa: E402


class TextBlockFake:
    """SDK TextBlock 흉내 — .type/.text 접근 + model_dump 직렬화 둘 다 지원."""
    type = "text"

    def __init__(self, text):
        self.text = text

    def model_dump(self, mode="json", exclude_none=True):
        return {"type": "text", "text": self.text}


def test_session_serialize():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [TextBlockFake("yo")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
    ]
    out = sessmod.serialize_messages(msgs)
    assert out[1]["content"][0] == {"type": "text", "text": "yo"}
    assert out[2]["content"][0]["type"] == "tool_result"
    json.dumps(out)  # JSON 직렬화 가능해야 함
    print("PASS: session 직렬화 (블록->dict)")


def test_session_save_load_resume_progress():
    base = tempfile.mkdtemp()
    mgr = sessmod.SessionManager(base)

    s = mgr.new_session("sid1", system_prompt="sp", tools=["read_file"])
    s.messages.append({"role": "user", "content": "first"})
    mgr.save(s)
    mgr.append_progress(s, "did step 1")

    loaded = mgr.load("sid1")
    assert loaded is not None
    assert loaded.messages == [{"role": "user", "content": "first"}]
    assert loaded.system_prompt == "sp"

    # 기존 세션 이어받기
    assert mgr.resume_or_new("sid1").messages == [{"role": "user", "content": "first"}]
    # 없는 id 는 새 세션
    assert mgr.resume_or_new("brand_new").messages == []
    # progress 읽기
    assert "did step 1" in mgr.read_progress(loaded)

    print("PASS: session save/load/resume/progress")


def test_run_session_resume():
    base = tempfile.mkdtemp()
    captured = []

    def fake_create(*, model, max_tokens, tools, messages, system=None):
        captured.append([dict(m) for m in messages])
        return SimpleNamespace(stop_reason="end_turn", content=[TextBlockFake("done")])

    agent.client.messages.create = fake_create

    # 1회차
    _, a1 = agent.run_session("작업1", session_id="rs1", base_dir=base)
    assert a1 == "done"
    assert len(captured[0]) == 1  # user 작업1 만

    # 2회차: 같은 id -> 이전 히스토리 이어받음
    agent.run_session("작업2", session_id="rs1", base_dir=base)
    second = captured[1]
    assert len(second) >= 3, second  # user 작업1 + assistant done + user 작업2
    assert second[0]["role"] == "user" and "작업1" in str(second[0]["content"])
    assert second[-1]["role"] == "user"  # 새 작업2

    # progress 2개 누적
    prog = open(os.path.join(base, "rs1.progress.txt"), encoding="utf-8").read()
    assert prog.count("[작업]") == 2, prog

    print("PASS: run_session 이어받기 + progress 누적")


# ---- 컨텍스트 관리 (4단계) ------------------------------------------------

import context as ctxmod  # noqa: E402


def _big_messages(n=400):
    """should_compact 를 트리거할 만큼 큰 히스토리 (user 텍스트 경계 포함)."""
    msgs = [{"role": "user", "content": "작업 시작"}]
    filler = "x" * 2000
    for i in range(n):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": filler}]})
        msgs.append({"role": "user", "content": f"계속 {i} {filler}"})
    return msgs


def test_estimate_and_should_compact():
    small = [{"role": "user", "content": "hi"}]
    assert ctxmod.estimate_tokens(small) >= 0
    assert ctxmod.should_compact(small) is False
    assert ctxmod.should_compact(_big_messages()) is True
    print("PASS: estimate_tokens / should_compact (70% 임계)")


def test_strip_old_tool_results():
    msgs = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "오래된 큰 결과"}]},
        # 최근부분
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "최근 결과"}]},
    ]
    out = ctxmod.strip_old_tool_results(msgs, keep_recent=2)
    # 오래된 tool_result 는 placeholder, ID 는 보존
    old_tr = out[2]["content"][0]
    assert old_tr["content"] == ctxmod.STRIP_PLACEHOLDER and old_tr["tool_use_id"] == "t1"
    # 최근 tool_result 는 그대로
    assert out[4]["content"][0]["content"] == "최근 결과"
    print("PASS: strip_old_tool_results (오래된 것만 치환 / ID 보존)")


def test_compact_context_safe_boundary():
    msgs = [
        {"role": "user", "content": "옛날 작업"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "끝"}]},
        {"role": "user", "content": "새 작업"},  # 깨끗한 경계
        {"role": "assistant", "content": [{"type": "text", "text": "응답"}]},
    ]
    out = ctxmod.compact_context(msgs, summarize=lambda t: "요약본", keep_recent=2)
    # 첫 메시지는 요약, tail 은 깨끗한 user 경계('새 작업')에서 시작
    assert out[0]["role"] == "user" and out[0]["content"].startswith("[이전 컨텍스트 요약]")
    assert "요약본" in out[0]["content"]
    # tail 머리에 떠도는 tool_result 가 없어야 함 (user 텍스트로 시작)
    assert out[1] == {"role": "user", "content": "새 작업"}
    print("PASS: compact_context (요약 주입 / 안전 경계 tail)")


def test_manage_context_escalation():
    # 작으면 변경 없음
    small = [{"role": "user", "content": "hi"}]
    msgs, did = ctxmod.manage_context(small, summarize=lambda t: "S")
    assert did is False and msgs is small
    # 크면 관리 수행
    msgs, did = ctxmod.manage_context(_big_messages(), summarize=lambda t: "S")
    assert did is True
    print("PASS: manage_context (소형 무변경 / 대형 관리)")


def test_run_agent_compaction_wiring():
    base = tempfile.mkdtemp()
    sess = sessmod.SessionManager(base).new_session("c1")

    def fake_create(*, model, max_tokens, tools, messages, system=None):
        return SimpleNamespace(stop_reason="end_turn", content=[TextBlockFake("ok")])

    orig_create = agent.client.messages.create
    orig_manage = agent.manage_context
    agent.client.messages.create = fake_create
    # manage_context 가 '관리함'을 보고 -> run_agent 가 compaction_count 증가시켜야
    agent.manage_context = lambda messages, summarize, **kw: (list(messages), True)
    try:
        agent.run_agent("hi", messages=sess.messages, session=sess)
    finally:
        agent.client.messages.create = orig_create
        agent.manage_context = orig_manage

    assert sess.compaction_count >= 1
    print("PASS: run_agent 압축 wiring (compaction_count 증가)")


# ---- 시스템 프롬프트 + 스킬 (5단계) ---------------------------------------

import skills as skillsmod  # noqa: E402


def test_build_system_prompt():
    sp = skillsmod.build_system_prompt(
        role="R", environment="E", task_context="T",
        rules="X", output_format="O", skills="S",
    )
    for tag in ["[ROLE & IDENTITY]", "[ENVIRONMENT]", "[TASK CONTEXT]", "[RULES]", "[OUTPUT FORMAT]", "[SKILLS]"]:
        assert tag in sp, tag
    assert sp.index("[ROLE") < sp.index("[ENVIRONMENT]") < sp.index("[RULES]")
    # 빈 섹션은 생략
    sp2 = skillsmod.build_system_prompt(role="R", environment="E")
    assert "[TASK CONTEXT]" not in sp2 and "[SKILLS]" not in sp2
    print("PASS: build_system_prompt (섹션 구조 / 빈 섹션 생략)")


def test_load_relevant_skills():
    sk = tempfile.mkdtemp()
    with open(os.path.join(sk, "py.md"), "w", encoding="utf-8") as f:
        f.write("<!-- keywords: 함수, python -->\n# 파이썬\n함수 분석 규칙")
    with open(os.path.join(sk, "net.md"), "w", encoding="utf-8") as f:
        f.write("<!-- keywords: 네트워크, http -->\n# 네트워크\n요청 규칙")

    # 키워드 매칭 -> 해당 스킬만
    out = skillsmod.load_relevant_skills("이 파일의 함수 목록 뽑아줘", sk)
    assert "함수 분석 규칙" in out and "요청 규칙" not in out
    # 비매칭 -> 빈
    assert skillsmod.load_relevant_skills("그냥 인사", sk) == ""
    # 디렉토리 없음 -> 빈
    assert skillsmod.load_relevant_skills("x", "/no/such/dir") == ""
    print("PASS: load_relevant_skills (키워드 검색 / 비매칭 / 디렉토리 없음)")


def test_run_session_injects_skill():
    base = tempfile.mkdtemp()
    sk = tempfile.mkdtemp()
    with open(os.path.join(sk, "py.md"), "w", encoding="utf-8") as f:
        f.write("<!-- keywords: 함수 -->\n함수는 grep 으로 찾아라")

    captured = {}

    def fake_create(*, model, max_tokens, tools, messages, system=None):
        captured["system"] = _sys_text(system)
        return SimpleNamespace(stop_reason="end_turn", content=[TextBlockFake("ok")])

    agent.client.messages.create = fake_create
    agent.run_session("함수 목록 뽑아줘", session_id="sk1", base_dir=base, skills_dir=sk)

    sys_prompt = captured["system"]
    assert "[ROLE & IDENTITY]" in sys_prompt          # 구조화 프롬프트
    assert "[SKILLS]" in sys_prompt                   # 스킬 섹션
    assert "함수는 grep 으로 찾아라" in sys_prompt     # 관련 스킬 주입됨
    print("PASS: run_session 가 관련 스킬을 시스템 프롬프트에 주입")


def test_list_and_get_skill():
    sk = tempfile.mkdtemp()
    with open(os.path.join(sk, "alpha.md"), "w", encoding="utf-8") as f:
        f.write("<!-- keywords: a, b -->\n# 알파 가이드\n본문")

    names = [n for n, _, _ in skillsmod.list_skills(sk)]
    assert names == ["alpha"], names
    title = skillsmod.list_skills(sk)[0][2]
    assert title == "알파 가이드", title

    got = skillsmod.get_skill_text("alpha", sk)
    assert got.startswith("### alpha") and "본문" in got
    assert skillsmod.get_skill_text("없음", sk) is None
    print("PASS: list_skills / get_skill_text (슬래시 명령용)")


def test_run_session_extra_skills():
    base = tempfile.mkdtemp()
    captured = {}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, tool_choice=None):
        captured["system"] = _sys_text(system)
        return SimpleNamespace(stop_reason="end_turn", content=[TextBlockFake("ok")])

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    try:
        # 키워드와 무관한 작업이라도 extra_skills 는 강제로 주입돼야 함
        agent.run_session("아무 작업", session_id="es1", base_dir=base,
                          skills_dir=tempfile.mkdtemp(), extra_skills="### 강제스킬\n강제 주입 내용")
    finally:
        agent.client.messages.create = orig

    assert "강제 주입 내용" in captured["system"], captured["system"]
    print("PASS: extra_skills 강제 주입 (/skill 명령 기반)")


def test_system_prompt_four_elements():
    base = tempfile.mkdtemp()
    captured = {}

    def fake_create(*, model, max_tokens, messages, tools=None, system=None, tool_choice=None):
        captured["system"] = _sys_text(system)
        return SimpleNamespace(stop_reason="end_turn", content=[TextBlockFake("ok")])

    orig = agent.client.messages.create
    agent.client.messages.create = fake_create
    try:
        agent.run_session("아무 작업", session_id="sp1", base_dir=base, skills_dir=tempfile.mkdtemp())
    finally:
        agent.client.messages.create = orig

    sp = captured["system"]
    assert "작업 디렉토리" in sp and "OS:" in sp          # ① 환경 정보 주입
    assert "[툴 사용 규율]" in sp                          # ② 툴 선택 규율
    assert "[에러 복구]" in sp                             # ③ 에러 복구 유도
    assert "[보안" in sp and "데이터" in sp and "지시" in sp  # ④ 인젝션 방어(신뢰 경계)
    print("PASS: 시스템 프롬프트 4요소 (환경/툴규율/에러복구/인젝션방어)")


if __name__ == "__main__":
    test_read_file()
    test_write_file()
    test_bash()
    test_grep()
    test_glob()
    test_web_search()
    test_unknown_tool()
    test_max_turns_guard()
    test_is_error_propagation()
    test_approval_gate()
    test_tool_choice_first_turn_only()
    test_agent_loop_with_read_file()
    test_session_serialize()
    test_session_save_load_resume_progress()
    test_run_session_resume()
    test_estimate_and_should_compact()
    test_strip_old_tool_results()
    test_compact_context_safe_boundary()
    test_manage_context_escalation()
    test_run_agent_compaction_wiring()
    test_build_system_prompt()
    test_load_relevant_skills()
    test_run_session_injects_skill()
    test_list_and_get_skill()
    test_run_session_extra_skills()
    test_system_prompt_four_elements()
    test_streaming_emits_text_and_tool()
    print("\n모든 테스트 통과 ✅")
