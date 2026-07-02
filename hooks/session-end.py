"""
SessionEnd hook - captures conversation transcript for memory extraction.

When a Claude Code session ends, this hook reads the transcript path from
stdin, extracts conversation context, and spawns flush.py as a background
process to extract knowledge into the daily log.

Shared-KB capture is OPT-IN per project (gating.py, same state as injection,
default OFF) — the gate sits before any transcript excerpt is extracted, so
non-opted projects never leave content in scripts/ or daily/. The per-project
FTS5 reindex always runs regardless of opt-in.

The hook itself does NO API calls - only local file I/O for speed (<10s).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Recursion guard: if we were spawned by flush.py (which calls Agent SDK,
# which runs Claude Code, which would fire this hook again), exit immediately.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_DIR = SCRIPTS_DIR

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "flush.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [hook] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000
MIN_TURNS_TO_FLUSH = 1


def extract_conversation_context(transcript_path: Path) -> tuple[str, int]:
    """Read JSONL transcript and extract last ~N conversation turns as markdown."""
    turns: list[str] = []

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Assistant"
                turns.append(f"**{label}:** {content.strip()}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1 :]

    return context, len(recent)


def run_kb_capture(hook_input: dict, session_id: str) -> None:
    """Extract the transcript excerpt and spawn flush.py. Opt-in callers only."""
    transcript_path_str = hook_input.get("transcript_path", "")

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        logging.info("SKIP: no transcript path")
        return

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    # Extract conversation context in the hook (fast, no API calls)
    try:
        context, turn_count = extract_conversation_context(transcript_path)
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if not context.strip():
        logging.info("SKIP: empty context")
        return

    if turn_count < MIN_TURNS_TO_FLUSH:
        logging.info("SKIP: only %d turns (min %d)", turn_count, MIN_TURNS_TO_FLUSH)
        return

    # Write context to a temp file for the background process
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = STATE_DIR / f"session-flush-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")

    # Spawn flush.py as a background process
    flush_script = SCRIPTS_DIR / "flush.py"

    cmd = [
        "uv",
        "run",
        "--directory",
        str(ROOT),
        "python",
        str(flush_script),
        str(context_file),
        session_id,
    ]

    # On Windows, use CREATE_NO_WINDOW to avoid flash console window.
    # Do NOT use DETACHED_PROCESS — it breaks the Agent SDK's subprocess I/O.
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        logging.info(
            "Spawned flush.py for session %s (%d turns, %d chars)",
            session_id,
            turn_count,
            len(context),
        )
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)


def main() -> None:
    # Read hook input from stdin
    # Claude Code on Windows may pass paths with unescaped backslashes
    try:
        raw_input = sys.stdin.read()
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            fixed_input = re.sub(r'(?<!\\)\\(?!["\\])', r"\\\\", raw_input)
            hook_input = json.loads(fixed_input)
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s", e)
        return

    session_id = hook_input.get("session_id", "unknown")
    source = hook_input.get("source", "unknown")

    logging.info("SessionEnd fired: session=%s source=%s", session_id, source)

    cwd = Path(hook_input.get("cwd") or os.getcwd()).resolve()

    # Shared-KB capture gate (gating.py = single source, shared with injection).
    # Fails CLOSED: any gate error means no capture.
    capture = False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gating import is_opted_in

        capture, gate_reason = is_opted_in(cwd, allow_prompt=False)
        logging.info("Capture gate: %s (%s) cwd=%s", capture, gate_reason, cwd)
    except Exception as e:
        logging.error("Capture gate error (failing CLOSED, no capture): %s", e)

    if capture:
        run_kb_capture(hook_input, session_id)

    # FTS5 index of per-project auto-memory dir (fast, stdlib-only, in-process).
    # Always runs — per-project service, independent of KB opt-in.
    # Failure here must never block anything else.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from memory_fts_index import memory_dir_for_cwd, reindex

        md = memory_dir_for_cwd(cwd)
        if md.is_dir():
            result = reindex(md)
            logging.info("FTS5 reindex: %s", result)
    except Exception as e:
        logging.error("FTS5 reindex failed: %s", e)


if __name__ == "__main__":
    main()
