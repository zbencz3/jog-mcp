"""How the Portal Legislativ marks up an act, and how we turn it into markdown.

Everything in this module is knowledge about `legislatie.just.ro`'s HTML and
nothing about our corpus — the job that decides WHICH acts to fetch and when
lives in `acts.py`. Same split as `legal.py`, which knows how to download a
page and nothing about what it is for.

The markup is regular. Each structural element of kind K appears as a
container `S_K` holding `S_K_TTL` (its number — "Articolul 24"), `S_K_DEN`
(its name) and `S_K_BDY` (its content):

    document   S_DEN title · S_EMT emitter · S_PUB Monitorul Oficial reference
    grouping   S_PRT part · S_TTL title · S_CAP chapter · S_SEC section · S_POR
    section    S_ART article · S_ANX annex
    inline     S_ALN alineat "(2)" · S_LIT litera "b)" · S_PCT point · S_LIN dash

So an article boundary is a parse, not a heuristic, and an answer can cite
"Art. 24 alin. (2)" exactly as a surveyor would. A PDF pipeline would throw
that structure away and make us guess it back from indentation.

Three oddities of the real pages, each of which cost a bug:

* Every `S_*_BDY` has an `S_*_SHORT` twin holding the literal string "..." —
  the collapsed-preview placeholder. Including it put a stray ellipsis after
  every litera.
* `S_EMT_BDY` wraps a bare `<li>`. Flushing the text buffer on every end tag,
  rather than only on the ones being captured, silently dropped the emitter.
* The signature block is sometimes marked up INSIDE the last article, and
  sometimes as plain `S_PAR` that cannot be told apart from the act's text.

"""
from __future__ import annotations

import re
from html.parser import HTMLParser

DETAIL_URL = "https://legislatie.just.ro/Public/DetaliiDocumentAfis/{doc_id}"

# What we DOWNLOAD, as opposed to the link we show people. Same document, same
# markup, byte-for-byte the same parsed text — but the "Afis" page is rendered
# per request and took 119 s for the 1.8 MB Regulament 600/2023, one second
# under the portal's own 120 s gateway timeout. From 2026-09-24 it lost that
# race every morning and the daily card reported a 504. The plain page served
# the same document in 3 s, and it also serves the 2026 consolidations
# (312552, 307663) that had 504'd for weeks and were written off as broken.
FETCH_URL = "https://legislatie.just.ro/Public/DetaliiDocument/{doc_id}"

# Bump when a change to this module alters the TEXT we extract. An act
# whose content hash is unchanged but whose parser version is not was
# re-rendered by US, not amended by Parliament, and must never be reported
# as a legal change.
#
# 1 -> 2: S_PAR started a new line inside a section, and table cells gained
# " | ". Six acts re-rendered, and every one was reported as amended. Nothing
# had changed: the entire diff was line breaks where two sentences had been
# glued together ("funciară.Documentele"). Alert fatigue, caused by the
# watcher itself.
PARSER_VERSION = 2

# Kinds that become one searchable chunk each.
SECTION_KINDS = ("ART", "ANX")

# Kinds that only set the breadcrumb above a section. Nested: a section sits
# inside a chapter sits inside a part.
GROUP_KINDS = ("PRT", "TTL", "CAP", "SEC", "SSEC", "POR")

# Kinds whose title opens a new line inside a body: "(2)", "b)", "1.", "-".
INLINE_KINDS = ("ALN", "LIT", "PCT", "LIN")

# Document-level singletons.
_DOC_FIELDS = {"S_DEN": "title", "S_EMT_BDY": "emitent", "S_PUB_BDY": "publicat"}

# Chrome, not text. The "+" collapse toggle sits inside every article and would
# otherwise land in the middle of its title; the signature block is noise; and
# every S_*_SHORT is the portal's collapsed-preview placeholder, which holds
# the literal string "..." while its S_*_BDY sibling holds the real text
# (verified 2026-09-19 — without this every litera ends in a stray ellipsis).
_CHROME = ("TAG_COLLAPSED", "S_SMN", "S_SMN_PAR")

_CLS_RE = re.compile(r"^S_([A-Z]+)(?:_([A-Z]+))?$")
# A line that is ONLY an enumerator: "(2)", "b)", "3.", "-". The portal puts
# the marker in its own span, so how much whitespace lands between it and its
# body depends on the page's indentation rather than on the law. Rejoining
# them here makes the rendered article independent of that.
_LONE_MARKER = re.compile(
    r"^(\(\d+(?:\^\d+)?\)|[a-zA-Z]\)|\d+\.|[-–—])$")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")


def _split_cls(cls: str):
    """'S_CAP_TTL' -> ('CAP', 'TTL'); 'S_ART' -> ('ART', ''); 'nav' -> (None, None)."""
    m = _CLS_RE.match(cls or "")
    if not m:
        return None, None
    return m.group(1), (m.group(2) or "")


