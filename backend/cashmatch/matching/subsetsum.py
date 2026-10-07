"""Bounded subset-sum over a customer's open items.

The problem
-----------
A single bank credit may settle any combination of the customer's open
invoices. Finding which combination sums to the payment is subset-sum, which
is NP-complete in general. All of the engineering is therefore in the pruning,
not in the search itself.

Complexity
----------
Unpruned, the search space is the power set: **O(2^n)** for n candidate
invoices. Three prunes bring that down:

1. **Customer scoping** (applied before this module runs) cuts n from every
   invoice in the business to one customer's open items -- 14 to 199 in the
   generated book, median 28.
2. **Date window** (also upstream) trims it further.
3. **Combination-size cap `k`** bounds the enumeration at
   ``sum(C(n, i) for i in 1..k)``, which is **O(n^k / k!)** -- polynomial in n
   for fixed k, rather than exponential.

Concretely, with n = 45 and k = 5:

=====================  ====================
unpruned power set     35,184,372,088,832
C(n, <=5)                       1,385,979
with the sum prunes     tens to hundreds of nodes
=====================  ====================

On top of the size cap, two arithmetic prunes cut most of what remains. All
amounts are positive, so:

* if the running total already exceeds the upper target, every extension of
  this branch also exceeds it -- abandon the branch;
* if the running total plus *everything still available* falls below the
  lower target, no extension can reach it -- abandon the branch.

Sorting candidates descending makes both fire early and often.

The cost of the cap
-------------------
A genuine seven-invoice bundle will not be found when k = 5. That is the
correct failure: the payment goes to review rather than producing a confident
wrong answer. In cash application a missed automation costs minutes; a wrong
auto-match costs hours and a customer relationship.

A note on exact amounts
-----------------------
Every amount here is an integer count of paise, so the sum comparisons are
exact equality within an explicit tolerance -- not floating-point
near-equality. Subset-sum over floats would be unanswerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import comb

from cashmatch.matching.candidates import OpenItem


@dataclass(slots=True)
class SubsetSearchResult:
    """Solutions found, plus what the search cost."""

    #: Each solution is a list of items whose open amounts fall in the target band.
    solutions: list[list[OpenItem]] = field(default_factory=list)
    #: Combinations whose sum was actually evaluated.
    nodes_visited: int = 0
    #: Branches abandoned by an arithmetic prune.
    branches_pruned: int = 0
    #: True when the budget ran out before the space was exhausted.
    budget_exhausted: bool = False
    #: True when max_solutions was reached and the search stopped early.
    solution_cap_reached: bool = False
    pool_size: int = 0

    @property
    def found(self) -> bool:
        return bool(self.solutions)

    @property
    def complete(self) -> bool:
        """True when the search space was fully explored within its limits."""
        return not (self.budget_exhausted or self.solution_cap_reached)


def find_subsets(
    pool: list[OpenItem],
    *,
    target_low: int,
    target_high: int,
    min_size: int = 1,
    max_size: int = 4,
    max_solutions: int = 5,
    budget: int = 250_000,
) -> SubsetSearchResult:
    """Find subsets of `pool` whose open amounts total into [low, high].

    A *band* rather than a point target, because the same search serves two
    strategies: an exact match uses a narrow band around the payment, and
    short-pay detection uses a band sitting above it by a plausible
    deduction.

    Determinism is a hard requirement -- the same inputs must always yield
    the same solutions in the same order, or results are not comparable
    between runs. Candidates are therefore sorted by ``(-amount,
    invoice_number)``, giving a total order even when two invoices are for
    the same amount.

    Args:
        pool: candidate open items, already scoped to one customer and a
            date window.
        target_low: inclusive lower bound on the subset total, in paise.
        target_high: inclusive upper bound on the subset total, in paise.
        min_size: smallest subset to report. Pass 2 to skip single invoices
            when an earlier strategy has already covered them.
        max_size: the `k` in C(n, k). The main complexity lever.
        max_solutions: stop once this many distinct subsets qualify. More
            than a handful means the payment is ambiguous, not that the
            search should keep going.
        budget: abort after this many nodes, so one pathological account
            cannot stall a batch run.
    """
    result = SubsetSearchResult(pool_size=len(pool))
    if not pool or target_high < 0 or max_size < 1:
        return result

    # Descending amount makes both arithmetic prunes bite early; the
    # invoice number is the tiebreak that makes the order total.
    ordered = sorted(pool, key=lambda item: (-item.open_amount_paise, item.invoice_number))
    amounts = [item.open_amount_paise for item in ordered]
    size = len(ordered)

    # suffix_total[i] is the sum of everything from i onwards, which is the
    # most any branch starting at i can still add.
    suffix_total = [0] * (size + 1)
    for index in range(size - 1, -1, -1):
        suffix_total[index] = suffix_total[index + 1] + amounts[index]

    chosen: list[int] = []

    def search(start: int, running: int) -> None:
        if len(result.solutions) >= max_solutions:
            result.solution_cap_reached = True
            return
        if result.nodes_visited >= budget:
            result.budget_exhausted = True
            return

        for index in range(start, size):
            value = amounts[index]
            total = running + value

            # Amounts are positive and sorted descending, so once this one
            # overshoots, every later (smaller) one might still fit -- keep
            # scanning, but do not descend from here.
            if total > target_high:
                result.branches_pruned += 1
                continue

            # Nothing from here onwards can lift the running total to the
            # band. Because the suffix sums shrink as the index grows, that is
            # true of every later start too -- so abandon the whole tail.
            if running + suffix_total[index] < target_low:
                result.branches_pruned += 1
                break

            if result.nodes_visited >= budget:
                result.budget_exhausted = True
                return

            chosen.append(index)
            result.nodes_visited += 1

            if target_low <= total <= target_high and len(chosen) >= min_size:
                result.solutions.append([ordered[i] for i in chosen])
                if len(result.solutions) >= max_solutions:
                    result.solution_cap_reached = True
                    chosen.pop()
                    return

            if len(chosen) < max_size:
                search(index + 1, total)

            chosen.pop()

            if result.budget_exhausted or result.solution_cap_reached:
                return

    search(0, 0)
    return result


def theoretical_space(pool_size: int, max_size: int) -> int:
    """Number of combinations the cap admits: ``sum(C(n, i) for i in 1..k)``.

    Reported in diagnostics so the pruning can be seen working rather than
    taken on trust.
    """
    return sum(comb(pool_size, size) for size in range(1, min(max_size, pool_size) + 1))
