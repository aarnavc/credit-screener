# Credit & Special Situations Distress Screener

A daily SEC EDGAR screener that surfaces capital-structure and distress
signals across the US public-filer universe and builds a ranked
watchlist of credits to investigate.

The goal is **completeness over the public-filer universe**, not
speed-to-event. Designed to systematically never miss a public-filer
signal — not to beat Reorg / 9fin to a trade.

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Set your SEC contact (required by EDGAR)
export SEC_USER_AGENT="Your Name your@email.com"

# Pull a date
.venv/bin/python main.py 2026-05-28

# Launch the GUI
.venv/bin/streamlit run app.py
```

## Architecture

- **`main.py`** — Layer 1 ingestion. Pulls the EDGAR daily form-index,
  filters to target forms (8-K item codes, NT 10-K/Q, 25-NSE), persists
  to SQLite.
- **`scoring.py`** — Layer 1 distress scoring with item weights,
  90-day window, repeat cap, distinct-signal-type multiplier, and a
  ±3-day 2.01 co-presence suppressor for take-private false positives.
- **`enrich.py`** — Layer 2 full-text enrichment. Reads the underlying
  filing prose to confirm or downgrade 2.03 / 3.01 signals (toxic
  convert detection, IG-issuance de-escalation, transaction-language
  suppression). Curated keyword matching with definitional-vs-active
  context windows — no NLP / LLM.
- **`presentation.py`** — Severity bands (High / Elevated / Watch) and
  the rule-based plain-English summary template used by the GUI.
- **`universe.py`** — SPAC / shell filter (primary: SIC 6770; backup:
  name pattern). Demoted into a separate bucket, never silently dropped.
- **`app.py`** — Streamlit GUI. Auto-pulls yesterday's filings on first
  open. Sidebar has manual pull + N-day backfill.
- **`db.py`** — SQLite schema + helpers. PK is `(accession, cik)` so the
  same SEC filing tied to multiple CIKs (e.g. 25-NSEs that name both the
  exchange and the delisted issuer) is preserved.

## Hard constraints

- Public data sources only. No paid terminals / feeds.
- SEC EDGAR requires a `User-Agent` header on every request, in the form
  `"Name email@example.com"`. Set via `SEC_USER_AGENT` env var; without
  it the client falls back to a documented default.
- SEC rate limit is 10 req/sec; client throttles at 8.

## Cloud deployment

This repo deploys to **Streamlit Community Cloud** for free:

1. Fork / push this repo to GitHub.
2. At https://streamlit.io/cloud, click "New app", pick this repo, set
   `app.py` as the entry point.
3. Add a secret: `SEC_USER_AGENT = "Your Name your@email.com"`.
4. (Optional) The included GitHub Actions workflow at
   `.github/workflows/daily-pull.yml` runs daily and commits the
   updated SQLite back to the repo, which triggers a Streamlit redeploy
   with the latest data.

## Status

This is an **unvalidated hypothesis**, not a verified credit score.
Scoring weights and the 90-day window are starting guesses. Always read
the underlying filings before acting on a name.
