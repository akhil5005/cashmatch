"""Builders for matching tests.

The engine takes its open-item book and config as arguments rather than
reaching for a database, which is what makes these helpers possible: every
strategy can be exercised against a hand-built book of three invoices.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cashmatch.matching import MatchingConfig, OpenItem, OpenItemBook, TransactionInput
from cashmatch.normalize import normalize_narration, normalize_party_name, normalize_reference

MATCHING_CONFIG = Path(__file__).resolve().parents[1] / "config" / "matching.yml"

DEFAULT_VALUE_DATE = date(2026, 3, 15)


def load_config() -> MatchingConfig:
    """The shipped matching config, so tests exercise the real defaults."""
    return MatchingConfig.from_yaml(MATCHING_CONFIG)


def make_item(
    number: int,
    rupees: str | int,
    *,
    customer_id: int = 1,
    invoice_date: date = date(2026, 2, 1),
    due_days: int = 30,
) -> OpenItem:
    """One open item. Amount given in rupees for readability, stored as paise."""
    from datetime import timedelta

    from cashmatch.money import rupees_to_paise

    invoice_number = f"INV-{number:05d}"
    return OpenItem(
        invoice_id=number,
        invoice_number=invoice_number,
        normalized_number=normalize_reference(invoice_number),
        customer_id=customer_id,
        invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=due_days),
        open_amount_paise=rupees_to_paise(str(rupees)),
    )


def make_book(
    items: list[OpenItem] | None = None,
    customers: dict[int, str] | None = None,
    aliases: dict[str, int] | None = None,
) -> OpenItemBook:
    """Assemble a book by hand, with every index built.

    Args:
        items: open items to register.
        customers: customer id -> legal name. Normalised names are derived,
            so tests state the realistic name and get the real normalisation.
        aliases: raw alias -> customer id.
    """
    book = OpenItemBook()

    if customers is None:
        customers = {1: "ABC Traders Pvt Ltd"}

    for customer_id, legal_name in customers.items():
        book.register_customer(customer_id, normalize_party_name(legal_name), legal_name)

    for alias, customer_id in (aliases or {}).items():
        book.register_alias(customer_id, normalize_party_name(alias))

    for item in items or []:
        book.add(item)

    return book.finalise()


def make_txn(
    rupees: str | int,
    *,
    payer: str = "ABC Traders Pvt Ltd",
    narration: str = "",
    value_date: date = DEFAULT_VALUE_DATE,
    statement_ref: str = "UTR-TEST-0001",
) -> TransactionInput:
    """A bank credit, normalised the same way the ingest path would."""
    from cashmatch.money import rupees_to_paise

    return TransactionInput(
        statement_ref=statement_ref,
        amount_paise=rupees_to_paise(str(rupees)),
        value_date=value_date,
        payer_name_normalized=normalize_party_name(payer),
        normalized_narration=normalize_narration(narration),
    )
