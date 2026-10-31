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
    quote: str = ""        # '"', "`", "[" — SQLite's error text depends on how it was written


@dataclass
class BinaryOp(Node):
    op: str                # = != < <= > >= AND OR || + - * / %
    left: Node
    right: Node


@dataclass
class Collate(Node):
    expr: Node
    name: str


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
    text: "str | None" = None   # expression source text; SQLite uses it as the column heading


@dataclass
class Analyze(Node):
    target: "str | None" = None


@dataclass
class DropTable(Node):
    name: str
    if_exists: bool = False


@dataclass
class Vacuum(Node):
    pass


@dataclass
class Pragma(Node):
    name: str
    value: "Node | None" = None


@dataclass
class Constraint(Node):
    kind: str              # PRIMARY KEY | NOT NULL | UNIQUE | DEFAULT | CHECK | REFERENCES
    expr: "Node | None" = None
    text: "str | None" = None       # source text, for CHECK: SQLite quotes it back in errors
    ref_table: "str | None" = None
    ref_column: "str | None" = None


@dataclass
class ColumnDef(Node):
    name: str
    type_name: "str | None" = None
    type_args: list = field(default_factory=list)
    constraints: list = field(default_factory=list)


@dataclass
class TableConstraint(Node):
    kind: str                       # PRIMARY KEY | FOREIGN KEY | CHECK | UNIQUE
    columns: list = field(default_factory=list)
    ref_table: "str | None" = None
    ref_columns: list = field(default_factory=list)
    expr: "Node | None" = None
    text: "str | None" = None


@dataclass
class CreateTable(Node):
    name: str
    columns: list
    table_constraints: list = field(default_factory=list)
    if_not_exists: bool = False
    sql: "str | None" = None    # original statement text; it is what the catalog stores


@dataclass
class Begin(Node):
    mode: "str | None" = None      # DEFERRED | IMMEDIATE | EXCLUSIVE (parsed, not yet meaningful)


@dataclass
class Commit(Node):
    pass


@dataclass
class Rollback(Node):
    pass


@dataclass
class CreateIndex(Node):
    name: str
    table: str
    columns: list            # list[str]
    unique: bool = False
    if_not_exists: bool = False
    sql: "str | None" = None
    where: "Node | None" = None     # partial index predicate


@dataclass
class Insert(Node):
    table: str
    columns: list          # list[str] or None
    rows: list             # list[list[Node]]


@dataclass
class Join(Node):
    table: "str | None" = None
    alias: "str | None" = None
    on: "Node | None" = None
    kind: str = "inner"        # inner | cross | left | right | full
    select: "Select | None" = None   # derived table: FROM (SELECT ...) AS x


@dataclass
class Case(Node):
    operand: "Node | None" = None          # simple CASE x WHEN ...
    whens: list = field(default_factory=list)   # [(when_expr, then_expr)]
    else_expr: "Node | None" = None


@dataclass
class Cast(Node):
    expr: Node = None
    type_name: str = ""


@dataclass
class Between(Node):
    expr: Node = None
    low: Node = None
    high: Node = None
    negated: bool = False


@dataclass
class FuncCall(Node):
    name: str              # aggregate (COUNT/SUM/AVG/MIN/MAX) or scalar function
    arg: "Node | None" = None
    star: bool = False     # COUNT(*)
    distinct: bool = False # COUNT(DISTINCT x)
    args: list = field(default_factory=list)   # all arguments, for scalar functions


@dataclass
class Subquery(Node):
    select: "Select"


@dataclass
class InList(Node):
    expr: Node
    values: list
    negated: bool = False


@dataclass
class Exists(Node):
    select: "Select"


@dataclass
class InSubquery(Node):
    expr: Node
    select: "Select"
    negated: bool = False


@dataclass
class CreateView(Node):
    name: str
    columns: list = field(default_factory=list)   # optional `CREATE VIEW v (a, b) AS ...`
    select: "Node | None" = None
    if_not_exists: bool = False
    sql: "str | None" = None


@dataclass
class CreateTrigger(Node):
    name: str
    timing: str = "BEFORE"        # BEFORE | AFTER
    event: str = "INSERT"         # INSERT | UPDATE | DELETE
    table: str = ""
    when: "Node | None" = None
    statements: list = field(default_factory=list)
    if_not_exists: bool = False
    sql: "str | None" = None


@dataclass
class DropTrigger(Node):
    name: str
    if_exists: bool = False


@dataclass
class Raise(Node):
    action: str = "ABORT"
    message: "str | None" = None


@dataclass
class AlterTable(Node):
    table: str
    action: str                  # add | rename_table | rename_column | drop_column
    column: "ColumnDef | None" = None
    old_name: "str | None" = None
    new_name: "str | None" = None


@dataclass
class Update(Node):
    table: str
    assignments: list      # [(column, expression)]
    where: "Node | None" = None


@dataclass
class Delete(Node):
    table: str
    where: "Node | None" = None


@dataclass
class WindowFunc(Node):
    name: str
    args: list = field(default_factory=list)
    partition_by: list = field(default_factory=list)
    order_by: list = field(default_factory=list)      # [(expr, descending)]
    frame: "tuple | None" = None                     # ('rows'|'range', start, end)


@dataclass
class DropView(Node):
    name: str
    if_exists: bool = False


@dataclass
class CTE(Node):
    name: str
    columns: list
    select: "Node"
    recursive: bool = False


@dataclass
class With(Node):
    ctes: list
    body: "Node"


@dataclass
class Compound(Node):
    left: "Node"           # Select | Compound
    op: str                # UNION | UNION ALL | INTERSECT | EXCEPT
    right: "Node"
    order_by: list = field(default_factory=list)
    limit: "Node | None" = None
    offset: "Node | None" = None


@dataclass
class Select(Node):
    items: list            # list[SelectItem] or [Star]
    table: "str | None"
    where: "Node | None" = None
    distinct: bool = False
    alias: "str | None" = None
    base_select: "Select | None" = None      # derived table in FROM
    joins: list = field(default_factory=list)
    group_by: list = field(default_factory=list)
    having: "Node | None" = None
    order_by: list = field(default_factory=list)      # [(expr, descending)]
    limit: "Node | None" = None
    offset: "Node | None" = None
