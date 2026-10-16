"""One-time migration (ADR 0008): split cold-storage archives back into individual facts.

The retired Tier-3 consolidation squeezed ~100 facts at a time into a narrative file and
left one pointer vector behind, which rarely ranked in search, so those facts were
effectively forgotten. This splits each archive into standalone facts, stores them in
ChromaDB dated by the archive, and removes the pointer vectors. The archive files stay on
disk as a backup. Idempotent: archives already imported are skipped.

    python3 import_cold_storage.py --dry-run   # print the facts, change nothing
    python3 import_cold_storage.py
"""
import argparse
import datetime
import os
import re
import sys

from langchain_core.messages import HumanMessage, SystemMessage

import memory
from llm_router import get_llm

COLD_STORAGE_DIR = os.path.join(os.path.dirname(__file__), "cold_storage")
POINTER_PREFIX = "Extensive long-term memory archive"

SPLIT_PROMPT = """Below is a long-term memory archive about the user, written as a profile
narrative. Split it into standalone facts for a searchable memory store.

- One fact per line, each a complete sentence that makes sense on its own: name people and
  things explicitly (never start with "He", "She" or "They").
- Keep every specific: names, dates, places, numbers, email addresses, amounts.
- Keep time framing: if something is previous, past, pending, or planned, say so.
- One idea per fact, but don't shred a single fact into fragments.
- Skip headings, archive references, and filler.
- No bullets, numbering, or commentary — just the facts, one per line.

ARCHIVE:
{archive}"""


def archive_date(filename: str) -> str:
    """memory_archive_20261010_080141.txt -> 2026-10-10 (the date the facts were archived)."""
    m = re.search(r"(\d{8})_\d{6}", filename)
    day = datetime.datetime.strptime(m.group(1), "%Y%m%d").date() if m else datetime.date.today()
    return day.isoformat()


def split_archive(text: str, llm) -> list:
    response = llm.invoke([SystemMessage(content="You split narratives into standalone facts."),
                           HumanMessage(content=SPLIT_PROMPT.format(archive=text))])
    content = response.content
    if isinstance(content, list):
        content = next((b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), str(content))
    return memory.fact_lines(content)


def _imported_sources(collection) -> set:
    return {(m or {}).get("source") for m in collection.get(include=["metadatas"])["metadatas"]}


def _pointer_ids(collection) -> list:
    got = collection.get(include=["documents"])
    return [i for i, d in zip(got["ids"], got["documents"]) if d.startswith(POINTER_PREFIX)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the facts, change nothing")
    args = parser.parse_args(argv)

    collection = memory.collection
    if collection is None:
        print("ChromaDB is unavailable — nothing imported.")
        return 1
    files = sorted(f for f in os.listdir(COLD_STORAGE_DIR) if f.endswith(".txt")) \
        if os.path.isdir(COLD_STORAGE_DIR) else []
    done = _imported_sources(collection)
    todo = [f for f in files if f"archive:{f}" not in done]
    for f in sorted(set(files) - set(todo)):
        print(f"{f}: already imported, skipping.")

    llm = get_llm(temperature=0) if todo else None
    for f in todo:
        source = f"archive:{f}"
        with open(os.path.join(COLD_STORAGE_DIR, f)) as fh:
            facts = split_archive(fh.read(), llm)
        if args.dry_run:
            print(f"\n{f}: {len(facts)} facts (dated {archive_date(f)})")
            for fact in facts:
                print(f"  - {fact}")
            continue
        try:
            stored = memory.store_facts(facts, added=archive_date(f), source=source)
        except Exception:
            collection.delete(where={"source": source})   # all or nothing, so a rerun retries it
            raise
        print(f"{f}: {len(facts)} facts split, {stored} stored (dated {archive_date(f)}).")

    if not args.dry_run:
        pointers = _pointer_ids(collection)
        if pointers:
            collection.delete(ids=pointers)
            print(f"Removed {len(pointers)} archive pointer(s).")
    print(f"Semantic memory now holds {collection.count()} facts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
