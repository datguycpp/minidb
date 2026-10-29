"""AST nodes for the MiniDB SQL subset.

Plain dataclasses: equality is structural, so parser tests can compare an expected tree
against a parsed one without hand-written matchers.
"""

from dataclasses import dataclass, field


class Node:
    """Marker base class."""


@dataclass
class Literal(Node):
    value: object          # int | float | str | None | bool
    kind: str              # number | string | null | bool


@dataclass
class ColumnRef(Node):
    name: str
    table: "str | None" = None


@dataclass
class BinaryOp(Node):
    op: str                # = != < <= > >= AND OR || + - * / %
    left: Node
    right: Node


@dataclass
class UnaryOp(Node):
    op: str                # NOT | - | +
    operand: Node


@dataclass
class IsNull(Node):
    expr: Node
    negated: bool = False


@dataclass
class Star(Node):
    table: str = None


@dataclass
class SelectItem(Node):
    expr: Node
    alias: "str | None" = None


@dataclass
class Constraint(Node):
    kind: str              # PRIMARY KEY | NOT NULL | UNIQUE | DEFAULT | CHECK
    value: "Node | None" = None


@dataclass
class ColumnDef(Node):
    name: str
    type_name: "str | None" = None
    type_args: list = field(default_factory=list)
    constraints: list = field(default_factory=list)


@dataclass
class CreateTable(Node):
    name: str
    columns: list
    if_not_exists: bool = False


@dataclass
class Insert(Node):
    table: str
    columns: list          # list[str] or None
    rows: list             # list[list[Node]]


@dataclass
class Select(Node):
    items: list            # list[SelectItem] or [Star]
    table: str
    where: "Node | None" = None
    distinct: bool = False
