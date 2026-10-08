"""Extraction orchestration: call, validate, retry once, fall back, cross-check.

The order of these steps is the whole safety argument for putting a language
model anywhere near money:

1. **Call** the provider with a schema-constrained, few-shot prompt.
2. **Validate** the answer against a strict Pydantic model. Amounts arrive as
   strings and are converted through ``rupees_to_paise``, which refuses
   anything it cannot represent exactly.
3. **Retry once**, feeding the validation error back. A blind second call
   usually reproduces the first mistake; a corrected one often does not.
4. **Fall back** to the rule-based extractor. A degraded reading beats none,
   and the method is recorded so Phase 5 can weight it differently.
5. **Cross-check** the extracted amounts against the bank credit that
   actually arrived. This is the step that matters most: a hallucinated
   figure almost never balances against a real payment.

The model gets to *propose* structure. It never gets to decide anything, and
nothing it says is believed without the arithmetic agreeing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from cashmatch.extraction.prompt import build_prompt
from cashmatch.extraction.providers import ExtractionProvider, ProviderError
from cashmatch.extraction.reasons import classify_reason
from cashmatch.extraction.regex_extractor import extract_with_regex
from cashmatch.extraction.schema import (
    DraftLine,
    ExtractedLine,
    ExtractedRemittance,
    Reconciliation,
    RemittanceDraft,
    normalize_line_reference,
    parse_amount,
)
from cashmatch.matching.config import ReferenceConfig
from cashmatch.models.enums import DeductionReason, ExtractionMethod

# Models wrap JSON in markdown fences often enough to be worth stripping
# rather than failing on.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


@dataclass(slots=True)
class ExtractionResult:
    """What extraction produced, and how hard it had to work."""

    payload: ExtractedRemittance
    method: ExtractionMethod
    attempts: int = 0
    error: str | None = None
    #: Every provider answer that failed validation, for debugging.
    rejected: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.payload.is_empty

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form, stored on ``remittances.extracted_payload``."""
        return {
            "method": self.method.value,
            "attempts": self.attempts,
            "error": self.error,
            "payload": self.payload.model_dump(mode="json"),
        }


def extract_remittance(
    text: str,
    *,
    provider: ExtractionProvider | None,
    reference_config: ReferenceConfig,
    amount_paise: int | None = None,
    tolerance_paise: int = 100,
) -> ExtractionResult:
    """Read one advice document into a validated structure.

    Args:
        text: the raw document.
        provider: the model to ask, or None to go straight to rules.
        reference_config: how to recognise an invoice reference.
        amount_paise: the bank credit this advice is believed to accompany.
            When given, the extraction is cross-checked against it.
        tolerance_paise: slack on that cross-check.
    """
    if not text or not text.strip():
        return ExtractionResult(
            payload=ExtractedRemittance(),
            method=ExtractionMethod.NONE,
            error="The document is empty, so there is nothing to extract.",
        )

    if provider is None:
        payload = extract_with_regex(text, reference_config)
        _reconcile(payload, amount_paise, tolerance_paise)
        return ExtractionResult(payload=payload, method=ExtractionMethod.REGEX_FALLBACK)

    result = _ask_provider(text, provider, reference_config)

    if result.payload.is_empty and result.method is not ExtractionMethod.REGEX_FALLBACK:
        # The provider answered but found nothing usable. The rules get a
        # turn before giving up.
        fallback = extract_with_regex(text, reference_config)
        if not fallback.is_empty:
            result.payload = fallback
            result.method = ExtractionMethod.REGEX_FALLBACK

    _reconcile(result.payload, amount_paise, tolerance_paise)
    return result


