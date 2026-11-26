from __future__ import annotations

import json
import time
from collections import Counter

import pymysql
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import build_scope

from .database import MAX_STEPS, InputError, check_dataset_size, snapshot, table_names
from .models import Snapshot, Step, Trace

MUTATIONS = (exp.Insert, exp.Update, exp.Delete)
BLOCKED_FUNCTIONS = {
    "SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK",
    "RELEASE_ALL_LOCKS", "IS_FREE_LOCK", "IS_USED_LOCK", "MASTER_POS_WAIT",
    "SOURCE_POS_WAIT", "WAIT_FOR_EXECUTED_GTID_SET", "UUID", "UUID_SHORT",
    "RAND", "RANDOM_BYTES", "SYSDATE",
    "FOUND_ROWS", "ROW_COUNT", "LAST_INSERT_ID",
}


def parse_sql(sql: str, *, setup: bool = False, external: bool = False) -> list[exp.Expression]:
    try:
        statements = [item for item in sqlglot.parse(sql, read="mysql") if item is not None]
    except ParseError as error:
        raise InputError(f"SQL parse error: {error}") from error
    if not statements or len(statements) > (30 if setup else 1):
        raise InputError("Provide 1 query, or at most 30 statements in a setup script.")
    for statement in statements:
        allowed = (exp.Query,) if external else (exp.Query, *MUTATIONS)
        if setup:
            allowed = (exp.Create, exp.Insert)
        if not isinstance(statement, allowed):
            raise InputError("Use SELECT/CTEs or INSERT/UPDATE/DELETE; dataset setup accepts CREATE TABLE and INSERT.")
        if isinstance(statement, exp.Create):
            if statement.kind != "TABLE" or statement.args.get("replace"):
                raise InputError("Setup supports CREATE TABLE, not views, routines or replacements.")
            if not isinstance(statement.this, (exp.Table, exp.Schema)):
                raise InputError("Unsupported CREATE TABLE target.")
            for prop in statement.find_all(exp.Property):
                if isinstance(prop, exp.EngineProperty) and prop.this.name.upper() != "INNODB":
                    raise InputError("Only transactional InnoDB tables are supported.")
                if prop.key.lower() in {"temporaryproperty", "externalproperty"}:
                    raise InputError("Temporary and external tables are not supported.")
        for node in statement.walk():
            if isinstance(node, exp.Table) and (node.db or node.catalog) and not external:
                raise InputError("Workspace SQL cannot reference another database or catalog.")
            if isinstance(node, (exp.Into, exp.Lock, exp.Parameter, exp.SessionParameter)):
                raise InputError("File output, locking clauses and session/user variables are not supported.")
            if isinstance(node, exp.Func):
                name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
                if name in BLOCKED_FUNCTIONS:
                    raise InputError(f"{name} is disabled: tracing requires bounded, repeatable expressions.")
    return statements


def in_scope(query: exp.Expression) -> list[exp.Expression]:
    return list(query.walk(prune=lambda node: node is not query and isinstance(node, exp.Query)))


def contextualize(query: exp.Query, ctes: list[exp.CTE], recursive: bool) -> exp.Query:
    result = query.copy()
    if ctes:
        result.set("with_", exp.With(expressions=[cte.copy() for cte in ctes], recursive=recursive))
    return result


def without(query: exp.Query, *keys: str) -> exp.Query:
    result = query.copy()
    for key in keys:
        result.set(key, None)
    return result


