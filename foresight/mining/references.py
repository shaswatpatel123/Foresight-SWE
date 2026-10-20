"""Find fix phrases like "regression in #N"."""

from __future__ import annotations

import re
from collections.abc import Iterator

# (category, pattern); group 1 is the number
_RAW_PATTERNS: list[tuple[str, str]] = [
    # PR1 broke something
    ("regression", r"regress(?:ion|ed)?\s+(?:from|in|introduced\s+(?:in|by)|caused\s+by)\s+#(\d+)"),
    ("regression", r"\bbroke(?:n)?\s+(?:in|by)\s+#(\d+)"),
    # PR1 added the bug
    ("introduced", r"introduced\s+(?:in|by)\s+#(\d+)"),
    ("introduced", r"bug\s+(?:from|in)\s+#(\d+)"),
    # follow-up
    ("followup", r"follow[\s-]?up\s+(?:to|of|for|on)\s+#(\d+)"),
    ("followup", r"\bfollow[\s-]?up\b[^#\n]{0,20}#(\d+)"),
    # PR2 finishes or replaces PR1
    ("supersedes", r"supersed(?:es|ing|e)\s+#(\d+)"),
    ("supersedes", r"replaces\s+#(\d+)"),
    ("partial", r"(?:complete(?:s| )|finish(?:es)?)\s+(?:the\s+)?(?:fix\s+(?:in|from)\s+)?#(\d+)"),
    ("partial", r"partial(?:ly)?\s+(?:fix(?:ed|es)?\s+)?(?:in|by|of)\s+#(\d+)"),
    # backports: not a chain
    ("backport", r"backport\s+(?:of\s+)?#(\d+)"),
    ("backport", r"cherry[\s-]?picked?\s+from\s+#(\d+)"),
]

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (cat, re.compile(pat, re.IGNORECASE)) for cat, pat in _RAW_PATTERNS
]

CATEGORIES = sorted({cat for cat, _ in _RAW_PATTERNS})


def extract_references(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield (number, category, phrase), deduped."""
    seen: set[tuple[int, str]] = set()
    for category, pattern in PATTERNS:
        for m in pattern.finditer(text):
            number = int(m.group(1))
            key = (number, category)
            if key in seen:
                continue
            seen.add(key)
            phrase = " ".join(m.group(0).split())
            yield number, category, phrase
