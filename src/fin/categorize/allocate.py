"""Split a charge across item categories (SPEC §12.4)."""

from __future__ import annotations


def largest_remainder(total: int, weights: list[int]) -> list[int]:
    """Split `total` cents in proportion to `weights`; the parts always sum to `total`.

    Works for negative totals (refunds) too. Zero total weight gives everything to the first.
    """
    if not weights:
        return []
    wsum = sum(weights)
    if wsum <= 0:
        return [total] + [0] * (len(weights) - 1)
    sign = -1 if total < 0 else 1
    t = abs(total)
    exact = [t * w / wsum for w in weights]
    parts = [int(x) for x in exact]
    short = t - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in order[:short]:
        parts[i] += 1
    return [sign * p for p in parts]


def split_by_category(total: int, items: list[tuple[str, int]]) -> dict[str, int]:
    """items = [(category_id, weight)]. Returns {category_id: cents}, summing to total."""
    parts = largest_remainder(total, [w for _, w in items])
    out: dict[str, int] = {}
    for (cat, _), p in zip(items, parts, strict=True):
        out[cat] = out.get(cat, 0) + p
    return out
