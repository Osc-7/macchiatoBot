// ===== Shared helpers =====
const CONSOLE_BASE = "/console";
const LOGIN_PATH = "/login";

const $ = (id) => document.getElementById(id);
const toastEl = $("toast");

// Persisted active session across re-renders. Set by switchSessionBtn,
// used by chat and console to send the right session_id to the daemon.
let activeSessionId = "";

function showToast(message, timeoutMs = 2600) {
  toastEl.textContent = message;
  toastEl.classList.remove("hidden");
  window.setTimeout(() => toastEl.classList.add("hidden"), timeoutMs);
}

function apiUrl(path) {
  if (!path.startsWith("/")) return path;
  return `${CONSOLE_BASE}${path}`;
}

async function requestJson(url, options = {}) {
  const resp = await fetch(apiUrl(url), {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (resp.status === 401) {
    window.location.href = `${LOGIN_PATH}?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("Unauthorized");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data?.detail || resp.statusText || "request failed";
    throw new Error(detail);
  }
  return data;
}

function redirectToLogin() {
  window.location.href = `${LOGIN_PATH}?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

// ===== Navigation =====
const views = document.querySelectorAll(".view");
const navItems = document.querySelectorAll(".nav-item");

const viewMeta = {
  chat: { title: "Chat", subtitle: "Talk to the running macchiato daemon." },
  settings: { title: "Settings", subtitle: "Edit config.yaml. Daemon restart applies changes." },
  kernel: { title: "Kernel", subtitle: "Monitor agent processes, manage sessions, and run diagnostics." },
  backups: { title: "Backups", subtitle: "Manage on-disk config snapshots." },
};

function switchView(name) {
  views.forEach((v) => v.classList.toggle("active", v.dataset.view === name));
  navItems.forEach((n) => n.classList.toggle("active", n.dataset.view === name));
  const meta = viewMeta[name];
  if (meta) {
    $("pageTitle").textContent = meta.title;
    $("pageSubtitle").textContent = meta.subtitle;
  }
  if (name === "kernel") {
    requestAnimationFrame(() => $("consoleInput")?.focus({ preventScroll: true }));
  }
  if (name === "chat") {
    const sid = activeSessionId || $("activeSessionSelect").value || "";
    restoreChatMessages(sid);
  }
}

navItems.forEach((n) => n.addEventListener("click", () => switchView(n.dataset.view)));

// ===== Daemon health =====
async function pollHealth() {
  try {
    const data = await requestJson("/api/health");
    const statusEl = $("daemonStatus");
    statusEl.classList.toggle("online", Boolean(data.connected));
    statusEl.classList.toggle("offline", !data.connected);
    statusEl.querySelector(".status-text").textContent = data.connected ? "daemon online" : "daemon offline";
  } catch (error) {
    const statusEl = $("daemonStatus");
    statusEl.classList.remove("online");
    statusEl.classList.add("offline");
    statusEl.querySelector(".status-text").textContent = "daemon offline";
  }
}

// ===== Settings (config) =====
let originalConfig = {};
let editedConfig = {};
let flatSettings = [];

function deepClone(value) {
  return JSON.parse(JSON.stringify(value ?? {}));
}

function valueType(value) {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  return typeof value;
}

function flattenObject(input, prefix = "") {
  const out = [];
  Object.keys(input || {}).forEach((key) => {
    const path = prefix ? `${prefix}.${key}` : key;
    const value = input[key];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      out.push(...flattenObject(value, path));
      return;
    }
    out.push({ path, value, type: valueType(value) });
  });
  return out;
}

function getByPath(target, path) {
  return path.split(".").reduce((cur, k) => (cur == null ? cur : cur[k]), target);
}

function setByPath(target, path, value) {
  const parts = path.split(".");
  let cursor = target;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const k = parts[i];
    if (!cursor[k] || typeof cursor[k] !== "object" || Array.isArray(cursor[k])) cursor[k] = {};
    cursor = cursor[k];
  }
  cursor[parts[parts.length - 1]] = value;
}

function parseControlValue(raw, type) {
  if (type === "boolean") return raw === "true";
  if (type === "number") {
    const n = Number(raw);
    if (Number.isNaN(n)) throw new Error("must be a number");
    return n;
  }
  if (type === "null") {
    const t = (raw || "").trim();
    if (!t || t.toLowerCase() === "null") return null;
    return raw;
  }
  if (type === "array" || type === "object") {
    const parsed = JSON.parse(raw || (type === "array" ? "[]" : "{}"));
    if (type === "array" && !Array.isArray(parsed)) throw new Error("must be a JSON array");
    if (type === "object" && (parsed === null || Array.isArray(parsed) || typeof parsed !== "object")) {
      throw new Error("must be a JSON object");
    }
    return parsed;
  }
  return raw;
}

const SENSITIVE_RE = /(api[_-]?key|secret|password|passwd|token|credential|bearer|access[_-]?key)/i;

function isSensitivePath(path) {
  return SENSITIVE_RE.test(path);
}

function previewValue(value) {
  if (value == null) return "null";
  if (typeof value === "string") {
    if (value.length > 40) return JSON.stringify(value.slice(0, 37) + "…");
    return JSON.stringify(value);
  }
  if (typeof value === "object") {
    try {
      const s = JSON.stringify(value);
      return s.length > 40 ? s.slice(0, 37) + "…" : s;
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function createControl(setting) {
  const value = getByPath(editedConfig, setting.path);
  if (setting.type === "boolean") {
    const sel = document.createElement("select");
    sel.className = "control setting-control";
    sel.innerHTML = `<option value="true">true</option><option value="false">false</option>`;
    sel.value = String(Boolean(value));
    return sel;
  }
  if (setting.type === "array") {
    return createArrayEditor(setting);
  }
  if (setting.type === "object") {
    const ta = document.createElement("textarea");
    ta.className = "control setting-control";
    ta.value = JSON.stringify(value, null, 2);
    return ta;
  }
  const input = document.createElement("input");
  input.className = "control setting-control";
  if (setting.type === "number") {
    input.type = "number";
    input.step = "any";
  } else if (isSensitivePath(setting.path)) {
    input.type = "password";
    input.autocomplete = "off";
    input.spellcheck = false;
  } else {
    input.type = "text";
  }
  input.value = value == null ? "" : String(value);
  return input;
}

function inferArrayElementKind(arr) {
  if (!Array.isArray(arr) || !arr.length) return "string";
  for (const item of arr) {
    if (item != null && typeof item === "object") {
      return Array.isArray(item) ? "json" : "object";
    }
    if (typeof item === "number") return "number";
    if (typeof item === "boolean") return "boolean";
  }
  return "string";
}

function objectArrayTemplate(items) {
  const template = {};
  (items || []).forEach((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return;
    Object.entries(item).forEach(([key, val]) => {
      if (!(key in template)) {
        if (Array.isArray(val)) template[key] = [];
        else if (val && typeof val === "object") template[key] = {};
        else if (typeof val === "number") template[key] = 0;
        else if (typeof val === "boolean") template[key] = false;
        else template[key] = "";
      }
    });
  });
  return template;
}

function defaultArrayItem(kind, items) {
  if (kind === "object") return objectArrayTemplate(items);
  if (kind === "number") return 0;
  if (kind === "boolean") return false;
  return "";
}

function createArrayEditor(setting) {
  const root = document.createElement("div");
  root.className = "array-editor setting-control";

  const listEl = document.createElement("div");
  listEl.className = "array-list";

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn ghost small array-add";
  addBtn.textContent = "+ Add item";

  const readValue = () => {
    const val = getByPath(editedConfig, setting.path);
    return Array.isArray(val) ? val : [];
  };

  const commit = (next) => {
    setByPath(editedConfig, setting.path, next);
    root.classList.remove("invalid");
    const row = root.closest(".setting-row");
    if (row) applyRowState(row, setting);
    renderDiff();
    renderList();
  };

  const renderPrimitiveRow = (item, index, items, kind) => {
    const row = document.createElement("div");
    row.className = "array-item";
    let input;
    if (kind === "boolean") {
      input = document.createElement("select");
      input.className = "control";
      input.innerHTML = `<option value="true">true</option><option value="false">false</option>`;
      input.value = String(Boolean(item));
    } else {
      input = document.createElement("input");
      input.className = "control";
      input.type = kind === "number" ? "number" : "text";
      if (kind === "number") input.step = "any";
      input.value = item == null ? "" : String(item);
    }
    input.addEventListener("change", () => {
      const next = [...items];
      if (kind === "number") {
        const n = Number(input.value);
        if (Number.isNaN(n)) {
          input.classList.add("invalid");
          return;
        }
        next[index] = n;
      } else if (kind === "boolean") {
        next[index] = input.value === "true";
      } else {
        next[index] = input.value;
      }
      input.classList.remove("invalid");
      commit(next);
    });

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn-icon-mini array-remove";
    removeBtn.title = "Remove item";
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      commit(items.filter((_, i) => i !== index));
    });

    row.appendChild(input);
    row.appendChild(removeBtn);
    return row;
  };

  const renderObjectRow = (item, index, items) => {
    const row = document.createElement("div");
    row.className = "array-item array-item-object";
    const header = document.createElement("div");
    header.className = "array-item-head";
    header.innerHTML = `<span class="muted small">Item ${index + 1}</span>`;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn-icon-mini array-remove";
    removeBtn.title = "Remove item";
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      commit(items.filter((_, i) => i !== index));
    });
    header.appendChild(removeBtn);
    row.appendChild(header);

    const fields = document.createElement("div");
    fields.className = "array-object-fields";
    const obj = item && typeof item === "object" && !Array.isArray(item) ? item : {};

    const updateKey = (key, val) => {
      const next = items.map((entry, i) => (i === index ? { ...entry, [key]: val } : entry));
      commit(next);
    };

    Object.entries(obj).forEach(([key, val]) => {
      const field = document.createElement("div");
      field.className = "array-object-field";
      const label = document.createElement("label");
      label.textContent = key;
      field.appendChild(label);

      if (val != null && typeof val === "object") {
        const ta = document.createElement("textarea");
        ta.className = "control array-object-json";
        ta.rows = Math.min(6, Math.max(2, String(JSON.stringify(val)).split("\n").length));
        ta.value = JSON.stringify(val, null, 2);
        ta.addEventListener("change", () => {
          try {
            updateKey(key, JSON.parse(ta.value));
            ta.classList.remove("invalid");
          } catch {
            ta.classList.add("invalid");
            showToast(`${setting.path}[${index}].${key}: invalid JSON`);
          }
        });
        field.appendChild(ta);
      } else {
        const inp = document.createElement("input");
        inp.className = "control";
        if (typeof val === "number") {
          inp.type = "number";
          inp.step = "any";
        }
        inp.value = val == null ? "" : String(val);
        inp.addEventListener("change", () => {
          let parsed = inp.value;
          if (typeof val === "number") {
            const n = Number(inp.value);
            if (Number.isNaN(n)) {
              inp.classList.add("invalid");
              return;
            }
            parsed = n;
          } else if (typeof val === "boolean") {
            parsed = inp.value === "true";
          }
          inp.classList.remove("invalid");
          updateKey(key, parsed);
        });
        field.appendChild(inp);
      }
      fields.appendChild(field);
    });

    row.appendChild(fields);
    return row;
  };

  const renderList = () => {
    const items = readValue();
    const kind = inferArrayElementKind(items);
    listEl.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "muted small array-empty";
      empty.textContent = "No items yet. Click + Add item.";
      listEl.appendChild(empty);
      return;
    }
    items.forEach((item, index) => {
      if (kind === "object") {
        listEl.appendChild(renderObjectRow(item, index, items));
      } else {
        listEl.appendChild(renderPrimitiveRow(item, index, items, kind));
      }
    });
  };

  addBtn.addEventListener("click", () => {
    const items = readValue();
    const kind = inferArrayElementKind(items);
    commit([...items, defaultArrayItem(kind, items)]);
  });

  root.refresh = renderList;
  root.appendChild(listEl);
  root.appendChild(addBtn);
  renderList();
  return root;
}

