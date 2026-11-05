"""The MCP layer: the tools as a client calls them.

The library tests check retrieval; these check what reaches the model — that
a repealed act is flagged in the TEXT the client sees, that a bad act
reference is answered rather than raised, and that every tool is declared
read-only.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from jog_mcp.server import build, load_corpus, main

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def mcp():
    return build(load_corpus(glossary_path=os.path.join(REPO, "glossary.sample.md")))


def call(mcp, name, **args) -> str:
    result = asyncio.run(mcp.call_tool(name, args))
    return "\n".join(c.text for c in result.content)


def test_the_tools_are_the_documented_five_and_all_read_only(mcp):
    tools = asyncio.run(mcp.list_tools())
    tools = getattr(tools, "tools", tools)
    names = sorted(t.name for t in tools)
    assert names == ["find_exact", "get_article", "list_acts", "outline", "search_law"]
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


def test_get_article_cites_act_source_and_version(mcp):
    out = call(mcp, "get_article", act="7/1996", article="art. 24")
    assert out.startswith("Articolul 24 — LEGEA cadastrului")
    assert "legislatie.just.ro/Public/DetaliiDocumentAfis/311746" in out
    assert "verzió: MONITORUL OFICIAL" in out


def test_a_repealed_article_is_flagged_in_the_text_the_model_reads(mcp):
    out = call(mcp, "get_article", act="regulament-700-2014", article="89")
    assert "!! ⚠️ HATÁLYON KÍVÜL 2023-02-15" in out
    assert "regulament-600-2023" in out


def test_an_ambiguous_act_is_a_question_back_not_a_guess(mcp):
    out = call(mcp, "get_article", act="regulament", article="1")
    assert "matches more than one act" in out


@pytest.mark.parametrize("evil", ["../../acts.yaml", "/etc/passwd", "~/.ssh/id_rsa"])
def test_a_path_is_not_an_act(mcp, evil):
    out = call(mcp, "get_article", act=evil, article="1")
    assert out.startswith("No act matches")
    assert "root:" not in out


def test_search_law_and_find_exact_return_citable_rows(mcp):
    out = call(mcp, "search_law", query="Ce este uzufructul?", limit=2)
    assert "Codul civil" in out or "CODUL CIVIL" in out.upper()
    out = call(mcp, "find_exact", phrase="15%", act="600/2023")
    assert "Articolul 98" in out


def test_list_acts_shows_dates_and_repeals(mcp):
    out = call(mcp, "list_acts")
    assert "fetched:" in out and "!! ⚠️" in out
    assert out.strip().endswith("indexed sections")


def test_http_mode_refuses_to_start_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("JOG_MCP_TOKEN", raising=False)
    assert main(["--http"]) == 1
    assert "JOG_MCP_TOKEN is not set" in capsys.readouterr().err


def test_http_mode_rejects_a_wrong_token():
    from starlette.testclient import TestClient

    from jog_mcp.server import _http_app
    app = _http_app(build(load_corpus()), "s3cret", "127.0.0.1")
    with TestClient(app) as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={},
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
