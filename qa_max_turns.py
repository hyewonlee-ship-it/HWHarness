"""QA: max-turns 가드 + 우아한 마무리를 눈으로 확인하는 스크립트.

기본 MAX_TURNS(25)는 라이브로 넘기기 어렵다. 여기선 일부러 4로 낮춰서, 여러 파일을
하나씩 읽게 시킨다 -> 턴 상한을 넘기면 _final_wrapup 으로 마무리되는 걸 관찰한다.

실행: python qa_max_turns.py
"""

import os

import agent

agent.MAX_TURNS = 4  # QA 용으로 낮춤 (기본 25). 런타임 전역값을 바꾸면 루프가 즉시 반영.

# 깨끗한 상태에서 시작
for f in ("sessions/qa-maxturns.json", "sessions/qa-maxturns.progress.txt"):
    if os.path.exists(f):
        os.remove(f)

print(f"== QA: MAX_TURNS={agent.MAX_TURNS} 로 낮춰 테스트 ==")
print("여러 파일을 한 번에 하나씩 읽게 시켜 턴 상한을 넘긴다.\n")

_, answer = agent.run_session(
    "현재 디렉토리의 .py 파일을 glob 으로 찾은 뒤, 각 파일을 read_file 로 "
    "한 번에 하나씩 읽고 각각 한 줄로 요약해줘. 반드시 한 파일씩 순서대로.",
    session_id="qa-maxturns",
)

print("\n[최종 응답]\n" + answer)
print(
    f"\n→ 위에 [tool:ok] 가 약 {agent.MAX_TURNS}번 찍힌 뒤, 툴 없이 '마무리 호출'이 일어나 "
    "최종 응답이 나왔다면 = 턴 상한 가드 + 우아한 마무리 동작 OK"
)
