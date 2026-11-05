"""The HU-RO term bridge.

The corpus is Romanian and stays Romanian — you quote Romanian at the counter.
What this covers is the query side, and the reason it exists is a measurement:
everyday Hungarian terms each returned ZERO hits while
their Romanian equivalents all found the right article. "telekkönyv" and
"carte funciară" share no letters, so neither the keyword index nor the n-gram
fallback could bridge them.
"""
from __future__ import annotations

import pytest

from jog_mcp import search
from jog_mcp.glossary import Glossary

TABLE = """# Szakszótár

| magyar | română |
|---|---|
| telekkönyv, telekkönyvi lap | carte funciară |
| telekkönyvi kivonat | extras de carte funciară |
| megosztás, felosztás | dezmembrare, dezlipire |
| osztatlan közös tulajdon | proprietate comună, indiviziune |
"""


@pytest.fixture
def gloss(tmp_path):
    (tmp_path / "_glossary.md").write_text(TABLE, encoding="utf-8")
    return Glossary(str(tmp_path / "_glossary.md"))


def test_the_table_is_parsed_not_the_header_or_the_rule(gloss):
    assert len(gloss.pairs) == 4
    assert all(hu and ro for hu, ro in gloss.pairs)
    assert all("---" not in "".join(hu) for hu, _ in gloss.pairs)


def test_expand_adds_the_romanian_side(gloss):
    out = gloss.expand("telekkönyv")
    assert "telekkönyv" in out and "carte funciară" in out


def test_expand_keeps_the_hungarian(gloss):
    """People write Hungarian and Romanian in one sentence; one query has to
    reach both, so the original words are never thrown away."""
    assert "megosztás" in gloss.expand("megosztás")


def test_the_longest_phrase_wins(gloss):
    """"telekkönyvi kivonat" is the extras, not carte funciară + kivonat."""
    out = gloss.expand("telekkönyvi kivonat kell")
    assert "extras de carte funciară" in out
    assert "carte funciară" in out          # contained in the longer phrase
    assert out.count("carte funciară") == 1, "the shorter term must not re-fire"


def test_a_multiword_concept_maps_as_one_idea(gloss):
    out = gloss.expand("osztatlan közös tulajdon esetén")
    assert "proprietate comună" in out and "indiviziune" in out


def test_all_variants_on_the_left_are_keys(gloss):
    assert "dezmembrare" in gloss.expand("felosztás")


def test_a_term_inside_a_sentence_is_found(gloss):
    out = gloss.expand("mit kell a telekkönyvi kivonat igényléséhez?")
    assert "extras de carte funciară" in out


def test_diacritics_are_optional(gloss):
    assert "dezmembrare" in gloss.expand("megosztas")


def test_a_query_with_no_hungarian_term_is_untouched(gloss):
    q = "dezmembrare documentatie cadastrala"
    assert gloss.expand(q) == q


def test_expansion_is_capped(gloss):
    """An unbounded expansion stops being a query and starts being the
    glossary, and every result then scores the same. The cap counts TERMS, not
    words — "extras de carte funciară" is one term and four words."""
    from jog_mcp.glossary import MAX_EXPANSIONS
    q = ("telekkönyv telekkönyvi kivonat megosztás "
         "osztatlan közös tulajdon felosztás")
    suffix = gloss.expand(q)[len(q):]
    all_ro = [ro for _, ros in gloss.pairs for ro in ros]
    added = [ro for ro in all_ro if ro in suffix]
    assert 0 < len(added) <= MAX_EXPANSIONS


def test_hits_reports_what_it_used(gloss):
    used = dict(gloss.hits("telekkönyv és megosztás"))
    assert "carte funciară" in used[search.fold("telekkönyv")]
    assert "dezmembrare" in used[search.fold("megosztás")]


def test_a_missing_file_is_not_an_error(tmp_path):
    g = Glossary(str(tmp_path / "nope.md"))
    assert g.pairs == []
    assert Glossary("").expand("telekkönyv") == "telekkönyv"
    assert g.expand("telekkönyv") == "telekkönyv"