function isModified(path) {
  return JSON.stringify(getByPath(originalConfig, path)) !==
    JSON.stringify(getByPath(editedConfig, path));
}

function computeDiff() {
  const orig = new Map(flattenObject(originalConfig).map((i) => [i.path, JSON.stringify(i.value)]));
  const curr = new Map(flattenObject(editedConfig).map((i) => [i.path, JSON.stringify(i.value)]));
  const paths = new Set([...orig.keys(), ...curr.keys()]);
  const changedPaths = [];
  paths.forEach((p) => {
    if (orig.get(p) !== curr.get(p)) changedPaths.push(p);
  });
  return { changed: changedPaths.length, total: curr.size, changedPaths };
}

function renderDiff() {
  const { changed, total, changedPaths } = computeDiff();
  $("settingsDiff").textContent = `${changed} changed / ${total} total`;
  $("settingsRevertAllBtn").disabled = changed === 0;

  const panel = $("settingsDiffPanel");
  const body = $("settingsDiffBody");
  $("settingsDiffCount").textContent = String(changed);
  if (!changed) {
    panel.classList.add("hidden");
    panel.open = false;
    body.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");
  body.innerHTML = "";
  changedPaths.sort().forEach((path) => {
    const row = document.createElement("div");
    row.className = "diff-row";
    const masked = isSensitivePath(path);
    const oldVal = masked ? "•••" : previewValue(getByPath(originalConfig, path));
    const newVal = masked ? "•••" : previewValue(getByPath(editedConfig, path));
    row.innerHTML = `
      <code title="${path}">${path}</code>
      <span class="diff-old" title="${oldVal}">${oldVal}</span>
      <span class="diff-new" title="${newVal}">→ ${newVal}</span>
    `;
    body.appendChild(row);
  });
}

let activeSettingsGroup = null;

function groupId(name) {
  return `settings-group-${name.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

function renderSettingsNav(groupNames, groupItems) {
  const navEl = $("settingsNav");
  navEl.innerHTML = '<div class="nav-section-title">Categories</div>';
  groupNames.forEach((name) => {
    const items = groupItems.get(name) || [];
    const changed = items.filter((it) => isModified(it.path)).length;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "settings-nav-item";
    if (changed > 0) btn.classList.add("has-changes");
    if (name === activeSettingsGroup) btn.classList.add("active");
    btn.dataset.group = name;
    btn.innerHTML = `
      <span><span class="dot"></span>${name}</span>
      <span class="count">${changed > 0 ? `${changed}/` : ""}${items.length}</span>
    `;
    btn.addEventListener("click", () => {
      activeSettingsGroup = name;
      navEl
        .querySelectorAll(".settings-nav-item")
        .forEach((el) => el.classList.toggle("active", el.dataset.group === name));
      const target = document.getElementById(groupId(name));
      if (target) {
        target.open = true;
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
    navEl.appendChild(btn);
  });
}

function applyRowState(row, item) {
  const modified = isModified(item.path);
  row.classList.toggle("modified", modified);
  const pill = row.querySelector(".modified-pill");
  if (pill) pill.classList.toggle("hidden", !modified);
  const resetBtn = row.querySelector(".reset-btn");
  if (resetBtn) resetBtn.disabled = !modified;
}

function renderSettings() {
  const query = ($("settingsSearch").value || "").trim().toLowerCase();
  const items = flatSettings.filter((s) => s.path.toLowerCase().includes(query));

  const groups = new Map();
  items.forEach((item) => {
    const group = item.path.split(".")[0] || "root";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(item);
  });

  const groupNames = [...groups.keys()];
  if (!activeSettingsGroup || !groups.has(activeSettingsGroup)) {
    activeSettingsGroup = groupNames[0] || null;
  }
  renderSettingsNav(groupNames, groups);

  const listEl = $("settingsList");
  listEl.innerHTML = "";
  if (!items.length) {
    listEl.innerHTML = '<p class="muted">No settings match this query.</p>';
    return;
  }

  groupNames.forEach((groupName) => {
    const groupItems = groups.get(groupName);
    const details = document.createElement("details");
    details.className = "setting-group";
    details.id = groupId(groupName);
    details.open = true;
    const summary = document.createElement("summary");
    summary.innerHTML = `<span>${groupName} <span class="muted small">(${groupItems.length})</span></span>`;
    details.appendChild(summary);

    groupItems.forEach((item) => {
      const row = document.createElement("div");
      row.className = "setting-row";
      if (item.type === "array") row.classList.add("setting-row-array");
      row.dataset.path = item.path;
      const sensitive = isSensitivePath(item.path);

      const key = document.createElement("div");
      key.className = "setting-key";
      key.innerHTML = `
        <code>${item.path}</code>
        <span class="setting-row-meta">
          <span class="type-badge">${item.type}</span>
          ${sensitive ? '<span class="type-badge" style="color:var(--accent-strong)">secret</span>' : ""}
          <span class="modified-pill hidden">modified</span>
        </span>
      `;
      row.appendChild(key);

      const wrap = document.createElement("div");
      wrap.className = "setting-control-wrap";
      const control = createControl(item);
      wrap.appendChild(control);

      const meta = document.createElement("div");
      meta.className = "setting-row-meta";

      if (sensitive && control.tagName === "INPUT") {
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "btn-icon-mini";
        eye.title = "Show / hide";
        eye.textContent = "👁";
        eye.addEventListener("click", () => {
          control.type = control.type === "password" ? "text" : "password";
        });
        meta.appendChild(eye);
      }

      const resetBtn = document.createElement("button");
      resetBtn.type = "button";
      resetBtn.className = "btn-icon-mini reset-btn";
      resetBtn.title = "Revert to disk value";
      resetBtn.textContent = "↶";
      resetBtn.addEventListener("click", () => {
        const original = getByPath(originalConfig, item.path);
        setByPath(editedConfig, item.path, deepClone(original));
        if (typeof control.refresh === "function") {
          control.refresh();
        } else if (control.tagName === "SELECT") {
          control.value = String(Boolean(original));
        } else if (control.tagName === "TEXTAREA") {
          control.value = JSON.stringify(original, null, 2);
        } else {
          control.value = original == null ? "" : String(original);
        }
        control.classList.remove("invalid");
        applyRowState(row, item);
        renderDiff();
      });
      meta.appendChild(resetBtn);
      wrap.appendChild(meta);
      row.appendChild(wrap);

      if (item.type !== "array") {
        control.addEventListener("change", () => {
          try {
            const next = parseControlValue(control.value, item.type);
            setByPath(editedConfig, item.path, next);
            control.classList.remove("invalid");
            applyRowState(row, item);
            renderDiff();
          } catch (error) {
            control.classList.add("invalid");
            showToast(`${item.path}: ${error.message}`);
          }
        });
      }

      details.appendChild(row);
      applyRowState(row, item);
    });

    listEl.appendChild(details);
  });
}

async function loadConfig() {
  const data = await requestJson("/api/config");
  $("settingsPath").textContent = data.path;
  originalConfig = deepClone(data.content || {});
  editedConfig = deepClone(data.content || {});
  flatSettings = flattenObject(editedConfig);
  renderSettings();
  renderDiff();
}

async function saveConfig() {
  await requestJson("/api/config", {
    method: "PUT",
    body: JSON.stringify({ content: editedConfig }),
  });
  originalConfig = deepClone(editedConfig);
  flatSettings = flattenObject(editedConfig);
  renderDiff();
  await loadBackups();
  showToast("Config saved. Restart the daemon for changes to take effect.");
}

$("settingsReloadBtn").addEventListener("click", async () => {
  try {
    await loadConfig();
    showToast("Config reloaded from disk.");
  } catch (e) {
    showToast(`Reload failed: ${e.message}`);
  }
});

$("settingsSaveBtn").addEventListener("click", async () => {
  try {
    await saveConfig();
  } catch (e) {
    showToast(`Save failed: ${e.message}`);
  }
});

$("settingsSearch").addEventListener("input", renderSettings);

$("settingsRevertAllBtn").addEventListener("click", () => {
  const { changed } = computeDiff();
  if (!changed) return;
  if (!window.confirm(`Revert all ${changed} pending change(s) back to disk?`)) return;
  editedConfig = deepClone(originalConfig);
  flatSettings = flattenObject(editedConfig);
  renderSettings();
  renderDiff();
  showToast("Reverted all pending changes.");
});

// ===== Backups =====
async function loadBackups() {
  const data = await requestJson("/api/config/backups");
  const items = Array.isArray(data.items) ? data.items : [];
  $("backupCount").textContent = `${items.length} backups`;
  const listEl = $("backupList");
  if (!items.length) {
    listEl.innerHTML = '<p class="muted">No backups yet.</p>';
    return;
  }
  listEl.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "backup-row";
    row.innerHTML = `
      <div>
        <div class="name">${item.name}</div>
        <div class="when">${item.modified_at} · ${item.size} bytes</div>
      </div>
      <button class="btn small ghost">Restore</button>
      <button class="btn small ghost">Copy path</button>
    `;
    const [restoreBtn, copyBtn] = row.querySelectorAll("button");
    restoreBtn.addEventListener("click", async () => {
      if (!window.confirm(`Restore ${item.name}? This overwrites config.yaml on disk.`)) return;
      try {
        await requestJson("/api/config/restore", {
          method: "POST",
          body: JSON.stringify({ backup_name: item.name }),
        });
        await loadConfig();
        await loadBackups();
        showToast(`Restored ${item.name}. Restart the daemon to apply.`);
      } catch (e) {
        showToast(`Restore failed: ${e.message}`);
      }
    });
    copyBtn.addEventListener("click", () => {
      navigator.clipboard.writeText(item.path).then(
        () => showToast("Path copied."),
        () => showToast("Clipboard not available."),
      );
    });
    listEl.appendChild(row);
  });
}

$("backupsReloadBtn").addEventListener("click", async () => {
  try {
    await loadBackups();
  } catch (e) {
    showToast(`Refresh failed: ${e.message}`);
  }
});

$("backupCreateBtn").addEventListener("click", async () => {
  try {
    await requestJson("/api/config/backups", {
      method: "POST",
      body: JSON.stringify({ reason: "manual" }),
    });
    await loadBackups();
    showToast("Backup created.");
  } catch (e) {
    showToast(`Backup failed: ${e.message}`);
  }
});

// ===== Kernel =====
let currentCores = [];
let consoleKnownSessions = [];

function updateConsoleKnownSessions(cores, sessions) {
  const ids = new Set();
  (Array.isArray(cores) ? cores : []).forEach((c) => {
    if (c?.session_id) ids.add(c.session_id);
  });
  (Array.isArray(sessions) ? sessions : []).forEach((sid) => {
    if (sid) ids.add(sid);
  });
  consoleKnownSessions = [...ids];
}

function renderCoreList(cores) {
  currentCores = Array.isArray(cores) ? cores : [];
  const listEl = $("coreList");
  if (!listEl) return;
  if (!currentCores.length) {
    listEl.innerHTML = `
      <p class="muted small">No cores running.</p>
      <p class="muted small">Pick a session on the left and click <strong>Start core</strong>, or run <code>spawn &lt;session_id&gt;</code> in the console.</p>
    `;
    return;
  }
  listEl.innerHTML = "";
  currentCores.forEach((core) => {
    const sid = core?.session_id || "";
    if (!sid) return;
    const lifecycle = core?.lifecycle || core?.status || "running";
    const card = document.createElement("div");
    card.className = "core-card";
    card.innerHTML = `
      <div class="core-card-main">
        <code class="core-sid">${sid}</code>
        <span class="core-badge ${lifecycle}">${lifecycle}</span>
      </div>
      <div class="core-card-meta muted small">
        ${core?.source || "-"} · ${core?.user_id || "-"} · ${core?.mode || "-"}
        · ${core?.turn_count ?? 0} turns · ${core?.total_tokens ?? 0} tokens
        · idle ${Math.round(core?.idle_seconds ?? 0)}s
      </div>
      <div class="core-card-actions">
        <button type="button" class="btn ghost small" data-core-action="inspect">Inspect</button>
        <button type="button" class="btn ghost small" data-core-action="cancel">Cancel turn</button>
        <button type="button" class="btn danger small" data-core-action="kill">Kill</button>
      </div>
    `;
    card.querySelector('[data-core-action="inspect"]').addEventListener("click", () => {
      runKernelCommand(`inspect ${sid}`);
      consoleInput?.focus();
    });
    card.querySelector('[data-core-action="cancel"]').addEventListener("click", async () => {
      try {
        await callKernelAction("/api/kernel/cancel", { session_id: sid }, `Cancel requested for ${sid}.`);
      } catch (e) {
        showToast(`Cancel failed: ${e.message}`);
      }
    });
    card.querySelector('[data-core-action="kill"]').addEventListener("click", async () => {
      if (!window.confirm(`Kill core ${sid}? This tears down the agent process.`)) return;
      try {
        await callKernelAction("/api/kernel/kill", { session_id: sid }, `Killed ${sid}.`);
      } catch (e) {
        showToast(`Kill failed: ${e.message}`);
      }
    });
    listEl.appendChild(card);
  });
}

function renderSessionsDropdowns(sessions, active) {
  const list = Array.isArray(sessions) ? sessions.slice(0, 200) : [];
  const sessionSel = $("sessionSelect");
  const activeSel = $("activeSessionSelect");
  sessionSel.innerHTML = '<option value="">Select a session…</option>';
  activeSel.innerHTML = '<option value="">(daemon default)</option>';
  list.forEach((sid) => {
    const opt = document.createElement("option");
    opt.value = sid;
    opt.textContent = sid;
    if (sid === active) opt.selected = true;
    sessionSel.appendChild(opt);
    const opt2 = opt.cloneNode(true);
    activeSel.appendChild(opt2);
  });
  // Restore the locally-persisted active session selection across re-renders.
  if (activeSessionId && list.includes(activeSessionId)) {
    activeSel.value = activeSessionId;
    sessionSel.value = activeSessionId;
  }
}

function renderModels(models) {
  const sel = $("modelSelect");
  sel.innerHTML = '<option value="">Select a model…</option>';
  if (!Array.isArray(models)) return;
  models.forEach((m) => {
    const name = m?.name || "";
    if (!name) return;
    const opt = document.createElement("option");
    opt.value = name;
    const modelId = m?.model || "";
    opt.textContent = modelId ? `${name} (${modelId})` : name;
    if (m?.active) opt.selected = true;
    sel.appendChild(opt);
  });
}

function renderUsage(usage) {
  const u = usage || {};
  $("tuPrompt").textContent = u.prompt_tokens ?? 0;
  $("tuCompletion").textContent = u.completion_tokens ?? 0;
  $("tuTotal").textContent = u.total_tokens ?? 0;
  $("tuCalls").textContent = u.call_count ?? 0;
  $("tuCost").textContent = (u.cost_yuan ?? 0).toFixed ? (u.cost_yuan ?? 0).toFixed(4) : u.cost_yuan;
  $("tuCacheHit").textContent = u.prompt_cache_hit_tokens ?? 0;
}

async function loadKernel() {
  const data = await requestJson("/api/kernel");
  const connected = Boolean(data.connected);
  $("kpiConnected").textContent = connected ? "online" : "offline";
  $("kpiActiveCores").textContent = data?.top?.active_cores ?? 0;
  $("kpiQueue").textContent = data?.queue?.queue_size ?? 0;
  $("kpiInflight").textContent = data?.queue?.active_task_count ?? 0;
  $("kpiActiveSession").textContent = data?.active_session_id || "-";
  $("kpiTurns").textContent = data?.turn_count ?? 0;
  renderCoreList(data.cores);
  renderSessionsDropdowns(data.sessions, data.active_session_id);
  updateConsoleKnownSessions(data.cores, data.sessions);
  renderModels(data.models);
  renderUsage(data.token_usage);
  if (!connected && data.error) showToast(`Daemon offline: ${data.error}`);
}

async function callKernelAction(endpoint, payload, successMsg) {
  await requestJson(endpoint, {
    method: "POST",
    body: JSON.stringify(payload || {}),
  });
  showToast(successMsg);
  await loadKernel();
}

$("spawnBtn").addEventListener("click", async () => {
  const sid = $("sessionSelect").value;
  if (!sid) return showToast("Select a session first.");
  try {
    await callKernelAction("/api/kernel/spawn", { session_id: sid }, `Started core for ${sid}.`);
  } catch (e) { showToast(`Spawn failed: ${e.message}`); }
});

$("switchSessionBtn").addEventListener("click", async () => {
  const sid = $("sessionSelect").value;
  if (!sid) return showToast("Select a session first.");
  try {
    await callKernelAction("/api/kernel/session/switch", { session_id: sid }, `Active session is now ${sid}.`);
    activeSessionId = sid;
    $("activeSessionSelect").value = sid;
  } catch (e) { showToast(`Switch failed: ${e.message}`); }
});

$("clearContextBtn").addEventListener("click", async () => {
  if (!window.confirm("Clear context for the active session?")) return;
  try {
    await callKernelAction("/api/kernel/context/clear", {}, "Context cleared.");
  } catch (e) { showToast(`Clear failed: ${e.message}`); }
});

$("switchModelBtn").addEventListener("click", async () => {
  const name = $("modelSelect").value;
  if (!name) return showToast("Select a model first.");
  try {
    await callKernelAction("/api/kernel/model/switch", { name }, `Model switched to ${name}.`);
  } catch (e) { showToast(`Model switch failed: ${e.message}`); }
});

$("kernelRefreshBtn").addEventListener("click", async () => {
  try {
    await loadKernel();
    showToast("Kernel refreshed.");
  } catch (e) {
    showToast(`Refresh failed: ${e.message}`);
  }
});

// ===== Kernel console =====
const KERNEL_COMMANDS = [
  { cmd: "help", desc: "List available commands" },
  { cmd: "ping", desc: "Probe the daemon" },
  { cmd: "ps", desc: "List active cores (table)" },
  { cmd: "top", desc: "Kernel summary metrics" },
  { cmd: "queue", desc: "Scheduler queue state" },
  { cmd: "jobs", desc: "Automation cron jobs" },
  { cmd: "cron", desc: "Alias of jobs" },
  { cmd: "tasks", desc: "Recent agent tasks (tasks [limit])" },
  { cmd: "inspect ", desc: "inspect <session_id>" },
  { cmd: "sessions", desc: "List session IDs" },
  { cmd: "models", desc: "List configured models" },
  { cmd: "usage", desc: "Token usage for active session" },
  { cmd: "turns", desc: "Turn count for active session" },
  { cmd: "spawn ", desc: "spawn <session_id>" },
  { cmd: "cancel ", desc: "cancel <session_id>" },
  { cmd: "kill ", desc: "kill <session_id>" },
  { cmd: "attach ", desc: "attach <session_id> <message>" },
  { cmd: "user list", desc: "List memory users on a frontend" },
  { cmd: "user create ", desc: "user create <user_id> [--frontend cli] [--warm]" },
  { cmd: "/help", desc: "Slash: show daemon slash help" },
  { cmd: "/clear", desc: "Slash: clear context" },
  { cmd: "/usage", desc: "Slash: token usage summary" },
  { cmd: "/session list", desc: "Slash: list sessions" },
  { cmd: "/session new", desc: "Slash: new session" },
  { cmd: "/model list", desc: "Slash: list models" },
  { cmd: "/compress", desc: "Slash: compress context" },
  { cmd: "/dangerously status", desc: "Slash: dangerous mode" },
  { cmd: "/remote-status", desc: "Slash: remote workspace state" },
];

const consoleOutput = $("consoleOutput");
const consoleInput = $("consoleInput");
const consoleSuggestEl = $("consoleSuggest");
const consoleHistory = [];
let consoleHistoryIdx = -1;
let consoleSuggestItems = [];
let consoleSuggestIdx = 0;

function consoleScrollToBottom() {
  requestAnimationFrame(() => {
    consoleOutput.scrollTop = consoleOutput.scrollHeight;
    const last = consoleOutput.lastElementChild;
    if (last) {
      last.scrollIntoView({ block: "end", behavior: "auto" });
    }
  });
}

function consoleAppendEntry(command, body, kind) {
  const entry = document.createElement("div");
  entry.className = "console-entry";
  const cmdEl = document.createElement("div");
  cmdEl.className = "console-cmd";
  cmdEl.textContent = command;
  entry.appendChild(cmdEl);
  if (body != null) {
    const bodyEl = document.createElement("div");
    bodyEl.className = `console-body kind-${kind || "text"}`;
    bodyEl.textContent = body;
    entry.appendChild(bodyEl);
  }
  consoleOutput.appendChild(entry);
  consoleScrollToBottom();
}

function consoleHideSuggest() {
  consoleSuggestEl.classList.add("hidden");
  consoleSuggestItems = [];
  consoleSuggestIdx = 0;
}

function consoleRenderSuggest() {
  consoleSuggestEl.innerHTML = "";
  if (!consoleSuggestItems.length) {
    consoleHideSuggest();
    return;
  }
  const hint = document.createElement("div");
  hint.className = "console-suggest-hint";
  hint.innerHTML = `<kbd>Tab</kbd> accept · <kbd>↑/↓</kbd> navigate · <kbd>Esc</kbd> dismiss`;
  consoleSuggestEl.appendChild(hint);
  consoleSuggestItems.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = `console-suggest-item ${idx === consoleSuggestIdx ? "active" : ""}`;
    row.innerHTML = `<span class="cmd">${item.cmd}</span><span class="desc">${item.desc}</span>`;
    row.addEventListener("mousedown", (e) => {
      e.preventDefault();
      consoleAcceptSuggest(item);
    });
    consoleSuggestEl.appendChild(row);
  });
  consoleSuggestEl.classList.remove("hidden");
}

function consoleUpdateSuggest() {
  const value = consoleInput.value;
  if (!value.trim()) {
    consoleHideSuggest();
    return;
  }
  const q = value.toLowerCase();
  let items = KERNEL_COMMANDS.filter((c) => c.cmd.toLowerCase().startsWith(q));

  const sessionPrefixes = ["inspect ", "cancel ", "kill ", "spawn ", "attach "];
  const matchedPrefix = sessionPrefixes.find((p) => q.startsWith(p));
  if (matchedPrefix && consoleKnownSessions.length) {
    const partial = value.slice(matchedPrefix.length).toLowerCase();
    const verb = matchedPrefix.trim();
    items = consoleKnownSessions
      .filter((sid) => !partial || sid.toLowerCase().startsWith(partial))
      .slice(0, 12)
      .map((sid) => ({
        cmd: `${verb} ${sid}${verb === "attach" ? " " : ""}`,
        desc: sid,
      }));
  }

  consoleSuggestItems = items;
  if (!consoleSuggestItems.length) {
    consoleHideSuggest();
    return;
  }
  if (consoleSuggestItems.length === 1 && consoleSuggestItems[0].cmd.toLowerCase() === q.trim()) {
    consoleHideSuggest();
    return;
  }
  if (consoleSuggestIdx >= consoleSuggestItems.length) consoleSuggestIdx = 0;
  consoleRenderSuggest();
}

function consoleAcceptSuggest(item) {
  consoleInput.value = item.cmd.endsWith(" ") ? item.cmd : item.cmd + " ";
  consoleInput.focus();
  consoleInput.selectionStart = consoleInput.selectionEnd = consoleInput.value.length;
  consoleHideSuggest();
}

async function runKernelCommand(command) {
  const trimmed = command.trim();
  if (!trimmed) return;
  consoleHistory.push(trimmed);
  if (consoleHistory.length > 200) consoleHistory.shift();
  consoleHistoryIdx = consoleHistory.length;

  const sessionId = activeSessionId || $("activeSessionSelect").value || "";
  consoleAppendEntry(trimmed, "running…", "text");
  const lastEntry = consoleOutput.lastElementChild;
  try {
    const data = await requestJson("/api/kernel/exec", {
      method: "POST",
      body: JSON.stringify({ command: trimmed, session_id: sessionId || undefined }),
    });
    const body = lastEntry.querySelector(".console-body");
    body.textContent = data.output || "(no output)";
    body.className = `console-body kind-${data.ok ? data.kind || "text" : "error"}`;
    consoleScrollToBottom();
    if (data.ok && (trimmed === "ps" || trimmed === "top" || trimmed === "queue"
      || trimmed.startsWith("kill ") || trimmed.startsWith("cancel ")
      || trimmed.startsWith("spawn ") || trimmed === "clear"
      || trimmed.startsWith("/clear") || trimmed.startsWith("/session")
      || trimmed.startsWith("/model"))) {
      // mutating/state-affecting commands → refresh KPI/cores
      loadKernel().catch(() => {});
    }
  } catch (error) {
    const body = lastEntry.querySelector(".console-body");
    body.textContent = `error: ${error.message}`;
    body.className = "console-body kind-error";
    consoleScrollToBottom();
  }
}

$("consoleForm").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const cmd = consoleInput.value;
  consoleInput.value = "";
  consoleHideSuggest();
  await runKernelCommand(cmd);
});

$("consoleClearBtn").addEventListener("click", () => {
  consoleOutput.innerHTML = '<div class="console-welcome">Cleared. Type <code>help</code> for commands.</div>';
});

consoleInput.addEventListener("input", consoleUpdateSuggest);
consoleInput.addEventListener("blur", () => {
  window.setTimeout(consoleHideSuggest, 120);
});

consoleInput.addEventListener("keydown", (evt) => {
  if (!consoleSuggestEl.classList.contains("hidden")) {
    if (evt.key === "ArrowDown") {
      evt.preventDefault();
      consoleSuggestIdx = (consoleSuggestIdx + 1) % consoleSuggestItems.length;
      consoleRenderSuggest();
      return;
    }
    if (evt.key === "ArrowUp") {
      evt.preventDefault();
      consoleSuggestIdx = (consoleSuggestIdx - 1 + consoleSuggestItems.length) % consoleSuggestItems.length;
      consoleRenderSuggest();
      return;
    }
    if (evt.key === "Tab") {
      evt.preventDefault();
      consoleAcceptSuggest(consoleSuggestItems[consoleSuggestIdx]);
      return;
    }
    if (evt.key === "Escape") {
      evt.preventDefault();
      consoleHideSuggest();
      return;
    }
  }

  if (evt.key === "ArrowUp") {
    if (!consoleHistory.length) return;
    evt.preventDefault();
    consoleHistoryIdx = Math.max(0, consoleHistoryIdx - 1);
    consoleInput.value = consoleHistory[consoleHistoryIdx] || "";
    consoleInput.selectionStart = consoleInput.selectionEnd = consoleInput.value.length;
    return;
  }
  if (evt.key === "ArrowDown") {
    if (!consoleHistory.length) return;
    evt.preventDefault();
    consoleHistoryIdx = Math.min(consoleHistory.length, consoleHistoryIdx + 1);
    consoleInput.value =
      consoleHistoryIdx >= consoleHistory.length ? "" : consoleHistory[consoleHistoryIdx];
    consoleInput.selectionStart = consoleInput.selectionEnd = consoleInput.value.length;
    return;
  }
  if (evt.key === "Tab") {
    evt.preventDefault();
    consoleUpdateSuggest();
    if (consoleSuggestItems.length === 1) {
      consoleAcceptSuggest(consoleSuggestItems[0]);
    }
  }
});

document.addEventListener("click", (evt) => {
  const btn = evt.target.closest("[data-console-cmd]");
  if (!btn) return;
  evt.preventDefault();
  const cmd = btn.getAttribute("data-console-cmd");
  if (!cmd || cmd.includes("SESSION")) {
    consoleInput.focus();
    consoleInput.value = "inspect ";
    consoleUpdateSuggest();
    return;
  }
  runKernelCommand(cmd);
  consoleInput.focus();
});

// ===== Chat =====
const chatWindow = $("chatWindow");

// ── File upload ──
const chatFileInput = $("chatFileInput");
const chatAttachBtn = $("chatAttachBtn");
const chatAttachments = $("chatAttachments");
const uploadedFiles = [];  // { filename, path }

chatAttachBtn?.addEventListener("click", () => chatFileInput?.click());

chatFileInput?.addEventListener("change", async () => {
  const files = Array.from(chatFileInput.files || []);
  chatFileInput.value = "";
  for (const file of files) {
    await uploadChatFile(file);
  }
});

async function uploadChatFile(file) {
  const chip = document.createElement("span");
  chip.className = "attach-chip uploading";
  chip.innerHTML = `<span class="attach-name">${escapeHtml(file.name)}</span><span class="attach-remove">×</span>`;
  chatAttachments.appendChild(chip);
  chatAttachments.classList.remove("hidden");

  try {
    const form = new FormData();
    form.append("file", file);
    const resp = await fetch(apiUrl("/api/chat/upload"), {
      method: "POST",
      credentials: "same-origin",
      body: form,
    });
    if (resp.status === 401) { redirectToLogin(); throw new Error("Unauthorized"); }
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.detail || "upload failed");
    uploadedFiles.push({ filename: data.filename, path: data.path });
    chip.classList.remove("uploading");
    chip.querySelector(".attach-remove").addEventListener("click", () => {
      const idx = uploadedFiles.findIndex((f) => f.path === data.path);
      if (idx >= 0) uploadedFiles.splice(idx, 1);
      chip.remove();
      if (!uploadedFiles.length) chatAttachments.classList.add("hidden");
    });
  } catch (err) {
    chip.remove();
    if (!uploadedFiles.length) chatAttachments.classList.add("hidden");
    showToast(`Upload failed: ${err.message}`);
  }
}

// ── Keyboard-aware input on mobile ──
// iOS Safari anchors position:fixed to the layout viewport, which
// doesn't resize when the keyboard opens.  We adjust `bottom`
// dynamically so the input always sits right above the keyboard.
// iOS also auto-scrolls on focus — we cancel that so our calculation
// (which assumes offsetTop≈0) is correct.
(function setupKeyboardHandling() {
  if (!window.visualViewport) return;
  const input = document.querySelector(".chat-input");
  const textarea = document.querySelector(".chat-input textarea");
  if (!input) return;

  const isMobile = () => window.matchMedia("(max-width: 720px)").matches;

  function positionInput() {
    if (!isMobile()) {
      input.style.bottom = "";
      return;
    }
    const vv = window.visualViewport;
    const kb = window.innerHeight - vv.height - vv.offsetTop;
    input.style.bottom = Math.max(0, kb) + "px";
  }

  window.visualViewport.addEventListener("resize", positionInput);
  window.visualViewport.addEventListener("scroll", positionInput);

  if (textarea) {
    textarea.addEventListener("focus", () => {
      if (!isMobile()) return;
      // 1) Fight iOS auto-scroll for the first ~500ms
      let count = 0;
      const cancel = () => { count = 999; };
      const fight = () => {
        if (count >= 10) return;
        window.scrollTo(0, 0);
        count++;
        setTimeout(fight, 50);
      };
      fight();
      textarea.addEventListener("blur", cancel, { once: true });

      // 2) Proactively reposition — visualViewport events fire late on iOS
      [100, 250, 400, 550].forEach(ms => {
        setTimeout(positionInput, ms);
      });
    });

    // 3) On blur, immediately reset bottom so the bar slides back smoothly
    textarea.addEventListener("blur", () => {
      if (!isMobile()) return;
      // Short delay to let iOS keyboard dismiss animation start
      setTimeout(() => {
        input.style.bottom = "";
      }, 50);
    });
  }
})();

// ── Chat history persistence (localStorage) ──

function chatStorageKey(sessionId) {
  return `chat_history_${sessionId || "__default"}`;
}

function saveChatPair(sessionId, userText, assistantText) {
  try {
    const key = chatStorageKey(sessionId);
    const history = JSON.parse(localStorage.getItem(key) || "[]");
    history.push({ role: "user", text: userText });
    history.push({ role: "assistant", text: assistantText });
    // Keep last 100 messages per session
    if (history.length > 100) history.splice(0, history.length - 100);
    localStorage.setItem(key, JSON.stringify(history));
  } catch { /* storage full or unavailable */ }
}

function loadChatHistory(sessionId) {
  try {
    const key = chatStorageKey(sessionId);
    return JSON.parse(localStorage.getItem(key) || "[]");
  } catch {
    return [];
  }
}

function clearChatHistory(sessionId) {
  try {
    const key = chatStorageKey(sessionId);
    localStorage.removeItem(key);
  } catch { /* ignore */ }
}

function restoreChatMessages(sessionId) {
  // Remove the empty state if present
  const empty = chatWindow.querySelector(".empty");
  if (empty) empty.remove();
  // Clear existing messages
  chatWindow.querySelectorAll(".message").forEach((el) => el.remove());
  const history = loadChatHistory(sessionId);
  history.forEach((entry) => {
    if (entry.role === "user") {
      const wrap = makeMessage("user");
      const bubble = document.createElement("div");
      bubble.className = "bubble user";
      bubble.innerHTML = `<span class="role">you</span><div class="body"></div>`;
      bubble.querySelector(".body").textContent = entry.text;
      wrap.appendChild(bubble);
      chatWindow.appendChild(wrap);
    } else {
      const wrap = makeMessage("assistant");
      wrap.innerHTML = `
        <div class="bubble assistant">
          <span class="role">macchiato</span>
          <div class="body"></div>
        </div>
      `;
      const body = wrap.querySelector(".body");
      body.innerHTML = renderMarkdown(entry.text);
      chatWindow.appendChild(wrap);
    }
  });
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

function escapeHtml(text) {
  return (text || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function safeUrl(url) {
  const cleaned = (url || "").trim();
  if (/^(javascript|data|vbscript):/i.test(cleaned)) return "#";
  return cleaned;
}

// Lightweight markdown renderer optimized for streaming chat output.
function renderMarkdown(text) {
  if (!text) return "";
  let src = String(text);

  // 1. Pull out fenced code blocks first to keep them verbatim.
  const codeBlocks = [];
  src = src.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push({ lang: (lang || "").trim(), code });
    return `\u0000CODE${codeBlocks.length - 1}\u0000`;
  });

  // 1b. Pull out math blocks ($$...$$ and $...$) to protect from HTML escaping.
  const mathBlocks = [];
  // Block-level: $$...$$
  src = src.replace(/\$\$([\s\S]*?)\$\$/g, (_, tex) => {
    mathBlocks.push({ tex: tex.trim(), display: true });
    return `\u0000MATH${mathBlocks.length - 1}\u0000`;
  });
  // Inline: $...$ (but not $$ which is already handled)
  src = src.replace(/(?<!\$)\$(?!\$)([^\$\n]+?)\$(?!\$)/g, (_, tex) => {
    mathBlocks.push({ tex: tex.trim(), display: false });
    return `\u0000MATH${mathBlocks.length - 1}\u0000`;
  });

  // 2. Escape HTML so the model can't inject tags.
  src = escapeHtml(src);

  // 3. Inline code spans.
  src = src.replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);

  // 4. Bold and italic.
  src = src.replace(/\*\*([^*\n][^*]*?)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[\s(])\*([^*\n]+?)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
  src = src.replace(/(^|[\s(])_([^_\n]+?)_(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");

  // 5. Links and bare URLs.
  src = src.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) =>
    `<a href="${safeUrl(url)}" target="_blank" rel="noopener">${label}</a>`
  );
  src = src.replace(/(^|[\s(])(https?:\/\/[^\s<]+)/g, (_, pre, url) =>
    `${pre}<a href="${safeUrl(url)}" target="_blank" rel="noopener">${url}</a>`
  );

  // 6. Headings + blockquotes + horizontal rule (line-anchored).
  src = src.replace(/^######\s+(.+)$/gm, "<h6>$1</h6>");
  src = src.replace(/^#####\s+(.+)$/gm, "<h5>$1</h5>");
  src = src.replace(/^####\s+(.+)$/gm, "<h4>$1</h4>");
  src = src.replace(/^###\s+(.+)$/gm, "<h3>$1</h3>");
  src = src.replace(/^##\s+(.+)$/gm, "<h2>$1</h2>");
  src = src.replace(/^#\s+(.+)$/gm, "<h1>$1</h1>");
  src = src.replace(/^&gt;\s?(.+)$/gm, "<blockquote>$1</blockquote>");
  src = src.replace(/^---+$/gm, "<hr />");

  // 7. List blocks (ul/ol). Scan line-by-line.
  const lines = src.split("\n");
  const buf = [];
  let listType = null;
  const closeList = () => {
    if (listType) {
      buf.push(`</${listType}>`);
      listType = null;
    }
  };
  for (const line of lines) {
    const ul = /^\s*[-*]\s+(.+)$/.exec(line);
    const ol = /^\s*\d+\.\s+(.+)$/.exec(line);
    if (ul) {
      if (listType !== "ul") {
        closeList();
        buf.push("<ul>");
        listType = "ul";
      }
      buf.push(`<li>${ul[1]}</li>`);
    } else if (ol) {
      if (listType !== "ol") {
        closeList();
        buf.push("<ol>");
        listType = "ol";
      }
      buf.push(`<li>${ol[1]}</li>`);
    } else {
      closeList();
      buf.push(line);
    }
  }
  closeList();
  src = buf.join("\n");

  // 8. Paragraphs and line breaks.
  const blocks = src.split(/\n{2,}/).map((block) => {
    const trimmed = block.trim();
    if (!trimmed) return "";
    if (/^<(h\d|ul|ol|li|blockquote|pre|hr|table)/.test(trimmed)) return trimmed;
    return `<p>${trimmed.replace(/\n/g, "<br>")}</p>`;
  });
  src = blocks.join("\n");

  // 9. Restore code blocks.
  src = src.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => {
    const item = codeBlocks[+i];
    const langAttr = item.lang ? ` data-lang="${escapeHtml(item.lang)}"` : "";
    return `<pre><code${langAttr}>${escapeHtml(item.code)}</code></pre>`;
  });

  // 10. Restore math blocks with KaTeX rendering.
  src = src.replace(/\u0000MATH(\d+)\u0000/g, (_, i) => {
    const item = mathBlocks[+i];
    try {
      return katex.renderToString(item.tex, { displayMode: item.display, throwOnError: false });
    } catch {
      return `<code>${escapeHtml(item.tex)}</code>`;
    }
  });

  return src;
}

function makeMessage(role) {
  const empty = chatWindow.querySelector(".empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = `message ${role}`;
  return wrap;
}

function makeUserBubble(text) {
  const wrap = makeMessage("user");
  const bubble = document.createElement("div");
  bubble.className = "bubble user";
  bubble.innerHTML = `<span class="role">you</span>`;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  bubble.appendChild(body);
  wrap.appendChild(bubble);
  chatWindow.appendChild(wrap);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

function makeAssistantBubble() {
  const wrap = makeMessage("assistant");
  wrap.innerHTML = `
    <div class="tools hidden"></div>
    <div class="interactive hidden"></div>
    <details class="reasoning hidden">
      <summary>Reasoning</summary>
      <div class="reasoning-body"></div>
    </details>
    <div class="bubble assistant">
      <span class="role">macchiato</span>
      <div class="body streaming"></div>
    </div>
  `;
  chatWindow.appendChild(wrap);
  chatWindow.scrollTop = chatWindow.scrollHeight;
  return {
    wrap,
    bubble: wrap.querySelector(".bubble"),
    body: wrap.querySelector(".body"),
    reasoning: wrap.querySelector(".reasoning"),
    reasoningBody: wrap.querySelector(".reasoning-body"),
    tools: wrap.querySelector(".tools"),
    interactive: wrap.querySelector(".interactive"),
    toolMap: new Map(),
    rawText: "",
  };
}

function appendDelta(ctx, delta) {
  ctx.rawText += delta;
  ctx.body.innerHTML = renderMarkdown(ctx.rawText);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

function formatJson(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function summarizeArguments(args) {
  if (!args || typeof args !== "object") return "";
  const keys = Object.keys(args);
  if (!keys.length) return "()";
  const first = keys[0];
  const rendered = typeof args[first] === "string"
    ? `"${args[first].slice(0, 40)}${args[first].length > 40 ? "…" : ""}"`
    : JSON.stringify(args[first]);
  return `${first}=${rendered}${keys.length > 1 ? `, +${keys.length - 1}` : ""}`;
}

function addToolCall(ctx, evt) {
  ctx.tools.classList.remove("hidden");
  const card = document.createElement("details");
  card.className = "tool-card pending";
  card.innerHTML = `
    <summary>
      <span class="tool-icon">fn</span>
      <span class="tool-name"></span>
      <span class="tool-meta"></span>
    </summary>
    <div class="tool-body">
      <h4>Arguments</h4>
      <pre class="tool-args"></pre>
      <div class="tool-result-section hidden">
        <h4>Result</h4>
        <pre class="tool-result"></pre>
        <div class="tool-error hidden"></div>
      </div>
    </div>
  `;
  card.querySelector(".tool-name").textContent = evt.name || "(tool)";
  card.querySelector(".tool-meta").innerHTML = `<span class="tool-status pending">running…</span>`;
  card.querySelector(".tool-args").textContent = formatJson(evt.arguments);
  ctx.tools.appendChild(card);
  ctx.toolMap.set(evt.tool_call_id, card);
}

function finishToolCall(ctx, evt) {
  const card = ctx.toolMap.get(evt.tool_call_id);
  if (!card) return;
  card.classList.remove("pending");
  card.classList.add(evt.success ? "success" : "failed");
  const meta = card.querySelector(".tool-meta");
  const duration = evt.duration_ms != null ? `${evt.duration_ms} ms` : "";
  meta.innerHTML = `<span>${evt.success ? "ok" : "failed"}</span>${duration ? ` · <span>${duration}</span>` : ""}`;
  const icon = card.querySelector(".tool-icon");
  icon.textContent = evt.success ? "✓" : "!";
  const section = card.querySelector(".tool-result-section");
  section.classList.remove("hidden");
  const result = card.querySelector(".tool-result");
  const preview = evt.data_preview || evt.message || "";
  result.textContent = preview;
  if (!evt.success && evt.error) {
    const err = card.querySelector(".tool-error");
    err.textContent = evt.error;
    err.classList.remove("hidden");
  }
}

function appendReasoning(ctx, delta) {
  ctx.reasoning.classList.remove("hidden");
  ctx.reasoningBody.appendChild(document.createTextNode(delta));
}

async function resolvePermissionDecision(permissionId, decision) {
  return requestJson("/api/permission/resolve", {
    method: "POST",
    body: JSON.stringify({
      permission_id: permissionId,
      allowed: Boolean(decision.allowed),
      persist_acl: Boolean(decision.persist_acl),
      clarify_requested: Boolean(decision.clarify_requested),
      user_instruction: decision.user_instruction || undefined,
    }),
  });
}

async function resolveAskUserBatch(batchId, answers) {
  return requestJson("/api/ask-user/resolve", {
    method: "POST",
    body: JSON.stringify({ batch_id: batchId, answers }),
  });
}

function addPermissionRequest(ctx, event) {
  const pid = event.permission_id;
  const payload = event.payload || {};
  if (!pid || !ctx.interactive) return;

  ctx.interactive.classList.remove("hidden");
  const card = document.createElement("div");
  card.className = "permission-card pending";
  card.dataset.permissionId = pid;

  const summary = payload.summary || "Permission required";
  const kind = payload.kind || "";
  const command = payload.command || "";
  const cwd = payload.cwd || "";
  const risks = Array.isArray(payload.risk_reasons) ? payload.risk_reasons : [];
  const grants = Array.isArray(payload.path_grants) ? payload.path_grants : [];
  const autoExec = Boolean(payload.auto_execute_after_approval);

  let grantsHtml = "";
  grants.forEach((g) => {
    if (!g || typeof g !== "object") return;
    grantsHtml += `<li><code>${g.access_mode || "write"}</code> ${escapeHtml(g.path_prefix || "")}</li>`;
  });

  card.innerHTML = `
    <div class="permission-head">
      <span class="permission-icon">🔐</span>
      <div>
        <strong>Permission required</strong>
        <div class="permission-sub">${escapeHtml(kind)}${payload.tool_name ? ` · ${escapeHtml(payload.tool_name)}` : ""}</div>
      </div>
    </div>
    <div class="permission-summary">${escapeHtml(summary)}</div>
    ${cwd ? `<div class="permission-meta"><span>cwd</span><code>${escapeHtml(cwd)}</code></div>` : ""}
    ${command ? `<pre class="permission-command">${escapeHtml(command)}</pre>` : ""}
    ${risks.length ? `<ul class="permission-risks">${risks.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : ""}
    ${grantsHtml ? `<ul class="permission-grants">${grantsHtml}</ul>` : ""}
    ${autoExec ? `<p class="permission-note muted small">Will auto-continue the original action after approval.</p>` : ""}
    <div class="permission-clarify hidden">
      <textarea class="control permission-clarify-input" rows="2" placeholder="What should the agent clarify or change?"></textarea>
    </div>
    <div class="permission-actions">
      <button type="button" class="btn primary small" data-perm-action="once">Allow once</button>
      <button type="button" class="btn ghost small" data-perm-action="always">Always allow</button>
      <button type="button" class="btn ghost small" data-perm-action="clarify">Need clarification</button>
      <button type="button" class="btn danger small" data-perm-action="deny">Deny</button>
    </div>
    <div class="permission-status muted small"></div>
  `;

  const setResolved = (label, ok) => {
    card.classList.remove("pending");
    card.classList.add(ok ? "resolved-ok" : "resolved-deny");
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    const status = card.querySelector(".permission-status");
    if (status) status.textContent = label;
    // Auto-collapse after a short delay
    setTimeout(() => {
      card.classList.add("collapsing");
      card.addEventListener("transitionend", () => {
        card.remove();
      }, { once: true });
      // Fallback: force remove if transition doesn't fire
      setTimeout(() => card.remove(), 500);
    }, 800);
  };

  card.querySelectorAll("[data-perm-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.getAttribute("data-perm-action");
      const clarifyBox = card.querySelector(".permission-clarify");
      let decision = { allowed: false, persist_acl: false, clarify_requested: false };
      if (action === "once") {
        decision = { allowed: true, persist_acl: false, clarify_requested: false };
      } else if (action === "always") {
        decision = { allowed: true, persist_acl: true, clarify_requested: false };
      } else if (action === "deny") {
        decision = { allowed: false, persist_acl: false, clarify_requested: false };
      } else if (action === "clarify") {
        if (clarifyBox?.classList.contains("hidden")) {
          clarifyBox.classList.remove("hidden");
          clarifyBox.querySelector("textarea")?.focus();
          return;
        }
        const note = clarifyBox?.querySelector("textarea")?.value?.trim() || "";
        decision = {
          allowed: false,
          persist_acl: false,
          clarify_requested: true,
          user_instruction: note || undefined,
        };
      }
      btn.disabled = true;
      try {
        const res = await resolvePermissionDecision(pid, decision);
        if (!res.ok) throw new Error("permission not pending");
        if (action === "once") setResolved("Allowed once.", true);
        else if (action === "always") setResolved("Added to allowlist.", true);
        else if (action === "deny") setResolved("Denied.", false);
        else setResolved("Clarification requested.", false);
      } catch (error) {
        btn.disabled = false;
        showToast(`Permission resolve failed: ${error.message}`);
      }
    });
  });

  ctx.interactive.appendChild(card);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

