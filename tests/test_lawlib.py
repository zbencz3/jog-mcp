"""The law corpus as something to search and cite (`jog_mcp/lawlib.py`).

These tests build a tiny corpus in a tmp_path rather than reading `acts/`, so
they say nothing about whether the real fetch worked (that is
`tests/test_acts.py`) and everything about the behaviour the tools
depend on: resolving an act the way a person names it, exact citation lookup,
and the two properties that make this safe to expose over HTTP.
"""
from __future__ import annotations

import pytest

from jog_mcp import lawlib
from jog_mcp.lawlib import LawLibrary, norm_article
from jog_mcp.ngram import MIN_SCORE, NgramFallback

ACT_A = """# LEGE nr. 7 din 13 martie 1996 (**republicată**)

<!-- jog-mcp:act
act_id: legea-7-1996
doc_id: 202841
source: https://legislatie.just.ro/Public/DetaliiDocumentAfis/202841
emitent: PARLAMENTUL
publicat: MONITORUL OFICIAL nr. 720 din 24 septembrie 2015
fetched: 2026-09-19T11:13:02Z
text_hash: abc123
articles: 2
annexes: 0
-->

## Articolul 1

*Capitolul I — Dispoziții generale*

Cadastrul este sistemul unitar de evidență a imobilelor.

## Articolul 24

*Capitolul II — Documentații cadastrale*

Documentația cadastrală se întocmește pe suport analogic și digital.
"""

ACT_B = """# REGULAMENTUL din 9 iulie 2014

<!-- jog-mcp:act
act_id: regulament-700-2014
doc_id: 222204
source: https://legislatie.just.ro/Public/DetaliiDocumentAfis/222204
emitent: ANCPI
publicat: MONITORUL OFICIAL nr. 571 bis din 31 iulie 2014
fetched: 2026-09-19T11:13:02Z
text_hash: def456
articles: 1
annexes: 1
-->

## Articolul 89

*Capitolul IV — Procedura de înscriere*

Documentația pentru înscrierea dezmembrămintelor cuprinde borderou.

## Anexa nr. 1.31

Formular de cerere pentru recepție.
"""

ACT_C = """# ORDIN nr. 700 din 9 iulie 2014 (*actualizat*)

<!-- jog-mcp:act
act_id: ordin-700-2014
doc_id: 186631
source: https://legislatie.just.ro/Public/DetaliiDocumentAfis/186631
emitent: ANCPI
fetched: 2026-09-19T11:13:02Z
text_hash: ghi789
articles: 1
annexes: 0
-->

## Articolul 1

Se aprobă Regulamentul prevăzut în anexă.
"""

TRACKED = {
    "legea-7-1996": {"id": "legea-7-1996",
                     "title": "Legea 7/1996 — kataszter és ingatlan-nyilvántartás",
                     "note": "Az alaptörvény."},
    "regulament-700-2014": {"id": "regulament-700-2014",
                            "title": "Regulament 700/2014 — avizare, recepție",
                            "note": "A napi munka szabálykönyve."},
    "ordin-700-2014": {"id": "ordin-700-2014",
                       "title": "Ordinul ANCPI 700/2014 — jóváhagyó rendelet",
                       "note": "Csak a jogi kapocs."},
}


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "legea-7-1996.md").write_text(ACT_A, encoding="utf-8")
    (tmp_path / "regulament-700-2014.md").write_text(ACT_B, encoding="utf-8")
    (tmp_path / "ordin-700-2014.md").write_text(ACT_C, encoding="utf-8")
    (tmp_path / "_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_notes.md").write_text("# not an act", encoding="utf-8")
    return LawLibrary(str(tmp_path), tracked=TRACKED)


# --- what the corpus holds --------------------------------------------------

def test_acts_are_loaded_with_their_metadata(corpus):
    ids = [a.act_id for a in corpus.acts()]
    assert ids == ["legea-7-1996", "ordin-700-2014", "regulament-700-2014"]
    a = corpus.act("legea-7-1996")
    assert a.doc_id == "202841"
    assert a.publicat.startswith("MONITORUL OFICIAL nr. 720")
    assert a.fetched == "2026-09-19T11:13:02Z"


