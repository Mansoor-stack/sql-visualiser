import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Cell = string | number | boolean | null;
type Snapshot = { columns: string[]; rows: Cell[][]; truncated: boolean };
type Table = { name: string; row_count: number; columns: { name: string; type: string; key: string }[]; data: Snapshot };
type Step = { id: number; kind: string; title: string; explanation: string; sql: string; data: Snapshot; depth: number; details: string[]; source_start: number | null; source_end: number | null };
type Trace = { query: string; steps: Step[]; plan: Record<string, unknown>; analyze: string | null; mutation: boolean; affected_rows: number | null; notices: string[] };
type Examples = { dataset: string; queries: { name: string; sql: string }[] };
type Audience = "beginner" | "intermediate" | "advanced";
type Health = { mysql: string; ai_provider: "none" | "ollama" | "openai-compatible"; ai_model: string | null; row_limit: number };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data));
  }
  return data;
}

const post = (body: object): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const display = (cell: Cell) => cell === null ? "NULL" : String(cell);

function DataTable({ data, previous, focus = -1 }: { data: Snapshot; previous?: Snapshot; focus?: number }) {
  const [page, setPage] = useState(0);
  useEffect(() => setPage(focus < 0 ? 0 : Math.floor(focus / 25)), [data, focus]);
  const comparable = previous && !previous.truncated && !data.truncated
    && JSON.stringify(previous.columns) === JSON.stringify(data.columns);
  const counts = new Map<string, number>();
  if (comparable) previous.rows.forEach(row => counts.set(JSON.stringify(row), (counts.get(JSON.stringify(row)) ?? 0) + 1));
  const added = data.rows.map(row => {
    const key = JSON.stringify(row), count = counts.get(key) ?? 0;
    if (count) counts.set(key, count - 1);
    return comparable && count === 0;
  });
  const removed = comparable ? [...counts.values()].reduce((sum, count) => sum + count, 0) : 0;
  const pages = Math.max(1, Math.ceil(data.rows.length / 25));
  return <div className="table-section">
    {data.truncated && <div className="notice warning">Preview capped at 1,000 rows; more rows exist. This is not the complete result.</div>}
    {comparable && <div className="diff-key"><span className="dot green" /> New row versions highlighted · {removed} removed row versions · duplicate counts preserved</div>}
    <div className="table-scroll"><table>
      <thead><tr><th className="row-number">#</th>{data.columns.map((column, i) => <th key={i}>{column}</th>)}</tr></thead>
      <tbody>{data.rows.slice(page * 25, page * 25 + 25).map((row, i) => {
        const index = page * 25 + i;
        return <tr key={index} className={`${added[index] ? "added" : ""} ${focus === index ? "focused" : ""}`}>
          <td className="row-number">{index + 1}</td>
          {row.map((cell, j) => <td key={j} className={cell === null ? "null" : ""} title={display(cell)}>{display(cell)}</td>)}
        </tr>;
      })}</tbody>
    </table>{!data.rows.length && <div className="empty small">No rows. An empty result is still a valid result.</div>}</div>
    <div className="table-footer"><span>{data.rows.length}{data.truncated ? "+" : ""} rows · {data.columns.length} columns</span>
      <div><button disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="Previous result page">‹</button>
        <span> {page + 1} / {pages} </span>
        <button disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="Next result page">›</button></div>
    </div>
  </div>;
}

