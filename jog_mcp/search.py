"""Keyword search over markdown, in pure Python.

Why no embeddings / vector DB: the corpus is a few MB of statute in Romanian,
queried in Romanian and Hungarian, which share loanwords. It has to be
greppable and reviewable as a git diff. A keyword index rebuilt in
milliseconds beats a service to keep alive. If the corpus ever grows by an
order of magnitude this is the module to replace — nothing else knows how
search works.

Matching notes: Hungarian is agglutinative ("telekkönyv", "telekkönyvet",
"telekkönyvezés") and both HU and RO are written with diacritics that people
drop when typing fast ("telekkonyv", "carte funciara"). So every token is
folded to ASCII and a shared 5-character prefix counts as a partial match.
"""
from __future__ import annotations

import os
import re
import unicodedata

# Words that carry no retrieval signal in Hungarian, Romanian and English.
STOPWORDS = {
    # hu
    "a", "az", "és", "es", "vagy", "hogy", "nem", "is", "de", "meg", "van",
    "volt", "lesz", "kell", "mi", "mit", "mik", "ez", "ezt", "azt", "ki",
    "hol", "mikor", "hogyan", "milyen", "egy", "ha", "csak", "már", "mar",
    # ro
    "si", "și", "sau", "de", "la", "in", "în", "cu", "pe", "un", "o", "este",
    "sunt", "ce", "care", "pentru", "din", "nu", "se", "al", "ale", "cum",
    # en
    "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was",
    "what", "how", "when", "where", "which", "do", "does", "can", "with",
    "it", "this", "that", "be", "at", "as", "by", "from", "an",
}

MIN_PREFIX = 5          # shared prefix length that counts as a partial hit
PARTIAL_WEIGHT = 0.55   # a stem match is worth this much of an exact match
HEADING_BOOST = 2.5     # a query word in the heading is a strong signal
TITLE_BOOST = 1.6

# Length normalisation, BM25-style. Without it a long section wins almost
# every query simply by containing more words: in the law corpus the median
# section is ~670 characters and the worst offenders are 20-50k with nearly
# 500 distinct terms, so "what do I need to register a servitude" came back
# with the ANCPI fee table and "what documents for a first registration" with
# a 22k-character annex of preemption forms. B is how much length is punished
# (0 = not at all, 1 = fully); K1 is the saturation constant.
LEN_B = 0.6      # swept over a 15-question benchmark; see tests/test_retrieval.py
LEN_K1 = 1.5

# A section longer than this is INDEXED as several chunks. Length
# normalisation alone cannot rescue a 25k-character fee table or a
# 53k-character article: normalised, they lose every query; unnormalised, they
# win every query. Neither is retrieval. Splitting happens here, at indexing
# time, and NOT in the stored markdown — `lawlib.article` reads the file and
# must still hand back a whole article.
MAX_CHUNK = 5000

# A query term the caller INFERRED — a glossary translation, not a word the
# person typed — counts for less. "telekhatár" expands to limită and hotar;
# "hotar" is decisive, but "limită" is ordinary legal Romanian that appears in
# tax indexation articles as readily as in setback rules, and at full weight
# it decided the query.
WEAK_TERM_WEIGHT = 0.45


