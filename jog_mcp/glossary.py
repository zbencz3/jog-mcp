"""HU ⇄ RO term bridge for search. The corpus stays Romanian.

The statute is Romanian and must remain so: at a land-registry counter you
quote the Romanian text, and a translation is not a citation. What this fixes
is the QUERY side. Measured on this corpus, everyday Hungarian terms each
returned zero hits while their Romanian equivalents all found the right
article — "telekkönyv" and "carte funciară" share no letters, so neither the
keyword index nor the n-gram fallback could bridge them.

So a query is EXPANDED, never translated: "telekkönyv" is searched as
"telekkönyv carte funciară". Both halves stay in the query because people
write in both languages, often in one sentence.

The table is a two-column markdown file (`| magyar | română |`), passed with
`--glossary`. The repository ships a small `glossary.sample.md` to show the
format; bring your own for real use. With no glossary at all, search still
works — Hungarian queries simply reach less.
"""
from __future__ import annotations

import os
import re

from . import search

_ROW = re.compile(r"^\|([^|]+)\|([^|]+)\|\s*$")
MAX_EXPANSIONS = 6        # a query that expands without limit stops ranking

# Shared-prefix length for matching a glossary word to a typed one. STRICTER
# than search.MIN_PREFIX (5), because Hungarian compounds share long prefixes that
# mean different things: "telek" (plot) opens telekkönyv, telekhatár AND
# telekmegosztás, so at 5 the query "telekkönyvezéshez" also dragged in
# dezmembrare and limită. Six keeps "bejegyzés" ~ "bejegyeztetni" (shared
# "bejegy") and drops the telek- collisions.
GLOSS_PREFIX = 6


def load(path: str) -> list:
    """[(hu_variants, ro_variants)] from the markdown table. A missing or
    unreadable file is an empty glossary, not an error."""
    if not path:
        return []
    p = os.path.abspath(os.path.expanduser(path))
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    pairs = []
    for line in text.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        left, right = m.group(1).strip(), m.group(2).strip()
        if not left or not right or set(left) <= set("-: "):
            continue                      # the |---|---| separator
        if left.lower() in ("magyar", "hu") :
            continue                      # the header
        hu = [x.strip() for x in left.split(",") if x.strip()]
        ro = [x.strip() for x in right.split(",") if x.strip()]
        if hu and ro:
            pairs.append((hu, ro))
    return pairs


