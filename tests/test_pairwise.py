from itertools import combinations, product

from tests._pairwise import pairwise


def test_every_value_pair_is_covered_with_fewer_cases_than_the_product():
    dims = [range(8), "abc", [True, False], [None, 1, 2, 3]]
    cases = pairwise(*dims)
    assert set(cases) <= set(product(*dims))
    assert len(cases) == len(set(cases)) < 8 * 3 * 2 * 4
    for (i, a), (j, b) in combinations(enumerate(dims), 2):
        assert {(c[i], c[j]) for c in cases} == set(product(a, b))
    assert cases == pairwise(*dims)  # stable IDs
    assert pairwise([1, 2], "xy") == list(product([1, 2], "xy"))
    assert pairwise([], "xy") == []
