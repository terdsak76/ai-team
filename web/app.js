const AGENT_KEYS = ["specification", "ui_ux", "frontend", "backend", "tester"];
const OUTPUT_KEYS = { specification: "specification", ui_ux: "ui_design", frontend: "frontend", backend: "backend", tester: "test_report" };
const STORED_OUTPUT_KEYS = { specification: "specification", ui_ux: "ui_ux", frontend: "frontend", backend: "backend", tester: "tester" };
const PROMPT_STORAGE_KEY = "agent-team-prompts-v1";
const REPOSITORY_STORAGE_KEY = "agent-team-repository-url-v1";
const ACTIVE_PROJECT_STORAGE_KEY = "agent-team-active-project-v1";
const API_BASE_URL = "/api";
const state = { prompts: {}, projects: [], activeProjectId: "", running: false, currentRunId: "" };

const $ = (selector) => document.querySelector(selector);
const agentSelector = (key) => `[data-agent="${key}"]`;

function setError(message = "") {
  const error = $("#error-message");
  error.textContent = message;
  error.hidden = !message;
}

function setAgentState(key, label, className = "") {
  const element = $(`${agentSelector(key)} [data-state]`);
  element.textContent = label;
  element.className = `agent-state ${className}`.trim();
}

function formatOutput(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function setOutput(key, value) {
  const element = $(`${agentSelector(key)} [data-output="${OUTPUT_KEYS[key]}"]`);
  const text = formatOutput(value) ?? "";
  if ("value" in element) element.value = text;
  else element.textContent = text;
}

function getOutputText(key) {
  const element = $(`${agentSelector(key)} [data-output="${OUTPUT_KEYS[key]}"]`);
  return "value" in element ? element.value : element.textContent;
}

function readStorage(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage is optional; the app remains usable when it is blocked.
  }
}

function readSavedPrompts() {
  try {
    const saved = JSON.parse(readStorage(PROMPT_STORAGE_KEY, "{}"));
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

function savePrompts() {
  for (const key of AGENT_KEYS) state.prompts[key] = $(`#prompt-${key}`).value;
  writeStorage(PROMPT_STORAGE_KEY, JSON.stringify(state.prompts));
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: text };
  }
}

async function apiRequest(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  const data = await readJson(response);
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

async function loadPrompts() {
  const data = await apiRequest("/prompts");
  const saved = readSavedPrompts();
  for (const key of AGENT_KEYS) {
    const prompt = typeof saved[key] === "string" ? saved[key] : data.prompts?.[key];
    if (typeof prompt !== "string") throw new Error(`The ${key} agent prompt is unavailable.`);
    state.prompts[key] = prompt;
    $(`#prompt-${key}`).value = prompt;
  }
  $("#repository-input").value = readStorage(REPOSITORY_STORAGE_KEY);
}

function setPromptInputs(prompts) {
  for (const key of AGENT_KEYS) {
    if (typeof prompts[key] !== "string") continue;
    state.prompts[key] = prompts[key];
    $(`#prompt-${key}`).value = prompts[key];
  }
}

function projectById(projectId) {
  return state.projects.find((project) => String(project.id) === String(projectId));
}

function renderProjectOptions() {
  const select = $("#project-select");
  select.innerHTML = '<option value="">Use workspace values / choose a project</option>';
  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.project_name;
    select.appendChild(option);
  }
  select.value = state.activeProjectId;
}

function renderProjectList() {
  const list = $("#project-list");
  list.textContent = "";
  if (!state.projects.length) {
    list.innerHTML = '<div class="project-list-empty">No projects yet. Create one to save a repository and agent prompts.</div>';
    return;
  }
  for (const project of state.projects) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `project-list-item${String(project.id) === String(state.activeProjectId) ? " selected" : ""}`;
    button.innerHTML = `<strong></strong><span>${project.github_token_set ? "Private" : "Public"}</span>`;
    button.querySelector("strong").textContent = project.project_name;
    button.addEventListener("click", () => selectProject(project.id));
    list.appendChild(button);
  }
}

function populateProjectForm(project = null) {
  $("#project-id-input").value = project?.id ?? "";
  $("#project-form-title").textContent = project ? `Edit ${project.project_name}` : "New project";
  $("#master-project-name").value = project?.project_name ?? "";
  $("#master-repository").value = project?.github_repo ?? "";
  $("#master-project-context").value = project?.project_context ?? "";
  $("#master-token").value = "";
  $("#master-token").placeholder = project?.github_token_set ? "Leave blank to keep the saved token" : "ghp_...";
  $("#clear-master-token").checked = false;
  const prompts = project?.system_prompts || state.prompts;
  for (const key of AGENT_KEYS) $(`[data-project-prompt="${key}"]`).value = prompts[key] || "";
  $("#project-form-message").textContent = "";
}

