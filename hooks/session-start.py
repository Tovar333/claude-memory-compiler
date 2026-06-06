"""SessionStart hook with opt-in gating.

Default state: do NOT inject memory-kb context. Only inject if the cwd is
explicitly opted in. This keeps non-bot projects (scratch dirs, one-off
tasks) free of irrelevant context bloat — saves ~5,800 tokens per session
on opted-out projects.

Decision tree (first match wins):
  1. MEMORY_KB_SKIP=1 env var          → skip (one-shot disable)
  2. MEMORY_KB_FORCE=1 env var         → inject (one-shot enable)
  3. .claude/memory-kb.disabled in cwd → skip
  4. .claude/memory-kb.enabled in cwd  → inject
  5. Walk ancestor dirs for .enabled   → inject if found
  6. Cwd in global allow-list          → inject
  7. First-time auto-prompt            → ask user, write answer to marker
  8. Default                           → skip (silent fast exit)

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

# Paths relative to memory-kb project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
COMPOUNDS_DIR = KNOWLEDGE_DIR / "compounds"
ALLOW_LIST = ROOT / ".enabled-paths.txt"
ANSWERED_LIST = ROOT / ".answered-projects.txt"

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


def is_opted_in(cwd: Path) -> tuple[bool, str]:
    """Apply the 8-layer decision tree. Returns (should_inject, reason)."""
    # 1. Env var: skip
    if os.environ.get("MEMORY_KB_SKIP"):
        return False, "MEMORY_KB_SKIP env var set"
    # 2. Env var: force inject
    if os.environ.get("MEMORY_KB_FORCE"):
        return True, "MEMORY_KB_FORCE env var set"
    # 3. Explicit opt-out marker in cwd
    if (cwd / ".claude" / "memory-kb.disabled").exists():
        return False, "explicit .claude/memory-kb.disabled in cwd"
    # 4. Explicit opt-in marker in cwd
    if (cwd / ".claude" / "memory-kb.enabled").exists():
        return True, "explicit .claude/memory-kb.enabled in cwd"
    # 5. Ancestor walk
    for parent in cwd.parents:
        if (parent / ".claude" / "memory-kb.enabled").exists():
            return True, f"inherited from ancestor {parent}"
        if (parent / ".claude" / "memory-kb.disabled").exists():
            return False, f"explicit disable in ancestor {parent}"
        # Stop at filesystem root or home
        if parent == Path.home() or parent == parent.parent:
            break
    # 6. Global allow-list
    if ALLOW_LIST.exists():
        for line in ALLOW_LIST.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                if str(cwd).startswith(str(Path(line).resolve())):
                    return True, f"matched global allow-list entry {line}"
            except Exception:
                continue
    # 7. First-time auto-prompt
    if not project_already_answered(cwd):
        # Returning False but caller checks separately whether to inject the prompt-text;
        # we set is_prompt_first_time = True via a side-channel marker file write here
        # (see main() — handles this by inserting a one-shot prompt into context).
        return False, "first-time prompt"
    # 8. Default
    return False, "default skip"


def project_already_answered(cwd: Path) -> bool:
    """Check whether we've already shown the first-time prompt for this project."""
    if not ANSWERED_LIST.exists():
        return False
    try:
        for line in ANSWERED_LIST.read_text(encoding="utf-8").splitlines():
            if line.strip() == str(cwd):
                return True
    except Exception:
        pass
    return False


def mark_project_answered(cwd: Path) -> None:
    """Append cwd to the answered list so the prompt never re-fires."""
    try:
        ANSWERED_LIST.parent.mkdir(parents=True, exist_ok=True)
        with ANSWERED_LIST.open("a", encoding="utf-8") as f:
            f.write(str(cwd) + "\n")
    except Exception:
        pass


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
