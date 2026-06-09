"""시스템 프롬프트 구조화 + 스킬 로더.

- build_system_prompt: [ROLE]/[ENVIRONMENT]/[TASK CONTEXT]/[RULES]/[OUTPUT FORMAT]/[SKILLS]
  섹션으로 시스템 프롬프트를 조립한다. TASK CONTEXT 는 progress 등 컴팩션 후에도
  살아남아야 하는 정보 자리다 (시스템 프롬프트는 매 호출 재전송되어 압축 영향을 안 받음).
- load_relevant_skills: skills/ 의 .md 문서를 키워드로 검색해 관련 내용만 주입한다.
  RAG(임베딩) 가 아니라 '키워드 검색 -> 컨텍스트 주입' 흐름이다.

스킬 .md 형식(선택): 첫 부분에 `<!-- keywords: a, b, c -->` 주석으로 키워드 지정.
없으면 파일명에서 키워드를 유추한다.
"""

import os
import re


def _parse_skill(path: str):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    name = os.path.splitext(os.path.basename(path))[0]
    m = re.search(r"<!--\s*keywords:\s*(.*?)\s*-->", text, re.IGNORECASE | re.DOTALL)
    if m:
        keywords = [k.strip().lower() for k in re.split(r"[,\n]", m.group(1)) if k.strip()]
    else:  # 파일명에서 유추 (예: python_functions -> ["python", "functions"])
        keywords = [w for w in re.split(r"[_\-\s]+", name.lower()) if w]
    return name, keywords, text.strip()


def load_relevant_skills(query: str, skills_dir: str = "skills", max_skills: int = 3) -> str:
    """query 와 키워드가 겹치는 스킬 문서를 점수순으로 골라 합쳐 반환. 없으면 빈 문자열."""
    if not os.path.isdir(skills_dir):
        return ""
    q = query.lower()
    scored = []
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".md"):
            continue
        name, keywords, text = _parse_skill(os.path.join(skills_dir, fn))
        score = sum(1 for kw in keywords if kw and kw in q)
        if score > 0:
            scored.append((score, name, text))
    scored.sort(key=lambda x: -x[0])
    chunks = [f"### {name}\n{text}" for _, name, text in scored[:max_skills]]
    return "\n\n".join(chunks)


def list_skills(skills_dir: str = "skills"):
    """[(이름, 키워드 리스트, 첫 제목)] 목록을 반환 (슬래시 명령용)."""
    out = []
    if not os.path.isdir(skills_dir):
        return out
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".md"):
            continue
        name, keywords, text = _parse_skill(os.path.join(skills_dir, fn))
        title = ""
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                title = s.lstrip("#").strip()
                break
        out.append((name, keywords, title))
    return out


def get_skill_text(name: str, skills_dir: str = "skills"):
    """이름으로 스킬 내용을 '### 이름\\n{본문}' 형식으로 반환. 없으면 None."""
    path = os.path.join(skills_dir, name + ".md")
    if not os.path.isfile(path):
        return None
    _, _, text = _parse_skill(path)
    return f"### {name}\n{text}"


def build_system_prompt(*, role: str, environment: str, task_context: str = "",
                        rules: str = "", output_format: str = "", skills: str = "") -> str:
    """구조화된 시스템 프롬프트 조립. 빈 섹션은 생략한다."""
    sections = [("ROLE & IDENTITY", role), ("ENVIRONMENT", environment)]
    if task_context:
        sections.append(("TASK CONTEXT", task_context))
    if rules:
        sections.append(("RULES", rules))
    if output_format:
        sections.append(("OUTPUT FORMAT", output_format))
    if skills:
        sections.append(("SKILLS", skills))
    return "\n\n".join(f"[{title}]\n{body}" for title, body in sections)
