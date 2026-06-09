"""대화형 테스트 모드 — 직접 입력하며 에이전트를 돌려본다.

실행: python chat.py
명령: /help /skills /skill <이름> <작업> /new /exit

같은 세션(session_id="chat")으로 대화가 누적되므로 이전 작업을 이어서 물어볼 수 있다.
실제 회사 프록시로 호출하므로 .env 에 토큰이 채워져 있어야 한다.
"""

import os

# input() 의 줄 편집(백스페이스·방향키·히스토리) 활성화. 이게 없으면 일부 환경에서
# 입력한 글자가 지워지지 않는다. 모듈이 없을 수도 있으니 안전하게 import.
try:
    import readline  # noqa: F401
except ImportError:
    pass

import agent
from skills import get_skill_text, list_skills

SESSION_ID = "chat"


def reset():
    for suffix in (".json", ".progress.txt"):
        path = os.path.join("sessions", SESSION_ID + suffix)
        if os.path.exists(path):
            os.remove(path)
    print("(세션 초기화됨)")


def print_skills():
    skills = list_skills()
    if not skills:
        print("(skills/ 에 스킬이 없습니다)")
        return
    print("\n사용 가능한 스킬 (슬래시로 강제 주입):")
    for name, keywords, title in skills:
        print(f"  /skill {name} <작업>")
        print(f"      {title}  [키워드: {', '.join(keywords)}]")


def print_help():
    print("\n명령어:")
    print("  /skills              — 스킬 목록 보기")
    print("  /skill <이름> <작업>  — 특정 스킬을 강제 주입하고 작업 실행")
    print("  /new                 — 세션 초기화")
    print("  /help                — 이 도움말")
    print("  /exit                — 종료 (Ctrl-D 도 가능)")
    print("그 외 입력은 일반 작업으로 처리됩니다.")


def main():
    print("=" * 60)
    print("  HWHarness 대화형 테스트")
    print("  - 작업을 입력하면 에이전트가 툴을 써서 처리합니다.")
    print("  - 툴: read_file / write_file / bash / grep / glob / web_search")
    print("  - '/' 로 시작하면 명령. /help 로 전체 명령 보기")
    print("=" * 60)

    while True:
        try:
            task = input("\n나> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not task:
            continue

        # ---- 슬래시 명령 ----
        if task in ("/exit", "/quit"):
            print("종료합니다.")
            break
        if task == "/new":
            reset()
            continue
        if task in ("/help", "/"):
            print_help()
            continue
        if task == "/skills":
            print_skills()
            continue

        extra_skills = ""
        if task.startswith("/skill "):
            rest = task[len("/skill "):].strip()
            parts = rest.split(maxsplit=1)
            name = parts[0] if parts else ""
            skill_text = get_skill_text(name)
            if skill_text is None:
                print(f"스킬 '{name}' 을(를) 찾을 수 없습니다. /skills 로 목록을 확인하세요.")
                continue
            if len(parts) < 2:
                print(f"사용법: /skill {name} <작업 내용>")
                continue
            extra_skills = skill_text
            task = parts[1]  # 실제 작업으로 교체
            print(f"(스킬 '{name}' 강제 주입)")
        elif task.startswith("/"):
            print(f"알 수 없는 명령: {task}  (/help 참고)")
            continue

        try:
            # bash 등 위험 툴은 cli_approve 로 실행 전 y/N 확인 (human-in-the-loop)
            _, answer = agent.run_session(
                task, session_id=SESSION_ID, approve=agent.cli_approve, extra_skills=extra_skills,
            )
            print("\n에이전트>", answer)
        except Exception as exc:  # 한 작업이 실패해도 REPL 은 계속
            print(f"\n[오류] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