function addAskUserRequest(ctx, event) {
  const batchId = event.batch_id;
  const payload = event.payload || {};
  const questions = Array.isArray(payload.questions) ? payload.questions : [];
  if (!batchId || !questions.length || !ctx.interactive) return;

  ctx.interactive.classList.remove("hidden");
  const card = document.createElement("div");
  card.className = "ask-user-card pending";
  card.dataset.batchId = batchId;

  const blocks = questions.map((q, qi) => {
    const qid = q.id || `q${qi + 1}`;
    const text = q.text || q.question || `Question ${qi + 1}`;
    const options = Array.isArray(q.options) ? q.options : [];
    const allowCustom = q.allow_custom !== false;
    let optionsHtml = "";
    if (options.length) {
      optionsHtml = `<div class="ask-options">${options.map((opt, oi) => {
        const val = opt.value || opt.label || String(oi);
        const label = opt.text || opt.label || val;
        return `<label class="ask-option"><input type="radio" name="ask-${batchId}-${qid}" value="${escapeHtml(val)}" /> ${escapeHtml(label)}</label>`;
      }).join("")}</div>`;
    }
    const customHtml = allowCustom
      ? `<input class="control ask-custom" data-qid="${escapeHtml(qid)}" placeholder="Or type a custom answer…" />`
      : "";
    return `
      <div class="ask-block" data-qid="${escapeHtml(qid)}">
        <div class="ask-q">${qi + 1}. ${escapeHtml(text)}</div>
        ${optionsHtml}
        ${customHtml}
      </div>
    `;
  }).join("");

  card.innerHTML = `
    <div class="permission-head">
      <span class="permission-icon">❓</span>
      <div><strong>Agent question</strong><div class="permission-sub">Answer to continue</div></div>
    </div>
    ${blocks}
    <div class="permission-actions">
      <button type="button" class="btn primary small ask-submit">Submit answers</button>
      <button type="button" class="btn ghost small ask-skip">Skip all</button>
    </div>
    <div class="permission-status muted small"></div>
  `;

  const collectAnswers = (skip) => {
    if (skip) {
      return questions.map((q, qi) => ({
        question_id: q.id || `q${qi + 1}`,
        selected_option: null,
        custom_text: null,
      }));
    }
    const answers = [];
    questions.forEach((q, qi) => {
      const qid = q.id || `q${qi + 1}`;
      const block = card.querySelector(`.ask-block[data-qid="${CSS.escape(qid)}"]`);
      if (!block) return;
      const checked = block.querySelector(`input[type="radio"]:checked`);
      const custom = block.querySelector(".ask-custom")?.value?.trim() || "";
      answers.push({
        question_id: qid,
        selected_option: checked ? checked.value : null,
        custom_text: custom || null,
      });
    });
    return answers;
  };

  const finish = async (skip) => {
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const answers = collectAnswers(skip);
      const res = await resolveAskUserBatch(batchId, answers);
      if (!res.ok) throw new Error("ask_user not pending");
      card.classList.remove("pending");
      card.classList.add("resolved-ok");
      const status = card.querySelector(".permission-status");
      if (status) status.textContent = skip ? "Skipped." : "Answers submitted.";
    } catch (error) {
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      showToast(`Ask-user resolve failed: ${error.message}`);
    }
  };

  card.querySelector(".ask-submit")?.addEventListener("click", () => finish(false));
  card.querySelector(".ask-skip")?.addEventListener("click", () => finish(true));
  ctx.interactive.appendChild(card);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

