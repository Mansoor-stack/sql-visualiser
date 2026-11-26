import os

import httpx
import pytest
from fastapi.testclient import TestClient

from sqlviz.api import app
from sqlviz.database import create_workspace, drop_workspace, workspace_connection
from sqlviz.examples import DATASET, EXAMPLES
from sqlviz.tracing import make_trace


@pytest.fixture
def client():
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.fixture
def workspace(client):
    workspace = create_workspace()
    try:
        response = client.post(f"/api/workspaces/{workspace}/setup", json={"sql": DATASET})
        assert response.status_code == 200, response.text
        yield workspace
    finally:
        drop_workspace(workspace)


def run(client, workspace, sql, **options):
    response = client.post(f"/api/workspaces/{workspace}/trace", json={"sql": sql, **options})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("example", EXAMPLES, ids=[example["name"] for example in EXAMPLES])
def test_examples_match_mysql(client, workspace, example):
    trace = run(client, workspace, example["sql"])
    assert trace["steps"] and trace["plan"]
    if not trace["mutation"]:
        with workspace_connection(workspace) as connection:
            with connection.cursor() as cursor:
                cursor.execute(example["sql"])
                expected = [[str(value) if type(value).__name__ == "Decimal" else value for value in row] for row in cursor.fetchall()]
        assert trace["steps"][-1]["data"]["rows"] == expected


def test_join_keeps_zero_null_and_duplicate_rows(client, workspace):
    response = client.post(f"/api/workspaces/{workspace}/setup", json={"sql": """
    CREATE TABLE a (id INT); INSERT INTO a VALUES (0),(NULL),(2);
    CREATE TABLE b (id INT); INSERT INTO b VALUES (0),(NULL),(2),(2);
    """})
    assert response.status_code == 200, response.text
    trace = run(client, workspace, "SELECT a.id AS a_id,b.id AS b_id FROM a LEFT JOIN b ON a.id=b.id ORDER BY a.id,b.id")
    join = next(step for step in trace["steps"] if step["kind"] == "JOIN")
    assert len(join["data"]["rows"]) == 4
    assert [0, 0] in join["data"]["rows"]
    assert join["data"]["rows"].count([2, 2]) == 2
    assert [None, None] in join["data"]["rows"]


def test_predicate_shows_three_valued_logic_and_correlation(client, workspace):
    sql = "SELECT e.name FROM employees e WHERE e.department_id=1 AND EXISTS(SELECT 1 FROM sales s WHERE s.employee_id=e.id)"
    trace = run(client, workspace, sql)
    predicate = next(step for step in trace["steps"] if step["kind"] == "PREDICATE")
    assert {row[-1] for row in predicate["data"]["rows"]} >= {0, 1}
    null_trace = run(client, workspace, "SELECT name FROM employees WHERE department_id=1")
    evaluated = next(step for step in null_trace["steps"] if step["kind"] == "PREDICATE")
    assert None in [row[-1] for row in evaluated["data"]["rows"]]


def test_group_alias_having_and_window_over_aggregates(client, workspace):
    sql = """SELECT department_id AS dep, SUM(salary) AS total,
    DENSE_RANK() OVER(ORDER BY SUM(salary) DESC) AS r
    FROM employees GROUP BY dep HAVING total > 100000 ORDER BY r LIMIT 2"""
    trace = run(client, workspace, sql)
    kinds = [step["kind"] for step in trace["steps"]]
    assert kinds.index("GROUP BY") < kinds.index("HAVING") < kinds.index("WINDOW") < kinds.index("ORDER BY") < kinds.index("LIMIT")
    assert len(trace["steps"][-1]["data"]["rows"]) == 2


def test_derived_subquery_is_traced_before_its_consumer(client, workspace):
    trace = run(client, workspace, "SELECT e.id FROM (SELECT id FROM employees WHERE salary>90000) e ORDER BY e.id")
    nested = [step for step in trace["steps"] if step["depth"] == 1]
    assert any(step["kind"] == "WHERE" for step in nested)
    assert trace["steps"][0]["depth"] == 1
    assert trace["steps"][-1]["data"]["rows"] == [[1], [2], [3], [4]]


def test_current_timestamp_is_consistent_across_snapshots(client, workspace):
    trace = run(client, workspace, "SELECT CURRENT_TIMESTAMP(6) AS current_time_value")
    assert len(trace["steps"]) >= 2
    assert trace["steps"][0]["data"]["rows"] == trace["steps"][-1]["data"]["rows"]


