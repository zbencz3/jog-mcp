"""The law-corpus fetcher and parser.

Every fixture here is a cut-down copy of real Portal Legislativ markup
(2026-09-19). The parser's job is to survive that markup exactly as served,
so the fixtures keep its oddities — the "+" collapse span inside every
article title, the `S_*_SHORT` ellipsis twin of every body, the `<li>` nested
inside the emitter, and the signature paragraphs that the portal puts INSIDE
the last article.
"""
from __future__ import annotations

import json

import pytest

from jog_mcp import search
from jog_mcp.fetch import acts

ART = '''
<span class="S_ART" id="id_artA{n}">
  <span class="TAG_COLLAPSED" id="c{n}"> + </span>
  <span class="S_ART_TTL">Articolul {n}</span><span class="S_ART_DEN">{den}</span>
  <span class="S_ART_BDY">
    <span class="S_ALN"><span class="S_ALN_TTL">(1)</span>
      <span class="S_ALN_BDY">{body}</span></span>
    <span class="S_LIT"><span class="S_LIT_TTL">a)</span>
      <span class="S_LIT_SHORT">&nbsp;...&nbsp;</span>
      <span class="S_LIT_BDY">prima literă;</span></span>
    <span class="S_LIT"><span class="S_LIT_TTL">b)</span>
      <span class="S_LIT_SHORT">&nbsp;...&nbsp;</span>
      <span class="S_LIT_BDY">a doua literă.</span></span>
  </span>
</span>'''


def page(articles: str, chapter: str = "", extra: str = "") -> str:
    inner = articles
    if chapter:
        inner = (f'<span class="S_CAP"><span class="S_CAP_TTL">Capitolul I</span>'
                 f'<span class="S_CAP_DEN">{chapter}</span>'
                 f'<span class="S_CAP_BDY">{articles}</span></span>')
    return f'''<html><head><title>Portal Legislativ</title></head><body>
<div id="meniu">Acasă Despre Proiect GDPR Meniu</div>
<script>var x = "Articolul 999 should never be parsed";</script>
<div id="infoactinfoact">
  <span class="S_DEN">LEGE nr. 1 din 1 ianuarie 2020</span>
  <table class="S_EMT"><tr><td><span class="S_EMT_TTL">EMITENT</span></td>
  <td><span class="S_EMT_BDY"><li>PARLAMENTUL</li></span></td></tr></table>
  <span class="S_PUB"><span class="S_PUB_TTL">Publicat în</span>
    <span class="S_PUB_BDY">MONITORUL OFICIAL nr. 5 din 5 ianuarie 2020</span></span>
  <span class="S_PAR">Având în vedere prevederile legii;</span>
  {inner}{extra}
</div></body></html>'''


@pytest.fixture
def parsed():
    return acts.parse(page(ART.format(n=1, den="", body="Text de bază.")
                           + ART.format(n=2, den="Definiții", body="Al doilea.")))


# --- metadata ---------------------------------------------------------------

def test_document_fields(parsed):
    assert parsed["title"] == "LEGE nr. 1 din 1 ianuarie 2020"
    assert parsed["publicat"] == "MONITORUL OFICIAL nr. 5 din 5 ianuarie 2020"


def test_title_is_not_the_page_title(parsed):
    """S_DEN must reset its buffer, or it hands back the <title> tag and the
    whole navigation menu that precede it in the document."""
    assert "Portal Legislativ" not in parsed["title"]
    assert "Meniu" not in parsed["title"]


def test_emitent_survives_nested_markup(parsed):
    """S_EMT_BDY wraps a bare <li>. Flushing the buffer on every end tag —
    rather than only on the ones we capture — silently dropped this."""
    assert parsed["emitent"] == "PARLAMENTUL"


def test_preamble_is_captured(parsed):
    assert parsed["preamble"] == ["Având în vedere prevederile legii;"]


# --- articles ---------------------------------------------------------------

def test_articles_and_numbering(parsed):
    assert [a["ttl"] for a in parsed["articles"]] == ["Articolul 1", "Articolul 2"]
    assert parsed["articles"][1]["den"] == "Definiții"