async function streamChat(text, sessionId, ctx) {
  const resp = await fetch(apiUrl("/api/chat/stream"), {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, session_id: sessionId || undefined }),
  });
  if (resp.status === 401) {
    redirectToLogin();
    throw new Error("Unauthorized");
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let receivedFinal = false;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      switch (event.type) {
        case "assistant_delta":
          appendDelta(ctx, event.delta || "");
          break;
        case "reasoning_delta":
          appendReasoning(ctx, event.delta || "");
          break;
        case "trace": {
          const data = event.data || {};
          if (data.type === "tool_call") addToolCall(ctx, data);
          else if (data.type === "tool_result") finishToolCall(ctx, data);
          break;
        }
        case "permission_request":
          addPermissionRequest(ctx, event);
          break;
        case "ask_user":
          addAskUserRequest(ctx, event);
          break;
        case "system": {
          ctx.bubble.classList.remove("assistant");
          ctx.bubble.classList.add("system");
          const roleEl = ctx.bubble.querySelector(".role");
          if (roleEl) roleEl.textContent = "slash";
          ctx.rawText = event.message || "";
          ctx.body.textContent = ctx.rawText;
          break;
        }
        case "final":
          receivedFinal = true;
          if (event.output_text && !ctx.rawText) {
            ctx.rawText = event.output_text;
            ctx.body.innerHTML = renderMarkdown(ctx.rawText);
          }
          break;
        case "error":
          ctx.body.textContent = `Error: ${event.message || "unknown error"}`;
          break;
        default:
          break;
      }
    }
  }
  ctx.body.classList.remove("streaming");
  return receivedFinal;
}