def test_underscore_files_are_not_acts(corpus):
    """_manifest.json and _notes.md are bookkeeping, and serving either as an
    act would be as wrong as serving a notes file as an answer."""
    assert "_notes" not in [a.act_id for a in corpus.acts()]
    assert "_manifest" not in [a.act_id for a in corpus.acts()]


# --- naming an act ----------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("legea-7-1996", "legea-7-1996"),
    ("7/1996", "legea-7-1996"),          # separators normalise
    ("7.1996", "legea-7-1996"),
    ("kataszter", "legea-7-1996"),       # the friendly Hungarian name
    ("regulament", "regulament-700-2014"),
    ("REGULAMENT", "regulament-700-2014"),
])
def test_resolve_act(corpus, ref, expected):
    assert corpus.resolve_act(ref).act_id == expected


def test_ambiguous_reference_resolves_to_nothing(corpus):
    """Answering about the wrong act is worse than one more round trip."""
    assert corpus.resolve_act("700") is None
    assert {a.act_id for a in corpus.candidates("700")} == {
        "ordin-700-2014", "regulament-700-2014"}


def test_a_word_in_the_id_beats_a_word_in_another_act_note(corpus):
    """'regulament' appears in the regulation's id AND could appear in another
    act's note; the id is the specific one and must win."""
    c = LawLibrary(corpus.root, tracked={
        **TRACKED,
        "legea-7-1996": {**TRACKED["legea-7-1996"],
                         "note": "lásd a regulament-700-2014 alatt"}})
    assert c.resolve_act("regulament").act_id == "regulament-700-2014"


def test_unknown_act_resolves_to_nothing(corpus):
    assert corpus.resolve_act("nonexistent") is None
    assert corpus.resolve_act("") is None


# --- exact citation ---------------------------------------------------------

@pytest.mark.parametrize("ref", ["24", "art. 24", "Art 24", "Articolul 24"])
def test_article_accepts_the_ways_a_citation_is_written(corpus, ref):
    row = corpus.article("legea-7-1996", ref)
    assert row["article"] == "Articolul 24"
    assert "suport analogic" in row["text"]


def test_article_carries_its_chapter_and_source(corpus):
    row = corpus.article("regulament", "89")
    assert row["chapter"] == "Capitolul IV — Procedura de înscriere"
    assert row["source"].endswith("/222204")
    assert row["text"].startswith("Documentația")
    assert "*Capitolul" not in row["text"], "the chapter marker is lifted out"


def test_article_does_not_fuzzy_match_a_number(corpus):
    """A citation must be exact: art. 2 is not art. 24."""
    assert corpus.article("legea-7-1996", "2") is None


def test_missing_article_returns_none(corpus):
    assert corpus.article("legea-7-1996", "999") is None


def test_norm_article():
    assert norm_article("Articolul 24") == "24"
    assert norm_article("art. 24^1") == "24^1"
    assert norm_article("nonsense") == ""


# --- the security properties ------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../../acts.yaml", "/etc/passwd", "~/.ssh/id_rsa",
    "legea-7-1996/../../acts.yaml", "../acts/legea-7-1996",
])
def test_no_path_argument_can_escape(corpus, evil):
    """There is no path guard here because no path is ever built: an act
    reference is looked up in a dictionary of known acts. A traversal string
    simply does not name an act."""
    assert corpus.resolve_act(evil) is None
    assert corpus.article(evil, "1") is None


def test_corpus_never_writes(corpus, tmp_path):
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    corpus.search("cadastru")
    corpus.article("legea-7-1996", "24")
    corpus.headings("legea-7-1996")
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert before == after


# --- retrieval --------------------------------------------------------------

def test_search_finds_and_labels_the_act(corpus):
    rows = corpus.search("dezmembraminte borderou", limit=3)
    assert rows
    assert rows[0]["act_id"] == "regulament-700-2014"
    assert rows[0]["article"] == "Articolul 89"
    assert rows[0]["act_title"].startswith("REGULAMENTUL")


