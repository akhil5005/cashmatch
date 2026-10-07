"""Rendering the evaluation.

Two outputs from the same numbers: a terminal report for working, and a
Markdown file for the README and the client deployment summary. Keeping the
Markdown generated rather than hand-copied is the difference between results
that stay true and results that quietly go stale.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cashmatch.evaluation.comparison import Comparison
from cashmatch.evaluation.metrics import EvaluationReport
from cashmatch.evaluation.sweep import SweepRow, recommend
from cashmatch.money import format_inr

COST_ASYMMETRY = """\
Why precision matters more than coverage
----------------------------------------
The two ways this system fails do not cost the same.

A payment sent to review costs about two minutes: an analyst opens it, agrees
with the suggestion, clicks approve. Bounded, visible, and it still produces
the correct ledger entry.

A payment auto-applied to the wrong invoice marks one invoice paid that is
not, and leaves another open that should be closed. The customer's balance is
wrong in two places at once. A dunning letter goes to someone who already
paid. Someone disputes it, a reversal is raised, approved, posted and
reconciled. Hours across two teams, plus the relationship damage -- and none
of it starts until the customer complains, which may be weeks later.

So precision is a constraint and automation is what gets optimised underneath
it. The right question is never "how high can the auto-match rate go?" but
"what is the highest auto-match rate available while precision stays above the
bar finance will accept?" The sweep below is that question, answered.\
"""


def render_text(report: EvaluationReport, rows: list[SweepRow], *, width: int = 78) -> str:
    """The terminal report."""
    out: list[str] = []
    add = out.append

    add("=" * width)
    add("CashMatch evaluation".center(width))
    add("=" * width)
    add(f"  dataset seed      {report.seed}")
    add(f"  config digest     {report.config_digest}")
    auto_at, review_at = report.thresholds
    add(f"  thresholds        auto >= {auto_at:.2f}, review >= {review_at:.2f}")
    add(f"  payments judged   {report.total}")
    add(f"  value judged      {format_inr(report.total_value_paise)}")

    add("")
    add("-- headline " + "-" * (width - 12))
    add(
        f"  auto-match rate        {report.auto_match_rate:>7.1%}   "
        f"({report.auto_applied} payments)"
    )
    add(f"  precision of those     {report.auto_precision:>7.2%}   ({report.auto_wrong} wrong)")
    add(
        f"  precision by value     {report.value_precision:>7.2%}   "
        f"({format_inr(report.auto_wrong_value_paise)} misapplied)"
    )
    add(f"  review rate            {report.review_rate:>7.1%}   ({report.review} payments)")
    add(f"  unapplied rate         {report.unapplied_rate:>7.1%}   ({report.unapplied} payments)")
    add(f"  overall accuracy       {report.overall_accuracy:>7.1%}")
    if report.reason_scored:
        add(
            f"  deduction reason       {report.reason_accuracy:>7.1%}   "
            f"({report.reason_correct}/{report.reason_scored} categorised correctly)"
        )

    add("")
    add("-- where the errors are " + "-" * (width - 24))
    add(f"  wrong, auto-applied    {report.auto_wrong:>5}   <- the only costly failure")
    add(
        f"  wrong, but in review   {report.review - report.review_correct:>5}   "
        "a human will catch these"
    )
    add(f"  missed entirely        {report.missed:>5}   left as unapplied cash")
    add(f"  correct, in review     {report.review_correct:>5}   head-room if the threshold moves")

    add("")
    add("-- accuracy by scenario " + "-" * (width - 24))
    add(f"  {'scenario':<22}{'n':>5}{'auto':>8}{'precision':>11}{'accuracy':>10}")
    for name, metrics in sorted(report.by_scenario.items(), key=lambda kv: -kv[1].total):
        precision = f"{metrics.auto_precision:.1%}" if metrics.auto_applied else "-"
        add(
            f"  {name:<22}{metrics.total:>5}{metrics.auto_rate:>8.0%}"
            f"{precision:>11}{metrics.accuracy:>10.1%}"
        )

    add("")
    add("-- accuracy by strategy " + "-" * (width - 24))
    add(f"  {'strategy':<22}{'n':>5}{'auto':>8}{'precision':>11}{'accuracy':>10}")
    for name, metrics in sorted(report.by_strategy.items(), key=lambda kv: -kv[1].total):
        precision = f"{metrics.auto_precision:.1%}" if metrics.auto_applied else "-"
        add(
            f"  {name:<22}{metrics.total:>5}{metrics.auto_rate:>8.0%}"
            f"{precision:>11}{metrics.accuracy:>10.1%}"
        )

    add("")
    add("-- threshold sweep " + "-" * (width - 19))
    add(
        f"  {'auto >=':>8}{'auto rate':>11}{'precision':>11}{'errors':>8}"
        f"{'review':>9}{'value at risk':>16}"
    )
    for row in rows:
        marker = " *" if abs(row.threshold - report.thresholds[0]) < 1e-9 else "  "
        add(
            f"{marker}{row.threshold:>6.2f}{row.auto_rate:>11.1%}{row.precision:>11.2%}"
            f"{row.auto_wrong:>8}{row.review_rate:>9.1%}"
            f"{format_inr(row.value_at_risk_paise):>16}"
        )
    add("  * current setting")

    best = recommend(rows, min_precision=0.99)
    if best is not None:
        add("")
        add(f"  At a 99% precision floor, the most automation available is {best.auto_rate:.1%}")
        add(
            f"  at a threshold of {best.threshold:.2f} ({best.auto_wrong} errors across "
            f"{best.total} payments)."
        )

    if report.worst_errors:
        add("")
        add("-- costliest wrong auto-matches " + "-" * (width - 32))
        for error in report.worst_errors[:5]:
            add(
                f"  {error.statement_ref}  {format_inr(error.amount_paise):>16}  "
                f"{error.scenario} via {error.strategy}"
            )
            add(f"      expected {error.expected}")
            add(f"      proposed {error.proposed}")

    add("")
    add(COST_ASYMMETRY)
    add("")
    return "\n".join(out)


