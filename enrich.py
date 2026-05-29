"""Layer 2: full-text enrichment over filings that scored onto the leaderboard.

Spec lives in CLAUDE.md "Scoring & Outputs (Layer 1)" + the Layer 2 brief.

Two key principles for 2.03 escalation:
  1. Strong standalone phrases (rare + specific) escalate on presence alone:
     super-priority, priming, stockholders' deficit, forbearance, exit fee,
     plus the compound "alternate conversion price" + "% of closing price"
     formula (toxic-convert signature). These don't go through context check.
  2. Everything else (default interest, alternate conv price alone, event of
     default ~ conversion price/rate, secured by + pledge) is context-checked
     in a ~100-char window: definitional context (Article I, "shall mean",
     etc.) suppresses; active context ("has asserted", "notice of default",
     dated event) escalates with high confidence; neither marker present →
     keep but flag as low-confidence in audit detail.

Boilerplate-vs-active is the failure mode this guards against. Every credit
agreement defines default interest; that phrase alone means nothing.

Audit detail keeps ALL matches (including suppressed definitional ones) so
the classifier verdict is visible — auditability beats brevity.

Job 2 (3.01) is unchanged here: transaction language suppresses, distress
language is recorded as confirmation. Curated keyword + regex only — NO
NLP / embeddings / LLM.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from pathlib import Path

from db import get_enrichments, upsert_enrichment
from edgar import get
from scoring import ScoredIssuer, recompute

CACHE_DIR = Path(__file__).parent / ".cache" / "text"

ESCALATED_2_03_WEIGHT: float = 10.0
PROXIMITY_DISTANCE: int = 200      # chars — for proximity-pair escalators
CONTEXT_WINDOW_CHARS: int = 100    # half-width around match for context check
ALT_CONV_FORMULA_WINDOW: int = 300 # alt-conv-price ↔ %-formula pairing window
NEGATION_LOOKBACK: int = 20        # chars — last token before the match


# --- Strong standalone escalators (no context check) -----------------------
# Rare + specific enough that any non-negated mention is significant.
STRONG_STANDALONE_ESCALATORS: list[tuple[str, re.Pattern]] = [
    ("super-priority",        re.compile(r"\bsuper[\s\-]?priorit\w*", re.I)),
    ("priming",               re.compile(r"\bpriming\b", re.I)),
    ("stockholders' deficit", re.compile(r"stockholders[’'‘']\s+deficit", re.I)),
    # Forbearance agreements only exist when there's an active default
    ("forbearance",           re.compile(r"\bforbearance\b", re.I)),
    # Exit fee is rescue-finance-specific
    ("exit fee",              re.compile(r"\bexit\s+fee", re.I)),
]


# --- Context-checked escalators --------------------------------------------
# These appear in every credit agreement as definitions — must verify
# active context before treating as a distress signal.
CONTEXT_ESCALATORS: list[tuple[str, re.Pattern]] = [
    ("default interest",          re.compile(r"\bdefault\s+interest\b", re.I)),
    ("alternate conversion price",
        re.compile(r"alternat\w+\s+conversion\s+price", re.I)),
    ("% of the closing price",
        re.compile(r"%\s+of\s+the\s+(?:lowest\s+|volume[\s\-]?weighted\s+)?"
                   r"(?:average\s+)?closing\s+price", re.I)),
]

# Conversion side is restricted to "conversion price/rate/ratio/security"
# so generic discussion of conversion doesn't drag this in.
CONTEXT_PROXIMITY: list[tuple[str, re.Pattern, re.Pattern]] = [
    ("event of default ~ conversion price/rate",
        re.compile(r"event\s+of\s+default", re.I),
        re.compile(r"convers(?:ion|ible)\s+(?:price|rate|ratio|securit)", re.I)),
    ("secured by + pledge / membership interests",
        re.compile(r"\bsecured\s+by\b", re.I),
        re.compile(r"\b(pledge|membership\s+interests?)\b", re.I)),
]


# --- Compound (alt-conv-price + % formula) ---------------------------------
_ALT_CONV_PAT = re.compile(r"alternat\w+\s+conversion\s+price", re.I)
_PCT_FORMULA_PAT = re.compile(
    r"%\s+of\s+the\s+(?:lowest\s+|volume[\s\-]?weighted\s+)?"
    r"(?:average\s+)?closing\s+price", re.I,
)


# --- Definitional vs active context markers --------------------------------
# Definitional context: typical legal-text scaffolding around defined terms.
DEFINITIONAL_MARKERS = re.compile(
    r"\bshall\s+mean\b"
    r"|\bis\s+defined\s+as\b"
    r"|\b(?:means|meaning)\b"
    r"|\bArticle\s+[IVXLCDM]+\b"
    r"|\bArticle\s+\d+\b"
    r"|\bDefinitions?\b"
    r"|\bDefined\s+Terms?\b",
    re.I,
)

# Active context: live event language, demands, notices, dated assertions.
ACTIVE_MARKERS = re.compile(
    r"\bhas\s+(?:asserted|notified|demanded|delivered\s+(?:a\s+)?notice|occurred)\b"
    r"|\b(?:is|are)\s+currently\s+in\s+default\b"
    r"|\b(?:event\s+of\s+)?default\s+has\s+occurred\b"
    r"|\bnotice\s+of\s+default\b"
    r"|\bdemand\s+for\s+payment\b"
    r"|\bforbearance(?:\s+agreement)?\b"
    r"|\bexit\s+fee\b"
    r"|\bas\s+of\s+\w+\s+\d{1,2},?\s+\d{4}\b",  # "as of May 26, 2026"
    re.I,
)


# --- De-escalators (unchanged) ---------------------------------------------
DE_ESCALATOR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("senior notes due",  re.compile(r"senior\s+notes?\s+due", re.I)),
    ("investment grade",  re.compile(r"investment[\s\-]+grade", re.I)),
    ("credit rating",
        re.compile(r"\b(?:rated\s+by|Moody|Standard\s*&\s*Poor|"
                   r"S&P\s+Global|Fitch|DBRS)", re.I)),
]

REVOLVER_PATTERN = re.compile(r"revolving\s+credit\s+facility", re.I)
DISTRESS_NEARBY = re.compile(
    r"\b(default|forbearance|breach|amendment|waiver|"
    r"maturity\s+extension|going\s+concern)\b", re.I,
)


# --- "incremental" companion-only escalator --------------------------------
INCREMENTAL_PATTERN = re.compile(r"\bincremental\b", re.I)


# --- 3.01 matchers (unchanged) ---------------------------------------------
DISTRESS_3_01: list[tuple[str, re.Pattern]] = [
    ("stockholders' equity/deficit",
        re.compile(r"stockholders[’'‘']\s+(?:equity|deficit)", re.I)),
    ("minimum bid price",   re.compile(r"minimum\s+bid\s+price", re.I)),
    ("continued listing",   re.compile(r"continued\s+listing", re.I)),
    ("hearing panel",       re.compile(r"hearing\s+panel", re.I)),
    ("regain compliance",   re.compile(r"regain\s+compliance", re.I)),
]

TRANSACTION_3_01: list[tuple[str, re.Pattern]] = [
    ("merger",                re.compile(r"\bmerger\b", re.I)),
    ("business combination",  re.compile(r"business\s+combination", re.I)),
    ("going private",         re.compile(r"going\s+private", re.I)),
    ("deregistration",        re.compile(r"\bderegistration\b", re.I)),
]


# --- Fetch + clean ---------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_to_text(raw: str) -> str:
    """Strip SGML/HTML wrappers from a full EDGAR submission to plain text."""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return text


def _cache_path(accession: str) -> Path:
    safe = accession.replace("/", "_")
    return CACHE_DIR / f"{safe}.txt"


def fetch_filing_text(accession: str, filing_url: str) -> str | None:
    """Return cleaned text for the full submission. Cached per accession."""
    if not accession or not filing_url:
        return None
    cache = _cache_path(accession)
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    try:
        raw = get(filing_url, accept="text/plain").text
    except Exception:
        return None
    text = strip_to_text(raw)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


# --- Matching primitives ---------------------------------------------------

_PREV_TOKEN_RE = re.compile(r"(\S+)\s*$")


def _is_negated(text: str, match_start: int) -> bool:
    """Crude negation: previous token is 'no' or 'not'."""
    prefix = text[max(0, match_start - NEGATION_LOOKBACK):match_start]
    m = _PREV_TOKEN_RE.search(prefix)
    if not m:
        return False
    return m.group(1).rstrip(".,;:()'\"").lower() in ("no", "not")


def _classify_context(text: str, match_start: int, match_end: int) -> str:
    """Return 'active', 'definitional', or 'ambiguous'.

    Active wins over definitional when both markers are present in the
    window — a live event reference trumps the surrounding defined-term
    scaffolding.
    """
    start = max(0, match_start - CONTEXT_WINDOW_CHARS)
    end = min(len(text), match_end + CONTEXT_WINDOW_CHARS)
    window = text[start:end]
    if ACTIVE_MARKERS.search(window):
        return "active"
    if DEFINITIONAL_MARKERS.search(window):
        return "definitional"
    return "ambiguous"


def _find_standalone(text: str, patterns) -> list[str]:
    """First-hit-per-label phrase match with negation filter."""
    found = []
    for label, pat in patterns:
        for m in pat.finditer(text):
            if _is_negated(text, m.start()):
                continue
            found.append(label)
            break
    return found


def _find_with_context(text: str, patterns) -> list[tuple[str, str]]:
    """Return (label, classification) for first-hit-per-label."""
    found = []
    for label, pat in patterns:
        for m in pat.finditer(text):
            if _is_negated(text, m.start()):
                continue
            cls = _classify_context(text, m.start(), m.end())
            found.append((label, cls))
            break
    return found


def _find_proximity_with_context(
    text: str,
    pairs,
    max_distance: int = PROXIMITY_DISTANCE,
) -> list[tuple[str, str]]:
    """Proximity matcher that classifies the matched region for context."""
    found = []
    for label, pat_a, pat_b in pairs:
        a_iter = [m for m in pat_a.finditer(text) if not _is_negated(text, m.start())]
        if not a_iter:
            continue
        b_iter = [m for m in pat_b.finditer(text) if not _is_negated(text, m.start())]
        if not b_iter:
            continue
        hit = False
        for a in a_iter:
            if hit:
                break
            for b in b_iter:
                if abs(a.start() - b.start()) <= max_distance:
                    start = min(a.start(), b.start())
                    end = max(a.end(), b.end())
                    cls = _classify_context(text, start, end)
                    found.append((label, cls))
                    hit = True
                    break
    return found


def _alt_conv_compound_fires(text: str) -> bool:
    """True if alt-conv-price and a %-of-closing-price formula are within
    ALT_CONV_FORMULA_WINDOW chars of each other — toxic-convert signature."""
    alt_pos = [m.start() for m in _ALT_CONV_PAT.finditer(text)]
    if not alt_pos:
        return False
    pct_pos = [m.start() for m in _PCT_FORMULA_PAT.finditer(text)]
    if not pct_pos:
        return False
    return any(abs(a - p) <= ALT_CONV_FORMULA_WINDOW for a in alt_pos for p in pct_pos)


def _label_with_class(label: str, cls: str) -> str:
    if cls == "active":
        return f"{label} [active]"
    if cls == "definitional":
        return f"{label} [definitional — suppressed]"
    if cls == "ambiguous":
        return f"{label} [low confidence]"
    return label


# --- Per-item enrichment logic --------------------------------------------

def evaluate_2_03(text: str) -> tuple[float | None, list[str], str | None]:
    """Return (new_weight | None, audit_labels, adjustment_note).

    audit_labels includes EVERY match found — strong ones plain, context-
    checked ones annotated with [active] / [low confidence] / [definitional
    — suppressed]. The decision logic only counts strong + non-definitional
    matches as effective escalators.
    """
    # 1. Strong standalone (no context check)
    strong = _find_standalone(text, STRONG_STANDALONE_ESCALATORS)
    alt_compound = _alt_conv_compound_fires(text)

    # 2. Context-checked simple + proximity
    ctx_simple = _find_with_context(text, CONTEXT_ESCALATORS)
    ctx_proximity = _find_proximity_with_context(text, CONTEXT_PROXIMITY)

    # --- Build audit labels (everything found, annotated) ---
    audit_labels: list[str] = list(strong)
    for label, cls in ctx_simple:
        if alt_compound and label in ("alternate conversion price", "% of the closing price"):
            # Compound promotes both members to strong-equivalent for audit
            audit_labels.append(f"{label} [active — compound w/ formula]")
        else:
            audit_labels.append(_label_with_class(label, cls))
    for label, cls in ctx_proximity:
        audit_labels.append(_label_with_class(label, cls))

    # --- Build effective escalators (drive the decision) ---
    # Only strong standalones + alt-compound + ACTIVE-classified context
    # drive a weight bump. Ambiguous-only matches stay visible in audit
    # but DO NOT escalate — principle: when in doubt, less confident, not
    # more. Definitional matches don't count (suppressed).
    effective: list[str] = list(strong)
    if alt_compound:
        effective.append("alternate conversion price + % formula")
    for label, cls in ctx_simple:
        if alt_compound and label in ("alternate conversion price", "% of the closing price"):
            continue  # already counted in compound
        if cls == "active":
            effective.append(label)
    for label, cls in ctx_proximity:
        if cls == "active":
            effective.append(label)

    # --- "incremental" companion-only — piggyback on STRONG/active only ---
    # Definition: any strong-standalone OR alt-compound OR ACTIVE-classified
    # context match. Ambiguous-only doesn't gate incremental.
    strong_or_active = set(strong)
    if alt_compound:
        strong_or_active.add("alt-compound")
    for label, cls in ctx_simple + ctx_proximity:
        if cls == "active":
            strong_or_active.add(label)
    if strong_or_active:
        for m in INCREMENTAL_PATTERN.finditer(text):
            if not _is_negated(text, m.start()):
                effective.append("incremental (companion to strong escalator)")
                audit_labels.append("incremental (companion to strong escalator)")
                break

    # --- De-escalators ---
    de_escalators = _find_standalone(text, DE_ESCALATOR_PATTERNS)
    if REVOLVER_PATTERN.search(text) and not DISTRESS_NEARBY.search(text):
        de_escalators.append("revolving credit facility (no distress nearby)")

    # --- Decide: de-escalator wins, else escalate, else no change ---
    if de_escalators:
        note = "DE-ESCALATED → routine issuance"
        if effective:
            note += " (escalator hits overruled)"
        return 0.0, audit_labels + de_escalators, note

    if effective:
        return ESCALATED_2_03_WEIGHT, audit_labels, "ESCALATED → toxic / rescue debt"

    # No active/strong escalators but there ARE context-checked matches —
    # call it out for the summary template to use the soft fallback.
    if any("[low confidence]" in lbl for lbl in audit_labels):
        return None, audit_labels, "ambiguous escalator signals (low confidence) — no weight change"

    # Nothing of interest, or only definitional matches suppressed.
    return None, audit_labels, None


def evaluate_3_01(text: str) -> tuple[bool, list[str], str | None]:
    """Return (suppress_as_transaction, matched_labels, note)."""
    transaction = _find_standalone(text, TRANSACTION_3_01)
    distress = _find_standalone(text, DISTRESS_3_01)

    if transaction and not distress:
        return True, transaction, "transaction language → suppress"
    if distress and transaction:
        return False, distress + transaction, "mixed — distress wins, kept"
    if distress:
        return False, distress, "distress language confirmed"
    return False, [], None


# --- Result helpers --------------------------------------------------------
# Live evaluators return tuples; we normalize into a dict that matches the
# DB enrichment row shape so the apply-step works identically whether the
# verdict came from the DB or a fresh computation.

def _live_2_03_enrichment(text: str) -> dict:
    new_w, phrases, note = evaluate_2_03(text)
    suppressed = new_w == 0.0
    return {
        "weight_override":  new_w,
        "suppressed":       bool(suppressed),
        "matched_phrases":  phrases or [],
        "adjustment_note":  note,
        "suppression_note": "de-escalated by full-text" if suppressed else None,
    }


def _live_3_01_enrichment(text: str) -> dict:
    suppress, phrases, note = evaluate_3_01(text)
    return {
        "weight_override":  0.0 if suppress else None,
        "suppressed":       bool(suppress),
        "matched_phrases":  phrases or [],
        "adjustment_note":  note,
        "suppression_note": "transaction language (Layer 2)" if suppress else None,
    }


def _apply_enrichment(c, e: dict) -> bool:
    """Apply an enrichment dict to a contribution. Returns True if anything
    score-relevant changed (weight bumped, suppressed, etc.)."""
    if e["matched_phrases"]:
        c.matched_phrases = list(e["matched_phrases"])
    if e["adjustment_note"]:
        c.adjustment_note = e["adjustment_note"]
    changed = False
    w_new = e["weight_override"]
    if w_new is not None and w_new != c.weight:
        c.weight = float(w_new)
        if e["suppressed"]:
            c.suppressed = True
            c.note = e["suppression_note"] or c.note
        changed = True
    elif e["suppressed"] and not c.suppressed:
        c.weight = 0.0
        c.suppressed = True
        c.note = e["suppression_note"] or c.note
        changed = True
    return changed


# --- Issuer-level orchestration -------------------------------------------

def enrich_issuer(
    issuer: ScoredIssuer,
    url_lookup: dict[str, str],
    cached: dict[tuple[str, str], dict] | None = None,
) -> bool:
    """Apply Layer 2 to one issuer (mutates in place).

    `cached` is a {(accession, item): enrichment_dict} map — when an entry
    is present we use it directly (no network, no text fetch). When missing,
    we fall back to live evaluation. Returns True if anything score-relevant
    changed.
    """
    cached = cached or {}
    by_accession: dict[str, list] = defaultdict(list)
    for c in issuer.contributions:
        if c.item in ("2.03", "3.01") and not c.suppressed:
            by_accession[c.accession].append(c)

    if not by_accession:
        return False

    pre_score = issuer.score
    changed = False

    for accession, contribs in by_accession.items():
        text: str | None = None  # lazy-loaded only if we have a cache miss

        for c in contribs:
            key = (accession, c.item)
            e = cached.get(key)
            if e is None:
                # Cache miss → live compute. Fetch the text once per filing,
                # then reuse for any other items on that accession.
                if text is None:
                    filing_url = url_lookup.get(accession)
                    if not filing_url:
                        continue
                    text = fetch_filing_text(accession, filing_url)
                    if not text:
                        continue
                if c.item == "2.03":
                    e = _live_2_03_enrichment(text)
                elif c.item == "3.01":
                    e = _live_3_01_enrichment(text)
                else:
                    continue
            if _apply_enrichment(c, e):
                changed = True

    if changed:
        recompute(issuer)
        issuer.pre_enrich_score = pre_score  # type: ignore[attr-defined]
    return changed


def enrich_results(
    results: list[ScoredIssuer],
    url_lookup: dict[str, str],
) -> None:
    """Enrich and re-sort. Bulk-loads precomputed enrichments from the DB
    in one query, then applies them per-issuer (no per-issuer DB hits)."""
    accessions = sorted({
        c.accession
        for r in results
        for c in r.contributions
        if c.accession and c.item in ("2.03", "3.01") and not c.suppressed
    })
    cached = get_enrichments(accessions) if accessions else {}
    for issuer in results:
        enrich_issuer(issuer, url_lookup, cached)
    results.sort(key=lambda r: r.score, reverse=True)
