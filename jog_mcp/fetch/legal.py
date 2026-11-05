"""Fetch a legal page and reduce it to comparable text.

Run by a person, never by the model: the page is downloaded and parsed by
this module, and only the parsed statute text ever reaches a model, as data.
Fetched HTML therefore has no way to make a request of its own, which is the
point: legislatie.just.ro is a third party, and an injected instruction in a
page must be inert.

There is deliberately no HTML parser dependency. The output is never
rendered — it is hashed, diffed and summarised — so a regex strip that keeps
the visible words in order is all the fidelity this needs.
"""
from __future__ import annotations

import difflib
import hashlib
import html as html_mod
import re

import requests

MAX_BYTES = 5_000_000          # a statute page is ~100KB; anything larger is a trap
TIMEOUT = 30
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")

# Text that changes on every load and says nothing about the law. Left out of
# the hash, or every single check would report a change and the alert would be
# trained out of existence within a week.
_VOLATILE = [
    re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"),          # 15.09.2026
    re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"),                    # 14:03
    re.compile(r"(?i)\b(vizualiz\w+|afis\w+|views?|accesari)\b[^\n]{0,20}\d+"),
    re.compile(r"(?i)copyright[^\n]{0,60}"),
    re.compile(r"(?i)ultima actualizare[^\n]{0,60}"),
    re.compile(r"\bcsrf[_-]?token\b[^\n]{0,80}", re.I),
    re.compile(r"__(VIEWSTATE|EVENTVALIDATION)[^\n]{0,200}"),
]


class FetchError(RuntimeError):
    pass


def fetch(url: str, timeout: int = TIMEOUT, max_bytes: int = MAX_BYTES) -> str:
    """GET a tracked page. URLs built from acts.yaml only — hence the scheme check
    rather than a full SSRF guard, plus a hard size cap so a misconfigured
    entry pointing at a large file cannot exhaust memory.

    `max_bytes` is raisable per call because one real document breaks the
    default: the Civil Code is 6.9 MB of HTML (2664 articles). The cap stays
    where it is by default; only an act configured with `max_bytes` in
    acts.yaml may ask for more."""
    if not re.match(r"^https?://", url or "", re.I):
        raise FetchError(f"refusing non-http(s) url: {url!r}")
    try:
        with requests.get(url, timeout=timeout, stream=True,
                          headers={"User-Agent": UA,
                                   "Accept-Language": "ro,hu;q=0.8,en;q=0.6"}) as r:
            r.raise_for_status()
            body = r.raw.read(max_bytes + 1, decode_content=True) or b""
            if len(body) > max_bytes:
                raise FetchError(f"response over {max_bytes} bytes")
            enc = r.encoding or "utf-8"
    except requests.RequestException as e:
        raise FetchError(f"{type(e).__name__}: {str(e)[:200]}") from None
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def to_text(raw: str) -> str:
    """Visible words, in order, with markup and entities resolved."""
    t = _DROP_BLOCKS.sub(" ", raw or "")
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", t)
    t = _TAGS.sub(" ", t)
    t = html_mod.unescape(t)
    t = _WS.sub(" ", t)
    t = "\n".join(ln.strip() for ln in t.splitlines())
    return _BLANK.sub("\n\n", t).strip()


def canonical(text: str) -> str:
    """The form that gets hashed: volatile noise removed, case folded."""
    t = text or ""
    for pat in _VOLATILE:
        t = pat.sub(" ", t)
    return _WS.sub(" ", t).lower().strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()[:16]


def similarity(before: str, after: str) -> float:
    """0..1 over the canonical forms. A page can change hash for reasons the
    volatile list did not anticipate (a reordered menu, a new cookie banner),
    so the scan also requires a real drop in similarity before it calls the
    LLM and wakes anyone up."""
    a, b = canonical(before), canonical(after)
    if not a or not b:
        return 0.0
    # autojunk misfires on long legal text with many repeated stock phrases
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def changed_excerpt(before: str, after: str, budget: int = 4000) -> str:
    """The changed lines only, so the summarising prompt sees the diff rather
    than two 14k-character walls that it has to compare itself."""
    a = [ln for ln in (before or "").splitlines() if ln.strip()]
    b = [ln for ln in (after or "").splitlines() if ln.strip()]
    out, used = [], 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for ln in a[i1:i2]:
            out.append(f"- {ln}")
        for ln in b[j1:j2]:
            out.append(f"+ {ln}")
        used = sum(len(x) for x in out)
        if used > budget:
            out.append("… (diff truncated)")
            break
    return "\n".join(out)[:budget]


def probe(url: str, timeout: int = 15) -> tuple[int | None, str]:
    """(status, note) for --check-laws. Never raises: the point is a report of
    which configured URLs are dead, not a crash on the first one."""
    if not re.match(r"^https?://", url or "", re.I):
        return None, "not an http(s) url"
    try:
        r = requests.get(url, timeout=timeout, stream=True, allow_redirects=True,
                         headers={"User-Agent": UA})
        note = "ok" if r.ok else r.reason or ""
        if r.url.rstrip("/") != url.rstrip("/"):
            note += f" (redirected to {r.url[:90]})"
        r.close()
        return r.status_code, note
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
