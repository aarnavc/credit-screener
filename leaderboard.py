"""Output 1 CLI: distress leaderboard, decomposable.

Usage:
    python leaderboard.py                    # asof = today, 90d window
    python leaderboard.py --asof 2026-05-27  # asof a specific date
    python leaderboard.py --window 60        # tighter window
    python leaderboard.py --limit 20         # cap rows
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from db import connect, init_db
from enrich import enrich_results
from scoring import WINDOW_DAYS, ScoredIssuer, score_filings
from universe import filter_reason


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _load_rows(asof: date, window_days: int):
    """Return (rows for scoring, accession→filing_url lookup)."""
    from datetime import timedelta
    start = (asof - timedelta(days=window_days - 1)).isoformat()
    end = asof.isoformat()
    with connect() as conn:
        raw = list(conn.execute(
            "SELECT cik, company, items, filing_date, accession, filing_url "
            "FROM filings "
            "WHERE form_type='8-K' AND items IS NOT NULL "
            "AND filing_date BETWEEN ? AND ?",
            (start, end),
        ))
    rows = [(cik, company, items, fd, acc) for cik, company, items, fd, acc, _ in raw]
    url_lookup = {acc: url for *_, acc, url in raw}
    return rows, url_lookup


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Distress leaderboard (Output 1)")
    p.add_argument("--asof", help="As-of date YYYY-MM-DD (default today UTC)")
    p.add_argument("--window", type=int, default=WINDOW_DAYS,
                   help=f"Trailing window in days (default {WINDOW_DAYS})")
    p.add_argument("--limit", type=int, default=25, help="Max issuers shown (default 25)")
    args = p.parse_args(argv)

    init_db()  # ensure entities table exists for older DBs

    asof = _parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    rows, url_lookup = _load_rows(asof, args.window)
    results = score_filings(rows, asof=asof, window_days=args.window)

    # Partition first so we don't waste fetches on demoted shells/SPACs.
    kept: list[ScoredIssuer] = []
    demoted: list[tuple[ScoredIssuer, str]] = []
    for r in results:
        reason = filter_reason(r.cik, r.company)
        if reason is None:
            kept.append(r)
        else:
            demoted.append((r, reason))

    # Layer 2: enrich the top of the kept list (cap fetches at the display
    # limit — Layer 2 only fires for issuers the user will actually see).
    enrich_results(kept[:args.limit], url_lookup)
    kept.sort(key=lambda r: r.score, reverse=True)

    print(f"Distress leaderboard  asof={asof}  window={args.window}d  "
          f"(scoring is an UNVALIDATED HYPOTHESIS — review manually)")
    print()

    if not kept:
        print("(no non-filtered issuers with weighted items in window)")
    else:
        for i, r in enumerate(kept[:args.limit], 1):
            _print_issuer(i, r)

    if demoted:
        print()
        print(f"Filtered (shell/SPAC) — demoted, shown for auditability "
              f"({len(demoted)} issuer(s))")
        print()
        for i, (r, reason) in enumerate(demoted[:args.limit], 1):
            _print_issuer(i, r, tag=f"[{reason}]")

    print()
    print(f"main: {min(len(kept), args.limit)} of {len(kept)} kept  |  "
          f"filtered: {min(len(demoted), args.limit)} of {len(demoted)}")
    return 0


def _print_issuer(i: int, r: ScoredIssuer, tag: str = "") -> None:
    suffix = f"  {tag}" if tag else ""
    pre = getattr(r, "pre_enrich_score", None)
    score_str = f"{pre:>5.1f}→{r.score:<5.1f}" if pre is not None else f"{r.score:>5.1f}"
    print(f"{i:>3}. {score_str}  {r.company}  (CIK {r.cik}){suffix}")
    contributing = [c for c in r.contributions if not c.suppressed]
    if contributing:
        print(f"     = ({' + '.join(f'{c.weight:g}' for c in contributing)}) "
              f"x {r.multiplier:g}")
    else:
        print(f"     = 0  (all weighted items suppressed)")
    for c in r.contributions:
        line = f"     - {c.item} on {c.date}"
        if c.suppressed:
            line += f"  (suppressed: {c.note})"
        else:
            line += f"  (+{c.weight:g})"
        if c.adjustment_note:
            line += f"  [{c.adjustment_note}]"
        if c.matched_phrases:
            line += f"  matched: {', '.join(c.matched_phrases)}"
        print(line)
    print()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
