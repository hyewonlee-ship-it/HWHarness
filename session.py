"""세션 관리 — messages 히스토리 JSON 저장/로드 + progress.txt.

- session_id 기반으로 파일을 관리한다 (sessions/<id>.json).
- 시작 시 기존 세션을 이어받을 수 있다 (resume_or_new).
- progress.txt(sessions/<id>.progress.txt)에 매 작업 요약을 누적하고,
  다음 세션 시작 시 읽어와 컨텍스트로 주입한다.

assistant 메시지의 content 는 Anthropic SDK 블록 객체(Pydantic)일 수 있으므로
저장 시 dict 로 직렬화한다. dict 형태는 그대로 다시 API 에 보낼 수 있다.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _serialize_content(content):
    """content 가 블록 객체 리스트면 dict 리스트로 변환 (JSON 직렬화 가능하게)."""
    if isinstance(content, list):
        result = []
        for item in content:
            if hasattr(item, "model_dump"):  # Pydantic 블록 객체
                result.append(item.model_dump(mode="json", exclude_none=True))
            else:  # 이미 dict (tool_result 등)
                result.append(item)
        return result
    return content  # 문자열


def serialize_messages(messages):
    """messages 전체를 JSON 직렬화 가능한 dict 리스트로 정규화."""
    return [{"role": m["role"], "content": _serialize_content(m["content"])} for m in messages]


@dataclass
class Session:
    session_id: str
    created_at: str
    messages: list = field(default_factory=list)
    system_prompt: str = ""
    tools: list = field(default_factory=list)
    token_count: int = 0
    compaction_count: int = 0  # 4단계(컨텍스트 관리)에서 사용
    progress_file: str = ""


class SessionManager:
    """세션 JSON + progress 파일을 base_dir 아래에서 관리."""

    def __init__(self, base_dir: str = "sessions"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _json_path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.json")

    def _progress_path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.progress.txt")

    def new_session(self, session_id=None, system_prompt="", tools=None) -> Session:
        sid = session_id or uuid.uuid4().hex[:12]
        return Session(
            session_id=sid,
            created_at=datetime.now(timezone.utc).isoformat(),
            system_prompt=system_prompt,
            tools=tools or [],
            progress_file=self._progress_path(sid),
        )

    def load(self, session_id: str):
        """저장된 세션을 로드. 없으면 None."""
        path = self._json_path(session_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return Session(
            session_id=data["session_id"],
            created_at=data["created_at"],
            messages=data.get("messages", []),
            system_prompt=data.get("system_prompt", ""),
            tools=data.get("tools", []),
            token_count=data.get("token_count", 0),
            compaction_count=data.get("compaction_count", 0),
            progress_file=data.get("progress_file") or self._progress_path(session_id),
        )

    def resume_or_new(self, session_id=None, system_prompt="", tools=None) -> Session:
        """session_id 가 있고 저장본이 있으면 이어받고, 아니면 새로 만든다."""
        if session_id:
            existing = self.load(session_id)
            if existing:
                return existing
        return self.new_session(session_id, system_prompt, tools)

    def save(self, session: Session) -> None:
        """세션을 JSON 으로 저장. in-memory messages 도 dict 로 정규화한다."""
        session.messages = serialize_messages(session.messages)
        data = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "messages": session.messages,
            "system_prompt": session.system_prompt,
            "tools": session.tools,
            "token_count": session.token_count,
            "compaction_count": session.compaction_count,
            "progress_file": session.progress_file,
        }
        with open(self._json_path(session.session_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def append_progress(self, session: Session, text: str) -> None:
        """progress 파일에 타임스탬프와 함께 한 항목 추가."""
        ts = datetime.now(timezone.utc).isoformat()
        with open(session.progress_file, "a", encoding="utf-8") as f:
            f.write(f"## {ts}\n{text}\n\n")

    def read_progress(self, session: Session) -> str:
        """progress 파일 전체를 읽어 반환 (없으면 빈 문자열)."""
        if session.progress_file and os.path.exists(session.progress_file):
            with open(session.progress_file, encoding="utf-8") as f:
                return f.read().strip()
        return ""
