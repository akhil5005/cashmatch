"""Phase 4 -- turning messy remittance advice into structured candidates.

The model proposes structure; it never decides anything. Its output is
validated against a strict schema, retried once on failure, cross-checked
against the bank credit that actually arrived, and falls back to a
rule-based extractor that needs no API key and no network.

``LLM_MODE`` defaults to ``mock``, so a cold clone of this repository runs
the whole pipeline -- and the whole test suite -- with no credentials.
"""

from cashmatch.extraction.advice import AdviceIndex, LinkedAdvice
from cashmatch.extraction.documents import document_text, read_pdf_text
from cashmatch.extraction.pipeline import ExtractionResult, extract_remittance
from cashmatch.extraction.providers import (
    ExtractionProvider,
    GeminiProvider,
    MockProvider,
    ProviderError,
    ScriptedProvider,
)
from cashmatch.extraction.reasons import classify_reason, describes_a_deduction
from cashmatch.extraction.regex_extractor import extract_with_regex
from cashmatch.extraction.runner import (
    ExtractionSummary,
    build_provider,
    extracted_count,
    extraction_is_pending,
    run_extraction,
)
from cashmatch.extraction.schema import (
    DraftLine,
    ExtractedLine,
    ExtractedRemittance,
    Reconciliation,
    RemittanceDraft,
    parse_amount,
)

__all__ = [
    "AdviceIndex",
    "DraftLine",
    "ExtractedLine",
    "ExtractedRemittance",
    "ExtractionProvider",
    "ExtractionResult",
    "ExtractionSummary",
    "GeminiProvider",
    "LinkedAdvice",
    "MockProvider",
    "ProviderError",
    "Reconciliation",
    "RemittanceDraft",
    "ScriptedProvider",
    "build_provider",
    "classify_reason",
    "describes_a_deduction",
    "document_text",
    "extract_remittance",
    "extract_with_regex",
    "extracted_count",
    "extraction_is_pending",
    "parse_amount",
    "read_pdf_text",
    "run_extraction",
]
