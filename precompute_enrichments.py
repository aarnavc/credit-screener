"""Precompute Layer 2 enrichment for all 8-K filings with 2.03 or 3.01 items.

Idempotent — only processes filings without an enrichment row in the DB.
Designed to run during the daily GitHub Action (after main.py) so the
committed SQLite always carries fresh, pre-baked Layer 2 verdicts. The
Streamlit app then becomes a pure reader: window changes don't trigger
live SEC fetches.

Manual use:
    python precompute_enrichments.py
"""
from __future__ import annotations

import sys

from db import connect, init_db, upsert_enrichment
from enrich import (
    _live_2_03_enrichment,
    _live_3_01_enrichment,
    fetch_filing_text,
)

PROGRESS_EVERY = 25


def _needs_enrichment() -> list[tuple[str, str, str, str]]:
    """Return (accession, cik, items_csv, filing_url) for 8-K filings
    whose 2.03 / 3.01 items don't yet have an enrichment row."""
    with connect() as conn:
        return list(conn.execute(
            """
            SELECT f.accession, f.cik, f.items, f.filing_url
            FROM filings f
            WHERE f.form_type = '8-K'
              AND f.items IS NOT NULL
              AND (
                    (',' || f.items || ',') LIKE '%,2.03,%'
                 OR (',' || f.items || ',') LIKE '%,3.01,%'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM enrichments e
                  WHERE e.accession = f.accession
                    AND e.item IN ('2.03', '3.01')
              )
            ORDER BY f.filing_date DESC, f.accession
            """
        ))


def main() -> int:
    init_db()
    todo = _needs_enrichment()
    if not todo:
        print("All 2.03 / 3.01 filings already enriched. Nothing to do.")
        return 0

    print(f"Enriching {len(todo)} filings...")
    enriched_filings = 0
    enriched_rows = 0
    failures = 0

    for i, (acc, cik, items_csv, url) in enumerate(todo, 1):
        text = fetch_filing_text(acc, url)
        if not text:
            failures += 1
            print(f"  [{i:>4}/{len(todo)}] {acc}: skip (couldn't fetch text)",
                  file=sys.stderr)
            continue

        items = {it.strip() for it in items_csv.split(",")}
        wrote_any = False
        if "2.03" in items:
            e = _live_2_03_enrichment(text)
            upsert_enrichment(
                acc, "2.03",
                e["weight_override"], e["suppressed"],
                e["matched_phrases"], e["adjustment_note"], e["suppression_note"],
            )
            enriched_rows += 1
            wrote_any = True
        if "3.01" in items:
            e = _live_3_01_enrichment(text)
            upsert_enrichment(
                acc, "3.01",
                e["weight_override"], e["suppressed"],
                e["matched_phrases"], e["adjustment_note"], e["suppression_note"],
            )
            enriched_rows += 1
            wrote_any = True
        if wrote_any:
            enriched_filings += 1
        if i % PROGRESS_EVERY == 0:
            print(f"  processed {i}/{len(todo)}")

    print(
        f"Done — {enriched_filings} filings enriched ({enriched_rows} "
        f"enrichment rows), {failures} skipped."
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
