"""Test fixtures.

Tests run against in-memory SQLite, not Postgres. That is a deliberate
trade-off: the suite finishes in seconds and needs no Docker, at the cost of
constraining the schema to types both engines support (see
``cashmatch/db/types.py``). The constraint is cheap and the fast feedback
loop is worth a lot.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import cashmatch.models  # noqa: F401  (registers every table on Base.metadata)
from cashmatch.db.base import Base
from cashmatch.models import BankTransaction, Customer, Invoice
from cashmatch.money import rupees_to_paise
from cashmatch.normalize import normalize_narration, normalize_party_name, normalize_reference


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """One in-memory SQLite database for the whole session."""
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        # SQLite ignores foreign keys unless asked. Without this the cascade
        # and referential tests would pass vacuously.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session inside a transaction that is rolled back after each test, so
    tests stay isolated without recreating the schema every time.

    ``join_transaction_mode="create_savepoint"`` makes the session work inside
    a SAVEPOINT. Several tests deliberately provoke an IntegrityError; without
    the savepoint that failure would tear down the outer transaction and the
    teardown rollback would have nothing left to roll back.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# --- Small builders so tests read as domain scenarios, not ORM boilerplate ---


@pytest.fixture
def customer(session: Session) -> Customer:
    c = Customer(
        code="CUST-0001",
        legal_name="A.B.C. Traders Private Limited",
        normalized_name=normalize_party_name("A.B.C. Traders Private Limited"),
        city="Pune",
        payment_terms_days=30,
    )
    session.add(c)
    session.flush()
    return c


def make_invoice(
    session: Session,
    customer: Customer,
    number: str = "INV-00042",
    rupees: str = "41850.00",
    open_rupees: str | None = None,
) -> Invoice:
    amount = rupees_to_paise(rupees)
    inv = Invoice(
        invoice_number=number,
        normalized_number=normalize_reference(number),
        customer_id=customer.id,
        invoice_date=date(2026, 1, 10),
        due_date=date(2026, 2, 9),
        amount_paise=amount,
        open_amount_paise=rupees_to_paise(open_rupees) if open_rupees else amount,
    )
    session.add(inv)
    session.flush()
    return inv


def make_transaction(
    session: Session,
    rupees: str = "41850.00",
    payer: str = "ABC TRADERS",
    narration: str = "NEFT INV-00042",
    ref: str = "UTR0000000001",
) -> BankTransaction:
    txn = BankTransaction(
        statement_ref=ref,
        value_date=date(2026, 2, 11),
        amount_paise=rupees_to_paise(rupees),
        payer_name_raw=payer,
        payer_name_normalized=normalize_party_name(payer),
        narration=narration,
        normalized_narration=normalize_narration(narration),
    )
    session.add(txn)
    session.flush()
    return txn
