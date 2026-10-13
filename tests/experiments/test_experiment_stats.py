"""Statistics helpers behind the E1 decision rule, checked against known values."""

import pytest

from scripts.experiments._stats import clopper_pearson, ols, proportion_ci, t_ppf


def test_t_quantiles_match_tables():
    assert t_ppf(0.975, 1) == pytest.approx(12.7062, abs=1e-3)
    assert t_ppf(0.975, 10) == pytest.approx(2.2281, abs=1e-3)
    assert t_ppf(0.975, 1000) == pytest.approx(1.9623, abs=1e-3)


def test_clopper_pearson_matches_known_intervals():
    assert clopper_pearson(0, 120)[1] == pytest.approx(0.03027, abs=1e-4)
    lo, hi = clopper_pearson(5, 100)
    assert lo == pytest.approx(0.01643, abs=1e-4)
    assert hi == pytest.approx(0.11283, abs=1e-4)


def test_zero_misses_still_get_an_honest_upper_bound():
    ci = proportion_ci([(0, 5)] * 24)
    assert ci.rate == 0 and ci.n == 120 and ci.clusters == 24
    assert ci.cr_upper == 0
    assert ci.upper == pytest.approx(0.03027, abs=1e-4)


def test_clustered_misses_widen_the_interval():
    clustered = proportion_ci([(5, 5)] * 6 + [(0, 5)] * 18)
    spread = proportion_ci([(1, 5)] * 6 + [(0, 5)] * 24)
    assert clustered.rate == 0.25
    assert clustered.design_effect > 3
    assert clustered.upper - clustered.rate > spread.upper - spread.rate


def test_one_cluster_says_nothing_about_between_cluster_variance():
    ci = proportion_ci([(0, 120)])
    assert ci.cr_upper == 1.0 and ci.upper == 1.0


def test_ols_recovers_exact_coefficients():
    rows = [[1.0, x, x * x % 7] for x in range(10)]
    y = [3 + 2 * r[1] - 0.5 * r[2] for r in rows]
    assert ols(rows, y) == pytest.approx([3, 2, -0.5])
    assert ols([[1.0, 1.0], [1.0, 1.0]], [1, 2]) is None
