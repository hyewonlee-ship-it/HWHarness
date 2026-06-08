"""에이전트 루프 + 파일 툴 + 세션 관리 — 3단계.

read_file / write_file / bash / grep / glob 5종 툴 + session_id 기반 세션 저장/이어받기.
회사 AI 프록시 pass-through 로 연동한다 (Authorization: Bearer).
"""

import fnmatch
import glob as globlib
import json
import os
import re
import subprocess

import anthropic

from session import SessionManager, serialize_messages

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 16000

PROXY_URL = os.environ.get("ANTHROPIC_BASE_URL")
PROXY_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN")
if not PROXY_URL or not PROXY_TOKEN:
    raise SystemExit("ANTHROPIC_BASE_URL 과 ANTHROPIC_AUTH_TOKEN 환경변수가 필요합니다.")

client = anthropic.Anthropic(base_url=PROXY_URL, auth_token=PROXY_TOKEN)


# ---- 툴 정의 ---------------------------------------------------------------

TOOLS = [
    {
        "name": "read_file",
        "description": "로컬 파일을 읽어 텍스트 내용을 반환한다. 실패 시 에러 메시지 반환.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "읽을 파일 경로"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "텍스트를 파일에 쓴다. 상위 디렉토리가 없으면 자동 생성한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "쓸 파일 경로"},
                "content": {"type": "string", "description": "파일에 쓸 내용"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": "셸 커맨드를 실행하고 종료코드·stdout·stderr 를 반환한다. 타임아웃 30초. 위험 커맨드 차단.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "실행할 셸 커맨드"}},
            "required": ["command"],
        },
    },
    {
        "name": "grep",
        "description": "파일 내용에서 정규식을 검색해 'path:줄번호: 내용' 으로 반환한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "검색할 정규식"},
                "path": {"type": "string", "description": "대상 파일/디렉토리 (기본: 현재)"},
                "glob": {"type": "string", "description": "파일명 필터 (예: *.py). 선택."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": "파일명 패턴으로 파일 목록을 검색한다. ** 재귀 매칭 지원.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 패턴 (예: **/*.py)"},
                "path": {"type": "string", "description": "검색 기준 디렉토리 (기본: 현재)"},
            },
            "required": ["pattern"],
        },
    },
]


def read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: 파일을 찾을 수 없습니다: {path}"
    except IsADirectoryError:
        return f"Error: 디렉토리입니다: {path}"
    except UnicodeDecodeError:
        return f"Error: 텍스트로 디코딩할 수 없습니다: {path}"
    except OSError as exc:
        return f"Error: 읽기 실패: {path} ({exc})"


def write_file(path: str, content: str) -> str:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"OK: {len(content)}자를 {path} 에 썼습니다."
    except OSError as exc:
        return f"Error: 쓰기 실패: {path} ({exc})"


BASH_TIMEOUT = 30
DANGEROUS_PATTERNS = [
    (r"\brm\b[^\n|;&]*-[a-zA-Z]*[rf]", "rm -r/-f"),
    (r"\bsudo\b", "sudo"),
    (r"(^|[\s;&|])su\b", "su"),
    (r"\b(shutdown|reboot|halt|poweroff|init)\b", "시스템 종료/재부팅"),
    (r"\bmkfs\b", "mkfs"),
    (r"\bdd\b[^\n]*\bof=", "dd of="),
    (r">\s*/dev/[sh]d", "/dev 덮어쓰기"),
    (r":\s*\(\s*\)\s*\{", "fork bomb"),
    (r"\bchmod\b[^\n]*-R[^\n]*\s/(?:\s|$)", "chmod -R /"),
    (r"(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba)?sh", "원격 스크립트 파이프 실행"),
    (r"\bmv\b[^\n]*\s/(?:\s|$)", "루트로 이동"),
]


def _blocked_reason(command: str):
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


def run_bash(command: str, timeout: int = BASH_TIMEOUT) -> str:
    reason = _blocked_reason(command)
    if reason:
        return f"Error: 위험한 커맨드로 차단됨 [{reason}]: {command}"
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"Error: 타임아웃({timeout}s) 초과: {command}"
    out = [f"[exit] {proc.returncode}"]
    if proc.stdout:
        out.append(f"[stdout]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        out.append(f"[stderr]\n{proc.stderr.rstrip()}")
    return "\n".join(out)


GREP_MAX_MATCHES = 200
GREP_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


def grep(pattern: str, path: str = ".", glob: str = None) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: 잘못된 정규식: {pattern} ({exc})"
    if not os.path.exists(path):
        return f"Error: 경로를 찾을 수 없습니다: {path}"
    if os.path.isfile(path):
        files = [path]
    else:
        files = []
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in GREP_SKIP_DIRS]
            for n in names:
                if glob and not fnmatch.fnmatch(n, glob):
                    continue
                files.append(os.path.join(root, n))
    matches = []
    truncated = False
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{fp}:{lineno}: {line.rstrip()}")
                        if len(matches) >= GREP_MAX_MATCHES:
                            truncated = True
                            break
        except (OSError, UnicodeDecodeError):
            continue
        if truncated:
            break
    if not matches:
        return "(매치 없음)"
    result = "\n".join(matches)
    if truncated:
        result += f"\n... (상위 {GREP_MAX_MATCHES}개만 표시, 잘림)"
    return result


GLOB_MAX_RESULTS = 500


def glob_files(pattern: str, path: str = ".") -> str:
    full = pattern if os.path.isabs(pattern) else os.path.join(path, pattern)
    matches = sorted(p for p in globlib.glob(full, recursive=True))
    if not matches:
        return "(매치 없음)"
    shown = matches[:GLOB_MAX_RESULTS]
    result = "\n".join(shown)
    if len(matches) > GLOB_MAX_RESULTS:
        result += f"\n... (상위 {GLOB_MAX_RESULTS}개만 표시, 총 {len(matches)}개)"
    return result


def execute_tool(name: str, tool_input: dict) -> str:
    if name == "read_file":
        return read_file(tool_input["path"])
    if name == "write_file":
        return write_file(tool_input["path"], tool_input["content"])
    if name == "bash":
        return run_bash(tool_input["command"])
    if name == "grep":
        return grep(tool_input["pattern"], tool_input.get("path", "."), tool_input.get("glob"))
    if name == "glob":
        return glob_files(tool_input["pattern"], tool_input.get("path", "."))
    return f"알 수 없는 툴: {name}"


# ---- agent loop ------------------------------------------------------------

def run_agent(user_input: str, messages: list = None, system: str = None) -> str:
    """messages 를 넘기면 그 히스토리를 이어서(in-place) 누적한다. system 은 시스템 프롬프트."""
    if messages is None:
        messages = []
    messages.append({"role": "user", "content": user_input})

    while True:
        kwargs = {"model": MODEL, "max_tokens": MAX_TOKENS, "tools": TOOLS, "messages": messages}
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
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
                    print(f"[tool] {block.name}({json.dumps(block.input, ensure_ascii=False)}) -> {result}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue
        raise RuntimeError(f"예상치 못한 stop_reason: {response.stop_reason}")


# ---- 세션 기반 실행 --------------------------------------------------------

DEFAULT_SYSTEM = (
    "당신은 로컬 파일 시스템 작업을 돕는 에이전트입니다. "
    "제공된 툴(read_file, write_file, bash, grep, glob)을 사용해 작업을 수행하세요."
)


def run_session(task: str, session_id: str = None, base_dir: str = "sessions"):
    """세션을 이어받거나 새로 만들어 작업을 수행하고 히스토리·progress 를 저장한다."""
    mgr = SessionManager(base_dir)
    session = mgr.resume_or_new(session_id, system_prompt=DEFAULT_SYSTEM,
                                tools=[t["name"] for t in TOOLS])
    system = session.system_prompt
    prior = mgr.read_progress(session)
    if prior:
        system += f"\n\n[이전 세션 진행 기록 — 이어서 작업하세요]\n{prior}"

    answer = run_agent(task, messages=session.messages, system=system)
    session.token_count = len(json.dumps(serialize_messages(session.messages), ensure_ascii=False)) // 4
    mgr.save(session)
    mgr.append_progress(session, f"[작업] {task}\n[결과] {answer}")
    return session, answer


if __name__ == "__main__":
    session, answer = run_session(
        "현재 디렉토리의 .py 파일을 찾아 함수 목록을 뽑아줘.", session_id="demo",
    )
    print(f"\n[session {session.session_id}] {answer}")
