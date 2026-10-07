"""What the automation is worth, in hours and rupees.

Every number here is arithmetic over two things the client supplies (how long
a payment takes to apply by hand, and what an analyst hour costs) and one
thing the system measures (the auto-match rate). Nothing is researched,
benchmarked or borrowed from a vendor deck.

**The defaults are assumptions, not findings.** They are starting points for
a conversation, labelled as such everywhere they surface, and a client who
replaces them with their own figures gets their own answer. A business case
built on someone else's averages is not a business case.

The model has three buckets rather than two, because "automated" is not the
same as "free":

* an **auto-applied** payment costs nothing;
* a payment in **review** still costs an analyst's attention, just less of it
  than matching from scratch, because the suggestion and its reasoning are
  already on screen;
* an **unapplied** payment costs the full manual effort, and someone still
  has to investigate it.

And a fourth line that is not a saving at all: the **expected cost of wrong
auto-matches**. At the measured precision this is zero, but the row stays
visible, because the whole argument of this system is that it is the term
which would dominate if precision slipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from cashmatch.money import Paise, format_inr

#: Minutes to match one payment by hand, start to finish. A starting point
#: for a conversation -- a distributor with clean remittance habits will be
#: faster, one reconciling from PDFs will be slower.
DEFAULT_MINUTES_PER_PAYMENT = 4.0

#: Minutes to clear one review item. Lower than manual because the candidate
#: invoices and the reasoning are already on screen; the analyst is agreeing
#: or disagreeing, not searching.
DEFAULT_REVIEW_MINUTES = 2.0

#: Fully loaded cost of an AR analyst hour, in rupees.
DEFAULT_HOURLY_COST_RUPEES = 600.0

#: Hours to unwind one misapplied payment: spot it, raise the reversal, get
#: it approved, post it, reconcile, and talk to the customer who received a
#: dunning letter for an invoice they had already paid.
DEFAULT_ERROR_HOURS = 3.0


@dataclass(slots=True)
class RoiAssumptions:
    """What the client supplies. All of it is meant to be overridden."""

    monthly_payments: int
    minutes_per_payment: float = DEFAULT_MINUTES_PER_PAYMENT
    review_minutes: float = DEFAULT_REVIEW_MINUTES
    hourly_cost_rupees: float = DEFAULT_HOURLY_COST_RUPEES
    error_hours: float = DEFAULT_ERROR_HOURS

    def validate(self) -> None:
        """Refuse inputs that would produce a meaningless answer."""
        if self.monthly_payments <= 0:
            raise ValueError("Monthly payment volume must be more than zero.")
        if self.minutes_per_payment <= 0:
            raise ValueError("Minutes per payment must be more than zero.")
        if self.review_minutes < 0:
            raise ValueError("Review minutes cannot be negative.")
        if self.review_minutes > self.minutes_per_payment:
            raise ValueError(
                f"Reviewing a suggestion ({self.review_minutes} min) should not take longer "
                f"than matching from scratch ({self.minutes_per_payment} min). If it does, "
                "the review screen is the problem, not the matcher."
            )
        if self.hourly_cost_rupees <= 0:
            raise ValueError("The analyst hourly cost must be more than zero.")
        if self.error_hours < 0:
            raise ValueError("Hours to unwind an error cannot be negative.")


@dataclass(slots=True)
class RoiOutcome:
    """Hours and rupees, before and after."""

    assumptions: RoiAssumptions
    auto_match_rate: float
    review_rate: float
    unapplied_rate: float
    precision: float | None

    baseline_hours: float
    remaining_hours: float
    hours_saved: float

    baseline_cost_paise: Paise
    remaining_cost_paise: Paise
    gross_saving_paise: Paise
    expected_error_cost_paise: Paise
    net_saving_paise: Paise

    @property
    def hours_saved_per_year(self) -> float:
        return self.hours_saved * 12

    @property
    def net_saving_per_year_paise(self) -> int:
        return self.net_saving_paise * 12

    @property
    def effort_reduction(self) -> float:
        """Share of the manual effort that disappears."""
        return self.hours_saved / self.baseline_hours if self.baseline_hours else 0.0

    @property
    def analyst_days_saved(self) -> float:
        """Hours expressed as eight-hour days, which is how a manager thinks
        about headcount."""
        return self.hours_saved / 8

    def as_dict(self) -> dict[str, object]:
        return {
            "assumptions": {
                "monthly_payments": self.assumptions.monthly_payments,
                "minutes_per_payment": self.assumptions.minutes_per_payment,
                "review_minutes": self.assumptions.review_minutes,
                "hourly_cost_rupees": self.assumptions.hourly_cost_rupees,
                "error_hours": self.assumptions.error_hours,
            },
            "measured": {
                "auto_match_rate": round(self.auto_match_rate, 4),
                "review_rate": round(self.review_rate, 4),
                "unapplied_rate": round(self.unapplied_rate, 4),
                "precision": None if self.precision is None else round(self.precision, 4),
            },
            "hours": {
                "baseline": round(self.baseline_hours, 1),
                "remaining": round(self.remaining_hours, 1),
                "saved": round(self.hours_saved, 1),
                "saved_per_year": round(self.hours_saved_per_year, 1),
                "analyst_days_saved": round(self.analyst_days_saved, 1),
                "effort_reduction": round(self.effort_reduction, 4),
            },
            "money": {
                "baseline_cost": _money(self.baseline_cost_paise),
                "remaining_cost": _money(self.remaining_cost_paise),
                "gross_saving": _money(self.gross_saving_paise),
                "expected_error_cost": _money(self.expected_error_cost_paise),
                "net_saving": _money(self.net_saving_paise),
                "net_saving_per_year": _money(self.net_saving_per_year_paise),
            },
        }


def _money(paise: int) -> dict[str, object]:
    return {"paise": int(paise), "display": format_inr(int(paise))}


def calculate(
    assumptions: RoiAssumptions,
    *,
    auto_match_rate: float,
    review_rate: float,
    unapplied_rate: float,
    precision: float | None = None,
) -> RoiOutcome:
    """Work out the monthly saving at a measured auto-match rate.

    Args:
        assumptions: the client's own figures.
        auto_match_rate: share of payments cleared with no human.
        review_rate: share routed to a human with a suggestion in hand.
        unapplied_rate: share the engine could not place at all.
        precision: measured precision of the auto-applied matches. When
            known, the expected cost of the errors it implies is subtracted
            from the saving. When unknown, that term is omitted rather than
            assumed to be zero -- and the caller is told which.
    """
    assumptions.validate()

    volume = assumptions.monthly_payments
    hourly = assumptions.hourly_cost_rupees

    baseline_hours = volume * assumptions.minutes_per_payment / 60

    # Auto-applied payments cost nothing. Review items cost a shorter look.
    # Unapplied cash still costs a full manual investigation -- the engine
    # has not saved that work, it has only declined to guess at it.
    review_hours = volume * review_rate * assumptions.review_minutes / 60
    unapplied_hours = volume * unapplied_rate * assumptions.minutes_per_payment / 60
    remaining_hours = review_hours + unapplied_hours

    hours_saved = baseline_hours - remaining_hours

    baseline_cost = _to_paise(baseline_hours * hourly)
    remaining_cost = _to_paise(remaining_hours * hourly)
    gross_saving = baseline_cost - remaining_cost

    # The term that would dominate if precision slipped. Kept visible even
    # when it is zero, because that is the whole argument of the system.
    error_cost = 0
    if precision is not None:
        wrong = volume * auto_match_rate * (1 - precision)
        error_cost = _to_paise(wrong * assumptions.error_hours * hourly)

    return RoiOutcome(
        assumptions=assumptions,
        auto_match_rate=auto_match_rate,
        review_rate=review_rate,
        unapplied_rate=unapplied_rate,
        precision=precision,
        baseline_hours=baseline_hours,
        remaining_hours=remaining_hours,
        hours_saved=hours_saved,
        baseline_cost_paise=Paise(baseline_cost),
        remaining_cost_paise=Paise(remaining_cost),
        gross_saving_paise=Paise(gross_saving),
        expected_error_cost_paise=Paise(error_cost),
        net_saving_paise=Paise(gross_saving - error_cost),
    )


def _to_paise(rupees: float) -> int:
    """Round a derived rupee figure to whole paise.

    Rounding is correct *here* and nowhere else in the codebase. These are
    projections built from estimates, not ledger entries; the exactness rule
    protects money that actually moved, and pretending a forecast is exact
    would be its own kind of dishonesty.
    """
    return int(round(rupees * 100))


def sensitivity(
    assumptions: RoiAssumptions,
    *,
    auto_match_rate: float,
    review_rate: float,
    unapplied_rate: float,
    precision: float | None,
    precisions: list[float] | None = None,
) -> list[tuple[float, RoiOutcome]]:
    """The same case at several precision levels.

    This is the cost asymmetry expressed in rupees. The saving barely moves
    as precision drops -- the automation is still happening -- but the error
    term grows until it swallows the benefit entirely. A controller who sees
    this table does not need to be persuaded that precision matters.
    """
    levels = precisions or [1.0, 0.99, 0.97, 0.95, 0.90]
    return [
        (
            level,
            calculate(
                assumptions,
                auto_match_rate=auto_match_rate,
                review_rate=review_rate,
                unapplied_rate=unapplied_rate,
                precision=level,
            ),
        )
        for level in levels
    ]
