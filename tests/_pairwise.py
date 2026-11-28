"""All-pairs case selection for parameter grids.

A stacked ``@pytest.mark.parametrize`` runs the full cartesian product. When a
test's outcome depends on at most two of its parameters at a time, every pair
of values appearing together in some case catches the same faults with far
fewer cases. ``pairwise`` returns such a covering subset of the product,
deterministically, so test IDs stay stable between runs.

Use it only when no expected value depends on three parameters jointly. Keep the
full product (or list the three-way cases explicitly) when one does.
"""
from __future__ import annotations

from itertools import product
from typing import Sequence


def pairwise(*dimensions: Sequence) -> list[tuple]:
    """Cases (one value per dimension, in argument order) covering every value pair."""
    dims = [list(d) for d in dimensions]
    if any(not d for d in dims):
        return []
    if len(dims) <= 2:
        return list(product(*dims))
    # Build over dimensions sorted largest first (smaller result), then map back.
    order = sorted(range(len(dims)), key=lambda i: (-len(dims[i]), i))
    sizes = [len(dims[i]) for i in order]
    rows = [[a, b] for a in range(sizes[0]) for b in range(sizes[1])]
    for k in range(2, len(sizes)):
        uncovered = {(j, a, b) for j in range(k) for a in range(sizes[j]) for b in range(sizes[k])}
        for row in rows:
            best = max(range(sizes[k]),
                       key=lambda v: (sum((j, row[j], v) in uncovered for j in range(k)), -v))
            row.append(best)
            uncovered.difference_update((j, row[j], best) for j in range(k))
        while uncovered:
            j0, a0, b0 = min(uncovered)
            row = [None] * k + [b0]
            row[j0] = a0
            for j in range(k):
                if row[j] is None:
                    row[j] = max(range(sizes[j]), key=lambda v: ((j, v, b0) in uncovered, -v))
            rows.append(row)
            uncovered.difference_update((j, row[j], b0) for j in range(k))
    position = {dim: slot for slot, dim in enumerate(order)}
    return [tuple(dims[i][row[position[i]]] for i in range(len(dims))) for row in rows]