def test_script_content_is_not_parsed(parsed):
    assert all("999" not in a["ttl"] for a in parsed["articles"])


def test_collapse_toggle_stays_out_of_the_title(parsed):
    assert parsed["articles"][0]["ttl"] == "Articolul 1"


def test_alineat_and_litera_become_lines(parsed):
    body = parsed["articles"][0]["body"]
    assert "(1) Text de bază." in body
    assert "\na) prima literă;" in body
    assert "\nb) a doua literă." in body


def test_short_ellipsis_twin_is_dropped(parsed):
    """Every S_*_BDY has an S_*_SHORT sibling holding the literal "...".
    Including it left a stray ellipsis after every litera."""
    assert "..." not in parsed["articles"][0]["body"]


def test_chapter_becomes_a_breadcrumb():
    p = acts.parse(page(ART.format(n=1, den="", body="x."),
                        chapter="DISPOZIȚII GENERALE"))
    assert p["articles"][0]["group"] == "Capitolul I — DISPOZIȚII GENERALE"


def test_signature_paragraphs_are_dropped():
    sig = ('<span class="S_SMN"><span class="S_SMN_PAR">PREȘEDINTELE</span>'
           '<span class="S_SMN_PAR">ION POPESCU</span></span>')
    p = acts.parse(page(ART.format(n=1, den="", body="x.") + sig))
    assert "POPESCU" not in p["articles"][0]["body"]


def test_consolidation_note_attaches_to_the_article_it_follows():
    """A loose S_PAR between articles is the portal's amendment annotation.
    Floating them into one preamble bucket strands "this changed on X" far
    from the rule it changed."""
    note = '<span class="S_PAR">(la 01-02-2021, Articolul 1 a fost modificat)</span>'
    p = acts.parse(page(ART.format(n=1, den="", body="x.") + note
                        + ART.format(n=2, den="", body="y.")))
    assert p["articles"][0]["notes"] == [
        "(la 01-02-2021, Articolul 1 a fost modificat)"]
    assert p["articles"][1]["notes"] == []


def test_annexes_are_separate_sections():
    anx = ('<span class="S_ANX"><span class="S_ANX_TTL">Anexa nr. 1</span>'
           '<span class="S_ANX_DEN"></span>'
           '<span class="S_ANX_BDY">Formular de cerere.</span></span>')
    p = acts.parse(page(ART.format(n=1, den="", body="x.") + anx))
    assert len(p["articles"]) == 1
    assert [a["ttl"] for a in p["annexes"]] == ["Anexa nr. 1"]


# --- rendering --------------------------------------------------------------

def test_render_headings_are_what_search_splits_on(parsed):
    """The whole design rests on this: an article is a `##` section, so
    search.Index chunks the corpus one article at a time with no new code."""
    md = acts.render("legea-1-2020", "123", parsed, fetched="T", text_hash="h")
    headings = [h for h, _ in search.split_sections(md)]
    assert "Articolul 1" in headings
    assert "Articolul 2 — Definiții" in headings


def test_render_repeats_the_chapter_inside_the_section():
    """Retrieval returns ONE section; without the chapter inline the reader
    cannot tell which chapter an article belongs to, and that changes what it
    means."""
    p = acts.parse(page(ART.format(n=1, den="", body="x."), chapter="GENERALE"))
    md = acts.render("a", "1", p)
    body = dict(search.split_sections(md))["Articolul 1"]
    assert "Capitolul I — GENERALE" in body


def test_render_metadata_round_trips(tmp_path, parsed):
    md = acts.render("legea-1-2020", "999", parsed,
                     fetched="2026-01-01T00:00:00Z", text_hash="abc123")
    f = tmp_path / "legea-1-2020.md"
    f.write_text(md, encoding="utf-8")
    meta = acts.read_meta(str(f))
    assert meta["act_id"] == "legea-1-2020"
    assert meta["doc_id"] == "999"
    assert meta["text_hash"] == "abc123"
    assert meta["source"].endswith("/999")


# --- hashing ----------------------------------------------------------------

