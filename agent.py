"""기본 agent loop.

messages 배열에 대화 히스토리를 누적하고, stop_reason이 end_turn이면 멈추고
tool_use이면 툴을 실행해 결과를 다시 모델에 돌려주는 while 루프.

회사 AI 프록시 pass-through 패턴으로 연동한다. 프록시가 Authorization: Bearer
<토큰> 으로 인증을 받아 요청을 Anthropic 으로 그대로 전달한다.
"""

import fnmatch
import glob as globlib
import json
import os
import re
import subprocess

import anthropic

from context import manage_context
from session import SessionManager, serialize_messages
from skills import build_system_prompt, load_relevant_skills

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 16000

def _load_dotenv(path=".env"):
    """의존성 없이 .env 파일을 읽어 환경변수로 로드한다 (이미 설정된 값은 유지)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# 회사 AI 프록시 pass-through 연동. 인증/엔드포인트는 모두 환경변수로 주입한다
# (코드에 토큰·URL 하드코딩 금지). .env 파일이나 셸 export 로 설정:
#   ANTHROPIC_BASE_URL="https://<company-proxy>"  # 프록시 엔드포인트
#   ANTHROPIC_AUTH_TOKEN="<회사 AI 프록시 토큰>"                          # Authorization: Bearer 로 전송
#
# auth_token 을 쓰면 SDK 가 x-api-key 대신 Authorization: Bearer 헤더를 보낸다.
# (프록시 모드에서는 ANTHROPIC_API_KEY 를 설정하지 말 것 — 두 헤더가 동시에 나가면 거부될 수 있음)
PROXY_URL = os.environ.get("ANTHROPIC_BASE_URL")
PROXY_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN")
if not PROXY_URL or not PROXY_TOKEN:
    raise SystemExit(
        "회사 프록시 연동에는 다음 환경변수가 필요합니다:\n"
        '  export ANTHROPIC_BASE_URL="https://<회사-프록시>/anthropic"\n'
        '  export ANTHROPIC_AUTH_TOKEN="<프록시 토큰>"'
    )

client = anthropic.Anthropic(base_url=PROXY_URL, auth_token=PROXY_TOKEN)


# ---- 툴 정의 ---------------------------------------------------------------

TOOLS = [
    {
        "name": "read_file",
        "description": "로컬 파일을 읽어 텍스트 내용을 반환한다. 파일이 없거나 읽을 수 없으면 에러 메시지를 반환한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "읽을 파일 경로 (상대/절대)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "텍스트 내용을 파일에 쓴다. 상위 디렉토리가 없으면 자동 생성한다. 성공/실패 메시지를 반환한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "쓸 파일 경로 (상대/절대)"},
                "content": {"type": "string", "description": "파일에 쓸 내용"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": (
            "셸 커맨드를 실행하고 종료코드·stdout·stderr 를 반환한다. "
            "타임아웃 30초. rm -rf, sudo 등 위험한 커맨드는 차단된다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "실행할 셸 커맨드"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "grep",
        "description": (
            "파일 내용에서 정규식 패턴을 검색해 매치된 줄을 'path:줄번호: 내용' 형식으로 반환한다. "
            "path 가 디렉토리면 재귀 검색하며, glob 으로 파일명을 필터링할 수 있다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "검색할 정규식 패턴"},
                "path": {"type": "string", "description": "검색 대상 파일/디렉토리 (기본: 현재 디렉토리)"},
                "glob": {"type": "string", "description": "디렉토리 검색 시 파일명 필터 (예: *.py). 선택."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": (
            "파일명 패턴으로 파일 목록을 검색한다. ** 재귀 매칭을 지원한다 (예: *.py, src/**/*.js). "
            "path 기준으로 검색하며 매치된 경로 목록을 반환한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 패턴 (예: *.py, **/*.txt)"},
                "path": {"type": "string", "description": "검색 기준 디렉토리 (기본: 현재 디렉토리)"},
            },
            "required": ["pattern"],
        },
    },
]


def read_file(path: str) -> str:
    """파일을 읽어 내용을 반환. 실패 시 'Error: ...' 메시지 반환."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: 파일을 찾을 수 없습니다: {path}"
    except IsADirectoryError:
        return f"Error: 디렉토리입니다 (파일 경로를 지정하세요): {path}"
    except UnicodeDecodeError:
        return f"Error: 텍스트로 디코딩할 수 없습니다 (바이너리 파일?): {path}"
    except OSError as exc:
        return f"Error: 읽기 실패: {path} ({exc})"


