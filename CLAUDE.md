# Credit & Special Situations Distress Screener

## What this is
A daily screener over SEC public filers that surfaces capital-structure
and distress signals to build a ranked watchlist of credits to investigate.
Goal: completeness over the public-filer universe, NOT speed-to-event.
We are not trying to beat Reorg/9fin to a trade; we are trying to
systematically never miss a public-filer signal.

## Hard constraints
- Public data sources only. No paid terminals/feeds.
- SEC EDGAR requires a User-Agent header on every request:
  format "Name email@example.com". Requests without it get blocked.
- SEC rate limit: max ~10 requests/second. Throttle accordingly.
- Zero-pad CIK to 10 digits for data.sec.gov endpoints (e.g. CIK0000320193).

## Architecture decisions (do not re-litigate)
- CIK is the entity resolution key. Every event resolves to a CIK.
  Note: one CIK = one filing entity, NOT necessarily one credit. Mapping
  CIK -> corporate family / ultimate parent is a later, separate layer.
- Output is a RANKED ISSUER LIST, not a flat event log. Score issuers on
  co-occurrence/sequence of events over a trailing window (e.g. 30-90 days).
- Build Layer 1 end-to-end before adding any other layer.

## Build layers (in order)
1. Form/item metadata poller (BUILD FIRST):
   - Source: EDGAR daily index + data.sec.gov submissions API
   - Filter on form type + 8-K item code:
     8-K items 1.01, 1.02, 2.03, 2.04, 3.01, 4.01, 5.02;
     NT 10-K, NT 10-Q (Form 12b-25); 25, 25-NSE (delisting)
   - Store in SQLite
2. Full-text search layer (DEFERRED): efts.sec.gov for prose signals
   ("unrestricted subsidiary", "going concern", "forbearance", etc.)
3. Non-SEC feeds (DEFERRED): MSRB EMMA, rating agency press, WARN notices

## Scoring & Outputs (Layer 1)

Two SEPARATE outputs over the same SQLite store. Same ingestion, same
item codes, same CIK resolution — these are just two read queries/views.
Do NOT overload one into the other.

## Universe filter (Layer 1)
- Demotes blank-check SPACs/shells out of the distress leaderboard into a
  "Filtered (shell/SPAC)" bucket. Flag-and-demote, never silent drop.
- Primary key: SIC 6770. Secondary: name pattern /acquisition corp/i (backup only).
- DEFERRED (Cut B, do NOT build yet): debt-existence / balance-sheet filter
  via XBRL companyfacts. Out of scope. WARNING when we do build it: a dollar
  threshold risks excluding small distressed names — DNA X's "debt" was a
  single $3M convert and is a true positive. Don't size-filter the small end out.

### Output 1: Distress leaderboard (ranked)
Ranked issuer list, descending by score. Scores ONLY the intersection of
(items on filing) ∩ (weighted target list below). All other items ignored.

Item weights (per occurrence, 0-10):
- 2.04  triggering event / acceleration / missed payment   = 10
- 3.01  delisting / listing-standard failure               = 8
- 4.01  auditor change                                      = 6
- 1.02  termination of material agreement                   = 5
- 1.01  entry into material agreement (amend/waiver lives here) = 3
- 2.03  new direct financial obligation                     = 3  (low: new debt usually healthy; see Output 2 + escalation)
- 5.02  officer departure                                   = 2

HARD EXCLUDE from scoring entirely (ride on everything / pure noise):
9.01, 2.02, 5.07, 7.01, 8.01

Co-occurrence / sequence (the actual signal):
- Per-issuer trailing window: 90 days (config constant — TUNE LATER)
- Base score = sum of matched item weights in window
- Distinct-signal-type multiplier (count of distinct items w/ weight >= 5):
  1 type x1.0, 2 types x1.4, 3+ types x1.8
- Repeat cap: same item type counts full weight once, half-weight per
  repeat (prevents a chatty filer topping the list)

Output row MUST be decomposable: CIK, company, score, AND the contributing
items with dates (e.g. "19.2: 4.01 on 5/12, 2.04 on 5/27, x1.4").
A score you can't decompose is a score you'll stop trusting.

### Output 2: New issuance feed (flat, unranked, always-on)
Every filing with item 2.03, most recent first. NOT ranked, NOT scored —
this is routine cap-structure surveillance, reviewed regardless of distress
score. Just: date, CIK, company, filing URL.

### 2.03 escalation (bridges the two)
A 2.03 lives in Output 2 by default (weight 3 in Output 1).
IF Layer 2 full-text later trips "super-priority" / "priming" /
"incremental" language on that filing, escalate it into the distress
score at HIGH weight — that's rescue financing, a different animal.
(Deferred until Layer 2 exists; note here so it isn't forgotten.)

### Validation status: UNVALIDATED HYPOTHESIS
These weights and the 90-day window are starting guesses, not validated.
The only real test is backtesting: did high-scoring names actually
default / restructure / get downgraded? Not possible yet (insufficient
history). Until then: store EVERYTHING, run daily, and MANUALLY review
what surfaces vs. what you'd flag yourself. Build to be tuned, not trusted.

## Stack
- Python, requests/httpx, SQLite, pandas for scoring.
- Keep it simple; no frameworks until justified.

## Current status
- Veris suppressor done, universe filter being built, Layer 2 (full-text on 2.03/3.01 bodies) is next
  
## Known Issues / Discovered Failure Modes
(Findings from manually reviewing real results. These are MY conclusions
from reading filings — keep them, don't let them get paraphrased away.)

- 2026-05-27: 1.02/3.01/5.02 single-date clusters are often take-private /
  merger closes, NOT distress. Confirmed: Veris Residential (taken private),
  ranked #1 at 21.0 — false positive. Fix: 2.01 co-presence suppressor
  (±3d of the 3.01), suppress-and-flag not drop. Do NOT down-weight 3.01 —
  weight is right, context was missing. Open question: take-privates that
  file 2.01 late or under a different CIK will slip the rule.
- The 3.01 false-positive taxonomy now has three named species (take-private / SPAC-liquidation / real-distress) that are identical at the item-code level, and 2.03 severity is invisible to metadata (DNA X's toxic convert — 10%/20% default, EoD conversion reset, opco-equity pledge, bridge rollup — scored the same +3 as a healthy refi would). That's the entry, because it's the thing that justifies Layer 2 being next and that future-you will otherwise forget.
- "Investment-grade routine refinancing flagged HIGH because 'default interest' matched in standard contractual provisions. Fix: matcher must distinguish active assertion from definitional/hypothetical language. Don't trust the summary template to invent narrative from boilerplate matches."