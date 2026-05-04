"""Write a structured PreCompact resume snapshot for the per-project memory dir.

Inspired by context-mode's priority-ranked XML snapshot. We use markdown for
human-readability and aggregate optional plugin contributors so domain plugins
(like trading-bot-plugin) can register their own digest blocks without saiyan
ever importing their code.

Output path: ~/.claude/projects/<project-id>/memory/last_compact_snapshot.md
Hard cap: ~2 KB total. Sections truncate proportionally if over.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HARD_CAP_CHARS = 2000
PLUGIN_CONTRIB_GLOB = "*/precompact/contributors/*.py"
WORK_INTAKE_WINDOW_DAYS = 7


def _project_id(cwd: Path) -> str:
    s = str(cwd)
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s.replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")


def _memory_dir(cwd: Path) -> Path:
    return Path.home() / ".claude" / "projects" / _project_id(cwd) / "memory"


def _last_user_request(transcript_path: str) -> str:
    """Walk the JSONL transcript backwards to find the most recent user message."""
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or entry
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        return text
                if isinstance(block, str) and block.strip():
                    return block.strip()
        elif isinstance(content, str):
            if content.strip():
                return content.strip()
    return ""


def _section_last_request(transcript_path: str) -> tuple[str, str]:
    text = _last_user_request(transcript_path)
    if not text:
        return ("Last user request", "_(could not read transcript)_")
    if len(text) > 600:
        text = text[:600].rstrip() + " …"
    return ("Last user request", text)


def _section_in_flight_work(cwd: Path) -> tuple[str, str]:
    parts: list[str] = []

    intake = cwd / "docs" / "work_intake"
    if intake.is_dir():
        cutoff = (
            datetime.now(timezone.utc).timestamp() - WORK_INTAKE_WINDOW_DAYS * 86400
        )
        recent = [p for p in intake.glob("*.md") if p.stat().st_mtime >= cutoff]
        recent.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if recent:
            parts.append("Recent work-intake files:")
            for p in recent[:5]:
                parts.append(f"- {p.name}")

    handoff = _memory_dir(cwd) / "session_handoff.md"
    if handoff.is_file():
        try:
            text = handoff.read_text(encoding="utf-8", errors="replace")
            m = re.search(
                r"(?im)^#{1,4}\s*next session priorities.*?$(.*?)(?=^#{1,4}\s|\Z)",
                text,
                re.DOTALL,
            )
            if m:
                snippet = m.group(1).strip()
                if len(snippet) > 400:
                    snippet = snippet[:400].rstrip() + " …"
                parts.append("Handoff next-session priorities:")
                parts.append(snippet)
        except Exception:
            pass

    plugin_blocks = _plugin_contributors(cwd)
    for label, block in plugin_blocks:
        parts.append(f"Plugin contributor — {label}:")
        parts.append(block)

    body = "\n".join(parts) if parts else "_(no in-flight artifacts found)_"
    return ("In-flight work", body)


def _plugin_contributors(cwd: Path) -> list[tuple[str, str]]:
    plugins_dir = Path.home() / ".claude" / "plugins"
    if not plugins_dir.is_dir():
        return []
    try:
        paths = sorted(plugins_dir.glob(PLUGIN_CONTRIB_GLOB))
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for path in paths:
        try:
            spec = importlib.util.spec_from_file_location(
                f"saiyan_contrib_{path.stem}", path
            )
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            render = getattr(mod, "render", None)
            if not callable(render):
                continue
            block = render({"cwd": str(cwd)})
            if isinstance(block, str) and block.strip():
                # 4-up segments (plugin name from path layout: <plugin>/precompact/contributors/<seg>.py)
                plugin_name = path.parts[-4] if len(path.parts) >= 4 else "plugin"
                out.append((f"{plugin_name}:{path.stem}", block.strip()))
        except Exception:
            continue
    return out


def _section_standing_rules(memory_dir: Path) -> tuple[str, str]:
    """Top 3 feedback_*.md by mtime — proxy for 'recently in play'."""
    if not memory_dir.is_dir():
        return ("Standing rules in play", "_(no memory dir)_")
    try:
        feedback_files = sorted(
            memory_dir.glob("feedback_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:3]
    except Exception:
        feedback_files = []
    if not feedback_files:
        return ("Standing rules in play", "_(no feedback memories)_")

    bullets = []
    for p in feedback_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Pull `description: ...` from frontmatter, fall back to first H1
        desc = ""
        fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if fm:
            for line in fm.group(1).splitlines():
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
        if not desc:
            h = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            if h:
                desc = h.group(1).strip()
        bullets.append(f"- `{p.name}` — {desc[:120]}")
    return ("Standing rules in play", "\n".join(bullets))


def _section_saiyan_state(cwd: Path) -> tuple[str, str]:
    parts: list[str] = []

    stage_file = Path.home() / ".claude" / ".saiyan-stage"
    if stage_file.is_file():
        try:
            stage = stage_file.read_text(
                encoding="utf-8-sig", errors="replace"
            ).strip()[:1]
        except Exception:
            stage = ""
        if stage:
            parts.append(f"Saiyan stage: **{stage}**")

    try:
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        out = subprocess.run(
            ["git", "-C", str(cwd), "--no-optional-locks", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=1.5,
            creationflags=creation_flags,
        )
        modified = [
            line[3:].strip() for line in out.stdout.splitlines() if line.strip()
        ][:3]
        if modified:
            parts.append("Recent edits (git status):")
            for f in modified:
                parts.append(f"- {f}")
    except Exception:
        pass

    body = "\n".join(parts) if parts else "_(no saiyan state recorded)_"
    return ("Saiyan stage + recent edits", body)


def _assemble(sections: list[tuple[str, str]], max_chars: int) -> str:
    rendered = "\n\n".join(f"## {title}\n{body}" for title, body in sections)
    if len(rendered) <= max_chars:
        return rendered

    # Proportional truncation if over budget. Trim each section's body but keep its title.
    overshoot = len(rendered) - max_chars
    bodies = [body for _, body in sections]
    total_body = sum(len(b) for b in bodies)
    if total_body == 0:
        return rendered[:max_chars]

    new_sections: list[tuple[str, str]] = []
    for title, body in sections:
        share = int(len(body) * overshoot / total_body)
        new_len = max(80, len(body) - share)
        if new_len < len(body):
            body = body[:new_len].rstrip() + " …"
        new_sections.append((title, body))
    return "\n\n".join(f"## {title}\n{body}" for title, body in new_sections)


def write_snapshot(hook_input: dict) -> Path | None:
    """Public entry point. Writes the snapshot file and returns its path, or None on no-op."""
    cwd_str = hook_input.get("cwd") or os.getcwd()
    cwd = Path(cwd_str).resolve()
    memory_dir = _memory_dir(cwd)
    if not memory_dir.is_dir():
        # No project memory dir → snapshot has no place to live.
        return None

    transcript_path = hook_input.get("transcript_path", "")
    sections = [
        _section_last_request(transcript_path),
        _section_in_flight_work(cwd),
        _section_standing_rules(memory_dir),
        _section_saiyan_state(cwd),
    ]

    body = _assemble(sections, max_chars=HARD_CAP_CHARS)
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    header = f"# Resume snapshot from compaction at {ts}\n\n"

    out_path = memory_dir / "last_compact_snapshot.md"
    out_path.write_text(header + body, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    # Allow CLI testing: read JSON from stdin and write the snapshot.
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if "cwd" not in data:
        data["cwd"] = os.getcwd()
    result = write_snapshot(data)
    sys.stdout.write(
        json.dumps({"ok": result is not None, "path": str(result) if result else None})
    )
