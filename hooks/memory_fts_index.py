"""FTS5 indexer + searcher for the per-project auto-memory directory.

Indexes every *.md file under ~/.claude/projects/<project-id>/memory/ into a
SQLite FTS5 database at .../memory/.fts5.db. Markdown remains the source of
truth; the database is a derived read-only mirror that can be rebuilt from
scratch at any time. Honors the single-writer rule.

Three CLI modes:

    python memory_fts_index.py --reindex            (default — used by SessionEnd)
    python memory_fts_index.py --query "<text>"     [--limit N] [--cwd PATH]
    python memory_fts_index.py --stats

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Allow `from fts5_index import FTS5Index` whether the script is run directly,
# imported, or invoked through the saiyan plugin venv.
_LIB = Path(__file__).resolve().parent.parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from fts5_index import FTS5Index  # noqa: E402

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def cwd_to_project_id(cwd: Path) -> str:
    """Convert a cwd into Claude Code's project-directory id encoding.

    Matches the encoding Claude Code itself uses for ~/.claude/projects/<id>/.
    Example: C:\\Users\\Colan\\OneDrive\\Desktop\\Trading bot →
             c--Users-Colan-OneDrive-Desktop-Trading-bot
    """
    s = str(cwd)
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    s = s.replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    return s


def memory_dir_for_cwd(cwd: Path) -> Path:
    return CLAUDE_PROJECTS_DIR / cwd_to_project_id(cwd) / "memory"


def extract_title(text: str, fallback: str) -> str:
    """Pull a one-line title from frontmatter `name:` or the first H1/H2 heading."""
    fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if line.lower().startswith("name:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    h_match = re.search(r"^#{1,2}\s+(.+)$", text, re.MULTILINE)
    if h_match:
        return h_match.group(1).strip()
    return fallback


def reindex(memory_dir: Path) -> dict:
    if not memory_dir.is_dir():
        return {"ok": False, "reason": f"not a directory: {memory_dir}"}

    db_path = memory_dir / ".fts5.db"
    written = 0
    skipped = 0
    valid_keys: set[str] = set()

    with FTS5Index(db_path) as idx:
        for md in memory_dir.rglob("*.md"):
            try:
                rel = md.relative_to(memory_dir).as_posix()
            except ValueError:
                continue
            valid_keys.add(rel)
            try:
                stat = md.stat()
                text = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            title = extract_title(text, fallback=md.stem)
            if idx.upsert(
                rel,
                title=title,
                body=text,
                source_path=str(md),
                mtime=stat.st_mtime,
            ):
                written += 1
            else:
                skipped += 1
        deleted = idx.delete_missing(valid_keys)
        stats = idx.stats()

    return {
        "ok": True,
        "memory_dir": str(memory_dir),
        "db_path": str(db_path),
        "written": written,
        "skipped_unchanged": skipped,
        "deleted": deleted,
        "doc_count": stats["doc_count"],
        "db_bytes": stats["db_bytes"],
    }


def query(memory_dir: Path, q: str, limit: int) -> dict:
    db_path = memory_dir / ".fts5.db"
    if not db_path.exists():
        return {"ok": False, "reason": f"no index at {db_path}; run --reindex first"}
    with FTS5Index(db_path) as idx:
        hits = idx.search(q, limit=limit)
    return {
        "ok": True,
        "query": q,
        "count": len(hits),
        "hits": [
            {
                "key": h.key,
                "title": h.title,
                "rank": round(h.rank, 3),
                "snippet": h.snippet,
                "source_path": h.source_path,
            }
            for h in hits
        ],
    }


def stats(memory_dir: Path) -> dict:
    db_path = memory_dir / ".fts5.db"
    if not db_path.exists():
        return {"ok": False, "reason": f"no index at {db_path}"}
    with FTS5Index(db_path) as idx:
        s = idx.stats()
    return {"ok": True, "memory_dir": str(memory_dir), **s}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="FTS5 indexer for per-project memory.")
    p.add_argument("--cwd", default=os.getcwd(), help="Working dir (defaults to cwd)")
    p.add_argument("--reindex", action="store_true", help="Rebuild the FTS5 index")
    p.add_argument("--query", "-q", default=None, help="Search the index")
    p.add_argument("--limit", "-n", type=int, default=5, help="Max results (default 5)")
    p.add_argument("--stats", action="store_true", help="Print index stats")
    p.add_argument("--json", action="store_true", help="Emit raw JSON, not markdown")
    args = p.parse_args(argv)

    memory_dir = memory_dir_for_cwd(Path(args.cwd).resolve())

    if args.query:
        result = query(memory_dir, args.query, args.limit)
        if args.json or not result.get("ok"):
            sys.stdout.write(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        # Pretty markdown
        sys.stdout.write(f"# memory-search: {result['query']}\n\n")
        sys.stdout.write(f"{result['count']} hit(s) from {memory_dir}\n\n")
        for i, h in enumerate(result["hits"], 1):
            sys.stdout.write(f"## {i}. {h['title']}\n")
            sys.stdout.write(f"- file: `{h['key']}` (rank {h['rank']})\n")
            sys.stdout.write(f"- snippet: {h['snippet']}\n")
            sys.stdout.write(f"- path: {h['source_path']}\n\n")
        return 0

    if args.stats:
        result = stats(memory_dir)
        sys.stdout.write(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    # Default action: reindex
    result = reindex(memory_dir)
    sys.stdout.write(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
