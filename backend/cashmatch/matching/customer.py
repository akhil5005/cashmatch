"""Step 2 of the pipeline: work out who paid.

Three tiers, strongest and cheapest first:

1. **Exact** -- the normalised payer name is a customer's normalised name.
   An index lookup. On the generated book this resolves 85.5% of payments,
   which is the whole argument for normalising before anything else.
2. **Alias** -- the normalised payer matches a spelling already on file.
   Another index lookup; 8.6% more.
3. **Fuzzy** -- rapidfuzz against every name and alias. The remaining 5.9%,
   and the only tier that can be wrong.

The fuzzy tier carries two guards. A score floor, and an **ambiguity margin**:
the best candidate must beat the runner-up by a configured gap. Two customers
scoring 90 and 89 is not a match, it is a coin flip, and a coin flip that
posts money against the wrong account is exactly the failure this project is
built to avoid.
"""

from __future__ import annotations

from rapidfuzz import fuzz, process

from cashmatch.matching.book import OpenItemBook
from cashmatch.matching.candidates import CustomerIdentification, IdentificationMethod
from cashmatch.matching.config import CustomerIdentificationConfig


def identify_customer(
    payer_name_normalized: str,
    book: OpenItemBook,
    config: CustomerIdentificationConfig,
) -> CustomerIdentification:
    """Resolve a normalised payer name to a customer.

    Returns an identification whose ``method`` records which tier succeeded,
    so Phase 5 can weight a fuzzy hit differently from an exact one and a
    reviewer can see which it was.
    """
    if not payer_name_normalized:
        return CustomerIdentification(
            customer_id=None,
            method=IdentificationMethod.NONE,
            score=0.0,
            matched_on=None,
        )

    exact = book.by_customer_name.get(payer_name_normalized)
    if exact is not None:
        return CustomerIdentification(
            customer_id=exact,
            method=IdentificationMethod.EXACT,
            score=1.0,
            matched_on=payer_name_normalized,
            considered=1,
        )

    alias = book.by_alias.get(payer_name_normalized)
    if alias is not None:
        return CustomerIdentification(
            customer_id=alias,
            method=IdentificationMethod.ALIAS,
            score=1.0,
            matched_on=payer_name_normalized,
            considered=1,
        )

    return _fuzzy_identify(payer_name_normalized, book, config)


def _fuzzy_identify(
    payer: str, book: OpenItemBook, config: CustomerIdentificationConfig
) -> CustomerIdentification:
    """Last resort: approximate string matching over names and aliases."""
    if not book.fuzzy_choices:
        return CustomerIdentification(customer_id=None, method=IdentificationMethod.NONE, score=0.0)

    scorer = getattr(fuzz, config.fuzzy_scorer)
    # Pull a few so the runner-up is available for the ambiguity check.
    ranked = process.extract(payer, book.fuzzy_choices, scorer=scorer, limit=5)
    considered = len(book.fuzzy_choices)

    if not ranked:
        return CustomerIdentification(
            customer_id=None,
            method=IdentificationMethod.NONE,
            score=0.0,
            considered=considered,
        )

    best_name, best_score, best_index = ranked[0]
    best_owner = book.fuzzy_owners[best_index]

    # The runner-up only counts if it points at a *different* customer. Two
    # aliases of the same customer scoring alike is agreement, not ambiguity.
    runner_name: str | None = None
    runner_score: float | None = None
    for name, score, index in ranked[1:]:
        if book.fuzzy_owners[index] != best_owner:
            runner_name, runner_score = name, score
            break

    if best_score < config.fuzzy_min_score:
        return CustomerIdentification(
            customer_id=None,
            method=IdentificationMethod.NONE,
            score=best_score / 100,
            matched_on=best_name,
            runner_up=runner_name,
            runner_up_score=(runner_score / 100) if runner_score is not None else None,
            considered=considered,
        )

    if runner_score is not None and (best_score - runner_score) < config.fuzzy_margin:
        # Close enough to be a coin flip. Refuse, and say why.
        return CustomerIdentification(
            customer_id=None,
            method=IdentificationMethod.AMBIGUOUS,
            score=best_score / 100,
            matched_on=best_name,
            runner_up=runner_name,
            runner_up_score=runner_score / 100,
            considered=considered,
        )

    return CustomerIdentification(
        customer_id=best_owner,
        method=IdentificationMethod.FUZZY,
        score=best_score / 100,
        matched_on=best_name,
        runner_up=runner_name,
        runner_up_score=(runner_score / 100) if runner_score is not None else None,
        considered=considered,
    )
