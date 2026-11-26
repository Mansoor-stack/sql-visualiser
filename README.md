# SQL Visualiser

Step-by-step visualiser for **MySQL** query execution. Write a query with CTEs,
joins, window functions, subqueries or set operations and watch it run in
**logical execution order** (FROM → JOIN → WHERE → GROUP BY → HAVING → WINDOW →
SELECT → DISTINCT → ORDER BY → LIMIT), with the intermediate table at every step,
a plain-language explanation next to it, and MySQL's physical `EXPLAIN` plan.

Bring your own data: upload CSV files (one table per file), paste setup SQL, or
import a SELECT result from an existing MySQL server.

> **Status: 0.1 preview.** The backend is tested against MySQL 8.4. The web UI is
> built by CI; please report rendering problems as issues.

- [Quick start](#quick-start)
- [Connect to MySQL](#connect-to-mysql)
- [Configuration](#configuration)
- [AI explanations (optional)](#ai-explanations-optional)
- [Company networks (proxies, mirrors, certificates)](#company-networks-proxies-mirrors-certificates)
- [Security model](#security-model)
- [Development](#development) · [Releasing](#releasing)

## Quick start

You need **MySQL 8.0+** (8.4 recommended) and either **Python 3.12+** or
**Docker/Podman**. Pick one option.

### Option 1 - pip / pipx (uses your own MySQL)

```sh
pipx install sql-visualiser          # or: pip install sql-visualiser
export MYSQL_HOST=127.0.0.1 MYSQL_USER=visualizer MYSQL_PASSWORD='<your password>'
sql-visualiser                       # open http://localhost:8080
```

The package already contains the built web UI; Node.js is **not** required.
Run `sql-visualiser --help` for `--host`/`--port`.

### Option 2 - Docker Compose (app + MySQL together)

```sh
git clone https://github.com/Mansoor-stack/sql-visualiser.git && cd sql-visualiser
export MYSQL_ROOT_PASSWORD="$(openssl rand -hex 24)"
export MYSQL_PASSWORD="$(openssl rand -hex 24)"
docker compose up -d                 # pulls ghcr.io/mansoor-stack/sql-visualiser
```

Open http://localhost:8080. Save the generated passwords: changing them later
does not change accounts in the existing MySQL volume. Only the app is
published, on `127.0.0.1`; MySQL is not reachable from the host. Use
`docker compose up -d --build` to build the image from source instead.

### Option 3 - Docker image with your own MySQL

```sh
docker run --rm -p 127.0.0.1:8080:8080 \
  -e MYSQL_HOST=host.docker.internal -e MYSQL_USER=visualizer -e MYSQL_PASSWORD \
  --add-host host.docker.internal:host-gateway \
  ghcr.io/mansoor-stack/sql-visualiser:latest
```

## Connect to MySQL

The app creates one scratch database per workspace, named `viz_<id>`, and never
touches other databases. Create a dedicated account with access to only those
databases (run as an administrator):

```sql
CREATE USER 'visualizer'@'%' IDENTIFIED BY '<strong password>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX ON `viz\_%`.* TO 'visualizer'@'%';
```

Do **not** point the app account at production data. To learn on real data, use
**Import from MySQL** in the UI with a separate read-only account; imports run in
a read-only transaction, are copied into the workspace, and credentials are not
stored.

## Configuration

All settings are environment variables. Invalid values stop the
`sql-visualiser` command with a clear error.

| Variable | Default | Purpose |
|---|---|---|
| `MYSQL_HOST` | `127.0.0.1` | Scratch MySQL server |
| `MYSQL_PORT` | `3306` | |
| `MYSQL_USER` | `visualizer` | Account with the grants above |
| `MYSQL_PASSWORD` | empty | |
| `MYSQL_SSL_CA` | unset | CA file path; enables TLS to the scratch server |
| `SQLVIZ_IMPORT_SSL_CA` | unset | CA file path; enables TLS for **Import from MySQL** |
| `SQLVIZ_HOST` | `127.0.0.1` | Bind address (same as `--host`) |
| `SQLVIZ_PORT` | `8080` | Port (same as `--port`) |
| `SQLVIZ_MAX_ROWS` | `1000` | Rows per table / per displayed step |
| `SQLVIZ_MAX_TABLES` | `20` | Tables per workspace |
| `SQLVIZ_MAX_STEPS` | `150` | Visualisation steps per query |
| `SQLVIZ_MAX_UPLOAD_BYTES` | `2000000` | CSV upload size |
| `SQLVIZ_QUERY_TIMEOUT_MS` | `2500` | MySQL `MAX_EXECUTION_TIME` per SELECT |
| `AI_PROVIDER` | `none` | `none`, `ollama` or `openai-compatible` |
| `AI_BASE_URL` | per provider | `http://127.0.0.1:11434` (Ollama) or `http://127.0.0.1:8000/v1` |
| `AI_MODEL` | per provider | `qwen2.5:7b` (Ollama) or `local-model` |
| `AI_API_KEY` | unset | Bearer token for `openai-compatible` servers that need one |

With Docker Compose, put these in a `.env` file next to `compose.yaml` (it is
git-ignored) or export them in your shell. Never commit real passwords.

## AI explanations (optional)

Every step always has a deterministic explanation. AI adds a narrated version
tuned to the chosen audience (beginner / intermediate / advanced) and is **off by
default**.

- **Ollama (local, free, open models):**
  `ollama pull qwen2.5:7b`, then `AI_PROVIDER=ollama`. In Docker use
  `AI_BASE_URL=http://host.docker.internal:11434`.
- **OpenAI-compatible server** (vLLM, LM Studio, llama.cpp server, LocalAI or an
  approved internal gateway): `AI_PROVIDER=openai-compatible`,
  `AI_BASE_URL=http://<server>/v1`, `AI_MODEL=<model>`, optional `AI_API_KEY`.

**Data sent to the AI server:** the SQL text, the step's deterministic
explanation, column names and up to 8 rows of that step. Use only an AI server
you are allowed to send that data to. Ollama requests ignore proxy settings;
OpenAI-compatible requests honour `HTTPS_PROXY`/`NO_PROXY`. If the AI server is
down, the UI shows a notice and keeps the deterministic explanation.

## Company networks (proxies, mirrors, certificates)

Nothing is fetched from the internet at runtime; the UI has no CDN dependencies.
Only installation needs package downloads.

**pip/pipx** - use your mirror and corporate CA:

```sh
pip install --index-url https://<your-mirror>/simple --cert /path/to/company-ca.pem sql-visualiser
```

**Pre-built image** - if `ghcr.io` is blocked, mirror the image to your internal
registry and set `SQLVIZ_IMAGE=<registry>/sql-visualiser:<version>` for Compose.

**Building the image from source** - all download sources are build arguments,
and the CA certificate is passed as a build secret (never stored in the image):

```sh
docker build --target runtime -t sql-visualiser \
  --build-arg NODE_IMAGE=<registry>/node:22-alpine \
  --build-arg PYTHON_IMAGE=<registry>/python:3.12-slim \
  --build-arg NPM_REGISTRY=https://<your-npm-mirror>/ \
  --build-arg PIP_INDEX_URL=https://<your-pypi-mirror>/simple \
  --secret id=ca_cert,src=/path/to/company-ca.pem .
```

Set `MYSQL_IMAGE` for Compose if Docker Hub is blocked too.

## Execution model

The logical player runs explanatory intermediate queries against **real MySQL**.
It does not split SQL on whitespace or pretend SQL runs in written line order.
CTE dependencies and independently evaluable derived queries precede their
consumers. Correlated predicates stay in their parent-row context.

The physical view receives `EXPLAIN FORMAT=JSON` from MySQL. Costs and row counts
are estimates. Opting into `EXPLAIN ANALYZE FORMAT=TREE` actually executes a
read-only query and returns runtime metrics; it is disabled for mutations.
Neither view is a complete debugger trace of MySQL's internal row operations.

## What is supported

- CSV uploads with quoted fields, numeric inference, NULL empty cells, and
  multiple destination tables.
- `CREATE TABLE`, `INSERT`, and `CREATE TABLE AS SELECT` setup scripts.
- Importing a SELECT result from an existing MySQL connection in a read-only
  transaction. Credentials are not persisted.
- Inner/outer joins, three-valued WHERE predicates, grouping, aggregate aliases,
  HAVING, projection, DISTINCT, ordering, LIMIT/OFFSET.
- Chained CTEs; recursive CTE results and verified depth snapshots for simple
  `UNION ALL` recursive forms.
- Window expressions, including ranking and running totals with explicit
  frames; grouped aggregates feeding windows.
- Derived queries, correlated EXISTS predicates and scalar subqueries evaluated
  in their applicable parent context.
- UNION/UNION ALL, INTERSECT and EXCEPT.
- Single-target INSERT/UPDATE/DELETE previews, including changes to related
  tables through foreign-key cascades. Previews roll back; a separate Apply
  endpoint commits.
- Optional AI narration of verified snapshots (Ollama or any OpenAI-compatible server). AI failure produces an
  explicit notice and leaves deterministic explanations available.

## Limits

- Target: small teaching datasets. Defaults: at most **20 tables and 1,000 rows
  per table**, 2 MB CSV uploads, 30 setup statements (limits are configurable,
  see [Configuration](#configuration)).
- Intermediate/final result previews show at most 1,000 rows. `truncated: true`
  explicitly means more rows exist. This is not a complete result export.
- Up to 150 stages and a 20-second logical trace budget; individual SELECTs have
  a 2.5-second (default) MySQL execution limit. These are not hard resource guarantees for
  arbitrary DDL/DML or hostile SQL.
- Recursive evaluation is capped at 100 levels. Iteration visualization is
  restricted to simple anchor/member `UNION ALL` queries without `SELECT *`,
  DISTINCT or LIMIT, and is used only after its complete row multiset matches
  the authoritative CTE result. Other valid forms retain result/plan access
  with an explicit iteration-detail limitation.
- Multi-table UPDATE/DELETE, stored routines, administrative SQL, session/user
  variables, locking clauses, file output, and nonrepeatable/stateful functions
  such as RAND, UUID, SLEEP and ROW_COUNT are not supported.
- Not every valid MySQL construct has a decomposable intermediate form. Such a
  form returns an explicit tracing error rather than invented snapshots.
- **MySQL DDL implicitly commits.** Setup scripts are not atomic. After setup
  failure, newly created tables are removed, but earlier changes to existing
  tables may already have committed.
- Row order without ORDER BY is unspecified. Row-version comparisons use
  multisets and preserve duplicates, not fabricated row identities.
- This is a trusted **local learning tool**, not a production multi-tenant SQL
  sandbox. There is no user authentication. Keep the service bound to loopback;
  use SELECT-only source accounts and trusted networks for external imports.

## Security model

SQL Visualiser is a **single-user local tool**, not a shared multi-tenant SQL
sandbox. It has no login. By default it listens on `127.0.0.1`, accepts only
`localhost` Host headers, and rejects cross-origin browser requests. Do not
expose it on a shared network or the internet. Running a shared team instance
needs authentication and per-user isolation, which is not implemented yet.

## Development

Requirements: Python 3.12+, Node.js 22+, and a disposable MySQL 8.4 instance.

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# backend (terminal 1)
export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 MYSQL_USER=root MYSQL_PASSWORD=
sql-visualiser

# frontend with hot reload (terminal 2) - proxies /api to :8080
cd web && npm install && npm run dev

# production UI build, written into backend/sqlviz/static
npm --prefix web run build

# tests (creates and drops only its own viz_* databases)
pytest -q
```

A throwaway MySQL for development:
`docker run -d --rm --name sqlviz-db -e MYSQL_ALLOW_EMPTY_PASSWORD=yes -p 127.0.0.1:3306:3306 mysql:8.4`.

API docs: http://localhost:8080/docs. Please commit `web/package-lock.json` after
changing frontend dependencies so builds are reproducible.

Layout: [backend/sqlviz](./backend/sqlviz) (FastAPI, SQLGlot, PyMySQL),
[web](./web) (React + TypeScript + Vite), [tests](./tests),
[.github/workflows](./.github/workflows).

## Releasing

1. Bump `version` in `pyproject.toml` and `backend/sqlviz/__init__.py`.
2. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`.
3. The release workflow builds the UI, publishes the wheel to PyPI (trusted
   publishing, environment `pypi`), creates a GitHub release, and pushes a
   multi-arch image (amd64/arm64) to `ghcr.io/<owner>/sql-visualiser`.

One-time setup: on PyPI add a *pending trusted publisher* for project
`sql-visualiser`, workflow `release.yml`, environment `pypi`; on GitHub create the
`pypi` environment; after the first image push, make the GHCR package public.

## License

[MIT](./LICENSE). Inspired by
[yashkantharia/sql-visualiser](https://github.com/yashkantharia/sql-visualiser).
