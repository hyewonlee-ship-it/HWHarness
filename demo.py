"""HWHarness 전체 기능 시연 (보고/데모용).

실제 회사 프록시로 호출하므로 .env 에 ANTHROPIC_AUTH_TOKEN 이 채워져 있어야 한다.
실행: python demo.py
"""

import os

import agent

DEMO_SESSION = "demo-report"


def section(title: str):
    print("\n" + "=" * 64)
    print(f"  {title}")
    print("=" * 64)


def reset_demo_session():
    """데모를 매번 깨끗한 상태에서 시작하도록 이 데모 전용 세션 파일만 초기화."""
    for suffix in (".json", ".progress.txt"):
        path = os.path.join("sessions", DEMO_SESSION + suffix)
        if os.path.exists(path):
            os.remove(path)
            print(f"(초기화) {path} 제거")


def main():
    print("HWHarness 데모 — 회사 AI 프록시 pass-through 로 실제 호출합니다.")
    reset_demo_session()

    # 1) 멀티 툴 자율 체이닝 + 스킬 주입
    section("1) 멀티 툴 체이닝 + 스킬 주입  (glob → read → grep → write)")
    print("작업: .py 파일을 찾아 함수 목록을 뽑아 result.txt 에 저장")
    print("기대: '함수' 키워드로 python_functions 스킬이 시스템 프롬프트에 주입됨\n")
    s1, a1 = agent.run_session(
        "현재 디렉토리의 .py 파일(하위 폴더 포함)을 찾아, 각 파일의 함수 정의 목록을 "
        "뽑아서 result.txt 에 저장해줘.",
        session_id=DEMO_SESSION,
    )
    print("\n[최종 응답]\n" + a1)
    print(f"\n[세션] id={s1.session_id}  compaction_count={s1.compaction_count}")

    # 2) 세션 이어받기 (히스토리 + progress)
    section("2) 세션 이어받기  (같은 session_id 로 재실행)")
    print("기대: 이전 작업 결과를 기억 (JSON 히스토리 + progress.txt 이어받기)\n")
    _, a2 = agent.run_session(
        "방금 result.txt 에 저장한 함수가 총 몇 개였는지 기억나? 숫자만 말해줘.",
        session_id=DEMO_SESSION,
    )
    print("\n[최종 응답]\n" + a2)

    # 3) 산출물 안내
    section("산출물")
    print(f"- 세션 히스토리 : sessions/{DEMO_SESSION}.json")
    print(f"- progress 기록 : sessions/{DEMO_SESSION}.progress.txt")
    print("- 작업 산출물   : result.txt")
    print("\n시연한 것: 에이전트 루프 · 5개 툴 자율 체이닝 · 세션 이어받기 · "
          "스킬 주입 · 컨텍스트 관리 · 프록시 pass-through")


if __name__ == "__main__":
    main()
