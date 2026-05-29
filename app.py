"""Streamlit GUI for the Credit Screener.

Launch from the project folder:
    .venv/bin/streamlit run app.py

Two surfaces per issuer:
  - Default (always visible): severity badge, name + ticker + CIK, one-line
    plain-English credit summary, EDGAR filing link(s).
  - Audit detail (one click deeper): the existing decomposition table,
    score arithmetic, distinct-heavy count, Layer 2 escalation arrow, and
    any CONFLICT / SUPPRESSED / mixed flags.

Sidebar carries settings (as-of, window, display limit) and an ad-hoc
ingest button. Pipeline stats live in a collapsed System panel at the
bottom — the headline is "Distress Board," not engineering output.
"""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from db import connect, init_db
from enrich import enrich_results
from items import fetch_entity_tickers
from presentation import (
    humanize_adjustment_note,
    humanize_phrase,
    humanize_suppression_note,
    label_item,
    severity,
    summary,
)
from scoring import WINDOW_DAYS, ScoredIssuer, score_filings
from universe import filter_reason


# ---------- Page setup ------------------------------------------------------

st.set_page_config(
    page_title="Distress Board",
    layout="wide",
    initial_sidebar_state="expanded",
)


SEVERITY_BADGE: dict[str, str] = {
    "High":     ":red-background[**HIGH**]",
    "Elevated": ":orange-background[**ELEVATED**]",
    "Watch":    ":gray-background[Watch]",
}


def _latest_business_day(today: date) -> date:
    """Most recent weekday strictly before `today`. (No federal-holiday
    awareness — SEC publishes nothing on holidays either, which the auto-
    pull handles via IndexNotPublished.)"""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _autopull_if_missing(target: date) -> tuple[bool, str]:
    """If target_date isn't in the DB, pull it from SEC. Returns
    (did_pull, status_message)."""
    with connect() as conn:
        present = conn.execute(
            "SELECT 1 FROM filings WHERE filing_date = ? LIMIT 1",
            (target.isoformat(),),
        ).fetchone()
    if present:
        return False, ""
    from daily_index import IndexNotPublished
    from main import run_for_date
    try:
        _kept, inserted = run_for_date(target)
    except IndexNotPublished:
        return False, (f"SEC hasn't published the daily index for "
                       f"{target:%b %-d, %Y} yet — try again later in the day.")
    except Exception as e:
        return False, f"Couldn't pull {target:%b %-d, %Y}: {e}"
    return True, f"Pulled {target:%b %-d, %Y} from SEC — {inserted} new filings added."


def _recent_business_days(n: int) -> list[date]:
    """Return the last `n` business days, latest first."""
    out: list[date] = []
    d = _latest_business_day(date.today())
    while len(out) < n:
        out.append(d)
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return out


def _run_backfill(n: int) -> None:
    """Pull the most recent N business days that aren't already in the DB.
    Renders live progress in the current Streamlit container."""
    from daily_index import IndexNotPublished
    from main import run_for_date

    targets = _recent_business_days(n)
    with connect() as conn:
        present = {
            row[0]
            for row in conn.execute("SELECT DISTINCT filing_date FROM filings")
        }
    missing = [d for d in targets if d.isoformat() not in present]

    if not missing:
        st.info(
            f"All {n} most-recent business days are already in the local index. "
            "Nothing to pull."
        )
        return

    with st.status(
        f"Backfilling {len(missing)} business day(s) from SEC EDGAR — "
        f"roughly 30–60 seconds per day...",
        expanded=True,
    ) as status:
        progress = st.progress(0.0)
        total_inserted = 0
        skipped: list[str] = []
        for i, d in enumerate(missing):
            try:
                _kept, inserted = run_for_date(d)
                total_inserted += inserted
                st.write(f"✓ {d:%b %-d, %Y}: {inserted} new filings")
            except IndexNotPublished:
                st.write(f"⚠ {d:%b %-d, %Y}: SEC hasn't published yet, skipped")
                skipped.append(f"{d:%b %-d}")
            except Exception as e:
                st.write(f"✗ {d:%b %-d, %Y}: {e}")
                skipped.append(f"{d:%b %-d}")
            progress.progress((i + 1) / len(missing))
        tail = (f" · skipped {len(skipped)} day(s)" if skipped else "")
        status.update(
            label=f"Backfill complete — {total_inserted} new filings added "
                  f"across {len(missing) - len(skipped)} day(s){tail}.",
            state="complete",
        )

    load_window.clear()


# ---------- Helpers ---------------------------------------------------------