class Builder:
    def __init__(self, connection: pymysql.Connection, source: str):
        self.connection = connection
        self.source = source
        self.steps: list[Step] = []
        self.started = time.monotonic()
        self.notices = [
            "Logical snapshots are explanatory MySQL queries, not a row-by-row engine trace.",
            "Without ORDER BY, row order is not guaranteed. Compare values, not row positions.",
        ]

    def add(
        self, kind: str, title: str, explanation: str, query: exp.Query,
        depth: int = 0, source: exp.Expression | None = None,
        details: list[str] | None = None, data: Snapshot | None = None,
    ) -> Step:
        if len(self.steps) >= MAX_STEPS or time.monotonic() - self.started > 20:
            raise InputError("Trace limit reached (150 stages or 20 seconds). Simplify the query.")
        if data is None:
            try:
                data = snapshot(self.connection, query)
            except pymysql.MySQLError as error:
                raise InputError(f"Cannot reconstruct stage '{title}': {error}. This intermediate form is not supported.") from error
        positions = [
            node.meta for node in (source.walk() if source is not None else [])
            if "start" in node.meta and "end" in node.meta
        ]
        step = Step(
            id=len(self.steps), kind=kind, title=title, explanation=explanation,
            sql=query.sql(dialect="mysql"), data=data, depth=depth,
            details=details or [],
            source_start=min((item["start"] for item in positions), default=None),
            source_end=max((item["end"] + 1 for item in positions), default=None),
        )
        self.steps.append(step)
        return step

    def query(
        self, original: exp.Query, inherited: list[exp.CTE] | None = None,
        recursive: bool = False, depth: int = 0, label: str = "Query",
    ) -> None:
        local = original.args.get("with_")
        local_ctes = list(local.expressions) if local else []
        local_names = {cte.alias.casefold() for cte in local_ctes}
        ctes = [cte for cte in inherited or [] if cte.alias.casefold() not in local_names]
        recursive = recursive or bool(local and local.args.get("recursive"))
        for cte in local_ctes:
            self_reference = any(table.name.casefold() == cte.alias.casefold() for table in cte.this.find_all(exp.Table))
            if not self_reference:
                self.query(cte.this, ctes, recursive, depth + 1, cte.alias)
            ctes.append(cte)
            result_query = contextualize(exp.select("*").from_(exp.to_table(cte.alias, quoted=True)), ctes, recursive)
            result = self.add(
                "CTE", f"CTE: {cte.alias}", "Resolve this named relation before its consumers. MySQL may inline or materialize it physically.",
                result_query, depth + 1, cte,
            )
            if self_reference:
                self.recursive_iterations(cte, ctes, result, depth + 2)

        query = contextualize(original, ctes, recursive)
        if isinstance(query, exp.Subquery):
            self.query(query.this, ctes, recursive, depth, label)
            return
        if isinstance(query, exp.SetOperation):
            self.query(original.this, ctes, recursive, depth + 1, "Left branch")
            self.query(original.expression, ctes, recursive, depth + 1, "Right branch")
            combined = without(query, "order", "limit", "offset")
            self.add(
                query.key.upper(), f"{label}: {query.key.upper()}",
                "Combine the branch results using MySQL's set and duplicate-handling semantics.",
                combined, depth, original,
            )
            self.finish(query, depth)
            return
        if not isinstance(query, exp.Select):
            raise InputError("This query expression is not yet traceable.")

        scope = build_scope(original)
        if scope:
            for child in scope.derived_table_scopes + scope.subquery_scopes:
                if not child.is_correlated_subquery:
                    self.query(child.expression, ctes, recursive, depth + 1, "Subquery")

        source = query.args.get("from_")
        base = without(query, "where", "group", "having", "windows", "qualify", "order", "limit", "offset", "distinct")
        base.set("expressions", [exp.Star()])
        joins = list(query.args.get("joins") or [])
        base.set("joins", [])
        if source:
            self.add("FROM", f"{label}: read source", "Start with the source relation. A derived table is evaluated in its own query scope.", base, depth, source)
            for index, join in enumerate(joins):
                base.set("joins", [item.copy() for item in joins[:index + 1]])
                self.add(
                    "JOIN", f"{join.side or join.kind or 'INNER'} JOIN {join.this.alias_or_name}",
                    "Apply this join's predicate. Outer joins preserve unmatched rows with NULLs; SQL NULL equality is not true.",
                    base, depth, join, [join.sql(dialect="mysql")],
                )
        where = query.args.get("where")
        if where:
            evaluated = base.copy()
            evaluated.set("expressions", [exp.Star(), exp.alias_(where.this.copy(), "__viz_predicate", quoted=True)])
            self.add(
                "PREDICATE", "Evaluate WHERE for each row",
                "The last column is 1 (true), 0 (false), or NULL (unknown). WHERE keeps only true. Correlated subqueries are evaluated in their parent-row context here; the optimizer may implement them differently.",
                evaluated, depth + 1, where,
            )
            base.set("where", where.copy())
            self.add("WHERE", "Keep rows satisfying WHERE", "Filter source rows before grouping or window calculations.", base, depth, where)

        nodes = in_scope(query)
        windows = [node for node in nodes if isinstance(node, exp.Window)]
        aggregates = [
            node for node in nodes if isinstance(node, exp.AggFunc)
            and not (isinstance(node.parent, exp.Window) and node.parent.this is node)
        ]
        if query.args.get("group") or aggregates:
            grouped = without(query, "having", "order", "limit", "offset", "distinct", "windows")
            expressions = [item.copy() for item in query.expressions if not any(isinstance(n, exp.Window) for n in in_scope(item))]
            for index, aggregate in enumerate(aggregates):
                if not any(aggregate.sql() in item.sql() for item in expressions):
                    expressions.append(exp.alias_(aggregate.copy(), f"__viz_aggregate_{index}"))
            grouped.set("expressions", expressions or [exp.alias_(exp.Count(this=exp.Star()), "__viz_count")])
            self.add(
                "GROUP BY", "Form groups and compute aggregates",
                "Compute one result per group (or one aggregate group without GROUP BY). The table includes expressions needed to explain the grouped result.",
                grouped, depth, query.args.get("group") or query,
            )
            if query.args.get("having"):
                grouped.set("having", query.args["having"].copy())
                self.add("HAVING", "Filter aggregated groups", "HAVING tests groups after aggregation, not individual input rows.", grouped, depth, query.args["having"])

        projected = without(query, "order", "limit", "offset", "distinct")
        if windows:
            self.add(
                "WINDOW", "Compute window expressions",
                "Compute windows after filtering/grouping while preserving result rows. PARTITION BY separates calculations; window ORDER BY and ROWS/RANGE control ordering and frame membership.",
                projected, depth, windows[0],
                list(dict.fromkeys(window.sql(dialect="mysql") for window in windows)),
            )
        self.add(
            "SELECT", f"{label}: project result columns",
            "Evaluate the selected expressions and aliases. Scalar subqueries in the projection are evaluated by MySQL in the applicable row context.",
            projected, depth, exp.Tuple(expressions=[item.copy() for item in original.expressions]),
        )
        if query.args.get("distinct"):
            self.add("DISTINCT", "Remove duplicate result rows", "DISTINCT compares the projected result values, not original row identity.", without(query, "order", "limit", "offset"), depth, query.args["distinct"])
        self.finish(query, depth)

    def finish(self, query: exp.Query, depth: int) -> None:
        if query.args.get("order"):
            self.add("ORDER BY", "Sort the result", "Apply the query's final ordering. A window's ORDER BY alone does not sort the final output.", without(query, "limit", "offset"), depth, query.args["order"])
        if query.args.get("limit") or query.args.get("offset"):
            self.add("LIMIT", "Apply LIMIT / OFFSET", "Select the requested slice after final ordering. Ties need a deterministic tie-breaker for reproducible pages.", query, depth, query.args.get("limit") or query)
        self.add("RESULT", "Final MySQL result", "This is the original query evaluated by MySQL. Earlier tables explain its logical transformations.", query, depth)

    def recursive_iterations(self, cte: exp.CTE, ctes: list[exp.CTE], result: Step, depth: int) -> None:
        union = cte.this
        reason = "Iteration-level detail is available only for a simple UNION ALL anchor/member without SELECT *, DISTINCT, or LIMIT. This CTE's result and physical plan remain available."
        safe = (
            isinstance(union, exp.Union) and union.args.get("distinct") is False
            and isinstance(union.this, exp.Select) and isinstance(union.expression, exp.Select)
            and not any(isinstance(node, (exp.Star, exp.Limit, exp.Distinct)) for node in union.walk())
            and not result.data.truncated
        )
        if not safe:
            result.details.append(reason)
            return
        references = [table for table in union.expression.find_all(exp.Table) if table.name.casefold() == cte.alias.casefold()]
        if len(references) != 1:
            result.details.append(reason)
            return
        marker = "__viz_iteration_" + str(len(self.steps))
        if any(identifier.name.casefold() == marker.casefold() for identifier in cte.find_all(exp.Identifier)):
            result.details.append(reason)
            return
        tagged = cte.copy()
        tagged.this.this.append("expressions", exp.alias_(exp.Literal.number(0), marker))
        tagged.this.expression.append(
            "expressions",
            exp.alias_(exp.Add(this=exp.column(marker, table=references[0].alias_or_name), expression=exp.Literal.number(1)), marker),
        )
        if tagged.args["alias"].args.get("columns"):
            tagged.args["alias"].append("columns", exp.to_identifier(marker))
        instrumented = contextualize(
            exp.select("*").from_(exp.to_table(cte.alias, quoted=True)),
            [*ctes[:-1], tagged], True,
        )
        data = snapshot(self.connection, instrumented)
        values = [row[:-1] for row in data.rows]
        fingerprint = lambda rows: Counter(json.dumps(row, sort_keys=True) for row in rows)
        if data.truncated or fingerprint(values) != fingerprint(result.data.rows):
            result.details.append("Iteration instrumentation did not reproduce the authoritative row multiset; iteration animation was not used.")
            return
        for iteration in sorted({int(row[-1]) for row in data.rows}):
            current = Snapshot(columns=data.columns[:-1], rows=[row[:-1] for row in data.rows if int(row[-1]) == iteration])
            self.add(
                "RECURSION", f"{cte.alias}: {'anchor' if iteration == 0 else f'iteration {iteration}'}",
                "Rows generated at this recursive depth. Depth-tagged instrumentation was checked against the original CTE's complete row multiset.",
                instrumented, depth, cte, data=current,
            )

    def mutation(self, statement: exp.Expression) -> int:
        target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        if not isinstance(target, exp.Table) or target.args.get("joins") or statement.args.get("tables") or statement.args.get("using"):
            raise InputError("Mutation tracing currently supports a single target table, not multi-table UPDATE/DELETE.")
        read = exp.select("*").from_(target.copy())
        related = {
            name: snapshot(self.connection, exp.select("*").from_(exp.to_table(name, quoted=True)))
            for name in table_names(self.connection) if name != target.name
        }
        before = self.add("BEFORE", f"Before: {target.name}", "The persisted table before the proposed change.", read)
        if statement.args.get("where"):
            matched = read.copy()
            matched.set("where", statement.args["where"].copy())
            if statement.args.get("with_"):
                matched.set("with_", statement.args["with_"].copy())
            self.add("MATCH", "Rows satisfying the mutation predicate", "This is the predicate match set before ORDER BY/LIMIT, if present.", matched, source=statement.args["where"])
        if isinstance(statement, exp.Insert) and isinstance(statement.expression, exp.Query):
            self.query(statement.expression, depth=1, label="INSERT source")
        with self.connection.cursor() as cursor:
            cursor.execute(statement.sql(dialect="mysql"))
            affected = cursor.rowcount
        check_dataset_size(self.connection)
        for name, old_data in related.items():
            related_query = exp.select("*").from_(exp.to_table(name, quoted=True))
            new_data = snapshot(self.connection, related_query)
            fingerprint = lambda data: Counter(json.dumps(row) for row in data.rows)
            if old_data.columns != new_data.columns or fingerprint(old_data) != fingerprint(new_data):
                self.add("BEFORE", f"Related table before: {name}", "A related table changed, for example through a foreign-key cascade.", related_query, depth=1, data=old_data)
                self.add("AFTER", f"Related table after: {name} (preview)", "This related-table change is also rolled back with the mutation preview.", related_query, depth=1, data=new_data)
        after = self.add("AFTER", f"After: {target.name} (preview)", "This change is rolled back after tracing. Only Apply mutation persists it in this isolated workspace.", read)
        if not before.data.truncated and not after.data.truncated:
            old = Counter(json.dumps(row) for row in before.data.rows)
            new = Counter(json.dumps(row) for row in after.data.rows)
            after.details = [
                f"Removed row versions: {sum((old - new).values())}",
                f"Added row versions: {sum((new - old).values())}",
                f"MySQL affected rows: {affected}",
                "Updates appear as a removed and added row version; duplicate rows retain their multiplicity.",
            ]
        return affected


def make_trace(connection: pymysql.Connection, sql: str, analyze: bool = False) -> Trace:
    statement = parse_sql(sql)[0]
    mutation = isinstance(statement, MUTATIONS)
    if mutation and analyze:
        raise InputError("EXPLAIN ANALYZE is available only for read-only queries.")
    with connection.cursor() as cursor:
        cursor.execute("SET timestamp=UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))")
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION" if mutation else "START TRANSACTION READ ONLY")
        cursor.execute("EXPLAIN FORMAT=JSON " + statement.sql(dialect="mysql"))
        plan = json.loads(cursor.fetchone()[0])
        analysis = None
        if analyze:
            cursor.execute("EXPLAIN ANALYZE FORMAT=TREE " + statement.sql(dialect="mysql"))
            analysis = "\n".join(row[0] for row in cursor.fetchall())
    builder = Builder(connection, sql)
    try:
        affected = builder.mutation(statement) if mutation else None
        if not mutation:
            builder.query(statement)
        return Trace(
            query=sql, steps=builder.steps, plan=plan, analyze=analysis,
            mutation=mutation, affected_rows=affected, notices=builder.notices,
        )
    finally:
        connection.rollback()
