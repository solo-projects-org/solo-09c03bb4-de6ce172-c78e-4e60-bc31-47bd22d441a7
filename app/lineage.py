"""SQL 解析、校验与模型读取关系提取。

输入：源表字段清单 + 各模型的 DuckDB SQL（普通 SELECT、非递归 CTE、
INNER JOIN、WHERE，无星号，列引用均带表名/别名）。

本模块只做静态解析，不执行任何 SQL。输出每个模型：
- 根查询的输出列清单；
- 直接上游模型集合；
- 全部列引用（含所在查询、子句 SELECT/WHERE/JOIN ON 与表达式片段），
  供联发分析判断“某版本模型读取的列在另一版本上游上是否存在”。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

DIALECT = "duckdb"

# 明确拒绝、且给出可读提示的语句/节点。
# 其余表达式节点（各类函数、运算、类型转换、CASE 等）一律放行：
# 列级引用提取只依赖 Column 节点与作用域结构，函数内部不改变引用口径。
_REJECTED = {
    "Union": "UNION / 集合运算",
    "Except": "EXCEPT 集合运算",
    "Intersect": "INTERSECT 集合运算",
    "Group": "GROUP BY 聚合分组",
    "Having": "HAVING 筛选",
    "Window": "窗口函数",
    "Pivot": "PIVOT 透视",
    "Merge": "MERGE 语句",
    "Insert": "INSERT 语句",
    "Update": "UPDATE 语句",
    "Delete": "DELETE 语句",
    "Create": "CREATE 语句",
    "Drop": "DROP 语句",
    "AlterTable": "ALTER TABLE 语句",
}

SNIPPET_LIMIT = 120


class AnalysisError(Exception):
    """分析无法继续时抛出，message 为可直接展示给用户的中文说明。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leaf:
    """叶子依赖：某个可读关系（源表/上游模型/CTE/子查询）的一列。"""
    relation: str
    column: str


@dataclass(frozen=True)
class ColumnRef:
    """一次可回溯的列引用。"""
    # 查询内的限定名（别名或关系名）
    qualifier: str
    # table | model | cte | subquery
    source_kind: str
    # 实际表名 / 模型名 / CTE 名 / 子查询别名
    source_name: str
    column: str
    # 所在查询：模型根查询为模型名，其余为 CTE 名 / 子查询别名
    query_name: str
    # SELECT | WHERE | JOIN ON
    clause: str
    # 所在表达式的 SQL 片段（截断），用于在页面上指出“哪处引用”
    snippet: str


@dataclass
class QueryNode:
    """一个 SELECT 作用域（根查询 / CTE / FROM 子查询 / WHERE 子查询）。"""
    node_id: str
    kind: str  # root | cte | subquery
    name: str
    output_columns: list[str]
    output_leaves: dict[str, set[Leaf]]
    # 关系限定名 -> (类型, 实际对象名)
    sources: dict[str, tuple[str, str]]
    predicate_leaves: set[Leaf]
    scope_expr: object = None
    # 本作用域内解析出的全部列引用（含子句与片段）
    refs: list[ColumnRef] = field(default_factory=list)


@dataclass
class ModelInfo:
    name: str
    sql: str
    nodes: list[QueryNode]
    by_name: dict[str, QueryNode]
    upstream: set[str]
    outputs: list[str]
    refs: list[ColumnRef]

    def model_reads(self) -> dict[str, dict[str, list[ColumnRef]]]:
        """上游模型名 -> (被读列 -> 该列的全部引用位置)，只统计对上游模型的读取。"""
        reads: dict[str, dict[str, list[ColumnRef]]] = {}
        for ref in self.refs:
            if ref.source_kind != "model":
                continue
            reads.setdefault(ref.source_name, {}).setdefault(ref.column, []).append(ref)
        return reads


# ---------------------------------------------------------------------------
# 解析与结构校验
# ---------------------------------------------------------------------------

