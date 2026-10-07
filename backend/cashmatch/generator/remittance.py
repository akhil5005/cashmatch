"""Remittance advice: the messy half of the input.

Advice documents are deliberately inconsistent, because that is what Phase 4's
extractor has to survive. Across the generated corpus you get prose and
tables, Indian and Western digit grouping, amounts with and without currency
markers, invoice numbers written four different ways, and a minority that
quote the bank reference so the link to the payment is explicit. The rest
must be paired with their payment by inference.

Emails are written to disk as ``.txt`` alongside the PDFs so Phase 4 has real
files to read rather than only database rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from random import Random

from faker import Faker

from cashmatch.generator.config import GeneratorConfig, ScenarioLabel
from cashmatch.generator.scenarios import PaymentSpec
from cashmatch.generator.vocabulary import DEDUCTION_NOTES, PRODUCT_CATEGORIES
from cashmatch.models.enums import RemittanceSource
from cashmatch.money import format_inr, paise_to_rupees

REMITTANCE_DIRNAME = "remittances"


@dataclass(slots=True)
class RemittanceSpec:
    """One advice document, ready to be written to disk and to the database."""

    ref: str
    source_type: RemittanceSource
    raw_text: str
    source_filename: str
    received_at: datetime
    statement_ref: str | None
    carries_bank_reference: bool


# --- amount rendering -------------------------------------------------------


def _money_text(rng: Random, paise: int) -> str:
    """Render an amount the way a particular accounts clerk happens to.

    Four conventions appear across the corpus. An extractor that only handles
    one of them will visibly underperform, which is the point.
    """
    plain = f"{paise_to_rupees(paise):.2f}"
    choice = rng.randint(0, 3)

    if choice == 0:
        return f"Rs. {format_inr(paise, symbol=False)}"
    if choice == 1:
        return plain
    if choice == 2:
        return f"{format_inr(paise, symbol=False)}/-"
    return f"INR {paise_to_rupees(paise):,.2f}"


def _reference_text(rng: Random, invoice_number: str) -> str:
    """How the customer writes the invoice number in their own document."""
    digits = invoice_number.split("-")[-1].lstrip("0") or "0"
    choice = rng.randint(0, 3)

    if choice == 0:
        return invoice_number
    if choice == 1:
        return f"INV {digits}"
    if choice == 2:
        return invoice_number.replace("-", "/")
    return f"Inv. No. {digits}"


def _deduction_sentence(rng: Random, payment: PaymentSpec) -> str:
    """Prose explaining the claim, in the customer's own words."""
    deducted = [a for a in payment.allocations if a.deduction_amount_paise > 0]
    if not deducted:
        return ""

    allocation = deducted[0]
    reason = allocation.deduction_reason
    template = rng.choice(DEDUCTION_NOTES[reason.value])
    note = template.format(n=rng.randint(2, 40), product=rng.choice(PRODUCT_CATEGORIES))
    return f"{_money_text(rng, allocation.deduction_amount_paise)} deducted - {note}."


# --- email bodies -----------------------------------------------------------


def _email_itemised(rng: Random, payment: PaymentSpec, faker: Faker, party_name: str) -> str:
    lines = [
        f"Subject: Payment advice - {payment.value_date:%d.%m.%Y}",
        "",
        "Dear Sir/Madam,",
        "",
        f"We have remitted {_money_text(rng, payment.amount_paise)} towards the following bills.",
        "",
    ]
    for allocation in payment.allocations:
        reference = _reference_text(rng, allocation.invoice_number)
        gross = allocation.allocated_amount_paise + allocation.deduction_amount_paise
        lines.append(f"  {reference}    {_money_text(rng, gross)}")

    claim = _deduction_sentence(rng, payment)
    if claim:
        lines += ["", claim]

    lines += [
        "",
        "Kindly acknowledge and update our ledger.",
        "",
        "Regards,",
        faker.name(),
        f"Accounts Department, {party_name}",
    ]
    return "\n".join(lines)


def _email_table(rng: Random, payment: PaymentSpec, faker: Faker, party_name: str) -> str:
    rows = ["Invoice        | Amount        | Remarks", "-" * 52]
    for allocation in payment.allocations:
        gross = allocation.allocated_amount_paise + allocation.deduction_amount_paise
        remark = ""
        if allocation.deduction_amount_paise:
            remark = (
                f"less {_money_text(rng, allocation.deduction_amount_paise)} "
                f"{allocation.deduction_reason.value}"
            )
        rows.append(f"{allocation.invoice_number:<14} | {_money_text(rng, gross):<13} | {remark}")

    return "\n".join(
        [
            f"Subject: RTGS done - {_money_text(rng, payment.amount_paise)}",
            "",
            "Hi,",
            "",
            "Payment released today. Details below.",
            "",
            *rows,
            "",
            f"Total transferred: {_money_text(rng, payment.amount_paise)}",
            "",
            "Thanks,",
            f"{faker.name()} | {party_name}",
        ]
    )