def test_hash_is_stable_and_text_sensitive(parsed):
    other = acts.parse(page(ART.format(n=1, den="", body="Text schimbat.")
                            + ART.format(n=2, den="Definiții", body="Al doilea.")))
    assert acts.act_hash(parsed) == acts.act_hash(parsed)
    assert acts.act_hash(parsed) != acts.act_hash(other)


def test_hash_covers_amendment_notes():
    """A repeal is often ONLY a note — if the hash ignored notes, the refresh
    would call an amended act unchanged and never rewrite it."""
    base = acts.parse(page(ART.format(n=1, den="", body="x.")))
    noted = acts.parse(page(
        ART.format(n=1, den="", body="x.")
        + '<span class="S_PAR">(la 01-02-2021, abrogat)</span>'))
    assert acts.act_hash(base) != acts.act_hash(noted)


# --- refresh ----------------------------------------------------------------

@pytest.fixture
def fake_portal(monkeypatch):
    calls = []

    def fake_fetch(url, timeout=30):
        calls.append(url)
        return page(ART.format(n=1, den="", body="Text de bază."))

    monkeypatch.setattr(acts, "fetch", fake_fetch)
    return calls


def _cfg(tmp_path, **over):
    entry = {"id": "legea-1-2020", "doc_id": "123"}
    entry.update(over)
    return {"root": str(tmp_path), "track": [entry]}


def test_refresh_writes_then_reports_unchanged(tmp_path, fake_portal):
    cfg = _cfg(tmp_path)
    first = acts.refresh(cfg, log=lambda *a: None)
    assert first[0]["status"] == "fetched"
    assert (tmp_path / "legea-1-2020.md").exists()

    second = acts.refresh(cfg, log=lambda *a: None)
    assert second[0]["status"] == "unchanged", \
        "an unchanged act must not be rewritten — git log acts/ is the " \
        "amendment history and a no-op commit ruins it"


def test_refresh_dry_run_writes_nothing(tmp_path, fake_portal):
    acts.refresh(_cfg(tmp_path), dry_run=True, log=lambda *a: None)
    assert not (tmp_path / "legea-1-2020.md").exists()


def test_refresh_writes_a_manifest(tmp_path, fake_portal):
    acts.refresh(_cfg(tmp_path), log=lambda *a: None)
    data = json.loads((tmp_path / "_manifest.json").read_text(encoding="utf-8"))
    assert data["acts"][0]["act_id"] == "legea-1-2020"


def test_refresh_survives_one_failing_act(tmp_path, monkeypatch):
    def flaky(url, timeout=30):
        if url.endswith("/bad"):
            raise acts.FetchError("boom")
        return page(ART.format(n=1, den="", body="ok."))

    monkeypatch.setattr(acts, "fetch", flaky)
    cfg = {"root": str(tmp_path), "track": [
        {"id": "bad-one", "doc_id": "bad"},
        {"id": "good-one", "doc_id": "123"}]}
    res = {r["id"]: r["status"] for r in acts.refresh(cfg, log=lambda *a: None)}
    assert res == {"bad-one": "failed", "good-one": "fetched"}


def test_empty_page_is_an_error_not_an_empty_act(monkeypatch):
    """A portal outage that serves the chrome with no act body must fail
    loudly; writing an empty file would quietly delete the law."""
    monkeypatch.setattr(acts, "fetch", lambda url, timeout=30: "<html></html>")
    with pytest.raises(acts.FetchError):
        acts.fetch_act("123")


# --- the two configuration traps --------------------------------------------

def test_check_flags_an_act_that_is_only_an_approving_ordin(tmp_path, monkeypatch):
    """Ordin 700/2014 is three articles saying "see the annex"; the 394-article
    Regulament it approves is a different document. Tracking the wrong one is
    silent — the corpus builds fine and every answer is missing."""
    monkeypatch.setattr(acts, "fetch",
                        lambda url, timeout=30: page(ART.format(n=1, den="", body="x.")))
    lines = []
    problems = acts.check(_cfg(tmp_path), log=lines.append)
    assert problems == 1
    assert any("approving ordin" in ln for ln in lines)