async function nonStreamChat(text, sessionId, ctx) {
  const data = await requestJson("/api/chat", {
    method: "POST",
    body: JSON.stringify({ text, session_id: sessionId || undefined }),
  });
  ctx.rawText = data.output_text || "(empty response)";
  ctx.body.innerHTML = renderMarkdown(ctx.rawText);
  ctx.body.classList.remove("streaming");
}

$("chatForm").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  let text = $("chatText").value.trim();
  // Append uploaded file info
  if (uploadedFiles.length) {
    const fileList = uploadedFiles.map((f) => `[file: ${f.filename} → ${f.path}]`).join("\n");
    text = text ? `${text}\n\n${fileList}` : fileList;
    // Clear attachments
    uploadedFiles.length = 0;
    chatAttachments.innerHTML = "";
    chatAttachments.classList.add("hidden");
  }
  if (!text) return;
  const sessionId = activeSessionId || $("activeSessionSelect").value || "";
  makeUserBubble(text);
  $("chatText").value = "";
  hideSuggest();

  // Handle /clear: also wipe history from localStorage
  if (text.startsWith("/clear")) {
    clearChatHistory(sessionId);
  }

  const ctx = makeAssistantBubble();
  $("chatSendBtn").disabled = true;
  try {
    if ($("streamToggle").checked) {
      const ok = await streamChat(text, sessionId, ctx);
      if (ok && ctx.rawText) {
        saveChatPair(sessionId, text, ctx.rawText);
      }
    } else {
      await nonStreamChat(text, sessionId, ctx);
      if (ctx.rawText) {
        saveChatPair(sessionId, text, ctx.rawText);
      }
    }
    await loadKernel();
  } catch (error) {
    ctx.body.classList.remove("streaming");
    ctx.body.textContent = `Error: ${error.message}`;
  } finally {
    $("chatSendBtn").disabled = false;
  }
});

