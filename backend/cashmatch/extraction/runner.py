"""Running extraction over every stored remittance, and recording the result.

Extraction is cached on the row rather than recomputed. Three reasons, and
only the first is about speed: a live LLM call costs money and latency; an
extraction that influenced a cash posting has to be auditable months later;
and replaying a matching run must not depend on a model being reachable.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.config import LLMMode, Settings
from cashmatch.extraction.documents import document_text
from cashmatch.extraction.pipeline import ExtractionResult, extract_remittance
from cashmatch.extraction.providers import (
    ExtractionProvider,
    GeminiProvider,
    MockProvider,
    ProviderError,
)
from cashmatch.extraction.schema import Reconciliation
from cashmatch.matching.config import MatchingConfig
from cashmatch.models import Remittance
from cashmatch.models.enums import ExtractionMethod, ExtractionStatus


@dataclass(slots=True)
class ExtractionSummary:
    """Aggregate diagnostics for one extraction run."""

    documents: int = 0
    with_lines: int = 0
    empty: int = 0
    by_method: Counter = field(default_factory=Counter)
    by_reconciliation: Counter = field(default_factory=Counter)
    retries: int = 0
    references_found: int = 0
    deductions_found: int = 0
    elapsed_s: float = 0.0

    @property
    def coverage(self) -> float:
        """Share of documents that yielded at least one invoice reference."""
        return self.with_lines / self.documents if self.documents else 0.0

    @property
    def reconciled_rate(self) -> float:
        """Share of extractions whose arithmetic tied to the bank credit."""
        checked = self.documents - self.by_reconciliation[Reconciliation.NOT_CHECKED.value]
        if not checked:
            return 0.0
        return self.by_reconciliation[Reconciliation.TIES.value] / checked


def build_provider(settings: Settings, matching: MatchingConfig) -> ExtractionProvider | None:
    """Pick a provider from ``LLM_MODE``.

    ``off`` returns None, which sends the pipeline straight to the rules.
    The default is ``mock``, so a cold clone with no API key still works.
    """
    if settings.llm_mode is LLMMode.OFF:
        return None
    if settings.llm_mode is LLMMode.LIVE:
        return GeminiProvider(settings.gemini_api_key or "", settings.gemini_model)
    return MockProvider(matching.reference)


def run_extraction(
    session: Session,
    settings: Settings,
    matching: MatchingConfig,
    *,
    limit: int | None = None,
    force: bool = False,
) -> ExtractionSummary:
    """Extract every pending remittance and write the result back.

    Args:
        limit: stop after this many documents.
        force: re-extract documents already processed. Needed when the
            prompt or the extractor changes, since results are cached.
    """
    try:
        provider = build_provider(settings, matching)
    except ProviderError as exc:
        raise RuntimeError(str(exc)) from exc

    query = select(Remittance).order_by(Remittance.id)
    if not force:
        query = query.where(Remittance.extraction_status == ExtractionStatus.PENDING)
    if limit:
        query = query.limit(limit)

    documents = session.scalars(query).all()
    directory = Path(settings.data_dir) / "generated" / "remittances"

    summary = ExtractionSummary(documents=len(documents))
    started = time.perf_counter()

    for remittance in documents:
        text = document_text(
            source_type=remittance.source_type,
            raw_text=remittance.raw_text,
            source_filename=remittance.source_filename,
            directory=directory,
        )

        # Only advice already tied to a payment can be cross-checked against
        # a real amount. Unlinked advice is extracted, then reconciled later
        # once something pairs it with a credit.
        amount = (
            remittance.bank_transaction.amount_paise
            if remittance.bank_transaction is not None
            else None
        )

        result = extract_remittance(
            text,
            provider=provider,
            reference_config=matching.reference,
            amount_paise=amount,
            tolerance_paise=matching.amount.tolerance_paise,
        )
        _persist(remittance, result)
        _tally(summary, result)

    summary.elapsed_s = time.perf_counter() - started
    return summary


def _persist(remittance: Remittance, result: ExtractionResult) -> None:
    remittance.extracted_payload = result.as_dict()
    remittance.extraction_method = result.method
    remittance.extraction_error = result.error
    remittance.extraction_status = (
        ExtractionStatus.EXTRACTED if result.succeeded else ExtractionStatus.FAILED
    )


def _tally(summary: ExtractionSummary, result: ExtractionResult) -> None:
    summary.by_method[result.method.value] += 1
    summary.by_reconciliation[result.payload.reconciliation.value] += 1
    if result.attempts > 1:
        summary.retries += 1

    if result.succeeded:
        summary.with_lines += 1
        summary.references_found += len(result.payload.references)
        # A claim stated at document level counts: the reason is just as
        # useful whether or not the document said which invoice bears it.
        summary.deductions_found += sum(
            1 for line in result.payload.lines if line.deduction_amount_paise
        ) + (1 if result.payload.document_deduction_paise else 0)
    else:
        summary.empty += 1


def extraction_is_pending(session: Session) -> bool:
    """True when any stored remittance has not been extracted yet."""
    return (
        session.execute(
            select(Remittance.id)
            .where(Remittance.extraction_status == ExtractionStatus.PENDING)
            .limit(1)
        ).first()
        is not None
    )


def extracted_count(session: Session) -> int:
    from sqlalchemy import func

    return session.scalar(
        select(func.count())
        .select_from(Remittance)
        .where(Remittance.extraction_method != ExtractionMethod.NONE)
    )
