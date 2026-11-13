"""Descriptive statistics for fully observed samples, without censoring claims."""

from __future__ import annotations

from typing import Any

import numpy as np


def distribution(values: list[float]) -> dict[str, Any]:
    """Describe observed samples; an empty sample has null statistics."""
    return {
        "count": len(values),
        "mean": float(np.mean(values)) if values else None,
        "p50": float(np.quantile(values, 0.5)) if values else None,
        "p95": float(np.quantile(values, 0.95)) if values else None,
        "max": max(values) if values else None,
    }
