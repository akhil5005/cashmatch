"""Turning the matcher's signals into a confidence, and a confidence into a
decision.

The arithmetic is deliberately simple: each signal scores in [0, 1], the
signals are combined by a weighted sum, and penalties multiply the result.
That is not a limitation to apologise for -- it is the point. A controller
can be walked through the number in sixty seconds, and a client who wants
fewer errors moves one threshold and sees the trade immediately. A
gradient-boosted model over the same signals would almost certainly score
better once there is labelled review history to train on, and the breakdown
stored on every decision is exactly the training set it would need.

Why penalties are multiplicative rather than subtractive: an ambiguity
should scale down whatever confidence the evidence produced, not shave a
fixed amount off it. Two invoice sets tying on amount is just as damning for
a strong match as for a weak one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from cashmatch.matching.candidates import IdentificationMethod, MatchCandidate, MatchOutcome
from cashmatch.matching.config import ScoringConfig
from cashmatch.models.enums import MatchDecision, MatchStrategy

# How much each identification tier is worth. An alias hit is an exact
# lookup against a spelling a human already confirmed, so it scores the same
# as the registered name; a fuzzy hit carries its own similarity score.
_IDENTITY_SCORES: dict[IdentificationMethod, float] = {
    IdentificationMethod.EXACT: 1.0,
    IdentificationMethod.ALIAS: 1.0,
    # The reference resolved to exactly one customer's invoice, which is
    # strong evidence of identity -- but it is inferred, not asserted.
    IdentificationMethod.REFERENCE: 0.90,
    IdentificationMethod.NONE: 0.0,
    IdentificationMethod.AMBIGUOUS: 0.0,
}

# How cleanly the money lands on the candidate set.
_AMOUNT_SCORES: dict[MatchStrategy, float] = {
    MatchStrategy.REFERENCE_EXACT: 1.0,
    MatchStrategy.REMITTANCE_GUIDED: 1.0,
    MatchStrategy.AMOUNT_EXACT: 1.0,
    # A subset summing exactly is still exact arithmetic, but it was found by
    # search rather than asserted by the customer, so collisions are possible.
    MatchStrategy.SUBSET_SUM: 0.80,
    # A plausible deduction is an inference about intent, not a fact.
    MatchStrategy.SHORT_PAY: 0.55,
    MatchStrategy.MANUAL: 1.0,
    MatchStrategy.NONE: 0.0,
}


@dataclass(slots=True)
class ScoredSignal:
    """One signal's contribution to the confidence.

    ``applicable`` is the distinction that makes the score honest. A signal
    that *could* have fired and did not is evidence against the match and
    scores zero. A signal that had nothing to look at -- no advice document
    exists for this payment -- is not evidence of anything, and counting its
    weight in the denominator would penalise a customer for how they
    communicate rather than for anything about the payment.
    """

    name: str
    fired: bool
    value: float
    weight: float
    detail: str
    raw: Any = None
    applicable: bool = True

    @property
    def effective_weight(self) -> float:
        return self.weight if self.applicable else 0.0

    @property
    def contribution(self) -> float:
        return self.value * self.effective_weight

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fired": self.fired,
            "applicable": self.applicable,
            "value": round(self.value, 4),
            "weight": round(self.effective_weight, 4),
            "contribution": round(self.contribution, 4),
            "detail": self.detail,
            "raw": self.raw,
        }


@dataclass(slots=True)
class AppliedPenalty:
    name: str
    multiplier: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "multiplier": round(self.multiplier, 4),
            "detail": self.detail,
        }


@dataclass(slots=True)
class Scored:
    """The verdict on one transaction, with the whole derivation attached."""

    outcome: MatchOutcome
    decision: MatchDecision
    confidence: float
    base_score: float
    signals: list[ScoredSignal] = field(default_factory=list)
    penalties: list[AppliedPenalty] = field(default_factory=list)
    reason_text: str = ""

    @property
    def candidate(self) -> MatchCandidate | None:
        return self.outcome.candidates[0] if self.outcome.candidates else None

    @property
    def confidence_decimal(self) -> Decimal:
        """Four decimal places, matching the Numeric(5,4) column."""
        return Decimal(f"{self.confidence:.4f}")

    def explanation(self) -> dict[str, Any]:
        """The payload stored on ``match_results.explanation``.

        It carries both halves of the story: how the candidates were found
        (the matcher's trail) and how they were scored. A reviewer who can
        see only one of those has to redo the other.
        """
        return {
            "decision": self.decision.value,
            "confidence": round(self.confidence, 4),
            "base_score": round(self.base_score, 4),
            "signals": [signal.as_dict() for signal in self.signals],
            # The denominator the weighted sum was divided by. Without it the
            # contributions in `signals` do not visibly add up to base_score,
            # and a reviewer cannot check the arithmetic.
            "applicable_weight": round(sum(signal.effective_weight for signal in self.signals), 4),
            "excluded_signals": [signal.name for signal in self.signals if not signal.applicable],
            "penalties": [penalty.as_dict() for penalty in self.penalties],
            "reason": self.reason_text,
            "matching": self.outcome.as_dict(),
        }


class ConfidenceScorer:
    """Scores a matched transaction and decides what to do with it."""

    def __init__(self, config: ScoringConfig) -> None:
        self._cfg = config

    def score(self, outcome: MatchOutcome) -> Scored:
        """Score one outcome and pick a decision."""
        if not outcome.candidates:
            return self._unapplied(outcome)

        signals = self._signals(outcome)
        # Divide by the weight of the signals that actually had something to
        # look at. Without this, a flawless match from a customer who never
        # sends remittance advice is capped below the auto threshold forever.
        available = sum(signal.effective_weight for signal in signals)
        base = sum(signal.contribution for signal in signals) / available if available else 0.0

        penalties = self._penalties(outcome)
        confidence = base
        for penalty in penalties:
            confidence *= penalty.multiplier
        confidence = max(0.0, min(1.0, confidence))

        decision = self._decide(confidence)
        scored = Scored(
            outcome=outcome,
            decision=decision,
            confidence=confidence,
            base_score=base,
            signals=signals,
            penalties=penalties,
        )
        scored.reason_text = _reason(scored)
        return scored

    # --- signals ----------------------------------------------------------

    def _signals(self, outcome: MatchOutcome) -> list[ScoredSignal]:
        weights = self._cfg.weights
        return [
            self._reference_signal(outcome, weights.reference_match),
            self._amount_signal(outcome, weights.amount_match),
            self._identity_signal(outcome, weights.customer_identity),
            self._remittance_signal(outcome, weights.remittance_agreement),
            self._date_signal(outcome, weights.date_proximity),
        ]

    def _reference_signal(self, outcome: MatchOutcome, weight: float) -> ScoredSignal:
        """Did the customer actually name the invoices being settled?"""
        strategy = outcome.strategy

        if strategy is MatchStrategy.REMITTANCE_GUIDED:
            return ScoredSignal(
                "reference_match",
                True,
                1.0,
                weight,
                "The customer's own remittance advice names these invoices.",
                raw=[token.normalized for token in outcome.references] or None,
            )
        if strategy is MatchStrategy.REFERENCE_EXACT:
            return ScoredSignal(
                "reference_match",
                True,
                1.0,
                weight,
                "An invoice reference in the bank narration resolved to these open items.",
                raw=[token.normalized for token in outcome.references],
            )

        found = bool(outcome.references)
        return ScoredSignal(
            "reference_match",
            False,
            0.0,
            weight,
            (
                "References were present in the narration but none resolved to these invoices."
                if found
                else "No invoice reference anywhere; the match rests on amount and payer."
            ),
            raw=[token.normalized for token in outcome.references] or None,
        )

    def _amount_signal(self, outcome: MatchOutcome, weight: float) -> ScoredSignal:
        candidate = outcome.candidates[0]
        value = _AMOUNT_SCORES.get(candidate.strategy, 0.0)

        shape = next((s.raw for s in candidate.signals if s.name == "settlement_shape"), "unknown")
        if shape == "partial":
            # A part payment is correct bookkeeping but weaker evidence: the
            # amount tied to nothing in particular, it was simply less.
            value *= 0.70
        elif shape == "short_pay" and candidate.strategy is not MatchStrategy.SHORT_PAY:
            # Reached by reference or advice, so the invoice set is asserted;
            # only the size of the claim is inferred.
            value *= 0.88

        return ScoredSignal(
            "amount_match",
            value > 0,
            value,
            weight,
            f"Found by {candidate.strategy.value}, settling as {shape}.",
            raw={"strategy": candidate.strategy.value, "shape": shape},
        )

    def _identity_signal(self, outcome: MatchOutcome, weight: float) -> ScoredSignal:
        customer = outcome.customer
        if customer.method is IdentificationMethod.FUZZY:
            value = customer.score
            detail = (
                f"Payer name matched by fuzzy similarity at {customer.score:.0%} "
                f"(via {customer.matched_on!r})."
            )
        else:
            value = _IDENTITY_SCORES.get(customer.method, 0.0)
            detail = {
                IdentificationMethod.EXACT: "Payer name matched a customer exactly.",
                IdentificationMethod.ALIAS: "Payer name matched a spelling already on file.",
                IdentificationMethod.REFERENCE: (
                    "Customer inferred from a resolved invoice reference, not the name."
                ),
                IdentificationMethod.NONE: "Payer could not be identified.",
                IdentificationMethod.AMBIGUOUS: (
                    "Two customers scored alike; the payer was not identified."
                ),
            }[customer.method]

        return ScoredSignal(
            "customer_identity",
            value > 0,
            value,
            weight,
            detail,
            raw={"method": customer.method.value, "score": round(customer.score, 4)},
        )

    def _remittance_signal(self, outcome: MatchOutcome, weight: float) -> ScoredSignal:
        """Did the customer's own advice corroborate this reading?"""
        guided = outcome.strategy is MatchStrategy.REMITTANCE_GUIDED
        advice_signal = next((s for s in outcome.trail if s.name == "remittance_guided"), None)

        if guided and advice_signal is not None:
            reconciled = bool((advice_signal.raw or {}).get("reconciled"))
            value = 1.0 if reconciled else 0.65
            return ScoredSignal(
                "remittance_agreement",
                True,
                value,
                weight,
                (
                    "Advice names these invoices and its amounts tie to the credit received."
                    if reconciled
                    else "Advice names these invoices but its amounts do not tie to the credit."
                ),
                raw=advice_signal.raw,
            )

        if advice_signal is not None and not advice_signal.fired:
            # Advice exists and does *not* support this reading. That is
            # evidence against the match, so it scores zero and counts.
            return ScoredSignal(
                "remittance_agreement",
                False,
                0.0,
                weight,
                f"Advice was available but did not support this match. {advice_signal.detail}",
            )

        # No advice at all. Most customers never send any; holding that
        # against the payment would be scoring the customer's filing habits,
        # not the match.
        return ScoredSignal(
            "remittance_agreement",
            False,
            0.0,
            weight,
            "No remittance advice is linked to this payment, so there is nothing to "
            "corroborate against. The signal is excluded rather than scored zero.",
            applicable=False,
        )

    def _date_signal(self, outcome: MatchOutcome, weight: float) -> ScoredSignal:
        """Is the money arriving when these invoices would plausibly be paid?

        The weakest signal, and deliberately so: a chronic late payer is
        still paying their own invoices. It breaks ties rather than deciding
        anything.
        """
        candidate = outcome.candidates[0]
        value_date = outcome.diagnostics.get("value_date")
        if not isinstance(value_date, date) or not candidate.allocations:
            return ScoredSignal(
                "date_proximity",
                False,
                0.0,
                weight,
                "No value date available to compare against the due dates.",
                applicable=False,
            )

        gaps = [
            abs((value_date - allocation.item.due_date).days)
            for allocation in candidate.allocations
        ]
        worst = max(gaps)
        value = _proximity(worst, self._cfg.date_proximity)

        return ScoredSignal(
            "date_proximity",
            value > 0,
            value,
            weight,
            (
                f"Payment landed {worst} day(s) from the furthest due date in the set "
                f"({'well within' if value > 0.8 else 'outside'} the expected window)."
            ),
            raw={"max_days_from_due": worst},
        )

    # --- penalties --------------------------------------------------------

    def _penalties(self, outcome: MatchOutcome) -> list[AppliedPenalty]:
        applied: list[AppliedPenalty] = []
        config = self._cfg.penalties

        if outcome.is_ambiguous:
            applied.append(
                AppliedPenalty(
                    "ambiguous",
                    config.ambiguous,
                    (
                        f"{len(outcome.candidates)} readings tie and nothing separates them. "
                        "Picking one would be a coin flip posted with high confidence."
                    ),
                )
            )

        advice_signal = next(
            (s for s in outcome.trail if s.name == "remittance_guided" and s.fired), None
        )
        if advice_signal is not None and not (advice_signal.raw or {}).get("reconciled"):
            applied.append(
                AppliedPenalty(
                    "unreconciled_advice",
                    config.unreconciled_advice,
                    "The advice guiding this match did not reconcile against the credit.",
                )
            )

        candidate = outcome.candidates[0]
        deducted = [a for a in candidate.allocations if a.deduction_amount_paise]
        if (
            deducted
            and len(candidate.allocations) > 1
            and not any(a.bearer_asserted for a in candidate.allocations)
        ):
            applied.append(
                AppliedPenalty(
                    "guessed_claim_bearer",
                    config.guessed_claim_bearer,
                    (
                        f"The claim of {deducted[0].deduction_amount_paise} paise was placed "
                        f"on {deducted[0].item.invoice_number} because it is the smallest "
                        f"invoice in a set of {len(candidate.allocations)} that can absorb "
                        "it. Nothing in the payment or the advice says which invoice the "
                        "claim actually belongs to."
                    ),
                )
            )

        if outcome.residual_paise > 0:
            applied.append(
                AppliedPenalty(
                    "residual_cash",
                    config.residual_cash,
                    (
                        f"{outcome.residual_paise} paise of the credit could not be placed "
                        "and would remain unapplied."
                    ),
                )
            )

        return applied

    # --- decision ---------------------------------------------------------

    def _decide(self, confidence: float) -> MatchDecision:
        thresholds = self._cfg.thresholds
        if confidence >= thresholds.auto_apply:
            return MatchDecision.AUTO_APPLIED
        if confidence >= thresholds.review_floor:
            return MatchDecision.NEEDS_REVIEW
        # Below the floor there are candidates, but proposing them costs a
        # reviewer more attention than it saves. They stay in the explanation.
        return MatchDecision.UNAPPLIED

    def _unapplied(self, outcome: MatchOutcome) -> Scored:
        scored = Scored(
            outcome=outcome,
            decision=MatchDecision.UNAPPLIED,
            confidence=0.0,
            base_score=0.0,
            signals=[],
            penalties=[],
        )
        scored.reason_text = (
            "No credible allocation found. The money stays as unapplied cash on the "
            "customer's account. "
            + next(
                (s.detail for s in reversed(outcome.trail) if not s.fired),
                "No signal fired.",
            )
        )
        return scored


def _proximity(days: int, config) -> float:
    """Linear decay from full marks at ``ideal_days`` to zero at ``zero_days``."""
    if days <= config.ideal_days:
        return 1.0
    if days >= config.zero_days:
        return 0.0
    span = config.zero_days - config.ideal_days
    return 1.0 - (days - config.ideal_days) / span


def _reason(scored: Scored) -> str:
    """One sentence a reviewer can act on without opening the JSON."""
    candidate = scored.candidate
    assert candidate is not None

    invoices = ", ".join(candidate.invoice_numbers)
    lead = next(
        (s for s in scored.signals if s.fired and s.name in ("reference_match", "amount_match")),
        None,
    )
    opening = {
        MatchDecision.AUTO_APPLIED: "Applied automatically",
        MatchDecision.NEEDS_REVIEW: "Sent for review",
        MatchDecision.UNAPPLIED: "Left unapplied",
    }[scored.decision]

    text = f"{opening} at {scored.confidence:.0%} confidence: {invoices}."
    if lead is not None:
        text += f" {lead.detail}"
    if candidate.deduction_paise:
        text += f" A {candidate.deduction_paise} paise deduction was detected."
    for penalty in scored.penalties:
        text += f" {penalty.detail}"

    return text[:500]