// ----- Slash command autocomplete -----
const SLASH_COMMANDS = [
  { cmd: "/help", desc: "Show available slash commands" },
  { cmd: "/clear", desc: "Clear conversation history" },
  { cmd: "/compress", desc: "Compress context (optional: keep N recent turns)" },
  { cmd: "/interrupt", desc: "Cancel the current turn" },
  { cmd: "/cancel", desc: "Alias of /interrupt" },
  { cmd: "/stop", desc: "Alias of /interrupt" },
  { cmd: "/dangerously on", desc: "Enable dangerous mode (bypass approvals)" },
  { cmd: "/dangerously off", desc: "Disable dangerous mode" },
  { cmd: "/dangerously status", desc: "Show dangerous mode state" },
  { cmd: "/session", desc: "Show current session" },
  { cmd: "/session list", desc: "List sessions in scope" },
  { cmd: "/session new", desc: "Create a new session" },
  { cmd: "/session switch", desc: "Switch active session" },
  { cmd: "/session delete", desc: "Delete a session" },
  { cmd: "/model", desc: "Show current model" },
  { cmd: "/model list", desc: "List available models" },
  { cmd: "/usage", desc: "Show token usage" },
  { cmd: "/remote-use", desc: "Use a remote workspace" },
  { cmd: "/remote-status", desc: "Show remote workspace state" },
  { cmd: "/remote-release", desc: "Release the remote workspace" },
];

