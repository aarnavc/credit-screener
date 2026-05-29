"""Universe filter for the distress leaderboard.

Demote blank-check SPACs and shells out of the main ranking. This is a
NOISE FILTER (things that can't be credits), not a precision filter — be
conservative, prefer under-filtering over killing real companies. Demoted
issuers go to a separate "Filtered (shell/SPAC)" bucket; never silently
dropped.

Primary signal: SIC code 6770 (Blank Checks) from the data.sec.gov
submissions API — trusted key.

Secondary signal: company name ending in "Acquisition Corp[oration]" —
anchored to end-of-name to minimize false positives. Use only as a
backup for SPACs that don't carry the 6770 code.

Out of scope (deferred — Cut B): debt-existence / balance-sheet checks.
Do NOT introduce one here.
"""
from __future__ import annotations

import re

from items import fetch_entity_meta

BLANK_CHECK_SIC: str = "6770"
_NAME_PATTERN = re.compile(r"acquisition corp(?:oration)?\.?$", re.IGNORECASE)


def filter_reason(cik: str, company: str) -> str | None:
    """Return a demotion reason string, or None if the issuer passes.

    Network call: fetches the CIK's submissions blob (cached in-process).
    On lookup failure, falls through to the name pattern only — don't
    fail closed on a network blip.
    """
    sic, sic_desc = None, None
    try:
        sic, sic_desc = fetch_entity_meta(cik)
    except Exception:
        pass

    if sic == BLANK_CHECK_SIC:
        return f"SIC 6770 ({sic_desc})" if sic_desc else "SIC 6770"

    if _NAME_PATTERN.search(company.strip()):
        return "name pattern: Acquisition Corp"

    return None
