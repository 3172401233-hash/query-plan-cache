"""Canonical fingerprinting and dependency extraction for SQL AST nodes.

The fingerprint is the structural identity of a statement *independent of*
bound values and cosmetic spelling.  Normalization rules:

1. Every literal becomes the placeholder ``?``; bound parameters keep their
   positional/name identity (``$n`` / ``$name``).
2. User-supplied table aliases are rewritten to positional aliases ``T0``,
   ``T1`` ... in FROM/JOIN order, so ``FROM orders o`` and ``FROM orders ord``
   share a fingerprint.
3. An ``IN`` list is rendered as ``IN(?+)`` plus a length-bucket tag, so
   similar lengths reuse one plan while extreme lengths get their own key.
4. Hints are *not* part of the base fingerprint: they are a separate key
   component, because different hint sets must produce different plans.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .ast import (
    BinaryOp,
    Column,
    Delete,
    Function,
    Hint,
    InList,
    Insert,
    IsNull,
    Join,
    Literal,
    Node,
    OrderBy,
    Parameter,
    Select,
    Star,
    Table,
    UnaryOp,
    Update,
)
from .params import DEFAULT_IN_LIST_BUCKETS, in_list_bucket
from .params import IN_LIST_TAG

PLACEHOLDER = "?"


@dataclass(frozen=True)
class Analysis:
    """Result of analyzing a statement AST."""

    fingerprint: str
    canonical: str
    tables: frozenset[str]
    columns: frozenset[tuple[str, str]]
    stats: frozenset[tuple[str, str]]
    in_list_slots: tuple[tuple[str, str], ...]
    hint_signature: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass
class _Normalizer:
    in_list_buckets: tuple[int, ...] = DEFAULT_IN_LIST_BUCKETS
    _aliases: dict[str, str] = field(default_factory=dict)
    _tables: set[str] = field(default_factory=set)
    _table_of_alias: dict[str, str] = field(default_factory=dict)
    _default_table: str | None = None
    _columns: set[tuple[str, str]] = field(default_factory=set)
    _in_slots: list[tuple[str, str]] = field(default_factory=list)

    def alias_for(self, table: Table) -> str:
        key = table.alias or table.name
        if key not in self._aliases:
            self._aliases[key] = f"T{len(self._aliases)}"
            self._tables.add(table.name)
            self._table_of_alias[key] = table.name
        return self._aliases[key]

    def render(self, node: Node) -> str:
        if isinstance(node, Table):
            alias = self.alias_for(node)
            return f"{node.name} AS {alias}"
        if isinstance(node, Star):
            return f"{self._aliases[node.table]}.*" if node.table else "*"
        if isinstance(node, Column):
            alias = self._aliases[node.table] if node.table else None
            if node.table:
                self._columns.add((self._table_of_alias[node.table], node.name))
            elif self._default_table is not None:
                self._columns.add((self._default_table, node.name))
            prefix = f"{alias}." if alias else ""
            return f"{prefix}{node.name}"
        if isinstance(node, Literal):
            return PLACEHOLDER
        if isinstance(node, Parameter):
            return f"${node.name}"
        if isinstance(node, BinaryOp):
            return f"({self.render(node.left)} {node.op.upper()} {self.render(node.right)})"
        if isinstance(node, UnaryOp):
            return f"({node.op.upper()} {self.render(node.operand)})"
        if isinstance(node, IsNull):
            op = "IS NOT NULL" if node.negated else "IS NULL"
            return f"({self.render(node.operand)} {op})"
        if isinstance(node, InList):
            return self._render_in_list(node)
        if isinstance(node, Function):
            distinct = "DISTINCT " if node.distinct else ""
            args = ", ".join(self.render(arg) for arg in node.args)
            return f"{node.name.upper()}({distinct}{args})"
        if isinstance(node, OrderBy):
            direction = " DESC" if node.desc else ""
            return f"{self.render(node.expr)}{direction}"
        if isinstance(node, Join):
            on = f" ON {self.render(node.on)}" if node.on is not None else ""
            return f"{node.kind.upper()} JOIN {self.render(node.table)}{on}"
        if isinstance(node, Hint):
            args = ", ".join(node.args)
            return f"/*+ {node.name}({args}) */"
        raise TypeError(f"unsupported AST node: {type(node).__name__}")

    def _render_in_list(self, node: InList) -> str:
        bucket = in_list_bucket(len(node.elements), self.in_list_buckets)
        slot = f"in{len(self._in_slots)}"
        self._in_slots.append((slot, f"{IN_LIST_TAG}:{bucket}"))
        keyword = "NOT IN" if node.negated else "IN"
        for element in node.elements:
            self.render(element)
        return f"({self.render(node.expr)} {keyword} (?+))"

    def render_statement(self, node: Node) -> str:
        if isinstance(node, Select):
            return self._render_select(node)
        if isinstance(node, Insert):
            return self._render_insert(node)
        if isinstance(node, Update):
            return self._render_update(node)
        if isinstance(node, Delete):
            return self._render_delete(node)
        raise TypeError(f"unsupported statement: {type(node).__name__}")

    def _render_select(self, node: Select) -> str:
        for table in node.from_:
            self.alias_for(table)
        for join in node.joins:
            self.alias_for(join.table)
        parts = ["SELECT"]
        parts.append(", ".join(self.render(col) for col in node.columns))
        if node.from_:
            parts.append("FROM " + ", ".join(self.render(table) for table in node.from_))
        for join in node.joins:
            parts.append(self.render(join))
        if node.where is not None:
            parts.append("WHERE " + self.render(node.where))
        if node.group_by:
            parts.append("GROUP BY " + ", ".join(self.render(expr) for expr in node.group_by))
        if node.having is not None:
            parts.append("HAVING " + self.render(node.having))
        if node.order_by:
            parts.append("ORDER BY " + ", ".join(self.render(expr) for expr in node.order_by))
        if node.limit is not None:
            parts.append("LIMIT " + self.render(node.limit))
        return " ".join(parts)

    def _render_insert(self, node: Insert) -> str:
        self._default_table = node.table.name
        columns = ", ".join(self.render(col) for col in node.columns)
        if isinstance(node.values, Select):
            values = self._render_select(node.values)
        else:
            values = "VALUES (" + ", ".join(self.render(v) for v in node.values) + ")"
        return f"INSERT INTO {self.render(node.table)} ({columns}) {values}"

    def _render_update(self, node: Update) -> str:
        self._default_table = node.table.name
        assignments = ", ".join(
            f"{self.render(item.column)} = {self.render(item.value)}"
            for item in node.assignments
        )
        stmt = f"UPDATE {self.render(node.table)} SET {assignments}"
        if node.where is not None:
            stmt += " WHERE " + self.render(node.where)
        return stmt

    def _render_delete(self, node: Delete) -> str:
        self._default_table = node.table.name
        stmt = f"DELETE FROM {self.render(node.table)}"
        if node.where is not None:
            stmt += " WHERE " + self.render(node.where)
        return stmt


def hint_signature(node: Select) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Order-insensitive signature of the statement's optimizer hints."""

    return tuple(
        sorted((hint.name.upper(), hint.args) for hint in getattr(node, "hints", ()))
    )


def analyze(
    node: Node,
    in_list_buckets: tuple[int, ...] = DEFAULT_IN_LIST_BUCKETS,
) -> Analysis:
    """Normalize a statement AST and extract everything the cache needs."""

    normalizer = _Normalizer(in_list_buckets=in_list_buckets)
    canonical = normalizer.render_statement(node)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    columns = set(normalizer._columns)

    stats = frozenset(columns)
    return Analysis(
        fingerprint=fingerprint,
        canonical=canonical,
        tables=frozenset(normalizer._tables),
        columns=frozenset(columns),
        stats=stats,
        in_list_slots=tuple(normalizer._in_slots),
        hint_signature=hint_signature(node) if isinstance(node, Select) else (),
    )


def entry_key(
    analysis: Analysis,
    shape_key: tuple[tuple[Any, str], ...] = (),
) -> str:
    """Full cache key: structural fingerprint + sensitive shapes + hints.

    Two executions share an entry only when they share all three components.
    """

    material = repr(
        (
            analysis.fingerprint,
            tuple(shape_key),
            analysis.hint_signature,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