def test_recursive_iterations_are_actual_frontiers(client, workspace):
    trace = run(client, workspace, EXAMPLES[3]["sql"])
    iterations = [step for step in trace["steps"] if step["kind"] == "RECURSION"]
    assert [step["data"]["rows"] for step in iterations] == [[[1]], [[2]], [[3]], [[4]], [[5]], [[6]]]
    distinct = run(client, workspace, "WITH RECURSIVE n(x) AS (SELECT 1 UNION DISTINCT SELECT x+1 FROM n WHERE x<3) SELECT * FROM n")
    assert distinct["steps"][-1]["data"]["rows"] == [[1], [2], [3]]
    cte = next(step for step in distinct["steps"] if step["kind"] == "CTE")
    assert cte["details"] and not any(step["kind"] == "RECURSION" for step in distinct["steps"])


@pytest.mark.parametrize("operation,expected", [("UNION ALL", 12), ("UNION", 6), ("INTERSECT", 6), ("EXCEPT", 0)])
def test_set_operations(client, workspace, operation, expected):
    trace = run(client, workspace, f"SELECT id FROM employees {operation} SELECT id FROM employees")
    assert len(trace["steps"][-1]["data"]["rows"]) == expected


@pytest.mark.parametrize("sql", [
    "INSERT INTO employees VALUES(7,'Test',1,1,1)",
    "UPDATE employees SET salary=0 WHERE id=2",
    "DELETE FROM employees WHERE id=2",
    "INSERT INTO departments SELECT id+10,name FROM departments",
])
def test_mutations_preview_rollback_then_apply(client, workspace, sql):
    before = client.get(f"/api/workspaces/{workspace}/tables").json()
    trace = run(client, workspace, sql)
    assert trace["mutation"] and trace["affected_rows"] > 0
    assert client.get(f"/api/workspaces/{workspace}/tables").json() == before
    applied = client.post(f"/api/workspaces/{workspace}/apply", json={"sql": sql})
    assert applied.status_code == 200, applied.text
    assert applied.json()["tables"] != before


def test_mutation_preview_explains_foreign_key_cascades(client, workspace):
    setup = client.post(f"/api/workspaces/{workspace}/setup", json={"sql": """
    CREATE TABLE parent_row(id INT PRIMARY KEY);
    INSERT INTO parent_row VALUES(1);
    CREATE TABLE child_row(id INT PRIMARY KEY, parent_id INT,
      FOREIGN KEY(parent_id) REFERENCES parent_row(id) ON DELETE CASCADE);
    INSERT INTO child_row VALUES(1,1);
    """})
    assert setup.status_code == 200, setup.text
    trace = run(client, workspace, "DELETE FROM parent_row WHERE id=1")
    related = next(step for step in trace["steps"] if step["title"] == "Related table after: child_row (preview)")
    assert related["data"]["rows"] == []
    assert run(client, workspace, "SELECT * FROM child_row")["steps"][-1]["data"]["rows"] == [[1, 1]]


def test_csv_types_quotes_empty_cells_and_empty_result(client, workspace):
    response = client.post(f"/api/workspaces/{workspace}/csv?table=uploaded", files={"file": ("sample.csv", b'id,name,amount\n0,"O\'Brien, Jr.",12.50\n1,,0.00\n', "text/csv")})
    assert response.status_code == 200, response.text
    trace = run(client, workspace, "SELECT id,name,amount FROM uploaded ORDER BY id")
    assert trace["steps"][-1]["data"]["rows"][0][:2] == [0, "O'Brien, Jr."]
    assert trace["steps"][-1]["data"]["rows"][1][1] is None
    empty = run(client, workspace, "SELECT * FROM uploaded WHERE id<0")
    assert empty["steps"][-1]["data"]["rows"] == []


def test_mysql_time_values_keep_sign_hours_and_precision(client, workspace):
    trace = run(client, workspace, "SELECT CAST('-125:34:56.123456' AS TIME(6)) AS duration")
    assert trace["steps"][-1]["data"]["rows"] == [["-125:34:56.123456"]]


def test_snapshot_cap_is_explicit_and_never_enlarges_limit(client, workspace):
    sql = "SELECT a.id,b.id FROM employees a CROSS JOIN employees b"
    with workspace_connection(workspace) as connection:
        from sqlviz import database
        original = database.MAX_ROWS
        database.MAX_ROWS = 5
        try:
            trace = make_trace(connection, sql)
            assert trace.steps[-1].data.truncated
            limited = make_trace(connection, sql + " LIMIT 2")
            assert len(limited.steps[-1].data.rows) == 2
            assert not limited.steps[-1].data.truncated
        finally:
            database.MAX_ROWS = original


@pytest.mark.parametrize("sql", [
    "SELECT * FROM mysql.user", "SELECT SLEEP(5)", "SELECT RAND()",
    "SELECT * INTO OUTFILE '/tmp/leak' FROM employees",
    "SELECT @x:=1", "DROP DATABASE visualizer", "SELECT 1; SELECT 2",
])
def test_unsafe_or_nonrepeatable_queries_rejected(client, workspace, sql):
    response = client.post(f"/api/workspaces/{workspace}/trace", json={"sql": sql})
    assert response.status_code == 422, response.text