@st.cache_data(ttl=120, show_spinner=False)
def load_window(asof_iso: str, window_days: int):
    """Pull 8-K rows in the window from SQLite. Cached per (asof, window).

    Returns (scoring_rows, primary_doc_lookup) where primary_doc_lookup
    maps accession → primary document filename (None if missing in DB).
    """
    asof_d = date.fromisoformat(asof_iso)
    start = (asof_d - timedelta(days=window_days - 1)).isoformat()
    end = asof_d.isoformat()
    with connect() as conn:
        raw = list(conn.execute(
            "SELECT cik, company, items, filing_date, accession, primary_document "
            "FROM filings "
            "WHERE form_type='8-K' AND items IS NOT NULL "
            "AND filing_date BETWEEN ? AND ?",
            (start, end),
        ))
    rows = [(c, co, i, fd, a) for c, co, i, fd, a, _ in raw]
    primary_docs = {a: pd for *_, a, pd in raw}
    return rows, primary_docs


def _humanize_exclusion_reason(reason: str) -> str:
    """Translate the universe-filter tag into something an outsider can read."""
    if reason.startswith("SIC 6770"):
        return "blank-check company (SIC industry code 6770)"
    if "name pattern" in reason.lower():
        return "name ends in 'Acquisition Corp' — typical blank-check SPAC name"
    return reason


def _ticker_str(cik: str) -> str:
    """Return ' (TICK)' or ' (TICK1, TICK2)' if cached/available, else ''."""
    try:
        tickers = fetch_entity_tickers(cik)
    except Exception:
        return ""
    return f" ({', '.join(tickers)})" if tickers else ""


def _filing_render_url(cik: str, accession: str, primary_doc: str | None) -> str:
    """One-click URL to the rendered SEC filing.

    Prefers the primary document (the actual 8-K HTML body) so the user
    lands on the readable filing directly. Falls back to the directory
    listing if we don't know the primary document filename — that page
    lists every exhibit as a clickable link.
    """
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    return f"{base}/{primary_doc}" if primary_doc else f"{base}/"


def _short_date(iso: str) -> str:
    # 2026-05-28 → 5/28
    _, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}"


def _filing_links(r: ScoredIssuer, primary_docs: dict[str, str | None]) -> str:
    """One link per distinct contributing filing.

    Single filing  → '↗ EDGAR'.
    Multiple files → '↗ EDGAR  5/27  5/28' with each date clickable to its
    own filing — so the reader can see which link goes where.
    """
    seen: dict[str, str] = {}  # accession → filing_date
    for c in r.contributions:
        if c.accession and c.accession not in seen:
            seen[c.accession] = c.date or ""
    if not seen:
        return ""
    if len(seen) == 1:
        acc = next(iter(seen))
        url = _filing_render_url(r.cik, acc, primary_docs.get(acc))
        return f"[↗ EDGAR]({url})"
    parts = ["↗ EDGAR"] + [
        f"[{_short_date(d)}]({_filing_render_url(r.cik, acc, primary_docs.get(acc))})"
        for acc, d in seen.items()
    ]
    return "  ".join(parts)


def _filing_dates_str(r: ScoredIssuer) -> str:
    """Distinct filing dates that contributed to this issuer's score."""
    dates = sorted({c.date for c in r.contributions if c.date})
    if not dates:
        return ""
    if len(dates) == 1:
        return f"filed {dates[0]}"
    return "filed " + ", ".join(dates)


def render_issuer(
    i: int,
    r: ScoredIssuer,
    primary_docs: dict[str, str | None],
    tag: str | None = None,
) -> None:
    """Default + audit view for one issuer, as a bordered container."""
    sev = severity(r)
    badge = SEVERITY_BADGE.get(sev, sev)
    ticker = _ticker_str(r.cik)
    links = _filing_links(r, primary_docs)
    dates = _filing_dates_str(r)
    summary_text = summary(r)

    with st.container(border=True):
        c_main, c_link = st.columns([12, 1])
        with c_main:
            head = (
                f"**#{i}**  ·  {badge}  ·  **{r.company}**{ticker}  "
                f"·  CIK `{r.cik}`"
            )
            if dates:
                head += f"  ·  {dates}"
            if tag:
                head += f"  ·  *{tag}*"
            st.markdown(head)
            st.markdown(summary_text)
        with c_link:
            if links:
                st.markdown(links)

        with st.expander("Why this is on the board (evidence & calculation)", expanded=False):
            pre = getattr(r, "pre_enrich_score", None)
            if pre is not None and abs(pre - r.score) > 1e-9:
                direction = "raised" if r.score > pre else "lowered"
                st.caption(
                    f"After reading the filing text, the severity score was {direction} "
                    f"from **{pre:.1f}** to **{r.score:.1f}**."
                )

            contrib = [c for c in r.contributions if not c.suppressed]
            eq = " + ".join(f"{c.weight:g}" for c in contrib) if contrib else "0"
            multi_explainer = (
                f"×{r.multiplier:g} (boosted because {r.distinct_heavy} distinct "
                f"high-severity event types appear together)"
                if r.multiplier > 1.0 else "no multiplier (only one high-severity event type)"
            )
            st.markdown(
                f"**Severity score: {r.score:.1f}**  &nbsp;·&nbsp;  "
                f"sum of event weights = {eq}  &nbsp;·&nbsp;  multiplier: {multi_explainer}"
            )
            st.markdown(
                "Below: each SEC 8-K filing event that contributed, the weight it "
                "carries, and what the underlying filing text said about it."
            )
            st.dataframe(
                [
                    {
                        "Filing event":
                            label_item(c.item),
                        "Filed on":
                            c.date,
                        "Severity weight":
                            "0 (suppressed)" if c.suppressed else f"+{c.weight:g}",
                        "Filing-text review":
                            humanize_adjustment_note(c.adjustment_note),
                        "Why kept / suppressed":
                            humanize_suppression_note(c.note),
                        "Phrases found in filing text":
                            ", ".join(humanize_phrase(p) for p in c.matched_phrases)
                            if c.matched_phrases else "",
                    }
                    for c in r.contributions
                ],
                hide_index=True,
                use_container_width=True,
            )


