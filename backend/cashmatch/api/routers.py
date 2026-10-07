"""HTTP endpoints.

Kept in one module because there are eleven of them and splitting eleven
routes across five files costs more to read than it saves. The real logic
lives in :mod:`cashmatch.api.review`, :mod:`cashmatch.api.ingest` and
:mod:`cashmatch.api.serialize`; these functions are the thin HTTP layer over
it.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from cashmatch.api import ingest, review, serialize
from cashmatch.api.schemas import (
    CustomerOut,
    DecisionCount,
    InvoiceOut,
    MetricsOut,
    Money,
    PipelineResponse,
    ReassignRequest,
    ResultDetail,
    ResultPage,
    ReviewRequest,
    ReviewResponse,
    UploadResponse,
)
from cashmatch.config import get_settings
from cashmatch.db.session import get_db
from cashmatch.matching import MatchingConfig
from cashmatch.models import BankTransaction, Customer, Invoice, MatchAllocation, MatchResult
from cashmatch.models.enums import MatchDecision, MatchStrategy
from cashmatch.models.invoice import OPEN_STATUSES
from cashmatch.roi import (
    DEFAULT_ERROR_HOURS,
    DEFAULT_HOURLY_COST_RUPEES,
    DEFAULT_MINUTES_PER_PAYMENT,
    DEFAULT_REVIEW_MINUTES,
    RoiAssumptions,
)
from cashmatch.roi import calculate as roi_calculate

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(get_db)]

#: Uploads are capped so a mistaken file cannot exhaust memory. Generous
#: enough for a month of statements.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


def _config() -> MatchingConfig:
    try:
        return MatchingConfig.from_yaml(get_settings().matching_config)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"The matching configuration could not be loaded: {exc}",
        ) from exc


def _loaded(query):
    """Eager-load everything the serialiser touches, in one round trip."""
    return query.options(
        selectinload(MatchResult.bank_transaction),
        selectinload(MatchResult.allocations)
        .selectinload(MatchAllocation.invoice)
        .selectinload(Invoice.customer),
    )


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


@router.get("/metrics", response_model=MetricsOut, tags=["metrics"])
def metrics(session: SessionDep) -> MetricsOut:
    """Everything the dashboard shows, in one call."""
    transactions = session.scalar(select(func.count()).select_from(BankTransaction)) or 0
    total_value = session.scalar(select(func.sum(BankTransaction.amount_paise))) or 0

    rows = session.execute(
        select(
            MatchResult.decision,
            func.count(MatchResult.id),
            func.sum(BankTransaction.amount_paise),
        )
        .join(BankTransaction, MatchResult.bank_transaction_id == BankTransaction.id)
        .group_by(MatchResult.decision)
    ).all()

    by_decision = [
        DecisionCount(decision=decision, count=count, value=Money.of(int(value or 0)))
        for decision, count, value in rows
    ]
    counts = {decision.value: count for decision, count, _ in rows}
    values = {decision.value: int(value or 0) for decision, _, value in rows}
    decided = sum(counts.values())

    strategies = dict(
        session.execute(
            select(MatchResult.strategy, func.count(MatchResult.id)).group_by(MatchResult.strategy)
        ).all()
    )

    open_count = (
        session.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.status.in_(OPEN_STATUSES), Invoice.open_amount_paise > 0)
        )
        or 0
    )
    open_value = (
        session.scalar(
            select(func.sum(Invoice.open_amount_paise)).where(Invoice.status.in_(OPEN_STATUSES))
        )
        or 0
    )

    auto = counts.get(MatchDecision.AUTO_APPLIED.value, 0)
    precision, basis = _precision(session)

    return MetricsOut(
        transactions=transactions,
        decided=decided,
        auto_match_rate=auto / decided if decided else 0.0,
        review_count=counts.get(MatchDecision.NEEDS_REVIEW.value, 0),
        unapplied_count=counts.get(MatchDecision.UNAPPLIED.value, 0),
        unapplied_value=Money.of(values.get(MatchDecision.UNAPPLIED.value, 0)),
        auto_applied_value=Money.of(values.get(MatchDecision.AUTO_APPLIED.value, 0)),
        total_value=Money.of(int(total_value)),
        by_decision=by_decision,
        by_strategy={strategy.value: count for strategy, count in strategies.items()},
        open_receivables=Money.of(int(open_value)),
        open_invoice_count=open_count,
        customers=session.scalar(select(func.count()).select_from(Customer)) or 0,
        precision=precision,
        precision_basis=basis,
        reviewed_by_humans=session.scalar(
            select(func.count()).select_from(MatchResult).where(MatchResult.reviewed_at.isnot(None))
        )
        or 0,
    )


def _precision(session: Session) -> tuple[float | None, str | None]:
    """Precision, but only where an answer key genuinely exists.

    Production has no ground truth. Returning a number anyway -- by scoring
    the engine against itself, or by quoting a figure from a demo run --
    would be the single most misleading thing this API could do.
    """
    from cashmatch.evaluation import evaluate
    from cashmatch.evaluation.runner import GroundTruthMissingError

    try:
        report, _rows, _comparisons = evaluate(session, _config(), get_settings().data_dir)
    except (GroundTruthMissingError, HTTPException, Exception):
        return None, None

    if not report.auto_applied:
        return None, None
    return report.auto_precision, (f"measured against the generated answer key, seed {report.seed}")


# ---------------------------------------------------------------------------
# return on investment
# ---------------------------------------------------------------------------


@router.get("/roi", tags=["metrics"])
def roi(
    session: SessionDep,
    monthly_payments: Annotated[
        int | None,
        Query(ge=1, le=1_000_000, description="Payments a month. Defaults to what is loaded."),
    ] = None,
    minutes_per_payment: Annotated[float, Query(gt=0, le=240)] = DEFAULT_MINUTES_PER_PAYMENT,
    review_minutes: Annotated[float, Query(ge=0, le=240)] = DEFAULT_REVIEW_MINUTES,
    hourly_cost: Annotated[float, Query(gt=0, le=100_000)] = DEFAULT_HOURLY_COST_RUPEES,
    error_hours: Annotated[float, Query(ge=0, le=100)] = DEFAULT_ERROR_HOURS,
) -> dict:
    """Hours and rupees saved at the measured auto-match rate.

    The rates come from the decisions actually recorded; the effort and cost
    figures come from the caller. Every default is an assumption meant to be
    replaced, not a benchmark.
    """
    summary = metrics(session)
    if not summary.decided:
        raise HTTPException(
            status_code=422,
            detail=(
                "No decisions have been recorded yet, so there is no auto-match rate to "
                "base a saving on. Run matching first."
            ),
        )

    assumptions = RoiAssumptions(
        monthly_payments=monthly_payments or summary.decided,
        minutes_per_payment=minutes_per_payment,
        review_minutes=review_minutes,
        hourly_cost_rupees=hourly_cost,
        error_hours=error_hours,
    )

    try:
        outcome = roi_calculate(
            assumptions,
            auto_match_rate=summary.auto_match_rate,
            review_rate=summary.review_count / summary.decided,
            unapplied_rate=summary.unapplied_count / summary.decided,
            precision=summary.precision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    body = outcome.as_dict()
    body["precision_basis"] = summary.precision_basis
    body["note"] = (
        "Savings assume an auto-applied payment costs nothing, a review item costs a "
        "shorter look because the suggestion is already on screen, and unapplied cash "
        "still costs a full manual investigation."
    )
    return body


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@router.get("/results", response_model=ResultPage, tags=["results"])
def list_results(
    session: SessionDep,
    decision: Annotated[MatchDecision | None, Query(description="Filter by decision.")] = None,
    strategy: Annotated[MatchStrategy | None, Query(description="Filter by strategy.")] = None,
    reviewed: Annotated[
        bool | None, Query(description="Only items a human has or has not touched.")
    ] = None,
    has_deduction: Annotated[bool | None, Query(description="Only items carrying a claim.")] = None,
    search: Annotated[
        str | None, Query(description="Bank reference, payer name or narration.")
    ] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    max_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResultPage:
    """The review queue, filtered.

    Defaults to newest first so an analyst working a backlog sees the most
    recent decisions at the top.
    """
    query = select(MatchResult).join(
        BankTransaction, MatchResult.bank_transaction_id == BankTransaction.id
    )

    if decision is not None:
        query = query.where(MatchResult.decision == decision)
    if strategy is not None:
        query = query.where(MatchResult.strategy == strategy)
    if reviewed is True:
        query = query.where(MatchResult.reviewed_at.isnot(None))
    elif reviewed is False:
        query = query.where(MatchResult.reviewed_at.is_(None))
    if min_confidence is not None:
        query = query.where(MatchResult.confidence >= min_confidence)
    if max_confidence is not None:
        query = query.where(MatchResult.confidence <= max_confidence)
    if search:
        term = f"%{search.strip()}%"
        query = query.where(
            BankTransaction.statement_ref.ilike(term)
            | BankTransaction.payer_name_raw.ilike(term)
            | BankTransaction.narration.ilike(term)
        )

    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0

    rows = session.scalars(
        _loaded(query)
        .order_by(BankTransaction.value_date.desc(), MatchResult.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    items = [serialize.summarise(row) for row in rows]
    if has_deduction is not None:
        items = [item for item in items if item.has_deduction is has_deduction]

    return ResultPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/results/{result_id}", response_model=ResultDetail, tags=["results"])
def get_result(result_id: int, session: SessionDep) -> ResultDetail:
    """One review item, with the full explanation."""
    return serialize.detail(session, _require(session, result_id))


# ---------------------------------------------------------------------------
# review actions
# ---------------------------------------------------------------------------


@router.post("/results/{result_id}/approve", response_model=ReviewResponse, tags=["review"])
def approve(result_id: int, request: ReviewRequest, session: SessionDep) -> ReviewResponse:
    """Accept the suggestion and, by default, post the cash."""
    return _act(session, result_id, lambda result: review.approve(session, result, request))


@router.post("/results/{result_id}/reject", response_model=ReviewResponse, tags=["review"])
def reject(result_id: int, request: ReviewRequest, session: SessionDep) -> ReviewResponse:
    """Discard the suggestion. The payment stays as unapplied cash."""
    return _act(session, result_id, lambda result: review.reject(session, result, request))


@router.post("/results/{result_id}/reassign", response_model=ReviewResponse, tags=["review"])
def reassign(result_id: int, request: ReassignRequest, session: SessionDep) -> ReviewResponse:
    """Replace the suggestion with the invoices the reviewer chose."""
    return _act(session, result_id, lambda result: review.reassign(session, result, request))


def _act(session: Session, result_id: int, action) -> ReviewResponse:
    result = _require(session, result_id)
    try:
        outcome = action(result)
    except review.ReviewError as exc:
        # A user error, not a server fault: say what is wrong in words they
        # can act on rather than returning a bare 400.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session.flush()
    session.refresh(outcome.result)
    return ReviewResponse(
        result=serialize.detail(session, outcome.result),
        cash_posted=outcome.cash_posted,
        invoices_updated=outcome.invoices_updated,
        alias_learned=outcome.alias_learned,
        message=outcome.message
        + (
            f" Learned '{outcome.alias_learned}' as a spelling for this customer, so the "
            "next payment from it will match without help."
            if outcome.alias_learned
            else ""
        ),
    )


def _require(session: Session, result_id: int) -> MatchResult:
    result = session.scalar(_loaded(select(MatchResult)).where(MatchResult.id == result_id))
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No match result with id {result_id}. It may have been replaced by a "
            "later matching run.",
        )
    return result


# ---------------------------------------------------------------------------
# reference data for the reassign picker
# ---------------------------------------------------------------------------


@router.get("/customers", response_model=list[CustomerOut], tags=["reference"])
def list_customers(
    session: SessionDep,
    search: Annotated[str | None, Query(description="Name or code.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CustomerOut]:
    query = select(Customer).order_by(Customer.legal_name)
    if search:
        term = f"%{search.strip()}%"
        query = query.where(Customer.legal_name.ilike(term) | Customer.code.ilike(term))

    customers = session.scalars(query.limit(limit)).all()
    stats = dict(
        session.execute(
            select(
                Invoice.customer_id,
                func.count(Invoice.id),
            )
            .where(Invoice.status.in_(OPEN_STATUSES))
            .group_by(Invoice.customer_id)
        ).all()
    )
    amounts = dict(
        session.execute(
            select(Invoice.customer_id, func.sum(Invoice.open_amount_paise))
            .where(Invoice.status.in_(OPEN_STATUSES))
            .group_by(Invoice.customer_id)
        ).all()
    )

    return [
        serialize.customer_out(
            customer, stats.get(customer.id, 0), int(amounts.get(customer.id, 0) or 0)
        )
        for customer in customers
    ]


@router.get("/invoices", response_model=list[InvoiceOut], tags=["reference"])
def list_invoices(
    session: SessionDep,
    customer_id: Annotated[int | None, Query(description="Restrict to one customer.")] = None,
    search: Annotated[str | None, Query(description="Invoice number.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InvoiceOut]:
    """Open items, for a reviewer choosing where cash should go."""
    query = (
        select(Invoice)
        .options(selectinload(Invoice.customer))
        .where(Invoice.status.in_(OPEN_STATUSES), Invoice.open_amount_paise > 0)
        .order_by(Invoice.due_date)
    )
    if customer_id is not None:
        query = query.where(Invoice.customer_id == customer_id)
    if search:
        query = query.where(Invoice.invoice_number.ilike(f"%{search.strip()}%"))

    return [serialize.invoice_out(invoice) for invoice in session.scalars(query.limit(limit)).all()]


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


@router.post("/uploads/bank-statement", response_model=UploadResponse, tags=["uploads"])
async def upload_bank_statement(
    session: SessionDep, file: Annotated[UploadFile, File()]
) -> UploadResponse:
    """Load a bank statement CSV as unmatched payments.

    Re-uploading the same file is safe: the bank reference is unique, so
    payments already present are skipped rather than duplicated.
    """
    content = await _read(file)
    try:
        result = ingest.load_bank_statement(session, content, file.filename or "upload.csv")
    except ingest.IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return UploadResponse(
        filename=file.filename or "upload.csv",
        rows_read=result.rows_read,
        created=result.created,
        skipped=result.skipped,
        errors=result.errors,
        message=(
            f"Loaded {result.created} payment(s)"
            + (f", skipped {result.skipped}" if result.skipped else "")
            + ". Run matching to produce suggestions."
        ),
    )


@router.post("/uploads/remittance", response_model=UploadResponse, tags=["uploads"])
async def upload_remittance(
    session: SessionDep, file: Annotated[UploadFile, File()]
) -> UploadResponse:
    """Store an advice document (text or PDF) ready for extraction."""
    content = await _read(file)
    try:
        result = ingest.load_remittance(session, content, file.filename or "advice.txt")
    except ingest.IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return UploadResponse(
        filename=file.filename or "advice.txt",
        rows_read=result.rows_read,
        created=result.created,
        skipped=result.skipped,
        message="Advice stored. Run extraction to read the invoice lines out of it.",
    )


async def _read(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail=f"{file.filename} is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{file.filename} is {len(content) // 1_048_576} MB, over the "
                f"{MAX_UPLOAD_BYTES // 1_048_576} MB limit. Split it into smaller files."
            ),
        )
    return content


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


@router.post("/pipeline/extract", response_model=PipelineResponse, tags=["pipeline"])
def run_extract(session: SessionDep, force: bool = False) -> PipelineResponse:
    """Read invoice lines out of every pending advice document."""
    from cashmatch.extraction import run_extraction

    started = time.perf_counter()
    summary = run_extraction(session, get_settings(), _config(), force=force)

    return PipelineResponse(
        transactions=summary.documents,
        elapsed_seconds=round(time.perf_counter() - started, 2),
        message=(
            f"Extracted {summary.with_lines} of {summary.documents} document(s); "
            f"{summary.references_found} invoice reference(s) found."
        ),
    )


@router.post("/pipeline/match", response_model=PipelineResponse, tags=["pipeline"])
def run_match(session: SessionDep, post: bool = False) -> PipelineResponse:
    """Match and score every unmatched payment, recording the decisions.

    Writes decisions only unless ``post`` is set. A first deployment runs
    this way for weeks while the client watches the precision number.
    """
    from cashmatch.scoring import run_decisions

    started = time.perf_counter()
    _scored, summary = run_decisions(session, _config(), post=post)

    return PipelineResponse(
        transactions=summary.transactions,
        by_decision=dict(summary.by_decision),
        auto_match_rate=round(summary.auto_match_rate, 4),
        elapsed_seconds=round(time.perf_counter() - started, 2),
        posted=post,
        message=(
            f"Decided {summary.transactions} payment(s): "
            f"{summary.by_decision.get('auto_applied', 0)} auto-applied, "
            f"{summary.by_decision.get('needs_review', 0)} for review, "
            f"{summary.by_decision.get('unapplied', 0)} unapplied."
            + (" Cash was posted." if post else " No cash was posted.")
        ),
    )
