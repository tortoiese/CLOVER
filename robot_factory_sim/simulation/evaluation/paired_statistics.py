"""Exploratory paired mean inference on independent run/cluster observations."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from simulation.evaluation.scenario import integer, number


def mean_inference(values: list[float], *, confidence: float = .95, resamples: int = 5000,
                   seed: int = 1001, null_difference: float = 0) -> dict[str, Any]:
    """Compute a two-sided t interval and paired-percentile bootstrap sensitivity.

    Args:
        values: One candidate-minus-control difference per independent unit.
        confidence: Pointwise coverage, not simultaneous family coverage.
        resamples: Number of bootstrap samples, resampling whole units.
        seed: Fixed reproducibility seed; no outcome-dependent method choice.
        null_difference: Prespecified candidate-minus-control margin tested by the t statistic.

    Returns:
        Nominal exploratory intervals or an explicit reason for withholding.
        Independence, completeness and sample-size gates belong to the caller.
    """
    number(confidence, "confidence", .5, .999)
    integer(resamples, "bootstrap resamples", 1000, 50000)
    integer(seed, "bootstrap seed", 0, 2**32 - 1)
    number(null_difference, "null difference", -math.inf)
    x = np.asarray([number(v, "paired difference", -math.inf) for v in values], dtype=float)
    result = {"n": len(x), "mean_difference": float(x.mean()) if len(x) else None,
              "confidence": confidence, "method": "two_sided_student_t_on_unit_differences",
              "null_difference": null_difference,
              "t_interval": None, "p_value": None, "bootstrap_percentile_interval": None,
              "bootstrap_resamples": resamples, "bootstrap_seed": seed, "withheld": None}
    if len(x) < 2:
        result["withheld"] = "INSUFFICIENT_UNITS"
        return result
    if np.ptp(x) <= np.finfo(float).eps * max(1, abs(float(x.mean()))) * 8:
        result["withheld"] = "ZERO_OR_NUMERICALLY_ZERO_VARIANCE"
        return result
    try:
        from scipy.stats import t
    except ImportError:
        result["withheld"] = "SCIPY_UNAVAILABLE"
        return result
    mean, se = float(x.mean()), float(x.std(ddof=1) / np.sqrt(len(x)))
    critical = float(t.ppf((1 + confidence) / 2, len(x) - 1))
    result["t_interval"] = [mean - critical * se, mean + critical * se]
    result["p_value"] = float(2 * t.sf(abs((mean - null_difference) / se), len(x) - 1))
    rng = np.random.default_rng(seed)
    samples = []
    for offset in range(0, resamples, 128):
        indices = rng.integers(0, len(x), size=(min(128, resamples - offset), len(x)))
        samples.extend(x[indices].mean(axis=1).tolist())
    alpha = (1 - confidence) / 2
    result["bootstrap_percentile_interval"] = np.quantile(samples, [alpha, 1 - alpha]).tolist()
    return result


def holm_adjust(p_values: list[float | None]) -> list[float | None]:
    """Adjust one declared family; unavailable hypotheses retain family slots.

    Missing p-values use 1 internally and remain null in the returned list.
    This avoids shrinking the family after seeing missing or failed outcomes.
    """
    values = [1.0 if p is None else number(p, "p-value", 0, 1) for p in p_values]
    order = sorted(range(len(values)), key=values.__getitem__)
    adjusted: list[float | None] = [None] * len(values)
    previous = 0.0
    for rank, index in enumerate(order):
        previous = min(1, max(previous, (len(values) - rank) * values[index]))
        if p_values[index] is not None:
            adjusted[index] = previous
    return adjusted
