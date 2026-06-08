"""컨텍스트 관리 — Compaction(요약 압축) + Stripping(툴 결과 제거).

모델이 claude-haiku-4-5(200K, 서버사이드 compaction 미지원)이고 프록시 pass-through
환경이라 모두 클라이언트사이드로 구현한다. 모든 함수는 순수 함수이거나 summarize
콜백을 주입받으므로 네트워크 없이 테스트할 수 있다.

핵심 안전장치: 압축 후 남기는 tail 은 반드시 '깨끗한 경계'(문자열 content 를 가진
user 턴 = 새 작업 시작)에서 시작하게 한다. 그래야 tool_use 없이 떠도는 tool_result
가 생기지 않아 API 가 400 을 내지 않는다.
"""

import json

from session import serialize_messages

DEFAULT_MAX_CONTEXT = 200_000  # claude-haiku-4-5 컨텍스트 윈도우
DEFAULT_THRESHOLD = 0.70       # 70% 넘으면 트리거
STRIP_PLACEHOLDER = "[이전 툴 결과 생략됨]"


def estimate_tokens(messages: list) -> int:
    """대략적인 토큰 수 추정 (문자 수 / 4). 정확한 카운팅은 count_tokens API."""
    return len(json.dumps(serialize_messages(messages), ensure_ascii=False)) // 4


def should_compact(messages: list, max_context: int = DEFAULT_MAX_CONTEXT,
                   threshold: float = DEFAULT_THRESHOLD) -> bool:
    return estimate_tokens(messages) > max_context * threshold


def _is_clean_head(msg) -> bool:
    """새 작업 경계인가 — 문자열 content 를 가진 user 턴 (tool_result 아님)."""
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def _clean_head_index(messages: list, start: int):
    """start 이후 첫 '깨끗한 경계' 인덱스. 없으면 None."""
    for i in range(start, len(messages)):
        if _is_clean_head(messages[i]):
            return i
    return None


def strip_old_tool_results(messages: list, keep_recent: int = 6) -> list:
    """오래된 tool_result 블록의 내용을 placeholder 로 치환 (구조/ID 는 보존).

    tool_use<->tool_result 짝과 ID 는 그대로 두므로 API 검증이 깨지지 않는다.
    최근 keep_recent 개 메시지는 손대지 않는다.
    """
    cutoff = len(messages) - keep_recent
    out = []
    for i, m in enumerate(messages):
        if i < cutoff and m.get("role") == "user" and isinstance(m.get("content"), list):
            new_content = []
            for blk in m["content"]:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    stripped = dict(blk)
                    stripped["content"] = STRIP_PLACEHOLDER
                    new_content.append(stripped)
                else:
                    new_content.append(blk)
            out.append({"role": m["role"], "content": new_content})
        else:
            out.append(m)
    return out


def compact_context(messages: list, summarize, keep_recent: int = 5) -> list:
    """오래된 부분을 summarize 로 요약하고 [요약] + 최근 tail 로 교체.

    summarize: (대화 텍스트) -> 요약 문자열. tail 은 깨끗한 경계에서 시작.
    """
    cut = max(0, len(messages) - keep_recent)
    head_idx = _clean_head_index(messages, cut)
    if head_idx is None:
        head_idx = len(messages)  # 안전 경계 없음 -> 전부 요약, tail 없음

    older = messages[:head_idx]
    recent = messages[head_idx:]
    if not older:
        return list(messages)  # 요약할 게 없음

    summary = summarize(json.dumps(serialize_messages(older), ensure_ascii=False))
    summary_msg = {"role": "user", "content": f"[이전 컨텍스트 요약]\n{summary}"}
    return [summary_msg] + recent


def manage_context(messages: list, summarize,
                   max_context: int = DEFAULT_MAX_CONTEXT,
                   threshold: float = DEFAULT_THRESHOLD,
                   keep_recent: int = 5):
    """70% 초과 시 stripping -> (여전히 초과면) compaction 으로 에스컬레이션.

    반환: (새 messages, 관리했는지 여부)
    """
    if not should_compact(messages, max_context, threshold):
        return messages, False

    stripped = strip_old_tool_results(messages)
    if not should_compact(stripped, max_context, threshold):
        return stripped, True  # stripping 만으로 충분

    compacted = compact_context(stripped, summarize, keep_recent)
    return compacted, True
