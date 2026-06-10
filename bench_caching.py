"""Prompt caching 측정 벤치마크.

두 가지를 실측한다 (실제 회사 프록시 + haiku-4-5):

A. 시스템 프롬프트 캐싱 (단발 반복 호출)
   같은 system(+tools) 프리픽스를 2번 호출해 1회차(write) → 2회차(read) 를 본다.
     1) no_cache    : 현재 프롬프트, cache_control 없음 (기준)
     2) cache       : 현재 프롬프트, system 에 cache_control (현재 동작)
     3) cache_large : 4096토큰 넘기게 패딩 (메커니즘 입증)

B. 멀티턴 메시지 캐싱 (NEW — 실제 agent 루프 모사)
   매 턴 [이전 히스토리 전체 + 새 tool_result]를 다시 보내는 run_agent 패턴을 재현한다.
     4) multiturn_no_msgcache : system 만 캐시 (마지막 메시지 브레이크포인트 없음)
     5) multiturn_msgcache    : agent._cache_messages 로 마지막 메시지에 브레이크포인트 (NEW)
   → 5)는 누적 프리픽스가 4096을 넘는 순간부터 2턴째 이후 cache_read 가 잡혀야 한다.

결과를 표로 출력하고 /tmp/caching_bench.json 에 저장. 실행: python bench_caching.py
"""

import json

import agent
from skills import build_system_prompt

# haiku-4-5 단가 ($/1M tokens) — cache write 1.25x input, cache read 0.1x input
PRICE_IN = 1.00
PRICE_OUT = 5.00
PRICE_CACHE_WRITE = PRICE_IN * 1.25
PRICE_CACHE_READ = PRICE_IN * 0.10

MSG = "준비됐으면 'ok' 한 단어만 답해."  # A 시나리오: 두 호출에서 동일 (프리픽스 일치 보장)

# 실제 run_session 과 동일하게 시스템 프롬프트 구성 (skills/progress 제외, 결정적)
# 주의: 아래 environment 의 툴 목록(6종)은 캐싱 리포트의 BASE≈3,913토큰 baseline 을 위한 고정값이다.
# 최신 8종으로 늘리면 프리픽스 토큰 수가 바뀌어 .docs/prompt-caching-report.md 의 측정치와 어긋나니,
# 의도적으로 그대로 둔다(데모용 합성 환경, 실제 TOOLS 와 무관).
BASE_SYSTEM = build_system_prompt(
    role=agent.DEFAULT_ROLE,
    environment="작업 디렉토리: /demo\nOS: Demo\n사용 가능한 툴: read_file, write_file, bash, grep, glob, web_search",
    rules=agent.DEFAULT_RULES,
    output_format=agent.DEFAULT_OUTPUT_FORMAT,
)
# 4096 토큰 최소 프리픽스를 확실히 넘기도록 패딩 (참조 문서를 시스템에 넣은 상황 모사)
PAD = ("\n[참고 자료] " + ("이 줄은 캐시 최소 프리픽스(haiku 4096토큰)를 넘기기 위한 더미 컨텍스트입니다. " * 40)) * 40
LARGE_SYSTEM = BASE_SYSTEM + PAD


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()

# 멀티턴 시뮬레이션에서 tool_result 로 누적할 실제 파일 내용 (프리픽스를 4096 위로 키움)
TOOL_PAYLOADS = [_read("agent.py"), _read("context.py"), _read("session.py")]


def call(system_param, msgs=None):
    r = agent.client.messages.create(
        model=agent.MODEL, max_tokens=16, tools=agent.TOOLS,
        system=system_param,
        messages=msgs if msgs is not None else [{"role": "user", "content": MSG}],
    )
    u = r.usage
    return {
        "input": u.input_tokens,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "output": u.output_tokens,
    }


def cost(rec):
    return (rec["input"] * PRICE_IN + rec["cache_write"] * PRICE_CACHE_WRITE
            + rec["cache_read"] * PRICE_CACHE_READ + rec["output"] * PRICE_OUT) / 1_000_000


def cached_block(text):
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def run(label, system_param):
    r1 = call(system_param)   # 1회차
    r2 = call(system_param)   # 2회차 (캐시면 read 기대)
    return {"label": label, "calls": [r1, r2]}