def _email_terse(rng: Random, payment: PaymentSpec, faker: Faker, party_name: str) -> str:
    references = ", ".join(_reference_text(rng, a.invoice_number) for a in payment.allocations)
    claim = _deduction_sentence(rng, payment)
    body = [
        "Subject: payment",
        "",
        f"pls note we have paid {_money_text(rng, payment.amount_paise)} today "
        f"against {references}.",
    ]
    if claim:
        body.append(claim.lower())
    body += ["", f"{faker.first_name()}", party_name]
    return "\n".join(body)


def _email_forwarded(rng: Random, payment: PaymentSpec, faker: Faker, party_name: str) -> str:
    """A forwarded thread, quote markers and all. Realistically horrible."""
    references = " / ".join(a.invoice_number for a in payment.allocations)
    return "\n".join(
        [
            "Subject: Fwd: Re: payment status",
            "",
            "---------- Forwarded message ----------",
            f"From: {faker.name()} <accounts@example.in>",
            f"Date: {payment.value_date:%a, %d %b %Y}",
            "",
            f"> could you confirm when payment for {references} will be released?",
            "",
            f"Sir, transfer of {_money_text(rng, payment.amount_paise)} has been "
            "done from our side. Please check and confirm.",
            "",
            _deduction_sentence(rng, payment),
            "",
            party_name,
        ]
    )


_EMAIL_BUILDERS = (_email_itemised, _email_table, _email_terse, _email_forwarded)


# --- PDF --------------------------------------------------------------------


def _write_pdf(path: Path, payment: PaymentSpec, party_name: str, rng: Random) -> str:
    """Render a simple remittance advice PDF and return its text content.

    The returned text is what the database stores as ``raw_text``; Phase 4
    re-extracts it from the PDF with pdfplumber, so the two can be compared.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    _page_width, height = A4
    pdf = canvas.Canvas(str(path), pagesize=A4)

    lines: list[tuple[str, bool]] = [
        ("REMITTANCE ADVICE", True),
        ("", False),
        (party_name, False),
        (f"Date: {payment.value_date:%d-%b-%Y}", False),
        (f"Payment reference: {payment.statement_ref}", False),
        ("", False),
        ("Invoice            Gross            Deduction        Net paid", True),
    ]
    for allocation in payment.allocations:
        gross = allocation.allocated_amount_paise + allocation.deduction_amount_paise
        deduction = (
            format_inr(allocation.deduction_amount_paise, symbol=False)
            if allocation.deduction_amount_paise
            else "-"
        )
        lines.append(
            (
                f"{allocation.invoice_number:<18} "
                f"{format_inr(gross, symbol=False):<16} "
                f"{deduction:<16} "
                f"{format_inr(allocation.allocated_amount_paise, symbol=False)}",
                False,
            )
        )

    claim = _deduction_sentence(rng, payment)
    lines += [
        ("", False),
        (f"Total remitted: {format_inr(payment.amount_paise, symbol=False)}", True),
    ]
    if claim:
        lines += [("", False), (claim, False)]

    y = height - 70
    for text, bold in lines:
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        pdf.drawString(55, y, text)
        y -= 16

    pdf.showPage()
    pdf.save()

    return "\n".join(text for text, _ in lines)


# --- orchestration ----------------------------------------------------------


def build_remittances(
    rng: Random,
    config: GeneratorConfig,
    payments: list[PaymentSpec],
    party_names: dict[str, str],
    output_dir: Path,
) -> list[RemittanceSpec]:
    """Create advice documents for a configured share of the payments.

    Payments with nothing to settle (``no_matching_invoice``) get no advice,
    which is exactly why they are hard: there is nothing to explain them.
    """
    faker = Faker("en_IN")
    Faker.seed(config.seed)

    directory = output_dir / REMITTANCE_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)

    specs: list[RemittanceSpec] = []
    for payment in payments:
        if payment.scenario is ScenarioLabel.NO_MATCHING_INVOICE:
            continue
        if rng.random() * 100 >= config.remittance.coverage_pct:
            continue

        ref = f"RMT-{len(specs) + 1:05d}"
        party_name = party_names.get(payment.customer_code or "", "Customer")
        explicit = rng.random() * 100 < config.remittance.explicit_link_share_pct
        is_pdf = rng.random() * 100 < config.remittance.pdf_share_pct

        if is_pdf:
            filename = f"{ref}.pdf"
            raw_text = _write_pdf(directory / filename, payment, party_name, rng)
            source_type = RemittanceSource.PDF
        else:
            filename = f"{ref}.txt"
            builder = rng.choice(_EMAIL_BUILDERS)
            raw_text = builder(rng, payment, faker, party_name)
            if explicit:
                raw_text += f"\n\nBank reference: {payment.statement_ref}"
            (directory / filename).write_text(raw_text, encoding="utf-8")
            source_type = RemittanceSource.EMAIL

        # Advice rarely arrives on the same day as the money.
        received_at = datetime.combine(
            payment.value_date + timedelta(days=rng.randint(-2, 3)),
            time(hour=rng.randint(9, 19), minute=rng.randint(0, 59)),
        )

        payment.remittance_refs.append(ref)
        specs.append(
            RemittanceSpec(
                ref=ref,
                source_type=source_type,
                raw_text=raw_text,
                source_filename=filename,
                received_at=received_at,
                statement_ref=payment.statement_ref,
                # A PDF always prints the payment reference in its header.
                carries_bank_reference=explicit or is_pdf,
            )
        )

    return specs