def _parse_sql(sql: str, model_name: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except sqlglot.errors.ParseError as exc:
        raise AnalysisError(f"模型「{model_name}」SQL 解析失败：{exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise AnalysisError(
            f"模型「{model_name}」必须只包含一条 SELECT 语句，当前解析出 {len(statements)} 条。"
        )
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise AnalysisError(f"模型「{model_name}」只允许普通 SELECT，不支持 {type(tree).__name__}。")
    _validate_structure(tree, model_name)
    return tree


def _validate_structure(tree: exp.Expression, model_name: str) -> None:
    with_node = tree.find(exp.With)
    if with_node is not None and with_node.args.get("recursive"):
        raise AnalysisError(f"模型「{model_name}」使用了递归 CTE，本工具仅支持非递归 CTE。")

    for node in tree.walk():
        tname = type(node).__name__
        if tname in _REJECTED:
            raise AnalysisError(
                f"模型「{model_name}」包含不支持的语法：{_REJECTED[tname]}。"
            )
        is_star = isinstance(node, exp.Star) or (
            isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
        )
        if is_star:
            raise AnalysisError(
                f"模型「{model_name}」不允许使用星号 SELECT * / t.*（{node.sql()}），"
                "请显式列出列。"
            )
        if isinstance(node, exp.Join):
            kind = (node.kind or "").upper()
            if kind != "INNER":
                raise AnalysisError(
                    f"模型「{model_name}」仅允许显式 INNER JOIN，"
                    f"发现 {kind or '逗号隐式连接/CROSS'} JOIN，请改写为 INNER JOIN ... ON ...。"
                )
            if node.args.get("side") or node.args.get("method"):
                raise AnalysisError(
                    f"模型「{model_name}」仅允许普通 INNER JOIN，发现特殊 JOIN 修饰。"
                )


# ---------------------------------------------------------------------------
# 列引用解析
# ---------------------------------------------------------------------------

def _resolve_leaf(col: exp.Column, scope, node: QueryNode, model_name: str) -> Leaf:
    qualifier = col.table
    if not qualifier:
        raise AnalysisError(
            f"模型「{model_name}」的查询「{node.name}」中列「{col.name}」未带表名或别名，"
            "请为所有列引用补充限定名。"
        )
    if qualifier not in scope.sources:
        outer = scope.parent
        in_outer = outer is not None and qualifier in outer.sources
        if in_outer:
            raise AnalysisError(
                f"模型「{model_name}」的子查询「{node.name}」引用了外层查询的表/别名"
                f"「{qualifier}」（列「{col.sql()}」），属于关联子查询，本工具不支持该写法。"
            )
        raise AnalysisError(
            f"模型「{model_name}」的查询「{node.name}」中引用了未定义的表名/别名「{qualifier}」"
            f"（列「{col.sql()}」），无法解析引用，分析已停止。"
        )
    return Leaf(relation=qualifier, column=col.name)


def _collect_leaves(expression: exp.Expression | None, scope, node: QueryNode,
                    model_name: str) -> set[Leaf]:
    """收集表达式中本作用域的全部列引用叶子；子查询内部剪枝，由其自身作用域处理。"""
    leaves: set[Leaf] = set()
    if expression is None:
        return leaves
    stack = [expression]
    while stack:
        child = stack.pop()
        if isinstance(child, exp.Expression):
            pass
        elif isinstance(child, list):
            stack.extend(child)
            continue
        else:
            continue
        if isinstance(child, exp.Subquery):
            continue
        if isinstance(child, exp.Select) and child is not expression:
            continue
        if isinstance(child, exp.Column) and not isinstance(child.this, exp.Star):
            leaves.add(_resolve_leaf(child, scope, node, model_name))
            continue
        stack.extend(child.args.values())
    return leaves


def _snippet(expression: exp.Expression) -> str:
    text = expression.sql(dialect=DIALECT)
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_LIMIT else text[:SNIPPET_LIMIT - 1] + "…"


def _record_refs(node: QueryNode, leaves: set[Leaf], clause: str,
                 expression: exp.Expression) -> None:
    snippet = _snippet(expression)
    for leaf in sorted(leaves, key=lambda x: (x.relation, x.column)):
        kind, actual = node.sources[leaf.relation]
        node.refs.append(ColumnRef(
            qualifier=leaf.relation,
            source_kind=kind,
            source_name=actual,
            column=leaf.column,
            query_name=node.name,
            clause=clause,
            snippet=snippet,
        ))


# ---------------------------------------------------------------------------
# 单模型构建
# ---------------------------------------------------------------------------

def _build_model(model_name: str, sql: str,
                 source_schemas: dict[str, list[str]],
                 model_names: set[str]) -> tuple[list[QueryNode], dict[str, QueryNode]]:
    tree = _parse_sql(sql, model_name)
    nodes: list[QueryNode] = []
    by_name: dict[str, QueryNode] = {}
    sub_seq = 0

    for scope in traverse_scope(tree):
        expr = scope.expression
        parent = expr.parent
        if scope.is_root or parent is None:
            kind, qname = "root", model_name
            node_id = f"{model_name}#root"
        elif isinstance(parent, exp.CTE):
            kind, qname = "cte", parent.alias
            node_id = f"{model_name}#cte:{qname}"
        else:
            is_in_sub = isinstance(parent, exp.Subquery) and parent.find_ancestor(exp.In) is not None
            is_exists = isinstance(parent, exp.Exists)
            in_predicate = is_in_sub or is_exists
            is_scalar = (
                isinstance(parent, exp.Subquery)
                and not isinstance(parent.parent, exp.From)
                and parent.find_ancestor(exp.In) is None
            )
            if is_scalar:
                raise AnalysisError(
                    f"模型「{model_name}」在 SELECT 投影中使用了标量子查询"
                    f"（{parent.sql()[:60]}），本工具不支持该写法，请改写为 INNER JOIN。"
                )
            if in_predicate:
                kind = "subquery"
                sub_seq += 1
                pred = "EXISTS" if is_exists else "IN"
                qname = f"{pred} 子查询#{sub_seq}"
                node_id = f"{model_name}#predsub:{sub_seq}"
            else:
                kind = "subquery"
                if not isinstance(parent, exp.Subquery):
                    raise AnalysisError(
                        f"模型「{model_name}」存在无法归类的子查询（父节点 {type(parent).__name__}），"
                        "本工具仅支持 FROM 派生表与 IN/EXISTS 谓词子查询。"
                    )
                sub_alias = parent.alias
                if not sub_alias:
                    raise AnalysisError(
                        f"模型「{model_name}」存在未起别名的 FROM 子查询，请为其添加别名。"
                    )
                qname = sub_alias
                sub_seq += 1
                node_id = f"{model_name}#sub:{qname}"

        if qname in by_name:
            raise AnalysisError(f"模型「{model_name}」中 CTE/子查询名「{qname}」重复。")

        sources: dict[str, tuple[str, str]] = {}
        for sname, source in scope.sources.items():
            if isinstance(source, exp.Table):
                actual = source.name
                if actual in model_names:
                    sources[sname] = ("model", actual)
                else:
                    if actual not in source_schemas:
                        raise AnalysisError(
                            f"模型「{model_name}」读取了未知表「{actual}」："
                            "它既不在源表清单中，也不是已定义的模型。无法解析引用，分析已停止。"
                        )
                    sources[sname] = ("table", actual)
            else:
                ptype = type(source.expression.parent).__name__ if source.expression.parent else ""
                if ptype == "CTE":
                    sources[sname] = ("cte", source.expression.parent.alias)
                else:
                    alias = source.expression.parent.alias
                    sources[sname] = ("subquery", alias)

        output_columns: list[str] = []
        output_leaves: dict[str, set[Leaf]] = {}

        node = QueryNode(
            node_id=node_id,
            kind=kind,
            name=qname,
            output_columns=output_columns,
            output_leaves=output_leaves,
            sources=sources,
            predicate_leaves=set(),
            scope_expr=expr,
        )

        # SELECT 投影
        for projection in expr.selects:
            out_name = projection.alias_or_name
            if not out_name:
                raise AnalysisError(
                    f"模型「{model_name}」的查询「{qname}」存在无法命名的输出表达式，请显式加别名。"
                )
            if out_name in output_leaves:
                raise AnalysisError(
                    f"模型「{model_name}」的查询「{qname}」输出列「{out_name}」重复。"
                )
            leaves = _collect_leaves(projection, scope, node, model_name)
            output_columns.append(out_name)
            output_leaves[out_name] = leaves
            _record_refs(node, leaves, "SELECT", projection)

        # WHERE
        where = expr.args.get("where")
        if where is not None:
            leaves = _collect_leaves(where, scope, node, model_name)
            node.predicate_leaves |= leaves
            _record_refs(node, leaves, "WHERE", where.this)

        # INNER JOIN ON
        for join in expr.args.get("joins", []):
            on = join.args.get("on")
            if on is not None:
                leaves = _collect_leaves(on, scope, node, model_name)
                node.predicate_leaves |= leaves
                _record_refs(node, leaves, "JOIN ON", on)
            if join.args.get("using"):
                raise AnalysisError(
                    f"模型「{model_name}」使用了 JOIN ... USING，请改写为 ON 等值条件并带限定名。"
                )

        if scope.is_correlated_subquery:
            raise AnalysisError(
                f"模型「{model_name}」的子查询「{qname}」引用了外层查询的列（关联子查询），"
                "本工具不支持该写法。"
            )

        # 源表列 / CTE / 子查询列存在性
        all_leaves: set[Leaf] = set(node.predicate_leaves)
        for ls in output_leaves.values():
            all_leaves |= ls
        for leaf in all_leaves:
            stype, actual = _source_of(node, leaf.relation)
            if stype == "table":
                if leaf.column not in source_schemas.get(actual, []):
                    raise AnalysisError(
                        f"模型「{model_name}」引用了源表「{actual}」上不存在的列「{leaf.column}」，"
                        "无法解析引用，分析已停止。"
                    )
            elif stype == "model":
                # 上游模型输出列在全部模型构建后统一校验
                pass
            else:
                upstream = by_name.get(actual)
                if upstream is not None and leaf.column not in upstream.output_columns:
                    raise AnalysisError(
                        f"模型「{model_name}」的查询「{qname}」引用了"
                        f"{'CTE' if stype == 'cte' else '子查询'}「{actual}」上不存在的列"
                        f"「{leaf.column}」，无法解析引用，分析已停止。"
                    )

        nodes.append(node)
        by_name[qname] = node

    return nodes, by_name


def _source_of(node: QueryNode, relation: str) -> tuple[str, str]:
    for sname, (stype, actual) in node.sources.items():
        if sname == relation:
            return stype, actual
    raise AnalysisError(f"内部错误：查询「{node.name}」找不到关系「{relation}」。")


# ---------------------------------------------------------------------------
# 模型 DAG
# ---------------------------------------------------------------------------

def build_models(payload: dict) -> tuple[dict[str, list[str]], dict[str, ModelInfo], list[str]]:
    """构建并校验一个版本的全部模型。

    返回（源表 schema、模型信息、模型拓扑序）。任一 SQL 或引用无法解析时
    抛出带模型名的 AnalysisError。
    """
    source_tables = payload.get("source_tables")
    models = payload.get("models")
    if not isinstance(source_tables, list) or not isinstance(models, list):
        raise AnalysisError("JSON 需包含数组字段 source_tables 和 models。")

    source_schemas: dict[str, list[str]] = {}
    for i, tbl in enumerate(source_tables):
        if not isinstance(tbl, dict):
            raise AnalysisError(f"source_tables[{i}] 必须是对象。")
        tname = tbl.get("table")
        cols = tbl.get("columns")
        if not tname or not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
            raise AnalysisError(
                f"source_tables[{i}] 需包含字符串 table 和字符串数组 columns。"
            )
        if tname in source_schemas:
            raise AnalysisError(f"源表「{tname}」在清单中重复出现。")
        source_schemas[tname] = list(cols)

    model_names = {m.get("name") for m in models if isinstance(m, dict)}
    if len(model_names) != len(models) or any(not n for n in model_names):
        raise AnalysisError("models 中每个模型需要唯一的字符串 name。")

    infos: dict[str, ModelInfo] = {}
    for m in models:
        mname, sql = m["name"], m.get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            raise AnalysisError(f"模型「{mname}」缺少非空 sql。")
        nodes, by_name = _build_model(mname, sql, source_schemas, model_names)
        upstream = {
            actual
            for node in nodes
            for stype, actual in node.sources.values()
            if stype == "model"
        }
        if mname in upstream:
            raise AnalysisError(f"模型「{mname}」不能读取自身。")
        root = nodes[-1]  # traverse_scope 为后序，根在最后
        refs = [ref for node in nodes for ref in node.refs]
        infos[mname] = ModelInfo(
            name=mname, sql=sql, nodes=nodes, by_name=by_name,
            upstream=upstream, outputs=list(root.output_columns), refs=refs,
        )

    order = _topological_order(infos)

    # 上游模型输出列存在性（同版本内自洽校验）
    for info in infos.values():
        for up, cols in info.model_reads().items():
            available = set(infos[up].outputs)
            for col in cols:
                if col not in available:
                    raise AnalysisError(
                        f"模型「{info.name}」引用了上游模型「{up}」上不存在的输出列"
                        f"「{col}」，无法解析引用，分析已停止。"
                    )
    return source_schemas, infos, order


def _topological_order(infos: dict[str, ModelInfo]) -> list[str]:
    order: list[str] = []
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成

    def visit(name: str, stack: list[str]) -> None:
        s = state.get(name, 0)
        if s == 2:
            return
        if s == 1:
            cycle = " -> ".join(stack[stack.index(name):] + [name])
            raise AnalysisError(f"模型依赖存在循环：{cycle}，本工具仅支持有向无环的模型血缘。")
        state[name] = 1
        for up in sorted(infos[name].upstream):
            visit(up, stack + [name])
        state[name] = 2
        order.append(name)

    for name in sorted(infos):
        visit(name, [])
    return order
