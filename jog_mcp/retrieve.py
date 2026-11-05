"""Literal phrase search over the law corpus.

`lawlib.search` ranks articles by WORDS. A number or a fixed legal phrase
("15%", "30 de zile", "art. 41 alin. (10)") is the one thing ranked search
cannot see, so `find_exact` scans every article for the phrase itself, with
case and diacritics ignored.
"""
from __future__ import annotations

import re

from . import search

EXACT_CONTEXT = 700      # chars either side of an exact match


def find_exact(law, phrase: str, act: str = "", limit: int = 20) -> list:
    """Every article containing `phrase` literally (case and diacritics
    ignored) — "15%", "servitute de trecere", "art. 41 alin. (10)".

    Ranked search scores WORDS; a number or a fixed legal phrase is the one
    thing it cannot see. "15%" is how art. 98 of the Regulament was found by
    hand after the ranked search missed it twice: the token "15" is in
    hundreds of articles, the string "15%" in four."""
    law.refresh()
    words = search.fold(phrase).split()
    if not words:
        return []
    # Whitespace-tolerant, on the folded text: fold() only strips combining
    # marks, so offsets in it line up with the original text.
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
    want = law.resolve_act(act) if act else None
    rows, seen = [], set()
    for chunk in law.index.chunks:
        if not chunk.get("heading"):
            continue
        row = law._decorate({"path": chunk["path"], "heading": chunk["heading"],
                             "text": chunk["text"], "score": 0})
        m = pattern.search(search.fold(row["text"]))
        if not m:
            continue
        if want is not None and row["act_id"] != want.act_id:
            continue
        key = (row["act_id"], row["article"])
        if key in seen:
            continue
        seen.add(key)
        text = row["text"]
        lo = max(0, m.start() - EXACT_CONTEXT)
        hi = m.end() + EXACT_CONTEXT
        row["text"] = (("… " if lo else "") + text[lo:hi].strip()
                       + (" …" if hi < len(text) else ""))
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows
