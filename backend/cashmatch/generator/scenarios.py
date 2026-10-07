"""Payment scenario construction.

Each generated bank credit belongs to exactly one scenario, and the scenario
decides three things at once: which invoices it settles, how much money
arrives, and how legible the bank narration and payer name are. Keeping the
labels mutually exclusive is what lets Phase 6 say "short-pay detection is the
weak spot" rather than reporting one averaged accuracy number.

Invoice consumption is exclusive: no invoice is targeted by two payments. That
rules out the genuine two-instalment case, and it is a deliberate
simplification -- it keeps the answer key unambiguous, which matters more at
this stage than covering every real-world shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from cashmatch.generator.config import GeneratorConfig, ScenarioLabel
from cashmatch.generator.ground_truth import TruthAllocation
from cashmatch.generator.ledger import InvoiceSpec
from cashmatch.generator.parties import (
    PartyProfile,
    build_unknown_payer,
    clean_payer_variant,
    hard_payer_variant,
)
from cashmatch.generator.vocabulary import CHANNELS, COLLECTING_ACCOUNTS
from cashmatch.models.enums import DeductionReason
from cashmatch.normalize import normalize_party_name

# Narration shapes that carry a usable, well-formed invoice reference.
_CLEAN_NARRATIONS: tuple[str, ...] = (
    "{channel} {payer} {refs}",
    "{channel}/{payer}/{refs}",
    "{channel} CR {refs}",
    "PAYMENT AGAINST {refs} - {payer}",
    "{channel} {payer} INV {refs}",
)

# Narration with no invoice reference at all. No digits, so nothing here can
# be mistaken for a mangled reference.
_BLANK_NARRATIONS: tuple[str, ...] = (
    "{channel} {payer}",
    "{channel} CREDIT",
    "{payer} ACCOUNT PAYMENT",
    "BULK PAYMENT {month}",
    "{channel} FUNDS TRANSFER",
    "COLLECTION {month} - {payer}",
)

_PARTIAL_NARRATIONS: tuple[str, ...] = (
    "{channel} {payer} PART PMT {refs}",
    "{channel} {payer} ON ACCOUNT {refs}",
    "PART PAYMENT {refs}",
)

_SHORT_PAY_NARRATIONS: tuple[str, ...] = (
    "{channel} {payer} {refs} LESS {claim}",
    "{channel} {payer} {refs} NET OF {claim}",
    "{refs} {claim} ADJUSTED - {payer}",
)

# Shorthand a customer scribbles into the reference field for each claim type.
_CLAIM_SHORTHAND: dict[DeductionReason, str] = {
    DeductionReason.DAMAGE: "DMG CLAIM",
    DeductionReason.PROMO: "SCHEME",
    DeductionReason.PRICING: "RATE DIFF",
    DeductionReason.SHORT_SHIP: "SHORT SUPPLY",
    DeductionReason.FREIGHT: "FRT",
    DeductionReason.TDS: "TDS",
    DeductionReason.UNKNOWN: "ADJ",
}

_MONTHS: tuple[str, ...] = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


@dataclass(slots=True)
class PaymentSpec:
    """One generated bank credit, plus the truth about what it settles."""

    statement_ref: str
    value_date: date
    amount_paise: int
    payer_name_raw: str
    narration: str
    bank_account: str
    scenario: ScenarioLabel
    customer_code: str | None
    allocations: list[TruthAllocation]
    # Invoices this payment consumed, kept so the remittance writer can
    # describe them without re-deriving anything.
    invoices: list[InvoiceSpec] = field(default_factory=list)
    remittance_refs: list[str] = field(default_factory=list)


def _largest_remainder(weights: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` across weights so the parts sum to exactly `total`.

    Naive rounding of percentages loses or invents a few payments. The
    largest-remainder method gives each scenario its floor share, then hands
    the leftovers to whichever scenarios were rounded down hardest.
    """
    exact = {name: total * weight / 100 for name, weight in weights.items()}
    counts = {name: int(value) for name, value in exact.items()}

    shortfall = total - sum(counts.values())
    if shortfall:
        ranked = sorted(exact, key=lambda name: (exact[name] - counts[name], name), reverse=True)
        for name in ranked[:shortfall]:
            counts[name] += 1

    return counts


