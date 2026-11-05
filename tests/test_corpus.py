"""Does the corpus actually answer a surveyor's questions?

Every other test here checks a mechanism. This one checks the OUTCOME: type a
real question, land in the right act. It runs against the real `acts/`
corpus, so it is a smoke test of the whole chain — glossary expansion, the
keyword index, length normalisation, the fallback, the repeal flags.

Why it exists: an earlier version answered 12 of 12 questions and three of the
answers were wrong — "what do I need to register a servitude" returned the
ANCPI fee table, and "what documents for a first registration" a
22k-character annex of preemption forms. Nothing was broken; long sections
simply outscored short ones, because the index had no length normalisation at
all. A passing test suite said nothing about it.

The expectation is the ACT, not the article: which article best answers "how
long is a certificat de urbanism valid" is a judgement, but it is certainly in
the urbanism code and certainly not in the fee schedule.
"""
from __future__ import annotations

import os

import pytest

from jog_mcp import lawlib, retrieve, search
from jog_mcp.server import load_corpus

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, "acts")
SAMPLE_GLOSSARY = os.path.join(REPO, "glossary.sample.md")

# (question, one of these act ids). Romanian questions need no glossary.
ROMANIAN = [
    ("Ce documente trebuie la prima înscriere?", "regulament-600-2023"),
    ("Care este termenul de valabilitate al certificatului de urbanism?",
     "codul-urbanism-169-2026"),
    ("Ce este uzufructul?", "codul-civil-bunuri"),
    ("Cine are drept de preempțiune la teren extravilan?",
     "legea-17-2014|norme-17-2014"),
    ("tarif dezlipire alipire", "ordin-16-2019"),
    # Diacritics are optional: this is how people type on a phone.
    ("Ce acte trebuie pentru dezmembrare", "regulament-600-2023|codul-urbanism-169-2026"),
    # A Hungarian question carrying a Romanian term of art.
    ("Mit kell egy dezmembrare dossziéba tenni?",
     "regulament-600-2023|codul-urbanism-169-2026"),
    ("Mi az a superficie?", "codul-civil-bunuri"),
]

# Purely Hungarian questions reach the Romanian corpus only through the
# glossary. These pass with the five-row sample; a real glossary covers more.
HUNGARIAN_WITH_SAMPLE = [
    ("Mi kell a szolgalmi jog bejegyzéséhez?",
     "codul-civil-bunuri|regulament-600-2023"),
    ("Ki jogosult telekkönyvi kivonatot kérni?",
     "regulament-600-2023|legea-7-1996"),
]


@pytest.fixture(scope="module")
def law():
    if not os.path.isdir(CORPUS) or not os.listdir(CORPUS):
        pytest.skip("no corpus on disk — run jog-mcp-fetch")
    return load_corpus(glossary_path=SAMPLE_GLOSSARY)


def _lands(law, question, expected):
    hits = law.search(question, limit=1)
    got = hits[0]["act_id"] if hits else "(nothing)"
    return any(e == got for e in expected.split("|")), got


@pytest.mark.parametrize("question,expected", ROMANIAN + HUNGARIAN_WITH_SAMPLE)
def test_a_real_question_lands_in_the_right_act(law, question, expected):
    ok, got = _lands(law, question, expected)
    assert ok, f"{question!r} -> {got}, wanted {expected}"


def test_without_a_glossary_hungarian_reaches_less_but_romanian_still_works():
    """The glossary is optional. Without it the Romanian questions must still
    land; a purely Hungarian one is allowed to find nothing, and finding
    nothing is a correct answer, not a crash."""
    bare = load_corpus()
    for question, expected in ROMANIAN:
        ok, got = _lands(bare, question, expected)
        assert ok, f"{question!r} -> {got} without a glossary"


def test_a_long_section_does_not_win_on_length_alone(law):
    """The concrete regression: the fee annex is ~10k characters with ~470
    distinct terms, so before length normalisation it answered questions that
    had nothing to do with money."""
    hits = law.search("Mi kell a szolgalmi jog bejegyzéséhez?", limit=3)
    assert hits
    assert hits[0]["act_id"] != "ordin-16-2019", \
        "the fee schedule is not where servitudes are defined"


def test_length_normalisation_is_actually_on():
    idx = search.Index(CORPUS)
    assert search.LEN_B > 0
    assert idx.avg_len > 1, "average section length must be computed"


def test_the_alias_rerank_can_actually_fire(law):
    """It re-ranks a POOL. With limit=1 the index returns one candidate and a
    re-rank has nothing to promote — which is why the boost looked inert at
    every setting until the pool was widened."""
    assert lawlib.RERANK_POOL > 1
    hits = law.search("tarif dezlipire alipire", limit=1)
    assert hits and hits[0]["act_id"] == "ordin-16-2019"


