"""Command-line entrypoint: `cashmatch <command>`.

Phase 1 shipped `version`, `init-db` and `health`; Phase 2 adds `generate`.
Later phases add `match` and `evaluate`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import cashmatch.models  # noqa: F401  (registers every table on Base.metadata)
from cashmatch import __version__
from cashmatch.config import get_settings
from cashmatch.db.base import Base
from cashmatch.db.session import get_engine, session_scope
from cashmatch.money import format_inr

app = typer.Typer(help="CashMatch - AI cash application agent.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"cashmatch {__version__}")


@app.command("init-db")
def init_db() -> None:
    """Create every table directly from the models.

    Handy for a scratch database. The Docker stack uses `alembic upgrade
    head` instead, so that schema changes are versioned.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    typer.echo(f"Created {len(Base.metadata.tables)} tables on {engine.url.render_as_string()}")


@app.command()
def health() -> None:
    """Check that the configured database is reachable."""
    settings = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        typer.secho(
            f"Cannot reach the database at {settings.database_url}\n"
            f"  {exc.__class__.__name__}: {exc}\n"
            "  Hint: run `docker compose up -d db`, or point DATABASE_URL at a "
            "running Postgres instance.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc
    typer.secho("Database reachable.", fg=typer.colors.GREEN)


@app.command()
def generate(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Scenario YAML. Defaults to config/scenarios.yml."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Data directory. Defaults to the DATA_DIR setting."),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset", help="Delete existing data first. Required if the DB is not empty."
        ),
    ] = False,
) -> None:
    """Generate the synthetic dataset and its ground-truth answer key."""
    # Imported here so `cashmatch version` stays fast and dependency-light.
    from cashmatch.generator import (
        GeneratorConfig,
        database_is_populated,
        generate_dataset,
        reset_database,
    )

    settings = get_settings()
    config_file = config_path or settings.scenario_config
    output_root = output or settings.data_dir

    try:
        config = GeneratorConfig.from_yaml(config_file)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    try:
        with session_scope() as session:
            if database_is_populated(session):
                if not reset:
                    typer.secho(
                        "The database already contains data. Generating now would mix two "
                        "datasets and make any accuracy measurement meaningless.\n"
                        "  Re-run with --reset to delete the existing customers, invoices, "
                        "payments, remittances and match results first.",
                        fg=typer.colors.RED,
                    )
                    raise typer.Exit(code=1)
                typer.echo("Clearing existing data...")
                reset_database(session)

            typer.echo(f"Generating from {config_file} (seed {config.seed})...")
            summary = generate_dataset(session, config, output_root)
    except SQLAlchemyError as exc:
        typer.secho(
            f"Database error while generating: {exc.__class__.__name__}: {exc}\n"
            "  Hint: is the schema up to date? Try `alembic upgrade head`.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc

    _report(summary)


def _report(summary) -> None:
    """Print a human-readable summary of what was generated."""
    typer.secho("\nDataset generated.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  seed           {summary.seed}")
    typer.echo(f"  config digest  {summary.config_digest}")
    typer.echo(f"  customers      {summary.customers:>6}  ({summary.aliases} aliases on file)")
    typer.echo(f"  invoices       {summary.invoices:>6}")
    typer.echo(f"  payments       {summary.payments:>6}")
    typer.echo(f"  remittances    {summary.remittances:>6}")

    typer.echo("\n  scenario mix")
    width = max(len(name) for name in summary.scenario_counts)
    total = sum(summary.scenario_counts.values()) or 1
    for name, count in summary.scenario_counts.items():
        typer.echo(f"    {name:<{width}}  {count:>5}  ({count * 100 / total:4.1f}%)")

    typer.echo(f"\n  answer key     {summary.ground_truth_path}")
    typer.echo(f"  advice files   {summary.remittance_dir}")
    typer.secho(
        "\n  The answer key is never read by the matcher. Phase 6 is the only "
        "component that loads it.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def extract(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Only extract the first N documents.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-extract documents already processed."),
    ] = False,
    show: Annotated[
        int, typer.Option("--show", help="Print this many extracted documents in full.")
    ] = 0,
) -> None:
    """Read remittance advice into structured invoice lines.

    Uses LLM_MODE from the environment: `mock` (default, no key, no network),
    `live` (Gemini), or `off` (rule-based only). Results are cached on the
    remittance row, so re-running is cheap; pass --force after changing the
    prompt or the extractor.
    """
    from cashmatch.extraction import run_extraction
    from cashmatch.matching import MatchingConfig

    settings = get_settings()
    try:
        matching = MatchingConfig.from_yaml(settings.matching_config)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Extracting with LLM_MODE={settings.llm_mode.value}...")

    with session_scope() as session:
        try:
            summary = run_extraction(session, settings, matching, limit=limit, force=force)
        except RuntimeError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        if summary.documents == 0:
            typer.secho(
                "Nothing to extract. Every stored remittance has already been "
                "processed -- pass --force to redo them, or run "
                "`cashmatch generate --reset` first.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1)

        if show:
            _show_extractions(session, show)

    _report_extraction(summary)


def _show_extractions(session, count: int) -> None:
    """Print a few extracted documents beside their source text."""
    from sqlalchemy import select

    from cashmatch.models import Remittance
    from cashmatch.models.enums import ExtractionStatus

    rows = session.scalars(
        select(Remittance)
        .where(Remittance.extraction_status == ExtractionStatus.EXTRACTED)
        .order_by(Remittance.id)
        .limit(count)
    ).all()

    for remittance in rows:
        payload = (remittance.extracted_payload or {}).get("payload", {})
        typer.secho(f"\n{remittance.source_filename}  ({remittance.extraction_method})", bold=True)
        for line in remittance.raw_text.splitlines()[:14]:
            typer.secho(f"    | {line}", fg=typer.colors.BRIGHT_BLACK)

        typer.echo("  extracted:")
        for line in payload.get("lines", []):
            text = f"    {line['normalized_reference']:<12}"
            if line.get("gross_amount_paise") is not None:
                text += f"  gross {format_inr(line['gross_amount_paise'])}"
            if line.get("deduction_amount_paise"):
                text += (
                    f"  deduction {format_inr(line['deduction_amount_paise'])}"
                    f" ({line.get('deduction_reason')})"
                )
            typer.echo(text)

        reconciliation = payload.get("reconciliation")
        colour = typer.colors.GREEN if reconciliation == "ties" else typer.colors.YELLOW
        typer.secho(f"    reconciliation: {reconciliation}", fg=colour)


def _report_extraction(summary) -> None:
    typer.secho("\nExtraction complete.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  documents            {summary.documents:>6}")
    typer.echo(f"  yielded invoices     {summary.with_lines:>6}  ({summary.coverage:.1%})")
    typer.echo(f"  yielded nothing      {summary.empty:>6}")
    typer.echo(f"  invoice references   {summary.references_found:>6}")
    typer.echo(f"  deductions found     {summary.deductions_found:>6}")
    typer.echo(f"  needed a retry       {summary.retries:>6}")

    typer.echo("\n  method")
    for method, count in summary.by_method.most_common():
        typer.echo(f"    {method:<16} {count:>6}")

    typer.echo("\n  cross-check against the bank credit")
    for state, count in summary.by_reconciliation.most_common():
        typer.echo(f"    {state:<16} {count:>6}")
    typer.echo(f"    reconciled rate  {summary.reconciled_rate:>6.1%}")

    typer.echo(f"\n  wall clock           {summary.elapsed_s:>6.2f}s")
    typer.secho(
        "\n  Extracted amounts are never trusted on their own: the matcher "
        "re-verifies\n  the arithmetic before proposing any allocation.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def match(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Matching YAML. Defaults to config/matching.yml."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Only match the first N transactions.")
    ] = None,
    statement_ref: Annotated[
        str | None, typer.Option("--ref", help="Match a single transaction by bank reference.")
    ] = None,
    explain: Annotated[
        bool, typer.Option("--explain", help="Print the full reasoning trail per transaction.")
    ] = False,
    use_advice: Annotated[
        bool,
        typer.Option(
            "--advice/--no-advice",
            help="Feed extracted remittance advice into the cascade.",
        ),
    ] = True,
) -> None:
    """Run the matching cascade and report what it found.

    Writes nothing. Phase 3 produces candidates; the confidence score, the
    auto-apply decision and persistence arrive in Phase 5, and accuracy
    against ground truth in Phase 6.
    """
    from cashmatch.matching import MatchingConfig, run_matching

    settings = get_settings()
    try:
        config = MatchingConfig.from_yaml(config_path or settings.matching_config)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    with session_scope() as session:
        outcomes, summary = run_matching(
            session,
            config,
            limit=limit,
            statement_ref=statement_ref,
            use_advice=use_advice,
        )

    if not outcomes:
        typer.secho(
            "No transactions to match. Run `cashmatch generate --reset` first, or check "
            "the --ref you passed.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    if explain:
        for outcome in outcomes:
            _explain(outcome)

    _report_batch(summary)


def _explain(outcome) -> None:
    """Print one transaction's full reasoning trail."""
    typer.secho(f"\n{outcome.statement_ref}  {format_inr(outcome.amount_paise)}", bold=True)

    for signal in outcome.trail:
        mark = (
            typer.style("fired", fg=typer.colors.GREEN)
            if signal.fired
            else typer.style(" ---  ", fg=typer.colors.BRIGHT_BLACK)
        )
        typer.echo(f"  [{mark}] {signal.name:<22} {signal.detail}")

    if not outcome.candidates:
        typer.secho("  -> no candidates; this would become unapplied cash", fg=typer.colors.YELLOW)
        return

    label = "candidates (ambiguous)" if outcome.is_ambiguous else "candidate"
    typer.secho(f"  -> {len(outcome.candidates)} {label} via {outcome.strategy.value}", bold=True)
    for candidate in outcome.candidates:
        for allocation in candidate.allocations:
            line = (
                f"       {allocation.item.invoice_number}  "
                f"{format_inr(allocation.allocated_amount_paise)}"
            )
            if allocation.deduction_amount_paise:
                line += f"   deduction {format_inr(allocation.deduction_amount_paise)}"
            typer.echo(line)
        typer.secho(f"       {candidate.note}", fg=typer.colors.BRIGHT_BLACK)


def _report_batch(summary) -> None:
    """Print aggregate engine diagnostics."""
    typer.secho("\nMatching run complete.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  transactions        {summary.transactions:>6}")
    typer.echo(
        f"  produced candidates {summary.with_candidates:>6}  ({summary.candidate_rate:.1%})"
    )
    typer.echo(f"  ambiguous           {summary.ambiguous:>6}")
    typer.echo(f"  no candidates       {summary.no_candidates:>6}")
    typer.echo(f"  advice available    {summary.advice_available:>6}")

    typer.echo("\n  customer identified by")
    for method, count in summary.by_identification.most_common():
        typer.echo(f"    {method:<12} {count:>6}")

    typer.echo("\n  leading strategy")
    for strategy, count in summary.by_strategy.most_common():
        typer.echo(f"    {strategy:<18} {count:>6}")

    typer.echo("\n  search cost")
    typer.echo(f"    combinations examined   {summary.nodes_visited:>10,}")
    typer.echo(f"    largest candidate pool  {summary.largest_pool:>10}")
    typer.echo(f"    budget exhaustions      {summary.budget_exhaustions:>10}")
    typer.echo(f"    wall clock              {summary.elapsed_s:>10.2f}s")

    typer.secho(
        "\n  Candidate rate is not the auto-match rate. Confidence scoring and the "
        "auto-apply\n  decision are Phase 5; accuracy against ground truth is Phase 6.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def apply(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Matching YAML. Defaults to config/matching.yml."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Only process the first N transactions.")
    ] = None,
    post: Annotated[
        bool,
        typer.Option(
            "--post",
            help="Also apply the cash to the ledger for auto-applied decisions.",
        ),
    ] = False,
    use_advice: Annotated[
        bool, typer.Option("--advice/--no-advice", help="Use extracted remittance advice.")
    ] = True,
) -> None:
    """Score every match and record the decision.

    By default this writes decisions only: no invoice balance changes. Pass
    --post to apply the cash for auto-applied matches, which is the step a
    real deployment keeps switched off for the first few weeks.
    """
    from cashmatch.matching import MatchingConfig
    from cashmatch.scoring import run_decisions

    settings = get_settings()
    try:
        config = MatchingConfig.from_yaml(config_path or settings.matching_config)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if post:
        typer.secho(
            "Posting mode: auto-applied matches will change invoice balances.",
            fg=typer.colors.YELLOW,
        )

    with session_scope() as session:
        scored, summary = run_decisions(
            session, config, limit=limit, use_advice=use_advice, post=post
        )

    if not scored:
        typer.secho(
            "No transactions to process. Run `cashmatch generate --reset` first.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    _report_decisions(summary, config, posted=post)


def _report_decisions(summary, config, *, posted: bool) -> None:
    thresholds = config.scoring.thresholds
    typer.secho("\nDecisions recorded.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  transactions         {summary.transactions:>6}")
    if summary.replaced:
        typer.echo(f"  replaced earlier     {summary.replaced:>6}")

    typer.echo("\n  outcome")
    rows = (
        ("auto_applied", summary.auto_match_rate, summary.auto_applied_paise, typer.colors.GREEN),
        ("needs_review", summary.review_rate, summary.review_paise, typer.colors.YELLOW),
        ("unapplied", summary.unapplied_rate, summary.unapplied_paise, typer.colors.RED),
    )
    for name, rate, amount, colour in rows:
        count = summary.by_decision[name]
        typer.secho(f"    {name:<14} {count:>5}  ({rate:5.1%})   {format_inr(amount)}", fg=colour)

    typer.echo(
        f"\n  thresholds           auto >= {thresholds.auto_apply:.2f}, "
        f"review >= {thresholds.review_floor:.2f}"
    )
    typer.echo(f"  deductions detected  {summary.deductions_detected:>6}")

    typer.echo("\n  confidence distribution")
    for band in sorted(summary.confidence_buckets, reverse=True):
        count = summary.confidence_buckets[band]
        bar = "#" * min(40, max(1, count * 40 // max(summary.transactions, 1)))
        typer.echo(f"    {band:<10} {count:>5}  {bar}")

    typer.echo("\n  leading strategy")
    for strategy, count in summary.by_strategy.most_common():
        typer.echo(f"    {strategy:<18} {count:>6}")

    if posted:
        typer.secho(
            f"\n  posted               {summary.posted_invoices} invoices, "
            f"{format_inr(summary.posted_cash_paise)} applied",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            "\n  Suggest-only: no invoice balance was changed. Pass --post to apply cash.",
            fg=typer.colors.BRIGHT_BLACK,
        )

    typer.secho(
        "  Auto-match rate alone means nothing -- a system that applies everything "
        "scores 100%.\n  Phase 6 measures precision against the answer key, which is "
        "the half that matters.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def evaluate(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Matching YAML. Defaults to config/matching.yml."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Data directory holding the answer key."),
    ] = None,
    use_advice: Annotated[
        bool, typer.Option("--advice/--no-advice", help="Use extracted remittance advice.")
    ] = True,
    markdown: Annotated[
        bool,
        typer.Option(
            "--markdown/--no-markdown",
            help="Write evaluation.md beside the data. On by default.",
        ),
    ] = True,
    ablation: Annotated[
        bool,
        typer.Option("--ablation", help="Measure what each signal is worth by removing it."),
    ] = False,
) -> None:
    """Score the engine against the ground-truth answer key.

    Runs the full pipeline, then compares every decision with what actually
    happened. The answer key is loaded only after matching has finished, so
    the matcher cannot have seen it.
    """
    from cashmatch.evaluation import (
        GroundTruthMissingError,
        render_text,
        signal_ablation,
        write_markdown,
    )
    from cashmatch.evaluation import evaluate as run_evaluation
    from cashmatch.matching import MatchingConfig

    settings = get_settings()
    output_root = output or settings.data_dir
    try:
        config = MatchingConfig.from_yaml(config_path or settings.matching_config)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    with session_scope() as session:
        try:
            report, rows, comparisons = run_evaluation(
                session, config, output_root, use_advice=use_advice
            )
        except GroundTruthMissingError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        typer.echo(render_text(report, rows))

        if ablation:
            _report_ablation(report, signal_ablation(session, config, output_root))

    if markdown:
        path = write_markdown(report, rows, Path(output_root) / "generated")
        typer.secho(f"  Markdown report written to {path}", fg=typer.colors.GREEN)

    if comparisons and report.auto_wrong == 0:
        typer.secho(
            "  No payment was auto-applied to the wrong invoice in this run.",
            fg=typer.colors.GREEN,
        )


def _report_ablation(baseline, variants) -> None:
    """Print what each signal is worth, measured by removing it."""
    typer.secho("-- signal ablation " + "-" * 59, bold=True)
    typer.echo(f"  {'signal removed':<24}{'auto rate':>11}{'precision':>12}{'delta auto':>12}")
    typer.echo(
        f"  {'(baseline)':<24}{baseline.auto_match_rate:>11.1%}"
        f"{baseline.auto_precision:>12.2%}{'':>12}"
    )
    for name, report in variants.items():
        delta = report.auto_match_rate - baseline.auto_match_rate
        typer.echo(
            f"  {name:<24}{report.auto_match_rate:>11.1%}{report.auto_precision:>12.2%}"
            f"{delta:>+12.1%}"
        )
    typer.echo("")


@app.command()
def roi(
    monthly_payments: Annotated[
        int | None, typer.Option("--volume", help="Payments a month. Defaults to the dataset.")
    ] = None,
    minutes: Annotated[
        float, typer.Option("--minutes", help="Minutes to match one payment by hand.")
    ] = 4.0,
    review_minutes: Annotated[
        float, typer.Option("--review-minutes", help="Minutes to clear one review item.")
    ] = 2.0,
    hourly_cost: Annotated[
        float, typer.Option("--hourly-cost", help="Fully loaded analyst cost per hour, INR.")
    ] = 600.0,
    error_hours: Annotated[
        float, typer.Option("--error-hours", help="Hours to unwind one misapplied payment.")
    ] = 3.0,
) -> None:
    """What the automation is worth, in hours and rupees.

    Rates come from the decisions recorded; effort and cost come from you.
    Every default is an assumption to replace, not a benchmark.
    """
    from cashmatch.evaluation import evaluate as run_evaluation
    from cashmatch.evaluation.runner import GroundTruthMissingError
    from cashmatch.matching import MatchingConfig
    from cashmatch.roi import RoiAssumptions, sensitivity
    from cashmatch.roi import calculate as roi_calculate

    settings = get_settings()
    config = MatchingConfig.from_yaml(settings.matching_config)

    with session_scope() as session:
        try:
            report, _rows, _comparisons = run_evaluation(session, config, settings.data_dir)
        except GroundTruthMissingError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

    assumptions = RoiAssumptions(
        monthly_payments=monthly_payments or report.total,
        minutes_per_payment=minutes,
        review_minutes=review_minutes,
        hourly_cost_rupees=hourly_cost,
        error_hours=error_hours,
    )

    try:
        outcome = roi_calculate(
            assumptions,
            auto_match_rate=report.auto_match_rate,
            review_rate=report.review_rate,
            unapplied_rate=report.unapplied_rate,
            precision=report.auto_precision,
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    _report_roi(outcome)

    typer.secho("\n-- what precision is worth " + "-" * 50, bold=True)
    typer.echo(f"  {'precision':>10}{'gross saving':>18}{'error cost':>18}{'net saving':>18}")
    for level, case in sensitivity(
        assumptions,
        auto_match_rate=report.auto_match_rate,
        review_rate=report.review_rate,
        unapplied_rate=report.unapplied_rate,
        precision=report.auto_precision,
    ):
        marker = " *" if abs(level - report.auto_precision) < 1e-9 else "  "
        typer.echo(
            f"{marker}{level:>8.0%}{format_inr(case.gross_saving_paise):>18}"
            f"{format_inr(case.expected_error_cost_paise):>18}"
            f"{format_inr(case.net_saving_paise):>18}"
        )
    typer.secho(
        "\n  The saving barely moves as precision drops -- the automation still happens.\n"
        "  The error term is what grows, until it swallows the benefit. That asymmetry\n"
        "  is the entire argument for holding precision above the bar.",
        fg=typer.colors.BRIGHT_BLACK,
    )


def _report_roi(outcome) -> None:
    a = outcome.assumptions
    typer.secho("\nReturn on investment", fg=typer.colors.GREEN, bold=True)
    typer.echo("  assumptions (yours to replace)")
    typer.echo(f"    volume              {a.monthly_payments:>10,} payments / month")
    typer.echo(f"    manual effort       {a.minutes_per_payment:>10.1f} min / payment")
    typer.echo(f"    review effort       {a.review_minutes:>10.1f} min / item")
    typer.echo(f"    analyst cost        {a.hourly_cost_rupees:>10,.0f} INR / hour")
    typer.echo(f"    cost of one error   {a.error_hours:>10.1f} hours to unwind")

    typer.echo("\n  measured")
    typer.echo(f"    auto-match rate     {outcome.auto_match_rate:>10.1%}")
    typer.echo(f"    review rate         {outcome.review_rate:>10.1%}")
    typer.echo(f"    unapplied rate      {outcome.unapplied_rate:>10.1%}")
    if outcome.precision is not None:
        typer.echo(f"    precision           {outcome.precision:>10.2%}")

    typer.echo("\n  effort")
    typer.echo(f"    today               {outcome.baseline_hours:>10.1f} hours / month")
    typer.echo(f"    with CashMatch      {outcome.remaining_hours:>10.1f} hours / month")
    typer.secho(
        f"    saved               {outcome.hours_saved:>10.1f} hours / month "
        f"({outcome.effort_reduction:.0%}, {outcome.analyst_days_saved:.1f} analyst-days)",
        fg=typer.colors.GREEN,
    )

    typer.echo("\n  money")
    typer.echo(f"    cost today          {format_inr(outcome.baseline_cost_paise):>18}")
    typer.echo(f"    cost with CashMatch {format_inr(outcome.remaining_cost_paise):>18}")
    typer.echo(f"    gross saving        {format_inr(outcome.gross_saving_paise):>18}")
    typer.echo(f"    expected error cost {format_inr(outcome.expected_error_cost_paise):>18}")
    typer.secho(
        f"    net saving          {format_inr(outcome.net_saving_paise):>18} / month",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.secho(
        f"    net saving          {format_inr(outcome.net_saving_per_year_paise):>18} / year",
        fg=typer.colors.GREEN,
    )


@app.command("show-payment")
def show_payment(
    statement_ref: Annotated[str, typer.Argument(help="Bank reference, e.g. UTR2026030100042")],
) -> None:
    """Print one generated payment and its advice, for eyeballing the data."""
    from sqlalchemy import select

    from cashmatch.models import BankTransaction, Remittance

    with session_scope() as session:
        txn = session.scalar(
            select(BankTransaction).where(BankTransaction.statement_ref == statement_ref)
        )
        if txn is None:
            typer.secho(
                f"No bank transaction with reference {statement_ref!r}. "
                "Run `cashmatch generate` first, or check the reference.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)

        typer.secho(f"{txn.statement_ref}", bold=True)
        typer.echo(f"  value date  {txn.value_date}")
        typer.echo(f"  amount      {format_inr(txn.amount_paise)}")
        typer.echo(f"  payer       {txn.payer_name_raw!r}")
        typer.echo(f"  normalised  {txn.payer_name_normalized!r}")
        typer.echo(f"  narration   {txn.narration!r}")

        advice = session.scalars(
            select(Remittance).where(Remittance.bank_transaction_id == txn.id)
        ).all()
        if not advice:
            typer.echo("\n  no linked remittance advice")
            return
        for item in advice:
            typer.secho(f"\n  advice {item.source_filename} ({item.source_type})", bold=True)
            for line in item.raw_text.splitlines():
                typer.echo(f"    {line}")


if __name__ == "__main__":
    app()
