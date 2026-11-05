"""Second-chance retrieval for questions the keyword index misses.

WHAT THIS IS, PLAINLY. Character-n-gram cosine similarity, not sentence
embeddings — and on THIS corpus it earns its place in exactly one situation,
measured rather than assumed:

  * The question is in Hungarian and the corpus is Romanian. "mennyi ideig
    érvényes az urbanisztikai bizonylat" scores 0 keyword hits on the word
    "urbanisztikai", but shares n-grams with "urbanism" and reaches Legea
    350/2001 art. 45 at 0.25. Shared Latin roots are the bridge; there is no
    other one, because the corpus is not translated.

What it does NOT buy, contrary to what you might expect of a "semantic"
layer, both verified against this corpus:

  * Word forms. `search.py`'s 5-character prefix stem ALREADY matches
    "dezmembrării" to "dezmembrămintelor" and "funciera" to "funciară". The
    fallback adds nothing there and never fires, because the keyword pass
    comes back full.
  * Concepts. "can I split a plot with no access road" is not answerable by
    string similarity in any form — the statute never says "access road".
    Only a real embedding model bridges that, and this is not one.

THRESHOLD. `MIN_SCORE` is not a guess. Measured on this corpus, useful matches
score 0.20-0.35 and pure noise 0.06-0.12, so the floor sits at 0.18 in the gap
between them. This matters more than the usual tuning knob: without a floor,
"zzzz nonexistent term" returned five confidently-formatted articles. For a
law tool "no article matches that" is a correct and necessary answer, and
inventing plausible-looking hits instead is the worst thing this server could
do. Re-measure the floor if the corpus grows a fourth language.

TO ADD REAL SEMANTIC SEARCH later, implement `score_all` on another class and
pass it as `fallback=` to LawLibrary — the interface is deliberately one method.
Note what it would cost, so it stays a decision rather than a drift: an
embedding API key, a per-query network call in the hot path, re-embedding the
corpus on every `--fetch-acts`, and a vector store to keep in sync with a
corpus whose whole point is being a git diff. Worth it the day a real question
from a real user actually fails; not before.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from . import search

NGRAM = 4
MIN_SCORE = 0.18         # measured: useful 0.20-0.35, noise 0.06-0.12


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", search.fold(s or ""))


def ngrams(s: str, n: int = NGRAM) -> Counter:
    """Character n-grams over word-boundary-padded text. Padding means a short
    word still contributes, and a word boundary is itself a feature."""
    out: Counter = Counter()
    for word in _norm(s).split():
        w = f" {word} "
        if len(w) <= n:
            out[w] += 1
            continue
        for i in range(len(w) - n + 1):
            out[w[i:i + n]] += 1
    return out


class NgramFallback:
    """Cosine similarity over IDF-weighted character n-grams.

    Built lazily: a server that never gets a hard question never pays for the
    index, which on this corpus is ~0.6s and a few MB."""

    def __init__(self, chunks: list):
        self._chunks = chunks
        self._vecs = None
        self._idf: dict = {}

    def _build(self) -> None:
        if self._vecs is not None:
            return
        raw = []
        df: Counter = Counter()
        for c in self._chunks:
            g = ngrams(f"{c.get('heading','')} {c.get('text','')}")
            raw.append(g)
            df.update(g.keys())
        n = len(raw) or 1
        self._idf = {t: math.log(1.0 + n / (1.0 + d)) for t, d in df.items()}
        self._vecs = []
        for g in raw:
            v = {t: cnt * self._idf.get(t, 0.0) for t, cnt in g.items()}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            self._vecs.append({t: x / norm for t, x in v.items()})

    def score_all(self, query: str, limit: int = 5) -> list:
        """[(index_into_chunks, score)] best first, above MIN_SCORE."""
        self._build()
        qg = ngrams(query)
        if not qg:
            return []
        qv = {t: cnt * self._idf.get(t, 0.0) for t, cnt in qg.items()}
        qnorm = math.sqrt(sum(x * x for x in qv.values())) or 1.0
        qv = {t: x / qnorm for t, x in qv.items()}
        # iterate the shorter side: a query has ~dozens of n-grams, a chunk
        # hundreds, so scoring is over the query's terms only
        scored = []
        for i, dv in enumerate(self._vecs):
            s = 0.0
            for t, x in qv.items():
                y = dv.get(t)
                if y:
                    s += x * y
            if s >= MIN_SCORE:
                scored.append((i, s))
        scored.sort(key=lambda p: -p[1])
        return scored[:limit]