def render_markdown(report: EvaluationReport, rows: list[SweepRow]) -> str:
    """The Markdown results table, for the README and the client summary."""
    out: list[str] = []
    add = out.append

    add("# CashMatch evaluation results")
    add("")
    add(
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC} · seed `{report.seed}` · "
        f"config `{report.config_digest}`"
    )
    add("")
    add(
        f"Measured over **{report.total} payments** worth "
        f"**{format_inr(report.total_value_paise)}**, against a ground-truth file the "
        "matcher cannot read."
    )
    add("")

    add("## Headline")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(
        f"| **Auto-match rate** | **{report.auto_match_rate:.1%}** "
        f"({report.auto_applied} payments) |"
    )
    add(
        f"| **Precision of auto-applied** | **{report.auto_precision:.2%}** "
        f"({report.auto_wrong} wrong) |"
    )
    add(
        f"| Precision by value | {report.value_precision:.2%} "
        f"({format_inr(report.auto_wrong_value_paise)} misapplied) |"
    )
    add(f"| Review rate | {report.review_rate:.1%} ({report.review} payments) |")
    add(f"| Unapplied rate | {report.unapplied_rate:.1%} ({report.unapplied} payments) |")
    add(f"| Overall accuracy | {report.overall_accuracy:.1%} |")
    if report.reason_scored:
        add(
            f"| Deduction reason accuracy | {report.reason_accuracy:.1%} "
            f"({report.reason_correct}/{report.reason_scored}) |"
        )
    add("")

    add("## Accuracy by scenario")
    add("")
    add("| Scenario | n | Auto | Precision | Accuracy |")
    add("|---|---:|---:|---:|---:|")
    for name, metrics in sorted(report.by_scenario.items(), key=lambda kv: -kv[1].total):
        precision = f"{metrics.auto_precision:.1%}" if metrics.auto_applied else "—"
        add(
            f"| `{name}` | {metrics.total} | {metrics.auto_rate:.0%} | {precision} "
            f"| {metrics.accuracy:.1%} |"
        )
    add("")

    add("## Accuracy by strategy")
    add("")
    add("| Strategy | n | Auto | Precision | Accuracy |")
    add("|---|---:|---:|---:|---:|")
    for name, metrics in sorted(report.by_strategy.items(), key=lambda kv: -kv[1].total):
        precision = f"{metrics.auto_precision:.1%}" if metrics.auto_applied else "—"
        add(
            f"| `{name}` | {metrics.total} | {metrics.auto_rate:.0%} | {precision} "
            f"| {metrics.accuracy:.1%} |"
        )
    add("")

    add("## Threshold sweep")
    add("")
    add("Every row is a setting the client could choose. The columns are what they get for it.")
    add("")
    add("| Auto ≥ | Auto rate | Precision | Errors | Review rate | Value at risk |")
    add("|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        marker = " ←" if abs(row.threshold - report.thresholds[0]) < 1e-9 else ""
        add(
            f"| {row.threshold:.2f}{marker} | {row.auto_rate:.1%} | {row.precision:.2%} "
            f"| {row.auto_wrong} | {row.review_rate:.1%} "
            f"| {format_inr(row.value_at_risk_paise)} |"
        )
    add("")

    best = recommend(rows, min_precision=0.99)
    if best is not None:
        add(
            f"At a **99% precision floor**, the most automation available is "
            f"**{best.auto_rate:.1%}** at a threshold of `{best.threshold:.2f}` "
            f"({best.auto_wrong} errors across {best.total} payments)."
        )
        add("")

    add("## Why precision matters more than coverage")
    add("")
    add(
        "A payment sent to review costs about two minutes of analyst time, and still "
        "produces the correct ledger entry. A payment auto-applied to the **wrong** "
        "invoice marks one invoice paid that is not and leaves another open that should "
        "be closed — the customer's balance is wrong in two places at once. A dunning "
        "letter goes to someone who already paid; a reversal has to be raised, approved, "
        "posted and reconciled. Hours across two teams, and none of it starts until the "
        "customer complains."
    )
    add("")
    add(
        "So precision is a constraint and automation is optimised underneath it. The "
        "sweep above exists so that trade is made with numbers rather than nerve."
    )
    add("")

    return "\n".join(out)


def write_markdown(report: EvaluationReport, rows: list[SweepRow], directory: Path) -> Path:
    """Write the Markdown report beside the generated data."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "evaluation.md"
    path.write_text(render_markdown(report, rows), encoding="utf-8")
    return path


def errors_as_rows(comparisons: list[Comparison]) -> list[dict[str, object]]:
    """Every costly failure, for eyeballing or exporting."""
    return [
        {
            "statement_ref": item.statement_ref,
            "scenario": item.scenario,
            "strategy": item.strategy,
            "confidence": round(item.confidence, 4),
            "amount_paise": item.amount_paise,
            "expected": item.expected,
            "proposed": item.proposed,
        }
        for item in comparisons
        if item.is_costly
    ]
