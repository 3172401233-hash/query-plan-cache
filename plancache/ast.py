"""Lightweight SQL AST node definitions consumed by the plan cache.

The cache works on an already-parsed AST rather than raw SQL text.  Nodes are
immutable dataclasses so that identical trees hash and compare deterministically.
Only the subset needed by the cache core is modelled: SELECT / INSERT / UPDATE
/ DELETE statements, the common expression nodes, optimizer hints and the DDL /
statistics events that can invalidate cached plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class Node:
    """Base class for every AST node."""


# --------------------------------------------------------------------------- #
# Table / column references
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Table(Node):
    name: str
    alias: str | None = None


@dataclass(frozen=True)
class Column(Node):
    name: str
    table: str | None = None


@dataclass(frozen=True)
class Star(Node):
    table: str | None = None


# --------------------------------------------------------------------------- #
# Expressions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Literal(Node):
    value: Any


@dataclass(frozen=True)
class Parameter(Node):
    """Bound parameter. ``name`` is an int for positional, str for named."""

    name: Any


@dataclass(frozen=True)
class BinaryOp(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class UnaryOp(Node):
    op: str
    operand: Node


@dataclass(frozen=True)
class IsNull(Node):
    operand: Node
    negated: bool = False


@dataclass(frozen=True)
class InList(Node):
    expr: Node
    elements: tuple[Node, ...]
    negated: bool = False


@dataclass(frozen=True)
class Function(Node):
    name: str
    args: tuple[Node, ...] = ()
    distinct: bool = False


@dataclass(frozen=True)
class OrderBy(Node):
    expr: Node
    desc: bool = False


@dataclass(frozen=True)
class Join(Node):
    kind: str  # INNER / LEFT / RIGHT / FULL / CROSS
    table: Table
    on: Node | None = None


@dataclass(frozen=True)
class Hint(Node):
    """Optimizer hint, e.g. ``INDEX(t, idx_foo)``."""

    name: str
    args: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# DML statements
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Select(Node):
    columns: tuple[Node, ...]
    from_: tuple[Table, ...] = ()
    joins: tuple[Join, ...] = ()
    where: Node | None = None
    group_by: tuple[Node, ...] = ()
    having: Node | None = None
    order_by: tuple[OrderBy, ...] = ()
    limit: Node | None = None
    hints: tuple[Hint, ...] = ()


@dataclass(frozen=True)
class Assignment(Node):
    column: Column
    value: Node


@dataclass(frozen=True)
class Insert(Node):
    table: Table
    columns: tuple[Column, ...]
    values: tuple[Node, ...] | Select


@dataclass(frozen=True)
class Update(Node):
    table: Table
    assignments: tuple[Assignment, ...]
    where: Node | None = None


@dataclass(frozen=True)
class Delete(Node):
    table: Table
    where: Node | None = None


# --------------------------------------------------------------------------- #
# DDL / statistics events (invalidation inputs, never cached themselves)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CreateTable:
    table: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class DropTable:
    table: str


@dataclass(frozen=True)
class RenameTable:
    old: str
    new: str


@dataclass(frozen=True)
class AddColumn:
    table: str
    column: str


@dataclass(frozen=True)
class DropColumn:
    table: str
    column: str


@dataclass(frozen=True)
class RenameColumn:
    table: str
    old: str
    new: str


@dataclass(frozen=True)
class Analyze:
    """Statistics update. Empty ``columns`` means whole-table statistics."""

    table: str
    columns: tuple[str, ...] = ()
