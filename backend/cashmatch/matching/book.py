"""The open-item book: every candidate the matcher is allowed to consider,
loaded once and indexed for the lookups the pipeline actually performs.

Loading the whole book into memory is a deliberate choice at this scale. Two
thousand invoices is a rounding error of RAM, and it turns the matcher into a
pure function over an in-memory structure -- which means every strategy can be
unit tested without a database. The note at the bottom of this module says
what to do when that stops being true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.matching.candidates import OpenItem
from cashmatch.matching.config import DateWindowConfig
from cashmatch.models import Customer, CustomerAlias, Invoice
from cashmatch.models.invoice import OPEN_STATUSES


@dataclass(slots=True)
class OpenItemBook:
    """Indexed view of the open items and the customer master."""

    items: list[OpenItem] = field(default_factory=list)

    #: normalised customer name -> customer id
    by_customer_name: dict[str, int] = field(default_factory=dict)
    #: normalised alias -> customer id
    by_alias: dict[str, int] = field(default_factory=dict)
    #: customer id -> their open items, oldest invoice first
    by_customer: dict[int, list[OpenItem]] = field(default_factory=dict)
    #: normalised invoice number ("INV42") -> the open item
    by_reference: dict[str, OpenItem] = field(default_factory=dict)
    #: digits only ("42") -> open items, for bare-number references
    by_digits: dict[str, list[OpenItem]] = field(default_factory=dict)
    #: customer id -> legal name, for readable explanations
    customer_names: dict[int, str] = field(default_factory=dict)

    # Parallel arrays for rapidfuzz, built once rather than per transaction.
    fuzzy_choices: list[str] = field(default_factory=list)
    fuzzy_owners: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, session: Session) -> OpenItemBook:
        """Read the customer master and every open item from the database."""
        book = cls()

        for customer in session.scalars(select(Customer)).all():
            book.customer_names[customer.id] = customer.legal_name
            book.by_customer_name[customer.normalized_name] = customer.id
            book.by_customer.setdefault(customer.id, [])

        for alias in session.scalars(select(CustomerAlias)).all():
            # First writer wins: a normalised alias shared by two customers is
            # not evidence for either, so it must not silently pick one.
            book.by_alias.setdefault(alias.normalized_alias, alias.customer_id)

        invoices = session.scalars(
            select(Invoice)
            .where(Invoice.status.in_(OPEN_STATUSES))
            .where(Invoice.open_amount_paise > 0)
            .order_by(Invoice.invoice_date, Invoice.invoice_number)
        ).all()

        for invoice in invoices:
            item = OpenItem(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                normalized_number=invoice.normalized_number,
                customer_id=invoice.customer_id,
                invoice_date=invoice.invoice_date,
                due_date=invoice.due_date,
                open_amount_paise=invoice.open_amount_paise,
            )
            book.add(item)

        book._build_fuzzy_index()
        return book

    def add(self, item: OpenItem) -> None:
        """Register one open item in every index. Used by load() and tests."""
        self.items.append(item)
        self.by_customer.setdefault(item.customer_id, []).append(item)
        self.by_reference.setdefault(item.normalized_number, item)

        digits = "".join(char for char in item.normalized_number if char.isdigit())
        if digits:
            self.by_digits.setdefault(digits.lstrip("0") or "0", []).append(item)

    def register_customer(self, customer_id: int, normalized_name: str, legal_name: str) -> None:
        """Add a customer to the index. Used by tests building a book by hand."""
        self.customer_names[customer_id] = legal_name
        self.by_customer_name[normalized_name] = customer_id
        self.by_customer.setdefault(customer_id, [])

    def register_alias(self, customer_id: int, normalized_alias: str) -> None:
        self.by_alias.setdefault(normalized_alias, customer_id)

    def _build_fuzzy_index(self) -> None:
        """Flatten names and aliases into the arrays rapidfuzz scans.

        Built once per run rather than per transaction: with 800 payments
        that is the difference between 800 index builds and one.
        """
        self.fuzzy_choices = []
        self.fuzzy_owners = []
        for name, customer_id in self.by_customer_name.items():
            self.fuzzy_choices.append(name)
            self.fuzzy_owners.append(customer_id)
        for alias, customer_id in self.by_alias.items():
            self.fuzzy_choices.append(alias)
            self.fuzzy_owners.append(customer_id)

    def finalise(self) -> OpenItemBook:
        """Rebuild derived indexes after hand-assembling a book. Returns self."""
        self._build_fuzzy_index()
        return self

    # --- candidate selection ---------------------------------------------

    def candidates_for(
        self,
        customer_id: int,
        value_date: date,
        window: DateWindowConfig,
        limit: int | None = None,
    ) -> list[OpenItem]:
        """Open items this customer could plausibly be settling.

        Two prunes, applied before any search runs:

        1. **Customer.** Cuts the pool from every invoice in the business to
           this customer's open items -- typically 14 to 200 here.
        2. **Date window.** An invoice raised fourteen months ago is not what
           this transfer settles.

        A third cap (``limit``) keeps the pool bounded for the handful of key
        accounts with very deep books, keeping the invoices closest in time
        to the payment.
        """
        earliest = value_date - timedelta(days=window.days_before_payment)
        latest = value_date + timedelta(days=window.days_after_payment)

        pool = [
            item
            for item in self.by_customer.get(customer_id, ())
            if earliest <= item.invoice_date <= latest
        ]

        if limit is not None and len(pool) > limit:
            # Nearest in time first, then a stable tiebreak so the same
            # payment always sees the same pool.
            pool.sort(
                key=lambda item: (abs((item.due_date - value_date).days), item.invoice_number)
            )
            pool = pool[:limit]

        pool.sort(key=lambda item: (item.invoice_date, item.invoice_number))
        return pool

    def lookup_reference(self, normalized: str, digits: str) -> list[OpenItem]:
        """Resolve a reference token to open items.

        Tries the full normalised form first (``INV42``), then falls back to
        the digits alone (``42``) for references typed without a prefix. The
        digits path can return several items across different customers,
        which the caller must then disambiguate -- an unresolvable ambiguity
        is a reason to send the payment to review, not to guess.
        """
        exact = self.by_reference.get(normalized)
        if exact is not None:
            return [exact]
        if not digits:
            return []
        return list(self.by_digits.get(digits.lstrip("0") or "0", ()))


# Scaling note: this design holds while the open-item book fits comfortably in
# memory. Past roughly a million open items, replace `candidates_for` with an
# indexed query (customer_id, status, invoice_date) and keep the rest of the
# engine unchanged -- every strategy already takes its candidate pool as an
# argument rather than reaching for the book itself.
