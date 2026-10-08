"""The contract an extractor must satisfy.

Two layers, deliberately.

:class:`RemittanceDraft` is the **wire format** -- what a model is allowed to
return. Every amount in it is a *string*, because a model emitting JSON will
happily produce ``418500.1`` or ``4.185e5`` and ``json.loads`` will hand back
a float. By the time that float exists the precision damage is done. Strings
force the conversion through :func:`cashmatch.money.rupees_to_paise`, which
rejects anything it cannot represent exactly.

:class:`ExtractedRemittance` is the **validated form** -- integer paise,
canonical references, enum reason codes. Nothing downstream ever sees the
wire format.

That split is rule 3 of the project ("never trust LLM-extracted numbers")
expressed as types rather than as a code review comment.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashmatch.models.enums import DeductionReason
from cashmatch.money import Paise, rupees_to_paise
from cashmatch.normalize import normalize_reference


class Reconciliation(StrEnum):
    """Whether the extracted amounts tie to the money that actually arrived.

    This is the cross-check that makes an LLM safe to use here. A
    hallucinated figure almost never balances against a real bank credit.
    """

    #: The extracted lines account for the credit exactly.
    TIES = "ties"
    #: Plausible but off by more than tolerance. Usable as a hint, not a fact.
    DOES_NOT_TIE = "does_not_tie"
    #: No transaction to compare against yet.
    NOT_CHECKED = "not_checked"


# ---------------------------------------------------------------------------
# wire format: what a model is allowed to return
# ---------------------------------------------------------------------------


class DraftLine(BaseModel):
    """One invoice line as the model reported it. Amounts are strings."""

    model_config = ConfigDict(extra="ignore")

    invoice_reference: str = Field(description="The invoice number exactly as written.")
    gross_amount: str | None = Field(
        default=None, description="What the invoice was for, e.g. '130500.00'."
    )
    paid_amount: str | None = Field(default=None, description="What was actually paid against it.")
    deduction_amount: str | None = Field(default=None, description="Amount withheld, if any.")
    deduction_reason: str | None = Field(
        default=None, description="Why it was withheld, in the document's own words."
    )


class RemittanceDraft(BaseModel):
    """A whole advice document as the model reported it."""

    model_config = ConfigDict(extra="ignore")

    lines: list[DraftLine] = Field(default_factory=list)
    total_amount: str | None = Field(
        default=None, description="The stated total transferred, if the document says."
    )
    unattributed_deduction: str | None = Field(
        default=None,
        description=(
            "A deduction the document states without saying which invoice it "
            "belongs to, e.g. a sentence under the table."
        ),
    )
    unattributed_deduction_reason: str | None = Field(
        default=None,
        description="Why that deduction was taken, in the document's own words.",
    )
    payer_name: str | None = None
    bank_reference: str | None = Field(
        default=None, description="UTR or bank transaction reference, if quoted."
    )

    @staticmethod
    def json_schema_for_prompt() -> dict[str, Any]:
        """The schema handed to the model, with amounts typed as strings.

        Kept as an explicit dict rather than generated from the model so the
        prompt stays readable and the string-typing of amounts is visible to
        anyone reading the prompt.
        """
        return {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "invoice_reference": {"type": "string"},
                            "gross_amount": {"type": "string"},
                            "paid_amount": {"type": "string"},
                            "deduction_amount": {"type": "string"},
                            "deduction_reason": {"type": "string"},
                        },
                        "required": ["invoice_reference"],
                    },
                },
                "total_amount": {"type": "string"},
                "unattributed_deduction": {"type": "string"},
                "unattributed_deduction_reason": {"type": "string"},
                "payer_name": {"type": "string"},
                "bank_reference": {"type": "string"},
            },
            "required": ["lines"],
        }


# ---------------------------------------------------------------------------
# validated form: integer paise, canonical references, enum reasons
# ---------------------------------------------------------------------------


class ExtractedLine(BaseModel):
    """One invoice line, validated."""

    model_config = ConfigDict(extra="forbid")

    invoice_reference: str
    #: ``INV-00042``, ``inv 42`` and ``Invoice No. 42`` all land here as ``INV42``.
    normalized_reference: str
    gross_amount_paise: int | None = Field(default=None, ge=0)
    paid_amount_paise: int | None = Field(default=None, ge=0)
    deduction_amount_paise: int = Field(default=0, ge=0)
    deduction_reason: DeductionReason | None = None
    deduction_note: str | None = None

    @model_validator(mode="after")
    def _amounts_are_coherent(self) -> Self:
        if self.deduction_amount_paise and self.deduction_reason is None:
            self.deduction_reason = DeductionReason.UNKNOWN
        if (
            self.gross_amount_paise is not None
            and self.paid_amount_paise is not None
            and self.paid_amount_paise > self.gross_amount_paise
        ):
            raise ValueError(
                f"{self.invoice_reference}: paid amount ({self.paid_amount_paise} paise) "
                f"exceeds the gross amount ({self.gross_amount_paise} paise). An advice "
                "document claiming to overpay a single invoice is not credible."
            )
        return self

    @property
    def effective_paid_paise(self) -> int | None:
        """What this line says was applied, however the document expressed it.

        Some documents give the gross and the deduction, others give the net
        paid. Both must reduce to the same number or the line is unusable.
        """
        if self.paid_amount_paise is not None:
            return self.paid_amount_paise
        if self.gross_amount_paise is not None:
            return self.gross_amount_paise - self.deduction_amount_paise
        return None


class ExtractedRemittance(BaseModel):
    """A validated advice document, ready to feed the matcher."""

    model_config = ConfigDict(extra="forbid")

    lines: list[ExtractedLine] = Field(default_factory=list)
    total_paise: int | None = Field(default=None, ge=0)
    payer_name: str | None = None
    bank_reference: str | None = None
    currency: str = "INR"

    #: A claim the document states without saying which invoice it sits on.
    #: Customers routinely write "Rs. 4,900 deducted - 12 cases damaged" on
    #: its own line. The claim is real and the reason is useful even when
    #: the attribution is not stated; the matcher decides the bearer.
    document_deduction_paise: int = Field(default=0, ge=0)
    document_deduction_reason: DeductionReason | None = None
    document_deduction_note: str | None = None

    reconciliation: Reconciliation = Reconciliation.NOT_CHECKED
    #: Difference between the extracted total and the bank credit, in paise.
    reconciliation_gap_paise: int | None = None

    @property
    def references(self) -> list[str]:
        """Canonical invoice references, in document order, de-duplicated."""
        seen: set[str] = set()
        ordered: list[str] = []
        for line in self.lines:
            if line.normalized_reference and line.normalized_reference not in seen:
                seen.add(line.normalized_reference)
                ordered.append(line.normalized_reference)
        return ordered

    @property
    def line_deduction_paise(self) -> int:
        """Claims attributed to a specific invoice."""
        return sum(line.deduction_amount_paise for line in self.lines)

    @property
    def unattributed_deduction_paise(self) -> int:
        """A document-level claim that is not already on a line.

        Advice routinely states the same claim twice -- once as a column in
        a table, once as a sentence underneath. Counting both would subtract
        it from the payment twice and make the document fail to reconcile
        against a credit it actually explains perfectly.
        """
        if self.line_deduction_paise:
            return 0
        return self.document_deduction_paise

    @property
    def stated_paid_paise(self) -> int | None:
        """Cash the document says was applied, or None if it never says.

        Per-line claims are already netted off by
        :attr:`ExtractedLine.effective_paid_paise`; only a claim belonging to
        no line is subtracted again here.
        """
        amounts = [
            line.effective_paid_paise
            for line in self.lines
            if line.effective_paid_paise is not None
        ]
        if not amounts:
            return None
        return sum(amounts) - self.unattributed_deduction_paise

    @property
    def deduction_total_paise(self) -> int:
        """Every distinct claim in the document, attributed or not."""
        return self.line_deduction_paise + self.unattributed_deduction_paise

    @property
    def claimed_reason(self) -> DeductionReason | None:
        """The most specific reason code the document gives.

        A table row supplies the claim *amount* but has no words to classify,
        so its line reason is ``unknown``; the sentence underneath supplies
        the *reason* but names no invoice. Returning the line's ``unknown``
        would mask a perfectly good ``pricing`` sitting one line below, so
        anything specific beats ``unknown`` wherever it is stated.
        """
        specific = [
            line.deduction_reason
            for line in self.lines
            if line.deduction_reason not in (None, DeductionReason.UNKNOWN)
        ]
        if specific:
            return specific[0]
        if self.document_deduction_reason not in (None, DeductionReason.UNKNOWN):
            return self.document_deduction_reason
        for line in self.lines:
            if line.deduction_reason is not None:
                return line.deduction_reason
        return self.document_deduction_reason

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def is_trustworthy(self) -> bool:
        """Did the arithmetic check out against the real bank credit?"""
        return self.reconciliation is Reconciliation.TIES


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------


def parse_amount(raw: str | None) -> Paise | None:
    """Turn a money string from a document into integer paise, or None.

    Handles the four conventions that appear across the corpus:
    ``Rs. 4,18,500.00``, ``418500.00``, ``4,18,500.00/-`` and
    ``INR 418,500.00``. Indian and Western digit grouping both reduce to the
    same number once the separators are stripped.

    Returns None rather than raising for anything unparseable: a single bad
    line should degrade that line, not discard the whole document.
    """
    if raw is None:
        return None

    cleaned = str(raw).strip()
    for marker in ("₹", "RS.", "RS", "INR", "/-", "/–"):
        cleaned = cleaned.upper().replace(marker, "")
    cleaned = cleaned.replace(",", "").replace(" ", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        # Accounting negatives. The sign is carried by the field, not the value.
        cleaned = cleaned[1:-1]
    if not cleaned:
        return None

    try:
        value = Decimal(cleaned)
    except Exception:
        return None

    if not value.is_finite():
        return None

    # Documents occasionally carry more precision than paise allow; quantise
    # rather than discard, because the line is still useful.
    if value.as_tuple().exponent < -2:
        value = value.quantize(Decimal("0.01"))

    try:
        return rupees_to_paise(value)
    except (TypeError, ValueError):
        return None


def normalize_line_reference(raw: str) -> str:
    """Canonical form of a reference written inside an advice document."""
    return normalize_reference(raw)