def test_search_can_be_scoped_to_one_act(corpus):
    rows = corpus.search("dezmembraminte", limit=5, act="regulament")
    assert rows
    assert {r["act_id"] for r in rows} == {"regulament-700-2014"}


def test_headings_lists_articles_in_document_order(corpus):
    heads = [h["heading"] for h in corpus.headings("regulament-700-2014")]
    assert heads == ["Articolul 89", "Anexa nr. 1.31"]


def test_stats(corpus):
    s = corpus.stats()
    assert s["acts"] == 3
    assert s["articles"] == 4       # from the metadata blocks: 2 + 1 + 1


# --- the fallback -----------------------------------------------------------

def test_fallback_rejects_noise(corpus):
    """Without a floor, nonsense returned five confidently-formatted articles.
    For a law tool "nothing matches that" is a correct answer and inventing
    plausible hits is the worst failure available to this server."""
    assert corpus.search("zzzz qqqq wwww", limit=5) == []


def test_fallback_only_tops_up_a_thin_keyword_result(corpus):
    """The keyword index is the tuned ranking; the fallback must not displace
    it when it already answered."""
    rows = corpus.search("dezmembraminte borderou", limit=1)
    assert rows[0].get("matched_by") != "similarity"


def test_fallback_is_not_consulted_when_disabled(corpus):
    assert corpus.search("zzzz qqqq", limit=3, use_fallback=False) == []


def test_ngram_scores_are_bounded():
    chunks = [{"heading": "Articolul 1", "text": "cadastru si publicitate imobiliara"}]
    fb = NgramFallback(chunks)
    hits = fb.score_all("cadastru si publicitate imobiliara")
    assert hits and 0.0 < hits[0][1] <= 1.0001
    assert hits[0][1] >= MIN_SCORE


def test_a_custom_fallback_can_be_injected(tmp_path):
    """The one-method interface is the hook for real embeddings later."""
    (tmp_path / "a.md").write_text(ACT_A, encoding="utf-8")
    calls = []

    class Stub:
        def __init__(self, chunks):
            self.chunks = chunks

        def score_all(self, query, limit=5):
            calls.append(query)
            # index 1: chunk 0 of a file is the text before the first "##",
            # which has no heading and is deliberately never citable
            return [(1, 0.9)]

    c = LawLibrary(str(tmp_path), tracked=TRACKED, fallback=Stub)
    rows = c.search("something the keyword index will not find", limit=3)
    assert calls, "the injected fallback was never consulted"
    assert rows and rows[0]["matched_by"] == "similarity"


# --- long sections are skimmed, not truncated --------------------------------

GRID = "\n".join(
    ["| Serviciu | Tarif (lei) | UM"]
    + [f"| Filler service {i} | {i} | buc" for i in range(120)]
    + ["| Recepție dezlipire/alipire | 60 | imobil",
       "| Intabulare persoane fizice | 0,15% | valoare"])


def test_focus_keeps_the_header_and_the_matching_rows():
    """A price without its column headings ("60" — lei? per what?) is not
    quotable, so the header row is always kept."""
    out = lawlib.focus(GRID, "tarif dezlipire alipire")
    assert out.split("\n")[0].startswith("| Serviciu")
    assert "Recepție dezlipire/alipire | 60 | imobil" in out
    assert "Filler service 7 " not in out
    assert len(out) < len(GRID) / 3


def test_focus_marks_what_it_skipped():
    out = lawlib.focus(GRID, "intabulare persoane fizice")
    assert "…" in out
    assert "0,15%" in out


def test_focus_leaves_a_short_section_alone():
    short = "Articolul 1 spune ceva scurt."
    assert lawlib.focus(short, "ceva") == short


def test_focus_returns_everything_when_nothing_matches():
    """Better to hand back the section than to hand back nothing."""
    assert lawlib.focus(GRID, "zzzz qqqq") == GRID
