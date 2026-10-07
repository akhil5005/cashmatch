"""Phase 6 -- measuring the engine against the answer key.

The only module here that opens the ground-truth file is
:mod:`cashmatch.evaluation.runner`, and it opens it *after* matching and
scoring have finished. That ordering is the integrity of every number the
report prints.

Two metrics carry the weight, and only as a pair: the **auto-match rate**
says how much work disappears, and the **precision of auto-applied matches**
says how often the work that disappeared was done correctly. Either alone is
meaningless.
"""

from cashmatch.evaluation.comparison import Comparison, Verdict, compare
from cashmatch.evaluation.metrics import EvaluationReport, ScenarioMetrics, build_report
from cashmatch.evaluation.report import (
    errors_as_rows,
    render_markdown,
    render_text,
    write_markdown,
)
from cashmatch.evaluation.runner import GroundTruthMissingError, evaluate, signal_ablation
from cashmatch.evaluation.sweep import SweepRow, recommend, rescore, sweep_thresholds

__all__ = [
    "Comparison",
    "EvaluationReport",
    "GroundTruthMissingError",
    "ScenarioMetrics",
    "SweepRow",
    "Verdict",
    "build_report",
    "compare",
    "errors_as_rows",
    "evaluate",
    "recommend",
    "render_markdown",
    "render_text",
    "rescore",
    "signal_ablation",
    "sweep_thresholds",
    "write_markdown",
]
