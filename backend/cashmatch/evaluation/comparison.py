"""Comparing what the engine decided against what was actually true.

The definition of "correct" is the whole of this module, and it is worth
being pedantic about: every number in the report inherits whatever this
function means by it.

A match is correct when it proposes **exactly** the invoices the payment
really settled, each for the right amount. Not a superset, not a subset, not
"two of the three". In cash application a partially right allocation is
still a wrong ledger entry -- it leaves one invoice open that should be
closed and closes one that should not be.

Reason codes are scored separately. Getting the money right and the claim
category wrong is a materially different failure from getting the money
wrong, and averaging them together would hide both.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cashmatch.generator.ground_truth import TruthPayment
from cashmatch.models.enums import DeductionReason, MatchDecision
from cashmatch.scoring.scorer import Scored


class Verdict(StrEnum):
    """How a decision compares with the truth."""

    #: Exactly the right invoices, each for the right amount.
    CORRECT = "correct"
    #: Proposed an allocation that is not what happened.
    WRONG = "wrong"
    #: The payment really did settle invoices; the engine proposed nothing.
    MISSED = "missed"
    #: The payment settled nothing and the engine proposed nothing. A win.
    CORRECTLY_UNAPPLIED = "correctly_unapplied"


@dataclass(slots=True)
class Comparison:
    """One payment, judged."""

    statement_ref: str
    scenario: str
    decision: MatchDecision
    verdict: Verdict
    confidence: float
    strategy: str
    amount_paise: int
    #: Right invoices but wrong amounts -- a near miss worth counting apart.
    invoices_correct: bool = False
    expected: list[str] | None = None
    proposed: list[str] | None = None
    #: None when the truth records no claim, so nothing was there to classify.
    reason_correct: bool | None = None

    @property
    def is_correct(self) -> bool:
        return self.verdict in (Verdict.CORRECT, Verdict.CORRECTLY_UNAPPLIED)

    @property
    def is_costly(self) -> bool:
        """A wrong allocation posted without a human ever seeing it.

        The only failure mode that actually hurts. Everything else is either
        right, or sitting in a queue where a human will catch it.
        """
        return self.verdict is Verdict.WRONG and self.decision is MatchDecision.AUTO_APPLIED


def compare(scored: Scored, truth: TruthPayment, tolerance_paise: int = 100) -> Comparison:
    """Judge one scored decision against the answer key."""
    candidate = scored.candidate
    proposed = (
        {a.item.invoice_number: a.allocated_amount_paise for a in candidate.allocations}
        if candidate is not None and scored.decision is not MatchDecision.UNAPPLIED
        else {}
    )
    expected = {a.invoice_number: a.allocated_amount_paise for a in truth.allocations}

    base = Comparison(
        statement_ref=scored.outcome.statement_ref,
        scenario=truth.scenario.value,
        decision=scored.decision,
        verdict=Verdict.WRONG,
        confidence=scored.confidence,
        strategy=scored.outcome.strategy.value,
        amount_paise=scored.outcome.amount_paise,
        expected=sorted(expected),
        proposed=sorted(proposed),
    )

    if not expected and not proposed:
        base.verdict = Verdict.CORRECTLY_UNAPPLIED
        return base

    if expected and not proposed:
        base.verdict = Verdict.MISSED
        return base

    if not expected and proposed:
        # The engine invented a match for money that settled nothing.
        base.verdict = Verdict.WRONG
        return base

    base.invoices_correct = set(expected) == set(proposed)
    amounts_tie = base.invoices_correct and all(
        abs(expected[number] - proposed[number]) <= tolerance_paise for number in expected
    )
    base.verdict = Verdict.CORRECT if amounts_tie else Verdict.WRONG

    if base.verdict is Verdict.CORRECT:
        base.reason_correct = _reason_matches(scored, truth)

    return base


def _reason_matches(scored: Scored, truth: TruthPayment) -> bool | None:
    """Did the engine put the right category on the claim?

    Returns None when the truth records no deduction at all, so there was
    nothing to classify and scoring it either way would be noise.
    """
    expected = {
        allocation.invoice_number: allocation.deduction_reason
        for allocation in truth.allocations
        if allocation.deduction_amount_paise and allocation.deduction_reason
    }
    if not expected:
        return None

    candidate = scored.candidate
    if candidate is None:
        return False

    proposed = {
        allocation.item.invoice_number: allocation.deduction_reason
        for allocation in candidate.allocations
        if allocation.deduction_amount_paise
    }

    for number, reason in expected.items():
        found = proposed.get(number)
        # UNKNOWN means the gap was detected but never explained. That is an
        # honest answer, not a correct one.
        if found is None or found is DeductionReason.UNKNOWN or found is not reason:
            return False
    return True