def test_setup_preflight_and_ctas(client, workspace):
    rejected = client.post(f"/api/workspaces/{workspace}/setup", json={"sql": "CREATE TABLE should_not_exist(id INT); DROP DATABASE visualizer"})
    assert rejected.status_code == 422
    assert "should_not_exist" not in [table["name"] for table in client.get(f"/api/workspaces/{workspace}/tables").json()]
    created = client.post(f"/api/workspaces/{workspace}/setup", json={"sql": "CREATE TABLE copied AS SELECT id,name FROM employees"})
    assert created.status_code == 200, created.text
    assert len(run(client, workspace, "SELECT * FROM copied")["steps"][-1]["data"]["rows"]) == 6
    invalid_engine = client.post(f"/api/workspaces/{workspace}/setup", json={"sql": "CREATE TABLE bad(id INT) ENGINE=MyISAM"})
    assert invalid_engine.status_code == 422


def test_analyze_is_actual_and_never_runs_for_mutations(client, workspace):
    trace = run(client, workspace, "SELECT * FROM employees WHERE id=1", analyze=True)
    assert "actual" in trace["analyze"].lower()
    response = client.post(f"/api/workspaces/{workspace}/trace", json={"sql": "DELETE FROM employees", "analyze": True})
    assert response.status_code == 422


def test_read_only_external_import(client, workspace):
    from sqlviz.database import schema
    body = dict(
        host=os.environ["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")),
        database=schema(workspace), user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""), query="SELECT id,name FROM employees LIMIT 2", table="external_copy",
    )
    response = client.post(f"/api/workspaces/{workspace}/import", json=body)
    assert response.status_code == 200, response.text
    assert next(table for table in response.json() if table["name"] == "external_copy")["row_count"] == 2
    body["query"] = "DELETE FROM employees"
    assert client.post(f"/api/workspaces/{workspace}/import", json=body).status_code == 422


def test_ai_outage_is_explicit_and_does_not_break_trace(client, workspace, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setenv("AI_PROVIDER", "ollama")
    trace = run(client, workspace, "SELECT 1")
    monkeypatch.setattr(httpx.Client, "post", unavailable)
    # TestClient inherits httpx.Client, so use request rather than its patched post method.
    response = client.request("POST", "/api/explain", json={"query": "SELECT 1", "step": trace["steps"][0]})
    assert response.status_code == 503
    assert "Deterministic explanations remain available" in response.json()["detail"]


def test_ai_uses_verified_trace_context_and_requested_audience(client, workspace, monkeypatch):
    captured = {}
    def available(_client, url, **kwargs):
        captured.update(kwargs["json"])
        return httpx.Response(200, json={"message": {"content": "The filter keeps true rows."}}, request=httpx.Request("POST", url))
    monkeypatch.setenv("AI_PROVIDER", "ollama")
    trace = run(client, workspace, "SELECT * FROM employees")
    monkeypatch.setattr(httpx.Client, "post", available)
    response = client.request("POST", "/api/explain", json={"query": trace["query"], "step": trace["steps"][0], "audience": "advanced"})
    assert response.status_code == 200
    assert response.json()["explanation"] == "The filter keeps true rows."
    assert "advanced" in captured["messages"][0]["content"]
    assert "employees" in captured["messages"][1]["content"]


def test_ai_disabled_by_default(client, workspace, monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    trace = run(client, workspace, "SELECT 1")
    response = client.request("POST", "/api/explain", json={"query": "SELECT 1", "step": trace["steps"][0]})
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_ai_openai_compatible_provider(client, workspace, monkeypatch):
    captured = {}
    def available(_client, url, **kwargs):
        captured.update(url=url, headers=kwargs["headers"], body=kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Rows are grouped."}}]}, request=httpx.Request("POST", url))
    monkeypatch.setenv("AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("AI_BASE_URL", "http://llm.internal:8000/v1/")
    monkeypatch.setenv("AI_MODEL", "llama3")
    monkeypatch.setenv("AI_API_KEY", "test-key")
    trace = run(client, workspace, "SELECT 1")
    monkeypatch.setattr(httpx.Client, "post", available)
    response = client.request("POST", "/api/explain", json={"query": "SELECT 1", "step": trace["steps"][0]})
    assert response.status_code == 200, response.text
    assert response.json()["explanation"] == "Rows are grouped."
    assert captured["url"] == "http://llm.internal:8000/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer test-key"}
    assert captured["body"]["model"] == "llama3"


def test_invalid_configuration_is_reported(monkeypatch):
    from sqlviz import settings
    monkeypatch.setenv("AI_PROVIDER", "cloud")
    with pytest.raises(settings.ConfigError):
        settings.ai()
    monkeypatch.setenv("SQLVIZ_MAX_ROWS", "lots")
    with pytest.raises(settings.ConfigError):
        settings.limits()


def test_cross_origin_requests_rejected(client):
    response = client.post("/api/workspaces", headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