function selectProject(projectId) {
  const project = projectById(projectId);
  if (!project) {
    state.activeProjectId = "";
    renderProjectOptions();
    renderProjectList();
    $("#project-name-input").value = "";
    $("#repository-input").value = readStorage(REPOSITORY_STORAGE_KEY);
    $("#project-name-input").readOnly = false;
    $("#repository-input").readOnly = false;
    return;
  }
  state.activeProjectId = String(project.id);
  writeStorage(ACTIVE_PROJECT_STORAGE_KEY, state.activeProjectId);
  $("#project-name-input").value = project.project_name;
  $("#repository-input").value = project.github_repo;
  $("#project-name-input").readOnly = true;
  $("#repository-input").readOnly = true;
  setPromptInputs(project.system_prompts || {});
  populateProjectForm(project);
  renderProjectOptions();
  renderProjectList();
}

async function loadProjects() {
  try {
    const data = await apiRequest("/projects");
    state.projects = Array.isArray(data.items) ? data.items : [];
    renderProjectOptions();
    renderProjectList();
    const savedProjectId = readStorage(ACTIVE_PROJECT_STORAGE_KEY);
    if (projectById(savedProjectId)) selectProject(savedProjectId);
    else populateProjectForm();
  } catch (error) {
    $("#project-list").textContent = error instanceof Error ? error.message : "Unable to load projects.";
    populateProjectForm();
  }
}

function toggleProjectManager() {
  const manager = $("#project-manager");
  manager.hidden = !manager.hidden;
  if (!manager.hidden) manager.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveProject(event) {
  event.preventDefault();
  const projectId = $("#project-id-input").value;
  const systemPrompts = {};
  for (const key of AGENT_KEYS) systemPrompts[key] = $(`[data-project-prompt="${key}"]`).value.trim();
  const payload = {
    project_name: $("#master-project-name").value.trim(),
    github_repo: $("#master-repository").value.trim(),
    github_token: $("#master-token").value,
    project_context: $("#master-project-context").value.trim(),
    clear_github_token: $("#clear-master-token").checked,
    system_prompts: systemPrompts,
  };
  const message = $("#project-form-message");
  message.textContent = "Saving...";
  try {
    const data = await apiRequest(projectId ? `/projects/${projectId}` : "/projects", {
      method: projectId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const index = state.projects.findIndex((project) => String(project.id) === String(data.id));
    if (index >= 0) state.projects[index] = data;
    else state.projects.push(data);
    renderProjectOptions();
    selectProject(data.id);
    message.textContent = "Project saved.";
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Unable to save the project.";
  }
}

function setRunState(running) {
  state.running = running;
  const button = $("#run-button");
  button.disabled = running;
  button.innerHTML = running
    ? '<span class="button-icon">...</span> Running agents...'
    : '<span class="button-icon">&gt;</span> Run agent team';
}

function initializeRunState() {
  for (const key of AGENT_KEYS) {
    setAgentState(key, key === "specification" ? "RUNNING" : "QUEUED", key === "specification" ? "running" : "");
    setOutput(key, key === "specification" ? "Generating the approved specification..." : "Waiting for the previous stage...");
  }
}

async function runAgents() {
  if (state.running) return;
  const request = $("#request-input").value.trim();
  const repositoryUrl = $("#repository-input").value.trim();
  if (!request) {
    setError("Describe the task you want the agent team to work on.");
    $("#request-input").focus();
    return;
  }
  const requirementCode = $("#requirement-code-input").value.trim();
  const projectName = $("#project-name-input").value.trim();
  if (!requirementCode || !projectName) {
    setError("Enter both a requirement code and project name before starting the agents.");
    return;
  }

  setError();
  savePrompts();
  writeStorage(REPOSITORY_STORAGE_KEY, repositoryUrl);
  setRunState(true);
  initializeRunState();
  try {
    const data = await apiRequest("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, prompts: state.prompts, repository_url: repositoryUrl, requirement_code: requirementCode, project_name: projectName, project_id: state.activeProjectId ? Number(state.activeProjectId) : undefined }),
    });
    state.currentRunId = data.run_id || "";
    for (const key of AGENT_KEYS) {
      setOutput(key, data[OUTPUT_KEYS[key]]);
      setAgentState(key, "COMPLETE", "complete");
    }
  } catch (error) {
    setError(error instanceof Error ? error.message : "Unable to reach the agent server.");
    for (const key of AGENT_KEYS) {
      if ($(`${agentSelector(key)} [data-state]`).textContent !== "COMPLETE") setAgentState(key, "ERROR", "error");
    }
  } finally {
    setRunState(false);
  }
}

async function loadSavedOutputs() {
  const requirementCode = $("#query-code-input").value.trim();
  const projectName = $("#query-project-input").value.trim();
  if (!requirementCode && !projectName) {
    $("#query-message").textContent = "Enter a requirement code or project name.";
    return;
  }
  const params = new URLSearchParams();
  if (requirementCode) params.set("requirement_code", requirementCode);
  if (projectName) params.set("project_name", projectName);
  try {
    const data = await apiRequest(`/outputs?${params.toString()}`);
    const item = data.items?.[0];
    if (!item) {
      $("#query-message").textContent = "No saved outputs matched that query.";
      return;
    }
    state.currentRunId = item.run_id;
    for (const key of AGENT_KEYS) setOutput(key, item.outputs?.[STORED_OUTPUT_KEYS[key]] ?? "");
    const savedPrompts = item.prompts || {};
    if (AGENT_KEYS.some((key) => typeof savedPrompts[key] === "string" && savedPrompts[key].trim())) {
      setPromptInputs(savedPrompts);
    }
    $("#requirement-code-input").value = item.requirement_code;
    $("#project-name-input").value = item.project_name;
    $("#query-message").textContent = `Loaded ${item.requirement_code} · ${item.project_name}. Edit the specification below and save it.`;
  } catch (error) {
    $("#query-message").textContent = error instanceof Error ? error.message : "Unable to load saved outputs.";
  }
}

async function saveSpecification() {
  if (!state.currentRunId) {
    setError("Load a saved run or complete a new run before editing its specification.");
    return;
  }
  const specification = $("#specification-editor").value.trim();
  if (!specification) {
    setError("The specification cannot be empty.");
    return;
  }
  try {
    const data = await apiRequest("/specification", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: state.currentRunId, specification }),
    });
    $("#query-message").textContent = `Specification saved as version ${data.version}.`;
    setError();
  } catch (error) {
    setError(error instanceof Error ? error.message : "Unable to save the specification.");
  }
}