def test_an_edit_is_picked_up_without_a_restart(gloss, tmp_path):
    """An edited glossary must not need a server restart."""
    assert "ipotecă" not in gloss.expand("jelzálog")
    (tmp_path / "_glossary.md").write_text(
        TABLE + "| jelzálog | ipotecă |\n", encoding="utf-8")
    assert "ipotecă" in gloss.expand("jelzálog")


def test_a_glossary_file_is_not_indexable_as_an_answer():
    """It starts with "_", so search.indexable skips it — a working aid must
    never be served as if it were law."""
    assert not search.indexable("_glossary.md")


def test_the_sample_glossary_parses():
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    g = Glossary(os.path.join(repo, "glossary.sample.md"))
    assert len(g.pairs) >= 3
    assert "carte funciară" in g.expand("telekkönyv")


# --- Hungarian is agglutinative; substring matching is not enough ------------

def test_an_inflected_form_still_matches(gloss, tmp_path):
    """"bejegyeztetni" does not CONTAIN "bejegyzés", so substring matching
    found neither înscriere nor intabulare, and the question "épületet
    akarunk bejegyeztetni telekkönyvbe" missed the article that answered it
    search.py stems for exactly this reason."""
    (tmp_path / "_glossary.md").write_text(
        TABLE + "| telekkönyvezés, bejegyzés | intabulare, înscriere |\n",
        encoding="utf-8")
    assert "înscriere" in gloss.expand("épületet akarunk bejegyeztetni")


def test_a_common_compound_prefix_does_not_drag_in_its_cousins(gloss, tmp_path):
    """"telek" opens telekkönyv, telekhatár AND telekmegosztás. At a 5-char
    stem "telekkönyvezéshez" also pulled dezmembrare and limită."""
    (tmp_path / "_glossary.md").write_text(
        TABLE + "| telekhatár | limită |\n| telekmegosztás | dezmembrare |\n",
        encoding="utf-8")
    out = gloss.expand("telekkönyvezéshez")
    assert "limită" not in out and "dezmembrare" not in out


def test_the_word_typed_exactly_wins_over_a_longer_stem_match(gloss, tmp_path):
    """The register and the act of registering are different words. Ordering
    candidates by key LENGTH resolved "telekkönyv" to intabulare, because
    "telekkönyvezés" is a longer string that matches it by stem."""
    (tmp_path / "_glossary.md").write_text(
        TABLE + "| telekkönyvezés | intabulare |\n", encoding="utf-8")
    out = gloss.expand("telekkönyv")
    assert "carte funciară" in out
    assert "intabulare" not in out


def test_a_short_word_must_be_a_real_prefix(gloss, tmp_path):
    (tmp_path / "_glossary.md").write_text(
        TABLE + "| jog | drept |\n", encoding="utf-8")
    assert "drept" in gloss.expand("milyen jogot jegyzünk be")
    assert "drept" not in gloss.expand("jegyzőkönyv")


# --- is the glossary actually Romanian? ---------------------------------------

def test_every_romanian_term_appears_in_the_corpus():
    """The glossary steers ranking, so "trust me" is not good enough. The
    statute is the authority on its own vocabulary: a Romanian term appearing
    nowhere in 2000-odd articles is a mistranslation or a colloquialism the
    law does not use. Checked here against the sample; run the same check on
    your own glossary."""
    import os
    from jog_mcp.glossary import validate
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(repo, "acts")
    if not os.path.isdir(corpus) or not os.listdir(corpus):
        pytest.skip("no corpus on disk")
    g = Glossary(os.path.join(repo, "glossary.sample.md"))
    bad = validate(g, search.Index(corpus))
    assert not bad, (
        "glossary terms the statute never uses:\n  "
        + "\n  ".join(f"{hu} -> {term} ({absent})" for hu, term, absent in bad))


def test_validate_flags_an_invented_term(tmp_path):
    from jog_mcp.glossary import validate
    (tmp_path / "_glossary.md").write_text(
        "| magyar | română |\n|---|---|\n| kitalált | zzzqqq |\n",
        encoding="utf-8")
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n\n## Articolul 1\n\ncadastru\n",
                                 encoding="utf-8")
    bad = validate(Glossary(str(tmp_path / "_glossary.md")), search.Index(str(corpus)))
    assert bad and bad[0][1] == "zzzqqq"
