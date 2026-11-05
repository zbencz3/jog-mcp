# jog-mcp

An MCP server that gives Claude, or any MCP client, the full text of Romanian
surveying and cadastre law: the cadastre and land-registry law, the ANCPI
registration regulation, the new urbanism code, the land-fund law, ANCPI fees
and the relevant books of the Civil and Fiscal codes. It has 18 acts and about
2,400 articles, taken verbatim from [legislatie.just.ro](https://legislatie.just.ro).

*Jog* is Hungarian for "law". The server is built for people who ask questions
in Hungarian or Romanian and must quote Romanian law at a land-registry
counter, so its rules are strict:

- **It cites everything.** Every result names its act and article, links the
  official source, and gives the Monitorul Oficial consolidation and the date
  it was fetched.
- **It never presents repealed law as current.** A repealed act, or one
  repealed article of an act that is otherwise in force, is flagged on every
  result together with what replaced it. Example: Legea 10/1995 lost
  art. 1–9, 11–40 and 42–44 on 2026-08-25, while art. 10, 41 and 45 onward
  remain in force.
- **It says when the corpus doesn't cover a question.** The server
  instructions and prompts tell the model to say so rather than answer from
  memory. For nonsense queries, search returns nothing rather than
  plausible-looking hits.

## Tools, prompts, resource

| Tool | What it does |
|---|---|
| `search_law` | Ranked keyword search across every act, or within one act. Romanian or Hungarian; diacritics optional. |
| `get_article` | One article by number: `art. 24`, `Articolul 24`, `24^1`. Exact lookup, so a citation never depends on ranking. |
| `find_exact` | Every article that contains a phrase literally: `15%`, `30 de zile`, `servitute de trecere`. Catches the numbers and fixed wording that ranked search can't see. |
| `outline` | The article headings of one act, chapter by chapter, so a 584-article code can be navigated without loading it into context. |
| `list_acts` | What the corpus holds: each act, its consolidation, its fetch date and its repeal status. |

Every tool is annotated read-only and non-destructive.

**Prompts:** `jogi_kerdes` (a legal question, answered with a citation) and
`dosszie_lista` (the documents a cadastral procedure needs, each traced to its
article). Both are in Hungarian.

**Resource:** `acts://list`, the same act list as `list_acts`, as JSON.

## Example

> **Q:** Care este termenul de valabilitate al certificatului de urbanism?

`search_law` returns the Codul urbanismului article first:

```
Articolul 257 — CODUL AMENAJĂRII TERITORIULUI, URBANISMULUI ȘI CONSTRUCȚIILOR din 5 august 2026
…
(15) Emitentul certificatului de urbanism stabilește termenul de valabilitate
pentru un interval de timp cuprins între 12 și 36 de luni de la data emiterii, …

Forrás / sursa: https://legislatie.just.ro/Public/DetaliiDocumentAfis/313329
verzió: MONITORUL OFICIAL nr. 661 din 10 august 2026
korpuszba került: 2026-10-16
```

Results from the old law say so before anything else:

```
Articolul 7 — LEGE nr. 50 din 29 iulie 1991 (**republicată**)
!! ⚠️ HATÁLYON KÍVÜL 2026-08-25 óta — Legea 169/2026 (Codul urbanismului), art. 576 alin. (3); helyette: codul-urbanism-169-2026
```

The answer the model is instructed to give: *"A certificate of urbanism is
valid for 12 to 36 months from issue, as set by the issuer (Codul
urbanismului, Legea 169/2026, art. 257 alin. (15))."* Legea 50/1991, which
used to govern this, has been repealed since 2026-08-25.

## Install

Requires Python 3.10 or newer.

```bash
git clone https://github.com/zbencz3/jog-mcp && cd jog-mcp
uv venv && uv pip install -e .        # or: python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/jog-mcp --stats             # what the corpus holds
```

### Claude Code

```bash
claude mcp add jog -- "$PWD/.venv/bin/jog-mcp" --glossary "$PWD/glossary.sample.md"
```

### Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jog": {
      "command": "/path/to/jog-mcp/.venv/bin/jog-mcp",
      "args": ["--glossary", "/path/to/jog-mcp/glossary.sample.md"]
    }
  }
}
```

### Over HTTP

```bash
JOG_MCP_TOKEN=$(openssl rand -hex 32) .venv/bin/jog-mcp --http --host 127.0.0.1 --port 8787
```

HTTP mode won't start without a token, and it checks the token as a bearer
token on every request. Read [SECURITY.md](SECURITY.md) before exposing it
beyond localhost.

## Hungarian queries and the glossary

The corpus is Romanian and stays Romanian, because a translation can't be
cited. Hungarian questions reach it through a glossary, which **expands** the
query: "telekkönyv" is searched as "telekkönyv carte funciară". Each
translation counts for less than a word the user actually typed.

The glossary is a two-column markdown table, passed with `--glossary`. The
repository ships `glossary.sample.md`, five generic rows that show the format.
Supply your own for real use. Without a glossary, Romanian questions work
unchanged; purely Hungarian ones reach less. The test suite measures both
cases. A test also checks every Romanian term in the glossary against the
corpus, because the statute is the authority on its own vocabulary.

## How search works

It's pure Python with no embeddings, no vector database and no API key.
`search.py` is a BM25-style keyword index with diacritic folding and a
5-character prefix stem. The stem handles Hungarian agglutination and the
dropped diacritics of fast typing. The ranking also does the following:

- **Length normalisation.** Without it, a 25k-character fee annex outranked
  the short article that actually answered the question.
- **Long-section splitting.** Long sections are split at paragraph breaks
  when indexed, never in the stored text, so `get_article` still returns the
  whole article.
- **Focused excerpts.** A long table returns its header row plus the matching
  rows. A price without its column headings can't be quoted.
- **Friendly act names.** `acts.yaml` gives each act a friendly name that
  queries can match. The official title "ORDIN nr. 16 din 7 ianuarie 2019"
  contains none of the words people search for.
- **N-gram fallback.** A character-n-gram similarity search tops up thin
  results. Its score floor was measured on this corpus: useful matches score
  0.20–0.35 and noise scores 0.06–0.12.

## Refreshing the corpus

```bash
uv pip install -e '.[fetch]'
.venv/bin/jog-mcp-fetch --check --deep   # is each doc_id the right document, and is there a newer consolidation?
.venv/bin/jog-mcp-fetch                  # fetch, rewrite only the acts whose text changed
git diff --stat acts/                    # every amendment is a reviewable diff
```

The fetcher handles two quirks of the portal, both of which fail silently:

- **An order is not the regulation it approves.** Ordin 700/2014 is three
  articles; the regulation itself is a separate document. `--check` flags an
  act that parses to suspiciously few articles.
- **Every consolidation gets a new document ID.** The portal has no link from
  the old ID to the new one. `--check --deep` compares the configured
  `alt_ids`.

A running server picks up re-fetched files on the next query, with no restart.

## Development

```bash
uv pip install -e '.[dev]'
.venv/bin/pytest
```

The tests cover the parser against real portal markup, exact citation,
act-reference resolution, every repeal case (whole act, partial, agreement
with the portal's own "Abrogat." stamps) and the security properties. They
also run a question benchmark against the real corpus that checks each
question lands in the right act.

## Licence

The code is MIT; see [LICENSE](LICENSE). The texts in `acts/` are Romanian
legislation as published on the Portal Legislativ of the Ministry of Justice,
included for convenience. Check the official source before relying on any of
them.
