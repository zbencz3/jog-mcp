"""The law corpus, as something to search and cite: acts, articles, retrieval.

Search is NOT reimplemented here. `search.py` already scores Hungarian and
Romanian text with diacritic folding and a prefix stem, it is tested, and it
should be the one module that knows how search works. This adds only what a
corpus of statutes needs that a corpus of plain notes does not:

  * an `Act` record parsed from the metadata comment at the top of each act
    file, so an answer can say which act and which consolidation it came from;
  * `article()`, an exact lookup by number — "art. 24 din Regulament" is a
    citation, not a search query, and must not depend on ranking;
  * act references resolved the way a person writes them ("7/1996",
    "regulament", "területrendezés"), ambiguity refused rather than guessed;
  * result rows carrying the act and chapter, because retrieval returns ONE
    article and "Articolul 89" alone does not say which of many acts you are
    reading.

Read-only by construction: nothing here writes to `acts/`. The corpus is
produced by `jog_mcp.fetch` (a person running `jog-mcp-fetch`) and consumed
here. That is most of what makes it safe to put behind a network listener.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import search

# "Articolul 24", "Art. 24", "art 24", "24" — all the ways a citation arrives.
_ART_NUM = re.compile(r"(?:art(?:icolul|\.)?\s*)?(\d+)(?:\s*\^?\s*(\d+))?", re.I)
_ANX_NUM = re.compile(r"(?:anexa?\s*(?:nr\.?)?\s*)?([\d.]+)", re.I)


@dataclass
class Act:
    act_id: str
    path: str
    title: str = ""
    doc_id: str = ""
    source: str = ""
    emitent: str = ""
    publicat: str = ""
    fetched: str = ""
    text_hash: str = ""
    articles: int = 0
    annexes: int = 0
    note: str = ""
    # A repealed act stays in the corpus — transitional provisions mean a file
    # opened before the repeal still runs under it — but every excerpt and
    # every lookup has to say so, unmissably. Quoting dead law at a counter is
    # the worst thing this corpus can do.
    repealed_on: str = ""
    repealed_by: str = ""
    superseded_by: str = ""
    # A PARTIAL repeal: only these articles fell, the rest of the act stands.
    # Flagging the whole act would be wrong — Legea 10/1995 lost art. 1-9,
    # 11-40 and 42-44 to the Codul urbanismului while art. 10, 41 and 45
    # onward are still in force. This is the kind of repeal that survives
    # unnoticed for years, because the act itself is not dead.
    repealed_articles: str = ""
    # A friendly name for the act ("Legea 7/1996 — kataszter..."), from
    # acts.yaml. `title` stays the official Romanian one off the markdown H1:
    # both are needed, because people search by the first and the land
    # registry counter wants the second.
    name: str = ""
    aliases: list = field(default_factory=list)

    def cite(self) -> str:
        return f"{self.title} ({self.source})" if self.source else self.title

    def dead_article(self, ttl: str) -> bool:
        """Is THIS article one of the repealed ones?

        Ranges come from acts.yaml as "1-9,11-40,42-44". An article outside them
        is still in force even though its neighbours are not."""
        if not self.repealed_articles:
            return False
        n = norm_article(ttl)
        n = int(n.split("^")[0]) if n.split("^")[0].isdigit() else None
        if n is None:
            return False
        for part in self.repealed_articles.split(","):
            part = part.strip()
            if not part:
                continue
            lo, _, hi = part.partition("-")
            try:
                if int(lo) <= n <= int(hi or lo):
                    return True
            except ValueError:
                continue
        return False

    def article_warning(self, ttl: str) -> str:
        """The warning for one article of a partially repealed act."""
        if not self.repealed_articles:
            return self.warning()
        if not self.dead_article(ttl):
            return ""
        w = f"⚠️ EZ A CIKKELY HATÁLYON KÍVÜL {self.repealed_on} óta"
        if self.repealed_by:
            w += f" — {self.repealed_by}"
        if self.superseded_by:
            w += f"; helyette: {self.superseded_by}"
        return w

    def warning(self) -> str:
        """The line that must appear wherever this act is quoted."""
        if self.repealed_articles and not self.repealed_on:
            return ""
        if self.repealed_articles:
            return (f"⚠️ RÉSZBEN hatályon kívül {self.repealed_on} óta "
                    f"(art. {self.repealed_articles}) — {self.repealed_by}")
        if not self.repealed_on:
            return ""
        w = f"⚠️ HATÁLYON KÍVÜL {self.repealed_on} óta"
        if self.repealed_by:
            w += f" — {self.repealed_by}"
        if self.superseded_by:
            w += f"; helyette: {self.superseded_by}"
        return w


def _read_meta(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(4000)
    except OSError:
        return {}
    m = re.search(r"<!--\s*jog-mcp:act\s*(.*?)-->", head, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _doc_title(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return ""


def _match_key(s: str) -> str:
    """Fold for act matching: diacritics off, and every separator a dash, so
    "7/1996", "7.1996" and "legea-7-1996" all reduce to the same shape."""
    return re.sub(r"[^a-z0-9]+", "-", search.fold(s or "")).strip("-")


# A section longer than this is skimmed rather than truncated. The ANCPI
# tariff grid is one 10k-character annex: truncating it hands back the top of
# the table ("avize și recepții") whatever you asked about, which for a price
# list is worse than no answer.
FOCUS_OVER = 2000
FOCUS_LINES = 14

# How much it counts that a query uses the friendly name for an act. The
# markdown title is the Romanian official one ("ORDIN nr. 16 din 7 ianuarie
# 2019"), which contains none of the words anybody searches for; acts.yaml
# calls that act "a hivatali illetékek (tarife)" precisely so it can be
# found. Without this, "mennyibe kerül egy extras de carte funciară"
# loses the fee schedule to the Regulament article that happens to mention
# extrase more often.
ALIAS_BOOST = 0.6
RERANK_POOL = 12          # candidates considered before the alias re-rank
SCOPED_POOL = 400         # …and when the caller pins one act


def focus(text: str, query: str) -> str:
    """The lines of a long section that actually answer the question.

    Keeps document order and always keeps the first line, because in a table
    that is the header row and a price without its column headings ("60" —
    lei? per what?) is not quotable."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if len(text or "") <= FOCUS_OVER or len(lines) < 4:
        return text
    qterms = set(search.tokens(query))
    if not qterms:
        return text
    scored = []
    for i, ln in enumerate(lines[1:], start=1):
        hits = len(qterms & set(search.tokens(ln)))
        if hits:
            scored.append((hits, -i, i))
    if not scored:
        return text
    scored.sort(reverse=True)
    keep = sorted({i for _, _, i in scored[:FOCUS_LINES]})
    out = [lines[0]]
    prev = 0
    for i in keep:
        if i - prev > 1:
            out.append("…")
        out.append(lines[i])
        prev = i
    if prev < len(lines) - 1:
        out.append("…")
    return "\n".join(out)


