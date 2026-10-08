"""The bounded subset-sum search.

Three properties carry the weight here: it finds what exists, it never
exceeds its budget, and it is deterministic. The last one is easy to lose and
expensive to lose -- a search whose answer depends on dict ordering makes
every accuracy comparison between runs meaningless.
"""

from __future__ import annotations

from math import comb

import pytest

from cashmatch.matching import find_subsets, theoretical_space
from cashmatch.money import rupees_to_paise

from .support import make_item


def _numbers(solutions) -> list[list[str]]:
    return [sorted(item.invoice_number for item in solution) for solution in solutions]


# --- finding what exists ----------------------------------------------------


def test_finds_an_exact_pair() -> None:
    pool = [make_item(1, "1000"), make_item(2, "2500"), make_item(3, "9999")]
    target = rupees_to_paise("3500")

    result = find_subsets(pool, target_low=target, target_high=target, min_size=2, max_size=3)

    assert result.found
    assert ["INV-00001", "INV-00002"] in _numbers(result.solutions)


def test_finds_a_triple() -> None:
    pool = [make_item(i, amount) for i, amount in enumerate(["100", "250", "375", "9000"], 1)]
    target = rupees_to_paise("725")

    result = find_subsets(pool, target_low=target, target_high=target, min_size=2, max_size=3)

    assert _numbers(result.solutions) == [["INV-00001", "INV-00002", "INV-00003"]]


def test_respects_the_tolerance_band() -> None:
    """A rupee of bank rounding should not cost a match."""
    pool = [make_item(1, "1000"), make_item(2, "2500")]
    target = rupees_to_paise("3500") + 60  # 60 paise over

    tight = find_subsets(pool, target_low=target, target_high=target, min_size=2, max_size=2)
    loose = find_subsets(
        pool, target_low=target - 100, target_high=target + 100, min_size=2, max_size=2
    )

    assert not tight.found
    assert loose.found


def test_min_size_excludes_single_invoices() -> None:
    """Step 4 already covers the single-invoice case, so subset-sum skips it."""
    pool = [make_item(1, "3500"), make_item(2, "1000"), make_item(3, "2500")]
    target = rupees_to_paise("3500")

    result = find_subsets(pool, target_low=target, target_high=target, min_size=2, max_size=3)

    assert ["INV-00001"] not in _numbers(result.solutions)
    assert ["INV-00002", "INV-00003"] in _numbers(result.solutions)


def test_no_solution_is_reported_honestly() -> None:
    pool = [make_item(1, "1000"), make_item(2, "2000")]
    target = rupees_to_paise("4321")

    result = find_subsets(pool, target_low=target, target_high=target, min_size=1, max_size=2)

    assert not result.found
    assert result.complete


def test_equal_amounts_produce_distinct_solutions() -> None:
    """Three invoices for the same value: every pair is a real alternative,
    and the search must surface the ambiguity rather than pick one."""
    pool = [make_item(i, "1500") for i in (1, 2, 3)]
    target = rupees_to_paise("3000")

    result = find_subsets(
        pool, target_low=target, target_high=target, min_size=2, max_size=2, max_solutions=5
    )

    assert len(result.solutions) == 3
    assert len(_numbers(result.solutions)) == len({tuple(s) for s in _numbers(result.solutions)})


def test_a_band_target_finds_sets_above_the_payment() -> None:
    """Short-pay detection asks for totals *exceeding* the payment."""
    pool = [make_item(1, "10000"), make_item(2, "5000")]
    amount = rupees_to_paise("14000")

    result = find_subsets(
        pool, target_low=amount + 1, target_high=amount + rupees_to_paise("2000"), min_size=1
    )

    assert _numbers(result.solutions) == [["INV-00001", "INV-00002"]]


# --- limits -----------------------------------------------------------------


def test_combination_size_cap_is_respected() -> None:
    """A genuine four-invoice bundle is invisible at k=3. That is the
    documented trade: it goes to review instead of being guessed at."""
    pool = [make_item(i, "1000") for i in range(1, 6)]
    target = rupees_to_paise("4000")

    capped = find_subsets(pool, target_low=target, target_high=target, min_size=2, max_size=3)
    allowed = find_subsets(pool, target_low=target, target_high=target, min_size=2, max_size=4)

    assert not capped.found
    assert allowed.found
    assert all(len(solution) == 4 for solution in allowed.solutions)