def _clean(s: str) -> str:
    s = (s or "").replace("\xa0", " ")
    s = _WS.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.splitlines())
    return _BLANK.sub("\n\n", s).strip()


def _join_markers(text: str) -> str:
    """Put each enumerator back on the same line as the text it introduces.

    Blank lines between the two are tolerated: how much whitespace the portal
    leaves between a marker span and its body span is a property of the page's
    indentation, not of the law."""
    out = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        k = len(out) - 1
        while k >= 0 and out[k] == "":
            k -= 1
        if k >= 0 and _LONE_MARKER.match(out[k]):
            del out[k + 1:]
            out[k] = f"{out[k]} {ln}"
        else:
            out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


class ActParser(HTMLParser):
    """Collects the act's metadata, its grouping breadcrumbs and its sections.

    A stack of open element classes is kept because the markup nests
    (S_CAP > S_CAP_BDY > S_ART > S_ART_BDY > S_ALN > S_ALN_BDY) and the
    innermost enclosing element decides where a run of text belongs.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title = ""
        self.emitent = ""
        self.publicat = ""
        self.preamble = []
        self.sections = []           # [{"kind","ttl","den","body","group"}]
        self._groups = []            # open grouping elements, outermost first
        self._sec = None
        self._buf = []
        self._titling = 0            # >0 while inside a section/group TTL or DEN
        self._cell = False           # inside a <td>: paragraphs stay on the row
        self._table = 0              # <table> depth; newlines in the SOURCE
                                     # must not break a row apart

    # --- helpers ---------------------------------------------------------

    def _breadcrumb(self) -> str:
        parts = []
        for g in self._groups:
            label = g["ttl"]
            if g["den"]:
                label = f"{label} — {g['den']}" if label else g["den"]
            if label:
                parts.append(label)
        return " / ".join(parts)

    def _flush(self) -> str:
        t = _clean("".join(self._buf))
        self._buf = []
        return t

    def _emit(self, text: str) -> None:
        """Append to whichever buffer is currently being filled."""
        if self._skipping():
            return
        if self._sec is not None and not self._titling:
            self._sec["body"].append(text)
        else:
            self._buf.append(text)

    def _skipping(self) -> bool:
        return any(c in ("__skip__",) + _CHROME or c.endswith("_SHORT")
                   for c in self.stack)

    # --- HTMLParser ------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.stack.append("__skip__")
            return
        cls = (dict(attrs).get("class") or "").strip()
        if tag == "br":
            self._emit("\n")
            return
        # Tables carry real content here — the whole ANCPI tariff list is one,
        # a grid of service / code / price / unit. Stripping the tags without
        # marking the cell boundaries turns it into
        # "Grupă serviciiCodServiciuTarif (lei)UM", which is unreadable and
        # unquotable. A row is a line and a cell is a pipe.
        if tag == "table":
            self._table += 1
            self._cell = False
            self._emit("\n")
            return
        if tag == "tr":
            self._cell = False
            self._emit("\n")
            return
        if tag in ("td", "th"):
            self._cell = True
            self._emit(" | ")
            return
        self.stack.append(cls)
        if cls in _DOC_FIELDS:
            # reset, or the flush at the close of S_DEN also hands back the
            # page <title> and the whole navigation menu that preceded it
            self._buf = []
            return
        kind, part = _split_cls(cls)
        if kind is None:
            return
        if cls == "S_PAR":
            # A paragraph starts a line — except inside a table cell, where
            # the cell IS the unit and a "Tarif" / "(lei)" split across two
            # paragraphs must stay on one row. NOT reachable from the generic
            # `part == ""` branch below, because _split_cls("S_PAR") yields
            # kind="PAR", part="" — the same shape that silently killed the
            # preamble once already.
            self._emit(" " if self._cell else "\n")
            return
        if part == "":
            if kind in GROUP_KINDS:
                self._groups.append({"kind": kind, "ttl": "", "den": ""})
            elif kind in SECTION_KINDS:
                # a section inside a section (an annex quoting an article) is
                # not something the corpus needs to model; the outer one wins
                if self._sec is None:
                    self._sec = {"kind": kind, "ttl": "", "den": "",
                                 "body": [], "notes": [],
                                 "group": self._breadcrumb()}
        elif part in ("TTL", "DEN"):
            if kind in GROUP_KINDS or kind in SECTION_KINDS:
                self._titling += 1
                self._buf = []
            elif kind in INLINE_KINDS and self._sec is not None:
                self._sec["body"].append("\n")


    def handle_endtag(self, tag):
        if not self.stack:
            return
        # These never push (see handle_starttag), so they must never pop —
        # a desynced stack closes sections early and swallows the rest of the
        # document, which is exactly what it did to the tariff table.
        if tag in ("br", "tr", "td", "th"):
            return
        if tag == "table":
            self._table = max(0, self._table - 1)
            self._cell = False
            return
        cls = self.stack.pop()
        if cls == "__skip__":
            return
        kind, part = _split_cls(cls)

        if cls in _DOC_FIELDS:
            text = self._flush()
            field = _DOC_FIELDS[cls]
            if text and not getattr(self, field):
                setattr(self, field, text)
            return
        if kind is None:
            return                      # nested markup — keep accumulating

        if part in ("TTL", "DEN") and (kind in GROUP_KINDS or kind in SECTION_KINDS):
            text = self._flush()
            self._titling = max(0, self._titling - 1)
            target = None
            if kind in SECTION_KINDS and self._sec is not None and self._sec["kind"] == kind:
                target = self._sec
            elif kind in GROUP_KINDS and self._groups and self._groups[-1]["kind"] == kind:
                target = self._groups[-1]
            if target is not None:
                target[part.lower()] = text
            return

        if part == "" and kind in GROUP_KINDS:
            if self._groups and self._groups[-1]["kind"] == kind:
                self._groups.pop()
            return
        if part == "" and kind in SECTION_KINDS:
            if self._sec is not None and self._sec["kind"] == kind:
                body = _join_markers(_clean("".join(self._sec["body"])))
                self._sec["body"] = body
                if self._sec["ttl"] or body:
                    self.sections.append(self._sec)
                self._sec = None
            return

        if cls == "S_PAR" and self._sec is None:
            text = self._flush()
            if not text:
                return
            # A loose paragraph BETWEEN sections is the portal's consolidation
            # annotation for the section it follows — "(la 28-01-2020,
            # Articolul 24 a fost modificat de ...)". Attaching it to the
            # preceding section keeps "this rule changed, and when" together
            # with the rule; floating them all into one preamble bucket would
            # strand an amendment note far from the article it amends.
            if self.sections:
                self.sections[-1]["notes"].append(text)
            else:
                self.preamble.append(text)

    def handle_data(self, data):
        if self._skipping():
            return
        if self._table and "\n" in data:
            # Whether the portal pretty-prints its table markup is not a fact
            # about the law. A newline between </td> and <td> must not split
            # a row; only <tr> starts a new line.
            data = data.replace("\n", " ")
        if self._sec is not None and not self._titling:
            self._sec["body"].append(data)
        else:
            self._buf.append(data)


def parse(html_text: str) -> dict:
    """Metadata plus one record per article / annex, in document order."""
    p = ActParser()
    p.feed(html_text or "")
    p.close()
    return {
        "title": p.title,
        "emitent": p.emitent,
        "publicat": p.publicat,
        "preamble": p.preamble,
        "sections": p.sections,
        "articles": [s for s in p.sections if s["kind"] == "ART"],
        "annexes": [s for s in p.sections if s["kind"] == "ANX"],
    }
# --- rendering ---------------------------------------------------------------

def _heading(sec: dict) -> str:
    """'## Articolul 24 — Continutul documentatiei cadastrale'.

    This is what `kb.split_sections` keys a chunk on and what search boosts, so
    the number has to appear verbatim: a surveyor asks "mit mond a 24-es
    cikkely" and "Articolul 24" must match."""
    ttl = sec["ttl"] or ("Anexă" if sec["kind"] == "ANX" else "Articol")
    den = (sec.get("den") or "").strip()
    return f"{ttl} — {den}" if den else ttl