def norm_article(ref: str) -> str:
    """'art. 24', 'Articolul 24', '24' -> '24'. '24^1' -> '24^1'."""
    m = _ART_NUM.search(ref or "")
    if not m:
        return ""
    return f"{m.group(1)}^{m.group(2)}" if m.group(2) else m.group(1)


class LawLibrary:
    """Loaded once, reloaded when acts/ changes on disk."""

    def __init__(self, root: str, tracked: dict | None = None,
                 fallback=None, glossary=None):
        """`tracked` is acts.yaml's `track` list, keyed by id. It supplies a
        friendly name for each act and its repeal status — people ask for "a
        700-as szabályzat" or "területrendezés", not for the Romanian official
        title that happens to be the markdown H1."""
        self.root = os.path.abspath(os.path.expanduser(root))
        self.tracked = tracked or {}
        self._fallback_factory = fallback
        self._fallback = None
        # The corpus is Romanian and stays Romanian; this bridges the QUERY.
        # Without it "telekkönyv" reaches nothing at all — see glossary.py.
        self.glossary = glossary
        self.index = search.Index(self.root)
        self._acts: dict = {}
        self._load_acts()

    # --- acts ------------------------------------------------------------

    def _load_acts(self) -> None:
        self._acts = {}
        if not os.path.isdir(self.root):
            return
        for fn in sorted(os.listdir(self.root)):
            if not fn.endswith(".md") or fn.startswith("_"):
                continue
            path = os.path.join(self.root, fn)
            meta = _read_meta(path)
            act_id = meta.get("act_id") or os.path.splitext(fn)[0]
            cfg = self.tracked.get(act_id) or {}
            self._acts[act_id] = Act(
                act_id=act_id,
                path=fn,
                title=_doc_title(path) or meta.get("act_id", act_id),
                doc_id=meta.get("doc_id", ""),
                source=meta.get("source", ""),
                emitent=meta.get("emitent", ""),
                publicat=meta.get("publicat", ""),
                fetched=meta.get("fetched", ""),
                text_hash=meta.get("text_hash", ""),
                articles=int(meta.get("articles") or 0),
                annexes=int(meta.get("annexes") or 0),
                note=str(cfg.get("note") or "").strip(),
                repealed_on=str(cfg.get("repealed_on") or "").strip(),
                repealed_by=str(cfg.get("repealed_by") or "").strip(),
                superseded_by=str(cfg.get("superseded_by") or "").strip(),
                repealed_articles=str(
                    cfg.get("repealed_articles") or "").strip(),
                name=str(cfg.get("title") or "").strip() or _doc_title(path)
                or act_id,
                aliases=[str(cfg.get("title") or ""), act_id,
                         str(cfg.get("note") or "")],
            )

    def refresh(self) -> bool:
        changed = self.index.refresh()
        if changed:
            self._load_acts()
            self._fallback = None      # the corpus moved; the vectors are stale
        return changed

    def _get_fallback(self):
        if self._fallback is None:
            factory = self._fallback_factory
            if factory is None:
                from .ngram import NgramFallback
                factory = NgramFallback
            self._fallback = factory(self.index.chunks)
        return self._fallback

    def acts(self) -> list:
        self.refresh()
        return [self._acts[k] for k in sorted(self._acts)]

    def act(self, act_id: str):
        self.refresh()
        return self._acts.get(act_id)

    def resolve_act(self, ref: str):
        """An id, a file name, a number, or a word from either the official
        title or the friendly name for the act.

        Matched in tiers, most specific first, because the sources differ in
        how much they mean: "regulament" appears in the ORDIN's note (which
        points at the regulation) as well as in the regulation's own id, and
        the id is the one that should win. A tier with more than one match is
        genuinely ambiguous and returns nothing — `candidates()` then lets the
        caller ask which one, since answering about the wrong act is worse
        than one extra round trip."""
        hits = self._tiered(ref)
        return hits[0] if len(hits) == 1 else None

    def candidates(self, ref: str) -> list:
        """The acts an ambiguous reference could have meant."""
        return self._tiered(ref)

    def _tiered(self, ref: str) -> list:
        self.refresh()
        r = _match_key(ref)
        if not r:
            return []
        acts = list(self._acts.values())
        tiers = [
            [a for a in acts if r in (_match_key(a.act_id), _match_key(a.path))],
            [a for a in acts if r in _match_key(a.act_id)],
            [a for a in acts if r in _match_key(a.title)],
            [a for a in acts if any(r in _match_key(x) for x in a.aliases if x)],
        ]
        for tier in tiers:
            uniq = list({a.act_id: a for a in tier}.values())
            if uniq:
                return uniq
        return []

    # --- retrieval -------------------------------------------------------

    def _decorate(self, hit: dict) -> dict:
        act = self._acts.get(os.path.splitext(hit["path"])[0].split(os.sep)[-1])
        if act is None:
            for a in self._acts.values():
                if a.path == hit["path"]:
                    act = a
                    break
        text = hit["text"]
        chapter = ""
        m = re.match(r"\*(.+?)\*\s*\n", text)
        if m:
            chapter = m.group(1).strip()
            text = text[m.end():].lstrip()
        return {
            "act_id": act.act_id if act else "",
            "act_title": act.title if act else "",
            "source": act.source if act else "",
            # WHICH VERSION this text is. An answer that cites "art. 89" without
            # saying which consolidation it came from is not checkable, and the
            # corpus deliberately pins one consolidation per act.
            "publicat": act.publicat if act else "",
            "fetched": act.fetched if act else "",
            "repealed": (act.article_warning(hit["heading"]) if act else ""),
            "article": hit["heading"],
            "chapter": chapter,
            "text": text,
            "score": hit["score"],
        }

    def search(self, query: str, limit: int = 6, act: str = "",
               char_budget: int = 9000, use_fallback: bool = True) -> list:
        """Keyword first, character-n-gram similarity only to top up.

        The keyword index decides the common case and its ranking is the one
        that was tuned. The fallback exists for the query whose words are
        right but whose forms are wrong ("dezmembrarii" vs
        "dezmembrămintelor"), and it runs only when the keyword pass came back
        short — so a normal question never pays the cost of building it. See
        ngram.py for what this does and does not buy."""
        self.refresh()
        # Glossary translations are appended AND marked weak: the index scores
        # them at a fraction, because a word we inferred is not as good
        # evidence as one the user typed.
        weak = ""
        if self.glossary is not None:
            expanded = self.glossary.expand(query)
            weak = expanded[len(query):].strip()
            query = expanded
        # Always pull a WIDER pool than we return: the re-rank below cannot
        # promote a hit that was never a candidate, and with limit=1 there is
        # nothing to re-rank at all.
        #
        # When the caller pins an act the pool must be wider still, because
        # the filter runs AFTER scoring: the 584-article Codul urbanismului
        # can fill the top twelve on its own, and a search scoped to another
        # act would then come back empty even though that act matches.
        wide = SCOPED_POOL if act else max(limit * 3, RERANK_POOL)
        hits = self.index.search(query, limit=wide, weak=weak,
                                 char_budget=char_budget * (40 if act else 3))
        rows = [self._decorate(h) for h in hits]
        rows = self._rerank(rows, query)
        for r in rows:
            r["text"] = focus(r["text"], query)
        want = self.resolve_act(act) if act else None
        if act and want is not None:
            rows = [r for r in rows if r["act_id"] == want.act_id]

        if use_fallback and len(rows) < limit:
            seen = {(r["act_id"], r["article"]) for r in rows}
            for i, score in self._get_fallback().score_all(query, limit=limit * 4):
                chunk = self.index.chunks[i]
                if not chunk.get("heading"):
                    continue        # the act's H1 blob, not a citable article
                row = self._decorate({
                    "path": chunk["path"], "heading": chunk["heading"],
                    "text": chunk["text"][:2400], "score": round(score, 3),
                })
                row["matched_by"] = "similarity"
                if want is not None and row["act_id"] != want.act_id:
                    continue
                if (row["act_id"], row["article"]) in seen:
                    continue
                seen.add((row["act_id"], row["article"]))
                rows.append(row)
                if len(rows) >= limit:
                    break
        return rows[:limit]

    def _rerank(self, rows: list, query: str) -> list:
        """Lift a hit whose ACT the question actually named.

        The keyword index scores the article's text and knows nothing about
        what people call the act. A question about a fee names
        "illeték"/"tarif", which appears in the act's configured title and
        nowhere in the statutory prose."""
        qt = set(search.tokens(query))
        if not qt:
            return rows
        for r in rows:
            act = self._acts.get(r.get("act_id") or "")
            if act is None:
                continue
            alias = set()
            for text in [act.name, act.note] + list(act.aliases):
                alias |= set(search.tokens(text or ""))
            if not alias:
                continue
            # Prefix-stemmed, the same way search.Index matches: acts.yaml
            # says "tarifa"/"tarife" and the user types "tarif".
            hit = 0
            for t in qt:
                if t in alias or any(
                        len(t) >= search.MIN_PREFIX and len(a) >= search.MIN_PREFIX
                        and (a.startswith(t[:search.MIN_PREFIX])
                             or t.startswith(a[:search.MIN_PREFIX]))
                        for a in alias):
                    hit += 1
            overlap = hit / len(qt)
            r["score"] = round(r["score"] * (1 + ALIAS_BOOST * overlap), 3)
        rows.sort(key=lambda r: -r["score"])
        return rows

    def article(self, act_ref: str, number: str) -> dict | None:
        """Exact article lookup. A citation must not depend on ranking."""
        self.refresh()
        act = self.resolve_act(act_ref)
        if act is None:
            return None
        want = norm_article(number)
        if not want:
            return None
        path = os.path.join(self.root, act.path)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None
        for heading, body in search.split_sections(text):
            if not heading.lower().startswith("articolul"):
                continue
            if norm_article(heading) == want:
                chapter = ""
                m = re.match(r"\*(.+?)\*\s*\n", body)
                if m:
                    chapter = m.group(1).strip()
                    body = body[m.end():].lstrip()
                return {
                    "act_id": act.act_id,
                    "act_title": act.title,
                    "source": act.source,
                    "publicat": act.publicat,
                    "fetched": act.fetched,
                    "repealed": act.article_warning(heading),
                    "article": heading,
                    "chapter": chapter,
                    "text": body,
                }
        return None

    def headings(self, act_id: str) -> list:
        """[{'heading','chapter'}] for one act, in document order — the
        navigation aid for a 394-article regulation. Reads the file rather
        than the index because the index is keyed for scoring, not ordering."""
        self.refresh()
        act = self._acts.get(act_id)
        if act is None:
            return []
        try:
            with open(os.path.join(self.root, act.path), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return []
        out = []
        for heading, body in search.split_sections(text):
            if not heading or heading == "Preambul":
                continue
            m = re.match(r"\*(.+?)\*\s*\n", body)
            out.append({"heading": heading,
                        "chapter": m.group(1).strip() if m else ""})
        return out

    def stats(self) -> dict:
        self.refresh()
        acts = self.acts()
        return {
            "root": self.root,
            "acts": len(acts),
            "sections": len(self.index.chunks),
            "terms": len(self.index.df),
            "articles": sum(a.articles for a in acts),
            "annexes": sum(a.annexes for a in acts),
        }