class Glossary:
    """Loaded once, reloaded when the file changes."""

    def __init__(self, path: str = ""):
        self.path = os.path.abspath(os.path.expanduser(path)) if path else ""
        self._mtime = None
        self.pairs: list = []
        self._index: dict = {}
        self.refresh()

    def refresh(self) -> bool:
        try:
            mtime = os.stat(self.path).st_mtime_ns if self.path else None
        except OSError:
            mtime = None
        if mtime == self._mtime and (self._index or mtime is None):
            return False
        self._mtime = mtime
        self.pairs = load(self.path)
        # folded HU phrase -> the Romanian side. Phrases are kept whole so
        # "osztatlan közös tulajdon" maps as one idea, not three words.
        self._index = {}
        for hu, ro in self.pairs:
            for term in hu:
                self._index[search.fold(term)] = ro
        return True

    def lookup(self, term: str) -> list:
        self.refresh()
        return self._index.get(search.fold(term), [])

    def _matches(self, query: str) -> list:
        """[(key, [ro])] for every glossary entry the query actually uses.

        Matched TOKEN-WISE with the same 5-character prefix stem `search.Index`
        uses, not by substring. Hungarian is agglutinative — that is why
        search.py stems at all — and a plain substring test fails on exactly the
        words people type: "bejegyeztetni" does not contain "bejegyzés", so
        the query "épületet akarunk bejegyeztetni telekkönyvbe" reached
        neither `înscriere` nor `intabulare` and the answer missed the
        article that had it (observed 2026-09-20).

        A multi-word entry matches only if EVERY one of its words is in the
        query, so "telekkönyvi kivonat" does not fire on "telekkönyv" alone.
        """
        self.refresh()
        qt = [t for t in search.tokens(query or "")]
        if not qt:
            return []

        def word_score(word: str) -> int:
            """2 for a word typed exactly, 1 for a stemmed match, 0 for none."""
            best = 0
            for t in qt:
                if t == word:
                    return 2
                # A short word must be a true prefix of what was typed —
                # "jog" of "jogot" — because six characters of agreement is
                # more than it has to give.
                if len(word) < GLOSS_PREFIX:
                    if t.startswith(word):
                        best = max(best, 1)
                    continue
                common = 0
                for a, b in zip(word, t):
                    if a != b:
                        break
                    common += 1
                if common >= GLOSS_PREFIX:
                    best = max(best, 1)
            return best

        def covered(words, taken):
            """Is every word of this entry already accounted for by a longer
            one we accepted? Compared by STEM, not by exact token: "telekkönyv"
            is subsumed by "telekkönyvi kivonat", and "telek" by "telekhatár",
            but neither pair is equal as strings. Without this the shorter
            entry re-fires and drags its own translation in beside the
            specific one — "extras de carte funciară" plus a stray "carte
            funciară", and "telekhatár" plus a stray "teren"."""
            for word in words:
                if not any(a == word or a.startswith(word) or word.startswith(a)
                           for k, _ in taken for a in search.tokens(k)):
                    return False
            return True

        # Rank candidate entries by how well they match, NOT by how long the
        # key is. Length alone made "telekkönyv" resolve to `intabulare`,
        # because "telekkönyvezés" is a longer string and matched it by stem —
        # but the register and the act of registering are different words, and
        # the one the user typed exactly is the one they meant.
        ranked = []
        for key in self._index:
            words = search.tokens(key)
            if not words:
                continue
            scores = [word_score(w) for w in words]
            if not all(scores):
                continue
            ranked.append((sum(scores), len(words), len(key), key, words))
        ranked.sort(reverse=True)

        out = []
        for _s, _n, _l, key, words in ranked:
            if covered(words, out):
                continue
            out.append((key, self._index[key]))
            if len(out) >= MAX_EXPANSIONS:
                break
        return out

    def expand(self, query: str) -> str:
        """The query plus the Romanian equivalents of any Hungarian terms in
        it. Longest phrases first, so "telekkönyvi kivonat" maps to the extras
        rather than being torn into "telekkönyv" + "kivonat"."""
        added = []
        folded = search.fold(query or "")
        for _key, ros in self._matches(query):
            for ro in ros:
                if search.fold(ro) not in folded and ro not in added:
                    added.append(ro)
        if not added:
            return query
        return f"{query} {' '.join(added[:MAX_EXPANSIONS])}"

    def hits(self, query: str) -> list:
        """[(hu, [ro])] for the terms an expansion actually used — a client can
        show these so the user learns the Romanian word they will have to
        say at the counter."""
        return self._matches(query)

    def as_prompt(self, limit: int = 0) -> str:
        """The table for an LLM prompt, so an answer can name both words."""
        self.refresh()
        rows = self.pairs[:limit] if limit else self.pairs
        return "\n".join(f"- {', '.join(hu)} = {', '.join(ro)}"
                         for hu, ro in rows)


def validate(gloss, index) -> list:
    """Which Romanian terms never appear in the corpus.

    The glossary now steers ranking, so a wrong pair silently misroutes
    questions — and a glossary is rarely written by a legal translator. This
    is the objective check available: the statute is the authority on its own
    vocabulary, so a Romanian term that appears nowhere in 1200-odd articles
    is either a mistranslation or a colloquialism the law does not use.

    Stem-matched, because the corpus writes "borne" where the glossary writes
    "bornă" and neither is wrong. A term that survives this is corroborated by
    the text itself; one that does not is a question for a human reviewer, not
    automatically an error — `topograf` is what surveyors call themselves and
    `persoană autorizată` is what the law calls them.

    Returns [(hu, ro_term, [words not found])].
    """
    vocab = set()
    for chunk in index.chunks:
        vocab |= set(chunk.get("_tokens") or [])
    stems = {w[:search.MIN_PREFIX] for w in vocab if len(w) >= search.MIN_PREFIX}
    out = []
    for hu, ro in gloss.pairs:
        for term in ro:
            absent = []
            for w in search.tokens(term):
                if w in vocab:
                    continue
                if len(w) >= search.MIN_PREFIX and w[:search.MIN_PREFIX] in stems:
                    continue
                absent.append(w)
            if absent:
                out.append((hu[0], term, absent))
    return out
