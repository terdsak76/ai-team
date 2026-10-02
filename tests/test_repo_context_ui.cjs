const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() { this.children = []; this.textContent = ""; this.disabled = false; this._value = ""; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; this._value = ""; }
  get value() { return this._value || this.children[0]?.value || ""; }
  set value(value) { this._value = value; }
}

function setup(fetch) {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      readyState: "loading", addEventListener() {},
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, new Element());
        return elements.get(selector);
      },
      createElement() { return new Element(); },
    },
    fetch, TextDecoder, URLSearchParams,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8"), context);
  return { context, elements, run: (code) => vm.runInContext(code, context) };
}

function streamed(lines, splitAt = 0) {
  const bytes = new TextEncoder().encode(lines);
  const pieces = splitAt ? [bytes.slice(0, splitAt), bytes.slice(splitAt)] : [bytes];
  return {
    ok: true, headers: new Headers({ "Content-Type": "application/x-ndjson" }),
    body: new ReadableStream({ start(controller) { for (const piece of pieces) controller.enqueue(piece); controller.close(); } }),
  };
}

test("progress parser handles fragmented Unicode, context status, and final result", async () => {
  const events = [
    { type: "repo_context", status: "indexing" },
    { type: "repo_context", status: "summarizing" },
    { type: "repo_context", status: "ready", data: { status: "ready", summary: "Architecture: ภาษาไทย", relationships: [], artifacts: [] } },
    { type: "agent", agent: "backend", status: "complete", output: "Backend code" },
    { type: "result", data: { run_id: "saved-run" } },
  ];
  const wire = events.map((event) => JSON.stringify(event)).join("\n");
  const bytesBeforeThai = new TextEncoder().encode(wire.slice(0, wire.indexOf("ภ"))).length;
  const app = setup(async () => streamed(wire, bytesBeforeThai + 1));
  const result = await app.run("runApiRequest({ request: 'task' })");
  assert.equal(result.run_id, "saved-run");
  assert.equal(app.elements.get("#repo-context-summary").textContent, "Architecture: ภาษาไทย");
  assert.equal(app.elements.get('[data-agent="repo_context"] [data-state]').textContent, "READY");
  assert.equal(app.elements.get('[data-agent="backend"] [data-output="backend"]').value, "Backend code");
});

test("stream errors and premature termination are reported, JSON fallback works", async () => {
  const app = setup(async () => streamed('{"type":"error","error":"Indexing failed"}\n'));
  await assert.rejects(app.run("runApiRequest({})"), /Indexing failed/);
  app.context.fetch = async () => streamed('{"type":"repo_context","status":"indexing"}\n');
  await assert.rejects(app.run("runApiRequest({})"), /ended before a completed run/);
  app.context.fetch = async () => ({ ok: true, headers: new Headers({ "Content-Type": "application/json" }), text: async () => '{"run_id":"json-run"}' });
  assert.equal((await app.run("runApiRequest({})")).run_id, "json-run");
});

test("saved context safely renders relationships and retrieves immutable artifacts", async () => {
  const edge = { source_name: "User", source_path: "models.py", target_name: "users", target_path: "schema.sql", relation_type: "orm_maps_to_table", confidence: 1, evidence: "<script>untrusted</script>" };
  const snapshot = { id: 42, artifacts: [{ artifact_path: "architecture.md", content: "Saved architecture." }], relationships: [edge] };
  const app = setup(async (url) => {
    assert.equal(url, "/api/repo-context?snapshot_id=42");
    return { ok: true, text: async () => JSON.stringify(snapshot) };
  });
  app.context.saved = JSON.stringify({ status: "ready", summary: "Grounded summary", snapshot: { snapshot_id: 42 }, artifacts: snapshot.artifacts, relationships: [edge] });
  app.run("renderRepoContext(saved)");
  await new Promise(setImmediate);
  assert.equal(app.elements.get("#repo-artifact-content").textContent, "Saved architecture.");
  assert.equal(app.elements.get("#repo-artifact-select").disabled, false);
  assert.equal(app.elements.get("#repo-relationships-body").children[0].children[4].textContent, edge.evidence);
  app.run("renderRepoContext(null)");
  assert.equal(app.elements.get('[data-agent="repo_context"] [data-state]').textContent, "UNAVAILABLE");
  assert.equal(app.elements.get("#repo-artifact-download").disabled, true);
});

test("stale artifact responses do not overwrite a different saved run", async () => {
  let resolve;
  const app = setup(() => new Promise((done) => { resolve = done; }));
  app.run("renderRepoContext({status:'ready',summary:'First',snapshot:{snapshot_id:1},artifacts:[],relationships:[]})");
  app.run("renderRepoContext({status:'skipped',summary:'Second',artifacts:[],relationships:[]})");
  resolve({ ok: true, text: async () => JSON.stringify({ id: 1, artifacts: [{ artifact_path: "architecture.md", content: "Stale" }], relationships: [] }) });
  await new Promise(setImmediate);
  assert.equal(app.elements.get("#repo-context-summary").textContent, "Second");
  assert.equal(app.elements.get("#repo-artifact-content").textContent, "No generated artifacts for this run.");
});

test("cache panel distinguishes reused and rebuilt context", () => {
  const app = setup();
  app.run("handleRunProgress({type:'repo_context',status:'checking'})");
  assert.equal(app.elements.get('[data-agent="repo_context"] [data-state]').textContent, "CHECKING CACHE");
  app.run("renderRepoContext({status:'ready',summary:'Architecture',cache:{reused:true,reason:'unchanged'},artifacts:[],relationships:[]})");
  assert.equal(app.elements.get('[data-agent="repo_context"] [data-state]').textContent, "REUSED");
  assert.match(app.elements.get("#repo-context-cache").textContent, /Reused saved context/);
  app.run("renderRepoContext({status:'ready',summary:'Architecture',cache:{reused:false,reason:'database_schema_expired'},artifacts:[],relationships:[]})");
  assert.equal(app.elements.get('[data-agent="repo_context"] [data-state]').textContent, "REBUILT");
  assert.match(app.elements.get("#repo-context-cache").textContent, /Database schema cache expired/);
});

test("manual refresh requests context only and does not require or replace a requirement", async () => {
  const requests = [];
  const app = setup(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return streamed(JSON.stringify({ type: "result", data: { repo_context: { status: "ready", summary: "Refreshed", cache: { reused: false, reason: "manual_refresh" }, artifacts: [], relationships: [] } } }));
  });
  app.run("state.activeProjectId = '7'; state.currentRunId = 'old-run';");
  app.run("$('#repository-input').value = '';");
  await app.run("refreshRepoContext()");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/repo-context");
  assert.equal(requests[0].body.project_id, 7);
  assert.equal(requests[0].body.stream, true);
  assert.equal(requests[0].body.request, undefined);
  assert.equal(app.run("state.currentRunId"), "old-run");
  assert.equal(app.elements.get('[data-agent="repo_context"] [data-state]').textContent, "REBUILT");
  assert.equal(app.elements.get("#repo-context-refresh").disabled, false);
});