def render(act_id: str, doc_id: str, parsed: dict,
           fetched: str = "", text_hash: str = "") -> str:
    """The act as markdown, one `##` section per article and per annex.

    The metadata block is an HTML comment: the refresh job reads it back, and
    it never shows up in an answer."""
    title = parsed["title"] or act_id
    out = [f"# {title}", ""]
    meta = [
        ("act_id", act_id),
        ("doc_id", doc_id),
        ("source", DETAIL_URL.format(doc_id=doc_id)),
        ("emitent", parsed["emitent"]),
        ("publicat", parsed["publicat"]),
        ("fetched", fetched),
        ("text_hash", text_hash),
        ("articles", len(parsed["articles"])),
        ("annexes", len(parsed["annexes"])),
        ("parser", PARSER_VERSION),
    ]
    out.append("<!-- jog-mcp:act")
    for k, v in meta:
        out.append(f"{k}: {v}")
    out.append("-->")
    out.append("")

    if parsed["preamble"]:
        out += ["## Preambul", ""] + parsed["preamble"] + [""]

    group = None
    for sec in parsed["sections"]:
        if sec["group"] and sec["group"] != group:
            group = sec["group"]
            out += [f"### {group}", ""]
        out += [f"## {_heading(sec)}", ""]
        if sec["group"]:
            # repeated inside the section because retrieval returns ONE
            # section: without this the reader cannot tell which chapter an
            # article came from, and chapter context changes what it means
            out += [f"*{sec['group']}*", ""]
        if sec["body"]:
            out += [sec["body"], ""]
        for n in sec.get("notes") or []:
            out += [f"> {n}", ""]
    return _BLANK.sub("\n\n", "\n".join(out)).strip() + "\n"


