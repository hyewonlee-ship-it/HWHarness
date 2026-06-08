# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`HWHarness` is a Python harness for a Claude-based agent loop. The single entry point is `agent.py`.

## Running

```bash
pip install anthropic           # only dependency
cp .env.example .env            # then paste your proxy token into ANTHROPIC_AUTH_TOKEN
python agent.py
```

`agent.py` auto-loads `.env` at startup (a tiny stdlib loader, no `python-dotenv`). Shell-exported env vars take precedence over `.env`.

## Configuration (company proxy pass-through)

The client authenticates to the **company AI proxy** (a multi-provider gateway) with a Bearer token; the proxy passes the request through to Anthropic. Both values come from environment variables (via `.env` or shell export) — never hardcoded — and `agent.py` exits with a clear message if either is missing:

- `ANTHROPIC_BASE_URL` — `https://aiproxy-api.backoffice.bagelgames.com/anthropic`. The `/anthropic` suffix is required: the SDK appends `/v1/messages`, so the real endpoint is `POST .../anthropic/v1/messages`. (The gateway also exposes `/openai/...`, `/google/...`, etc. — verify routes via its OpenAPI spec at `/api-json`.)
- `ANTHROPIC_AUTH_TOKEN` — the company AI proxy token (`aiproxy_...`). Constructed via `anthropic.Anthropic(base_url=..., auth_token=...)`, so the SDK sends `Authorization: Bearer <token>` instead of `x-api-key`.

Do **not** set `ANTHROPIC_API_KEY` in proxy mode — if both `x-api-key` and `Authorization` headers are sent, the request may be rejected.

Do not pass `base_url=` with a hardcoded URL literal in code — the PostToolUse hook (`.claude/hooks/check_agent.py`) blocks it. Read it from `ANTHROPIC_BASE_URL`.

## Architecture

`agent.py` is a manual agentic loop (not the SDK tool runner) — chosen so tool execution can be intercepted and logged:

- **History**: `messages` accumulates the full conversation. Each turn appends the assistant's entire `response.content` (text **and** `tool_use` blocks), not just extracted text — dropping `tool_use` blocks breaks the next request.
- **Loop control** keys off `response.stop_reason`:
  - `end_turn` → extract final text and return.
  - `tool_use` → run every called tool, append results as one `user` message, continue. Each `tool_result.tool_use_id` must match the originating `tool_use` block's id.
  - `pause_turn` → re-send unchanged to let a server-side tool continue.
- **Tools**: declared in `TOOLS` (JSON schema); dispatched by name in `execute_tool()`. Add a new tool by extending both. Current tools: `read_file`, `write_file`, `bash` (30s timeout + dangerous-command blocklist), `grep` (regex over file contents), `glob` (filename patterns).

## Session management (`session.py`)

`run_session(task, session_id=...)` wraps `run_agent` with persistence:

- **Storage**: `sessions/<id>.json` (full message history) + `sessions/<id>.progress.txt` (one timestamped entry per task). The `sessions/` dir is gitignored.
- **Resume**: re-running with the same `session_id` loads prior `messages` and continues; `read_progress` is injected into the system prompt as prior-session context.
- **Serialization**: assistant `content` holds SDK block objects (Pydantic). `serialize_messages()` converts them to dicts via `.model_dump()` before saving — dict-form content is re-sendable to the API as-is. `SessionManager.save()` also normalizes in-memory `messages` to dicts.
- `Session` dataclass carries `token_count` / `compaction_count` fields used by context management.

## Context management (`context.py`)

Client-side, because the pinned model (`claude-haiku-4-5`, 200K) has no server-side compaction and calls go through the pass-through proxy. `run_agent` calls `manage_context()` before every model call:

- **Trigger**: `should_compact()` fires at 70% of the 200K window (`estimate_tokens` = chars/4 heuristic).
- **Escalation**: first `strip_old_tool_results()` (blanks old `tool_result` *contents* but keeps the blocks + `tool_use_id`, so pairing stays valid); if still over threshold, `compact_context()` summarizes the older portion via a Haiku call (`_summarize` in `agent.py`) and keeps `[summary] + recent tail`.
- **Safe boundary (critical)**: the retained tail must start at a "clean head" — a `user` turn with **string** content (a fresh task), never a `tool_result`. This prevents orphaned `tool_result` blocks that would 400. See `_clean_head_index`.
- `compaction_count` on the session increments whenever context is managed.
- All functions are pure or take an injected `summarize` callable, so they're tested without network. Server-side `clear_tool_uses` / `compact_*` betas are the alternative but aren't used here (model + proxy portability).

## System prompt & skills (`skills.py`)

`run_session` assembles a structured system prompt via `build_system_prompt()` with sections `[ROLE & IDENTITY] [ENVIRONMENT] [TASK CONTEXT] [RULES] [OUTPUT FORMAT] [SKILLS]` (empty sections are omitted). Prior-session `progress` goes into `[TASK CONTEXT]` — the system prompt is resent every call, so it survives compaction.

`load_relevant_skills(query, skills_dir="skills")` is keyword search → injection (not RAG/embeddings):

- Each skill is a `.md` file in `skills/`. Declare matching terms with `<!-- keywords: a, b, c -->` near the top; otherwise keywords are derived from the filename.
- Scored by how many keywords appear in the task text; top matches are concatenated into `[SKILLS]`. No match → no injection.
- Add a skill by dropping a new `.md` into `skills/` — no code change.

## Conventions

- Model is pinned in the `MODEL` constant (`claude-haiku-4-5`). Change it there, not inline. A PostToolUse hook (`.claude/hooks/check_agent.py`) blocks edits that change `MODEL` to anything else — update this convention first if the pin must change.
- `MAX_TOKENS` is 16000 (non-streaming default). Switch to streaming before raising it substantially.
