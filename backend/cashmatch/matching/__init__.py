"""Phase 3 -- the deterministic matching cascade.

Normalise, identify the customer, then try four strategies in order of
decreasing certainty: exact reference, exact amount, subset-sum, short-pay.
The cascade stops at the first step that produces candidates, and records
every step that ran so the result can be explained.

Nothing here decides anything or writes to the database. Scoring the signals
into a confidence and thresholding that into auto-apply / review / unapplied
is Phase 5.
"""

from cashmatch.matching.book import OpenItemBook
from cashmatch.matching.candidates import (
    CandidateAllocation,
    CustomerIdentification,
    IdentificationMethod,
    MatchCandidate,
    MatchOutcome,
    OpenItem,
    ReferenceToken,
    Signal,
)
from cashmatch.matching.config import MatchingConfig
from cashmatch.matching.customer import identify_customer
from cashmatch.matching.engine import MatchingEngine, TransactionInput
from cashmatch.matching.references import (
    all_references,
    extract_references,
    labelled_references,
)
from cashmatch.matching.runner import BatchSummary, load_transactions, run_matching
from cashmatch.matching.settlement import Settlement, SettlementShape, deduction_band, settle
from cashmatch.matching.subsetsum import SubsetSearchResult, find_subsets, theoretical_space

__all__ = [
    "BatchSummary",
    "CandidateAllocation",
    "CustomerIdentification",
    "IdentificationMethod",
    "MatchCandidate",
    "MatchOutcome",
    "MatchingConfig",
    "MatchingEngine",
    "OpenItem",
    "OpenItemBook",
    "ReferenceToken",
    "Settlement",
    "SettlementShape",
    "Signal",
    "SubsetSearchResult",
    "TransactionInput",
    "all_references",
    "deduction_band",
    "extract_references",
    "find_subsets",
    "identify_customer",
    "labelled_references",
    "load_transactions",
    "run_matching",
    "settle",
    "theoretical_space",
]
