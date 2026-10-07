"""The business case.

The arithmetic is simple; what matters is that it is *honest*. Three things
these tests exist to hold in place: unapplied cash is not counted as a
saving, a review item is not counted as free, and the cost of wrong
auto-matches is subtracted rather than ignored.

Get any of those wrong and the model produces a flattering number that falls
apart the first time a client checks it.
"""

from __future__ import annotations

import pytest

from cashmatch.money import rupees_to_paise
from cashmatch.roi import RoiAssumptions, calculate, sensitivity


def _case(**overrides) -> RoiAssumptions:
    defaults = {
        "monthly_payments": 800,
        "minutes_per_payment": 4.0,
        "review_minutes": 2.0,
        "hourly_cost_rupees": 600.0,
        "error_hours": 3.0,
    }
    return RoiAssumptions(**{**defaults, **overrides})


def _measured(**overrides):
    rates = {
        "auto_match_rate": 0.819,
        "review_rate": 0.130,
        "unapplied_rate": 0.051,
        "precision": 1.0,
    }
    return {**rates, **overrides}


# ===========================================================================
# the arithmetic
# ===========================================================================


def test_the_baseline_is_volume_times_effort() -> None:
    outcome = calculate(_case(), **_measured())
    # 800 payments x 4 minutes = 3200 minutes = 53.33 hours.
    assert outcome.baseline_hours == pytest.approx(800 * 4 / 60)


def test_an_auto_applied_payment_costs_nothing() -> None:
    """At 100% automation there is no manual effort left at all."""
    outcome = calculate(
        _case(), auto_match_rate=1.0, review_rate=0.0, unapplied_rate=0.0, precision=1.0
    )
    assert outcome.remaining_hours == 0
    assert outcome.hours_saved == outcome.baseline_hours
    assert outcome.effort_reduction == 1.0


def test_a_review_item_is_not_counted_as_free() -> None:
    """It still costs an analyst's attention -- less, because the suggestion
    and its reasoning are already on screen, but not nothing."""
    outcome = calculate(
        _case(), auto_match_rate=0.0, review_rate=1.0, unapplied_rate=0.0, precision=1.0
    )
    assert outcome.remaining_hours == pytest.approx(800 * 2 / 60)
    assert outcome.hours_saved > 0  # still cheaper than matching from scratch


def test_unapplied_cash_is_not_counted_as_a_saving() -> None:
    """The engine has not saved that work, it has only declined to guess at
    it. Somebody still has to investigate every one."""
    outcome = calculate(
        _case(), auto_match_rate=0.0, review_rate=0.0, unapplied_rate=1.0, precision=None
    )
    assert outcome.remaining_hours == pytest.approx(outcome.baseline_hours)
    assert outcome.hours_saved == pytest.approx(0.0)
    assert outcome.net_saving_paise == 0


def test_cost_follows_hours_at_the_given_rate() -> None:
    outcome = calculate(_case(hourly_cost_rupees=1000.0), **_measured())
    assert outcome.baseline_cost_paise == rupees_to_paise(f"{outcome.baseline_hours * 1000:.2f}")


def test_the_yearly_figure_is_twelve_months() -> None:
    outcome = calculate(_case(), **_measured())
    assert outcome.net_saving_per_year_paise == outcome.net_saving_paise * 12
    assert outcome.hours_saved_per_year == pytest.approx(outcome.hours_saved * 12)


def test_analyst_days_are_eight_hour_days() -> None:
    """How a manager thinks about headcount."""
    outcome = calculate(_case(), **_measured())
    assert outcome.analyst_days_saved == pytest.approx(outcome.hours_saved / 8)


# ===========================================================================
# errors are subtracted, not ignored
# ===========================================================================


def test_perfect_precision_costs_nothing() -> None:
    outcome = calculate(_case(), **_measured(precision=1.0))
    assert outcome.expected_error_cost_paise == 0
    assert outcome.net_saving_paise == outcome.gross_saving_paise


