"""Output 2 CLI: new issuance feed (flat, unranked).

Every filing with 8-K item 2.03, most recent first. NOT ranked, NOT scored.
Routine cap-structure surveillance — review regardless of distress score.

Usage:
    python issuance.py                       # everything in DB
    python issuance.py --since 2026-05-01    # rolling window
    python issuance.py --limit 50            # cap rows
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from db import connect


def _parse_date(s: str) -> str:
    # validate + normalize
    return datetime.strptime(s, "%Y-%m-%d").date().isoformat()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="New issuance feed (Output 2)")
    p.add_argument("--since", help="Earliest filing date YYYY-MM-DD (inclusive)")
    p.add_argument("--limit", type=int, default=200, help="Max rows (default 200)")
    args = p.parse_args(argv)

    sql = (
        "SELECT filing_date, cik, company, filing_url "
        "FROM filings "
        "WHERE form_type='8-K' "
        # comma-bracketed match so 2.03 isn't matched inside e.g. '12.03'
        "AND (',' || items || ',') LIKE '%,2.03,%'"
    )
    params: list = []
    if args.since:
        sql += " AND filing_date >= ?"
        params.append(_parse_date(args.since))
    sql += " ORDER BY filing_date DESC, company LIMIT ?"
    params.append(args.limit)

    with connect() as conn:
        rows = list(conn.execute(sql, params))

    print("Issuance feed (8-K item 2.03)  unranked, most recent first")
    print()

    if not rows:
        print("(no 2.03 filings match)")
        return 0

    # render
    company_w = min(50, max(len("Company"), max(len(r[2]) for r in rows)))
    fmt = f"  {{:<10}}  {{:<10}}  {{:<{company_w}}}  {{}}"
    print(fmt.format("Date", "CIK", "Company", "URL"))
    print(fmt.format("-" * 10, "-" * 10, "-" * company_w, "-" * 3))
    for filing_date, cik, company, url in rows:
        print(fmt.format(filing_date, cik, company[:company_w], url))

    print(f"\n{len(rows)} row(s)" + (f" (limit {args.limit})" if len(rows) == args.limit else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
