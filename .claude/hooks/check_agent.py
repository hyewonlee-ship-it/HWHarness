#!/usr/bin/env python3
"""CLAUDE.md 컨벤션 강제 hook (PostToolUse, Write|Edit 대상).

agent.py 편집 후 다음을 검사하고, 위반 시 decision:block으로 모델에 피드백한다.
  1) MODEL 상수는 "claude-haiku-4-5"로 고정
  2) base_url 에 URL을 하드코딩 금지 (ANTHROPIC_BASE_URL 환경변수 사용)
"""

import json
import os
import re
import sys

PINNED_MODEL = "claude-haiku-4-5"

data = json.load(sys.stdin)
tool_input = data.get("tool_input", {})
tool_response = data.get("tool_response", {})
file_path = tool_input.get("file_path") or tool_response.get("filePath", "")

# agent.py(진입점)에만 적용
if os.path.basename(file_path) != "agent.py":
    sys.exit(0)

try:
    with open(file_path, encoding="utf-8") as f:
        src = f.read()
except OSError:
    sys.exit(0)

violations = []

# 1) 모델 고정
m = re.search(r'^\s*MODEL\s*=\s*["\']([^"\']+)["\']', src, re.M)
if m and m.group(1) != PINNED_MODEL:
    violations.append(
        f'MODEL은 "{PINNED_MODEL}"로 고정해야 합니다 (현재: "{m.group(1)}"). '
        "다른 모델이 필요하면 CLAUDE.md를 먼저 갱신하세요."
    )

# 2) base_url 하드코딩 금지 (env에서 받아야 함)
for mm in re.finditer(r'base_url\s*=\s*(["\'])(https?://[^"\']+)\1', src):
    violations.append(
        f'base_url에 URL을 직접 하드코딩하지 마세요 ("{mm.group(2)}"). '
        "ANTHROPIC_BASE_URL 환경변수에서 읽으세요."
    )

if violations:
    print(json.dumps({
        "decision": "block",
        "reason": "CLAUDE.md 규칙 위반:\n- " + "\n- ".join(violations),
    }, ensure_ascii=False))

sys.exit(0)