class PaymentFactory:
    """Builds the full set of payments against a pool of open invoices."""

    def __init__(
        self,
        rng: Random,
        config: GeneratorConfig,
        customers: list[PartyProfile],
        invoices: list[InvoiceSpec],
    ) -> None:
        self._rng = rng
        self._cfg = config
        self._by_code = {party.code: party for party in customers}
        self._known_name_keys = {party.normalized_name for party in customers}
        self._sequence = 0

        # Unconsumed invoices per customer, newest last so bundles tend to be
        # contemporaneous -- which is how real bundled payments look.
        self._pool: dict[str, list[InvoiceSpec]] = {party.code: [] for party in customers}
        for invoice in sorted(invoices, key=lambda i: (i.customer_code, i.invoice_date)):
            self._pool[invoice.customer_code].append(invoice)

        self._deduction_reasons = [DeductionReason(name) for name in config.deduction.reasons]
        self._deduction_weights = list(config.deduction.reasons.values())

    # --- public API -------------------------------------------------------

    def build_all(self) -> list[PaymentSpec]:
        """Generate every payment, in a deterministic shuffled order."""
        counts = _largest_remainder(self._cfg.scenarios.as_weights(), self._cfg.volume.payments)

        schedule: list[ScenarioLabel] = []
        for name, count in counts.items():
            schedule.extend([ScenarioLabel(name)] * count)
        self._rng.shuffle(schedule)

        payments = [self._build_one(label) for label in schedule]
        payments.sort(key=lambda p: (p.value_date, p.statement_ref))
        return payments

    # --- scenario dispatch ------------------------------------------------

    def _build_one(self, label: ScenarioLabel) -> PaymentSpec:
        if label is ScenarioLabel.NO_MATCHING_INVOICE:
            return self._no_matching_invoice()
        if label is ScenarioLabel.BUNDLED_MULTI:
            return self._bundled_multi()
        if label is ScenarioLabel.PARTIAL_PAYMENT:
            return self._partial_payment()
        if label is ScenarioLabel.SHORT_PAY_DEDUCTION:
            return self._short_pay()
        if label is ScenarioLabel.MISSING_REFERENCE:
            return self._missing_reference()
        if label is ScenarioLabel.TYPO_REFERENCE:
            return self._typo_reference()
        if label is ScenarioLabel.PAYER_NAME_VARIANT:
            return self._payer_name_variant()
        return self._exact_single()

    # --- individual scenarios ---------------------------------------------

    def _exact_single(self) -> PaymentSpec:
        """The easy case: one invoice, exact amount, legible reference."""
        party, invoices = self._take(1)
        invoice = invoices[0]
        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=invoice.amount_paise,
            scenario=ScenarioLabel.EXACT_SINGLE,
            narration=self._narrate(_CLEAN_NARRATIONS, party, [invoice.invoice_number]),
            allocations=[
                TruthAllocation(
                    invoice_number=invoice.invoice_number,
                    allocated_amount_paise=invoice.amount_paise,
                )
            ],
        )

    def _bundled_multi(self) -> PaymentSpec:
        """One credit settling several invoices. This is the subset-sum case."""
        wanted = self._rng.randint(self._cfg.bundle.min_invoices, self._cfg.bundle.max_invoices)
        party, invoices = self._take(wanted, minimum=self._cfg.bundle.min_invoices)
        total = sum(invoice.amount_paise for invoice in invoices)

        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=total,
            scenario=ScenarioLabel.BUNDLED_MULTI,
            narration=self._narrate(_CLEAN_NARRATIONS, party, [i.invoice_number for i in invoices]),
            allocations=[
                TruthAllocation(
                    invoice_number=invoice.invoice_number,
                    allocated_amount_paise=invoice.amount_paise,
                )
                for invoice in invoices
            ],
        )

    def _partial_payment(self) -> PaymentSpec:
        """Part of one invoice now, the rest to follow."""
        party, invoices = self._take(1)
        invoice = invoices[0]

        share = self._rng.randint(self._cfg.partial.min_share_pct, self._cfg.partial.max_share_pct)
        amount = max(100, invoice.amount_paise * share // 100)

        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=amount,
            scenario=ScenarioLabel.PARTIAL_PAYMENT,
            narration=self._narrate(_PARTIAL_NARRATIONS, party, [invoice.invoice_number]),
            allocations=[
                TruthAllocation(
                    invoice_number=invoice.invoice_number, allocated_amount_paise=amount
                )
            ],
        )

    def _short_pay(self) -> PaymentSpec:
        """Pays less than billed and says why. The deduction lands on one
        invoice of the set, which is how customers actually raise claims."""
        wanted = self._rng.choice([1, 1, 2, 3])
        party, invoices = self._take(wanted, minimum=1)

        claimed = invoices[-1]
        reason = self._pick_deduction_reason()
        deduction = self._deduction_amount(claimed.amount_paise)

        allocations = [
            TruthAllocation(
                invoice_number=invoice.invoice_number,
                allocated_amount_paise=invoice.amount_paise,
            )
            for invoice in invoices[:-1]
        ]
        allocations.append(
            TruthAllocation(
                invoice_number=claimed.invoice_number,
                allocated_amount_paise=claimed.amount_paise - deduction,
                deduction_amount_paise=deduction,
                deduction_reason=reason,
            )
        )
        amount = sum(allocation.allocated_amount_paise for allocation in allocations)

        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=amount,
            scenario=ScenarioLabel.SHORT_PAY_DEDUCTION,
            narration=self._narrate(
                _SHORT_PAY_NARRATIONS,
                party,
                [i.invoice_number for i in invoices],
                claim=_CLAIM_SHORTHAND[reason],
            ),
            allocations=allocations,
        )

    def _missing_reference(self) -> PaymentSpec:
        """Correct money, zero clue what it is for.

        Structurally a single or bundled payment, so the amount still ties --
        the matcher has to get there on amount and payer alone.
        """
        wanted = self._rng.choice([1, 1, 1, 2, 3])
        party, invoices = self._take(wanted, minimum=1)
        total = sum(invoice.amount_paise for invoice in invoices)

        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=total,
            scenario=ScenarioLabel.MISSING_REFERENCE,
            narration=self._narrate(_BLANK_NARRATIONS, party, []),
            allocations=[
                TruthAllocation(
                    invoice_number=invoice.invoice_number,
                    allocated_amount_paise=invoice.amount_paise,
                )
                for invoice in invoices
            ],
        )

    def _typo_reference(self) -> PaymentSpec:
        """A reference is there, but mangled by whoever typed it."""
        party, invoices = self._take(1)
        invoice = invoices[0]

        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=invoice.amount_paise,
            scenario=ScenarioLabel.TYPO_REFERENCE,
            narration=self._narrate(
                _CLEAN_NARRATIONS, party, [self._mangle_reference(invoice.invoice_number)]
            ),
            allocations=[
                TruthAllocation(
                    invoice_number=invoice.invoice_number,
                    allocated_amount_paise=invoice.amount_paise,
                )
            ],
        )

    def _payer_name_variant(self) -> PaymentSpec:
        """Clean reference, but the payer name is a spelling not on file."""
        party, invoices = self._take(1)
        invoice = invoices[0]

        return self._assemble(
            party=party,
            invoices=invoices,
            amount_paise=invoice.amount_paise,
            scenario=ScenarioLabel.PAYER_NAME_VARIANT,
            narration=self._narrate(_CLEAN_NARRATIONS, party, [invoice.invoice_number]),
            allocations=[
                TruthAllocation(
                    invoice_number=invoice.invoice_number,
                    allocated_amount_paise=invoice.amount_paise,
                )
            ],
            payer_name=self._unregistered_variant(party),
        )

    def _no_matching_invoice(self) -> PaymentSpec:
        """Money from a payer with nothing open. Correct answer: unapplied."""
        payer = build_unknown_payer(self._rng, self._known_name_keys)
        amount = self._rng.randint(
            self._cfg.invoice.min_amount_inr * 100, self._cfg.invoice.max_amount_inr * 100
        )
        value_date = self._rng.choice(
            [
                self._cfg.period.invoice_start + timedelta(days=offset)
                for offset in range(
                    0, (self._cfg.period.invoice_end - self._cfg.period.invoice_start).days + 60
                )
            ]
        )

        self._sequence += 1
        return PaymentSpec(
            statement_ref=self._statement_ref(value_date),
            value_date=value_date,
            amount_paise=amount,
            payer_name_raw=payer,
            narration=self._narrate(_BLANK_NARRATIONS, None, [], payer_override=payer),
            bank_account=self._rng.choice(COLLECTING_ACCOUNTS),
            scenario=ScenarioLabel.NO_MATCHING_INVOICE,
            customer_code=None,
            allocations=[],
        )

    # --- shared construction ---------------------------------------------

    def _assemble(
        self,
        *,
        party: PartyProfile,
        invoices: list[InvoiceSpec],
        amount_paise: int,
        scenario: ScenarioLabel,
        narration: str,
        allocations: list[TruthAllocation],
        payer_name: str | None = None,
    ) -> PaymentSpec:
        value_date = self._value_date(invoices)
        self._sequence += 1

        return PaymentSpec(
            statement_ref=self._statement_ref(value_date),
            value_date=value_date,
            amount_paise=amount_paise,
            payer_name_raw=payer_name or self._payer_name(party),
            narration=narration,
            bank_account=self._rng.choice(COLLECTING_ACCOUNTS),
            scenario=scenario,
            customer_code=party.code,
            allocations=allocations,
            invoices=invoices,
        )

    def _take(self, wanted: int, minimum: int = 1) -> tuple[PartyProfile, list[InvoiceSpec]]:
        """Reserve `wanted` unconsumed invoices from one customer.

        Falls back towards `minimum` when no customer has a deep enough pool,
        rather than failing: a slightly smaller bundle is a better outcome
        than an aborted run.
        """
        for size in range(wanted, minimum - 1, -1):
            eligible = [code for code, pool in self._pool.items() if len(pool) >= size]
            if not eligible:
                continue
            # Weight by pool depth so busy accounts generate more payments,
            # which is what makes their subset-sum search genuinely hard.
            code = self._rng.choices(eligible, weights=[len(self._pool[c]) for c in eligible])[0]
            pool = self._pool[code]
            start = self._rng.randint(0, len(pool) - size)
            taken = pool[start : start + size]
            del pool[start : start + size]
            return self._by_code[code], taken

        raise RuntimeError(
            f"Ran out of unconsumed invoices while building payments (needed "
            f"{minimum}). Raise volume.invoices or lower volume.payments in "
            "config/scenarios.yml."
        )

    def _value_date(self, invoices: list[InvoiceSpec]) -> date:
        """When the money actually landed, relative to the latest due date."""
        latest_due = max(invoice.due_date for invoice in invoices)
        low, high = self._cfg.period.payment_offset_days
        candidate = latest_due + timedelta(days=self._rng.randint(low, high))
        earliest = max(invoice.invoice_date for invoice in invoices) + timedelta(days=1)
        return max(candidate, earliest)

    def _statement_ref(self, value_date: date) -> str:
        """UTR-style reference. Unique by sequence, so re-uploading a
        statement is idempotent rather than duplicating payments."""
        return f"UTR{value_date:%Y%m%d}{self._sequence:05d}"

    def _payer_name(self, party: PartyProfile) -> str:
        """How the bank spelled the payer on this particular credit."""
        draw = self._rng.random()
        if draw < 0.35:
            return party.legal_name
        if draw < 0.85:
            return clean_payer_variant(self._rng, party)
        # An on-file alias: hard to read, but the ERP already knows it.
        if party.registered_aliases:
            return self._rng.choice(party.registered_aliases)
        return clean_payer_variant(self._rng, party)

    def _unregistered_variant(self, party: PartyProfile) -> str:
        """A hard spelling that is *not* among the customer's known aliases,
        so only fuzzy matching can resolve it."""
        known = {normalize_party_name(alias) for alias in party.registered_aliases}
        known.add(party.normalized_name)

        for _ in range(20):
            candidate = hard_payer_variant(self._rng, party)
            if normalize_party_name(candidate) not in known:
                return candidate
        # Every variant happened to be on file; append the branch city, which
        # normalisation never strips.
        return f"{party.legal_name.upper()}-{party.city.upper()} BR"

    def _narrate(
        self,
        templates: tuple[str, ...],
        party: PartyProfile | None,
        references: list[str],
        *,
        claim: str = "",
        payer_override: str | None = None,
    ) -> str:
        payer = payer_override or (party.legal_name.upper() if party else "CUSTOMER")
        template = self._rng.choice(templates)
        text = template.format(
            channel=self._rng.choice(CHANNELS),
            payer=payer,
            refs=" ".join(references),
            claim=claim,
            month=self._rng.choice(_MONTHS),
        )
        return " ".join(text.split())

    def _mangle_reference(self, invoice_number: str) -> str:
        """Produce a reference a human typed from memory.

        Some of these normalisation can rescue ("inv 42"); others it cannot
        (a bare number, a trailing revision letter). Both kinds are needed --
        if every typo normalised cleanly, the scenario would measure nothing.
        """
        digits = invoice_number.split("-")[-1].lstrip("0") or "0"
        choice = self._rng.randint(0, 6)

        if choice == 0:
            return f"inv {digits}"
        if choice == 1:
            return f"INV{digits}"
        if choice == 2:
            return f"Invoice No. {digits}"
        if choice == 3:
            return f"INV/{digits}"
        if choice == 4:
            # Bare number: normalisation keeps the digits but loses the prefix.
            return digits
        if choice == 5:
            # Revision suffix defeats the numeric-tail rule entirely.
            return f"{invoice_number}-A"
        return f"BILL {digits}"

    def _pick_deduction_reason(self) -> DeductionReason:
        return self._rng.choices(self._deduction_reasons, weights=self._deduction_weights)[0]

    def _deduction_amount(self, invoice_amount_paise: int) -> int:
        """A plausible claim: a share of the invoice, rounded like a human.

        Clamped to leave at least one rupee of cash on the invoice, so the
        payment is never zero or negative.
        """
        share = self._rng.randint(
            self._cfg.deduction.min_share_pct, self._cfg.deduction.max_share_pct
        )
        raw = invoice_amount_paise * share // 100
        step = self._cfg.deduction.round_to_rupees * 100
        rounded = max(step, (raw // step) * step)
        return min(rounded, invoice_amount_paise - 100)