async function runSelectedAgent(agentKey, button) {
  if (state.running) return;
  if (!state.currentRunId) {
    setError("Load a saved run or complete a full run before running one agent.");
    return;
  }
  const specification = $("#specification-editor").value.trim();
  if (!specification) {
    setError("Select or enter a specification before running an agent.");
    return;
  }
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Running...";
  setError();
  try {
    const data = await apiRequest("/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent: agentKey,
        run_id: state.currentRunId,
        specification,
        ui_design: getOutputText("ui_ux"),
        prompts: state.prompts,
        repository_url: $("#repository-input").value.trim(),
        project_id: state.activeProjectId ? Number(state.activeProjectId) : undefined,
      }),
    });
    setOutput(agentKey, data.output);
    setAgentState(agentKey, "COMPLETE", "complete");
    $("#query-message").textContent = `${agentKey} output saved as version ${data.version}.`;
  } catch (error) {
    setError(error instanceof Error ? error.message : `Unable to run ${agentKey}.`);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function initialize() {
  $("#run-button").addEventListener("click", runAgents);
  $("#query-button").addEventListener("click", loadSavedOutputs);
  $("#save-specification-button").addEventListener("click", saveSpecification);
  $("#projects-nav").addEventListener("click", toggleProjectManager);
  $("#manage-projects-button").addEventListener("click", toggleProjectManager);
  $("#new-project-button").addEventListener("click", () => populateProjectForm());
  $("#project-form").addEventListener("submit", saveProject);
  $("#project-select").addEventListener("change", (event) => selectProject(event.target.value));
  $("#request-input").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runAgents();
  });
  for (const key of AGENT_KEYS) $(`#prompt-${key}`).addEventListener("change", savePrompts);
  document.querySelectorAll("[data-run-agent]").forEach((button) => {
    button.addEventListener("click", () => runSelectedAgent(button.dataset.runAgent, button));
  });
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const outputName = OUTPUT_KEYS[button.dataset.copy] || button.dataset.copy;
      const outputElement = $(`${agentSelector(button.dataset.copy)} [data-output="${outputName}"]`);
      const text = "value" in outputElement ? outputElement.value : outputElement.textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy"; }, 1200);
      } catch {
        setError("Clipboard access failed. Select and copy the output instead.");
      }
    });
  });
  loadPrompts()
    .then(loadProjects)
    .catch((error) => setError(error instanceof Error ? error.message : "Unable to load agent prompts."));
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
else initialize();