function SQLText({ value }: { value: string }) {
  const pattern = /(--[^\n]*|'(?:''|\\.|[^'])*'|`[^`]*`|\b(?:SELECT|FROM|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|ON|WHERE|AND|OR|NOT|NULL|AS|WITH|RECURSIVE|UNION|ALL|INTERSECT|EXCEPT|GROUP|BY|HAVING|ORDER|ASC|DESC|LIMIT|OFFSET|DISTINCT|OVER|PARTITION|ROWS|RANGE|BETWEEN|UNBOUNDED|PRECEDING|CURRENT|ROW|FOLLOWING|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|EXISTS|CASE|WHEN|THEN|ELSE|END)\b|\b\d+(?:\.\d+)?\b)/gi;
  return <>{value.split(pattern).map((token, i) => {
    const cls = token.startsWith("--") ? "comment" : token.startsWith("'") ? "string" : /^\d/.test(token) ? "number" : /^(SELECT|FROM|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|ON|WHERE|AND|OR|NOT|NULL|AS|WITH|RECURSIVE|UNION|ALL|INTERSECT|EXCEPT|GROUP|BY|HAVING|ORDER|ASC|DESC|LIMIT|OFFSET|DISTINCT|OVER|PARTITION|ROWS|RANGE|BETWEEN|UNBOUNDED|PRECEDING|CURRENT|ROW|FOLLOWING|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|EXISTS|CASE|WHEN|THEN|ELSE|END)$/i.test(token) ? "keyword" : "";
    return <span key={i} className={cls}>{token}</span>;
  })}</>;
}

function Editor({ value, onChange, active, readOnly }: { value: string; onChange: (value: string) => void; active?: Step; readOnly: boolean }) {
  const mirror = useRef<HTMLPreElement>(null);
  let offset = 0;
  return <div className="editor">
    <pre ref={mirror} aria-hidden="true">{value.split("\n").map((line, i) => {
      const start = offset;
      offset += line.length + 1;
      const highlighted = active?.source_start != null && active.source_end != null && start < active.source_end && offset > active.source_start;
      return <div key={i} className={`code-line ${highlighted ? "active-line" : ""}`}><span className="line-number">{i + 1}</span><SQLText value={line || " "} /></div>;
    })}<div className="code-line">{" "}</div></pre>
    <textarea aria-label="SQL editor" value={value} readOnly={readOnly} spellCheck={false}
      onChange={event => onChange(event.target.value)}
      onScroll={event => { if (mirror.current) { mirror.current.scrollTop = event.currentTarget.scrollTop; mirror.current.scrollLeft = event.currentTarget.scrollLeft; } }}
      onKeyDown={event => {
        if (event.key === "Tab" && !readOnly) {
          event.preventDefault();
          const element = event.currentTarget, start = element.selectionStart, end = element.selectionEnd;
          onChange(value.slice(0, start) + "  " + value.slice(end));
          requestAnimationFrame(() => { element.selectionStart = element.selectionEnd = start + 2; });
        }
      }} />
  </div>;
}

function PlanNode({ name, value, depth = 0 }: { name: string; value: unknown; depth?: number }) {
  if (value === null || typeof value !== "object") return <div className="plan-value"><span>{name}</span><code>{String(value)}</code></div>;
  return <details className="plan-node" open={depth < 3}>
    <summary>{name.replaceAll("_", " ")}</summary>
    {Object.entries(value).map(([key, child]) => <PlanNode key={key} name={Array.isArray(value) ? `operator ${Number(key) + 1}` : key} value={child} depth={depth + 1} />)}
  </details>;
}

function ExternalForm({ busy, onImport }: { busy: boolean; onImport: (body: object) => Promise<void> }) {
  const [fields, setFields] = useState({ host: "host.docker.internal", port: "3306", database: "", user: "", password: "", table: "imported_data", query: "SELECT * FROM your_table LIMIT 1000" });
  return <form className="external-form" onSubmit={event => {
    event.preventDefault();
    void onImport({ ...fields, port: Number(fields.port) }).finally(() => setFields(current => ({ ...current, password: "" })));
  }}>
    <div className="notice">Uses a read-only transaction. Use a SELECT-only source account. Credentials are not saved. Inside Docker, localhost means the app container.</div>
    <div className="form-grid">{(["host", "port", "database", "user", "password", "table"] as const).map(key => <label key={key}>{key === "table" ? "Destination table" : key}
      <input required={key !== "password"} disabled={busy} type={key === "password" ? "password" : key === "port" ? "number" : "text"} autoComplete={key === "password" ? "off" : undefined} value={fields[key]} onChange={event => setFields({ ...fields, [key]: event.target.value })} />
    </label>)}</div>
    <label>Source SELECT<textarea required disabled={busy} value={fields.query} onChange={event => setFields({ ...fields, query: event.target.value })} rows={5} /></label>
    <button className="primary" disabled={busy}>Import query result</button>
  </form>;
}

