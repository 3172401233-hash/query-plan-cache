"""Schema and statistics version catalog.

Two independent version families are tracked per table:

- ``schema_version``: structure (columns, table identity); bumped by DDL.
- ``stats_version``: optimizer statistics; bumped by ``ANALYZE``. Whole-table
  statistics live under column ``*`` so column-level and table-level updates
  can be distinguished.

The catalog only records versions; deciding which cached entries depend on a
changed table/column is the cache's job (see :mod:`plancache.cache`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ast import (
    AddColumn,
    Analyze,
    CreateTable,
    DropColumn,
    DropTable,
    RenameColumn,
    RenameTable,
)

TABLE_STATS_COLUMN = "*"


@dataclass
class SchemaCatalog:
    _schema_versions: dict[str, int] = field(default_factory=dict)
    _stats_versions: dict[tuple[str, str], int] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def table_exists(self, table: str) -> bool:
        return table in self._schema_versions

    def schema_version(self, table: str) -> int:
        return self._schema_versions.get(table, 0)

    def stats_version(self, table: str, column: str = TABLE_STATS_COLUMN) -> int:
        return self._stats_versions.get((table, column), 0)

    def snapshot(
        self,
        tables: frozenset[str],
        columns: frozenset[tuple[str, str]],
    ) -> dict[str, object]:
        """Version snapshot captured when a plan is stored."""

        schema = {table: self.schema_version(table) for table in tables}
        stats: dict[tuple[str, str], int] = {}
        for table, column in columns:
            stats[(table, column)] = self.stats_version(table, column)
        for table in tables:
            stats[(table, TABLE_STATS_COLUMN)] = self.stats_version(table)
        return {"schema": schema, "stats": stats}

    # ------------------------------------------------------------------ #
    # Writes / events
    # ------------------------------------------------------------------ #

    def _bump_schema(self, table: str) -> int:
        self._schema_versions[table] = self._schema_versions.get(table, 0) + 1
        return self._schema_versions[table]

    def _bump_stats(self, table: str, column: str) -> int:
        key = (table, column)
        self._stats_versions[key] = self._stats_versions.get(key, 0) + 1
        return self._stats_versions[key]

    def create_table(self, table: str, columns: tuple[str, ...] = ()) -> None:
        self._bump_schema(table)
        for column in columns:
            self._bump_stats(table, column)

    def drop_table(self, table: str) -> None:
        self._schema_versions.pop(table, None)
        self._stats_versions = {
            key: version for key, version in self._stats_versions.items() if key[0] != table
        }

    def rename_table(self, old: str, new: str) -> None:
        version = self._schema_versions.pop(old, 0)
        self._schema_versions[new] = version + 1
        old_stats = {
            key: value for key, value in self._stats_versions.items() if key[0] == old
        }
        for key in old_stats:
            del self._stats_versions[key]
        for (_, column), value in old_stats.items():
            self._stats_versions[(new, column)] = value + 1

    def add_column(self, table: str, column: str) -> None:
        self._bump_schema(table)
        self._bump_stats(table, column)

    def drop_column(self, table: str, column: str) -> None:
        self._bump_schema(table)
        self._stats_versions.pop((table, column), None)

    def rename_column(self, table: str, old: str, new: str) -> None:
        self._bump_schema(table)
        value = self._stats_versions.pop((table, old), 0)
        self._stats_versions[(table, new)] = value + 1

    def analyze(self, table: str, columns: tuple[str, ...] = ()) -> None:
        if columns:
            for column in columns:
                self._bump_stats(table, column)
        else:
            self._bump_stats(table, TABLE_STATS_COLUMN)

    def apply(self, event: object) -> None:
        """Apply a DDL/statistics event from :mod:`plancache.ast`."""

        if isinstance(event, CreateTable):
            self.create_table(event.table, event.columns)
        elif isinstance(event, DropTable):
            self.drop_table(event.table)
        elif isinstance(event, RenameTable):
            self.rename_table(event.old, event.new)
        elif isinstance(event, AddColumn):
            self.add_column(event.table, event.column)
        elif isinstance(event, DropColumn):
            self.drop_column(event.table, event.column)
        elif isinstance(event, RenameColumn):
            self.rename_column(event.table, event.old, event.new)
        elif isinstance(event, Analyze):
            self.analyze(event.table, event.columns)
        else:
            raise TypeError(f"unsupported catalog event: {type(event).__name__}")