def multiturn(label, use_msg_cache):
    """실제 run_agent 루프 모사: 매 턴 [이전 히스토리 전체 + 새 tool_result]를 다시 보낸다.

    system 은 두 경우 모두 캐시 블록(현재 기본 동작). use_msg_cache=True 면
    agent._cache_messages 로 마지막 메시지에 브레이크포인트를 단다(= 새로 추가한 멀티턴 캐싱).
    """
    sys_param = cached_block(BASE_SYSTEM)
    messages = [{"role": "user", "content": "이 저장소를 조사해줘. 먼저 핵심 파일부터 읽어."}]
    recs = []
    for i in range(len(TOOL_PAYLOADS) + 1):  # 턴1(입력만) + 툴 결과 누적 턴들
        req = agent._cache_messages(messages) if use_msg_cache else messages
        recs.append(call(sys_param, req))
        if i < len(TOOL_PAYLOADS):  # 모델이 파일을 읽어 결과가 누적되는 상황 주입
            tid = f"toolu_{i}"
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": "파일을 확인합니다."},
                {"type": "tool_use", "id": tid, "name": "read_file", "input": {"path": f"f{i}.py"}},
            ]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": TOOL_PAYLOADS[i]},
            ]})
    return {"label": label, "calls": recs}


def print_scenario(sc):
    for n, rec in enumerate(sc["calls"], 1):
        head = sc["label"] if n == 1 else ""
        print(f"{head:<40} turn{n:<2} {rec['input']:>7} {rec['cache_write']:>8} "
              f"{rec['cache_read']:>7} {rec['output']:>5} {cost(rec):>10.6f}")


def main():
    # A. 시스템 프롬프트 캐싱
    a = [
        run("1) no_cache (현재, 캐시 OFF)", BASE_SYSTEM),
        run("2) cache (현재, system 캐시)", cached_block(BASE_SYSTEM)),
        run("3) cache_large (>4096 패딩)", cached_block(LARGE_SYSTEM)),
    ]
    # B. 멀티턴 메시지 캐싱
    b = [
        multiturn("4) multiturn (msg캐시 OFF)", use_msg_cache=False),
        multiturn("5) multiturn (msg캐시 ON, NEW)", use_msg_cache=True),
    ]

    base_total = a[0]["calls"][0]["input"]
    print(f"\nBASE 프리픽스(tools+system+msg) 약 {base_total} tokens "
          f"(haiku 최소 4096 {'미만' if base_total < 4096 else '이상'})\n")
    hdr = f"{'시나리오':<40} {'turn':<6} {'input':>7} {'c_write':>8} {'c_read':>7} {'out':>5} {'$/req':>10}"

    print("== A. 시스템 프롬프트 캐싱 (단발 반복) ==")
    print(hdr); print("-" * len(hdr))
    for sc in a:
        print_scenario(sc)

    print("\n== B. 멀티턴 메시지 캐싱 (agent 루프 모사) ==")
    print(hdr); print("-" * len(hdr))
    for sc in b:
        print_scenario(sc)

    print("\n[판정]")
    for sc in a[1:]:
        read = sc["calls"][1]["cache_read"]
        print(f"- {sc['label']}: 2회차 cache_read={read} → "
              + ("캐시 작동 ✅" if read > 0 else "미작동(프리픽스 4096 미만/미지원)"))
    for sc in b:
        reads = [c["cache_read"] for c in sc["calls"]]
        hit = any(r > 0 for r in reads)
        print(f"- {sc['label']}: 턴별 cache_read={reads} → "
              + ("멀티턴 캐시 작동 ✅" if hit else "미작동"))

    # 멀티턴 비용 비교 (전체 턴 합)
    def total_cost(sc):
        return sum(cost(c) for c in sc["calls"])
    off, on = total_cost(b[0]), total_cost(b[1])
    print(f"\n멀티턴 4턴 총비용: msg캐시 OFF ${off:.6f} → ON ${on:.6f} "
          f"(절감 {(1 - on / off) * 100:.1f}%)" if off else "")

    with open("/tmp/caching_bench.json", "w", encoding="utf-8") as f:
        json.dump({"system": a, "multiturn": b}, f, ensure_ascii=False, indent=2)
    print("\n결과 저장: /tmp/caching_bench.json")


if __name__ == "__main__":
    main()
