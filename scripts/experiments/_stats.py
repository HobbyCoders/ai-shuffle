"""Small statistics helpers for the experiment runners (no scipy).

The E1 decision rule needs a zero-cache rate with an upper 95% bound that
respects clustering: misses come in runs (a zero-cache call was followed by
another zero-cache call 307 times and by a hit 204 times in the review data),
so treating calls as independent would make the bound too tight.

``proportion_ci`` reports two bounds and uses the wider one:

* a cluster-robust (CR1) standard error with a t critical value on G-1
  degrees of freedom, and
* a Clopper-Pearson interval on the effective sample size N / design effect.

The second matters when no misses were seen at all: the CR1 standard error is
then zero and says nothing, while Clopper-Pearson still gives an honest upper
bound (about 3.0% for 0 of 120).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# ─── special functions ───────────────────────────────────────────────────────


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) for a, b > 0 and 0 <= x <= 1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _bisect(f, lo: float, hi: float, target: float, iters: int = 200) -> float:
    """x in [lo, hi] with f(x) == target, for f increasing."""
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile of the Beta(a, b) distribution."""
    return _bisect(lambda x: betainc(a, b, x), 0.0, 1.0, q)


def t_cdf(t: float, df: float) -> float:
    """CDF of Student's t with ``df`` degrees of freedom."""
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def t_ppf(q: float, df: float) -> float:
    """Quantile of Student's t (q in (0, 1))."""
    if q == 0.5:
        return 0.0
    if q < 0.5:
        return -t_ppf(1.0 - q, df)
    hi = 1.0
    while t_cdf(hi, df) < q:
        hi *= 2.0
    return _bisect(lambda t: t_cdf(t, df), 0.0, hi, q)


def clopper_pearson(k: float, n: float, level: float = 0.95) -> tuple[float, float]:
    """Exact binomial interval; accepts a non-integer (effective) k and n."""
    if n <= 0:
        return 0.0, 1.0
    alpha = 1.0 - level
    lower = 0.0 if k <= 0 else beta_ppf(alpha / 2.0, k, n - k + 1.0)
    upper = 1.0 if k >= n else beta_ppf(1.0 - alpha / 2.0, k + 1.0, n - k)
    return lower, upper


# ─── clustered proportion ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProportionCI:
    successes: int
    n: int
    clusters: int
    rate: float
    lower: float
    upper: float
    cr_upper: float  # cluster-robust t bound alone
    cp_upper: float  # Clopper-Pearson on the effective n alone
    design_effect: float

    def as_dict(self) -> dict:
        return {
            "k": self.successes, "n": self.n, "clusters": self.clusters,
            "rate": self.rate, "lower": self.lower, "upper": self.upper,
            "cr_upper": self.cr_upper, "cp_upper": self.cp_upper,
            "design_effect": self.design_effect,
        }


def proportion_ci(
    clusters: Iterable[tuple[int, int]], level: float = 0.95
) -> ProportionCI:
    """Rate of successes with a cluster-aware two-sided interval.

    ``clusters`` yields ``(successes, trials)`` per cluster. Returns the wider
    of the CR1 cluster-robust interval and the Clopper-Pearson interval on the
    effective sample size.
    """
    groups = [(int(y), int(n)) for y, n in clusters if n > 0]
    k = sum(y for y, _ in groups)
    n = sum(m for _, m in groups)
    g = len(groups)
    if n == 0:
        return ProportionCI(0, 0, 0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0)
    p = k / n
    if g >= 2:
        resid = sum((y - p * m) ** 2 for y, m in groups)
        var_cr = (g / (g - 1)) * resid / (n * n)
        se = math.sqrt(var_cr)
        tcrit = t_ppf(0.5 + level / 2.0, g - 1)
        cr_lower, cr_upper = max(0.0, p - tcrit * se), min(1.0, p + tcrit * se)
        var_bin = p * (1.0 - p) / n
        deff = max(1.0, var_cr / var_bin) if var_bin > 0 else 1.0
    else:
        # One cluster carries no information about between-cluster variance.
        cr_lower, cr_upper, deff = 0.0, 1.0, 1.0
    n_eff = n / deff
    cp_lower, cp_upper = clopper_pearson(p * n_eff, n_eff, level)
    return ProportionCI(
        successes=k, n=n, clusters=g, rate=p,
        lower=min(cr_lower, cp_lower), upper=max(cr_upper, cp_upper),
        cr_upper=cr_upper, cp_upper=cp_upper, design_effect=deff,
    )


# ─── least squares ───────────────────────────────────────────────────────────


def ols(rows: Sequence[Sequence[float]], y: Sequence[float]) -> list[float] | None:
    """Least-squares coefficients for ``y ~ rows`` (caller adds an intercept
    column). Returns None when the normal equations are singular."""
    if not rows:
        return None
    p = len(rows[0])
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for r, target in zip(rows, y):
        for i in range(p):
            xty[i] += r[i] * target
            for j in range(p):
                xtx[i][j] += r[i] * r[j]
    # Gaussian elimination with partial pivoting.
    a = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for col in range(p):
        pivot = max(range(col, p), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(p):
            if r != col:
                factor = a[r][col] / a[col][col]
                for c in range(col, p + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][p] / a[i][i] for i in range(p)]
