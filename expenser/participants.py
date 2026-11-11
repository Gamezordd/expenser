"""Participant list loading and name resolution."""
from __future__ import annotations

import csv
import re
from pathlib import Path


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.casefold()
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def load_participants(path: Path) -> list[str]:
    """Load participant names from a .txt (one per line) or .csv file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Participants file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    names: list[str] = []
    if path.suffix.lower() == ".csv":
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        rows = list(csv.reader(lines))
        col = 0
        if rows:
            header = [c.strip().lower() for c in rows[0]]
            if "name" in header:
                col = header.index("name")
                rows = rows[1:]
        for row in rows:
            if col < len(row) and row[col].strip():
                names.append(row[col].strip())
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    names = _dedupe(names)
    if not names:
        raise ValueError(f"No participants found in {path}")
    return names


def resolve_name(name: str, known: list[str]) -> str | None:
    """Case-insensitive exact match, then unique prefix match. None if none/ambiguous."""
    needle = name.strip().casefold()
    if not needle:
        return None
    for k in known:
        if k.casefold() == needle:
            return k
    matches = [k for k in known if k.casefold().startswith(needle)]
    return matches[0] if len(matches) == 1 else None


_EXCEPT_RE = re.compile(r"\b(?:except|excluding|minus|without)\b", re.IGNORECASE)


def parse_participants_cell(cell: str, known: list[str]) -> list[str]:
    """Parse a Participants cell into a list of known names.

    Supports ``Alice, Bob``, ``ALL``, ``ALL -Bob -Carol`` and ``ALL except Bob, Carol``.
    """
    text = (cell or "").strip()
    if not text:
        return list(known)
    tail = ""
    m = _EXCEPT_RE.search(text)
    if m:  # "ALL except Bob, Carol": everything after the keyword is an exclusion
        text, tail = text[: m.start()], text[m.end():]
    tokens = [t.strip() for t in re.split(r"[,;]", text) if t.strip()]
    tokens += ["-" + t.strip() for t in re.split(r"[,;]", tail) if t.strip()]
    include: list[str] = []
    exclude: list[str] = []
    all_flag = False
    unknown: list[str] = []

    def handle(raw: str, bucket: list[str]) -> None:
        nonlocal all_flag
        if raw.casefold() == "all":
            if bucket is include:
                all_flag = True
            return
        r = resolve_name(raw, known)
        if r is None:
            unknown.append(raw)
        else:
            bucket.append(r)

    for tok in tokens:
        # split on " -" / leading "-" to separate exclusions: "ALL -Bob -Carol"
        parts = re.split(r"(?:^|\s)-\s*", tok)
        head = parts[0].strip()
        if head:
            handle(head, include)
        for ex in parts[1:]:
            if ex.strip():
                handle(ex.strip(), exclude)
    if unknown:
        raise ValueError(f"Unknown participant(s): {', '.join(unknown)}")
    if all_flag or (not include and exclude):
        base = list(known)
    else:
        base = []
        for n in include:
            if n not in base:
                base.append(n)
    result = [n for n in base if n not in exclude]
    return result