const suggestEl = $("slashSuggest");
let suggestItems = [];
let suggestIdx = 0;

function suggestVisible() {
  return !suggestEl.classList.contains("hidden");
}

function hideSuggest() {
  suggestEl.classList.add("hidden");
  suggestItems = [];
  suggestIdx = 0;
}

function renderSuggest() {
  suggestEl.innerHTML = "";
  if (!suggestItems.length) {
    hideSuggest();
    return;
  }
  const hint = document.createElement("div");
  hint.className = "slash-suggest-hint";
  hint.innerHTML = `↑/↓ to navigate · <kbd>Tab</kbd> or <kbd>Enter</kbd> to accept · <kbd>Esc</kbd> to dismiss`;
  suggestEl.appendChild(hint);
  suggestItems.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = `slash-suggest-item ${idx === suggestIdx ? "active" : ""}`;
    row.innerHTML = `<span class="cmd">${item.cmd}</span><span class="desc">${item.desc}</span>`;
    row.addEventListener("mousedown", (e) => {
      e.preventDefault();
      acceptSuggest(item);
    });
    suggestEl.appendChild(row);
  });
  suggestEl.classList.remove("hidden");
}

function updateSuggest() {
  const value = $("chatText").value;
  if (!value.startsWith("/") || value.includes("\n")) {
    hideSuggest();
    return;
  }
  const q = value.toLowerCase();
  suggestItems = SLASH_COMMANDS.filter((c) => c.cmd.toLowerCase().startsWith(q));
  if (!suggestItems.length) {
    hideSuggest();
    return;
  }
  if (suggestItems.length === 1 && suggestItems[0].cmd.toLowerCase() === q) {
    hideSuggest();
    return;
  }
  if (suggestIdx >= suggestItems.length) suggestIdx = 0;
  renderSuggest();
}

