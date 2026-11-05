"""MCP server over the Romanian surveying-law corpus.

What it exposes, and why these tools and not a generic file reader:

  search_law    keyword search across every tracked act, or inside one
  get_article   exact lookup — "Art. 24 of the Regulament" is a citation, and
                a citation must not depend on a ranking function
  find_exact    literal phrase search, for numbers and fixed wording that
                ranked search cannot see ("15%", "30 de zile")
  outline       the article headings of one act, so a model can navigate a
                394-article regulation without pulling it into context
  list_acts     what the corpus holds and WHEN each act was fetched, so a
                stale answer is visible as a stale date

Security posture (the full threat model is in ../SECURITY.md). Three
properties are structural rather than enforced by care:

1. **Read-only by construction.** There is no tool that writes, and the
   process needs no write access to the repo. The corpus is refreshed by
   `jog-mcp-fetch`, which is a different program run by a person.
2. **No filesystem argument reaches a path.** `get_article` and `outline` take
   an act REFERENCE that is resolved against a dictionary of known acts, never
   joined into a path. `../../etc/passwd` does not name an act, so it resolves
   to nothing. There is no path to guard because no path is ever built.
3. **The corpus is public law.** Everything served here is published in
   Monitorul Oficial. A client with this server attached very likely also has
   web tools, and "read private data, then send it somewhere" is the classic
   exfiltration pair. Serving only public statute means that pair costs
   nothing if it happens. Adding private documents here would be a decision
   with a different threat model, not a one-line config change.

Transport. stdio for a local client; streamable HTTP with `--http`. HTTP
requires JOG_MCP_TOKEN to be set and checks it as a bearer token on every
request — see SECURITY.md for why that is the floor and not the ceiling.
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .glossary import Glossary
from .lawlib import LawLibrary
from .retrieve import find_exact as _find_exact

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_TEXT = 6000           # per article returned; the longest real one is ~5k
MAX_RESULTS = 12


def load_corpus(config_path: str = "", acts_root: str = "",
                glossary_path: str = "") -> LawLibrary:
    """The corpus, with acts.yaml supplying friendly names and repeal status.

    The server still starts without acts.yaml, for a client that only wants
    to read the corpus — but then no act is flagged as repealed, so ship it.

    The glossary is optional: it only widens a Hungarian QUERY into the
    corpus's Romanian. Without one, Hungarian words reach less."""
    tracked = {}
    root = acts_root
    cfg_path = config_path or os.path.join(REPO, "acts.yaml")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        tracked = {str(e.get("id")): e for e in (cfg.get("track") or [])}
        root = root or (cfg.get("root") or "").strip()
    except (OSError, yaml.YAMLError):
        pass
    gloss = Glossary(glossary_path) if glossary_path else None
    return LawLibrary(root or os.path.join(REPO, "acts"), tracked=tracked,
                      glossary=gloss)


# Every tool here only reads. Declaring it lets a client show the difference
# between this server and one that can act.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)


