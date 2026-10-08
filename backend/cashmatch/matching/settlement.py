"""Given a set of invoices and an amount, work out *how* the money applies.

There are two independent questions about any match, and conflating them is
the usual way these engines become unexplainable:

* **How were the candidates found?** By reference, by amount, by subset-sum,
  by short-pay search. That is the :class:`MatchStrategy`, chosen by the
  caller.
* **How does the money land on them?** Exactly, short of a claim, or as a
  part payment. That is the :class:`SettlementShape`, decided here.

A reference-guided match can perfectly well be short-paid, and an invoice
found by amount can be part-paid. Keeping the two axes separate means a
payment does not change its character depending on which strategy happened to
reach it first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cashmatch.matching.candidates import CandidateAllocation, OpenItem
from cashmatch.matching.config import AmountConfig, ShortPayConfig
from cashmatch.models.enums import DeductionReason


class SettlementShape(StrEnum):
    """How a payment lands on a candidate invoice set."""

    #: Totals tie within tolerance. Every invoice clears.
    EXACT = "exact"
    #: Invoices total more than arrived, by a plausible claim.
    SHORT_PAY = "short_pay"
    #: One invoice, less money than it asks for. It stays open for the rest.
    PARTIAL = "partial"


@dataclass(slots=True)
class Settlement:
    """How a candidate set absorbs the payment."""

    shape: SettlementShape
    allocations: list[CandidateAllocation]
    note: str

    @property
    def allocated_paise(self) -> int:
        return sum(a.allocated_amount_paise for a in self.allocations)

    @property
    def deduction_paise(self) -> int:
        return sum(a.deduction_amount_paise for a in self.allocations)


def claim_bearer(items: list[OpenItem], gap_paise: int) -> OpenItem | None:
    """Which invoice in the set carries the claim.

    The smallest invoice that can absorb the gap. Deductions are raised
    against the specific consignment that was damaged or short-shipped
    rather than spread across a remittance, and the smallest-that-fits rule
    both reflects that and stays deterministic.

    Returns None when no invoice in the set is large enough -- a gap bigger
    than every invoice is not a deduction, it is a different payment.
    """
    bearers = [item for item in items if item.open_amount_paise > gap_paise]
    if not bearers:
        return None
    return min(bearers, key=lambda item: (item.open_amount_paise, item.invoice_number))


def is_plausible_deduction(gap_paise: int, bearer: OpenItem, config: ShortPayConfig) -> bool:
    """Is this gap a credible claim against this specific invoice?

    Measured against the invoice carrying the claim, **not** against the
    remittance total. A Rs. 4,900 claim on a Rs. 56,865 invoice is a routine
    8.6% damage adjustment; expressed as a share of the Rs. 10,99,591 three-
    invoice remittance it contains, the same claim looks like 0.45% and would
    be dismissed as rounding.
    """
    if gap_paise <= 0 or gap_paise >= bearer.open_amount_paise:
        return False
    if gap_paise > config.max_deduction_paise:
        return False
    share = gap_paise / bearer.open_amount_paise * 100
    return config.min_deduction_pct <= share <= config.max_deduction_pct


def deduction_band(amount_paise: int, config: ShortPayConfig) -> tuple[int, int]:
    """Candidate-set totals worth *searching* for a short-pay reading.

    This is deliberately the widest defensible band, because the search runs
    before the claim-bearing invoice is known and so cannot apply the real
    per-invoice test yet. Every solution it returns is then validated by
    :func:`is_plausible_deduction`, which is the check that actually decides.

    The upper bound is exact. The gap ``d`` satisfies ``d <= max_pct * B``
    for the bearer ``B``, and ``B <= total = amount + d``, so::

        d <= amount * max_pct / (1 - max_pct)

    The lower bound is just above the payment: a claim against a small
    invoice inside a large remittance can be an arbitrarily small share of
    the total, so there is no useful floor to impose here.
    """
    ratio = config.max_deduction_pct / (100 - config.max_deduction_pct)
    max_gap = min(int(amount_paise * ratio), config.max_deduction_paise)
    # A total equal to the payment is an exact match, not a zero deduction.
    return amount_paise + 1, amount_paise + max(max_gap, 0)


def settle(
    items: list[OpenItem],
    amount_paise: int,
    amount_config: AmountConfig,
    short_pay_config: ShortPayConfig,
    *,
    allow_partial: bool = True,
    allow_short_pay: bool = True,
) -> Settlement | None:
    """Classify how `amount_paise` applies to `items`, or return None.

    Args:
        items: the candidate invoice set, already chosen by a strategy.
        amount_paise: the bank credit, in paise.
        allow_partial: set False when a part payment reading is not available
            to the caller, for instance on a multi-invoice candidate set.
        allow_short_pay: set False when the caller is already producing the
            short-pay reading by another route and would duplicate it.
    """
    if not items or amount_paise <= 0:
        return None

    total = sum(item.open_amount_paise for item in items)
    tolerance = amount_config.tolerance_paise

    if abs(total - amount_paise) <= tolerance:
        return _as_exact(items, amount_paise, total)

    if allow_short_pay and short_pay_config.enabled and total > amount_paise:
        gap = total - amount_paise
        bearer = claim_bearer(items, gap)
        if bearer is not None and is_plausible_deduction(gap, bearer, short_pay_config):
            return _as_short_pay(items, amount_paise, total, bearer)

    if allow_partial and len(items) == 1 and amount_paise < total - tolerance:
        item = items[0]
        return Settlement(
            shape=SettlementShape.PARTIAL,
            allocations=[CandidateAllocation(item=item, allocated_amount_paise=amount_paise)],
            note=(
                f"Part payment of {item.invoice_number}: {amount_paise} paise received "
                f"against {total} paise open, leaving {total - amount_paise} paise."
            ),
        )

    return None


def _as_exact(items: list[OpenItem], amount_paise: int, total: int) -> Settlement:
    """Settle a candidate set whose total ties the payment within tolerance.

    The tolerance exists so that a rupee of bank rounding does not cost a
    match. It must not be allowed to invent money: when the invoices total
    slightly *more* than arrived, allocating each in full would apply cash
    that was never received and the ledger would not balance.

    So the difference is booked as a tolerance write-off against the smallest
    invoice, keeping two invariants true at once::

        sum(allocated)            == amount received
        allocated + deduction     == invoice open amount

    A shortfall the other way needs no adjustment: the invoices are cleared
    in full and the few extra paise surface as residual on the outcome.
    """
    ordered = sorted(items, key=lambda item: item.invoice_number)
    allocations = [
        CandidateAllocation(item=item, allocated_amount_paise=item.open_amount_paise)
        for item in ordered
    ]

    overage = total - amount_paise
    note = _exact_note(items, amount_paise)

    if overage > 0:
        bearer = min(ordered, key=lambda item: (item.open_amount_paise, item.invoice_number))
        for allocation in allocations:
            if allocation.item.invoice_id == bearer.invoice_id:
                allocation.allocated_amount_paise -= overage
                allocation.deduction_amount_paise = overage
                allocation.deduction_reason = DeductionReason.UNKNOWN
                break
        note += (
            f" A {overage} paise rounding difference was written off against "
            f"{bearer.invoice_number} so the applied cash ties to the credit received."
        )

    return Settlement(shape=SettlementShape.EXACT, allocations=allocations, note=note)


def _as_short_pay(
    items: list[OpenItem], amount_paise: int, total: int, claimed: OpenItem
) -> Settlement:
    """Apply the cash and book the gap as a claim against `claimed`."""
    gap = total - amount_paise

    allocations = [
        CandidateAllocation(item=item, allocated_amount_paise=item.open_amount_paise)
        for item in items
        if item.invoice_id != claimed.invoice_id
    ]
    allocations.append(
        CandidateAllocation(
            item=claimed,
            allocated_amount_paise=claimed.open_amount_paise - gap,
            deduction_amount_paise=gap,
            # Phase 4 reads the reason code off the remittance advice. Until
            # then the gap is detected but not explained, and recording that
            # honestly beats guessing a category.
            deduction_reason=DeductionReason.UNKNOWN,
        )
    )
    allocations.sort(key=lambda allocation: allocation.item.invoice_number)

    return Settlement(
        shape=SettlementShape.SHORT_PAY,
        allocations=allocations,
        note=(
            f"Short payment: {len(items)} invoice(s) totalling {total} paise settled with "
            f"{amount_paise} paise. Gap of {gap} paise booked as a deduction on "
            f"{claimed.invoice_number}, which is "
            f"{gap / claimed.open_amount_paise * 100:.1f}% of that invoice; reason code not "
            "yet determined."
        ),
    )


def _exact_note(items: list[OpenItem], amount_paise: int) -> str:
    if len(items) == 1:
        return f"{items[0].invoice_number} settles exactly against {amount_paise} paise."
    numbers = ", ".join(sorted(item.invoice_number for item in items))
    return f"{len(items)} invoices ({numbers}) total exactly {amount_paise} paise."
