"""migrate.py — one-time import of legacy ~/.claude/projects/<id>/memory/ files
into the knowledge/concepts/ folder of the Saiyan memory-kb.

Reads each .md from the source directory, classifies it by filename prefix,
preserves any existing YAML frontmatter (name/description/type), augments with
the new schema fields (tags, sources, created, updated), and writes to the
appropriate target. Idempotent — skips files that already exist at the target.

Usage:
    uv run python scripts/migrate.py --source <PATH> [--dry-run]

Default source: ~/.claude/projects/c--Users-Colan-OneDrive-Desktop-Trading-bot/memory/

After migration, run:
    uv run python scripts/compile.py --all

…to add wikilinks, build the index, and create initial connections/.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

# Filename prefix → (target subdir under knowledge/, type tag for frontmatter)
PREFIX_RULES: list[tuple[str, str, str]] = [
    ("feedback_", "concepts", "lesson"),
    ("insight_", "concepts", "insight"),
    ("project_", "concepts", "project-state"),
    ("user_", "concepts", "user-preference"),
    ("reference_", "concepts", "reference"),
    ("research_", "concepts", "research"),
    ("completed_work_", "concepts", "archive"),
    ("layer", "concepts", "archive"),
]

# Specific filenames that get the archive type without a clean prefix match
NAMED_ARCHIVE = {"github_repos.md", "skill_consolidation.md", "codebase_map_skill.md"}

# Files we explicitly skip (managed by the new system or transient)
SKIP_FILES = {
    "session_handoff.md",
    "MEMORY.md",
    "last_active.json",
    "save_checkpoint.py",
}

# Backup/temp extensions to skip
SKIP_SUFFIXES = (".bak", ".backup", ".tmp")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter dict and body from a markdown file.

    Returns ({}, full_text) if no frontmatter present. Naïve parser — handles
    `key: value` pairs only (no nested dicts, no lists with hyphens). Sufficient
    for the existing memory files which all use a flat schema.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    fm: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm, body


def classify(filename: str) -> tuple[str, str] | None:
    """Return (target_subdir, type_tag) for a filename, or None to skip."""
    if filename in SKIP_FILES:
        return None
    if any(filename.endswith(suffix) for suffix in SKIP_SUFFIXES):
        return None
    if filename in NAMED_ARCHIVE:
        return ("concepts", "archive")
    for prefix, subdir, type_tag in PREFIX_RULES:
        if filename.startswith(prefix):
            return (subdir, type_tag)
    # Default for any unmatched .md → archive in concepts
    if filename.endswith(".md"):
        return ("concepts", "archive")
    return None


def slugify(filename: str) -> str:
    """Strip .md extension and normalize. Returns lowercase-hyphenated slug."""
    stem = filename[:-3] if filename.endswith(".md") else filename
    # Remove the prefix part (e.g. feedback_, insight_) so the slug is the
    # essential name. Keeps it readable in wikilinks: [[concepts/brutal-honesty]]
    # rather than [[concepts/feedback-brutal-honesty]].
    for prefix, _, _ in PREFIX_RULES:
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def derive_tags(type_tag: str, filename: str) -> list[str]:
    """Pick reasonable tags from filename + type. Compile.py's LLM pass will
    refine these later, but seeding makes the initial graph less empty."""
    tags = [type_tag]
    fname_low = filename.lower()
    keyword_tags = {
        "trading": ["trading", "bot", "btc", "neo"],
        "regime": ["regime", "regimes"],
        "telegram": ["telegram", "tg-"],
        "vpn": ["vpn", "winerror"],
        "kelly": ["kelly", "dd-kelly"],
        "f10": ["f10", "f-10"],
        "memory-kb": ["memory", "kb"],
        "tlp": ["tlp", "trigger-lifecycle"],
        "audit": ["audit"],
        "review": ["review", "monthly"],
        "obsidian": ["obsidian"],
        "claude-config": ["claude", "config"],
    }
    for tag, kws in keyword_tags.items():
        if any(kw in fname_low for kw in kws):
            tags.append(tag)
    return list(dict.fromkeys(tags))  # dedupe, preserve order


def build_frontmatter(
    *,
    title: str,
    type_tag: str,
    aliases: list[str],
    tags: list[str],
    sources: list[str],
    created: str,
    updated: str,
    extra: dict[str, str],
) -> str:
    """Render YAML frontmatter for a migrated concept article."""
    lines = ["---"]
    lines.append(f'title: "{title}"')
    lines.append(f"type: {type_tag}")
    if aliases:
        lines.append("aliases: [" + ", ".join(aliases) + "]")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    lines.append("sources:")
    for src in sources:
        lines.append(f'  - "{src}"')
    lines.append(f"created: {created}")
    lines.append(f"updated: {updated}")
    for k, v in extra.items():
        if k in ("title", "type", "tags", "aliases", "sources", "created", "updated"):
            continue
        # Preserve any other fields from original frontmatter (description, etc.)
        if v:
            safe_v = str(v).replace("\n", " ").strip()
            if ":" in safe_v or '"' in safe_v:
                safe_v = '"' + safe_v.replace('"', '\\"') + '"'
            lines.append(f"{k}: {safe_v}")
    lines.append("---\n")
    return "\n".join(lines)


def migrate_file(src: Path, dst_root: Path, today: str, dry_run: bool) -> str:
    """Migrate one source .md file to the new knowledge/ structure.

    Returns one of: 'migrated', 'skipped-rule', 'skipped-exists', 'skipped-error'.
    """
    classified = classify(src.name)
    if classified is None:
        return "skipped-rule"
    subdir, type_tag = classified

    slug = slugify(src.name)
    dst = dst_root / subdir / f"{slug}.md"

    if dst.exists():
        return "skipped-exists"

    try:
        text = src.read_text(encoding="utf-8")
    except Exception as exc:
        sys.stderr.write(f"[migrate] read error {src}: {exc}\n")
        return "skipped-error"

    orig_fm, body = parse_frontmatter(text)

    # Title: prefer explicit `name:` from old frontmatter, fall back to slug-cased.
    title = orig_fm.get("name") or slug.replace("-", " ").title()
    aliases = []
    if orig_fm.get("description"):
        # Preserve description as an alias hint — it's often a one-line summary
        # that's useful when the LLM scans the index. We keep it in the body too.
        pass

    tags = derive_tags(type_tag, src.name)
    today_iso = today

    # Source pointer back to the original (legacy) memory file location for traceability
    legacy_pointer = f"legacy:{src.name}"
    sources = [legacy_pointer]

    fm_text = build_frontmatter(
        title=title,
        type_tag=type_tag,
        aliases=aliases,
        tags=tags,
        sources=sources,
        created=today_iso,
        updated=today_iso,
        extra=orig_fm,
    )

    new_body = body.lstrip("\n")
    if not new_body.startswith(f"# {title}"):
        new_body = f"# {title}\n\n{new_body}"

    final_text = fm_text + "\n" + new_body
    if not final_text.endswith("\n"):
        final_text += "\n"

    if dry_run:
        sys.stdout.write(f"[dry-run] would migrate {src.name} → {subdir}/{slug}.md\n")
        return "migrated"

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(final_text, encoding="utf-8")
    return "migrated"


def main() -> int:
    # Windows cp1252 stdout chokes on Unicode arrows etc. — force utf-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Migrate legacy memory files into Saiyan memory-kb."
    )
    default_source = (
        Path.home()
        / ".claude/projects/c--Users-Colan-OneDrive-Desktop-Trading-bot/memory"
    )
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--knowledge-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "knowledge",
        help="Target knowledge/ directory (default: memory-kb/knowledge/).",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        sys.stderr.write(f"[migrate] source not found: {args.source}\n")
        return 2

    if not args.knowledge_root.is_dir():
        sys.stderr.write(f"[migrate] knowledge root not found: {args.knowledge_root}\n")
        return 2

    today = date.today().isoformat()
    counts = {"migrated": 0, "skipped-rule": 0, "skipped-exists": 0, "skipped-error": 0}

    files = sorted(args.source.rglob("*.md"))
    sys.stdout.write(f"[migrate] source: {args.source}\n")
    sys.stdout.write(f"[migrate] target: {args.knowledge_root}\n")
    sys.stdout.write(
        f"[migrate] {len(files)} .md files found, dry_run={args.dry_run}\n"
    )

    for src in files:
        # Handle archive subdirectory specially: archive/foo.md → concepts/archived/foo.md
        if src.parent.name == "archive":
            slug = slugify(src.name)
            archived_target = (
                args.knowledge_root / "concepts" / "archived" / f"{slug}.md"
            )
            if archived_target.exists():
                counts["skipped-exists"] += 1
                continue
            text = src.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            title = fm.get("name") or slug.replace("-", " ").title()
            tags = derive_tags("archived", src.name) + ["archive-pre-S"]
            fm_text = build_frontmatter(
                title=title,
                type_tag="archived",
                aliases=[],
                tags=tags,
                sources=[f"legacy:archive/{src.name}"],
                created=today,
                updated=today,
                extra=fm,
            )
            new_body = body.lstrip("\n")
            if not new_body.startswith(f"# {title}"):
                new_body = f"# {title}\n\n{new_body}"
            final_text = fm_text + "\n" + new_body
            if not final_text.endswith("\n"):
                final_text += "\n"
            if args.dry_run:
                sys.stdout.write(
                    f"[dry-run] would migrate archive/{src.name} → concepts/archived/{slug}.md\n"
                )
            else:
                archived_target.parent.mkdir(parents=True, exist_ok=True)
                archived_target.write_text(final_text, encoding="utf-8")
            counts["migrated"] += 1
            continue

        result = migrate_file(src, args.knowledge_root, today, args.dry_run)
        counts[result] += 1

    sys.stdout.write("[migrate] summary:\n")
    for k, v in counts.items():
        sys.stdout.write(f"  {k}: {v}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