def write_file(path: str, content: str) -> str:
    """파일에 내용을 쓴다. 상위 디렉토리가 없으면 생성. 실패 시 'Error: ...' 반환."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"OK: {len(content)}자를 {path} 에 썼습니다."
    except OSError as exc:
        return f"Error: 쓰기 실패: {path} ({exc})"


BASH_TIMEOUT = 30  # 초

# 위험 커맨드 차단 리스트 (실행 전에 검사, 매치되면 실행 안 함)
DANGEROUS_PATTERNS = [
    (r"\brm\b[^\n|;&]*-[a-zA-Z]*[rf]", "rm -r/-f (재귀·강제 삭제)"),
    (r"\bsudo\b", "sudo (권한 상승)"),
    (r"(^|[\s;&|])su\b", "su (계정 전환)"),
    (r"\b(shutdown|reboot|halt|poweroff|init)\b", "시스템 종료/재부팅"),
    (r"\bmkfs\b", "mkfs (파일시스템 포맷)"),
    (r"\bdd\b[^\n]*\bof=", "dd of= (디스크 직접 쓰기)"),
    (r">\s*/dev/[sh]d", "/dev 블록 디바이스 덮어쓰기"),
    (r":\s*\(\s*\)\s*\{", "fork bomb"),
    (r"\bchmod\b[^\n]*-R[^\n]*\s/(?:\s|$)", "chmod -R / (루트 권한 변경)"),
    (r"(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba)?sh", "원격 스크립트 파이프 실행"),
    (r"\bmv\b[^\n]*\s/(?:\s|$)", "루트로 이동"),
]


def _blocked_reason(command: str):
    """위험 패턴에 걸리면 사유 문자열, 아니면 None."""
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


def run_bash(command: str, timeout: int = BASH_TIMEOUT) -> str:
    """셸 커맨드를 실행하고 종료코드·stdout·stderr 를 반환. 위험 커맨드는 차단."""
    reason = _blocked_reason(command)
    if reason:
        return f"Error: 위험한 커맨드로 차단됨 [{reason}]: {command}"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: 타임아웃({timeout}s) 초과: {command}"

    out = [f"[exit] {proc.returncode}"]
    if proc.stdout:
        out.append(f"[stdout]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        out.append(f"[stderr]\n{proc.stderr.rstrip()}")
    return "\n".join(out)


GREP_MAX_MATCHES = 200  # 컨텍스트 폭주 방지
GREP_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


def grep(pattern: str, path: str = ".", glob: str = None) -> str:
    """파일 내용에서 정규식을 검색해 'path:줄번호: 내용' 으로 반환. 실패 시 'Error: ...'."""
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
            continue  # 못 읽는 파일(바이너리 등)은 건너뜀
        if truncated:
            break

    if not matches:
        return "(매치 없음)"
    result = "\n".join(matches)
    if truncated:
        result += f"\n... (상위 {GREP_MAX_MATCHES}개만 표시, 잘림)"
    return result


GLOB_MAX_RESULTS = 500  # 컨텍스트 폭주 방지


def glob_files(pattern: str, path: str = ".") -> str:
    """파일명 패턴으로 경로 목록을 검색. ** 재귀 매칭 지원. 없으면 '(매치 없음)'."""
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
    """툴 이름으로 디스패치해서 실제 실행. 결과는 문자열로 반환."""
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

def _summarize(conversation_text: str) -> str:
    """오래된 대화를 압축 요약 (툴 없이 모델 1회 호출). 컨텍스트 관리용."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": (
                "다음 대화를 압축 요약하세요.\n"
                "보존: 아키텍처/설계 결정, 미완성 작업, 에러 상태, 중요한 파일 경로.\n"
                "버릴 것: 반복되는 툴 출력, 중간 확인 메시지.\n\n" + conversation_text
            ),
        }],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")