def test_solution_cap_stops_the_search_early() -> None:
    pool = [make_item(i, "1000") for i in range(1, 10)]
    target = rupees_to_paise("2000")

    result = find_subsets(
        pool, target_low=target, target_high=target, min_size=2, max_size=2, max_solutions=3
    )

    assert len(result.solutions) == 3
    assert result.solution_cap_reached
    assert not result.complete


def test_budget_exhaustion_is_reported_not_hidden() -> None:
    """One pathological account must not stall a batch, and when the search
    gives up the caller has to be able to tell."""
    # Thirty equal invoices and a target needing twenty of them: reachable in
    # principle, so the suffix prune stays quiet and the search really does
    # have to enumerate, but unreachable at k=5 so it never finishes.
    pool = [make_item(i, "100") for i in range(1, 31)]
    target = rupees_to_paise("2000")

    result = find_subsets(
        pool, target_low=target, target_high=target, min_size=2, max_size=5, budget=50
    )

    assert result.budget_exhausted
    assert not result.complete
    assert not result.found
    assert result.nodes_visited <= 55  # the budget, plus frames already in flight


def test_empty_pool_is_safe() -> None:
    result = find_subsets([], target_low=0, target_high=1000)
    assert not result.found
    assert result.pool_size == 0


# --- determinism ------------------------------------------------------------


def test_the_same_inputs_always_give_the_same_answer() -> None:
    """Results from two runs must be comparable, so the search cannot depend
    on insertion order."""
    amounts = [str(1000 + (i * 7919) % 90000) for i in range(1, 40)]
    forward = [make_item(i, amount) for i, amount in enumerate(amounts, 1)]
    backward = list(reversed(forward))
    target = forward[2].open_amount_paise + forward[11].open_amount_paise

    first = find_subsets(forward, target_low=target, target_high=target, min_size=2, max_size=3)
    second = find_subsets(backward, target_low=target, target_high=target, min_size=2, max_size=3)

    assert _numbers(first.solutions) == _numbers(second.solutions)
    assert first.nodes_visited == second.nodes_visited


def test_invoices_of_equal_value_are_ordered_by_number() -> None:
    """The tiebreak that makes the ordering total."""
    pool = [make_item(3, "500"), make_item(1, "500"), make_item(2, "500")]
    target = rupees_to_paise("1000")

    result = find_subsets(
        pool, target_low=target, target_high=target, min_size=2, max_size=2, max_solutions=1
    )

    assert _numbers(result.solutions) == [["INV-00001", "INV-00002"]]


# --- the pruning actually prunes --------------------------------------------


def test_pruning_visits_a_tiny_fraction_of_the_admitted_space() -> None:
    """The complexity claim in the module docstring, asserted rather than
    trusted: the size cap admits over a million combinations and the
    arithmetic prunes bring the real cost down by orders of magnitude."""
    pool = [make_item(i, str(1000 + (i * 7919) % 90000)) for i in range(1, 46)]
    target = pool[3].open_amount_paise + pool[17].open_amount_paise

    result = find_subsets(
        pool, target_low=target, target_high=target, min_size=2, max_size=5, max_solutions=1
    )

    admitted = theoretical_space(45, 5)
    assert admitted > 1_000_000
    assert result.nodes_visited < admitted / 1000


@pytest.mark.parametrize(
    ("pool_size", "max_size"),
    [(10, 3), (45, 5), (20, 1)],
)
def test_theoretical_space_matches_the_binomial_sum(pool_size: int, max_size: int) -> None:
    expected = sum(comb(pool_size, size) for size in range(1, max_size + 1))
    assert theoretical_space(pool_size, max_size) == expected


def test_theoretical_space_caps_at_the_pool_size() -> None:
    """Asking for 5-invoice combinations from a pool of 3 is C(3,1..3)."""
    assert theoretical_space(3, 5) == 3 + 3 + 1
