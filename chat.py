"""대화형 테스트 모드 — 직접 입력하며 에이전트를 돌려본다.

실행: python chat.py
명령: /new (세션 초기화), /exit 또는 Ctrl-D (종료)

같은 세션(session_id="chat")으로 대화가 누적되므로 이전 작업을 이어서 물어볼 수 있다.
실제 회사 프록시로 호출하므로 .env 에 토큰이 채워져 있어야 한다.
"""

import os

import agent

SESSION_ID = "chat"


def reset():
    for suffix in (".json", ".progress.txt"):
        path = os.path.join("sessions", SESSION_ID + suffix)
        if os.path.exists(path):
            os.remove(path)
    print("(세션 초기화됨)")


def main():
    print("=" * 60)
    print("  HWHarness 대화형 테스트")
    print("  - 작업을 입력하면 에이전트가 툴을 써서 처리합니다.")
    print("  - 툴: read_file / write_file / bash / grep / glob")
    print("  - 명령: /new (초기화)  /exit (종료, Ctrl-D 도 가능)")
    print("=" * 60)

    while True:
        try:
            task = input("\n나> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not task:
            continue
        if task in ("/exit", "/quit"):
            print("종료합니다.")
            break
        if task == "/new":
            reset()
            continue

        try:
            # bash 등 위험 툴은 cli_approve 로 실행 전 y/N 확인 (human-in-the-loop)
            _, answer = agent.run_session(task, session_id=SESSION_ID, approve=agent.cli_approve)
            print("\n에이전트>", answer)
        except Exception as exc:  # 한 작업이 실패해도 REPL 은 계속
            print(f"\n[오류] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
