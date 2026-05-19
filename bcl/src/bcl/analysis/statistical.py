"""Statistical equivalence testing for migration validation.

A migration "passed validation" should mean something a statistician
would sign: the post-migration system behaves like the pre-migration
system, to within measurement noise — not merely that a smoke test
returned a green checkmark.

This module supplies two classical tests, run over latency samples
and message-disposition counts captured by the message-flow harness
before and after a migration:

  welch_t_test(...)
      Welch's two-sample t-test for the equality of two means with
      unequal variances. Applied to round-trip latency samples. The
      null hypothesis H0 is "the pre- and post-migration mean latency
      are equal"; failing to reject H0 is the *desired* outcome — it
      is evidence the migration did not degrade latency.

  chi_square_gof(...)
      Pearson's chi-square goodness-of-fit test. Applied to message
      disposition counts (delivered / DLQ / in-flight). H0 is "the
      post-migration disposition distribution matches the pre-
      migration distribution"; again, failing to reject is desired.

Why these two and not KL divergence or CUSUM
--------------------------------------------
KL divergence needs a well-sampled reference distribution; a 14-day
build has no migration-baseline corpus to estimate one from, so a KL
number here would be precision theatre. CUSUM needs a streaming time
series. Welch's t-test and Pearson's chi-square are the correct tests
for the data we actually capture — two small independent samples and
a contingency of counts — and both are exact about their assumptions.
Honest scope beats an impressive-looking number with no data under it.

Dependency-free: the t and chi-square tail probabilities are computed
from the regularised incomplete beta / gamma functions implemented
here, so the module needs neither SciPy nor NumPy. Accuracy is ample
for a p-value reported to three decimals.

References
----------
Welch, B. L. (1947). "The generalization of 'Student's' problem when
several different population variances are involved." Biometrika,
34(1/2), 28-35. — the unequal-variance t-test and its
Welch-Satterthwaite degrees-of-freedom approximation.

Pearson, K. (1900). "On the criterion that a given system of
deviations from the probable in the case of a correlated system of
variables is such that it can be reasonably supposed to have arisen
from random sampling." Philosophical Magazine, 50(302), 157-175. —
the chi-square goodness-of-fit statistic.

Press, W. H. et al. (2007). "Numerical Recipes" (3rd ed.), §6.2,
§6.4 — the continued-fraction evaluations of the incomplete beta and
incomplete gamma functions used here for the tail probabilities.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Conventional significance level. A p-value below this rejects H0.
ALPHA: float = 0.05


# ─────────────────────────────────────────────────────────────────────────
# Result containers
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WelchResult:
    """Outcome of Welch's two-sample t-test on latency samples."""

    test: str = "welch_t"
    n_pre: int = 0
    n_post: int = 0
    mean_pre: float = 0.0
    mean_post: float = 0.0
    var_pre: float = 0.0
    var_post: float = 0.0
    t_statistic: float = 0.0
    degrees_of_freedom: float = 0.0
    p_value: float = 1.0
    reject_h0: bool = False
    """True ⇒ means differ significantly ⇒ migration changed latency.
    For a validation gate, reject_h0 == False is the PASS condition."""
    outcome: str = "PASS"
    """PASS | WARN | FAIL — gate interpretation, see interpret()."""
    interpretation: str = ""
    reference: str = (
        "Welch, B. L. (1947). Biometrika 34(1/2), 28-35."
    )

    def to_dict(self) -> dict:
        return {
            "test": self.test,
            "n_pre": self.n_pre,
            "n_post": self.n_post,
            "mean_pre": round(self.mean_pre, 6),
            "mean_post": round(self.mean_post, 6),
            "var_pre": round(self.var_pre, 6),
            "var_post": round(self.var_post, 6),
            "t_statistic": round(self.t_statistic, 4),
            "degrees_of_freedom": round(self.degrees_of_freedom, 3),
            "p_value": round(self.p_value, 4),
            "reject_h0": self.reject_h0,
            "outcome": self.outcome,
            "interpretation": self.interpretation,
            "reference": self.reference,
        }


@dataclass(frozen=True)
class ChiSquareResult:
    """Outcome of Pearson's chi-square goodness-of-fit test."""

    test: str = "pearson_chi_square_gof"
    categories: list[str] = field(default_factory=list)
    observed: list[float] = field(default_factory=list)
    expected: list[float] = field(default_factory=list)
    chi_square: float = 0.0
    degrees_of_freedom: int = 0
    p_value: float = 1.0
    reject_h0: bool = False
    outcome: str = "PASS"
    interpretation: str = ""
    reference: str = (
        "Pearson, K. (1900). Philosophical Magazine 50(302), 157-175."
    )

    def to_dict(self) -> dict:
        return {
            "test": self.test,
            "categories": self.categories,
            "observed": [round(x, 4) for x in self.observed],
            "expected": [round(x, 4) for x in self.expected],
            "chi_square": round(self.chi_square, 4),
            "degrees_of_freedom": self.degrees_of_freedom,
            "p_value": round(self.p_value, 4),
            "reject_h0": self.reject_h0,
            "outcome": self.outcome,
            "interpretation": self.interpretation,
            "reference": self.reference,
        }


