"""Bound-parameter summaries and parameter-sensitivity rules.

Literal values are erased from the fingerprint, but a small number of
parameters are "plan-sensitive": their value shape (not the value itself) can
legitimately switch the optimizer to a different plan.  Such shapes are folded
into the cache key so that an extreme parameter can never silently reuse a
plan chosen for a normal parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

NULL_TAG = "null"
"""Shape tag emitted for parameters whose bound value is ``None``."""

IN_LIST_TAG = "in_len"
"""Shape tag used for the length of a parameter-expanded ``IN`` list."""

DEFAULT_IN_LIST_BUCKETS: tuple[int, ...] = (1, 5, 25, 100)
"""Upper bounds of IN-list length buckets: 1, 2-5, 6-25, 26-100, >100."""


def in_list_bucket(length: int, buckets: tuple[int, ...] = DEFAULT_IN_LIST_BUCKETS) -> str:
    """Map an IN-list length to a stable, coarse bucket label.

    Bucketing (rather than exact length) keeps reuse high: lists of length 3
    and 4 share a shape, while a length-300 list gets its own extreme shape.
    """

    if length < 1:
        raise ValueError("IN list must contain at least one element")
    for upper in buckets:
        if length <= upper:
            low = 1 if upper == buckets[0] else buckets[buckets.index(upper) - 1] + 1
            return f"{low}-{upper}"
    return f">{buckets[-1]}"


@dataclass(frozen=True)
class ParameterSummary:
    """Shape summary of the bound parameters of a query execution.

    - ``shapes`` only contains *plan-sensitive* features; ordinary literal
      values never appear in a cache key.
    - Nullability is detected automatically because ``IS NULL`` predicates and
      nullable join keys frequently flip the plan.
    - ``tags`` lets callers declare extra sensitivity (e.g. selectivity
      classes, wide ranges) without leaking actual values into the key.
    - ``in_list_lengths`` records the length of each parameter-expanded IN
      list, keyed by an opaque slot id chosen by the caller (commonly the
      parameter name or index).
    """

    values: Mapping[Any, Any] = field(default_factory=dict)
    tags: Mapping[Any, str] = field(default_factory=dict)
    in_list_lengths: Mapping[Any, int] = field(default_factory=dict)
    in_list_buckets: tuple[int, ...] = DEFAULT_IN_LIST_BUCKETS

    def shape_key(self) -> tuple[tuple[Any, str], ...]:
        """Deterministic, hashable key of every plan-sensitive shape."""

        shapes: dict[Any, str] = {}
        for name, value in self.values.items():
            if value is None:
                shapes[name] = NULL_TAG
        for name, length in self.in_list_lengths.items():
            shapes[name] = f"{IN_LIST_TAG}:{in_list_bucket(length, self.in_list_buckets)}"
        for name, tag in self.tags.items():
            shapes[name] = f"tag:{tag}"
        return tuple(sorted(shapes.items(), key=lambda item: str(item[0])))


def combined_shape_key(
    summary: ParameterSummary | None,
    in_list_slots: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[Any, str], ...]:
    """Merge caller-supplied parameter shapes with AST-derived IN buckets."""

    shapes: dict[Any, str] = {}
    for slot, bucket_label in in_list_slots:
        shapes[slot] = bucket_label
    if summary is not None:
        for name, label in summary.shape_key():
            shapes[name] = label
    return tuple(sorted(shapes.items(), key=lambda item: str(item[0])))