def _ask_provider(
    text: str, provider: ExtractionProvider, reference_config: ReferenceConfig
) -> ExtractionResult:
    """Call the provider, validating and retrying at most once."""
    method = ExtractionMethod.MOCK if provider.name == "mock" else ExtractionMethod.LLM
    rejected: list[str] = []
    previous_error: str | None = None

    for attempt in (1, 2):
        prompt = build_prompt(text, previous_error=previous_error)

        try:
            raw = provider.complete(prompt)
        except ProviderError as exc:
            # The provider is unreachable. Retrying a network failure inside
            # a batch run just doubles the wait, so fall back immediately.
            payload = extract_with_regex(text, reference_config)
            return ExtractionResult(
                payload=payload,
                method=ExtractionMethod.REGEX_FALLBACK,
                attempts=attempt,
                error=f"Provider unavailable, used rule-based extraction instead: {exc}",
                rejected=rejected,
            )

        try:
            draft = _parse_draft(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            rejected.append(raw[:2000])
            previous_error = _describe(exc)
            if attempt == 1:
                continue
            payload = extract_with_regex(text, reference_config)
            return ExtractionResult(
                payload=payload,
                method=ExtractionMethod.REGEX_FALLBACK,
                attempts=attempt,
                error=(
                    "The model returned output that failed schema validation twice. "
                    f"Used rule-based extraction instead. Last error: {previous_error}"
                ),
                rejected=rejected,
            )

        return ExtractionResult(
            payload=_to_validated(draft),
            method=method,
            attempts=attempt,
            error=None if attempt == 1 else f"Accepted on retry after: {previous_error}",
            rejected=rejected,
        )

    raise AssertionError("unreachable")  # pragma: no cover


def _parse_draft(raw: str) -> RemittanceDraft:
    """Parse and schema-validate a provider answer."""
    cleaned = _FENCE.sub("", raw.strip())
    if not cleaned:
        raise ValueError("The model returned an empty answer.")

    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object at the top level, got {type(parsed).__name__}.")
    return RemittanceDraft.model_validate(parsed)


def _to_validated(draft: RemittanceDraft) -> ExtractedRemittance:
    """Convert the wire format into integer paise and canonical references.

    Lines whose reference cannot be normalised are dropped: a line the
    matcher cannot look up contributes nothing and would only inflate the
    apparent quality of the extraction.
    """
    lines: list[ExtractedLine] = []
    seen: set[str] = set()

    for line in draft.lines:
        normalized = normalize_line_reference(line.invoice_reference)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        built = _build_line(line, normalized)
        if built is not None:
            lines.append(built)

    unattributed = parse_amount(draft.unattributed_deduction) or 0
    return ExtractedRemittance(
        lines=lines,
        total_paise=parse_amount(draft.total_amount),
        payer_name=draft.payer_name,
        bank_reference=draft.bank_reference,
        document_deduction_paise=unattributed,
        document_deduction_reason=(
            classify_reason(draft.unattributed_deduction_reason) or DeductionReason.UNKNOWN
            if unattributed
            else classify_reason(draft.unattributed_deduction_reason)
        ),
        document_deduction_note=draft.unattributed_deduction_reason,
    )


def _build_line(line: DraftLine, normalized: str) -> ExtractedLine | None:
    """One validated line, or None if its numbers are incoherent."""
    gross = parse_amount(line.gross_amount)
    paid = parse_amount(line.paid_amount)
    deduction = parse_amount(line.deduction_amount) or 0

    reason: DeductionReason | None = None
    if deduction:
        reason = classify_reason(line.deduction_reason) or DeductionReason.UNKNOWN

    # Reconcile the two ways a document can express the same line before
    # the model's arithmetic reaches anything that matters.
    if gross is not None and paid is None and deduction:
        paid = gross - deduction
    if gross is None and paid is not None and deduction:
        gross = paid + deduction

    try:
        return ExtractedLine(
            invoice_reference=line.invoice_reference,
            normalized_reference=normalized,
            gross_amount_paise=gross,
            paid_amount_paise=paid,
            deduction_amount_paise=deduction,
            deduction_reason=reason,
            deduction_note=line.deduction_reason,
        )
    except ValidationError:
        # A single incoherent line (paid > gross, negative amounts) is
        # dropped rather than discarding the whole document.
        return None


def _reconcile(
    payload: ExtractedRemittance, amount_paise: int | None, tolerance_paise: int
) -> None:
    """Check the extraction against the money that actually arrived.

    The single most important guard in the pipeline. A model that invents an
    amount produces lines that do not add up to a real bank credit, and this
    is where that shows. A reading that does not tie is kept -- its invoice
    references are often still correct -- but flagged, so Phase 5 can weight
    it as a hint rather than treat it as fact.
    """
    if amount_paise is None:
        payload.reconciliation = Reconciliation.NOT_CHECKED
        payload.reconciliation_gap_paise = None
        return

    candidates = [
        value for value in (payload.stated_paid_paise, payload.total_paise) if value is not None
    ]
    if not candidates:
        payload.reconciliation = Reconciliation.NOT_CHECKED
        payload.reconciliation_gap_paise = None
        return

    gaps = [abs(value - amount_paise) for value in candidates]
    best = min(gaps)

    payload.reconciliation_gap_paise = best
    payload.reconciliation = (
        Reconciliation.TIES if best <= tolerance_paise else Reconciliation.DOES_NOT_TIE
    )


def _describe(exc: Exception) -> str:
    """A validation failure in words a model can act on."""
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()[:6]
        )
        return f"schema validation failed -- {problems}"
    if isinstance(exc, json.JSONDecodeError):
        return f"the answer was not valid JSON ({exc.msg} at line {exc.lineno})"
    return str(exc)
