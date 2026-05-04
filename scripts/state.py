"""state.py — project- and session-level state management for memory-kb.

Implements the simple file-marker subcommands the memory-compiler skill exposes.
None of these touch the LLM — they're all marker-file mutations.

Subcommands:
    enable          Create .claude/memory-kb.enabled in cwd
    disable         Create .claude/memory-kb.disabled in cwd
    tags <list>     Write expected-tags.txt to cwd's .claude/
    pause           Write session-state pause marker (current SESSION_ID env var)
    resume          Clear session pause marker
    exclude-last N  Mark last N messages of session as "do not extract"
    status          Print project + session state

Usage:
    uv run python scripts/state.py <subcommand> [args]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_STATE_DIR = ROOT / ".session-state"


def project_marker(cwd: Path, kind: str) -> Path:
    """Return path to the project-level marker file (cwd/.claude/memory-kb.<kind>)."""
    assert kind in ("enabled", "disabled")
    return cwd / ".claude" / f"memory-kb.{kind}"


def session_marker(session_id: str, kind: str) -> Path:
    """Return path to the session-level marker file."""
    SESSION_STATE_DIR.mkdir(exist_ok=True)
    return SESSION_STATE_DIR / f"{session_id}.{kind}"


def cmd_enable() -> int:
    cwd = Path.cwd()
    enabled = project_marker(cwd, "enabled")
    disabled = project_marker(cwd, "disabled")
    enabled.parent.mkdir(parents=True, exist_ok=True)
    enabled.touch()
    if disabled.exists():
        disabled.unlink()
    sys.stdout.write(f"memory-kb enabled for {cwd}\n")
    sys.stdout.write(f"  marker: {enabled}\n")
    return 0


def cmd_disable() -> int:
    cwd = Path.cwd()
    enabled = project_marker(cwd, "enabled")
    disabled = project_marker(cwd, "disabled")
    disabled.parent.mkdir(parents=True, exist_ok=True)
    disabled.touch()
    if enabled.exists():
        enabled.unlink()
    sys.stdout.write(f"memory-kb disabled for {cwd}\n")
    sys.stdout.write(f"  marker: {disabled}\n")
    return 0


def cmd_tags(tag_list: str) -> int:
    cwd = Path.cwd()
    tags_file = cwd / ".claude" / "memory-kb-tags.txt"
    tags_file.parent.mkdir(parents=True, exist_ok=True)
    cleaned = ",".join(t.strip() for t in tag_list.split(",") if t.strip())
    tags_file.write_text(cleaned + "\n", encoding="utf-8")
    sys.stdout.write(f"memory-kb expected tags for {cwd}: {cleaned}\n")
    return 0


def get_session_id() -> str:
    """Best-effort session id. Claude Code sets SESSION_ID env var; falls back to 'unknown'."""
    return (
        os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("SESSION_ID") or "unknown"
    )


def cmd_pause() -> int:
    sid = get_session_id()
    marker = session_marker(sid, "paused")
    marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    sys.stdout.write(f"memory-kb capture paused for session {sid}\n")
    sys.stdout.write(f"  marker: {marker}\n")
    return 0


def cmd_resume() -> int:
    sid = get_session_id()
    marker = session_marker(sid, "paused")
    if marker.exists():
        marker.unlink()
        sys.stdout.write(f"memory-kb capture resumed for session {sid}\n")
    else:
        sys.stdout.write(f"memory-kb was not paused for session {sid} (no-op)\n")
    return 0


def cmd_exclude_last(n: int) -> int:
    sid = get_session_id()
    marker = session_marker(sid, "exclude-last")
    marker.write_text(str(int(n)) + "\n", encoding="utf-8")
    sys.stdout.write(f"memory-kb will exclude last {n} messages from session {sid}\n")
    return 0


def cmd_status() -> int:
    cwd = Path.cwd()
    sid = get_session_id()
    enabled = project_marker(cwd, "enabled")
    disabled = project_marker(cwd, "disabled")
    paused_marker = session_marker(sid, "paused")
    exclude_marker = session_marker(sid, "exclude-last")
    tags_file = cwd / ".claude" / "memory-kb-tags.txt"

    sys.stdout.write(f"memory-kb status for cwd={cwd}\n")
    if disabled.exists():
        sys.stdout.write("  project: DISABLED (explicit)\n")
    elif enabled.exists():
        sys.stdout.write("  project: ENABLED (explicit)\n")
    else:
        # Walk parents
        ancestor_marker = None
        for parent in cwd.parents:
            cand = parent / ".claude" / "memory-kb.enabled"
            if cand.exists():
                ancestor_marker = cand
                break
        if ancestor_marker:
            sys.stdout.write(
                f"  project: ENABLED (inherited from {ancestor_marker.parent.parent})\n"
            )
        else:
            sys.stdout.write("  project: not set (default = OFF)\n")

    sys.stdout.write(f"  session id: {sid}\n")
    if paused_marker.exists():
        sys.stdout.write(
            f"  session: PAUSED since {paused_marker.read_text().strip()}\n"
        )
    else:
        sys.stdout.write("  session: active (capture will run on session-end)\n")

    if exclude_marker.exists():
        n = exclude_marker.read_text().strip()
        sys.stdout.write(f"  exclude-last: {n} messages\n")

    if tags_file.exists():
        sys.stdout.write(f"  expected tags: {tags_file.read_text().strip()}\n")
    else:
        sys.stdout.write(
            "  expected tags: (none set — auto-derived from CLAUDE.md keywords)\n"
        )

    # KB stats
    kb = ROOT / "knowledge"
    if kb.is_dir():
        for sub in ("concepts", "connections", "qa", "compounds", "debugging"):
            count = len(list((kb / sub).rglob("*.md"))) if (kb / sub).is_dir() else 0
            sys.stdout.write(f"  knowledge/{sub}: {count}\n")
        index = kb / "index.md"
        if index.exists():
            mtime = datetime.fromtimestamp(index.stat().st_mtime).isoformat(
                timespec="seconds"
            )
            sys.stdout.write(f"  knowledge/index.md last updated: {mtime}\n")
        else:
            sys.stdout.write("  knowledge/index.md: missing\n")

    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="memory-kb state management")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("enable")
    sub.add_parser("disable")
    p_tags = sub.add_parser("tags")
    p_tags.add_argument("tag_list", help="comma-separated tag list")
    sub.add_parser("pause")
    sub.add_parser("resume")
    p_excl = sub.add_parser("exclude-last")
    p_excl.add_argument("n", type=int)
    sub.add_parser("status")

    args = parser.parse_args()

    if args.cmd == "enable":
        return cmd_enable()
    if args.cmd == "disable":
        return cmd_disable()
    if args.cmd == "tags":
        return cmd_tags(args.tag_list)
    if args.cmd == "pause":
        return cmd_pause()
    if args.cmd == "resume":
        return cmd_resume()
    if args.cmd == "exclude-last":
        return cmd_exclude_last(args.n)
    if args.cmd == "status":
        return cmd_status()
    return 1


if __name__ == "__main__":
    sys.exit(main())
