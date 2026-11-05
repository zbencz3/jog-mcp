"""The law corpus: which acts we keep the text of, and keeping them current.

This keeps the actual TEXT of each act, so "what does Art. 24 of the
Regulament require in the file" is answered by quoting the statute rather than
by a model's recollection of it.

How the portal's HTML becomes markdown is `portal.py`; this module only
decides which documents to fetch, when to rewrite a file, and how to tell a
misconfigured entry from a good one. The corpus is committed to the repo, so
every refresh is a reviewable diff.

Two traps, both of which `check()` exists to surface, because both fail
silently: the corpus builds fine and the answers are quietly wrong.

* AN ORDIN IS NOT THE REGULATION IT APPROVES. Ordin 700/2014 (doc 186631) is
  three articles saying "the Regulament is approved, see the annex"; the
  Regulament itself, 394 articles, is doc 222204. Track the document that
  holds the text, not the one announcing it. An entry parsing to a handful of
  articles is the symptom.

* EACH CONSOLIDATION GETS A NEW DOCUMENT ID. Legea 50/1991 is both 184771 (57
  articles) and 307791 (64 articles, 35 amendment notes), both "consolidated",
  the second newer — and the portal publishes no link from the old id to the
  new. A pinned doc_id therefore freezes the corpus at one consolidation
  forever. `check(deep=True)` fetches the configured `alt_ids` and flags one
  that looks more consolidated than the pinned id.

A third lesson: an early note that this portal is JavaScript-rendered was
WRONG. It was measured on a one-paragraph promulgation decree that genuinely
is 2.4k characters. A page-size probe needs a document whose real size you
already know, or a short document reads as a broken fetch. Hence `check()`
reporting article counts rather than byte counts.

The MCP server reads the markdown this writes and imports none of it.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re

from .legal import FetchError, fetch
from .portal import (  # noqa: F401
    FETCH_URL, PARSER_VERSION, parse, render)


def read_meta(path: str) -> dict:
    """The `jog-mcp:act` block back out of a rendered file, for the refresh
    job: it needs the previous text_hash to tell a real amendment from a
    re-render."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(4000)
    except OSError:
        return {}
    m = re.search(r"<!--\s*jog-mcp:act\s*(.*?)-->", text, re.S)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


# --- fetching ----------------------------------------------------------------

def act_hash(parsed: dict) -> str:
    """Hash of the TEXT only, WHITESPACE-NORMALISED.

    Normalising is the point, and it is the same reasoning as
    `legal.canonical`: what matters is when the LAW moved, and how many
    newlines our renderer puts between two paragraphs is not the law. Without
    this, improving the parser announces every act as amended."""
    body = "\n".join(
        "\n".join([s["ttl"], s["den"], s["group"], s["body"]]
                  + list(s.get("notes") or []))
        for s in parsed["sections"])
    return hashlib.sha256(
        re.sub(r"\s+", " ", body).strip().encode("utf-8")).hexdigest()[:16]


def article_number(ttl: str):
    """24 from "Articolul 24"; None if there is no plain number. "24^1" sorts
    with 24, which is what a range like 535-952 should mean."""
    m = re.search(r"(\d+)", ttl or "")
    return int(m.group(1)) if m else None


def keep_range(parsed: dict, spec: str) -> dict:
    """Narrow an act to an article range, e.g. "535-952".

    Why this exists: the Civil Code is 2664 articles and cadastre work needs
    Cartea III — bunuri, proprietate, dezmembrăminte and cartea funciară. The
    rest is contract and family law, which would quadruple the corpus and
    give every cadastre question a few hundred irrelevant articles to
    outrank. Narrowing is a decision recorded per act in acts.yaml, not a
    silent truncation."""
    if not spec:
        return parsed
    lo, _, hi = str(spec).partition("-")
    try:
        lo_i, hi_i = int(lo), int(hi or lo)
    except ValueError:
        return parsed
    kept = []
    for sec in parsed["sections"]:
        if sec["kind"] != "ART":
            continue
        n = article_number(sec["ttl"])
        if n is not None and lo_i <= n <= hi_i:
            kept.append(sec)
    out = dict(parsed)
    out["sections"] = kept
    out["articles"] = kept
    out["annexes"] = []
    out["preamble"] = []
    return out


def fetch_act(doc_id: str, timeout: int = 30, max_bytes: int = 0,
              article_range: str = "") -> dict:
    """Download and parse one act. Raises FetchError."""
    kw = {"max_bytes": max_bytes} if max_bytes else {}
    html_text = fetch(FETCH_URL.format(doc_id=doc_id), timeout=timeout, **kw)
    parsed = parse(html_text)
    if article_range:
        parsed = keep_range(parsed, article_range)
    if not parsed["sections"]:
        raise FetchError(
            f"doc {doc_id}: no articles parsed — the page may be a stub, a "
            f"repealed act, or the portal's markup changed "
            f"({len(html_text)} bytes downloaded)")
    return parsed


def acts_root(cfg: dict) -> str:
    """Where the corpus lives: `root` in acts.yaml, else `acts/` at the
    repository root."""
    root = (cfg.get("root") or "").strip()
    if root:
        return os.path.abspath(os.path.expanduser(root))
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return os.path.join(here, "acts")


def configured(cfg: dict) -> list:
    """The acts to keep full text for, from acts.yaml's `track` list."""
    return list(cfg.get("track") or [])


