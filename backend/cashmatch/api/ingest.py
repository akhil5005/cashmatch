"""Reading uploaded bank statements and remittance advice.

CSV is a placeholder for the real thing. A production deployment receives
CAMT.053 or MT940 from the bank, and swapping this module for a parser of
those formats is the whole of that change -- everything downstream takes
:class:`BankTransaction` rows and does not care where they came from.

The parsing here is deliberately forgiving about *shape* and strict about
*money*. A missing optional column should not cost someone their upload; an
amount that cannot be read exactly must never be guessed at.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.models import BankTransaction, Remittance
from cashmatch.models.enums import RemittanceSource, TransactionStatus
from cashmatch.money import rupees_to_paise
from cashmatch.normalize import normalize_narration, normalize_party_name

#: Accepted spellings for each column, lowercased. Banks and ERPs disagree
#: about everything, including what to call a date.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "statement_ref": ("statement_ref", "utr", "reference", "transaction_id", "txn_id", "ref"),
    "value_date": ("value_date", "date", "transaction_date", "posting_date", "txn_date"),
    "amount": ("amount", "credit", "credit_amount", "amount_inr", "value"),
    "payer_name": ("payer_name", "payer", "remitter", "customer", "beneficiary", "name"),
    "narration": ("narration", "description", "remarks", "particulars", "details", "memo"),
    "bank_account": ("bank_account", "account", "account_number", "our_account"),
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
)


@dataclass(slots=True)
class IngestResult:
    rows_read: int = 0
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class IngestError(ValueError):
    """The file cannot be read at all, with a reason the user can act on."""


def load_bank_statement(session: Session, content: bytes, filename: str) -> IngestResult:
    """Parse a bank statement CSV and create unmatched transactions.

    Re-uploading the same file is safe: ``statement_ref`` is unique, so a
    payment already present is skipped rather than duplicated. That is the
    whole reason the column exists.
    """
    text = _decode(content, filename)
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        raise IngestError(
            f"{filename} has no header row. The first line should name the columns, "
            "for example: statement_ref,value_date,amount,payer_name,narration"
        )

    mapping = _map_columns(reader.fieldnames)
    missing = [
        name
        for name in ("statement_ref", "value_date", "amount", "payer_name")
        if name not in mapping
    ]
    if missing:
        raise IngestError(
            f"{filename} is missing required column(s): {', '.join(missing)}. "
            f"Found: {', '.join(reader.fieldnames)}. Accepted names for each are listed "
            "in the API docs."
        )

    existing = set(session.scalars(select(BankTransaction.statement_ref)).all())
    result = IngestResult()

    for line_number, row in enumerate(reader, start=2):
        result.rows_read += 1
        try:
            transaction = _row_to_transaction(row, mapping)
        except ValueError as exc:
            result.skipped += 1
            if len(result.errors) < 25:
                result.errors.append(f"Row {line_number}: {exc}")
            continue

        if transaction.statement_ref in existing:
            result.skipped += 1
            continue

        existing.add(transaction.statement_ref)
        session.add(transaction)
        result.created += 1

    session.flush()
    return result


def load_remittance(session: Session, content: bytes, filename: str) -> IngestResult:
    """Store an uploaded advice document, ready for extraction.

    It arrives unlinked, which is the honest state: pairing advice with a
    payment is matching work, not an upload detail.
    """
    result = IngestResult(rows_read=1)
    is_pdf = filename.lower().endswith(".pdf")

    if is_pdf:
        text = _pdf_text(content, filename)
        source = RemittanceSource.PDF
    else:
        text = _decode(content, filename)
        source = RemittanceSource.EMAIL

    if not text.strip():
        raise IngestError(
            f"{filename} contains no readable text. A scanned PDF with no text layer "
            "needs OCR, which this build does not do."
        )

    session.add(
        Remittance(
            source_type=source,
            raw_text=text,
            source_filename=filename,
            received_at=datetime.now(),
        )
    )
    session.flush()
    result.created = 1
    return result


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _decode(content: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError(
        f"{filename} is not readable as text in UTF-8 or Windows-1252. If it is a "
        "spreadsheet, export it as CSV first."
    )


def _pdf_text(content: bytes, filename: str) -> str:
    import tempfile
    from pathlib import Path

    from cashmatch.extraction.documents import read_pdf_text

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / filename
        path.write_bytes(content)
        return read_pdf_text(path)


def _map_columns(fieldnames: list[str]) -> dict[str, str]:
    """Match the file's headers to the fields we need, however they are spelled."""
    lookup = {name.strip().lower().replace(" ", "_"): name for name in fieldnames if name}
    mapping: dict[str, str] = {}
    for field_name, candidates in _COLUMNS.items():
        for candidate in candidates:
            if candidate in lookup:
                mapping[field_name] = lookup[candidate]
                break
    return mapping


def _row_to_transaction(row: dict[str, str], mapping: dict[str, str]) -> BankTransaction:
    statement_ref = (row.get(mapping["statement_ref"]) or "").strip()
    if not statement_ref:
        raise ValueError("the bank reference is blank, so this payment cannot be identified.")

    payer = (row.get(mapping["payer_name"]) or "").strip()
    if not payer:
        raise ValueError("the payer name is blank.")

    amount_paise = _parse_amount(row.get(mapping["amount"]))
    value_date = _parse_date(row.get(mapping["value_date"]))
    narration = (row.get(mapping.get("narration", "")) or "").strip()
    account = (row.get(mapping.get("bank_account", "")) or "").strip() or None

    return BankTransaction(
        statement_ref=statement_ref[:64],
        value_date=value_date,
        amount_paise=amount_paise,
        payer_name_raw=payer[:255],
        payer_name_normalized=normalize_party_name(payer),
        narration=narration,
        normalized_narration=normalize_narration(narration),
        bank_account=account,
        status=TransactionStatus.UNMATCHED,
    )


def _parse_amount(raw: str | None) -> int:
    """Read a credit amount exactly, or refuse it.

    Never a float, and never a guess: an amount that cannot be represented
    exactly is a row the user has to fix, not one to round.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("the amount is blank.")

    cleaned = text.replace(",", "").replace("₹", "").strip()
    for marker in ("INR", "Rs.", "Rs", "CR", "/-"):
        cleaned = cleaned.replace(marker, "").replace(marker.lower(), "")
    cleaned = cleaned.strip()

    try:
        paise = rupees_to_paise(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f'could not read {text!r} as a rupee amount. Expected something like "41850.00".'
        ) from exc

    if paise <= 0:
        raise ValueError(
            f"the amount {text!r} is not a credit. This endpoint takes incoming payments only."
        )
    return paise


def _parse_date(raw: str | None) -> date:
    text = (raw or "").strip()
    if not text:
        raise ValueError("the value date is blank.")

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    raise ValueError(
        f"could not read {text!r} as a date. Accepted formats include YYYY-MM-DD and DD/MM/YYYY."
    )
