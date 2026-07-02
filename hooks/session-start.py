"""SessionStart hook with opt-in gating.

Default state: do NOT inject memory-kb context. Only inject if the cwd is
explicitly opted in. This keeps non-bot projects (scratch dirs, one-off
tasks) free of irrelevant context bloat — saves ~5,800 tokens per session
on opted-out projects.

Decision tree: see gating.py — the SINGLE SOURCE of the opt-in tree, shared
with the capture hooks (session-end.py / pre-compact.py) so one marker
controls the whole subsystem.

When injecting, the context includes:
  - Today's date
  - Knowledge Base Index (master catalog)
  - Last N lines of today's / yesterday's daily log
  - Last 3 compound notes in full (recency bias)
  - Tag-matched compound summaries from CLAUDE.md keyword scan
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gating import is_opted_in, mark_project_answered

# Paths relative to memory-kb project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
COMPOUNDS_DIR = KNOWLEDGE_DIR / "compounds"

MAX_CONTEXT_CHARS = 20_000
MAX_LOG_LINES = 30
RECENT_COMPOUND_COUNT = 3
TAG_MATCH_COMPOUND_COUNT = 5


def read_hook_input() -> dict:
    """Read JSON from stdin (Claude Code passes session metadata). Returns {} on failure."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def parse_cwd(hook_input: dict) -> Path:
    """Extract cwd from hook input. Falls back to actual cwd if not provided."""
    cwd_str = hook_input.get("cwd") or os.getcwd()
    return Path(cwd_str).resolve()


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday). Last N lines."""
    today = datetime.now(timezone.utc).astimezone()
    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)
    return "(no recent daily log)"


def get_recent_compounds(n: int) -> list[tuple[str, str]]:
    """Return the N most recently-modified compound notes as (slug, content) tuples."""
    if not COMPOUNDS_DIR.is_dir():
        return []
    compounds = sorted(
        COMPOUNDS_DIR.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    out: list[tuple[str, str]] = []
    for path in compounds[:n]:
        try:
            content = path.read_text(encoding="utf-8")
            slug = path.stem
            out.append((slug, content))
        except Exception:
            continue
    return out


def get_tag_matched_compounds(
    cwd: Path, count: int, exclude_slugs: set
) -> list[tuple[str, str]]:
    """Find compound notes whose tags match keywords in cwd's CLAUDE.md.

    Quick keyword scan — splits CLAUDE.md into words, compares against compound
    frontmatter tags. Not LLM-based. Cheap.
    """
    claude_md = cwd / "CLAUDE.md"
    if not claude_md.is_file():
        return []
    try:
        keywords = set(
            re.findall(
                r"[a-z][a-z0-9-]{2,}", claude_md.read_text(encoding="utf-8").lower()
            )
        )
    except Exception:
        return []
    if not COMPOUNDS_DIR.is_dir():
        return []
    candidates: list[tuple[int, str, str]] = []  # (match_count, slug, summary)
    for path in COMPOUNDS_DIR.rglob("*.md"):
        if path.stem in exclude_slugs:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        # Pull tag list from frontmatter
        tags_match = re.search(r"^tags:\s*\[([^\]]*)\]", text, re.MULTILINE)
        tags = []
        if tags_match:
            tags = [t.strip().strip("\"'") for t in tags_match.group(1).split(",")]
        match_count = sum(1 for t in tags if t.lower() in keywords)
        if match_count == 0:
            continue
        # Pull problem_one_line for summary
        summary_match = re.search(
            r'^problem_one_line:\s*"?([^"\n]+)"?', text, re.MULTILINE
        )
        summary = summary_match.group(1).strip() if summary_match else "(no summary)"
        candidates.append((match_count, path.stem, summary))
    candidates.sort(key=lambda c: -c[0])
    return [(slug, summary) for _, slug, summary in candidates[:count]]


def build_context(cwd: Path, first_time: bool) -> str:
    """Assemble the SessionStart context to inject."""
    parts = []
    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    if first_time:
        parts.append(
            "## memory-kb first-time prompt\n\n"
            "memory-kb is not enabled for this project. To opt in, run "
            "`/saiyan:memory enable`. To permanently dismiss, run "
            "`/saiyan:memory disable`. (This prompt only appears once per project.)"
        )
        return "\n\n---\n\n".join(parts)

    # Index dump policy: small indexes (<2000 chars) inline; larger indexes
    # surface as a count + search-pointer to avoid duplicating MEMORY.md.
    # Full text is searchable via mcp memory-kb / FTS5 when needed.
    if INDEX_FILE.exists():
        idx_text = INDEX_FILE.read_text(encoding="utf-8")
        if len(idx_text) <= 2000:
            parts.append(f"## Knowledge Base Index\n\n{idx_text}")
        else:
            article_count = sum(
                1 for ln in idx_text.splitlines() if ln.lstrip().startswith("| [[")
            )
            parts.append(
                f"## Knowledge Base\n\n{article_count} articles indexed at "
                f"`{INDEX_FILE}`. Query via mcp memory-kb FTS5 search when a "
                f"specific topic is needed (do not always-load)."
            )
    else:
        parts.append("## Knowledge Base Index\n\n(empty — no articles yet)")

    parts.append(f"## Recent Daily Log\n\n{get_recent_log()}")

    recent = get_recent_compounds(RECENT_COMPOUND_COUNT)
    if recent:
        section = "## Recent Compounds (most-recent problem→solution receipts)\n\n"
        for slug, content in recent:
            section += f"### [[compounds/{slug}]]\n\n{content}\n\n---\n\n"
        parts.append(section.rstrip("\n-").rstrip())

    exclude = {slug for slug, _ in recent}
    matched = get_tag_matched_compounds(cwd, TAG_MATCH_COMPOUND_COUNT, exclude)
    if matched:
        section = "## Tag-Matched Compounds (relevant to this project's CLAUDE.md keywords)\n\n"
        for slug, summary in matched:
            section += f"- [[compounds/{slug}]] — {summary}\n"
        parts.append(section)

    context = "\n\n---\n\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"
    return context


SNAPSHOT_MAX_AGE_HOURS = 24.0


def _project_id(cwd: Path) -> str:
    s = str(cwd)
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s.replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")


def read_recent_snapshot(cwd: Path) -> str:
    """Return last_compact_snapshot.md content if newer than SNAPSHOT_MAX_AGE_HOURS,
    else an empty string. Reads from per-project auto-memory dir, NOT memory-kb.
    Independent of memory-kb opt-in gating — a recent compaction snapshot is
    always relevant to the resuming session."""
    pid = _project_id(cwd)
    if not pid:
        return ""
    snap = (
        Path.home()
        / ".claude"
        / "projects"
        / pid
        / "memory"
        / "last_compact_snapshot.md"
    )
    if not snap.is_file():
        return ""
    try:
        now = datetime.now(timezone.utc).timestamp()
        age_hours = (now - snap.stat().st_mtime) / 3600.0
    except Exception:
        return ""
    if age_hours > SNAPSHOT_MAX_AGE_HOURS:
        return ""
    try:
        text = snap.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text.strip()


def main() -> int:
    hook_input = read_hook_input()
    cwd = parse_cwd(hook_input)

    snapshot = read_recent_snapshot(cwd)

    inject, reason = is_opted_in(cwd)
    is_first_time_prompt = (not inject) and (reason == "first-time prompt")

    memory_kb_context = ""
    if inject or is_first_time_prompt:
        memory_kb_context = build_context(cwd, first_time=is_first_time_prompt)
        if is_first_time_prompt:
            mark_project_answered(cwd)

    parts = [p for p in (snapshot, memory_kb_context) if p and p.strip()]
    additional = "\n\n---\n\n".join(parts)

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