def test_check_accepts_a_legitimately_short_act(tmp_path, monkeypatch):
    monkeypatch.setattr(acts, "fetch",
                        lambda url, timeout=30: page(ART.format(n=1, den="", body="x.")))
    assert acts.check(_cfg(tmp_path, min_articles=1), log=lambda *a: None) == 0


def test_check_deep_flags_a_richer_alternate_id(tmp_path, monkeypatch):
    """Each consolidation gets a NEW document id and the portal publishes no
    link from the old one to the new, so a pinned doc_id silently freezes the
    corpus at one consolidation."""
    def by_id(url, timeout=30):
        n = 1 if url.endswith("/123") else 6
        return page("".join(ART.format(n=i, den="", body="x.")
                            for i in range(1, n + 1)))

    monkeypatch.setattr(acts, "fetch", by_id)
    lines = []
    problems = acts.check(_cfg(tmp_path, min_articles=1, alt_ids=["999"]),
                          deep=True, log=lines.append)
    assert problems == 1
    assert any("more consolidated" in ln for ln in lines)


# --- tables ------------------------------------------------------------------

TARIF_ANNEX = '''
<span class="S_ANX"><span class="S_ANX_TTL">ANEXĂ</span>
 <span class="S_ANX_DEN"></span>
 <span class="S_ANX_BDY"><table>
  <tr><td><span class="S_PAR">Serviciu</span></td>
      <td><span class="S_PAR">Tarif </span><span class="S_PAR">(lei)</span></td>
      <td><span class="S_PAR">UM</span></td></tr>
  <tr><td><span class="S_PAR">Recepție dezlipire/alipire</span></td>
      <td><span class="S_PAR">60</span></td>
      <td><span class="S_PAR">imobil</span></td></tr>
  <tr><td><span class="S_PAR">Înființare carte funciară</span></td>
      <td><span class="S_PAR">120</span></td>
      <td><span class="S_PAR">imobil</span></td></tr>
 </table></span></span>'''


def test_a_table_keeps_its_rows_and_cells():
    """The ANCPI tariff grid is a real HTML table. Stripping the tags without
    marking the boundaries produced "ServiciuTarif (lei)UM" — a price list
    nobody can read or quote."""
    p = acts.parse(page(ART.format(n=1, den="", body="x."), extra=TARIF_ANNEX))
    body = p["annexes"][0]["body"]
    rows = [ln for ln in body.split("\n") if ln.strip()]
    assert any("Recepție dezlipire/alipire | 60 | imobil" in r for r in rows), body
    assert any("Înființare carte funciară | 120 | imobil" in r for r in rows), body


def test_two_paragraphs_in_one_cell_stay_on_the_row():
    """"Tarif" and "(lei)" are separate S_PAR spans inside one <td>; a newline
    between them would break the header row apart from its table."""
    p = acts.parse(page(ART.format(n=1, den="", body="x."), extra=TARIF_ANNEX))
    body = p["annexes"][0]["body"]
    assert "Tarif (lei)" in body


def test_a_table_does_not_desync_the_tag_stack():
    """<td>/<tr> do not push onto the stack, so they must not pop either.
    When they did, the stack unwound early and the parser swallowed every
    article after the first table."""
    p = acts.parse(page(ART.format(n=1, den="", body="elso.")
                        + ART.format(n=2, den="", body="masodik."),
                        extra=TARIF_ANNEX))
    assert [a["ttl"] for a in p["articles"]] == ["Articolul 1", "Articolul 2"]
    assert "masodik." in p["articles"][1]["body"]


def test_the_download_uses_the_fast_page_and_the_link_stays_human(fake_portal, tmp_path):
    """DetaliiDocumentAfis took 119 s for Regulament 600/2023 against the
    portal's 120 s gateway timeout, and 504'd every morning from 2026-09-24.
    DetaliiDocument serves the identical text in seconds. The `source:` link
    people click stays on the display page."""
    acts.refresh(_cfg(tmp_path), log=lambda *a: None)
    assert fake_portal and all("/Public/DetaliiDocument/123" in u for u in fake_portal)
    md = open(tmp_path / "legea-1-2020.md", encoding="utf-8").read()
    assert "source: https://legislatie.just.ro/Public/DetaliiDocumentAfis/123" in md