def test_nonsense_finds_nothing(law):
    """For a law tool "no article matches that" is a correct and necessary
    answer; plausible-looking hits for nonsense are the worst output there
    is."""
    assert law.search("zzzz qqqq nonexistent", limit=5) == []


# --- repealed acts must announce themselves --------------------------------

def test_a_repealed_act_carries_a_warning(law):
    """Legea 50/1991 and 350/2001 were repealed on 2026-08-25 by the Codul
    urbanismului. They stay in the corpus because transitional provisions mean
    a file opened before that date still runs under them — but quoting one as
    current law is the worst error available here, so every result says so."""
    expected = {
        "legea-50-1991": ("2026-08-25", "codul-urbanism-169-2026"),
        "legea-350-2001": ("2026-08-25", "codul-urbanism-169-2026"),
        "regulament-700-2014": ("2023-02-15", "regulament-600-2023"),
        "ordin-700-2014": ("2023-02-15", "regulament-600-2023"),
    }
    for act_id, (when, replacement) in expected.items():
        act = law.act(act_id)
        assert act is not None, act_id
        assert act.repealed_on == when, act_id
        assert "HATÁLYON KÍVÜL" in act.warning()
        assert act.superseded_by == replacement, act_id


def test_the_replacements_are_in_the_corpus(law):
    code = law.act("codul-urbanism-169-2026")
    assert code is not None and code.articles > 500
    reg = law.act("regulament-600-2023")
    assert reg is not None and reg.articles > 200
    assert not reg.repealed_on, "the replacement must not itself be flagged"


def test_a_repealed_row_carries_the_warning_for_any_consumer(law):
    """The warning rides on the ROW, not on one caller's formatting, so
    anything reading these rows gets it for free."""
    row = law.article("regulament-700-2014", "89")
    assert row and "HATÁLYON KÍVÜL" in row["repealed"]
    hits = law.search("dezmembraminte", limit=6, act="regulament-700-2014")
    assert hits and all(h["repealed"] for h in hits)


def test_a_live_act_carries_no_warning(law):
    row = law.article("regulament-600-2023", "80")
    assert row and not row["repealed"]


def test_an_inferred_term_counts_for_less_than_a_typed_one():
    assert 0 < search.WEAK_TERM_WEIGHT < 1
    idx = search.Index(CORPUS)
    chunk = next(c for c in idx.chunks if c.get("_tokens"))
    term = chunk["_tokens"][0]
    assert idx.score([term], chunk, weak={term}) < idx.score([term], chunk)


# --- a partial repeal is per ARTICLE, not per act ----------------------------

def test_only_the_repealed_articles_are_flagged(law):
    """Legea 10/1995 lost art. 1-9, 11-40 and 42-44 to the Codul urbanismului
    while art. 10 and 41 stand. Flagging the whole act would abandon articles
    that still bind; flagging none would quote dead ones."""
    act = law.act("legea-10-1995")
    assert act is not None and act.repealed_articles
    for dead in ("Articolul 5", "Articolul 12", "Articolul 44"):
        assert act.dead_article(dead), dead
        assert "HATÁLYON KÍVÜL" in act.article_warning(dead)
    for alive in ("Articolul 10", "Articolul 41"):
        assert not act.dead_article(alive), alive
        assert act.article_warning(alive) == ""


def test_our_flags_agree_with_the_portal_text(law):
    """The consolidated text stamps each repealed article 'Abrogat.'. The
    ranges in acts.yaml must not drift from it."""
    for n in ("5", "12"):
        row = law.article("legea-10-1995", n)
        assert row and row["repealed"], f"art. {n} should be flagged"
        assert row["text"].lower().startswith("abrogat"), \
            f"the portal no longer marks art. {n} as repealed — recheck the range"
    for n in ("10", "41"):
        row = law.article("legea-10-1995", n)
        assert row and not row["repealed"], f"art. {n} is still in force"
        assert not row["text"].lower().startswith("abrogat")


# --- find_exact: what ranked search cannot see ------------------------------

def test_find_exact_finds_the_percentage_ranked_search_missed(law):
    rows = retrieve.find_exact(law, "15%", act="600/2023")
    assert "Articolul 98" in [r["article"] for r in rows]
    art98 = next(r for r in rows if r["article"] == "Articolul 98")
    assert "15%" in art98["text"], "the window must be centred on the match"


def test_find_exact_ignores_diacritics_and_spacing(law):
    found = {(r["act_id"], r["article"])
             for r in retrieve.find_exact(law, "servituti   de trecere")}
    assert ("codul-urbanism-169-2026", "Articolul 256") in found