function acceptSuggest(item) {
  const el = $("chatText");
  el.value = item.cmd + " ";
  el.focus();
  el.selectionStart = el.selectionEnd = el.value.length;
  hideSuggest();
}

// Auto-resize on mobile: grow textarea height as user types
function autoResizeTextarea(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 120) + "px";
}

$("chatText").addEventListener("input", (evt) => {
  autoResizeTextarea(evt.target);
  updateSuggest();
});
$("chatText").addEventListener("focus", updateSuggest);
$("chatText").addEventListener("blur", () => {
  window.setTimeout(hideSuggest, 120);
});

$("chatText").addEventListener("keydown", (evt) => {
  // Suggestion navigation first
  if (suggestVisible()) {
    if (evt.key === "ArrowDown") {
      evt.preventDefault();
      suggestIdx = (suggestIdx + 1) % suggestItems.length;
      renderSuggest();
      return;
    }
    if (evt.key === "ArrowUp") {
      evt.preventDefault();
      suggestIdx = (suggestIdx - 1 + suggestItems.length) % suggestItems.length;
      renderSuggest();
      return;
    }
    if (evt.key === "Tab" || (evt.key === "Enter" && !evt.metaKey && !evt.ctrlKey && !evt.shiftKey && !evt.isComposing)) {
      evt.preventDefault();
      acceptSuggest(suggestItems[suggestIdx]);
      return;
    }
    if (evt.key === "Escape") {
      evt.preventDefault();
      hideSuggest();
      return;
    }
  }

  if (evt.key !== "Enter") return;
  if (evt.isComposing) return;
  if (evt.metaKey || evt.ctrlKey || evt.shiftKey) {
    evt.preventDefault();
    const el = evt.target;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    el.value = el.value.slice(0, start) + "\n" + el.value.slice(end);
    el.selectionStart = el.selectionEnd = start + 1;
    return;
  }
  evt.preventDefault();
  $("chatForm").dispatchEvent(new Event("submit"));
});

// ===== Global refresh =====
$("globalRefresh").addEventListener("click", async () => {
  try {
    await Promise.all([pollHealth(), loadKernel(), loadBackups()]);
    showToast("Refreshed.");
  } catch (e) {
    showToast(`Refresh failed: ${e.message}`);
  }
});

// ===== Boot =====
function refreshAuthUi(status) {
  const userEl = $("authUser");
  const logoutBtn = $("authLogout");
  if (!userEl || !logoutBtn) return;
  if (!status?.auth_required) {
    userEl.classList.add("hidden");
    logoutBtn.classList.add("hidden");
    return;
  }
  userEl.classList.remove("hidden");
  logoutBtn.classList.remove("hidden");
  userEl.textContent = status.username || "signed in";
}

async function boot() {
  try {
    const authResp = await fetch(apiUrl("/api/auth/status"), { credentials: "same-origin" });
    const auth = await authResp.json().catch(() => ({}));
    if (auth.auth_required && !auth.authenticated) {
      redirectToLogin();
      return;
    }
    refreshAuthUi(auth);
    await pollHealth();
    await loadConfig();
    await loadBackups();
    await loadKernel();
    // Restore chat history for the initial session
    const sid = activeSessionId || $("activeSessionSelect").value || "";
    restoreChatMessages(sid);
  } catch (e) {
    showToast(`Init failed: ${e.message}`, 4000);
  }
  window.setInterval(pollHealth, 8000);
}

// Reload chat history when the user picks a different session
$("activeSessionSelect")?.addEventListener("change", () => {
  const sid = activeSessionId || $("activeSessionSelect").value || "";
  restoreChatMessages(sid);
});

$("authLogout")?.addEventListener("click", async () => {
  try {
    await fetch(apiUrl("/api/auth/logout"), { method: "POST", credentials: "same-origin" });
  } catch (_) {
    /* ignore */
  }
  redirectToLogin();
});

boot();
