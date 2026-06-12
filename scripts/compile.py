"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config import AGENTS_FILE, CONCEPTS_DIR, CONNECTIONS_DIR, DAILY_DIR, KNOWLEDGE_DIR, now_iso
from sdk_nowindow import apply as _silence_sdk_windows
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    save_state,
)

# This script is spawned with a hidden console (CREATE_NO_WINDOW) by flush.py's
# end-of-day trigger; keep the SDK patch anyway so a manual/console-less launch
# can't give claude.exe a stray visible window.
_silence_sdk_windows()

# ── Paths for the LLM to use ──────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent

# ── Cost guards (2026-06-11, after $64 cumulative plan-usage burn) ────
# Automatic runs compile at most N logs; a log that keeps erroring is
# skipped after MAX_FAILURES so a usage-limit night can't retry-loop the
# whole backlog every compact. --all / --file bypass both guards.
MAX_FILES_PER_RUN = 3
MAX_FAILURES_BEFORE_SKIP = 2


def _schema_excerpt(agents_md: str) -> str:
    """Slice AGENTS.md to the sections the compiler actually needs.

    The full file (~24 KB) mostly documents architecture/hooks/scripts — dead
    weight in every compile prompt. Keep Article Formats + Conventions; fall
    back to the full file if the section markers ever change.
    """
    try:
        formats = agents_md.split("## Article Formats")[1].split("## Core Operations")[0]
        conventions = agents_md.split("## Conventions")[1].split("## Full Project Structure")[0]
        return "## Article Formats" + formats + "## Conventions" + conventions
    except IndexError:
        return agents_md


async def compile_daily_log(log_path: Path, state: dict) -> tuple[float, bool]:
    """Compile a single daily log into knowledge articles.

    Returns (API cost, success flag).
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    log_content = log_path.read_text(encoding="utf-8")
    schema = _schema_excerpt(AGENTS_FILE.read_text(encoding="utf-8"))
    wiki_index = read_wiki_index()

    # Cost guard: do NOT embed existing articles in the prompt. The agent has
    # Read/Glob/Grep — the index below is its map, and it reads only the few
    # articles it actually updates. Embedding the whole KB made every compile
    # re-send ~all articles each turn (cost grew with the square of KB size).

    timestamp = now_iso()

    prompt = f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index}

## Existing Wiki Articles

The index above is the complete map of existing articles. Article files live
under {KNOWLEDGE_DIR}. **Read only the specific articles you intend to update
or link to (typically 2-5) — never bulk-read the whole knowledge base.**

## Daily Log to Compile

**File:** {log_path.name}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - First Read that specific article file, then add the new information and the source to frontmatter
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""

    cost = 0.0
    ok = True

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
                # Pin Sonnet so this nightly background compile doesn't inherit
                # the interactive session's flagship model (settings.json "opus")
                # and quietly drain the plan budget over ~30 unattended turns.
                model="sonnet",
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # compilation output - LLM writes files directly
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
    except Exception as e:
        print(f"  Error: {e}")
        ok = False

    rel_path = log_path.name
    if ok:
        state.setdefault("ingested", {})[rel_path] = {
            "hash": file_hash(log_path),
            "compiled_at": now_iso(),
            "cost_usd": cost,
        }
        state.setdefault("failures", {}).pop(rel_path, None)
    else:
        failures = state.setdefault("failures", {})
        failures[rel_path] = failures.get(rel_path, 0) + 1
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)

    return cost, ok


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    state = load_state()

    # Determine which files to compile
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name
                prev = state.get("ingested", {}).get(rel, {})
                if not prev or prev.get("hash") != file_hash(log_path):
                    to_compile.append(log_path)

            # Cost guards (automatic runs only; --all / --file bypass).
            failures = state.get("failures", {})
            skipped = [
                p for p in to_compile
                if failures.get(p.name, 0) >= MAX_FAILURES_BEFORE_SKIP
            ]
            if skipped:
                names = ", ".join(p.name for p in skipped)
                print(
                    f"Skipping {len(skipped)} repeatedly-failing log(s) "
                    f"(rerun with --all to force): {names}"
                )
                to_compile = [p for p in to_compile if p not in skipped]
            if len(to_compile) > MAX_FILES_PER_RUN:
                print(
                    f"Cost guard: compiling {MAX_FILES_PER_RUN} of "
                    f"{len(to_compile)} pending logs; the rest drain on "
                    f"later runs."
                )
                to_compile = to_compile[:MAX_FILES_PER_RUN]

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    # Compile each file sequentially
    total_cost = 0.0
    failed = 0
    for i, log_path in enumerate(to_compile, 1):
        print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
        cost, ok = asyncio.run(compile_daily_log(log_path, state))
        total_cost += cost
        failed += 0 if ok else 1
        print("  Done." if ok else "  FAILED (will retry next run, max 2 attempts).")

    articles = list_wiki_articles()
    status = f", {failed} failed" if failed else ""
    print(f"\nCompilation complete ({len(to_compile)} file(s){status}). Total cost: ${total_cost:.2f}")
    print(f"Knowledge base: {len(articles)} articles")


if __name__ == "__main__":
    main()