def refresh(cfg: dict, only: str = "", dry_run: bool = False,
            log=print) -> list:
    """Fetch every configured act and write the ones that changed.

    Returns [{'id','status','articles','note'}]; status is one of
    fetched / unchanged / failed / skipped. Writing only on a changed
    text_hash keeps `git log acts/` a list of real amendments."""
    root = acts_root(cfg)
    os.makedirs(root, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = []
    for entry in configured(cfg):
        act_id = str(entry.get("id") or "").strip()
        doc_id = str(entry.get("doc_id") or "").strip()
        if not act_id or not doc_id:
            continue
        if only and only != act_id:
            results.append({"id": act_id, "status": "skipped",
                            "articles": 0, "note": ""})
            continue
        path = os.path.join(root, f"{act_id}.md")
        meta = read_meta(path)
        prev = meta.get("text_hash", "")
        prev_parser = str(meta.get("parser") or "1")
        try:
            parsed = fetch_act(doc_id,
                               timeout=int(entry.get("timeout") or 30),
                               max_bytes=int(entry.get("max_bytes") or 0),
                               article_range=str(entry.get("articles") or ""))
        except FetchError as e:
            log(f"  {act_id}: FAILED — {e}")
            results.append({"id": act_id, "status": "failed",
                            "articles": 0, "note": str(e)[:200]})
            continue
        h = act_hash(parsed)
        n = len(parsed["articles"])
        if h == prev and prev_parser == str(PARSER_VERSION):
            log(f"  {act_id}: unchanged ({n} articles)")
            results.append({"id": act_id, "status": "unchanged",
                            "articles": n, "note": ""})
            continue
        if h == prev:
            # Same law, new renderer. Rewrite the file so the corpus is
            # consistent, but this is OUR churn and nobody must be told a
            # law moved.
            log(f"  {act_id}: reformatted by parser v{prev_parser}"
                f"->v{PARSER_VERSION} ({n} articles)")
            if not dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(render(act_id, doc_id, parsed, fetched=now,
                                   text_hash=h))
            results.append({"id": act_id, "status": "reformatted",
                            "articles": n, "note": f"parser v{PARSER_VERSION}"})
            continue
        note = "new" if not prev else f"changed ({prev} -> {h})"
        log(f"  {act_id}: {note}, {n} articles")
        if not dry_run:
            with open(path, "w", encoding="utf-8") as f:
                f.write(render(act_id, doc_id, parsed, fetched=now, text_hash=h))
        results.append({"id": act_id, "status": "fetched",
                        "articles": n, "note": note})
    if not dry_run:
        _write_manifest(root, results)
    return results


def _write_manifest(root: str, results: list) -> None:
    """A machine-readable index of the corpus, so the MCP server can list acts
    and report staleness without re-parsing every file."""
    entries = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".md") or fn.startswith("_"):
            continue
        meta = read_meta(os.path.join(root, fn))
        if meta:
            entries.append(meta)
    with open(os.path.join(root, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"acts": entries}, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- configuration check -----------------------------------------------------

# Below this many articles an entry is probably the approving ordin rather than
# the regulation it approves (the Ordin 700/2014 trap). Override per entry with
# `min_articles` for acts that really are this short.
THIN_ACT = 5

# The portal 504s on its largest documents, and the largest is usually the
# newest. Retrying is not politeness here, it is correctness.
RANK_RETRIES = 3
RANK_BACKOFF = 4          # seconds, multiplied by the attempt number


def check(cfg: dict, deep: bool = False, only: str = "", log=print) -> int:
    """Report what each configured document actually contains. Returns the
    number of problems found.

    It catches a doc_id that serves the WRONG text. That failure is silent
    otherwise — the corpus builds fine and the answers are confidently out of
    date.

    `deep` also fetches every `alt_ids` entry and prints them side by side, so
    a newer consolidation published under a fresh id shows up as a row with
    more articles than the pinned one."""
    problems = 0
    for entry in configured(cfg):
        act_id = str(entry.get("id") or "")
        if only and only != act_id:
            continue
        doc_id = str(entry.get("doc_id") or "")
        floor = int(entry.get("min_articles") or THIN_ACT)
        log(f"\n{act_id}  (doc {doc_id})")
        rows = [("pinned", doc_id)]
        if deep:
            rows += [("alt", str(a)) for a in (entry.get("alt_ids") or [])]
        best = None
        for label, did in rows:
            try:
                parsed = fetch_act(
                    did, timeout=int(entry.get("timeout") or 90),
                    max_bytes=int(entry.get("max_bytes") or 0),
                    article_range=str(entry.get("articles") or ""))
            except FetchError as e:
                log(f"  {label:7} {did:8} FAILED — {str(e)[:90]}")
                if label == "pinned":
                    problems += 1
                continue
            n = len(parsed["articles"])
            notes = sum(len(s.get("notes") or []) for s in parsed["sections"])
            chars = sum(len(s["body"]) for s in parsed["sections"])
            log(f"  {label:7} {did:8} {n:4} art {len(parsed['annexes']):3} anx "
                f"{notes:4} notes {chars:8} chars  {parsed['title'][:44]}")
            if parsed["publicat"]:
                log(f"  {'':16} publicat: {parsed['publicat'][:70]}")
            rank = (n, notes, chars)
            if best is None or rank > best[0]:
                best = (rank, label, did)
            if label == "pinned" and n < floor:
                log(f"  ^^ only {n} articles — is this the approving ordin "
                    f"rather than the regulation it approves?")
                problems += 1
        if deep and best and best[1] != "pinned":
            log(f"  ^^ {best[2]} looks more consolidated than the pinned "
                f"{doc_id} — check and repin")
            problems += 1
    return problems
