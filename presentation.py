"""Presentation helpers for the GUI.

severity()  → "High" / "Elevated" / "Watch" tag for a scored issuer.
summary()   → one-line plain-English credit description.

These exist so the default GUI surface reads like a credit note instead of
engineering output. Rule-based and deterministic — same debuggability
principle as Layer 2's keyword matchers. NO LLM calls.

Tunable: thresholds and theme wording are constants at the top of this
file. Treat them like Layer 1 weights — starting guesses, not validated.
"""
from __future__ import annotations

from scoring import ScoredIssuer

# --- Severity bands --------------------------------------------------------
HIGH_THRESHOLD: float = 20.0
ELEVATED_THRESHOLD: float = 10.0
ESCALATED_2_03_WEIGHT: float = 10.0  # mirror enrich.py — escalation override


def severity(issuer: ScoredIssuer) -> str:
    """Return 'High' / 'Elevated' / 'Watch'.

    Forced-High overrides (regardless of raw score):
      - any 2.04 (triggering event / acceleration) is live, OR
      - any 2.03 escalated by Layer 2 (weight bumped to 10).
    """
    contribs = [c for c in issuer.contributions if not c.suppressed]
    if any(c.item == "2.04" for c in contribs):
        return "High"
    if any(c.item == "2.03" and c.weight >= ESCALATED_2_03_WEIGHT for c in contribs):
        return "High"
    if issuer.score >= HIGH_THRESHOLD:
        return "High"
    if issuer.score >= ELEVATED_THRESHOLD:
        return "Elevated"
    return "Watch"


# --- Plain-English summary -------------------------------------------------

_ITEM_PLAIN: dict[str, str] = {
    "1.01": "new material agreement",
    "1.02": "material agreement terminated",
    "2.03": "new debt facility",
    "2.04": "debt acceleration / triggering event",
    "3.01": "listing-standard issue",
    "4.01": "auditor change",
    "5.02": "officer change",
}


# Plain-English labels shown alongside 8-K item codes in the audit table.
# Keep the code on the left so credit folks can map back to SEC docs; the
# tail is for anyone who doesn't have the item codes memorized.
ITEM_LABELS: dict[str, str] = {
    "1.01": "1.01 · New material agreement",
    "1.02": "1.02 · Material agreement terminated",
    "2.01": "2.01 · Acquisition or disposition completed",
    "2.03": "2.03 · New debt facility",
    "2.04": "2.04 · Debt acceleration / triggering event",
    "3.01": "3.01 · Listing-standard issue",
    "4.01": "4.01 · Auditor change",
    "5.02": "5.02 · Officer change",
}


def label_item(code: str) -> str:
    """Return '1.01 · New material agreement' for known codes, else the code."""
    return ITEM_LABELS.get(code, code)


# Translation of internal adjustment_note flags into reader-friendly text.
# We keep the source-of-truth strings short and machine-checkable; this
# function does the translation only at display time.
def humanize_adjustment_note(note: str | None) -> str:
    if not note:
        return ""
    n = note.lower()
    if note.startswith("ESCALATED"):
        return "Filing text shows toxic / rescue financing terms — severity raised"
    if note.startswith("DE-ESCALATED"):
        tail = " (other distress phrases in the same text were overruled)" \
            if "overruled" in n else ""
        return "Filing text reads as routine investment-grade issuance — severity removed" + tail
    if "ambiguous" in n:
        return ("Filing text mentions distress phrases but only in boilerplate / "
                "ambiguous context — severity kept at the base level")
    if "transaction language" in n and "suppress" in n:
        return "Filing text describes a transaction (merger / business combination), not distress"
    if n.startswith("mixed"):
        return "Filing text shows both distress and transaction language — distress signal kept"
    if "distress language confirmed" in n:
        return "Filing text confirms distress signals"
    return note


def humanize_suppression_note(note: str | None) -> str:
    """Translate Contribution.note (suppression reason) into reader-friendly text."""
    if not note:
        return ""
    n = note.lower()
    if "2.01 co-present" in n:
        return ("Suppressed — an acquisition / disposition was filed within ±3 days, "
                "almost always a transaction close, not distress.")
    if "de-escalated by full-text" in n:
        return "Suppressed — severity removed after reading the filing text."
    if "transaction language" in n:
        return "Suppressed — filing text describes a transaction, not distress."
    return note


# In-place rewrite of audit annotations on matched phrases. The internal
# tags '[active]' / '[low confidence]' / '[definitional — suppressed]' /
# '[active — compound w/ formula]' come out of enrich.py; this function
# replaces them with plain-English descriptions.
def humanize_phrase(phrase: str) -> str:
    return (phrase
            .replace("[active — compound w/ formula]",
                     "[live signal — confirmed by % formula]")
            .replace("[definitional — suppressed]",
                     "[boilerplate language — ignored]")
            .replace("[active]", "[live signal in filing]")
            .replace("[low confidence]", "[weak signal — context unclear]"))


def _has_phrase(c, *needles: str) -> bool:
    """True if any matched_phrase contains any needle as a substring.

    Tolerates Layer 2's annotation suffixes like '[active]'/'[low confidence]'
    so callers can search by core phrase.
    """
    return any(any(n in p for n in needles) for p in c.matched_phrases)


def _has_active_phrase(c, *needles: str) -> bool:
    """True if a matched_phrase contains any needle AND is classified active.

    "active" markers come from Layer 2's context classifier — they indicate
    the phrase appeared in present-event language (lender assertions, notice
    delivered, dated events), not definitional boilerplate.
    """
    for p in c.matched_phrases:
        if "[active]" not in p:
            continue
        if any(n in p for n in needles):
            return True
    return False


