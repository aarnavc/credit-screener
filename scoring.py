"""Output 1 (distress leaderboard) scoring — pure functions over filings rows.

Spec lives in CLAUDE.md "Scoring & Outputs (Layer 1)". Keep this module
faithful to that spec; do not silently drift.

UNVALIDATED HYPOTHESIS: weights + window are starting guesses, not tuned.

Suppression rule (CLAUDE.md "Known Issues"):
  3.01 (delisting) co-located with 2.01 (completion of acquisition) within
  ±3 days is almost always a transaction close, not distress. The 3.01
  weight is dropped but the item stays in the decomposition with a note —
  suppress-and-flag, never drop silently. Caveats: 2.01 may be filed late
  or under a different CIK; the rule is a strong heuristic, not airtight.
  It also depends on 2.01 being present in the stored items list, which
  only happens when 2.01 is on the same filing as a targeted item (since
  a 2.01-only 8-K is not ingested).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

WEIGHTS: dict[str, int] = {
    "2.04": 10,  # triggering event / acceleration / missed payment
    "3.01":  8,  # delisting / listing-standard failure
    "4.01":  6,  # auditor change
    "1.02":  5,  # termination of material agreement
    "1.01":  3,  # entry into material agreement (amend/waiver)
    "2.03":  3,  # new direct financial obligation (see Output 2 + escalation)
    "5.02":  2,  # officer departure
}

# >=5 weight items count as "heavy" signals for the distinct-type multiplier.
HEAVY_ITEMS: frozenset[str] = frozenset(k for k, w in WEIGHTS.items() if w >= 5)

WINDOW_DAYS: int = 90  # TUNE LATER

# Suppression constants — see module docstring.
SUPPRESSOR_ITEM: str = "2.01"
SUPPRESSED_ITEM: str = "3.01"
SUPPRESSION_WINDOW_DAYS: int = 3
SUPPRESSION_NOTE: str = "SUPPRESSED: 2.01 co-present ±3d, likely transaction"


@dataclass
class Contribution:
    item: str
    date: str               # YYYY-MM-DD
    weight: float           # applied weight (0.0 if suppressed)
    accession: str = ""     # source filing — used by Layer 2 to fetch text
    suppressed: bool = False
    note: str | None = None
    # Layer 2 enrichment fields — populated by enrich.py, ignored by Layer 1.
    matched_phrases: list[str] = field(default_factory=list)
    adjustment_note: str | None = None


@dataclass
class ScoredIssuer:
    cik: str
    company: str
    score: float
    base: float
    multiplier: float
    distinct_heavy: int
    contributions: list[Contribution] = field(default_factory=list)

    def decompose(self) -> str:
        """One-line human summary per spec exemplar."""
        parts = []
        for c in self.contributions:
            if c.suppressed:
                parts.append(f"{c.item} on {_short_date(c.date)} ({c.note})")
            else:
                parts.append(f"{c.item} on {_short_date(c.date)} (+{c.weight:g})")
        return f"{self.score:.1f}: " + ", ".join(parts) + f", x{self.multiplier:g}"


def _short_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}"


def _multiplier(distinct_heavy: int) -> float:
    if distinct_heavy >= 3:
        return 1.8
    if distinct_heavy == 2:
        return 1.4
    return 1.0


def _parse(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()


def _within_days(a: str, b: str, window: int) -> bool:
    return abs((_parse(a) - _parse(b)).days) <= window


def score_filings(
    filings: list[tuple[str, str, str, str, str]],
    asof: date,
    window_days: int = WINDOW_DAYS,
) -> list[ScoredIssuer]:
    """Score a list of (cik, company, items_csv, filing_date, accession) rows.

    Rows are kept when filing_date is in [asof - window_days + 1, asof].
    Suppression: 3.01 occurrences within ±SUPPRESSION_WINDOW_DAYS of any
    2.01 from the same CIK get weight 0 and a note (but still appear in
    contributions for auditability).
    """
    start = asof - timedelta(days=window_days - 1)
    start_iso, asof_iso = start.isoformat(), asof.isoformat()

    # Per-CIK collection. Accession travels with each weighted-item occurrence
    # so Layer 2 enrichment can later fetch the right filing.
    weighted: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )  # cik -> item -> [(date, accession), ...]
    suppressor_dates: dict[str, set[str]] = defaultdict(set)
    latest_company: dict[str, tuple[str, str]] = {}  # cik -> (date, company)

    for cik, company, items_csv, filing_date, accession in filings:
        if not (start_iso <= filing_date <= asof_iso):
            continue
        if not items_csv:
            continue
        codes = [c.strip() for c in items_csv.split(",")]
        for code in codes:
            if code == SUPPRESSOR_ITEM:
                suppressor_dates[cik].add(filing_date)
            if code in WEIGHTS:
                weighted[cik][code].append((filing_date, accession))
        if codes:
            prev = latest_company.get(cik)
            if prev is None or filing_date >= prev[0]:
                latest_company[cik] = (filing_date, company)

    results: list[ScoredIssuer] = []
    for cik, by_item in weighted.items():
        suppressors = suppressor_dates.get(cik, set())
        contribs: list[Contribution] = []
        base = 0.0

        for code, occs in by_item.items():
            w = WEIGHTS[code]
            occs.sort()  # sort by (date, accession)

            flags = [
                code == SUPPRESSED_ITEM and any(
                    _within_days(dt, s, SUPPRESSION_WINDOW_DAYS) for s in suppressors
                )
                for dt, _ in occs
            ]

            survivor_n = 0
            for (dt, acc), suppressed in zip(occs, flags):
                if suppressed:
                    contribs.append(Contribution(
                        item=code, date=dt, weight=0.0, accession=acc,
                        suppressed=True, note=SUPPRESSION_NOTE,
                    ))
                    continue
                applied = float(w) if survivor_n == 0 else w / 2.0
                survivor_n += 1
                contribs.append(Contribution(
                    item=code, date=dt, weight=applied, accession=acc,
                ))
                base += applied

        distinct_heavy = len({
            c.item for c in contribs
            if c.item in HEAVY_ITEMS and not c.suppressed
        })
        mult = _multiplier(distinct_heavy)
        score = base * mult

        if score <= 0:
            continue

        contribs.sort(key=lambda c: (c.date, c.item))
        results.append(ScoredIssuer(
            cik=cik,
            company=latest_company[cik][1],
            score=score,
            base=base,
            multiplier=mult,
            distinct_heavy=distinct_heavy,
            contributions=contribs,
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def recompute(issuer: ScoredIssuer) -> None:
    """Rebuild base / distinct_heavy / multiplier / score from contributions.

    Used by Layer 2 after it mutates contribution weights. distinct_heavy
    is computed by *effective* weight (>=5) so an escalated 2.03 counts
    as heavy.
    """
    issuer.base = sum(c.weight for c in issuer.contributions if not c.suppressed)
    max_weight_per_item: dict[str, float] = defaultdict(float)
    for c in issuer.contributions:
        if c.suppressed:
            continue
        if c.weight > max_weight_per_item[c.item]:
            max_weight_per_item[c.item] = c.weight
    issuer.distinct_heavy = sum(1 for w in max_weight_per_item.values() if w >= 5)
    issuer.multiplier = _multiplier(issuer.distinct_heavy)
    issuer.score = issuer.base * issuer.multiplier
