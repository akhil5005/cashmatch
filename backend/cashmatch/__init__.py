"""CashMatch: an AI cash application agent for Order-to-Cash."""

__version__ = "0.1.0"

# Stamped onto every MatchResult so decisions made by different engine builds
# can be told apart when evaluating accuracy over time.
ENGINE_VERSION = f"matcher@{__version__}"