# ---------- Auto-pull on open ----------------------------------------------
# Make sure the entities + filings schemas exist, then once per session
# top up the local DB with the most recent business day's filings.
init_db()
_TARGET_DATE = _latest_business_day(date.today())
_AUTOPULL_KEY = f"_autopull_{_TARGET_DATE.isoformat()}"
if _AUTOPULL_KEY not in st.session_state:
    with st.spinner(
        f"Checking SEC for {_TARGET_DATE:%b %-d, %Y}'s filings — first time today..."
    ):
        st.session_state[_AUTOPULL_KEY] = _autopull_if_missing(_TARGET_DATE)
    if st.session_state[_AUTOPULL_KEY][0]:
        load_window.clear()  # invalidate cached query so new rows appear
_autopull_did, _autopull_msg = st.session_state[_AUTOPULL_KEY]


# ---------- Sidebar ---------------------------------------------------------

with st.sidebar:
    st.header("Settings")
    asof = st.date_input("As-of date", value=_TARGET_DATE)
    window = int(st.number_input(
        "Lookback window (days)", min_value=1, max_value=365, value=WINDOW_DAYS,
        help="How many days back to scan for SEC 8-K filings.",
    ))
    limit = int(st.number_input(
        "Issuers shown on the board", min_value=5, max_value=200, value=25,
        help="How many issuers to display. The same set get their filing text read.",
    ))

    st.divider()
    st.subheader("Pull a day's filings from SEC")
    ingest_date = st.date_input("Filing date", value=asof, key="ingest_date")
    if st.button("Fetch from SEC EDGAR", use_container_width=True):
        from daily_index import IndexNotPublished
        from main import run_for_date
        with st.spinner(f"Pulling {ingest_date} from EDGAR..."):
            try:
                kept_n, inserted_n = run_for_date(ingest_date)
                st.success(
                    f"{ingest_date}: {kept_n} relevant filings found, "
                    f"{inserted_n} newly added to the local index."
                )
                load_window.clear()
            except IndexNotPublished:
                st.warning(
                    f"{ingest_date}: SEC hasn't published its daily index yet "
                    f"(try later in the day or pick an earlier date)."
                )
            except Exception as e:
                st.error(f"Failed: {e}")

    st.divider()
    st.subheader("Backfill historical filings")
    backfill_n = int(st.number_input(
        "Business days to backfill",
        min_value=1, max_value=90, value=30,
        help="Pulls each business day from yesterday backward, skipping days "
             "already in the local index. Roughly 30–60 seconds per day.",
    ))
    if st.button(
        f"Backfill last {backfill_n} business days",
        use_container_width=True,
        key="backfill_btn",
    ):
        st.session_state["_pending_backfill_n"] = backfill_n

    st.divider()
    st.caption(
        "These rankings are a starting heuristic, not a verified credit score. "
        "Always read the underlying filings before acting on a name."
    )


# ---------- Main page -------------------------------------------------------

st.title("Distress Board")

# Honest caption — show the *effective* range (intersection of lookback
# window and locally-available filings), and surface the gap if any.
with connect() as conn:
    _min_iso, _max_iso = conn.execute(
        "SELECT MIN(filing_date), MAX(filing_date) FROM filings"
    ).fetchone()
if _min_iso and _max_iso:
    _window_start_iso = (asof - timedelta(days=window - 1)).isoformat()
    _eff_start = max(_min_iso, _window_start_iso)
    _eff_end = min(_max_iso, asof.isoformat())
    _gap_note = (
        f"Lookback set to **{window} days** through {asof}, but bounded by "
        f"what's in the local index (filings from {_min_iso} to {_max_iso}). "
        f"Pull earlier dates via the sidebar to extend the historical view."
        if _window_start_iso < _min_iso
        else f"Lookback window: **{window} days** through {asof}."
    )
    st.caption(
        f"Showing SEC 8-K filings filed between **{_eff_start}** and "
        f"**{_eff_end}**. {_gap_note}"
    )
