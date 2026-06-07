"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os

os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# On Windows the Agent SDK spawns the bundled claude.exe via anyio.open_process()
# with no window flag. Because this background process has no console of its own
# (the hook launches it with CREATE_NO_WINDOW), Windows would hand that child a
# brand-new VISIBLE console window. Patch anyio so the child is spawned windowless
# too. Covers both the main transport and the SDK's `claude -v` version check.
if sys.platform == "win32":
    import subprocess as _subprocess

    import anyio as _anyio

    _orig_open_process = _anyio.open_process

    async def _no_window_open_process(*args, **kwargs):
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
        )
        return await _orig_open_process(*args, **kwargs)

    _anyio.open_process = _no_window_open_process

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def append_to_daily_log(content: str, section: str = "Session") -> None:
    """Append content to today's daily log."""
    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    entry = f"### {section} ({time_str})\n\n{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def detect_ce_phase(transcript_text: str) -> str:
    """Detect CE workflow phase from transcript markers.

    Returns one of: 'compound', 'debug', 'work', 'plan', 'brainstorm', 'generic'.
    Compound and debug phases route output to dedicated knowledge subdirs;
    others fall through to the generic daily-log flow.
    """
    text = transcript_text.lower()
    explicit_markers = {
        "compound": ["/ce-compound", "ce_compound"],
        "debug": ["/ce-debug", "ce_debug", "wat loop", "root cause:"],
        "work": ["/ce-work", "ce_work"],
        "plan": ["/ce-plan", "ce_plan"],
        "brainstorm": ["/ce-brainstorm", "ce_brainstorm"],
    }
    for phase, needles in explicit_markers.items():
        if any(n in text for n in needles):
            return phase
    # Heuristic: a session with commits + tests + clear problem framing is
    # implicitly compound-worthy even without explicit /ce-compound.
    if all(s in text for s in ("commit", "test", "problem")):
        return "compound"
    return "generic"


GENERIC_FLUSH_PROMPT = """Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK
"""


COMPOUND_EXTRACTION_PROMPT = """Review the conversation context below. This session looks like a complete
work cycle (problem framing → investigation → solution → prevention). Extract
a structured compound-note draft that captures the problem→solution receipt.
Do NOT use any tools — just return plain text.

Tag this entry with `**Phase:** compound` so the compiler routes it to
`knowledge/compounds/`. Format:

**Phase:** compound

**Problem (one-line):** <one sentence>

**Solution (one-line):** <one sentence>

**Tags:** [domain, subsystem, error-type — comma separated]

**Effort hours:** <approximate>

**Problem (detailed):**
<2-4 sentence root-cause description>

**Investigation Path:**
- <chronological: what we tried, what failed, what worked>

**Solution (detailed):**
1. <numbered step>
2. <numbered step>

**Why It Works:**
<1-2 sentences on the underlying mechanism>

**Exact Fix:**
- <file:line refs with what changed>

**Prevention:**
<how to avoid this in future — checklist item, code review rule, etc.>

If the conversation doesn't actually contain a complete problem-solution cycle
(e.g. just exploratory discussion without resolution), respond with exactly:
FLUSH_OK
"""


DEBUG_EXTRACTION_PROMPT = """Review the conversation context below. This session looks like a debugging
trace (symptoms → hypotheses → diagnostic commands → root cause). Extract a
structured debug-trace draft that captures the diagnostic *journey* — useful
when a similar bug returns with different surface symptoms.
Do NOT use any tools — just return plain text.

Tag this entry with `**Phase:** debug` so the compiler routes it to
`knowledge/debugging/`. Format:

**Phase:** debug

**Symptom (one-line):** <what the user/system was seeing>

**Root cause (one-line):** <the actual underlying cause once identified>

**Tags:** [domain, error-type, debugging-technique — comma separated]

**Diagnosis minutes:** <approximate>

**Symptom (detailed):**
<error message, behavior, log line — what was visible>

**Diagnostic Journey:**
- Hypothesis 1: <theory> → outcome (✅/❌)
- Hypothesis 2: <theory> → outcome (✅/❌)
- (continue for each hypothesis tested)

**Key Diagnostic Commands:**
```
<actual commands that helped narrow it down>
```

**Why The Initial Hypotheses Were Wrong:**
<misleading signals that pointed away from the real cause>

**Resolution:**
<pointer to the fix — either inline if small, or "see compounds/<slug>" if separate>

If the conversation doesn't actually contain a real diagnostic journey,
respond with exactly: FLUSH_OK
"""


async def run_flush(context: str, phase: str = "generic") -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context.

    The phase parameter selects which extraction prompt to use:
    - 'compound' → COMPOUND_EXTRACTION_PROMPT (problem→solution receipt)
    - 'debug'    → DEBUG_EXTRACTION_PROMPT (diagnostic trace)
    - other      → GENERIC_FLUSH_PROMPT (existing daily-log behavior)
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    if phase == "compound":
        prompt_template = COMPOUND_EXTRACTION_PROMPT
    elif phase == "debug":
        prompt_template = DEBUG_EXTRACTION_PROMPT
    else:
        prompt_template = GENERIC_FLUSH_PROMPT

    prompt = f"{prompt_template}\n\n## Conversation Context\n\n{context}"

    response = ""

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                # This is a cheap summarization job — pin Haiku so the background
                # flush never inherits the interactive session's flagship model
                # (e.g. ~/.claude/settings.json "model": "opus") and quietly drains
                # the plan budget on every compact / session-end.
                model="haiku",
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
    except Exception as e:
        import traceback

        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    return response


COMPILE_AFTER_HOUR = 18  # 6 PM local time


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    # Check if today's log has already been compiled
    today_log = f"{now.strftime('%Y-%m-%d')}.md"
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                # Already compiled today - check if the log has changed since
                from hashlib import sha256

                log_path = DAILY_DIR / today_log
                if log_path.exists():
                    current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
                    if ingested[today_log].get("hash") == current_hash:
                        return  # log unchanged since last compile
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


def main():
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id>", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]

    logging.info(
        "flush.py started for session %s, context: %s", session_id, context_file
    )

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: skip if same session was flushed within 60 seconds
    state = load_flush_state()
    if (
        state.get("session_id") == session_id
        and time.time() - state.get("timestamp", 0) < 60
    ):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    # Detect CE phase from transcript markers; phase routes the daily-log
    # entry to compounds/, debugging/, or stays generic. compile.py reads
    # the phase tag and creates the appropriate article type.
    phase = detect_ce_phase(context)
    logging.info(
        "Flushing session %s: %d chars, phase=%s", session_id, len(context), phase
    )

    # Run the LLM extraction with the phase-appropriate prompt
    response = asyncio.run(run_flush(context, phase=phase))

    # Append to daily log. Phase-tagged entries get a section header that
    # tells compile.py to route to the right knowledge subdir.
    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush"
        )
    elif "FLUSH_ERROR" in response:
        logging.error("Result: %s", response)
        append_to_daily_log(response, "Memory Flush")
    else:
        section_header = {
            "compound": "Compound (problem→solution receipt)",
            "debug": "Debug Trace (diagnostic journey)",
        }.get(phase, "Session")
        logging.info(
            "Result: saved to daily log (%d chars, phase=%s)", len(response), phase
        )
        append_to_daily_log(response, section_header)

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file
    context_file.unlink(missing_ok=True)

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
