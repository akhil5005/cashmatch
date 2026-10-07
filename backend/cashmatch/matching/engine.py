"""The matching cascade.

Six steps per bank transaction, cheapest and most certain first:

1. **Normalise** the payer name and narration. Done upstream at ingest and
   stored on the row, so the hot path is an index lookup rather than a regex.
2. **Identify the customer** -- exact, then alias, then fuzzy.
3. **Exact invoice reference** found in the narration.
4. **Exact amount** against that customer's open items.
5. **Subset-sum** for bundled payments, pruned by customer, date window and
   combination size.
6. **Short-pay detection** -- a candidate set exceeding the payment by a
   plausible deduction.

The cascade **stops at the first step that produces candidates**. That is the
point of the ordering: a reference hit is cheap and nearly certain, so there
is no reason to run a combinatorial search behind it. Every step that ran and
failed is still recorded in the trail, because a reviewer needs to know the
matcher looked for a reference and found none, rather than wondering whether
it looked at all.

Phase 3 stops at candidates. Turning signals into a confidence, thresholding
it into auto-apply / review / unapplied, and persisting the result is Phase 5.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from cashmatch.matching.book import OpenItemBook
from cashmatch.matching.candidates import (
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
from cashmatch.matching.references import all_references
from cashmatch.matching.settlement import (
    Settlement,
    SettlementShape,
    deduction_band,
    settle,
)
from cashmatch.matching.subsetsum import find_subsets, theoretical_space
from cashmatch.models.enums import DeductionReason, MatchStrategy


@dataclass(slots=True)
class TransactionInput:
    """What the engine needs about one bank credit.

    A plain dataclass rather than the ORM row, so every strategy can be unit
    tested without a database.
    """

    statement_ref: str
    amount_paise: int
    value_date: object  # datetime.date; kept loose to avoid an import cycle
    payer_name_normalized: str
    normalized_narration: str


class MatchingEngine:
    """Runs the cascade over an in-memory open-item book."""

    def __init__(
        self, book: OpenItemBook, config: MatchingConfig, advice: object | None = None
    ) -> None:
        self._book = book
        self._cfg = config
        # An AdviceIndex when Phase 4 has run, otherwise None. Typed loosely
        # so the matching package does not import the extraction package,
        # keeping the dependency one-directional.
        self._advice = advice

    def match(self, txn: TransactionInput) -> MatchOutcome:
        """Produce candidate allocations for one transaction."""
        started = time.perf_counter()

        references = all_references(txn.normalized_narration, self._cfg.reference)
        customer = identify_customer(
            txn.payer_name_normalized, self._book, self._cfg.customer_identification
        )

        outcome = MatchOutcome(
            statement_ref=txn.statement_ref,
            amount_paise=txn.amount_paise,
            customer=customer,
            references=references,
        )
        # Phase 5's date-proximity signal compares this against the due
        # dates of whatever candidate set wins.
        outcome.diagnostics["value_date"] = txn.value_date
        outcome.trail.append(_customer_signal(customer, self._book))
        outcome.trail.append(
            Signal(
                name="references_found",
                fired=bool(references),
                detail=(
                    "Narration carries " + ", ".join(token.normalized for token in references)
                    if references
                    else "No invoice reference in the narration."
                ),
                raw=[token.normalized for token in references],
            )
        )

        # Extracted advice outranks the narration: the customer wrote it
        # deliberately and it names invoices in full.
        if self._try_remittance(txn, outcome):
            return self._finish(outcome, started)

        # Then the narration. A resolved reference can also tell us who paid,
        # rescuing transactions whose payer name was unrecognisable.
        if self._try_reference(txn, outcome):
            return self._finish(outcome, started)

        if not outcome.customer.resolved:
            outcome.trail.append(
                Signal(
                    name="amount_strategies",
                    fired=False,
                    detail=(
                        "Skipped: amount, subset-sum and short-pay all need a known "
                        "customer to bound the candidate pool."
                    ),
                )
            )
            return self._finish(outcome, started)

        pool = self._book.candidates_for(
            outcome.customer.customer_id,
            txn.value_date,
            self._cfg.date_window,
            limit=self._cfg.subset_sum.max_candidate_invoices,
        )
        outcome.diagnostics["candidate_pool"] = len(pool)
        outcome.diagnostics["customer_open_items"] = len(
            self._book.by_customer.get(outcome.customer.customer_id, ())
        )

        if not pool:
            outcome.trail.append(
                Signal(
                    name="candidate_pool",
                    fired=False,
                    detail=(
                        f"Customer has no open items inside the date window "
                        f"({self._cfg.date_window.days_before_payment} days before to "
                        f"{self._cfg.date_window.days_after_payment} days after the payment)."
                    ),
                )
            )
            return self._finish(outcome, started)

        for step in (self._try_amount_exact, self._try_subset_sum, self._try_short_pay):
            if step(txn, outcome, pool):
                break

        return self._finish(outcome, started)

    # --- step 3a: extracted remittance advice -----------------------------

    def _try_remittance(self, txn: TransactionInput, outcome: MatchOutcome) -> bool:
        """Settle against the invoices the customer's own advice names.

        The advice is a claim, not proof. Its references are resolved against
        the open-item book and the arithmetic is re-checked by ``settle``
        exactly as for any other candidate set -- so an extraction that
        hallucinated an invoice number simply fails to resolve, and one that
        got the amounts wrong fails to settle.
        """
        if not self._cfg.remittance.enabled or self._advice is None:
            return False

        documents = self._advice.for_transaction(txn.statement_ref)
        if not documents:
            return False

        usable = [
            doc
            for doc in documents
            if doc.references and (doc.reconciled or not self._cfg.remittance.require_reconciled)
        ]
        if not usable:
            outcome.trail.append(
                Signal(
                    name="remittance_guided",
                    fired=False,
                    detail=(
                        f"{len(documents)} advice document(s) linked, but none usable "
                        "(no invoice references, or the amounts did not reconcile)."
                    ),
                )
            )
            return False

        for document in usable:
            tokens = [
                ReferenceToken(raw=ref, normalized=ref, digits=_digits(ref))
                for ref in document.references
            ]
            resolved, unresolved, _ = self._resolve_references(tokens, outcome.customer.customer_id)
            if not resolved:
                continue

            owners = {item.customer_id for item in resolved}
            if len(owners) > 1:
                continue
            owner = owners.pop()

            if not outcome.customer.resolved:
                outcome.customer = CustomerIdentification(
                    customer_id=owner,
                    method=IdentificationMethod.REFERENCE,
                    score=1.0,
                    matched_on=resolved[0].invoice_number,
                    considered=len(resolved),
                )
            elif owner != outcome.customer.customer_id:
                continue

            settlement = settle(
                resolved,
                txn.amount_paise,
                self._cfg.amount,
                self._cfg.short_pay,
                allow_partial=True,
                allow_short_pay=True,
            )
            if settlement is None:
                continue

            settlement = _apply_advice_reasons(settlement, document)
            outcome.candidates.append(
                _candidate(
                    MatchStrategy.REMITTANCE_GUIDED,
                    settlement,
                    extra=[
                        Signal(
                            name="remittance_guided",
                            fired=True,
                            detail=(
                                f"Advice {document.source_filename or document.remittance_id} "
                                f"({document.method}) names "
                                f"{len(document.references)} invoice(s); "
                                f"{len(resolved)} resolved to open items; "
                                f"{settlement.shape.value} settlement. Extracted amounts "
                                + (
                                    "tie to the credit received."
                                    if document.reconciled
                                    else "do not tie to the credit received, so the "
                                    "references were used as a hint and the arithmetic "
                                    "verified independently."
                                )
                            ),
                            raw={
                                "remittance_id": document.remittance_id,
                                "method": document.method,
                                "reconciled": document.reconciled,
                                "references": document.references,
                                "unresolved": unresolved,
                            },
                        )
                    ],
                )
            )
            outcome.trail.append(outcome.candidates[-1].signals[0])
            return True

        outcome.trail.append(
            Signal(
                name="remittance_guided",
                fired=False,
                detail=(
                    f"{len(usable)} advice document(s) linked, but their invoice "
                    "references did not resolve to open items that settle this amount."
                ),
            )
        )
        return False

    # --- step 3: exact invoice reference ---------------------------------

    def _try_reference(self, txn: TransactionInput, outcome: MatchOutcome) -> bool:
        """Resolve narration references to open items and settle against them."""
        if not outcome.references:
            return False

        resolved, unresolved, ambiguous = self._resolve_references(
            outcome.references, outcome.customer.customer_id
        )

        if not resolved:
            outcome.trail.append(
                Signal(
                    name="reference_exact",
                    fired=False,
                    detail=(
                        f"{len(outcome.references)} reference-like token(s) found but none "
                        "resolved to an open invoice"
                        + (f" ({ambiguous} were ambiguous across customers)." if ambiguous else ".")
                    ),
                    raw={"unresolved": unresolved, "ambiguous": ambiguous},
                )
            )
            return False

        # A reference that resolves identifies the customer even when the
        # payer name did not. This is the single most valuable fallback in
        # the cascade: a mangled name plus a good reference is still a match.
        owners = {item.customer_id for item in resolved}
        if len(owners) > 1:
            outcome.trail.append(
                Signal(
                    name="reference_exact",
                    fired=False,
                    detail=(
                        f"References resolved to invoices belonging to {len(owners)} different "
                        "customers, so the set cannot be a single payment."
                    ),
                    raw=sorted(owners),
                )
            )
            return False

        owner = owners.pop()
        if not outcome.customer.resolved:
            outcome.customer = CustomerIdentification(
                customer_id=owner,
                method=IdentificationMethod.REFERENCE,
                score=1.0,
                matched_on=resolved[0].invoice_number,
                considered=len(resolved),
            )
            outcome.trail.append(
                Signal(
                    name="customer_from_reference",
                    fired=True,
                    detail=(
                        f"Payer name was unrecognisable, but reference "
                        f"{resolved[0].invoice_number} belongs to "
                        f"{self._book.customer_names.get(owner, owner)}."
                    ),
                    raw=owner,
                )
            )
        elif owner != outcome.customer.customer_id:
            outcome.trail.append(
                Signal(
                    name="reference_exact",
                    fired=False,
                    detail=(
                        "References resolve to a different customer than the payer name "
                        "does. Refusing to guess which is right."
                    ),
                    raw={"by_name": outcome.customer.customer_id, "by_reference": owner},
                )
            )
            return False

        settlement = settle(
            resolved,
            txn.amount_paise,
            self._cfg.amount,
            self._cfg.short_pay,
            allow_partial=True,
            allow_short_pay=True,
        )
        if settlement is None:
            outcome.trail.append(
                Signal(
                    name="reference_exact",
                    fired=False,
                    detail=(
                        f"References resolved to "
                        f"{', '.join(item.invoice_number for item in resolved)} totalling "
                        f"{sum(i.open_amount_paise for i in resolved)} paise, which does not "
                        f"relate to the {txn.amount_paise} paise received as an exact, part or "
                        "short payment."
                    ),
                    raw=[item.invoice_number for item in resolved],
                )
            )
            return False

        outcome.candidates.append(
            _candidate(
                MatchStrategy.REFERENCE_EXACT,
                settlement,
                extra=[
                    Signal(
                        name="reference_exact",
                        fired=True,
                        detail=(
                            f"{len(resolved)} reference(s) in the narration resolved to open "
                            f"invoices; {settlement.shape.value} settlement."
                        ),
                        raw=[item.invoice_number for item in resolved],
                    )
                ],
            )
        )
        outcome.trail.append(outcome.candidates[-1].signals[0])
        return True

    def _resolve_references(
        self, references: list[ReferenceToken], customer_id: int | None
    ) -> tuple[list[OpenItem], list[str], int]:
        """Map reference tokens to open items, preferring the known customer."""
        resolved: list[OpenItem] = []
        unresolved: list[str] = []
        ambiguous = 0
        seen: set[int] = set()

        for token in references:
            hits = self._book.lookup_reference(token.normalized, token.digits)
            if not hits:
                unresolved.append(token.normalized)
                continue

            if len(hits) > 1:
                # A bare number can land on several customers' invoices.
                # Prefer the identified customer; without one, refuse.
                narrowed = [item for item in hits if item.customer_id == customer_id]
                if len(narrowed) != 1:
                    ambiguous += 1
                    unresolved.append(token.normalized)
                    continue
                hits = narrowed

            item = hits[0]
            if item.invoice_id not in seen:
                seen.add(item.invoice_id)
                resolved.append(item)

        resolved.sort(key=lambda item: item.invoice_number)
        return resolved, unresolved, ambiguous

    # --- step 4: exact amount --------------------------------------------

    def _try_amount_exact(
        self, txn: TransactionInput, outcome: MatchOutcome, pool: list[OpenItem]
    ) -> bool:
        tolerance = self._cfg.amount.tolerance_paise
        hits = [
            item for item in pool if abs(item.open_amount_paise - txn.amount_paise) <= tolerance
        ]

        if not hits:
            outcome.trail.append(
                Signal(
                    name="amount_exact",
                    fired=False,
                    detail=(
                        f"No single open item of this customer matches {txn.amount_paise} "
                        f"paise within {tolerance} paise tolerance "
                        f"({len(pool)} considered)."
                    ),
                )
            )
            return False

        for item in hits:
            settlement = settle(
                [item],
                txn.amount_paise,
                self._cfg.amount,
                self._cfg.short_pay,
                allow_partial=False,
                allow_short_pay=False,
            )
            if settlement is not None:
                outcome.candidates.append(_candidate(MatchStrategy.AMOUNT_EXACT, settlement))

        detail = (
            f"{len(hits)} open item(s) match the payment amount exactly."
            if len(hits) == 1
            else (
                f"{len(hits)} open items all match {txn.amount_paise} paise exactly. "
                "Nothing in the narration separates them."
            )
        )
        outcome.trail.append(
            Signal(
                name="amount_exact",
                fired=True,
                detail=detail,
                raw=[item.invoice_number for item in hits],
            )
        )
        return bool(outcome.candidates)

    # --- step 5: subset-sum ----------------------------------------------

    def _try_subset_sum(
        self, txn: TransactionInput, outcome: MatchOutcome, pool: list[OpenItem]
    ) -> bool:
        config = self._cfg.subset_sum
        if not config.enabled:
            return False

        tolerance = self._cfg.amount.tolerance_paise
        result = find_subsets(
            pool,
            target_low=txn.amount_paise - tolerance,
            target_high=txn.amount_paise + tolerance,
            # Size 1 is step 4's job; starting at 2 avoids re-reporting it.
            min_size=2,
            max_size=config.max_combination_size,
            max_solutions=config.max_solutions,
            budget=config.combination_budget,
        )
        outcome.diagnostics["subset_sum"] = _search_diagnostics(result, config.max_combination_size)

        if not result.found:
            outcome.trail.append(
                Signal(
                    name="subset_sum",
                    fired=False,
                    detail=(
                        f"No combination of up to {config.max_combination_size} open items "
                        f"totals {txn.amount_paise} paise "
                        f"({result.nodes_visited} combinations examined across a pool of "
                        f"{result.pool_size}"
                        + (", search budget exhausted" if result.budget_exhausted else "")
                        + ")."
                    ),
                )
            )
            return False

        for solution in result.solutions:
            settlement = settle(
                solution,
                txn.amount_paise,
                self._cfg.amount,
                self._cfg.short_pay,
                allow_partial=False,
                allow_short_pay=False,
            )
            if settlement is not None:
                outcome.candidates.append(_candidate(MatchStrategy.SUBSET_SUM, settlement))

        detail = (
            f"{len(result.solutions)} invoice combination(s) total the payment exactly "
            f"(pool of {result.pool_size}, {result.nodes_visited} combinations examined)."
        )
        if len(result.solutions) > 1:
            detail += " Several tie, so no single reading is safe without a reference."
        outcome.trail.append(
            Signal(
                name="subset_sum",
                fired=True,
                detail=detail,
                raw=[[item.invoice_number for item in s] for s in result.solutions],
            )
        )
        return bool(outcome.candidates)

    # --- step 6: short-pay ------------------------------------------------

    def _try_short_pay(
        self, txn: TransactionInput, outcome: MatchOutcome, pool: list[OpenItem]
    ) -> bool:
        config = self._cfg.short_pay
        if not config.enabled:
            return False

        low, high = deduction_band(txn.amount_paise, config)
        result = find_subsets(
            pool,
            target_low=low,
            target_high=high,
            min_size=1,
            max_size=config.max_combination_size,
            max_solutions=config.max_solutions,
            budget=config.combination_budget,
        )
        outcome.diagnostics["short_pay"] = _search_diagnostics(result, config.max_combination_size)

        if not result.found:
            outcome.trail.append(
                Signal(
                    name="short_pay",
                    fired=False,
                    detail=(
                        f"No combination of up to {config.max_combination_size} open items "
                        f"exceeds the payment by a plausible deduction "
                        f"({config.min_deduction_pct}% to {config.max_deduction_pct}%, "
                        f"i.e. a total between {low} and {high} paise)."
                    ),
                )
            )
            return False

        # Prefer the reading with the smallest claim: a 2% deduction is far
        # more likely than a 14% one, and ordering here keeps the leading
        # candidate the most plausible.
        ordered = sorted(
            result.solutions,
            key=lambda s: (
                sum(item.open_amount_paise for item in s),
                [item.invoice_number for item in s],
            ),
        )
        for solution in ordered:
            settlement = settle(
                solution,
                txn.amount_paise,
                self._cfg.amount,
                self._cfg.short_pay,
                allow_partial=False,
                allow_short_pay=True,
            )
            if settlement is not None and settlement.shape is SettlementShape.SHORT_PAY:
                outcome.candidates.append(_candidate(MatchStrategy.SHORT_PAY, settlement))

        if not outcome.candidates:
            outcome.trail.append(
                Signal(
                    name="short_pay",
                    fired=False,
                    detail=(
                        f"{len(result.solutions)} candidate set(s) fell in the search band but "
                        "none left a gap credible as a claim against a single invoice "
                        f"({config.min_deduction_pct}% to {config.max_deduction_pct}% of the "
                        "invoice bearing it)."
                    ),
                )
            )
            return False

        outcome.trail.append(
            Signal(
                name="short_pay",
                fired=True,
                detail=(
                    f"{len(outcome.candidates)} candidate set(s) exceed the payment by a "
                    f"plausible deduction; smallest claim is "
                    f"{outcome.candidates[0].deduction_paise} paise."
                ),
                raw=[candidate.invoice_numbers for candidate in outcome.candidates],
            )
        )
        return True

    # --- plumbing ---------------------------------------------------------

    def _finish(self, outcome: MatchOutcome, started: float) -> MatchOutcome:
        outcome.diagnostics["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return outcome


def _digits(reference: str) -> str:
    """Numeric tail of a canonical reference, for the bare-number fallback."""
    return "".join(char for char in reference if char.isdigit())


def _apply_advice_reasons(settlement: Settlement, document) -> Settlement:
    """Fill in deduction reason codes the advice document supplied.

    The matcher detects *that* money was withheld; only the advice says
    *why*. Phase 3 books those gaps as ``unknown``, and this is where they
    acquire a real category -- which is what lets a claim be routed to
    logistics rather than sitting in a review queue.

    Two places the reason can come from, in order of specificity:

    1. **The line.** "INV-00746 | 2,04,931.43 | less 12,250.00 damage" names
       both the invoice and the reason.
    2. **The document.** "Rs. 4,900 deducted - 12 cases damaged in transit"
       on its own line gives the reason without saying which invoice bears
       it. That is the common shape in practice, and the matcher has already
       worked out the bearer from the open items -- so the category applies
       to whichever line carries the gap.
    """
    payload = document.payload
    by_reference = {line.normalized_reference: line for line in payload.lines}
    best = payload.claimed_reason
    if best is None:
        return settlement

    note = payload.document_deduction_note
    for line in payload.lines:
        if line.deduction_reason == best and line.deduction_note:
            note = line.deduction_note
            break

    for allocation in settlement.allocations:
        if not allocation.deduction_amount_paise:
            continue

        line = by_reference.get(allocation.item.normalized_number)
        reason = line.deduction_reason if line is not None else None

        # The document named *this* invoice as carrying a claim, so the
        # attribution is asserted rather than inferred by the matcher.
        if line is not None and line.deduction_amount_paise:
            allocation.bearer_asserted = True

        # Anything specific beats "unknown", wherever the document states it.
        if reason in (None, DeductionReason.UNKNOWN):
            reason = best

        allocation.deduction_reason = reason
        line_note = line.deduction_note if line is not None else None
        chosen_note = line_note or note
        if chosen_note:
            settlement.note += f" Advice gives the reason as: {chosen_note}"

    return settlement


def _candidate(
    strategy: MatchStrategy, settlement: Settlement, extra: list[Signal] | None = None
) -> MatchCandidate:
    signals = list(extra or [])
    signals.append(
        Signal(
            name="settlement_shape",
            fired=True,
            detail=settlement.note,
            raw=settlement.shape.value,
        )
    )
    return MatchCandidate(
        strategy=strategy,
        allocations=settlement.allocations,
        signals=signals,
        note=settlement.note,
    )


def _customer_signal(customer: CustomerIdentification, book: OpenItemBook) -> Signal:
    if customer.method is IdentificationMethod.AMBIGUOUS:
        return Signal(
            name="customer_identified",
            fired=False,
            detail=(
                f"Payer name is close to both {customer.matched_on!r} "
                f"({customer.score:.0%}) and {customer.runner_up!r} "
                f"({customer.runner_up_score:.0%}). Too close to call."
            ),
            raw=None,
        )
    if not customer.resolved:
        return Signal(
            name="customer_identified",
            fired=False,
            detail=(
                "Payer name matches no customer, alias or close variant"
                + (
                    f" (best was {customer.matched_on!r} at {customer.score:.0%})."
                    if customer.matched_on
                    else "."
                )
            ),
        )
    name = book.customer_names.get(customer.customer_id, str(customer.customer_id))
    detail = {
        IdentificationMethod.EXACT: f"Payer name matches {name} exactly after normalisation.",
        IdentificationMethod.ALIAS: f"Payer name matches a known alias of {name}.",
        IdentificationMethod.FUZZY: (
            f"Payer name is a {customer.score:.0%} fuzzy match for {name} "
            f"(via {customer.matched_on!r})."
        ),
        IdentificationMethod.REFERENCE: f"Customer recovered from an invoice reference: {name}.",
    }[customer.method]
    return Signal(
        name="customer_identified",
        fired=True,
        detail=detail,
        raw={"customer_id": customer.customer_id, "method": customer.method.value},
    )


def _search_diagnostics(result, max_size: int) -> dict[str, object]:
    """What the search cost, so the pruning can be seen rather than trusted."""
    return {
        "pool_size": result.pool_size,
        "nodes_visited": result.nodes_visited,
        "branches_pruned": result.branches_pruned,
        "combinations_admitted_by_cap": theoretical_space(result.pool_size, max_size),
        "budget_exhausted": result.budget_exhausted,
        "solutions": len(result.solutions),
    }
