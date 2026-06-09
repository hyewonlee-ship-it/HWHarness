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
        assert tool_result_msg[0]["content"] == "SENTINEL_CONTENT_12345"

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


def test_unknown_tool():
    assert "알 수 없는" in agent.execute_tool("nope", {})
    print("PASS: 미지의 툴 디스패치")


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
    assert trs[0]["content"].startswith("Error:")
    print("PASS: is_error 전파 (실패 툴 결과에 is_error=True)")


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
        captured["system"] = system
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
        captured["system"] = system
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
    test_streaming_emits_text_and_tool()
    print("\n모든 테스트 통과 ✅")