def run_agent(user_input: str, messages: list = None, system: str = None, session=None) -> str:
    """한 작업을 수행한다. messages 를 넘기면 그 히스토리를 이어서(in-place) 누적한다.

    messages=None 이면 새 리스트로 시작. system 이 있으면 시스템 프롬프트로 전달.
    session 을 넘기면 컨텍스트 압축 발생 시 compaction_count 를 증가시킨다.
    """
    if messages is None:
        messages = []
    messages.append({"role": "user", "content": user_input})

    while True:
        # 매 호출 전 컨텍스트 관리: 70% 초과 시 stripping -> compaction
        managed, did = manage_context(messages, _summarize)
        if did:
            messages[:] = managed
            if session is not None:
                session.compaction_count += 1
            print(f"[context] 컨텍스트 관리 수행 (현재 추정 메시지 {len(messages)}개)")

        kwargs = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "tools": TOOLS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)

        # 모델 응답(텍스트 + tool_use 블록 전체)을 히스토리에 누적
        messages.append({"role": "assistant", "content": response.content})

        # 모델이 더 이상 툴을 호출하지 않고 응답을 마쳤으면 종료
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        # 서버사이드 툴이 반복 한도에 걸렸을 때: 그대로 다시 보내 이어가게 함
        if response.stop_reason == "pause_turn":
            continue

        # tool_use: 호출된 모든 툴을 실행하고 결과를 한 번에 user 메시지로 반환
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    print(f"[tool] {block.name}({json.dumps(block.input, ensure_ascii=False)}) -> {result}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,  # tool_use 블록의 id와 반드시 일치
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # max_tokens, refusal 등 그 외 종료 사유
        raise RuntimeError(f"예상치 못한 stop_reason: {response.stop_reason}")


# ---- 세션 기반 실행 --------------------------------------------------------

DEFAULT_ROLE = "당신은 로컬 파일 시스템 작업을 돕는 자율 에이전트입니다."
DEFAULT_RULES = (
    "- 제공된 툴(read_file, write_file, bash, grep, glob)만 사용한다.\n"
    "- 위험한 셸 커맨드(rm -rf, sudo 등)는 시도하지 않는다.\n"
    "- 추측하지 말고 툴로 사실을 확인한 뒤 답한다."
)
DEFAULT_OUTPUT_FORMAT = "작업 결과를 한국어로 간결하게 요약한다."


def run_session(task: str, session_id: str = None, base_dir: str = "sessions",
                skills_dir: str = "skills"):
    """세션을 이어받거나 새로 만들어 한 작업을 수행하고, 히스토리·progress 를 저장한다.

    구조화된 시스템 프롬프트(ROLE/ENVIRONMENT/TASK CONTEXT/RULES/OUTPUT FORMAT/SKILLS)를
    조립한다. progress 는 TASK CONTEXT 에, 작업과 관련된 스킬 문서는 SKILLS 에 주입된다.

    반환: (Session, 최종 응답 텍스트)
    """
    mgr = SessionManager(base_dir)
    session = mgr.resume_or_new(session_id, tools=[t["name"] for t in TOOLS])

    environment = (
        f"작업 디렉토리: {os.getcwd()}\n"
        f"사용 가능한 툴: {', '.join(t['name'] for t in TOOLS)}"
    )
    system = build_system_prompt(
        role=DEFAULT_ROLE,
        environment=environment,
        task_context=mgr.read_progress(session),       # 이전 세션 진행 기록 (이어받기)
        rules=DEFAULT_RULES,
        output_format=DEFAULT_OUTPUT_FORMAT,
        skills=load_relevant_skills(task, skills_dir),  # 키워드 검색 -> 주입
    )
    session.system_prompt = system  # 기록용

    answer = run_agent(task, messages=session.messages, system=system, session=session)

    # 토큰 수 대략 추정(문자/4) — 정확한 카운팅/컴팩션은 4단계에서
    session.token_count = len(json.dumps(serialize_messages(session.messages), ensure_ascii=False)) // 4
    mgr.save(session)
    mgr.append_progress(session, f"[작업] {task}\n[결과] {answer}")
    return session, answer


if __name__ == "__main__":
    # 같은 session_id 로 다시 실행하면 직전 히스토리·progress 를 이어받는다.
    session, answer = run_session(
        "glob 으로 현재 디렉토리의 .py 파일을 찾아 개수만 알려줘.",
        session_id="demo",
    )
    print(f"\n[session {session.session_id}] {answer}")
