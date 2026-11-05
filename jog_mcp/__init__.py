"""jog-mcp — an MCP server over the full text of Romanian surveying and
cadastre law.

`server.py` is the MCP layer. Everything under it is plain stdlib Python:
`search.py` (keyword index), `lawlib.py` (acts, articles, citation lookup),
`retrieve.py` (literal phrase search), `ngram.py` (similarity fallback),
`glossary.py` (Hungarian → Romanian query bridge). `fetch/` builds the corpus
from legislatie.just.ro and is a separate command, run by a person.
"""
__version__ = "0.1.0"
