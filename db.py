"""SQLite store for filings.

One row per filing (accession is PK). `items` is a comma-joined list of
8-K item codes (e.g. "1.02,2.04") or NULL for non-8-K forms.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Default DB lives in data/ so it can be committed alongside the code in
# the GitHub repo. Override with SCREENER_DB env var (used by GitHub
# Actions / Streamlit Cloud if the data path ever needs to move).
DB_PATH = Path(os.environ.get(
    "SCREENER_DB",
    Path(__file__).parent / "data" / "screener.sqlite",
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession        TEXT NOT NULL,
    cik              TEXT NOT NULL,
    company          TEXT NOT NULL,
    form_type        TEXT NOT NULL,
    items            TEXT,
    filing_date      TEXT NOT NULL,
    filing_url       TEXT NOT NULL,
    primary_document TEXT,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (accession, cik)
);

CREATE INDEX IF NOT EXISTS idx_filings_cik         ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_filing_date ON filings(filing_date);
CREATE INDEX IF NOT EXISTS idx_filings_form_type   ON filings(form_type);

-- Per-CIK entity metadata, populated lazily from data.sec.gov submissions.
-- SIC codes are effectively stable, so we cache forever; refresh manually
-- by DELETE if a code ever changes. `tickers` is comma-joined.
CREATE TABLE IF NOT EXISTS entities (
    cik              TEXT PRIMARY KEY,
    sic              TEXT,
    sic_description  TEXT,
    tickers          TEXT,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Precomputed Layer 2 enrichment per filing/item — populated by
-- precompute_enrichments.py during ingestion. The app reads from here
-- so window changes don't trigger live SEC fetches. matched_phrases is
-- a JSON-encoded list of strings (with audit annotations like [active]).
CREATE TABLE IF NOT EXISTS enrichments (
    accession         TEXT NOT NULL,
    item              TEXT NOT NULL,
    weight_override   REAL,
    suppressed        INTEGER NOT NULL DEFAULT 0,
    matched_phrases   TEXT,
    adjustment_note   TEXT,
    suppression_note  TEXT,
    enriched_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (accession, item)
);
"""


@contextmanager
def connect(path: Path = DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        # Migrations: SQLite ALTER TABLE doesn't support IF NOT EXISTS for ADD
        # COLUMN until 3.35+, so try and swallow the "duplicate column name"
        # errors for older DBs that pre-date these columns.
        for ddl in (
            "ALTER TABLE entities ADD COLUMN tickers TEXT",
            "ALTER TABLE filings  ADD COLUMN primary_document TEXT",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise


def upsert_filings(rows: list[dict], path: Path = DB_PATH) -> int:
    """Insert filings; ignore conflicts on accession. Returns rows inserted."""
    if not rows:
        return 0
    with connect(path) as conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO filings
                (accession, cik, company, form_type, items, filing_date,
                 filing_url, primary_document)
            VALUES
                (:accession, :cik, :company, :form_type, :items, :filing_date,
                 :filing_url, :primary_document)
            """,
            rows,
        )
        return cur.rowcount


def update_primary_documents(
    pairs: list[tuple[str, str, str]],
    path: Path = DB_PATH,
) -> int:
    """Backfill helper. Each tuple is (accession, cik, primary_document).
    Returns the number of rows touched."""
    if not pairs:
        return 0
    with connect(path) as conn:
        cur = conn.executemany(
            "UPDATE filings SET primary_document = ? "
            "WHERE accession = ? AND cik = ? AND primary_document IS NULL",
            [(pd, acc, cik) for acc, cik, pd in pairs],
        )
        return cur.rowcount


import json as _json  # used by enrichment helpers below


def get_enrichments(
    accessions: list[str],
    path: Path = DB_PATH,
) -> dict[tuple[str, str], dict]:
    """Bulk-load precomputed enrichments. Returns dict keyed by (accession, item)."""
    if not accessions:
        return {}
    placeholders = ",".join("?" * len(accessions))
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT accession, item, weight_override, suppressed, "
            f"matched_phrases, adjustment_note, suppression_note "
            f"FROM enrichments WHERE accession IN ({placeholders})",
            accessions,
        ).fetchall()
    out: dict[tuple[str, str], dict] = {}
    for acc, item, weight_override, suppressed, mp, an, sn in rows:
        out[(acc, item)] = {
            "weight_override":  weight_override,
            "suppressed":       bool(suppressed),
            "matched_phrases":  _json.loads(mp) if mp else [],
            "adjustment_note":  an,
            "suppression_note": sn,
        }
    return out


def upsert_enrichment(
    accession: str,
    item: str,
    weight_override: float | None,
    suppressed: bool,
    matched_phrases: list[str] | None,
    adjustment_note: str | None,
    suppression_note: str | None,
    path: Path = DB_PATH,
) -> None:
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO enrichments
                (accession, item, weight_override, suppressed,
                 matched_phrases, adjustment_note, suppression_note, enriched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(accession, item) DO UPDATE SET
                weight_override = excluded.weight_override,
                suppressed = excluded.suppressed,
                matched_phrases = excluded.matched_phrases,
                adjustment_note = excluded.adjustment_note,
                suppression_note = excluded.suppression_note,
                enriched_at = excluded.enriched_at
            """,
            (
                accession, item, weight_override, int(bool(suppressed)),
                _json.dumps(matched_phrases) if matched_phrases else None,
                adjustment_note, suppression_note,
            ),
        )


def get_entity(cik: str, path: Path = DB_PATH) -> tuple[str | None, str | None] | None:
    """Return cached (sic, sic_description) for a CIK, or None if not cached.

    A row with NULL SIC is still a cache hit — returns (None, None).
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT sic, sic_description FROM entities WHERE cik = ?",
            (cik,),
        ).fetchone()
    return row if row is not None else None


def get_entity_tickers(cik: str, path: Path = DB_PATH) -> list[str] | None:
    """Return cached tickers for a CIK, or None if the row is missing OR
    has never had tickers populated."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT tickers FROM entities WHERE cik = ?", (cik,),
        ).fetchone()
    if row is None or row[0] in (None, ""):
        return None
    return [t for t in row[0].split(",") if t]


def upsert_entity(
    cik: str,
    sic: str | None,
    sic_description: str | None,
    tickers: list[str] | None = None,
    path: Path = DB_PATH,
) -> None:
    tickers_str = ",".join(tickers) if tickers else None
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO entities (cik, sic, sic_description, tickers, fetched_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(cik) DO UPDATE SET
                sic = excluded.sic,
                sic_description = excluded.sic_description,
                tickers = COALESCE(excluded.tickers, entities.tickers),
                fetched_at = excluded.fetched_at
            """,
            (cik, sic, sic_description, tickers_str),
        )