def test_imperfect_precision_is_charged_for() -> None:
    perfect = calculate(_case(), **_measured(precision=1.0))
    imperfect = calculate(_case(), **_measured(precision=0.95))

    assert imperfect.gross_saving_paise == perfect.gross_saving_paise
    assert imperfect.expected_error_cost_paise > 0
    assert imperfect.net_saving_paise < perfect.net_saving_paise


def test_the_error_cost_is_wrong_matches_times_hours_times_rate() -> None:
    outcome = calculate(_case(), **_measured(precision=0.95))
    wrong = 800 * 0.819 * 0.05
    assert outcome.expected_error_cost_paise == pytest.approx(
        round(wrong * 3.0 * 600.0 * 100), rel=1e-6
    )


def test_unknown_precision_omits_the_term_rather_than_assuming_zero() -> None:
    """Production has no answer key. Silently treating that as perfect would
    produce exactly the flattering number this model exists to avoid."""
    outcome = calculate(_case(), **_measured(precision=None))
    assert outcome.precision is None
    assert outcome.expected_error_cost_paise == 0
    assert outcome.as_dict()["measured"]["precision"] is None


def test_a_low_enough_precision_makes_the_system_net_negative() -> None:
    """The whole thesis of the project, in rupees: at some precision the
    automation costs more than it saves, and no amount of coverage fixes
    that."""
    outcome = calculate(_case(), **_measured(precision=0.90))
    assert outcome.net_saving_paise < 0


# ===========================================================================
# the sensitivity table
# ===========================================================================


def test_sensitivity_holds_the_saving_and_moves_the_error() -> None:
    """The saving barely shifts as precision drops -- the automation still
    happens. The error term is what grows."""
    rows = sensitivity(_case(), **_measured())

    gross = {outcome.gross_saving_paise for _level, outcome in rows}
    assert len(gross) == 1  # the saving does not depend on precision

    errors = [outcome.expected_error_cost_paise for _level, outcome in rows]
    assert errors == sorted(errors)  # lower precision, higher cost


def test_sensitivity_crosses_from_positive_to_negative() -> None:
    rows = sensitivity(_case(), **_measured())
    nets = [outcome.net_saving_paise for _level, outcome in rows]

    assert nets[0] > 0  # at 100% precision
    assert nets[-1] < 0  # at 90%


def test_sensitivity_levels_can_be_chosen() -> None:
    rows = sensitivity(_case(), **_measured(), precisions=[1.0, 0.5])
    assert [level for level, _ in rows] == [1.0, 0.5]


# ===========================================================================
# inputs that would produce a meaningless answer
# ===========================================================================


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"monthly_payments": 0}, "more than zero"),
        ({"minutes_per_payment": 0}, "more than zero"),
        ({"hourly_cost_rupees": 0}, "more than zero"),
        ({"review_minutes": -1}, "cannot be negative"),
        ({"error_hours": -1}, "cannot be negative"),
    ],
)
def test_nonsense_inputs_are_refused(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        calculate(_case(**overrides), **_measured())


def test_reviewing_cannot_cost_more_than_matching_from_scratch() -> None:
    """If it does, the review screen is the problem, not the matcher — and
    the error message says so."""
    with pytest.raises(ValueError, match="review screen is the problem"):
        calculate(_case(minutes_per_payment=2.0, review_minutes=5.0), **_measured())


# ===========================================================================
# serialisation
# ===========================================================================


def test_every_money_value_serialises_as_integer_paise() -> None:
    body = calculate(_case(), **_measured()).as_dict()
    for key, value in body["money"].items():
        assert isinstance(value["paise"], int), key
        assert "₹" in value["display"], key


def test_the_payload_states_what_it_assumed() -> None:
    """A business case whose assumptions are invisible is a business case
    nobody can argue with, which makes it worthless."""
    body = calculate(_case(minutes_per_payment=7.5), **_measured()).as_dict()

    assert body["assumptions"]["minutes_per_payment"] == 7.5
    assert body["assumptions"]["hourly_cost_rupees"] == 600.0
    assert body["measured"]["auto_match_rate"] == 0.819


def test_the_payload_round_trips_as_json() -> None:
    import json

    body = calculate(_case(), **_measured()).as_dict()
    assert json.loads(json.dumps(body)) == body
