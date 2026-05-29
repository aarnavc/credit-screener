"""Pull and parse EDGAR daily form-index.

Daily index lives at
  https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{q}/form.{YYYYMMDD}.idx

Each line after the header looks like:
  Form Type        Company Name        CIK     Date Filed   Filename
fixed-width-ish, separated by 2+ spaces; the trailing Filename is a path
under /Archives/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import requests

from edgar import get


class IndexNotPublished(Exception):
    """Raised when EDGAR has not yet published the daily index for a date."""

BASE = "https://www.sec.gov/Archives/edgar/daily-index"


@dataclass(frozen=True)
class IndexRow:
    form_type: str
    company: str
    cik: str          # zero-padded to 10 digits
    filing_date: str  # YYYY-MM-DD
    filename: str     # e.g. edgar/data/320193/0000320193-25-000010.txt

    @property
    def accession(self) -> str:
        # filename ends in <accession>.txt; accession has dashes
        stem = self.filename.rsplit("/", 1)[-1]
        return stem.removesuffix(".txt")

    @property
    def filing_url(self) -> str:
        return f"https://www.sec.gov/Archives/{self.filename}"


def index_url_for(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{BASE}/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"


# Separator on real EDGAR daily indices is one solid dash line.
_DASH_LINE = re.compile(r"^-{20,}\s*$")
# Data rows have 5 whitespace-delimited fields; form types like
# "NT 10-K" contain a single internal space, so split on 2+ spaces.
_SPLIT_RE = re.compile(r"\s{2,}")


def parse_index(text: str) -> list[IndexRow]:
    lines = text.splitlines()
    sep_idx = next(
        (i for i, ln in enumerate(lines) if _DASH_LINE.match(ln)),
        None,
    )
    if sep_idx is None:
        raise ValueError("daily index: separator line not found")

    rows: list[IndexRow] = []
    for ln in lines[sep_idx + 1:]:
        if not ln.strip():
            continue
        fields = _SPLIT_RE.split(ln.strip())
        if len(fields) != 5:
            continue
        form_type, company, cik, date_filed, filename = fields
        rows.append(IndexRow(
            form_type=form_type.strip(),
            company=company.strip(),
            cik=cik.strip().zfill(10),
            filing_date=_normalize_date(date_filed.strip()),
            filename=filename.strip(),
        ))
    return rows


def _normalize_date(s: str) -> str:
    """EDGAR daily index uses YYYYMMDD; normalize to YYYY-MM-DD."""
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d").date().isoformat()
    return s


def fetch_index(d: date) -> list[IndexRow]:
    url = index_url_for(d)
    try:
        resp = get(url, accept="text/plain")
    except requests.HTTPError as e:
        # EDGAR returns 403 (sometimes 404) for dates whose index file
        # hasn't been generated yet — typical for the current trading day
        # before EOD, or for non-trading days.
        status = e.response.status_code if e.response is not None else None
        if status in (403, 404):
            raise IndexNotPublished(
                f"daily index for {d} not yet published (HTTP {status})"
            ) from e
        raise
    return parse_index(resp.text)