def summary(issuer: ScoredIssuer) -> str:
    """Return a one-line plain-English summary of what fired.

    Priority order, first matching theme wins (themes can compose where
    the spec examples show composition, e.g. toxic convert + listing
    deficit). If nothing matches, fall back to listing items in plain
    English.
    """
    contribs = issuer.contributions
    active = {c.item for c in contribs if not c.suppressed}

    has_2_04 = "2.04" in active

    # 2.03 Layer 2 state — three sub-cases:
    #   esc_2_03:      weight bumped to ESCALATED (strong + active signals)
    #   ambiguous_2_03: matches found but only ambiguous — weight unchanged
    #   de_esc_2_03:   suppressed to 0 (routine IG)
    esc_2_03 = next(
        (c for c in contribs if c.item == "2.03" and not c.suppressed
         and c.adjustment_note and c.adjustment_note.startswith("ESCALATED")),
        None,
    )
    ambiguous_2_03 = next(
        (c for c in contribs if c.item == "2.03" and not c.suppressed
         and c.adjustment_note and "ambiguous" in c.adjustment_note.lower()
         and not c.adjustment_note.startswith("ESCALATED")),
        None,
    )
    de_esc_2_03 = any(
        c.item == "2.03" and c.suppressed and c.adjustment_note
        and "DE-ESCALATED" in c.adjustment_note
        for c in contribs
    )

    # 3.01 state — distress confirmed vs suppressed by L1 (2.01) vs L2 transaction
    distress_3_01 = next(
        (c for c in contribs if c.item == "3.01" and not c.suppressed and c.matched_phrases),
        None,
    )
    l1_take_private = any(
        c.item == "3.01" and c.suppressed
        and c.note and "2.01" in c.note
        for c in contribs
    )
    l2_transaction = any(
        c.item == "3.01" and c.suppressed
        and c.note and "transaction" in c.note.lower()
        for c in contribs
    )

    # --- Priority 1: take-private (2.01 co-presence rule fired) ---
    if l1_take_private:
        bits: list[str] = []
        if "1.02" in active: bits.append("material agreement terminated")
        if "5.02" in active: bits.append("officer changes")
        if "1.01" in active: bits.append("new agreement entered")
        suffix = (" — " + ", ".join(bits)) if bits else ""
        return f"Likely take-private transaction (not distress){suffix}."

    # --- Priority 2: 3.01 suppressed by Layer 2 transaction language ---
    if l2_transaction and not distress_3_01:
        return "Likely merger / business-combination close (not distress)."

    # --- Priority 3: 2.04 acceleration (forced High by severity) ---
    if has_2_04:
        return "Triggering event / debt acceleration disclosed — possible event of default."

    # --- Priority 4: 2.03 escalated (toxic convert / rescue lender) ---
    if esc_2_03 is not None:
        # Pick wording from matched phrases — toxic-convert pattern vs
        # rescue-finance pattern vs secured-pledge. Anything below the
        # specific cases falls back to a generic-but-honest line.
        if (
            _has_phrase(esc_2_03, "alternate conversion price + % formula")
            or _has_phrase(esc_2_03, "compound w/ formula")
            or (_has_phrase(esc_2_03, "alternate conversion price")
                and _has_phrase(esc_2_03, "% of the closing price"))
        ):
            lead = "New convertible note with toxic conversion mechanics"
        elif (
            _has_phrase(esc_2_03, "forbearance", "exit fee", "notice of default")
            and (
                _has_active_phrase(esc_2_03, "default interest")
                or _has_phrase(esc_2_03, "forbearance", "exit fee", "notice of default")
            )
        ):
            # "Lender asserting" requires an active default-interest hit OR
            # a self-evidently-active strong escalator (forbearance / exit
            # fee). Plain definitional 'default interest' alone doesn't
            # earn this line.
            lead = "Lender asserting default interest / rescue-finance terms"
        elif _has_active_phrase(esc_2_03, "secured by + pledge", "membership interests"):
            lead = "Secured borrowing with subsidiary-equity pledge"
        else:
            lead = "New debt agreement — possible toxic / rescue terms (review filing)"

        if distress_3_01 is not None:
            return f"{lead}; Nasdaq listing-deficit appeal in process."
        return f"{lead}."

    # --- Priority 4.5: 2.03 with ambiguous (low-confidence) signals only ---
    # Soft fallback — don't invent a specific narrative from weak evidence.
    if ambiguous_2_03 is not None:
        if distress_3_01 is not None:
            return ("New debt agreement — context unclear; Nasdaq listing-deficit "
                    "issue also present, review filing.")
        return "New debt agreement — context unclear, review filing for severity."

    # --- Priority 5: routine IG issuance (2.03 de-escalated) ---
    if de_esc_2_03:
        return "Routine investment-grade debt issuance."

    # --- Priority 6: standalone listing-standard issue ---
    if distress_3_01 is not None and "2.03" not in active and not has_2_04:
        return "Nasdaq listing-deficit / continued-listing issue."

    # --- Priority 7: auditor change (4.01) ---
    if "4.01" in active:
        accompany: list[str] = []
        if "2.03" in active: accompany.append("new debt issued")
        if "1.02" in active: accompany.append("material agreement terminated")
        tail = f" — also {', '.join(accompany)}" if accompany else ""
        return f"Auditor change disclosed{tail}."

    # --- Fallback: list items in plain words ---
    plain = [_ITEM_PLAIN[i] for i in ("1.02", "1.01", "2.03", "3.01", "5.02") if i in active]
    if plain:
        head = plain[0][0].upper() + plain[0][1:]
        return head + (", " + ", ".join(plain[1:]) if len(plain) > 1 else "") + "."
    return "Weighted items reported — see audit detail."
