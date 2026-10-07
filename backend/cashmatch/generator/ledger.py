"""Invoice generation: the open-item book the matcher will search."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from random import Random

from cashmatch.generator.config import GeneratorConfig
from cashmatch.generator.parties import PartyProfile
from cashmatch.money import Paise
from cashmatch.normalize import normalize_reference

# Account size tiers, as (share of customers, relative invoice volume).
# A real distributor's book is long-tailed: a handful of key accounts
# generate most of the invoices, which is also what makes subset-sum hard
# for those accounts specifically.
_SIZE_TIERS: tuple[tuple[float, int], ...] = (
    (0.10, 8),  # key accounts
    (0.30, 3),  # mid-market
    (0.60, 1),  # long tail
)


@dataclass(slots=True)
class InvoiceSpec:
    """One invoice, before it reaches the database."""

    invoice_number: str
    normalized_number: str
    customer_code: str
    invoice_date: date
    due_date: date
    amount_paise: Paise
    po_number: str | None


def _amount_paise(rng: Random, cfg: GeneratorConfig) -> Paise:
    """A plausible invoice total in paise.

    Most totals are GST-inclusive and land on an arbitrary paise value, which
    makes them highly distinguishable. A configurable minority are negotiated
    flat rates rounded to Rs.500, and those collide far more easily in
    subset-sum -- which is exactly the realism worth keeping.
    """
    low = cfg.invoice.min_amount_inr * 100
    high = cfg.invoice.max_amount_inr * 100
    value = rng.randint(low, high)

    if rng.random() * 100 < cfg.invoice.round_amount_share_pct:
        value = round(value / 50_000) * 50_000
        value = max(value, low)

    return Paise(value)


def _size_weights(rng: Random, customers: list[PartyProfile]) -> list[int]:
    """Assign each customer a relative invoice volume from the size tiers."""
    weights: list[int] = []
    for _ in customers:
        draw = rng.random()
        cumulative = 0.0
        chosen = _SIZE_TIERS[-1][1]
        for share, weight in _SIZE_TIERS:
            cumulative += share
            if draw <= cumulative:
                chosen = weight
                break
        weights.append(chosen)
    return weights


def build_invoices(
    rng: Random, customers: list[PartyProfile], cfg: GeneratorConfig
) -> list[InvoiceSpec]:
    """Generate the invoice book, long-tailed across customers.

    Every customer gets at least one invoice so that no customer is a dead
    entry; the remainder is distributed by size tier.
    """
    total = cfg.volume.invoices
    if total < len(customers):
        raise ValueError(
            f"volume.invoices ({total}) is below volume.customers "
            f"({len(customers)}); every customer needs at least one invoice."
        )

    by_code = {party.code: party for party in customers}
    owners = [party.code for party in customers]
    weights = _size_weights(rng, customers)
    owners.extend(
        rng.choices([p.code for p in customers], weights=weights, k=total - len(customers))
    )

    span_days = (cfg.period.invoice_end - cfg.period.invoice_start).days

    invoices: list[InvoiceSpec] = []
    for index, code in enumerate(owners, start=1):
        party = by_code[code]
        issued = cfg.period.invoice_start + timedelta(days=rng.randint(0, span_days))
        number = f"INV-{index:05d}"

        invoices.append(
            InvoiceSpec(
                invoice_number=number,
                normalized_number=normalize_reference(number),
                customer_code=code,
                invoice_date=issued,
                due_date=issued + timedelta(days=party.payment_terms_days),
                amount_paise=_amount_paise(rng, cfg),
                po_number=(
                    f"PO/2026/{rng.randint(10000, 99999)}"
                    if rng.random() * 100 < cfg.invoice.po_number_share_pct
                    else None
                ),
            )
        )

    return invoices