else:
    st.caption(
        "No filings in the local index yet — pull a date via the sidebar."
    )

if _autopull_did:
    st.success(_autopull_msg)
elif _autopull_msg:
    st.warning(_autopull_msg)

# If the user clicked "Backfill" in the sidebar this run, render the
# live progress here (in the main area, where there's room for it).
if "_pending_backfill_n" in st.session_state:
    _run_backfill(st.session_state.pop("_pending_backfill_n"))

tab_lb, tab_iss = st.tabs(["Distress Board", "New Debt Issuance Feed"])


# ---------- Leaderboard tab -------------------------------------------------

with tab_lb:
    rows, primary_docs = load_window(asof.isoformat(), window)
    # Layer 2 fetches still need raw .txt URLs (the SGML submission contains
    # all exhibits inline) — build that lookup on the side from accession+cik.
    url_lookup = {
        a: f"https://www.sec.gov/Archives/edgar/data/{int(c)}/{a}.txt"
        for c, _, _, _, a in rows
    }
    results = score_filings(rows, asof=asof, window_days=window)

    kept: list[ScoredIssuer] = []
    demoted: list[tuple[ScoredIssuer, str]] = []
    for r in results:
        reason = filter_reason(r.cik, r.company)
        if reason is None:
            kept.append(r)
        else:
            demoted.append((r, reason))

    if kept:
        with st.spinner("Reading the underlying filing text for each issuer..."):
            enrich_results(kept[:limit], url_lookup)
        kept.sort(key=lambda r: r.score, reverse=True)

    if not kept:
        st.info("Nothing on the board for this date and window.")
    else:
        for i, r in enumerate(kept[:limit], 1):
            render_issuer(i, r, primary_docs)

    if demoted:
        with st.expander(
            f"Excluded — blank-check SPACs and shells ({len(demoted)} issuers)",
            expanded=False,
        ):
            st.caption(
                "Companies whose only business is acquiring other companies "
                "(blank-check SPACs / shells). These can't be credits, so they're "
                "kept out of the main board. Shown here so you can spot-check the filter."
            )
            for i, (r, reason) in enumerate(demoted[:limit], 1):
                render_issuer(i, r, primary_docs, tag=_humanize_exclusion_reason(reason))

    # --- Pipeline stats (bottom, collapsed) ---
    st.divider()
    with st.expander("Pipeline stats", expanded=False):
        with connect() as conn:
            n_filings = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
            n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            date_range = conn.execute(
                "SELECT MIN(filing_date), MAX(filing_date) FROM filings"
            ).fetchone()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Issuers with a signal", len(results))
        c2.metric("On the board", len(kept))
        c3.metric("Excluded (shell / SPAC)", len(demoted))
        c4.metric("Lookback", f"{window} days")
        st.write(
            f"Local index: **{n_filings:,}** filings stored · "
            f"**{n_entities:,}** companies with metadata cached · "
            f"covering `{date_range[0]} → {date_range[1]}`."
        )


# ---------- Issuance feed tab -----------------------------------------------

with tab_iss:
    st.subheader("New Debt Issuance Feed")
    st.caption(
        "Every SEC 8-K filing that reported a new debt obligation, most recent first. "
        "Unranked — this is routine capital-structure surveillance for any "
        "credit person to skim, regardless of where the issuer ranks on the "
        "Distress Board."
    )

    c1, c2 = st.columns([1, 1])
    since = c1.date_input("Show filings from this date onward (optional)",
                          value=None, key="iss_since")
    iss_limit = int(c2.number_input(
        "Maximum rows to display", min_value=10, max_value=2000, value=200, key="iss_limit"
    ))

    sql = (
        "SELECT filing_date, cik, company, filing_url FROM filings "
        "WHERE form_type='8-K' AND (',' || items || ',') LIKE '%,2.03,%'"
    )
    params: list = []
    if since:
        sql += " AND filing_date >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY filing_date DESC, company LIMIT ?"
    params.append(iss_limit)

    with connect() as conn:
        iss_rows = list(conn.execute(sql, params))

    if not iss_rows:
        st.info("No new-debt filings match these filters.")
    else:
        st.write(f"{len(iss_rows)} filing(s)")
        st.dataframe(
            [
                {"Filed on": fd, "CIK": cik, "Company": co, "Open filing": url}
                for fd, cik, co, url in iss_rows
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Open filing": st.column_config.LinkColumn(display_text="open"),
            },
        )
