const AGENT_KEYS = ["specification", "frontend", "backend", "tester"];
const OUTPUT_KEYS = { specification: "specification", frontend: "frontend", backend: "backend", tester: "test_report" };
const PROMPT_STORAGE_KEY = "agent-team-prompts-v1";
const REPOSITORY_STORAGE_KEY = "agent-team-repository-url-v1";
const API_BASE_URL = "/api";
const state = { prompts: {}, running: false };

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
    $(`${agentSelector(key)} [data-output="${OUTPUT_KEYS[key]}"]`).textContent =
      key === "specification" ? "Generating the approved specification..." : "Waiting for the previous stage...";
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

  setError();
  savePrompts();
  writeStorage(REPOSITORY_STORAGE_KEY, repositoryUrl);
  setRunState(true);
  initializeRunState();
  try {
    const data = await apiRequest("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, prompts: state.prompts, repository_url: repositoryUrl }),
    });
    for (const key of AGENT_KEYS) {
      $(`${agentSelector(key)} [data-output="${OUTPUT_KEYS[key]}"]`).textContent = formatOutput(data[OUTPUT_KEYS[key]]);
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

function initialize() {
  $("#run-button").addEventListener("click", runAgents);
  $("#request-input").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runAgents();
  });
  for (const key of AGENT_KEYS) $(`#prompt-${key}`).addEventListener("change", savePrompts);
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const outputName = OUTPUT_KEYS[button.dataset.copy] || button.dataset.copy;
      const text = $(`${agentSelector(button.dataset.copy)} [data-output="${outputName}"]`).textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy"; }, 1200);
      } catch {
        setError("Clipboard access failed. Select and copy the output instead.");
      }
    });
  });
  loadPrompts().catch((error) => setError(error instanceof Error ? error.message : "Unable to load agent prompts."));
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
else initialize();
