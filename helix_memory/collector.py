"""
Document collection: walk a memory directory and gather indexable documents.

Default layout (customizable):
    <memory_dir>/
        <category>/
            *.md          — markdown files (decisions, logs, notes)

Each document: {"id": <rel path>, "category": <category>, "text": <content>}

Chunking: markdown files are split on H2 sections ("## ") so each
section becomes its own retrievable unit. Small files stay whole.
"""

import os
import re

# Backwards-compatible default: auto-detected from memory_dir.
# Callers should prefer passing categories explicitly.
DIVISIONS: list[str] = []


def _detect_divisions(memory_dir: str | None = None) -> list[str]:
    """Auto-detect category subdirectories."""
    memory_dir = os.path.expanduser(memory_dir or "~/memory")
    if not os.path.isdir(memory_dir):
        return []
    return [
        d for d in sorted(os.listdir(memory_dir))
        if os.path.isdir(os.path.join(memory_dir, d))
        and not d.startswith(".")
    ]


def doc_count_by_division(docs: list[dict]) -> dict[str, int]:
    """Count documents per category."""
    counts: dict[str, int] = {}
    for d in docs:
        cat = d.get("category", d.get("division", "uncategorized"))
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def _split_sections(path: str, text: str) -> list[tuple[str, str]]:
    """Split a markdown file into (section_id, section_text) units.

    Splits on H2 headers. Files without H2s (or very small files) stay whole.
    """
    parts = re.split(r"(?m)^## ", text)
    if len(parts) <= 1 or len(text) < 1500:
        return [(path, text)]
    units: list[tuple[str, str]] = []
    # parts[0] is the pre-header preamble (usually the H1 title block)
    preamble = parts[0].strip()
    for i, part in enumerate(parts[1:], start=1):
        header = part.split("\n", 1)[0].strip()
        body = "## " + part
        if preamble and i == 1:
            body = preamble + "\n\n" + body
        units.append((f"{path}#s{i}", body))
    return units


def collect_documents(
    memory_dir: str | None = None,
    categories: list[str] | None = None,
    recursive: bool = True,
) -> list[dict]:
    """Walk the memory directory and return indexable document dicts.

    Args:
        memory_dir: Root directory containing memory files.
            Defaults to ~/memory.
        categories: Subdirectories to index. If None, all immediate
            subdirectories of memory_dir are treated as categories.
            If memory_dir contains markdown files directly, use ["."].
        recursive: If True, walk subdirectories recursively within
            each category.
    """
    memory_dir = os.path.expanduser(memory_dir or "~/memory")
    docs: list[dict] = []

    if not os.path.isdir(memory_dir):
        return docs

    if categories is None:
        # Auto-detect: subdirectories become categories
        categories = [
            d for d in sorted(os.listdir(memory_dir))
            if os.path.isdir(os.path.join(memory_dir, d))
            and not d.startswith(".")
        ]
        # If no subdirectories, index the root directly
        if not categories:
            categories = ["."]

    for cat in categories:
        cat_root = os.path.join(memory_dir, cat) if cat != "." else memory_dir
        if not os.path.isdir(cat_root):
            continue

        if recursive:
            walker = os.walk(cat_root)
        else:
            # Single-level: emulate os.walk output
            files = [
                f for f in sorted(os.listdir(cat_root))
                if f.endswith(".md")
            ]
            walker = [(cat_root, [], files)]

        for dirpath, _, filenames in walker:
            for fname in sorted(filenames):
                if not fname.endswith(".md"):
                    continue
                # Skip hidden files and common non-content files
                if fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                rel = os.path.relpath(fpath, os.path.expanduser("~"))
                try:
                    with open(fpath, "r", encoding="utf-8",
                              errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                if not text.strip():
                    continue
                for sec_id, sec_text in _split_sections(rel, text):
                    docs.append({
                        "id": sec_id,
                        "category": cat,
                        "text": sec_text,
                    })

    return docs