def fold(s: str) -> str:
    """Lowercase and strip diacritics: 'Telekkönyvezés' -> 'telekkonyvezes',
    'carte funciară' -> 'carte funciara'. Hungarian ő/ű decompose under NFKD
    like every other accented vowel."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", fold(s))
            if len(t) > 1 and t not in STOPWORDS]


# --- loading -----------------------------------------------------------------

def indexable(filename: str) -> bool:
    """Which files are corpus documents.

    `_`-prefixed files are notes ABOUT the corpus (the manifest, a glossary)
    and must never be served as an answer. README.md is documentation for the
    people who maintain the corpus; indexed, it would outrank real articles
    because it is the one document that mentions every term at once."""
    return (filename.endswith(".md")
            and not filename.startswith("_")
            and filename.lower() != "readme.md")


def _title_of(text: str, path: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        return m.group(1).strip()
    return os.path.splitext(os.path.basename(path))[0].replace("-", " ")


def split_long(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """One long body -> several indexable pieces, cut at blank lines.

    Cutting on paragraph boundaries keeps a table row, an alineat or a tariff
    line whole; a fixed-width cut would slice "Recepție dezlipire/alipire |
    60" down the middle and make it findable by neither half."""
    if len(text or "") <= limit:
        return [text]
    out, buf, size = [], [], 0
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p:
            continue
        if size and size + len(p) > limit:
            out.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 2
    if buf:
        out.append("\n\n".join(buf))
    return out or [text]


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)] split on markdown ## / ### lines. Text before the
    first subheading is kept under the empty heading so a short doc with only
    an H1 is still indexed."""
    out: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in text.splitlines():
        m = re.match(r"^(#{2,4})\s+(.+?)\s*#*$", line)
        if m:
            if "".join(buf).strip():
                out.append((heading, "\n".join(buf).strip()))
            heading, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((heading, "\n".join(buf).strip()))
    return out


def load(root: str) -> list[dict]:
    """Every markdown section under `root` as one searchable chunk.

    `area` is the top-level subdirectory, if any, kept so a caller can tell
    sources apart."""
    root = os.path.abspath(os.path.expanduser(root))
    chunks: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if not indexable(fn):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            title = _title_of(text, path)
            area = rel.split(os.sep)[0] if os.sep in rel else ""
            for i, (heading, body) in enumerate(split_sections(text)):
                for j, piece in enumerate(split_long(body)):
                    chunks.append({
                        "id": f"{rel}#{i}.{j}",
                        "path": rel,
                        "area": area,
                        "title": title,
                        "heading": heading,
                        "part": j,
                        "text": piece,
                    })
    return chunks


def signature(root: str) -> tuple:
    """Cheap change detector: (path, mtime, size) for every indexed file."""
    root = os.path.abspath(os.path.expanduser(root))
    sig = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if indexable(fn):
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                sig.append((p, st.st_mtime_ns, st.st_size))
    return tuple(sorted(sig))


class Index:
    """Loaded-once, reloaded-when-the-files-change index.

    A long-running server keeps one of these for its lifetime; `refresh()` is
    called before every search so a re-fetched act is searchable immediately
    instead of after a restart."""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self._sig: tuple = ()
        self.chunks: list[dict] = []
        self.df: dict[str, int] = {}
        self.avg_len = 1.0
        self.refresh()

    def refresh(self) -> bool:
        sig = signature(self.root)
        if sig == self._sig and self.chunks:
            return False
        self.chunks = load(self.root)
        for c in self.chunks:
            c["_tokens"] = tokens(f"{c['title']} {c['heading']} {c['text']}")
            c["_head"] = set(tokens(c["heading"]))
            c["_title"] = set(tokens(c["title"]))
        self.df = {}
        for c in self.chunks:
            for t in set(c["_tokens"]):
                self.df[t] = self.df.get(t, 0) + 1
        lens = [len(c["_tokens"]) for c in self.chunks]
        self.avg_len = (sum(lens) / len(lens)) if lens else 1.0
        self._sig = sig
        return True

    # --- scoring -------------------------------------------------------------

    def _idf(self, term: str) -> float:
        """Rare words decide the ranking; 'telekkonyv' appears in half the
        corpus and must not outvote 'dezmembrare'."""
        n = len(self.chunks) or 1
        return 1.0 + (n / (1.0 + self.df.get(term, 0))) ** 0.5

    def _match(self, term: str, chunk: dict) -> float:
        """Exact token hit, else a shared-prefix (poor-man's stemmer) hit."""
        counts = 0
        partial = 0
        for t in chunk["_tokens"]:
            if t == term:
                counts += 1
            elif len(term) >= MIN_PREFIX and len(t) >= MIN_PREFIX and (
                    t.startswith(term[:MIN_PREFIX]) or term.startswith(t[:MIN_PREFIX])):
                partial += 1
        if not counts and not partial:
            return 0.0
        # Saturating — ten mentions of a word do not make a chunk ten times
        # better — and length-normalised, so a 50k-character article has to
        # work harder for the same score than a 600-character one.
        raw = counts + PARTIAL_WEIGHT * partial
        norm = LEN_K1 * (1 - LEN_B
                         + LEN_B * len(chunk["_tokens"]) / (self.avg_len or 1))
        return (raw / (raw + norm)) * self._idf(term)

    def score(self, qterms: list[str], chunk: dict,
              weak: set | None = None) -> float:
        total = 0.0
        hit_terms = 0
        for term in qterms:
            s = self._match(term, chunk)
            if s <= 0:
                continue
            hit_terms += 1
            if term in chunk["_head"]:
                s *= HEADING_BOOST
            elif term in chunk["_title"]:
                s *= TITLE_BOOST
            if weak and term in weak:
                s *= WEAK_TERM_WEIGHT
            total += s
        if not hit_terms:
            return 0.0
        # covering more of the question beats hammering one word
        return total * (hit_terms / len(qterms)) ** 0.5

    def search(self, query: str, limit: int = 6,
               char_budget: int = 9000, weak: str = "") -> list[dict]:
        """Top sections for a question, newest-first within equal scores.

        char_budget caps what goes back to the model: a tool result is re-sent
        on every later turn, so an unbounded dump is paid for many times
        over."""
        self.refresh()
        qterms = tokens(query)
        if not qterms:
            return []
        weak_terms = set(tokens(weak)) - set(tokens(query.replace(weak, " ")))
        scored = []
        for c in self.chunks:
            s = self.score(qterms, c, weak=weak_terms)
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda p: -p[0])
        out, used = [], 0
        for s, c in scored[:limit * 3]:
            body = c["text"][:2400]
            if used + len(body) > char_budget and out:
                break
            used += len(body)
            out.append({k: c[k] for k in ("id", "path", "area", "title", "heading")}
                       | {"text": body, "score": round(s, 3)})
            if len(out) >= limit:
                break
        return out

    # --- whole documents -----------------------------------------------------

    def paths(self) -> list[str]:
        self.refresh()
        seen, out = set(), []
        for c in self.chunks:
            if c["path"] not in seen:
                seen.add(c["path"])
                out.append(c["path"])
        return out

    def stats(self) -> dict:
        self.refresh()
        by_area: dict[str, int] = {}
        for p in self.paths():
            by_area[p.split(os.sep)[0]] = by_area.get(p.split(os.sep)[0], 0) + 1
        return {"docs": len(self.paths()), "sections": len(self.chunks),
                "terms": len(self.df), "by_area": by_area}
