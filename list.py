"""Pretty-print filings from screener.sqlite.

Usage:
    python list.py                       # all rows, newest first
    python list.py --form 8-K            # filter by form type
    python list.py --company tower       # case-insensitive substring match
    python list.py --cik 0001053507      # filter by CIK
    python list.py --date 2026-05-27     # filter by filing date
    python list.py --limit 20            # cap rows shown
"""
from __future__ import annotations

import argparse
import sys

from db import connect


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="List filings from screener.sqlite")
    p.add_argument("--form", help="Form type (e.g. 8-K, NT 10-Q, 25-NSE)")
    p.add_argument("--company", help="Substring match on company name")
    p.add_argument("--cik", help="Exact CIK (10-digit, zero-padded ok)")
    p.add_argument("--date", help="Filing date YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=50, help="Max rows (default 50)")
    args = p.parse_args(argv)

    where: list[str] = []
    params: list[str] = []
    if args.form:
        where.append("form_type = ?")
        params.append(args.form)
    if args.company:
        where.append("LOWER(company) LIKE ?")
        params.append(f"%{args.company.lower()}%")
    if args.cik:
        where.append("cik = ?")
        params.append(args.cik.zfill(10))
    if args.date:
        where.append("filing_date = ?")
        params.append(args.date)

    sql = "SELECT filing_date, form_type, cik, company, items, filing_url FROM filings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY filing_date DESC, company LIMIT ?"
    params.append(args.limit)

    with connect() as conn:
        rows = list(conn.execute(sql, params))

    if not rows:
        print("(no matching filings)")
        return 0

    # column widths
    headers = ["Date", "Form", "CIK", "Company", "Items"]
    data = [(r[0], r[1], r[2], r[3], r[4] or "-") for r in rows]
    widths = [
        max(len(h), max(len(str(row[i])) for row in data))
        for i, h in enumerate(headers)
    ]
    # clamp Company to keep things readable
    widths[3] = min(widths[3], 50)

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in data:
        company = row[3][:50]
        print(fmt.format(row[0], row[1], row[2], company, row[4]))

    print(f"\n{len(rows)} row(s)" + (f" (limit {args.limit})" if len(rows) == args.limit else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