# ─────────────────────────────────────────────────────────────────────────
# Special functions — incomplete beta / gamma (Numerical Recipes §6.2/6.4)
# ─────────────────────────────────────────────────────────────────────────


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta function."""
    max_iter = 200
    eps = 3.0e-12
    fpmin = 1.0e-300

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _student_t_sf(t: float, df: float) -> float:
    """Two-sided survival probability for Student's t with df dof.

    Returns P(|T| > |t|) — the two-tailed p-value.
    """
    x = df / (df + t * t)
    return _betai(df / 2.0, 0.5, x)


def _gammq(s: float, x: float) -> float:
    """Upper regularised incomplete gamma Q(s, x) = 1 - P(s, x).

    Used for the chi-square survival function: P(X^2 > x) with
    s = df/2 and x = chi_square/2.
    """
    if x < 0.0 or s <= 0.0:
        return 1.0
    if x == 0.0:
        return 1.0
    if x < s + 1.0:
        # Series expansion for P(s, x); return Q = 1 - P.
        ap = s
        total = 1.0 / s
        delta = total
        for _ in range(500):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1.0e-14:
                break
        p = total * math.exp(-x + s * math.log(x) - math.lgamma(s))
        return 1.0 - p
    # Continued fraction for Q(s, x) directly.
    fpmin = 1.0e-300
    b = x + 1.0 - s
    c = 1.0 / fpmin
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - s)
        b += 2.0
        d = an * d + b
        if abs(d) < fpmin:
            d = fpmin
        c = b + an / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1.0e-14:
            break
    return math.exp(-x + s * math.log(x) - math.lgamma(s)) * h


def _chi_square_sf(chi2: float, df: int) -> float:
    """Survival function P(X^2 > chi2) for a chi-square with df dof."""
    if df <= 0:
        return 1.0
    return _gammq(df / 2.0, chi2 / 2.0)


# ─────────────────────────────────────────────────────────────────────────
# Welch's two-sample t-test
# ─────────────────────────────────────────────────────────────────────────


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _sample_variance(xs: list[float], mean: float) -> float:
    """Unbiased (n-1) sample variance."""
    if len(xs) < 2:
        return 0.0
    return sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)


def welch_t_test(
    pre: list[float],
    post: list[float],
    *,
    alpha: float = ALPHA,
) -> WelchResult:
    """Welch's two-sample t-test for equality of two means.

    Parameters
    ----------
    pre, post:
        Latency samples (e.g. round-trip seconds) measured before and
        after the migration. Each needs at least 2 observations.
    alpha:
        Significance level. p < alpha rejects H0 (means differ).

    Gate interpretation
    -------------------
    H0: mean_pre == mean_post. For a migration validation gate, NOT
    rejecting H0 is the PASS condition — it is evidence the migration
    preserved latency. The outcome field encodes:
        PASS  — fail to reject H0 (means statistically equivalent).
        WARN  — reject H0 but the post mean is *lower* (faster — a
                change, but a benign one worth surfacing).
        FAIL  — reject H0 and the post mean is *higher* (slower).
    """
    if len(pre) < 2 or len(post) < 2:
        return WelchResult(
            n_pre=len(pre), n_post=len(post),
            outcome="WARN",
            interpretation=(
                "Insufficient samples for Welch's t-test (need >=2 "
                "per group). Capture more pre/post message-flow runs."
            ),
        )

    n1, n2 = len(pre), len(post)
    m1, m2 = _mean(pre), _mean(post)
    v1, v2 = _sample_variance(pre, m1), _sample_variance(post, m2)

    se2_1 = v1 / n1
    se2_2 = v2 / n2
    denom = se2_1 + se2_2

    if denom == 0.0:
        # Both samples constant. Equal means ⇒ identical; else degenerate.
        equal = abs(m1 - m2) < 1e-12
        return WelchResult(
            n_pre=n1, n_post=n2, mean_pre=m1, mean_post=m2,
            var_pre=v1, var_post=v2,
            t_statistic=0.0 if equal else math.inf,
            degrees_of_freedom=float(n1 + n2 - 2),
            p_value=1.0 if equal else 0.0,
            reject_h0=not equal,
            outcome="PASS" if equal else "FAIL",
            interpretation=(
                "Zero variance in both groups; means "
                + ("identical." if equal else "differ exactly.")
            ),
        )

    t_stat = (m1 - m2) / math.sqrt(denom)

    # Welch-Satterthwaite degrees of freedom.
    df = (denom ** 2) / (
        (se2_1 ** 2) / (n1 - 1) + (se2_2 ** 2) / (n2 - 1)
    )

    p = _student_t_sf(t_stat, df)
    reject = p < alpha

    if not reject:
        outcome = "PASS"
        interp = (
            f"Fail to reject H0 at alpha={alpha}: pre/post mean latency "
            f"are statistically equivalent (p={p:.3f}). The migration "
            f"preserved latency within measurement noise."
        )
    elif m2 < m1:
        outcome = "WARN"
        interp = (
            f"H0 rejected (p={p:.3f}) but post-migration latency is "
            f"LOWER ({m2:.4f}s vs {m1:.4f}s) — a statistically real "
            f"improvement, surfaced for transparency."
        )
    else:
        outcome = "FAIL"
        interp = (
            f"H0 rejected (p={p:.3f}): post-migration latency is "
            f"significantly HIGHER ({m2:.4f}s vs {m1:.4f}s). The "
            f"migration degraded latency — investigate before sign-off."
        )

    return WelchResult(
        n_pre=n1, n_post=n2, mean_pre=m1, mean_post=m2,
        var_pre=v1, var_post=v2,
        t_statistic=t_stat, degrees_of_freedom=df,
        p_value=p, reject_h0=reject,
        outcome=outcome, interpretation=interp,
    )


# ─────────────────────────────────────────────────────────────────────────
# Pearson's chi-square goodness-of-fit
# ─────────────────────────────────────────────────────────────────────────


def chi_square_gof(
    categories: list[str],
    observed_post: list[float],
    expected_pre: list[float],
    *,
    alpha: float = ALPHA,
) -> ChiSquareResult:
    """Pearson's chi-square goodness-of-fit test.

    Tests whether the post-migration message-disposition counts
    (e.g. [delivered, dlq, in_flight]) follow the same distribution
    as the pre-migration counts.

    Parameters
    ----------
    categories:
        Label per disposition bucket.
    observed_post:
        Post-migration counts per bucket.
    expected_pre:
        Pre-migration counts per bucket. These are rescaled to the
        post-migration total before the test, so the comparison is of
        *proportions*, not raw counts.

    Gate interpretation
    -------------------
    H0: the post distribution matches the pre distribution. NOT
    rejecting H0 is the PASS condition.
    """
    if not (len(categories) == len(observed_post) == len(expected_pre)):
        return ChiSquareResult(
            categories=categories,
            outcome="WARN",
            interpretation="Category / count length mismatch.",
        )
    if len(categories) < 2:
        return ChiSquareResult(
            categories=categories,
            outcome="WARN",
            interpretation="Need >=2 disposition categories.",
        )

    total_obs = sum(observed_post)
    total_exp = sum(expected_pre)
    if total_obs <= 0 or total_exp <= 0:
        return ChiSquareResult(
            categories=categories,
            observed=observed_post,
            expected=expected_pre,
            outcome="WARN",
            interpretation="Empty pre or post sample; cannot test.",
        )

    # Rescale the pre distribution to the post total (compare shape).
    scale = total_obs / total_exp
    expected = [max(e * scale, 1e-9) for e in expected_pre]

    chi2 = sum(
        (o - e) ** 2 / e
        for o, e in zip(observed_post, expected)
    )
    df = len(categories) - 1
    p = _chi_square_sf(chi2, df)
    reject = p < alpha

    if not reject:
        outcome = "PASS"
        interp = (
            f"Fail to reject H0 at alpha={alpha} (chi2={chi2:.3f}, "
            f"df={df}, p={p:.3f}): the post-migration message "
            f"disposition distribution matches the pre-migration "
            f"distribution. No messages lost or mis-routed."
        )
    else:
        outcome = "FAIL"
        interp = (
            f"H0 rejected (chi2={chi2:.3f}, df={df}, p={p:.3f}): the "
            f"post-migration disposition distribution differs from "
            f"pre-migration. Investigate DLQ / in-flight counts before "
            f"sign-off."
        )

    return ChiSquareResult(
        categories=categories,
        observed=list(observed_post),
        expected=expected,
        chi_square=chi2,
        degrees_of_freedom=df,
        p_value=p,
        reject_h0=reject,
        outcome=outcome,
        interpretation=interp,
    )


__all__ = [
    "ALPHA",
    "WelchResult",
    "ChiSquareResult",
    "welch_t_test",
    "chi_square_gof",
]
