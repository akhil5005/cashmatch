"""Getting text out of whatever the advice arrived as."""

from __future__ import annotations

import logging
from pathlib import Path

from cashmatch.models.enums import RemittanceSource

logger = logging.getLogger(__name__)


def read_pdf_text(path: str | Path) -> str:
    """Extract text from a PDF with pdfplumber.

    Returns an empty string rather than raising on a file that cannot be
    read: one unreadable attachment should degrade that remittance, not
    abort a batch of four hundred.
    """
    path = Path(path)
    if not path.is_file():
        logger.warning("Remittance PDF not found at %s", path)
        return ""

    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - dependency is declared
        logger.error("pdfplumber is not installed; cannot read %s", path)
        return ""

    try:
        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return ""

    return "\n".join(pages).strip()


def document_text(
    *,
    source_type: RemittanceSource,
    raw_text: str,
    source_filename: str | None,
    directory: Path | None,
) -> str:
    """The text to extract from, preferring the file on disk for PDFs.

    Email bodies are stored verbatim in ``raw_text``, so the database copy
    is authoritative. A PDF's ``raw_text`` is what the generator *wrote*;
    re-reading the file through pdfplumber is what a real deployment would
    do, and exercising that path here is the point of generating PDFs at
    all. If the file is missing, the stored text is used rather than
    failing.
    """
    if source_type is RemittanceSource.PDF and source_filename and directory:
        extracted = read_pdf_text(directory / source_filename)
        if extracted:
            return extracted
        logger.info("Falling back to stored text for %s", source_filename)

    return raw_text or ""
