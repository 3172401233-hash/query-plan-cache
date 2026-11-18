"""Dependency-aware query plan cache.

The cache never keys off raw SQL text.  Each stored :class:`CacheEntry` is
keyed by a structural fingerprint plus plan-sensitive parameter shapes and
optimizer hints, and carries the exact schema/statistics version snapshot of
the tables and columns it depends on.

Invalidation is surgical: a DDL or statistics event evicts only entries that
depend on the affected table or column; plans touching unrelated tables stay
warm.  A lookup that meets a stale entry reports a precise rejection reason and
the entry is included in the returned eviction set.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .ast import (
    AddColumn,
    Analyze,
    DropColumn,
    DropTable,
    Node,
    RenameColumn,
    RenameTable,
)
from .fingerprint import Analysis, analyze, entry_key
from .params import ParameterSummary, combined_shape_key
from .schema import SchemaCatalog


# Rejection / outcome reasons ------------------------------------------------

HIT = "hit"
MISS = "miss"
REJECT_STALE_SCHEMA = "stale_schema"
REJECT_STALE_STATS = "stale_stats"
REJECT_PARAMETER_SHAPE = "parameter_shape"
REJECT_HINT = "hint_mismatch"


@dataclass
class CacheEntry:
    key: str
    fingerprint: str
    plan: Any
    cost: float
    analysis: Analysis
    shape_key: tuple[tuple[Any, str], ...]
    snapshot: dict[str, object]
    hits: int = 0

    @property
    def tables(self) -> frozenset[str]:
        return self.analysis.tables

    @property
    def columns(self) -> frozenset[tuple[str, str]]:
        return self.analysis.columns

    @property
    def hint_signature(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return self.analysis.hint_signature


@dataclass
class LookupResult:
    hit: bool
    reason: str
    entry: CacheEntry | None
    evicted: set[str]
    key: str

    @property
    def plan(self) -> Any:
        return self.entry.plan if self.entry is not None else None

    @property
    def cost(self) -> float | None:
        return self.entry.cost if self.entry is not None else None


@dataclass
class PutResult:
    key: str
    replaced: bool
    evicted_for_capacity: set[str] = field(default_factory=set)


class PlanCache:
    def __init__(self, catalog: SchemaCatalog, capacity: int = 1000):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.catalog = catalog
        self.capacity = capacity
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def lookup(
        self,
        node: Node,
        summary: ParameterSummary | None = None,
    ) -> LookupResult:
        analysis = analyze(node)
        shape_key = combined_shape_key(summary, analysis.in_list_slots)
        key = entry_key(analysis, shape_key)
        evicted: set[str] = set()

        family = [
            entry
            for entry in self._entries.values()
            if entry.fingerprint == analysis.fingerprint
        ]

        exact = self._entries.get(key)
        if exact is not None:
            stale_reason = self._stale_reason(exact)
            if stale_reason is None:
                self._entries.move_to_end(key)
                exact.hits += 1
                return LookupResult(True, HIT, exact, evicted, key)
            self._remove(key)
            evicted.add(key)
            return LookupResult(False, stale_reason, None, evicted, key)

        if family:
            candidate = family[0]
            if candidate.hint_signature != analysis.hint_signature:
                reason = REJECT_HINT
            else:
                reason = REJECT_PARAMETER_SHAPE
            for member in family:
                stale_reason = self._stale_reason(member)
                if stale_reason is not None:
                    self._remove(member.key)
                    evicted.add(member.key)
            return LookupResult(False, reason, None, evicted, key)

        return LookupResult(False, MISS, None, evicted, key)

    # ------------------------------------------------------------------ #
    # Insertion
    # ------------------------------------------------------------------ #

    def put(
        self,
        node: Node,
        plan: Any,
        cost: float,
        summary: ParameterSummary | None = None,
    ) -> PutResult:
        analysis = analyze(node)
        shape_key = combined_shape_key(summary, analysis.in_list_slots)
        key = entry_key(analysis, shape_key)
        replaced = key in self._entries
        entry = CacheEntry(
            key=key,
            fingerprint=analysis.fingerprint,
            plan=plan,
            cost=cost,
            analysis=analysis,
            shape_key=shape_key,
            snapshot=self.catalog.snapshot(analysis.tables, analysis.columns),
        )
        self._entries[key] = entry
        self._entries.move_to_end(key)

        capacity_evictions: set[str] = set()
        while len(self._entries) > self.capacity:
            old_key, _ = self._entries.popitem(last=False)
            capacity_evictions.add(old_key)
        return PutResult(key, replaced, capacity_evictions)

    # ------------------------------------------------------------------ #
    # Dependency-aware invalidation
    # ------------------------------------------------------------------ #

    def invalidate_table(self, table: str) -> set[str]:
        """Evict every plan that depends on ``table`` (DDL / drop)."""

        return self._evict(lambda entry: table in entry.tables)

    def invalidate_column(self, table: str, column: str) -> set[str]:
        """Evict plans depending on ``table.column`` (or that table at all).

        Renaming/dropping a column is a structural change: any plan touching
        the table could have referenced the affected column, so table-wide
        eviction is the safe conservative choice.
        """

        return self.invalidate_table(table)

    def invalidate_stats(
        self,
        table: str,
        columns: tuple[str, ...] = (),
    ) -> set[str]:
        """Evict plans whose statistics inputs changed.

        Column-level statistics refresh only evicts entries referencing those
        columns; a whole-table refresh evicts every entry on the table.
        """

        affected = set(columns)

        def depends(entry: CacheEntry) -> bool:
            if table not in entry.tables:
                return False
            if not affected:
                return True
            return any(
                entry_table == table and column in affected
                for entry_table, column in entry.columns
            )

        return self._evict(depends)

    def apply_event(self, event: object) -> set[str]:
        """Apply a catalog event to both versions and cache, returning evictions."""

        if isinstance(event, (DropTable, RenameTable, AddColumn, DropColumn, RenameColumn)):
            table = event.table if hasattr(event, "table") else event.old
            evicted = self.invalidate_table(table)
        elif isinstance(event, Analyze):
            evicted = self.invalidate_stats(event.table, event.columns)
        else:
            raise TypeError(f"unsupported cache event: {type(event).__name__}")
        self.catalog.apply(event)
        return evicted

    # ------------------------------------------------------------------ #
    # Introspection / housekeeping
    # ------------------------------------------------------------------ #

    def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    def entries_for(self, fingerprint: str) -> list[CacheEntry]:
        return [
            entry for entry in self._entries.values() if entry.fingerprint == fingerprint
        ]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def clear(self) -> None:
        self._entries.clear()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _stale_reason(self, entry: CacheEntry) -> str | None:
        schema_snapshot: dict[str, int] = entry.snapshot["schema"]  # type: ignore[assignment]
        for table, version in schema_snapshot.items():
            if self.catalog.schema_version(table) != version:
                return REJECT_STALE_SCHEMA
        stats_snapshot: dict[tuple[str, str], int] = entry.snapshot["stats"]  # type: ignore[assignment]
        for (table, column), version in stats_snapshot.items():
            if self.catalog.stats_version(table, column) != version:
                return REJECT_STALE_STATS
        return None

    def _evict(self, predicate) -> set[str]:
        doomed = [key for key, entry in self._entries.items() if predicate(entry)]
        for key in doomed:
            self._remove(key)
        return set(doomed)

    def _remove(self, key: str) -> None:
        self._entries.pop(key, None)
