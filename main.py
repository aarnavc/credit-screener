"""Layer 1 entry point.

Usage:
    python main.py                 # pull today (UTC)
    python main.py 2026-05-27      # pull a specific date
    python main.py 2026-05-20 2026-05-27   # inclusive range

Pulls EDGAR daily form index, filters to target forms, enriches 8-Ks with
item codes, writes to screener.sqlite. Idempotent on accession number.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone

from daily_index import IndexNotPublished, IndexRow, fetch_index
from db import init_db, upsert_filings
from items import (
    TARGET_8K_ITEMS,
    fetch_8k_items,
    fetch_primary_document,
    is_target_form,
    passes_8k_item_filter,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _row_to_record(
    row: IndexRow,
    items: list[str] | None,
    primary_document: str | None,
) -> dict:
    return {
        "accession":         row.accession,
        "cik":               row.cik,
        "company":           row.company,
        "form_type":         row.form_type,
        "items":             ",".join(items) if items else None,
        "filing_date":       row.filing_date,
        "filing_url":        row.filing_url,
        "primary_document":  primary_document,
    }


def run_for_date(d: date) -> tuple[int, int]:
    """Pull, filter, persist. Returns (kept, inserted)."""
    rows = fetch_index(d)
    target_rows = [r for r in rows if is_target_form(r.form_type)]

    records: list[dict] = []
    for r in target_rows:
        if r.form_type == "8-K":
            try:
                items = fetch_8k_items(r.cik, r.accession)
            except Exception as e:
                # Don't drop the row silently — log and skip.
                print(f"  ! 8-K items fetch failed {r.accession}: {e}", file=sys.stderr)
                continue
            if not passes_8k_item_filter(items):
                continue
            # primaryDocument comes from the same submissions API call, so
            # this is free (in-process cache hit).
            primary_doc = fetch_primary_document(r.cik, r.accession)
            records.append(_row_to_record(r, items, primary_doc))
        else:
            primary_doc = fetch_primary_document(r.cik, r.accession)
            records.append(_row_to_record(r, None, primary_doc))

    inserted = upsert_filings(records)
    return len(records), inserted


def main(argv: list[str]) -> int:
    init_db()

    if len(argv) == 0:
        dates = [datetime.now(timezone.utc).date()]
    elif len(argv) == 1:
        dates = [_parse_date(argv[0])]
    elif len(argv) == 2:
        start, end = _parse_date(argv[0]), _parse_date(argv[1])
        if end < start:
            print("end date before start date", file=sys.stderr)
            return 2
        dates = list(_date_range(start, end))
    else:
        print(__doc__, file=sys.stderr)
        return 2

    total_kept = total_inserted = 0
    for d in dates:
        # Skip weekends — no daily index published.
        if d.weekday() >= 5:
            print(f"{d} skip (weekend)")
            continue
        try:
            kept, inserted = run_for_date(d)
        except IndexNotPublished:
            print(f"{d} skip (index not yet published — try later or pick an earlier date)")
            continue
        except Exception as e:
            print(f"{d} FAILED: {e}", file=sys.stderr)
            continue
        total_kept += kept
        total_inserted += inserted
        print(f"{d} kept={kept} inserted={inserted}")

    print(f"\nTotal kept={total_kept} inserted={total_inserted}")
    print(f"8-K item filter: {sorted(TARGET_8K_ITEMS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
