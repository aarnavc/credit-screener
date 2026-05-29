"""Form filtering + 8-K item-code extraction.

Target forms per CLAUDE.md:
  - 8-K (subject to item-code filter below)
  - NT 10-K, NT 10-Q  (Form 12b-25, late filings)
  - 25, 25-NSE       (delisting notices)

Target 8-K items: 1.01, 1.02, 2.03, 2.04, 3.01, 4.01, 5.02

Item codes are pulled from data.sec.gov submissions API (one call per CIK,
covers all recent filings). The per-filing Archives index.json is just a
directory listing and does not carry item metadata.
"""
from __future__ import annotations

import re

from db import get_entity, get_entity_tickers, upsert_entity
from edgar import get

TARGET_FORMS: set[str] = {"8-K", "NT 10-K", "NT 10-Q", "25", "25-NSE"}
TARGET_8K_ITEMS: set[str] = {"1.01", "1.02", "2.03", "2.04", "3.01", "4.01", "5.02"}

_ITEM_RE = re.compile(r"(?:^|[^\d.])(\d\.\d{2})(?!\d)")


def extract_items(raw: str) -> list[str]:
    """Pull all N.NN item codes from a string, preserving order, dedup."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _ITEM_RE.finditer(raw):
        code = m.group(1)
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


_submissions_cache: dict[str, dict] = {}


def _load_submissions(cik: str) -> dict:
    """Fetch + cache a CIK's submissions blob.

    Returns: {
        "items_by_acc": {accession: items_string},
        "sic":           str | None,   # 4-digit code, e.g. "6770"
        "sic_description": str | None,
    }
    """
    cached = _submissions_cache.get(cik)
    if cached is not None:
        return cached

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = get(url, accept="application/json").json()
    recent = data.get("filings", {}).get("recent", {}) or {}
    accs = recent.get("accessionNumber", []) or []
    items = recent.get("items", []) or []
    primary_docs = recent.get("primaryDocument", []) or []
    items_by_acc = {
        accs[i]: (items[i] if i < len(items) else "")
        for i in range(len(accs))
    }
    primary_doc_by_acc = {
        accs[i]: (primary_docs[i] if i < len(primary_docs) else "")
        for i in range(len(accs))
    }
    sic = data.get("sic")
    raw_tickers = data.get("tickers") or []
    tickers = [str(t).strip() for t in raw_tickers if t]
    out = {
        "items_by_acc":        items_by_acc,
        "primary_doc_by_acc":  primary_doc_by_acc,
        "sic":                 str(sic) if sic not in (None, "") else None,
        "sic_description":     data.get("sicDescription") or None,
        "tickers":             tickers,
    }
    _submissions_cache[cik] = out
    # Opportunistically persist SIC + tickers so the GUI / leaderboard don't
    # refetch this CIK after ingestion has already paid the network cost.
    try:
        upsert_entity(cik, out["sic"], out["sic_description"], tickers=out["tickers"] or None)
    except Exception:
        pass  # disk cache is best-effort; never block an ingestion run
    return out


def fetch_8k_items(cik: str, accession: str) -> list[str]:
    """Return 8-K item codes for (cik, accession) via the submissions API."""
    raw = _load_submissions(cik)["items_by_acc"].get(accession, "")
    return extract_items(raw)


def fetch_primary_document(cik: str, accession: str) -> str | None:
    """Return the primary document filename (e.g. 'd947923d8k.htm') for a
    filing, or None if unknown. Cached per CIK via _load_submissions.
    """
    try:
        return _load_submissions(cik)["primary_doc_by_acc"].get(accession) or None
    except Exception:
        return None


def fetch_entity_meta(cik: str) -> tuple[str | None, str | None]:
    """Return (sic, sic_description) for a CIK.

    Lookup order: in-process cache → SQLite entities table → network.
    On network hit, _load_submissions writes back to disk.
    """
    sub = _submissions_cache.get(cik)
    if sub is not None:
        return sub["sic"], sub["sic_description"]

    cached = get_entity(cik)
    if cached is not None:
        return cached

    sub = _load_submissions(cik)
    return sub["sic"], sub["sic_description"]


def fetch_entity_tickers(cik: str) -> list[str]:
    """Return tickers list for a CIK (empty list if none / lookup fails).

    Tickers were added to the entities table later than SIC, so older rows
    won't have them — in that case we fall through to a fresh fetch which
    populates both via _load_submissions.
    """
    sub = _submissions_cache.get(cik)
    if sub is not None:
        return list(sub["tickers"] or [])

    cached = get_entity_tickers(cik)
    if cached is not None:
        return cached

    try:
        sub = _load_submissions(cik)
    except Exception:
        return []
    return list(sub["tickers"] or [])


def is_target_form(form_type: str) -> bool:
    return form_type in TARGET_FORMS


def passes_8k_item_filter(items: list[str]) -> bool:
    return any(code in TARGET_8K_ITEMS for code in items)