function App() {
  const [workspace, setWorkspace] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [examples, setExamples] = useState<Examples | null>(null);
  const [tables, setTables] = useState<Table[]>([]);
  const [query, setQuery] = useState("SELECT * FROM employees;");
  const [setupSQL, setSetupSQL] = useState("");
  const [tab, setTab] = useState<"query" | "setup" | "import">("query");
  const [view, setView] = useState<"logical" | "plan" | "result">("logical");
  const [trace, setTrace] = useState<Trace | null>(null);
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [audience, setAudience] = useState<Audience>("beginner");
  const [focus, setFocus] = useState(-1);
  const [analyze, setAnalyze] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [aiText, setAIText] = useState("");
  const [aiError, setAIError] = useState("");
  const [aiBusy, setAIBusy] = useState(false);
  const aiController = useRef<AbortController | null>(null);
  const step = trace?.steps[index];
  const currentPath = `/workspaces/${workspace}`;

  function invalidate() {
    setTrace(null); setPlaying(false); setIndex(0); setFocus(-1);
    setAIText(""); setAIError(""); aiController.current?.abort();
  }

  useEffect(() => {
    void (async () => {
      try {
        const [info, samples] = await Promise.all([api<Health>("/health"), api<Examples>("/examples")]);
        setHealth(info); setExamples(samples); setSetupSQL(samples.dataset);
        setQuery(samples.queries[0].sql);
        const saved = localStorage.getItem("sqlviz-workspace");
        const id = saved || (await api<{ id: string }>("/workspaces", { method: "POST" })).id;
        setWorkspace(id); localStorage.setItem("sqlviz-workspace", id);
        setTables(await api<Table[]>(`/workspaces/${id}/tables`));
      } catch (failure) { setError(message(failure)); }
      finally { setBusy(false); }
    })();
    return () => aiController.current?.abort();
  }, []);

  useEffect(() => {
    aiController.current?.abort(); setAIText(""); setAIError(""); setAIBusy(false); setFocus(-1);
  }, [index, trace]);

  useEffect(() => {
    if (!playing || !trace) return;
    if (index >= trace.steps.length - 1) { setPlaying(false); return; }
    const timer = window.setTimeout(() => setIndex(current => current + 1), 1300 / speed);
    return () => window.clearTimeout(timer);
  }, [playing, index, speed, trace]);

  async function action(operation: () => Promise<void>) {
    setBusy(true); setError(""); setStatus(""); setPlaying(false);
    try { await operation(); }
    catch (failure) { setError(message(failure)); }
    finally { setBusy(false); }
  }

  async function reset() {
    if (workspace && !window.confirm("Delete this isolated dataset and start a new one?")) return;
    await action(async () => {
      if (workspace) await api(currentPath, { method: "DELETE" });
      const { id } = await api<{ id: string }>("/workspaces", { method: "POST" });
      setWorkspace(id); localStorage.setItem("sqlviz-workspace", id);
      setTables([]); invalidate(); setStatus("New isolated workspace ready.");
    });
  }

  async function loadSample() {
    if (!examples) return;
    await action(async () => {
      if (tables.length) throw new Error("Reset the workspace before loading the sample dataset.");
      setTables(await api<Table[]>(`${currentPath}/setup`, post({ sql: examples.dataset })));
      invalidate(); setStatus("Sample dataset loaded. Choose an example or write your own query.");
    });
  }

  async function run() {
    await action(async () => {
      invalidate();
      const result = await api<Trace>(`${currentPath}/trace`, post({ sql: query, analyze }));
      setTrace(result); setIndex(0); setView("logical");
    });
  }

  async function enhance() {
    if (!trace || !step) return;
    const controller = new AbortController();
    aiController.current?.abort(); aiController.current = controller;
    setAIBusy(true); setAIText(""); setAIError("");
    try {
      const result = await api<{ explanation: string }>("/explain", { ...post({ query: trace.query, step, audience }), signal: controller.signal });
      if (!controller.signal.aborted) setAIText(result.explanation);
    } catch (failure) {
      if (!controller.signal.aborted) setAIError(message(failure));
    } finally { if (!controller.signal.aborted) setAIBusy(false); }
  }

  return <div className="app">
    <header className="topbar"><a href="/" className="brand"><span className="brand-icon">Q</span> SQL<span className="brand-light">visualiser</span></a>
      <div className="header-middle"><span className="dot green" /> {health ? `MySQL ${health.mysql.split("-")[0]}` : "Connecting to MySQL"} <span className="divider">/</span> Local execution lab</div>
      <button disabled={busy} onClick={() => void reset()}>{workspace ? "Reset workspace" : "Create workspace"}</button>
    </header>
    <div className="intro"><div><div className="eyebrow">WRITE IT. STEP THROUGH IT. UNDERSTAND IT.</div><h1>Make SQL <span>make sense.</span></h1>
      <p>Follow the data, not the written order. Real MySQL results at every logical stage.</p></div>
      <div className="workspace-badge"><span className="dot purple" /> Isolated workspace <code>{workspace ? workspace.slice(0, 8) : "—"}</code></div>
    </div>
    {error && <div className="notice error" role="alert">{error}<button onClick={() => setError("")} aria-label="Dismiss error">×</button></div>}
    {status && <div className="notice success" role="status">{status}</div>}
    <main className="workbench">
      <section className="input-column">
        <div className="panel query-panel">
          <div className="tabs">{(["query", "setup", "import"] as const).map(item => <button key={item} className={tab === item ? "selected" : ""} onClick={() => setTab(item)}>{item === "query" ? "SQL query" : item === "setup" ? "Dataset SQL" : "Import MySQL"}</button>)}</div>
          {tab === "import" ? <ExternalForm busy={busy || !workspace} onImport={body => action(async () => {
            setTables(await api<Table[]>(`${currentPath}/import`, post(body))); invalidate(); setStatus("Query result imported into the isolated workspace.");
          })} /> : <>
            <div className="editor-toolbar"><span className="file-label">{tab === "query" ? "query.sql" : "dataset.sql"}</span>
              {tab === "query" && <select aria-label="Example queries" disabled={!examples || busy} value="" onChange={event => {
                const sample = examples?.queries[Number(event.target.value)]; if (sample) { setQuery(sample.sql); invalidate(); }
              }}><option value="" disabled>Example queries</option>{examples?.queries.map((sample, i) => <option key={i} value={i}>{sample.name}</option>)}</select>}
            </div>
            <Editor value={tab === "query" ? query : setupSQL} readOnly={busy} active={tab === "query" ? step : undefined} onChange={value => {
              if (tab === "query") { setQuery(value); invalidate(); } else setSetupSQL(value);
            }} />
            <div className="editor-bottom">
              {tab === "query" ? <><label className="checkbox"><input type="checkbox" checked={analyze} disabled={busy} onChange={event => setAnalyze(event.target.checked)} /> EXPLAIN ANALYZE <span title="Actually executes a read-only query to collect runtime plan metrics. Mutations are excluded.">ⓘ</span></label>
                <button className="primary" disabled={busy || !workspace || !query.trim()} onClick={() => void run()}>{busy ? "Working…" : "▶ Visualise query"}</button></>
                : <><span className="muted">CREATE TABLE · INSERT · CTAS</span><button className="primary" disabled={busy || !workspace} onClick={() => void action(async () => {
                  try { setTables(await api<Table[]>(`${currentPath}/setup`, post({ sql: setupSQL }))); invalidate(); setStatus("Dataset SQL executed."); }
                  finally { setTables(await api<Table[]>(`${currentPath}/tables`)); invalidate(); }
                })}>Run dataset SQL</button></>}
            </div>
            {tab === "setup" && <div className="panel-note">Setup modifies only this workspace. MySQL DDL auto-commits; do not assume an entire setup script can be rolled back.</div>}
          </>}
        </div>
        <section className="panel dataset-panel">
          <div className="panel-heading"><h2><span className="section-icon">▤</span> Your dataset <span className="count">{tables.length}</span></h2>
            <label className={`button small-button ${busy || !workspace ? "disabled" : ""}`}>+ CSV files<input type="file" accept=".csv,text/csv" multiple hidden disabled={busy || !workspace} onChange={event => {
              const files = Array.from(event.target.files ?? []); event.target.value = "";
              void action(async () => {
                invalidate();
                for (const file of files) {
                  const form = new FormData(); form.append("file", file);
                  const name = file.name.replace(/\.csv$/i, "").replace(/[^a-zA-Z0-9_]/g, "_");
                  setTables(await api<Table[]>(`${currentPath}/csv?table=${encodeURIComponent(name)}`, { method: "POST", body: form }));
                }
                setStatus("CSV import completed. Empty cells became NULL; earlier successful files remain if a later file fails.");
              });
            }} /></label>
          </div>
          {!tables.length ? <div className="empty"><div className="empty-symbol">▤</div><h3>Start with some data</h3><p>Upload CSVs, run setup SQL, or explore a ready-made dataset.</p><button disabled={busy || !workspace || !examples} onClick={() => void loadSample()}>Load sample dataset</button></div>
            : <div className="dataset-list">{tables.map(table => <details className="dataset" key={table.name}><summary><span>▤ <strong>{table.name}</strong></span><span>{table.row_count} rows</span></summary>
              <div className="column-tags">{table.columns.map(column => <span key={column.name} title={`${column.type}${column.key ? ` · ${column.key}` : ""}`}>{column.key === "PRI" ? "⚿ " : ""}{column.name}<small>{column.type}</small></span>)}</div><DataTable data={table.data} />
            </details>)}</div>}
          <div className="panel-note">Up to 20 tables · 1,000 rows per table · CSVs up to 2 MB · all execution stays in MySQL</div>
        </section>
      </section>
      <section className="panel output-panel">
        <div className="output-header"><div className="tabs">{(["logical", "plan", "result"] as const).map(item => <button key={item} onClick={() => setView(item)} className={view === item ? "selected" : ""}>{item === "logical" ? "Logical flow" : item === "plan" ? "Physical plan" : "Final result"}</button>)}</div>
          <select aria-label="Explanation level" value={audience} onChange={event => { setAudience(event.target.value as Audience); setAIText(""); }}><option value="beginner">Beginner</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select>
        </div>
        {!trace ? <div className="empty output-empty"><div className="flow-preview"><span>FROM</span><i>→</i><span>WHERE</span><i>→</i><span>RESULT</span></div><h2>A query has a story.</h2><p>Load a dataset and visualise a query to see its intermediate tables, explanations, and actual MySQL plan.</p><div className="empty-features"><span>CTEs & recursion</span><span>Joins & subqueries</span><span>Windows & groups</span><span>Mutation previews</span></div></div>
          : view === "plan" ? <div className="plan-view"><div className="notice">Optimizer-selected plan from EXPLAIN FORMAT=JSON. Row counts and costs are estimates unless actual EXPLAIN ANALYZE output is shown below.</div><PlanNode name="MySQL execution plan" value={trace.plan} />
            {trace.analyze && <><h3>Actual execution metrics</h3><pre className="technical">{trace.analyze}</pre></>}
          </div>
            : view === "result" ? <div className="result-view"><div className="stage-heading"><span className="stage-type">{trace.mutation ? "PREVIEW" : "RESULT"}</span><h2>{trace.mutation ? `After mutation · ${trace.affected_rows} affected rows` : "Final query result"}</h2></div><DataTable data={trace.steps[trace.steps.length - 1].data} />
              {trace.mutation && <div className="notice warning">Preview only. No changes were saved.</div>}
            </div>
              : step && <>
                <div className="playback"><div className="playback-buttons">
                  <button disabled={index === 0} onClick={() => { setIndex(0); setPlaying(false); }} aria-label="First step">|‹</button>
                  <button disabled={index === 0} onClick={() => { setIndex(index - 1); setPlaying(false); }} aria-label="Previous step">‹</button>
                  <button className="play" onClick={() => { if (index === trace.steps.length - 1) setIndex(0); setPlaying(!playing); }} aria-label={playing ? "Pause playback" : "Play playback"}>{playing ? "Ⅱ" : "▶"}</button>
                  <button disabled={index === trace.steps.length - 1} onClick={() => { setIndex(index + 1); setPlaying(false); }} aria-label="Next step">›</button>
                  <button disabled={index === trace.steps.length - 1} onClick={() => { setIndex(trace.steps.length - 1); setPlaying(false); }} aria-label="Last step">›|</button>
                </div><span className="step-count">Step <strong>{index + 1}</strong> / {trace.steps.length}</span><select aria-label="Playback speed" value={speed} onChange={event => setSpeed(Number(event.target.value))}>{[0.5, 1, 2, 4].map(value => <option key={value} value={value}>{value}×</option>)}</select></div>
                <input className="scrubber" type="range" min={0} max={trace.steps.length - 1} value={index} aria-label="Execution timeline" onChange={event => { setIndex(Number(event.target.value)); setPlaying(false); }} />
                <div className="stage-list" aria-label="Query stages">{trace.steps.map((item, i) => <button key={i} title={item.title} onClick={() => { setIndex(i); setPlaying(false); }} className={`${i === index ? "current" : ""} ${i < index ? "completed" : ""}`}><span>{i + 1}</span>{item.kind}{item.depth > 0 && <small>↳</small>}</button>)}</div>
                <div className="stage-content"><div className="stage-heading"><span className="stage-type">{step.kind}</span><h2>{step.title}</h2><p>{step.data.rows.length}{step.data.truncated ? "+" : ""} rows at this stage</p></div>
                  {audience !== "beginner" && <div className="row-inspector"><span>Row inspector <small>(not engine scan order)</small></span><button disabled={!step.data.rows.length} onClick={() => setFocus(focus < 0 ? 0 : -1)}>{focus < 0 ? "Inspect rows" : "Show all"}</button>{focus >= 0 && <><button disabled={focus === 0} onClick={() => setFocus(focus - 1)}>‹</button><span>{focus + 1}/{step.data.rows.length}</span><button disabled={focus === step.data.rows.length - 1} onClick={() => setFocus(focus + 1)}>›</button></>}</div>}
                  <DataTable data={step.data} previous={index > 0 ? trace.steps[index - 1].data : undefined} focus={focus} />
                  <div className="explanation"><div className="eyebrow">WHAT HAPPENS HERE</div><p>{step.explanation}</p>
                    {!!step.details.length && <ul>{step.details.map((detail, i) => <li key={i}>{detail}</li>)}</ul>}
                    {health?.ai_provider && health.ai_provider !== "none"
                      ? <><button className="ai-button" disabled={aiBusy || playing} onClick={() => void enhance()}>{aiBusy ? "Asking AI model…" : "✦ Explain with AI"}</button><span className="ai-caption">{health.ai_provider} · {health.ai_model}</span></>
                      : <span className="ai-caption">AI explanations are off (set AI_PROVIDER to enable)</span>}
                    {aiText && <div className="ai-explanation"><strong>AI-assisted explanation · verify against the trace</strong><p>{aiText}</p></div>}
                    {aiError && <div className="notice warning" role="status">{aiError}</div>}
                  </div>
                  {audience !== "beginner" && <details className="generated-sql" open={audience === "advanced"}><summary>MySQL query used for this snapshot</summary><pre><SQLText value={step.sql} /></pre></details>}
                </div>
              </>}
        {trace?.mutation && <div className="mutation-bar"><span>Changes are rolled back until explicitly applied.</span><button className="danger" disabled={busy} onClick={() => {
          if (!window.confirm("Apply this mutation to your isolated workspace? This changes the dataset.")) return;
          void action(async () => {
            const result = await api<{ affected_rows: number; tables: Table[] }>(`${currentPath}/apply`, post({ sql: trace.query }));
            setTables(result.tables); invalidate(); setStatus(`Mutation applied to the isolated workspace: ${result.affected_rows} affected rows.`);
          });
        }}>Apply mutation</button></div>}
        {trace && <details className="trace-notices"><summary>About this visualization</summary>{trace.notices.map((notice, i) => <p key={i}>{notice}</p>)}</details>}
      </section>
    </main>
    <footer className="app-footer"><span>Built for understanding, not pretending SQL runs line by line.</span><span>Local-first · MySQL 8.4 · No paid AI required</span></footer>
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);
