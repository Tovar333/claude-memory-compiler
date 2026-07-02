"""Shared opt-in gate for memory-kb hooks — the single source of gating truth.

Injection (session-start) and capture (session-end / pre-compact) read the SAME
opt-in state: one marker controls the whole subsystem, default OFF. A project's
sessions are neither captured into the shared KB nor given KB context unless the
project is explicitly opted in. Capture callers pass allow_prompt=False (there is
no one to prompt at session end / pre-compact), so an unanswered project simply
skips instead of triggering the first-time prompt.

Decision tree (first match wins):
  1. MEMORY_KB_SKIP=1 env var          -> skip (one-shot disable)
  2. MEMORY_KB_FORCE=1 env var         -> opt in (one-shot enable)
  3. .claude/memory-kb.disabled in cwd -> skip
  4. .claude/memory-kb.enabled in cwd  -> opt in
  5. Walk ancestor dirs for markers    -> first marker found wins
  6. Cwd in global allow-list          -> opt in (.enabled-paths.txt, optional)
  7. First-time auto-prompt            -> (allow_prompt callers only) ask once
  8. Default                           -> skip
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_LIST = ROOT / ".enabled-paths.txt"
ANSWERED_LIST = ROOT / ".answered-projects.txt"


def is_opted_in(cwd: Path, allow_prompt: bool = True) -> tuple[bool, str]:
    """Apply the decision tree. Returns (opted_in, reason)."""
    # 1. Env var: skip
    if os.environ.get("MEMORY_KB_SKIP"):
        return False, "MEMORY_KB_SKIP env var set"
    # 2. Env var: force
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
    # 7. First-time auto-prompt (injection only — capture has no one to ask)
    if allow_prompt and not project_already_answered(cwd):
        return False, "first-time prompt"
    # 8. Default
    return False, "default skip"


def project_already_answered(cwd: Path) -> bool:
    """Whether the first-time prompt was already shown for this project."""
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
