"""Phase 2 -- synthetic AR data for a fictional Indian FMCG distributor.

The dataset is reproducible from a seed and shaped by ``config/scenarios.yml``.
Its answer key is written to a file on disk and never loaded into the database
the matcher queries, so accuracy measured in Phase 6 is real.
"""

from cashmatch.generator.config import GeneratorConfig, ScenarioLabel
from cashmatch.generator.engine import (
    GenerationSummary,
    database_is_populated,
    generate_dataset,
    load_ground_truth,
    reset_database,
)
from cashmatch.generator.ground_truth import (
    GROUND_TRUTH_FILENAME,
    GroundTruth,
    TruthAllocation,
    TruthPayment,
)

__all__ = [
    "GROUND_TRUTH_FILENAME",
    "GenerationSummary",
    "GeneratorConfig",
    "GroundTruth",
    "ScenarioLabel",
    "TruthAllocation",
    "TruthPayment",
    "database_is_populated",
    "generate_dataset",
    "load_ground_truth",
    "reset_database",
]
