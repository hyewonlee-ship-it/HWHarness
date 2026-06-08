"""기본 agent loop — 1단계.

messages 히스토리를 누적하며 stop_reason 으로 분기하는 while 루프.
회사 AI 프록시 pass-through 로 연동한다 (Authorization: Bearer).
"""

import json
import os

import anthropic

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 16000

# 인증/엔드포인트는 환경변수로 주입 (코드에 토큰·URL 하드코딩 금지)
PROXY_URL = os.environ.get("ANTHROPIC_BASE_URL")
PROXY_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN")
if not PROXY_URL or not PROXY_TOKEN:
    raise SystemExit("ANTHROPIC_BASE_URL 과 ANTHROPIC_AUTH_TOKEN 환경변수가 필요합니다.")

client = anthropic.Anthropic(base_url=PROXY_URL, auth_token=PROXY_TOKEN)


# ---- 툴 정의 (샘플) --------------------------------------------------------

TOOLS = [
    {
        "name": "get_weather",
        "description": "주어진 도시의 현재 날씨를 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "도시 이름"}},
            "required": ["city"],
        },
    },
]


def execute_tool(name: str, tool_input: dict) -> str:
    if name == "get_weather":
        return f"{tool_input['city']}은(는) 맑음, 22°C"
    return f"알 수 없는 툴: {name}"


# ---- agent loop ------------------------------------------------------------

def run_agent(user_input: str) -> str:
    messages = [{"role": "user", "content": user_input}]

    while True:
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    print(f"[tool] {block.name} -> {result}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue
        raise RuntimeError(f"예상치 못한 stop_reason: {response.stop_reason}")


if __name__ == "__main__":
    print(run_agent("서울 날씨 알려줘"))