def build(corpus: LawLibrary) -> MCPServer:
    mcp = MCPServer(
        name="jog-mcp",
        instructions=(
            "Romanian surveying and cadastre law, full text, kept in this "
            "repository. Quote the article you rely on and name its act — "
            "people act on these answers in front of a land-registry "
            "counter. If the corpus does not cover the question, say so "
            "rather than answering from memory of Romanian law. "
            "SOME ACTS ARE REPEALED and kept only for files opened before the "
            "repeal: a result marked '!!' says so, names what replaced it, "
            "and must not be presented as current law — say it is repealed in "
            "your first sentence and answer from the replacement."),
        log_level="ERROR")

    def _fmt(row: dict, budget: int = MAX_TEXT) -> str:
        head = f"{row['article']} — {row['act_title']}"
        parts = [head]
        # A repealed act stays in the corpus for files opened before the
        # repeal, so it CAN be returned — and must never be returned quietly.
        # An early version served art. 89 of a regulation dead since 2023
        # with no warning.
        if row.get("repealed"):
            parts.append(f"!! {row['repealed']}")
        if row.get("chapter"):
            parts.append(f"({row['chapter']})")
        body = row["text"]
        if len(body) > budget:
            body = body[:budget] + "\n… [truncated]"
        parts.append("")
        parts.append(body)
        if row.get("source"):
            parts.append("")
            parts.append(f"Forrás / sursa: {row['source']}")
        # Which consolidation, and when it was fetched — without these a
        # citation cannot be checked or dated.
        if row.get("publicat"):
            parts.append(f"verzió: {row['publicat']}")
        if row.get("fetched"):
            parts.append(f"korpuszba került: {row['fetched'][:10]}")
        return "\n".join(parts)

    @mcp.tool(
        name="search_law",
        annotations=READ_ONLY,
        description=(
            "Search the Romanian surveying-law corpus (cadastre, land "
            "registry, construction permits, land fund) and return the "
            "matching articles verbatim. Use this for a question about what "
            "the law requires. Queries may be in Romanian or Hungarian; "
            "diacritics are optional. Returns the article text, its act and "
            "its chapter, so the answer can be cited."),
    )
    def search_law(
        query: str = Field(description="The question or the key terms"),
        act: str = Field(
            default="",
            description=("Optional: restrict to one act. Accepts an id "
                         "('regulament-700-2014'), a number ('7/1996') or a "
                         "word from its name ('urbanism')")),
        limit: int = Field(default=5,
                           description=f"How many articles (max {MAX_RESULTS})"),
    ) -> str:
        limit = max(1, min(int(limit or 5), MAX_RESULTS))
        if act:
            if corpus.resolve_act(act) is None:
                cands = corpus.candidates(act)
                if cands:
                    names = ", ".join(a.act_id for a in cands)
                    return (f"'{act}' matches more than one act: {names}. "
                            f"Ask again naming one of them.")
                known = ", ".join(a.act_id for a in corpus.acts())
                return f"No act matches '{act}'. Tracked acts: {known}"
        rows = corpus.search(query, limit=limit, act=act)
        if not rows:
            return (f"No article matches {query!r}"
                    + (f" in {act}" if act else "")
                    + ". Try fewer or more common words, or drop the act "
                      "filter.")
        budget = max(1200, MAX_TEXT // max(1, len(rows)))
        return "\n\n---\n\n".join(_fmt(r, budget) for r in rows)

    @mcp.tool(
        name="get_article",
        annotations=READ_ONLY,
        description=(
            "Return one article of one act verbatim, by number. Use this "
            "whenever an article is cited by number ('art. 24 din "
            "Regulament'); it is exact and does not depend on search "
            "ranking."),
    )
    def get_article(
        act: str = Field(description="Act id, number or a word from its name"),
        article: str = Field(
            description="Article number: '24', 'art. 24' or 'Articolul 24'"),
    ) -> str:
        if corpus.resolve_act(act) is None:
            cands = corpus.candidates(act)
            if cands:
                return (f"'{act}' matches more than one act: "
                        f"{', '.join(a.act_id for a in cands)}")
            known = ", ".join(a.act_id for a in corpus.acts())
            return f"No act matches '{act}'. Tracked acts: {known}"
        row = corpus.article(act, article)
        if row is None:
            a = corpus.resolve_act(act)
            return (f"{a.title} has no article {article!r} "
                    f"(it has {a.articles}). Use outline to list them.")
        return _fmt(row)

    @mcp.tool(
        name="find_exact",
        annotations=READ_ONLY,
        description=(
            "Find every article that contains a phrase LITERALLY (case and "
            "diacritics ignored). Use it for numbers and fixed wording that "
            "ranked search cannot see: '15%', '30 de zile', 'servitute de "
            "trecere', 'art. 41 alin. (10)'. Returns each match with the text "
            "around it; pull the whole article with get_article."),
    )
    def find_exact(
        phrase: str = Field(description="The exact words or number to find"),
        act: str = Field(default="", description="Optional: restrict to one act"),
    ) -> str:
        rows = _find_exact(corpus, phrase, act=act)
        if not rows:
            return f"No article contains {phrase!r}" + (f" in {act}" if act else "")
        return "\n\n---\n\n".join(_fmt(r, 1600) for r in rows)

    @mcp.tool(
        name="outline",
        annotations=READ_ONLY,
        description=(
            "List the article headings of one act, with its chapters. Use "
            "this to find the right article in a long regulation before "
            "pulling its text."),
    )
    def outline(
        act: str = Field(description="Act id, number or a word from its name"),
    ) -> str:
        a = corpus.resolve_act(act)
        if a is None:
            cands = corpus.candidates(act)
            if cands:
                return (f"'{act}' matches more than one act: "
                        f"{', '.join(x.act_id for x in cands)}")
            known = ", ".join(x.act_id for x in corpus.acts())
            return f"No act matches '{act}'. Tracked acts: {known}"
        lines = [f"{a.title}", f"{a.articles} articles, {a.annexes} annexes",
                 f"source: {a.source}", ""]
        chapter = None
        for row in corpus.headings(a.act_id):
            if row["chapter"] and row["chapter"] != chapter:
                chapter = row["chapter"]
                lines.append("")
                lines.append(chapter)
            lines.append(f"  {row['heading']}")
        return "\n".join(lines)

    @mcp.tool(
        name="list_acts",
        annotations=READ_ONLY,
        description=(
            "What the law corpus holds: each act, its size, the consolidation "
            "it came from and the date it was last fetched. Check this when "
            "an answer needs to state how current the law text is."),
    )
    def list_acts() -> str:
        out = []
        for a in corpus.acts():
            out.append(
                f"{a.act_id}\n"
                + (f"  !! {a.warning()}\n" if a.warning() else "")
                + f"  {a.title}\n"
                f"  {a.articles} articles, {a.annexes} annexes"
                + (f" · {a.emitent}" if a.emitent else "")
                + (f"\n  published: {a.publicat}" if a.publicat else "")
                + (f"\n  fetched:   {a.fetched}" if a.fetched else "")
                + (f"\n  note:      {a.note}" if a.note else "")
                + f"\n  source:    {a.source}")
        s = corpus.stats()
        out.append(f"\n{s['acts']} acts · {s['articles']} articles · "
                   f"{s['annexes']} annexes · {s['sections']} indexed sections")
        return "\n\n".join(out)

    # --- prompts -------------------------------------------------------
    # Two, both of which encode something the tools cannot enforce on their
    # own: that an answer about the law is only useful with the article behind
    # it, and that the corpus has a known edge which the model must say out
    # loud rather than paper over from its own memory of Romanian law.

    @mcp.prompt(
        name="jogi_kerdes",
        title="Jogi kérdés — idézettel",
        description=("Answer a question about Romanian surveying law using "
                     "only this corpus, quoting the article it rests on."))
    def jogi_kerdes(
        kerdes: str = Field(description="The question, in Hungarian or Romanian"),
        act: str = Field(default="",
                         description="Optional: restrict to one act"),
    ) -> str:
        scope = (f"Keresd csak a(z) {act} jogszabályban." if act
                 else "Keresd végig a teljes korpuszt.")
        return f"""Kérdés: {kerdes}

{scope}

Így válaszolj:
1. Keress a search_law eszközzel. Ha a kérdés konkrét cikkelyt említ
   ("art. 24"), get_article-t használj — az pontos, nem függ a találati
   sorrendtől.
2. A választ MINDIG arra a cikkelyre alapozd, amit visszakaptál, és idézd
   a lényegi mondatot szó szerint, románul, utána a magyar értelmezést.
3. Nevezd meg a jogszabályt és a cikkelyt (pl. "Legea 7/1996, art. 24"),
   és add meg a forrás linket.
4. Ha a korpusz nem fedi a kérdést, ezt mondd ki. NE válaszolj a román jog
   általános ismeretéből — a válasz alapján állnak oda a földhivatali
   pulthoz. A list_acts megmutatja, mit tartalmaz a korpusz és mikor
   frissült.
5. Ha a cikkely alatt módosítási megjegyzés van ("la 28-01-2020, ...
   a fost modificat/abrogat"), azt is említsd — a hatályos állapot számít."""

    @mcp.prompt(
        name="dosszie_lista",
        title="Dosszié — mit kell csatolni",
        description=("Assemble the required-documents list for a cadastral "
                     "procedure, each item traced to its article."))
    def dosszie_lista(
        eljaras: str = Field(
            description=("The procedure, e.g. 'dezmembrare', 'alipire', "
                         "'prima inscriere', 'apartamentare'")),
    ) -> str:
        return f"""Állítsd össze, mit kell tartalmaznia egy „{eljaras}" dossziénak.

1. search_law-val keresd meg a vonatkozó cikkelyeket a Regulament
   600/2023-ban (a hatályos recepție- és înscriere-szabályzat), és ahol a
   Legea 7/1996 alapot ad, azt is.
2. Tételes listát adj. MINDEN tétel mellé írd oda, melyik cikkelyből jön
   (pl. "borderou — Regulament 600/2023, art. … lit. a").
3. A cikkely szövegét idézd, ne foglald össze emlékezetből: a felsorolások
   (lit. a, b, c...) pontosan azok, amiket a földhivatal kér.
4. A végén külön sorold fel, amit a cikkelyek NEM mondanak meg, de a
   gyakorlatban kérdés lehet — és jelezd, hogy ez nem a jogszabályból jön.
5. Ha van hatályon kívül helyező vagy módosító megjegyzés az adott
   cikkelynél, azt emeld ki."""

    @mcp.resource("acts://list", mime_type="application/json")
    def acts_resource() -> str:
        import json
        return json.dumps(
            [{"act_id": a.act_id, "title": a.title, "articles": a.articles,
              "annexes": a.annexes, "publicat": a.publicat,
              "fetched": a.fetched, "source": a.source,
              "repealed": a.warning() or None}
             for a in corpus.acts()], ensure_ascii=False, indent=2)

    return mcp


def _http_app(mcp: MCPServer, token: str, host: str):
    """Wrap the streamable-HTTP app in a bearer-token check.

    This is a shared secret, which is the floor for "not open to whoever can
    reach the port" and not an identity system: every user holds the same
    token, so it cannot say who asked, and rotating it means telling everyone.
    Acceptable for a read-only corpus of published law; read SECURITY.md
    before putting anything non-public behind it.

    The comparison is constant-time. That matters little for a token this
    long, and costs nothing."""
    import hmac

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected = f"Bearer {token}"

    class RequireToken(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            got = request.headers.get("authorization", "")
            if not hmac.compare_digest(got, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app = mcp.streamable_http_app(host=host)
    app.add_middleware(RequireToken)
    return app


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="jog-mcp",
        description="MCP server over the Romanian surveying-law corpus")
    ap.add_argument("--config", default="", help="path to acts.yaml")
    ap.add_argument("--acts", default="", help="path to the acts/ corpus")
    ap.add_argument("--glossary", default="",
                    help="HU→RO glossary (markdown table); optional")
    ap.add_argument("--http", action="store_true",
                    help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--stats", action="store_true",
                    help="print what the corpus holds and exit")
    args = ap.parse_args(argv)

    corpus = load_corpus(args.config, args.acts, args.glossary)
    if args.stats:
        s = corpus.stats()
        print(f"corpus: {s['root']}")
        for a in corpus.acts():
            print(f"  {a.act_id:24} {a.articles:4} art {a.annexes:3} anx  "
                  f"fetched {a.fetched or '?'}")
        print(f"{s['acts']} acts · {s['articles']} articles · "
              f"{s['sections']} sections · {s['terms']} terms")
        return 0
    if not corpus.acts():
        print(f"no acts in {corpus.root} — run `jog-mcp-fetch` first",
              file=sys.stderr)
        return 1

    mcp = build(corpus)
    if not args.http:
        mcp.run(transport="stdio")
        return 0

    token = os.environ.get("JOG_MCP_TOKEN", "").strip()
    if not token:
        print("JOG_MCP_TOKEN is not set. HTTP mode serves the corpus to "
              "anything that can reach the port, so it refuses to start "
              "without a shared secret.", file=sys.stderr)
        return 1
    import uvicorn
    uvicorn.run(_http_app(mcp, token, args.host), host=args.host,
                port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
