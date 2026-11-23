import { PublicClientApplication, InteractionRequiredAuthError } from "@azure/msal-browser";
import { changeScope, scopeRows, sourceKey } from "./scope.mjs";

const paths = {
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
  sync: '<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3"/>',
  chart: '<path d="M3 3v18h18M7 15l4-5 4 3 5-8"/>',
  settings: '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="9" cy="18" r="2"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
  bot: '<rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M8 12v2m8-2v2m-7 3h6M1 12v4m22-4v4"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14a8 3 0 0 0 16 0V5M4 12a8 3 0 0 0 16 0"/>',
  alert: '<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3v.1"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  logout: '<path d="M9 4H4v16h5m5-13 5 5-5 5m-6-5h11"/>',
  play: '<path d="m8 5 11 7-11 7V5Z"/>',
  pause: '<path d="M8 5v14m8-14v14"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  external: '<path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.layers}</svg>`;
const e = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const number = (value) => value === null || value === undefined ? "—" : new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
const date = (value) => value ? new Date(value).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "Never";
const duration = (start, end) => {
  const seconds = Math.max(0, Math.round((new Date(end) - new Date(start)) / 1000));
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${String(seconds % 60).padStart(2, "0")} s`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
};
const pages = [
  ["overview", "Overview", "grid"], ["sources", "Sources", "folder"],
  ["sync", "Synchronization", "sync"], ["groups", "Groups and access", "users"],
  ["purges", "Purges", "database"], ["maintenance", "Logs", "shield"],
  ["insights", "Insights", "chart"], ["mcp", "MCP · Copilot", "layers"], ["foundry", "Foundry agent", "bot"],
  ["configuration", "Configuration", "settings"],
];
const titles = {
  overview: ["Your document pipeline at a glance.", "Live connections from your files to your agent."],
  sources: ["Your document sources.", "Azure file shares connected to your knowledge base."],
  sync: ["Keep your knowledge up to date.", "Refresh and purge schedules for your shares."],
  groups: ["The right content for the right people.", "Associations between sources and existing Entra groups."],
  purges: ["Purge deleted files.", "Global purge schedule and rebuild history."],
  maintenance: ["Logs.", "Refresh, connection, access operations and administrative history."],
  insights: ["Understand what is happening.", "Azure Monitor metrics. Missing data is not zero."],
  mcp: ["Microsoft 365 Copilot connector (MCP).", "Hosted MCP server status, start/stop, and the step-by-step Microsoft 365 setup."],
  foundry: ["Foundry agent.", "Foundry project, knowledge-base connection, agent, and its Microsoft 365 publication."],
  configuration: ["Configuration and audit.", "Integration settings, limitations, and administrative activity."],
};
const integrationPages = ["mcp", "foundry"];
const M365_CLIENT_ID = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b";
const main = document.querySelector("#main");
const modal = document.querySelector("#modal");
let config, msal, account, page = "overview", busy = false, hours = "24", configurationTab = "settings";
let cachedOverview, cachedSources, cachedGroups, cachedState, cachedConfiguration, cachedMetrics, cachedMcp, cachedFoundry;
let loadSequence = 0;
let fileDialog = null, fileRequestSequence = 0;
const previousSchedules = new Map();
let scope = { resourceGroup: "", account: "", sourceKey: "" };
let resourceAccounts = [];
let sourceInventoryError = "";
let purgeStateError = "";
let draft = null;
let toastTimer;
let deferredToasts = [];
let groupSearchSequence = 0;
const groupSearchTimers = new WeakMap();

function toast(message, error = false) {
  if (modal.open) {
    deferredToasts.push({ message, error });
    return;
  }
  const element = document.querySelector("#toast");
  element.textContent = message;
  element.className = `toast${error ? " error" : ""}`;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, error ? 14000 : 7000);
}

modal.addEventListener("close", () => {
  if (!modal.open) { fileDialog = null; fileRequestSequence++; }
  if (!modal.open && deferredToasts.length) {
    const messages = deferredToasts;
    deferredToasts = [];
    toast(messages.map(item => item.message).join(" · "), messages.some(item => item.error));
  }
});

function badge(status) {
  const known = {
    success: ["Up to date", "good"], inProgress: ["Running", "accent"], neverRun: ["Never run", "warn"],
    transientFailure: ["Temporary failure", "bad"], persistentFailure: ["Failed", "bad"],
    queued: ["Queued", "warn"], running: ["Running", "accent"], failed: ["Failed", "bad"],
    accepted: ["Accepted", "accent"], completed: ["Completed", "good"], executing: ["Pending", "warn"],
    awaiting_access_verification: ["Published", "good"], Running: ["Running", "good"],
    cancelled: ["Cancelled", ""],
    Succeeded: ["Provisioned", "good"], Ready: ["Ready", "good"], Stopped: ["Stopped", "warn"],
  };
  const [text, style] = known[status] || [status || "Status unavailable", ""];
  return `<span class="pill ${style}">${e(text)}</span>`;
}

function notice(text, error = false) {
  return `<div class="notice${error ? " error" : ""}">${icon("alert")}<div>${e(text)}</div></div>`;
}

async function token() {
  if (!account) throw new Error("Sign in with your administrator account.");
  try {
    return (await msal.acquireTokenSilent({ scopes: [config.scope], account })).accessToken;
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      throw new Error("Your session has expired. Sign out and sign in again.");
    }
    throw new Error(`Could not acquire a console token (${error.errorCode || "authentication_error"}).`);
  }
}

async function api(path, body) {
  const headers = { Authorization: `Bearer ${await token()}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) };
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin", cache: "no-store",
  });
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error(`The server returned HTTP ${response.status} without a valid JSON response.`);
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.code = data.code;
    error.setting = data.setting;
    throw error;
  }
  return data;
}

function header() {
  const [title, subtitle] = titles[page];
  return (integrationPages.includes(page) ? "" : renderFilters()) + `<div class="page-head"><div><div class="eyebrow" style="margin-bottom:12px">${e(pages.find(p => p[0] === page)[1])}</div><h1>${title}</h1><p class="muted">${subtitle}</p></div><div class="head-actions">${page === "insights" ? `<select id="time-range" aria-label="Metrics time range" style="width:auto">${[["1", "Last hour"], ["6", "Last 6 hours"], ["24", "Last 24 hours"], ["168", "Last 7 days"]].map(([value, label]) => `<option value="${value}" ${hours === value ? "selected" : ""}>${label}</option>`).join("")}</select>` : '<span class="pill"><span class="dot good"></span>Azure data</span>'}<button data-action="refresh">${icon("sync")}Refresh</button></div></div>`;
}

const selectedRows = data => scopeRows(data?.indexers || [], scope);
const activeRows = data => selectedRows(data).filter(row => row.active);

function renderFilters() {
  const rows = cachedSources?.indexers.filter(row => row.active) || [];
  const groups = [...new Set(rows.map(row => row.resourceGroup))].sort();
  const accounts = [...new Set(scopeRows(rows, { resourceGroup: scope.resourceGroup }).map(row => row.account))].sort();
  const shares = scopeRows(rows, { resourceGroup: scope.resourceGroup, account: scope.account });
  const select = (field, label, all, values) => `<div class="field"><label for="scope-${field}">${label}</label><select id="scope-${field}" data-scope="${field}"><option value="">${all}</option>${values.map(([value, text]) => `<option value="${e(value)}" ${scope[field] === value ? "selected" : ""}>${e(text)}</option>`).join("")}</select></div>`;
  return `<section class="card resource-filters" aria-label="Azure resource filters"><div class="filter-grid"><div class="field"><label for="scope-subscription">Subscription</label><select id="scope-subscription" disabled><option>${e(config?.subscriptionId)}</option></select></div>${select("resourceGroup", "Resource group", "All connected resource groups", groups.map(value => [value, value]))}${select("account", "Storage account", "All connected accounts", accounts.map(value => [value, value]))}${select("sourceKey", "Share", "All connected shares", shares.map(row => [sourceKey(row), `${row.account} / ${row.share}`]))}</div><div class="filter-summary"><span>${cachedSources ? `${activeRows(cachedSources).length} / ${rows.length} shares in view${scope.sourceKey ? " · Selected share" : " · Filterable global view"}` : "Share inventory not loaded."}</span><button data-action="reset-scope" class="small">Reset filters</button></div>${sourceInventoryError ? notice(`Filters unavailable: ${sourceInventoryError}`, true) : ""}${["purges", "maintenance", "configuration", "insights"].includes(page) ? '<p class="small muted">This page covers shared services. Share filters do not limit its actions or metrics.</p>' : ""}</section>`;
}

function footer(timestamp) {
  return `<div class="footer"><span>Subscription ${e(config.subscriptionId)} · Azure API data</span><span>${timestamp ? `Updated ${e(date(timestamp))}` : "Updated on demand"} · No document content collected</span></div>`;
}

function empty(text) {
  return `<div class="empty">${icon("database")}<p>${e(text)}</p></div>`;
}

function stat(label, value, detail, name) {
  return `<div class="card"><div class="stat-label">${label}${icon(name)}</div><div class="stat-value">${e(value)}</div><div class="stat-detail">${e(detail)}</div></div>`;
}

const counterExplanation = "Counters come from the last jobs only: LAST SYNC is the number of new or modified files listed for the latest refresh; FAILED is Azure's result for it. INDEXED is the share's file list recorded after the latest refresh that changed something, or by the last purge if more recent. PURGED is the total of files purged since the first recorded purge list. Click a number to see the files. — means no completed job recorded it yet.";

const jobCount = value => Number.isInteger(value) && value >= 0 ? value : null;

function indexedTotal(rows) {
  const counts = rows?.map(row => jobCount(row.indexed?.count));
  return counts?.length && counts.every(value => value !== null) ? counts.reduce((sum, value) => sum + value, 0) : null;
}

function jobCell(value, detail = "", list = null) {
  const shown = value === null ? '<span class="muted">—</span>'
    : list ? `<button class="file-counter" ${Object.entries(list).map(([key, item]) => `data-${key}="${e(item)}"`).join(" ")}>${e(number(value))}</button>`
      : e(number(value));
  return `${shown}${detail ? `<div class="cell-sub">${e(detail)}</div>` : ""}`;
}

function indexedDetail(row) {
  const indexed = row.indexed;
  return indexed?.at ? `${indexed.from === "sync" ? "Sync" : "Purge"} ${date(indexed.at)}` : "";
}

const fileTitles = { lastSync: "Files of the last sync", indexed: "Indexed files", purged: "All purged files", purge: "Purged files" };
const fileExplanations = {
  lastSync: "New or modified files processed by the latest refresh of this share.",
  indexed: "Files of this share recorded by the latest job (refresh that changed something, or purge).",
  purged: "Every file removed from this share's index by all recorded purges, newest purge first.",
  purge: "Source files that existed at the previous purge and were gone at this purge. All shares, including paused shares.",
};

function showFailures(name) {
  const row = cachedSources?.indexers.find(item => item.name === name);
  const messages = row ? [row.errorMessage, ...row.errors].filter(Boolean) : [];
  openDialog(`<h2 id="modal-title">Failed files</h2><p class="small muted">Errors reported by Azure for the latest refresh of ${e(row?.share || name)} (first 10). Azure messages may not identify every file.</p>${messages.length ? `<ul>${messages.map(message => `<li>${e(message)}</li>`).join("")}</ul>` : empty(row?.itemsFailed === 0 ? "No failed files in the latest refresh." : "Azure returned no error details.")}<div class="modal-actions"><button data-close>Close</button></div>`);
}

function groupLabel(row) {
  return row.groupName && !row.groupNameError && row.groupName !== row.groupId
    ? e(row.groupName) : `Group name unavailable${row.groupNameError ? `: ${e(row.groupNameError)}` : ""}`;
}

const FILE_PAGE = 100, FILE_LIMIT = 5000;

function fileTree(files) {
  const root = { id: 0, name: "All files", depth: 0, parent: null, children: [], files: [], count: 0 };
  const nodes = [root], byKey = new Map();
  const child = (parent, name, key) => {
    if (!byKey.has(key)) {
      const node = { id: nodes.length, name, depth: parent.depth + 1, parent: parent.id, children: [], files: [], count: 0 };
      nodes.push(node); parent.children.push(node.id); byKey.set(key, node);
    }
    return byKey.get(key);
  };
  for (const file of files) {
    const source = file.account ? `${file.account} / ${file.share}` : file.sourceKey || "Files";
    const parts = file.path.split(/[\\/]+/).filter(Boolean);
    let node = child(root, source, source), key = source;
    root.count++; node.count++;
    for (const part of parts.slice(0, -1)) { key += `/${part}`; node = child(node, part, key); node.count++; }
    node.files.push({ ...file, name: parts.at(-1) || file.path, folder: parts.slice(0, -1).join(" / "), source });
  }
  const byName = (a, b) => nodes[a].name.localeCompare(nodes[b].name, undefined, { numeric: true, sensitivity: "base" });
  nodes.forEach(node => { node.children.sort(byName); node.files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" })); });
  return nodes;
}

const subtreeFiles = (nodes, id) => [...nodes[id].files, ...nodes[id].children.flatMap(childId => subtreeFiles(nodes, childId))];

const fileType = name => {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : "File";
};

function fileIcon(name) {
  const type = fileType(name);
  const tone = { PDF: "#d64545", DOCX: "#2b6cd4", DOC: "#2b6cd4", XLSX: "#1f8a4c", XLS: "#1f8a4c", CSV: "#1f8a4c", PPTX: "#d0632b", PPT: "#d0632b" }[type] || "var(--cp-text-muted)";
  return `<span class="file-glyph" style="color:${tone}">${type === "File" ? "•" : e(type.slice(0, 4))}</span>`;
}

const folderGlyph = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/></svg>';

function renderFileTree(view) {
  const { nodes, folder, expanded } = view;
  const item = id => {
    const node = nodes[id];
    const open = expanded.has(id);
    const caret = node.children.length ? `<span class="tree-caret" data-file-toggle="${id}">${open ? "▾" : "▸"}</span>` : '<span class="tree-caret"></span>';
    return `<button class="tree-item${folder === id ? " active" : ""}" data-file-folder="${id}" style="padding-left:${8 + node.depth * 14}px" aria-current="${folder === id}">${caret}<span class="tree-icon">${folderGlyph}</span><span class="tree-name">${e(node.name)}</span><span class="tree-count">${number(node.count)}</span></button>`
      + (open ? node.children.map(item).join("") : "");
  };
  return item(0);
}

function renderFileResults(view) {
  const { nodes, folder, query } = view;
  const node = nodes[folder];
  const needle = query.trim().toLowerCase();
  const flat = folder === 0 || needle;
  const files = (flat ? subtreeFiles(nodes, folder) : node.files)
    .filter(file => !needle || `${file.folder}/${file.name}`.toLowerCase().includes(needle));
  const folders = flat ? [] : node.children.map(id => nodes[id]);
  const hasErrors = files.some(file => file.error);
  const pageFiles = files.slice(view.offset, view.offset + FILE_PAGE);
  const rows = (view.offset ? [] : folders).map(sub => `<tr class="file-row folder-row" data-file-folder="${sub.id}"><td><span class="tree-icon">${folderGlyph}</span><span class="cell-title">${e(sub.name)}</span></td>${flat ? "<td></td>" : ""}<td class="muted">Folder · ${number(sub.count)} files</td>${hasErrors ? "<td></td>" : ""}</tr>`)
    .concat(pageFiles.map(file => `<tr class="file-row"><td>${fileIcon(file.name)}<span class="file-name">${e(file.name)}</span></td>${flat ? `<td class="cell-sub mono">${e([file.source, file.folder].filter(Boolean).join(" / "))}</td>` : ""}<td class="muted">${e(fileType(file.name))}</td>${hasErrors ? `<td class="error-text">${file.error ? e(file.error) : "—"}</td>` : ""}</tr>`));
  const table = rows.length
    ? `<div class="table-wrap file-list"><table><thead><tr><th>Name</th>${flat ? "<th>Location</th>" : ""}<th>Type</th>${hasErrors ? "<th>Error</th>" : ""}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : empty(needle ? "No file matches this search." : view.message || "This folder is empty.");
  const pager = files.length > FILE_PAGE
    ? `<div class="file-pagination"><button data-file-offset="${Math.max(0, view.offset - FILE_PAGE)}" ${view.offset === 0 ? "disabled" : ""}>Previous</button><span>${view.offset + 1}–${view.offset + pageFiles.length} of ${number(files.length)}</span><button data-file-offset="${view.offset + FILE_PAGE}" ${view.offset + FILE_PAGE >= files.length ? "disabled" : ""}>Next</button></div>` : "";
  return table + pager + `<p class="small muted explorer-status">${number(files.length)} file${files.length === 1 ? "" : "s"}${folders.length ? ` · ${number(folders.length)} folder${folders.length === 1 ? "" : "s"}` : ""}${needle ? ` matching “${e(query.trim())}”` : ""}</p>`;
}

function fileCrumbs(view) {
  const trail = [];
  for (let id = view.folder; id !== null; id = view.nodes[id].parent) trail.unshift(view.nodes[id]);
  return trail.map((node, index) => index === trail.length - 1 ? `<span class="crumb current">${e(node.name)}</span>` : `<button class="crumb" data-file-folder="${node.id}">${e(node.name)}</button>`).join('<span class="crumb-sep">›</span>');
}

function renderFileExplorer(selection) {
  const view = selection.view;
  modal.innerHTML = fileShell(selection, (view.message ? notice(view.message) : "")
    + (view.truncated ? notice(`Only the first ${number(FILE_LIMIT)} of ${number(view.total)} files are shown.`) : "")
    + `<div class="explorer"><nav class="explorer-tree" aria-label="Folders">${renderFileTree(view)}</nav><div class="explorer-main"><div class="explorer-bar"><div class="crumbs">${fileCrumbs(view)}</div><input type="search" data-file-search placeholder="Search in this folder" aria-label="Search files" value="${e(view.query)}"></div><div data-file-results>${renderFileResults(view)}</div></div></div>`
    + (view.at ? `<p class="small muted">Recorded ${e(date(view.at))}</p>` : ""));
}

const fileShell = (selection, content) => `<h2 id="modal-title">${e(fileTitles[selection.kind])}${selection.sourceKey ? ` · ${e(selection.sourceKey)}` : ""}</h2><p class="small muted">${e(fileExplanations[selection.kind])}</p>${content}<div class="modal-actions"><button data-close>Close</button></div>`;

function selectFileFolder(id) {
  const view = fileDialog?.view;
  if (!view || !view.nodes[id]) return;
  for (let node = id; node !== null; node = view.nodes[node].parent) view.expanded.add(node);
  Object.assign(view, { folder: id, offset: 0, query: "" });
  renderFileExplorer(fileDialog);
}

async function showFilePage() {
  const selection = fileDialog;
  if (!selection) return;
  const request = ++fileRequestSequence;
  const base = selection.kind === "purge"
    ? `/api/operations/${encodeURIComponent(selection.operationId)}/files`
    : `/api/sources/files?${new URLSearchParams({ kind: selection.kind, sourceKey: selection.sourceKey, ...(selection.slot ? { slot: selection.slot } : {}) })}`;
  const loading = fileShell(selection, '<p><span class="spinner"></span>Loading files…</p>');
  if (!modal.open) openDialog(loading, true);
  else modal.innerHTML = loading;
  try {
    const files = [];
    let data, offset = 0;
    do {
      const params = new URLSearchParams({ offset: String(offset), limit: String(FILE_PAGE) });
      data = await api(`${base}${base.includes("?") ? "&" : "?"}${params}`);
      if (!modal.open || fileDialog !== selection || request !== fileRequestSequence) return;
      if (!Number.isInteger(data.total) || data.total < 0 || data.offset !== offset || data.limit !== FILE_PAGE
        || !Array.isArray(data.files) || data.files.length > FILE_PAGE
        || data.files.some(file => typeof file.path !== "string")) throw new Error(data.message || "The file service returned an unexpected response.");
      files.push(...data.files);
      offset += FILE_PAGE;
    } while (data.files.length && offset < data.total && files.length < FILE_LIMIT);
    const nodes = fileTree(files);
    selection.view = { nodes, folder: 0, offset: 0, query: "", expanded: new Set([0, ...nodes[0].children]),
      at: data.at, message: data.message, total: data.total, truncated: files.length < data.total };
    renderFileExplorer(selection);
  } catch (error) {
    if (!modal.open || fileDialog !== selection || request !== fileRequestSequence) return;
    modal.innerHTML = fileShell(selection, notice(`File list unavailable: ${error.message}`, true) + "<button data-file-retry>Retry</button>");
  }
}

function renderOverview(data) {
  const sections = data.sections;
  const sourceData = sections.sources?.data;
  const rows = sourceData ? activeRows(sourceData) : null;
  const failureCount = rows ? rows.filter(row => row.status?.toLowerCase().includes("failure") || row.itemsFailed > 0).length : null;
  const stats = sections.index?.data;
  const errors = Object.entries(sections).filter(([, section]) => section.status === "error");
  document.querySelector("#connection-label").textContent = errors.length ? "Partially connected" : "Connected to Azure";
  document.querySelector("#connection-dot").className = `dot ${errors.length ? "warn" : "good"}`;
  const node = (name, title, description, id, note) => `<div class="node"><div class="node-icon">${icon(name)}</div><h3>${title}</h3><div class="mono">${e(id)}</div><p class="muted">${e(description)}</p>${note ? `<p>${e(note)}</p>` : ""}</div>`;
  const healthRows = [["files", "Azure Files", "folder"], ["search", "Azure AI Search", "search"], ["foundry", "Microsoft Foundry", "bot"], ["downloads", "File access", "external"]];
  return header()
    + `<div class="grid-4">${stat("Sources in view", rows?.length, "Active shares matching the filters", "folder")}${stat("Indexed files · last jobs", number(indexedTotal(rows)), indexedTotal(rows) === null ? "Not recorded by a completed job yet" : "Recorded by the latest refresh or purge of each share", "folder")}${stat("Indexed chunks · global", number(stats?.documentCount), "Shared index: total is not filtered by share", "database")}${stat("Failed indexers", number(failureCount), "Latest result for the filtered sources", "alert")}</div>`
    + (errors.length ? notice(`${errors.length} section(s) unavailable: ${errors.map(([key, item]) => `${key} — ${item.error}`).join(" ; ")}`, true) : "")
    + `<section class="card"><div class="card-head"><div><h2>From source to answer</h2><p class="small muted" style="margin-top:6px">Filtered sources and shared document pipeline services.</p></div><span class="pill accent">RAG pipeline</span></div><div class="topology">${node("folder", "File Shares", "Original file storage", rows ? [...new Set(rows.map(row => row.account))].join(" · ") || "No accounts in view" : "Unavailable", rows?.map(row => `${row.account} / ${row.share}`).join(" · "))}<div class="connector"></div>${node("search", "AI Search", "Chunks, vectors, and permissions", sections.search?.data?.name || config.searchService, sourceData?.activeIndex)}<div class="connector"></div>${node("database", "Knowledge base", "Agent search source", sourceData?.knowledgeSource || "Unavailable", "Configured through Foundry")}<div class="connector"></div>${node("bot", "Agent", "Teams / Copilot responses", sections.foundry?.data?.name || "Unavailable", "Channel availability requires verification")}</div><div class="legend"><span><span class="dot good"></span>Resource status from ARM</span><span>${icon("shield")}Group filtering, not NTFS ACLs</span><span>${icon("clock")}Actual frequency shown per indexer</span></div></section>`
    + `<div class="grid-2"><section class="card"><div class="card-head"><h2>Service status</h2><span class="page-count">4 integrations</span></div><div class="item-list">${healthRows.map(([key, title, symbol]) => {
      const section = sections[key];
      return `<div class="list-row"><div class="list-label"><span class="list-icon">${icon(symbol)}</span><div><div class="cell-title">${title}</div><div class="cell-sub">${e(section?.data?.name || section?.error || "Unavailable")}</div></div></div>${section?.status === "ok" ? (section.data.portal ? `<a target="_blank" rel="noopener noreferrer" href="${e(section.data.portal)}">${badge(section.data.state)}</a>` : badge(section.data.state)) : badge("failed")}</div>`;
    }).join("")}</div></section><section class="card"><div class="card-head"><h2>Things to know</h2>${icon("shield")}</div><div class="item-list"><div><div class="cell-title">Permissions are not NTFS ACLs</div><p class="small muted">Each chunk carries the group associated with its source.</p></div><div><div class="cell-title">Shared hosting plan</div><p class="small muted">${e(sections.hosting?.data?.name || sections.hosting?.error || "Unavailable")} · ${e(sections.hosting?.data?.sku)}</p></div><div><div class="cell-title">Access changes require maintenance</div><p class="small muted">Preview, index rebuild, link update, and access tests.</p></div></div></section></div>`
    + renderSourceTable(sourceData, true) + footer(data.generatedAt);
}

function scheduleLabel(value) {
  return ({ PT5M: "Every 5 min", PT15M: "Every 15 min", PT30M: "Every 30 min", PT1H: "Hourly" })[value] || value || "Paused";
}

function scheduleOptions(value) {
  const supported = ["PT5M", "PT15M", "PT30M", "PT1H"];
  const current = value && !supported.includes(value) ? `<option value="${e(value)}" selected disabled>${e(value)} (current)</option>` : "";
  return `<option value="" ${value ? "" : "selected"}>Paused</option>${current}${supported.map(option => `<option value="${option}" ${value === option ? "selected" : ""}>${e(scheduleLabel(option))}</option>`).join("")}`;
}

const intervalLabel = (hours) => hours === 24 ? "Daily" : hours === 1 ? "Hourly" : `Every ${hours} hours`;
const hourLabel = (hour) => `${String(hour).padStart(2, "0")}:00`;

function purgeScheduleLabel(state) {
  const schedule = state?.rebuild;
  if (!schedule || typeof schedule.enabled !== "boolean" || ![1, 2, 3, 4, 6, 8, 12, 24].includes(schedule.intervalHours)) return "Unavailable";
  const frequency = intervalLabel(schedule.intervalHours);
  return schedule.enabled ? frequency : `Disabled (configured: ${frequency})`;
}

function sourceRow(row, compact, state, standby = false) {
  const purgedAt = row.lastPurge?.at ? `Purge ${date(row.lastPurge.at)}` : "";
  const slot = standby ? { slot: "standby" } : {};
  const syncDetail = standby ? "Azure count · last run of this slot" : row.lastSyncListed || jobCount(row.itemsProcessed) === null ? "New or modified" : "New or modified · list recorded about 1 min after the sync";
  const indexedText = standby ? (row.indexed?.at ? `Built by purge ${date(row.indexed.at)}` : "") : indexedDetail(row);
  const actions = standby ? '<td><span class="pill">Standby · not queried</span></td>'
    : `<td><div class="row-actions"><button data-run="${e(row.name)}" title="Incremental indexing">${icon("play")}Run</button>${row.schedule === null ? `<button data-refresh-resume="${e(row.name)}">${icon("play")}Resume</button>` : row.schedule ? `<button data-refresh-pause="${e(row.name)}">${icon("pause")}Pause</button>` : ""}<button data-remove-source="${e(row.sourceKey)}" title="Disconnect this share">${icon("trash")}Remove</button></div></td>`;
  const frequency = compact || standby ? e(row.schedule ? scheduleLabel(row.schedule) : standby ? "Paused (standby)" : scheduleLabel(row.schedule))
    : `<select class="schedule-select" data-schedule="${e(row.name)}" aria-label="Refresh frequency for ${e(row.share)}">${scheduleOptions(row.schedule)}</select>`;
  return `<tr><td><div class="cell-title">${e(row.share)}</div><div class="mono cell-sub">${e(row.name)}</div></td><td>${badge(row.status)}${row.schedule === null && !standby ? '<span class="pill warn">Refresh paused</span>' : ""}${row.errors.length || row.warnings.length || row.errorMessage ? `<details style="margin-top:8px"><summary>Diagnostics</summary><ul>${[row.errorMessage, ...row.errors, ...row.warnings].filter(Boolean).map(message => `<li>${e(message)}</li>`).join("")}</ul></details>` : ""}</td><td>${e(date(row.startTime))}<div class="cell-sub" style="margin-top:4px">${row.endTime && row.startTime ? `Duration: ${e(duration(row.startTime, row.endTime))}` : row.startTime ? "Running…" : ""}</div></td><td>${jobCell(jobCount(row.lastSyncCount), syncDetail, row.lastSyncListed ? { "job-files": "lastSync", "source-key": row.sourceKey } : null)}</td><td>${jobCell(jobCount(row.indexed?.count), indexedText, row.indexed?.listed ? { "job-files": "indexed", "source-key": row.sourceKey, ...slot } : null)}</td><td>${jobCell(jobCount(row.lastPurge?.purgedCount), row.lastPurge?.at && jobCount(row.lastPurge?.purgedCount) === null ? "Counted from next purge" : purgedAt, { "job-files": "purged", "source-key": row.sourceKey, ...slot })}</td><td>${jobCell(jobCount(row.itemsFailed), "", { "failed-files": row.name })}</td><td>${frequency}</td>${compact ? "" : `<td>${e(purgeScheduleLabel(state))}</td>${actions}`}</tr>`;
}

const sourceHead = compact => `<thead><tr><th>Source / indexer</th><th>Status</th><th>Last run</th><th>LAST SYNC</th><th>INDEXED</th><th>PURGED</th><th>FAILED</th><th>Refresh frequency</th>${compact ? "" : "<th>Purge frequency (global)</th><th>Actions</th>"}</tr></thead>`;

function renderIndexSummary(data) {
  const rows = selectedRows(data);
  const active = rows.filter(row => row.active);
  const inactive = rows.filter(row => !row.active);
  const inactiveIndexes = [...new Set(inactive.map(row => row.index))];
  return `<div class="grid-2 index-summary" aria-label="Active and inactive indexes">
    <section class="card index-slot active"><div class="card-head"><h2>Active index</h2><span class="pill accent">${icon("check")}Used by the agent</span></div><div class="index-name mono">${e(data.activeIndex)}</div><p class="small muted">Serves document searches. A paused refresh does not make this index inactive.</p><div class="index-count">${active.length} active indexer${active.length === 1 ? "" : "s"} in view</div></section>
    <section class="card index-slot inactive"><div class="card-head"><h2>Inactive indexes</h2><span class="pill">${icon("pause")}Not queried</span></div>${inactiveIndexes.length ? inactiveIndexes.map(index => `<div class="index-name mono">${e(index)}</div>`).join("") : '<div class="index-name">None in view</div>'}<p class="small muted">Standby for full rebuilds and rollback. Not used for document searches.</p><div class="index-count">${inactive.length} inactive indexer${inactive.length === 1 ? "" : "s"} in view</div></section>
  </div>`;
}

function renderStandbyTable(data, state) {
  const rows = selectedRows(data).filter(row => !row.active).map(row => ({ ...row, sourceKey: sourceKey(row), share: `${row.account} / ${row.share}` }));
  if (!rows.length) return "";
  const indexes = [...new Set(rows.map(row => row.index))].join(" · ");
  return `<section class="card index-slot inactive" style="margin-top:24px"><div class="card-head"><div><h2>Inactive indexers</h2><p class="small muted index-role">${rows.length} indexer${rows.length === 1 ? "" : "s"} in view · Standby · Not queried by the agent</p></div><span class="pill index-name-pill">${e(indexes)}</span></div><div class="table-wrap"><table>${sourceHead(false)}<tbody>${rows.map(row => sourceRow(row, false, state, true)).join("")}</tbody></table></div><p class="small muted" style="margin-top:16px">Standby slot kept as a rollback copy of the index used before the last full rebuild. The agent does not query it; targeted purges do not update it, and a full rebuild (or the fallback when targeted deletion is not permitted) rebuilds it from the shares and switches to it. INDEXED is the file count of the rebuild that built this slot; LAST SYNC and FAILED are Azure's result for this indexer's last run.</p></section>`;
}

function renderSourceTable(data, compact = false, state = null) {
  if (!data) return "";
  const rows = activeRows(data).map(row => ({ ...row, sourceKey: sourceKey(row), share: `${row.account} / ${row.share}` }));
  if (!rows.length) return empty("No shares match the selected filters.") + (compact ? "" : renderStandbyTable(data, state));
  return `<section class="card${compact ? "" : " index-slot active"}" style="margin-top:24px"><div class="card-head"><div><h2>${compact ? "Recent synchronizations" : "Active indexers"}</h2>${compact ? "" : `<p class="small muted index-role">${rows.length} indexer${rows.length === 1 ? "" : "s"} in view · Used by the agent</p>`}</div><span class="pill accent index-name-pill">${e(data.activeIndex)}</span></div><div class="table-wrap"><table>${sourceHead(compact)}<tbody>${rows.map(row => sourceRow(row, compact, state)).join("")}</tbody></table></div><p class="small muted" style="margin-top:16px">${e(counterExplanation)}</p>${compact ? "" : `<p class="small muted" style="margin-top:12px">Refresh indexes new and changed files. Purge removes deleted files from the shared index and uses the same schedule for every share, including paused shares. Pausing stops scheduled refreshes, not a run already in progress; confirmed manual refresh is still possible.</p>`}</section>`
    + (compact ? "" : renderStandbyTable(data, state));
}

function renderSources(data, state = null, stateError = "") {
  const rows = activeRows(data);
  return header() + `<div class="card-head"><div><h2>Shares and settings</h2><p class="small muted">Access groups and refresh schedules are per share. Purge uses one global schedule.</p></div><button class="primary" data-action="add-source">${icon("folder")}Connect a share</button></div>`
    + (stateError ? notice(`Purge schedule unavailable: ${stateError}`, true) : "")
    + (rows.length ? `<div class="grid-2" style="margin-top:0">${rows.map(row => `<section class="card source-card"><div class="source-top"><div class="source-symbol">${icon("folder")}</div><div><h2>${e(row.share)}</h2><span class="pill">Azure Files · SMB</span></div></div><dl class="key-values"><dt>Resource group</dt><dd class="mono">${e(row.resourceGroup)}</dd><dt>Storage account</dt><dd class="mono">${e(row.account)}</dd><dt>Search source</dt><dd class="mono">${e(row.source)}</dd><dt>Active index (shared)</dt><dd class="mono">${e(row.index)}</dd><dt>Authorized group</dt><dd>${groupLabel(row)}</dd><dt>Indexed files (last job)</dt><dd>${jobCell(jobCount(row.indexed?.count), indexedDetail(row), row.indexed?.listed ? { "job-files": "indexed", "source-key": row.sourceKey } : null)}</dd><dt>Refresh frequency</dt><dd>${e(scheduleLabel(row.schedule))}</dd><dt>Purge frequency (global)</dt><dd>${e(purgeScheduleLabel(state))}</dd></dl><div class="source-bottom">${badge(row.status)}</div></section>`).join("")}</div>` : empty("No shares match the selected filters."))
    + notice("New shares are selected from the subscription's Azure inventory. Storage credentials must already be configured on the server; no key is entered in the browser.") + footer();
}

const phases = { snapshot: "Checking configuration", pause: "Pausing schedules", idle: "Waiting for indexers", block_download: "Restricting links", delete: "Removing indexes", skills: "Updating permissions", create: "Creating indexes", reset: "Resetting indexers", run: "Starting indexing", wait: "Indexing", verify: "Checking results", publish_download: "Publishing links", restore: "Restoring schedules", finish: "Finishing" };
const rebuildPhases = { purge: "Removing deleted files", prepare: "Preparing", idle: "Waiting for indexers", recreate: "Creating empty index", slot: "Configuring indexers", reset: "Resetting indexers", run: "Starting indexing", wait: "Indexing all shares", stats: "Checking results", switch: "Switching the agent", schedule: "Restoring schedules" };
const sourcePhases = { prepare: "Checking prerequisites", datasource: "Connecting Search", skills: "Configuring permissions", indexers: "Configuring indexers", download: "Configuring file links", activate: "Activating the indexer", run: "Starting indexing", wait: "Indexing" };

function operationTitle(op) {
  if (op.type === "source_add") return `Connect share · ${e(op.account)} / ${e(op.share)}`;
  if (op.type === "group_change") return `Change access · ${e(op.share)}`;
  if (op.type === "rebuild") return `Purge deleted files · ${op.trigger === "schedule" ? "Scheduled" : "Manual"}`;
  if (op.type === "source_remove") return `Remove share · ${e(op.sourceKey)}`;
  const action = { run: "Refresh", pause: "Pause refresh", schedule: "Change refresh schedule" }[op.action] || "Indexer action";
  return `${action} · ${e(op.target)}`;
}

function operationDetail(op) {
  if (!["queued", "running"].includes(op.status)) return "";
  const labels = op.type === "source_add" ? sourcePhases : op.type === "group_change" ? phases : op.type === "rebuild" ? rebuildPhases : {};
  return labels[op.phase] ? `<p class="small muted">${e(labels[op.phase])}</p>` : "";
}

function renderRebuild(state) {
  const settings = state?.rebuild;
  if (!settings) return "";
  const last = state.operations.find(op => op.type === "rebuild");
  const running = state.operations.some(op => ["group_change", "rebuild", "source_add"].includes(op.type) && !["completed", "accepted", "awaiting_access_verification", "failed", "cancelled"].includes(op.status));
  return `<section class="card" style="margin-top:24px"><div class="card-head"><div><h2>Global purge of deleted files</h2><p class="small muted" style="margin-top:6px">Remove the index chunks of deleted files from every share.</p></div>${settings.enabled ? '<span class="pill good">Scheduled</span>' : '<span class="pill warn">Disabled</span>'}</div>
  <div class="grid-2" style="margin-top:0;align-items:start"><form id="rebuild-form"><div class="field"><label style="display:flex;gap:10px;align-items:center;font-weight:600"><input type="checkbox" id="rebuild-enabled" style="width:auto;min-height:0" ${settings.enabled ? "checked" : ""}>Enable scheduled purge</label></div><div class="field"><label for="rebuild-interval">Frequency</label><select id="rebuild-interval">${[1, 2, 3, 4, 6, 8, 12, 24].map(value => `<option value="${value}" ${settings.intervalHours === value ? "selected" : ""}>${intervalLabel(value)}</option>`).join("")}</select></div><div class="field"><label for="rebuild-hour">Reference hour (Paris time)</label><select id="rebuild-hour">${Array.from({ length: 24 }, (_, hour) => `<option value="${hour}" ${settings.startHour === hour ? "selected" : ""}>${hourLabel(hour)}</option>`).join("")}</select><p class="field-help">Runs occur at this hour and at each interval afterward (for example, every 6 hours from 02:00: 02:00, 08:00, 14:00, and 20:00).</p></div><div style="display:flex;gap:8px;flex-wrap:wrap"><button class="primary" type="submit">${icon("check")}Save</button><button type="button" data-rebuild-now ${running ? "disabled" : ""}>${icon("sync")}Purge now</button></div></form>
  <div class="item-list"><div class="list-row"><div><div class="cell-title">Next run</div><div class="cell-sub">${settings.enabled ? `${e(date(settings.nextRun))} · ${e(intervalLabel(settings.intervalHours))} from ${hourLabel(settings.startHour)}` : "None: schedule disabled"}</div></div>${icon("clock")}</div><div class="list-row"><div><div class="cell-title">Last purge</div><div class="cell-sub">${last ? `${e(date(last.updatedAt || last.createdAt))}${last.status === "completed" ? ` · ${number(last.newChunks)} chunks` : ""}` : "None from this console"}</div></div>${last ? badge(last.status) : ""}</div>${settings.updatedAt ? `<div class="cell-sub">Schedule updated ${e(date(settings.updatedAt))}</div>` : ""}</div></div>
  ${notice("Each purge lists all connected shares, including paused shares, and deletes from the index only the chunks of files that no longer exist (no reindexing, no embedding costs). Source files are never deleted. If targeted deletion is not permitted, the purge falls back to a full index rebuild, which reindexes every share and incurs embedding costs. Incremental refreshes do not remove deleted files from the index.")}</section>`;
}

function operationKind(op) {
  if (op.type === "source_add") return ["Connect share", `${op.account} / ${op.share}`];
  if (op.type === "group_change") return ["Change access", op.share];
  if (op.type === "source_remove") return ["Remove share", op.sourceKey];
  if (op.type === "indexer") return [{ run: "Refresh", pause: "Pause refresh", schedule: "Change refresh schedule" }[op.action] || "Indexer action", op.target];
  if (op.type === "rebuild") return ["Purge deleted files", op.trigger === "schedule" ? "Scheduled · all shares" : "Manual · all shares"];
  return [op.type, op.target || op.share || op.sourceKey];
}

function operationScope(op) {
  const share = (key) => `<span class="pill">Share</span> <span class="mono">${e(String(key).replace("/", " / "))}</span>`;
  if (op.type === "rebuild") return '<span class="pill warn">Global</span>';
  if (op.type === "source_add") return share(`${op.account}/${op.share}`);
  if (op.type === "group_change") return share(op.share);
  if (op.type === "source_remove") return share(op.sourceKey);
  if (op.type === "indexer") {
    const key = cachedSources?.indexers?.find(row => row.name === op.target)?.sourceKey;
    return key ? share(key) : '<span class="pill">Share</span> <span class="muted">removed or unknown</span>';
  }
  return '<span class="pill warn">Global</span>';
}

const finished = (op) => ["completed", "accepted", "awaiting_access_verification", "failed", "cancelled"].includes(op.status);

function operationActions(op) {
  return `${op.status === "awaiting_access_verification" ? notice("Access verification required: test document search and file links with authorized and unauthorized users. Published does not mean access has been verified.") : ""}${op.error ? `<p class="small error-text">${e(op.error)}</p>` : ""}${op.status === "failed" && ["group_change", "rebuild", "source_add"].includes(op.type) ? `<button data-resume="${e(op.id)}" style="margin-top:12px">${icon("sync")}Resume after fixing</button>${op.type === "group_change" && op.phase === "snapshot" ? `<button data-cancel="${e(op.id)}" style="margin:12px 0 0 8px">Cancel before maintenance</button>` : ""}` : ""}`;
}

function renderLogOperations(state) {
  const cleared = state.logsClearedAt || "";
  const operations = state.operations.filter(op => !(cleared && (op.createdAt || "") <= cleared));
  const workerDown = state.worker?.running === false && operations.some(op => ["group_change", "rebuild", "source_add"].includes(op.type) && ["queued", "running"].includes(op.status));
  return `<section class="card" style="margin-top:24px"><div class="card-head"><h2>Operations</h2><div style="display:flex;gap:8px;align-items:center"><span class="page-count">${operations.length}</span><button class="small" data-clear-logs ${operations.length || state.audit?.length ? "" : "disabled"}>${icon("trash")}Clear log</button></div></div>${state.worker?.error ? notice(state.worker.error, true) : ""}${workerDown ? notice("The worker is not running. Check the application logs before resuming the operation.", true) : ""}${operations.length ? `<div class="table-wrap"><table><thead><tr><th>Scope</th><th>Type</th><th>Subject</th><th>Start</th><th>End</th><th>Duration</th><th>Status</th></tr></thead><tbody>${operations.map(op => {
    const [type, subject] = operationKind(op);
    const end = finished(op) ? op.updatedAt : null;
    return `<tr><td>${operationScope(op)}</td><td class="cell-title">${e(type)}</td><td class="mono">${e(subject)}</td><td>${e(date(op.createdAt))}</td><td>${end ? e(date(end)) : "—"}</td><td>${end && op.createdAt ? e(duration(op.createdAt, end)) : op.createdAt ? "Running…" : "—"}</td><td>${badge(op.status)}${operationDetail(op)}${operationActions(op)}</td></tr>`;
  }).join("")}</tbody></table></div>` : empty("No operations recorded.")}<p class="small muted" style="margin-top:12px">Clear log empties this page. Purge history remains in the Purges tab.</p></section>`;
}

function renderOperations(state, purges = false) {
  if (!state) return empty("Operation history is unavailable.");
  if (!purges) return renderLogOperations(state);
  const operations = state.operations.filter(op => (op.type === "rebuild") === purges);
  const recent = operations.slice(0, 20);
  const actionable = operations.filter(op => ["failed", "awaiting_access_verification"].includes(op.status) && !recent.includes(op));
  return `<section class="card" style="margin-top:24px"><div class="card-head"><h2>${purges ? "Purge operation history" : "Recent operations"}</h2><span class="page-count">${operations.length}</span></div><p class="small muted">Latest 20 operations; older failures and pending access checks remain visible.</p>${state.worker?.error ? notice(state.worker.error, true) : ""}${state.worker?.running === false && operations.some(op => ["group_change", "rebuild", "source_add"].includes(op.type) && ["queued", "running"].includes(op.status)) ? notice("The worker is not running. Check the application logs before resuming the operation.", true) : ""}${operations.length ? [...recent, ...actionable].map(op => `<div class="operation"><div class="operation-head"><div><div class="cell-title">${operationTitle(op)}</div><div class="cell-sub">${e(date(op.createdAt))}</div></div>${badge(op.status)}</div>${operationDetail(op)}${purges ? purgeFiles(op) : ""}${op.status === "awaiting_access_verification" ? notice("Access verification required: test document search and file links with authorized and unauthorized users. Published does not mean access has been verified.") : ""}${op.error ? `<p class="small error-text">${e(op.error)}</p>` : ""}${op.status === "failed" && ["group_change", "rebuild", "source_add"].includes(op.type) ? `<button data-resume="${e(op.id)}" style="margin-top:12px">${icon("sync")}Resume after fixing</button>${op.type === "group_change" && op.phase === "snapshot" ? `<button data-cancel="${e(op.id)}" style="margin:12px 0 0 8px">Cancel before maintenance</button>` : ""}` : ""}</div>`).join("") : empty("No operations recorded.")}</section>`;
}

function purgeFiles(op) {
  const fallback = op.fallbackReason ? `<p class="small muted">${e(op.fallbackReason)}</p>` : "";
  if (op.inventoryError) return fallback + notice(`Purged file count unavailable: ${op.inventoryError}`, true);
  const done = op.switched || Boolean(op.purgedAt);
  if (!done || !Number.isInteger(op.purgedFileCount) || op.purgedFileCount < 0) {
    return fallback + `<p class="small muted">Purged files: ${op.status === "completed" && done ? "baseline recorded; counted from the next purge." : "Unavailable."}</p>`;
  }
  return fallback + `<button class="file-counter" data-purge-files="${e(op.id)}">Purged files: ${number(op.purgedFileCount)}</button>`;
}

function auditScope(event) {
  if (["rebuild_schedule", "clear_logs", "start_rebuild", "resume_rebuild"].includes(event.action)) return '<span class="pill warn">Global</span>';
  const key = cachedSources?.indexers?.find(row => row.name === event.detail)?.sourceKey || (/^[^/\s]+\/[^/\s]+$/.test(event.detail || "") ? event.detail : null);
  return key ? `<span class="pill">Share</span> <span class="mono">${e(key.replace("/", " / "))}</span>` : '<span class="pill">Share</span> <span class="muted">removed or unknown</span>';
}

function renderAudit(state) {
  if (!state) return empty("Audit log unavailable.");
  return `<section class="card" style="margin-top:24px"><div class="card-head"><h2>Administrative history</h2><span class="page-count">Last 500 events retained</span></div>${state.audit?.length ? `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Scope</th><th>Action</th><th>Entra actor</th><th>Target</th></tr></thead><tbody>${state.audit.map(event => `<tr><td>${e(date(event.time))}</td><td>${auditScope(event)}</td><td>${e(event.action)}</td><td class="mono">${e(event.actor)}</td><td class="mono">${e(event.detail)}</td></tr>`).join("")}</tbody></table></div>` : empty("No actions recorded.")}</section>`;
}

function renderSync(sources, state = null, stateError = "") {
  return header() + (stateError ? notice(`Purge schedule unavailable: ${stateError}`, true) : "") + renderIndexSummary(sources) + renderSourceTable(sources, false, state) + footer();
}

function groupOptions(groups) {
  const counts = new Map();
  groups.forEach(group => counts.set(group.displayName, (counts.get(group.displayName) || 0) + 1));
  return groups.map(group => `<option value="${e(group.id)}" data-group-name="${e(group.displayName)}">${e(counts.get(group.displayName) > 1 ? `${group.displayName} · ${group.id}` : group.displayName)}</option>`).join("");
}

function refreshGroupOptionLabels(select) {
  const options = Array.from(select.options).filter(option => option.value);
  const counts = new Map();
  options.forEach(option => counts.set(option.dataset.groupName, (counts.get(option.dataset.groupName) || 0) + 1));
  options.forEach(option => {
    const name = option.dataset.groupName;
    option.textContent = counts.get(name) > 1 ? `${name} · ${option.value}` : name;
  });
}

function renderGroupPicker(id, data) {
  const enabled = data.directoryStatus === "ok";
  const status = enabled
    ? (data.groups.length ? "Select an existing security group." : "No security groups available.")
    : "Directory search is disabled. Only groups already associated with sources are available.";
  return `<div class="group-picker" data-group-picker data-request-id="0"><div class="field"><label for="${id}-search">Search Entra groups</label><input id="${id}-search" data-group-query type="search" placeholder="Start of group name" maxlength="256" autocomplete="off" ${enabled ? "" : "disabled"}></div><div class="field"><label for="${id}">Authorized Entra group</label><select id="${id}" data-group-select aria-describedby="${id}-status" required><option value="">Choose a group</option>${groupOptions(data.groups)}</select><p class="field-help" id="${id}-status" data-group-status role="status" aria-live="polite">${e(status)}${data.nextCursor ? " More results are available." : ""}</p><button type="button" data-group-more data-cursor="${e(data.nextCursor || "")}" ${data.nextCursor ? "" : "hidden"}>Load more groups</button><button type="button" data-group-retry hidden>Retry search</button></div></div>`;
}

async function requestGroupPage(picker, append = false) {
  const input = picker.querySelector("[data-group-query]");
  const select = picker.querySelector("[data-group-select]");
  const status = picker.querySelector("[data-group-status]");
  const more = picker.querySelector("[data-group-more]");
  const retry = picker.querySelector("[data-group-retry]");
  const query = input.value.trim(), requestId = picker.dataset.requestId;
  const cursor = append ? more.dataset.cursor : "";
  if (append && !cursor) {
    status.textContent = "Cannot load the next page: the pagination token is missing.";
    status.classList.add("error-text");
    status.setAttribute("role", "alert");
    return;
  }
  const params = new URLSearchParams();
  if (query) params.set("search", query);
  if (cursor) params.set("cursor", cursor);
  more.disabled = true;
  retry.hidden = true;
  status.classList.remove("error-text");
  status.setAttribute("role", "status");
  status.textContent = "Searching groups…";
  try {
    const data = await api(`/api/groups${params.toString() ? `?${params}` : ""}`);
    if (!picker.isConnected || picker.dataset.requestId !== requestId || input.value.trim() !== query) return;
    if (data.directoryStatus !== "ok" || !Array.isArray(data.groups)
      || data.groups.some(group => typeof group.id !== "string" || typeof group.displayName !== "string")
      || Boolean(data.moreAvailable) !== Boolean(data.nextCursor)) {
      throw new Error("The directory returned an unexpected response.");
    }
    if (!append) {
      select.innerHTML = '<option value="">Choose a group</option>' + groupOptions(data.groups);
    } else {
      const existing = new Set(Array.from(select.options, option => option.value));
      select.insertAdjacentHTML("beforeend", groupOptions(data.groups.filter(group => !existing.has(group.id))));
    }
    refreshGroupOptionLabels(select);
    more.dataset.cursor = data.nextCursor || "";
    more.hidden = !data.nextCursor;
    status.textContent = !append && !data.groups.length ? "No security groups match the search."
      : data.nextCursor ? "Select a group. More results are available."
        : "Select an existing security group.";
  } catch (error) {
    if (!picker.isConnected || picker.dataset.requestId !== requestId || input.value.trim() !== query) return;
    status.textContent = `Search failed: ${error.message}`;
    status.classList.add("error-text");
    status.setAttribute("role", "alert");
    more.hidden = true;
    retry.dataset.append = append ? "true" : "false";
    retry.hidden = false;
  } finally {
    if (picker.isConnected && picker.dataset.requestId === requestId) more.disabled = false;
  }
}

function renderGroups(sources, groups, state) {
  const active = activeRows(sources);
  const groupName = id => groups.groups.find(group => group.id === id)?.displayName || id;
  if (!active.length) return header() + empty("No shares match the selected filters.") + footer();
  return header() + notice("Filtering uses the Entra groups associated with sources, not NTFS ACLs. Changing an association requires global search maintenance and a rebuild of both application indexes.") + `<div class="grid-2" style="align-items:start"><section class="card"><div class="card-head"><h2>Current associations</h2>${icon("shield")}</div><div class="item-list">${active.map(row => `<div><div class="list-row"><div class="cell-title">${icon("folder")} ${e(row.account)} / ${e(row.share)}</div><span class="pill accent">Source → Group</span></div><div class="mono" style="margin:12px 0 4px">${e(groupName(row.groupId))}</div><div class="mono muted">${e(row.groupId)}</div><hr class="divider"></div>`).join("")}</div><p class="small muted">Document access groups do not grant access to this console.</p></section><section class="card"><div class="card-head"><h2>Prepare a change</h2>${icon("settings")}</div><form id="group-form"><div class="field"><label for="group-share">Document source</label><select id="group-share" required>${active.map(row => `<option value="${e(sourceKey(row))}">${e(row.account)} / ${e(row.share)}</option>`).join("")}</select></div>${renderGroupPicker("group-id", groups)}<button class="primary" type="submit">${icon("search")}Preview impact</button></form></section></div>` + footer();
}

function sparkline(points) {
  const usable = points.filter(point => Number.isFinite(point.value));
  if (!usable.length) return empty("No data for this time range.");
  const values = usable.map(point => point.value);
  const low = Math.min(...values), high = Math.max(...values);
  let path = "", connected = false;
  points.forEach((point, index) => {
    if (!Number.isFinite(point.value)) { connected = false; return; }
    const x = 8 + (index / Math.max(points.length - 1, 1)) * 384;
    const y = 110 - ((point.value - low) / (high - low || 1)) * 90;
    path += `${connected ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
    connected = true;
  });
  return `<div class="chart"><svg viewBox="0 0 400 130" preserveAspectRatio="none" role="img" aria-label="Metric, ${usable.length} data points available"><path class="grid-line" d="M0 20H400M0 65H400M0 110H400"/><path d="${path}" stroke-width="2" vector-effect="non-scaling-stroke"/>${usable.length === 1 ? '<circle cx="8" cy="110" r="3"/>' : ""}</svg></div>`;
}

function metricSummary(metric, values) {
  const list = values.map(point => point.value);
  if (["Count", "Total"].includes(metric.aggregation)) return [list.reduce((a, b) => a + b, 0), "Total over the period"];
  if (["P95", "Maximum"].includes(metric.aggregation)) return [Math.max(...list), "Peak over the period"];
  return [list.length ? list.reduce((a, b) => a + b, 0) / list.length : null, "Average over the period"];
}

function renderInsights(data) {
  const labels = { search: "Azure AI Search", foundry: "Microsoft Foundry", hosting: "Shared App Service plan", downloads: "File access" };
  const names = { SearchLatency: "Search latency", ThrottledSearchQueriesPercentage: "Throttled Search queries", DocumentsProcessedCount: "Processed documents", ModelRequests: "Model requests", ModelRequests429: "Throttled model requests (429)", TimeToResponse: "Model response time", TotalTokens: "Processed tokens", FoundryModelEstimatedCost: "Estimated model cost (Azure metric)", CpuPercentage: "Plan CPU", MemoryPercentage: "Plan memory", Requests: "HTTP requests", Http5xx: "HTTP 5xx errors", DurationMs: "HTTP response time", P95DurationMs: "HTTP P95 latency" };
  return header() + Object.entries(data.resources).map(([key, resource]) => `<section style="margin-bottom:28px"><div class="card-head"><h2>${e(labels[key])}</h2><span class="small muted">${e(date(data.start))} → ${e(date(data.end))}</span></div>${resource.status === "error" ? notice(resource.error, true) : `<div class="metric-grid">${resource.metrics.map(metric => {
    const values = metric.points?.filter(point => Number.isFinite(point.value)) || [];
    const [summary, summaryLabel] = metricSummary(metric, values);
    return `<article class="card"><div class="metric-title">${e(names[metric.name] || metric.name)}</div><div class="metric-sub">${e(metric.aggregation || "Metric unavailable")} · ${e(metric.unit)}</div>${metric.status === "unavailable" ? empty(metric.reason) : `<div class="metric-number">${values.length ? number(summary) : "—"}</div>${sparkline(metric.points)}<div class="chart-label"><span>${summaryLabel}</span><span>${values.length} points</span></div>`}</article>`;
  }).join("")}</div>`}</section>`).join("") + notice("Plan metrics include all hosted applications. TimeToResponse measures a model call, not the agent's complete response time. Estimated model cost is not the total Azure bill. File metrics come from aggregated Application Insights queries without URLs or content.") + footer(data.end);
}

function renderConfiguration(data, state) {
  const keys = { resourceGroup: "Resource group", subscriptionId: "Subscription", searchService: "Search service", searchApiVersion: "Search API", knowledgeSource: "Knowledge source", knowledgeBase: "Knowledge base", filesAccount: "Azure Files account", foundryAccount: "Foundry account", downloadApp: "File access application", hostingPlan: "Hosting plan", permissionMode: "Permission model", directoryLookupEnabled: "Directory search enabled", consoleAdministratorCount: "Console administrators" };
  return header() + `<div class="tabs"><button class="${configurationTab === "settings" ? "active" : ""}" data-tab="settings">${icon("settings")}Integration</button><button class="${configurationTab === "audit" ? "active" : ""}" data-tab="audit">${icon("clock")}Audit log</button></div>` + (configurationTab === "settings" ? `<section class="card"><div class="card-head"><h2>Environment configuration</h2><span class="pill">No secrets</span></div><dl class="key-values" style="grid-template-columns:minmax(160px,.4fr) minmax(0,1fr)">${Object.entries(keys).map(([key, label]) => `<dt>${label}</dt><dd class="mono">${e(typeof data[key] === "boolean" ? (data[key] ? "Yes" : "No") : data[key])}</dd>`).join("")}</dl></section>${notice("Changing identities, endpoints, or models requires a reviewed deployment change.")}` : `<section class="card"><div class="card-head"><h2>Administrative history</h2><span class="page-count">Last 500 events retained</span></div>${state.audit.length ? `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Action</th><th>Entra actor</th><th>Target</th></tr></thead><tbody>${state.audit.map(event => `<tr><td>${e(date(event.time))}</td><td>${e(event.action)}</td><td class="mono">${e(event.actor)}</td><td class="mono">${e(event.detail)}</td></tr>`).join("")}</tbody></table></div>` : empty("No actions recorded.")}</section>`) + footer();
}

const link = (href, text) => `<a target="_blank" rel="noopener noreferrer" href="${e(href)}">${e(text)}${icon("external")}</a>`;

function step(title, body, done) {
  const [mark, label] = done === true ? ["good", "Verified"] : done === false ? ["bad", "To fix"] : ["", "Manual"];
  return `<li><span class="pill ${mark} step-mark">${label}</span><div><div class="cell-title">${e(title)}</div><div class="step-body">${body}</div></div></li>`;
}

function guide(title, subtitle, steps) {
  return `<section class="card" style="margin-top:24px"><div class="card-head"><h2>${e(title)}</h2><span class="page-count">${e(subtitle)}</span></div><ol class="steps">${steps.join("")}</ol></section>`;
}

function sectionCard(title, section, rows) {
  return `<section class="card"><div class="card-head"><h2>${e(title)}</h2>${section.status === "ok" ? '<span class="pill good">Azure data</span>' : badge("failed")}</div>${section.status === "ok" ? `<dl class="key-values" style="grid-template-columns:minmax(140px,.4fr) minmax(0,1fr);margin-top:0">${rows(section.data).map(([key, value]) => `<dt>${e(key)}</dt><dd class="mono">${value}</dd>`).join("")}</dl>` : notice(section.error, true)}</section>`;
}

function renderMcp(data) {
  const app = data.app.data || {}, configured = data.settings.data?.values || {};
  const running = app.state === "Running";
  const audiences = (configured.MCP_TOKEN_AUDIENCES || "").split(",").map(value => value.trim()).filter(Boolean);
  const ssoUri = audiences.find(value => value.startsWith("api://auth-"));
  const registrationId = ssoUri ? ssoUri.slice("api://auth-".length).split("/")[0] : "";
  const allowed = (configured.MCP_ALLOWED_CLIENT_IDS || "").split(",").map(value => value.trim());
  const known = data.settings.status === "ok";
  const check = condition => known ? condition : null;
  const probe = (value, expected) => value === null || value === undefined ? (running ? "Unreachable" : "Not tested (stopped)") : `HTTP ${value}${value === expected ? " ✓" : ` (expected ${expected})`}`;
  const power = data.app.status === "ok"
    ? `<button data-mcp-power="${running ? "stop" : "start"}" class="${running ? "" : "primary"}">${icon(running ? "pause" : "play")}${running ? "Stop MCP server" : "Start MCP server"}</button>` : "";
  return header()
    + `<div class="grid-4">${stat("Web App", app.state || "Unavailable", app.plan ? `${app.plan} · ${app.planSku}` : data.app.error || "", "layers")}${stat("Health (/healthz)", data.probes.health ?? "—", probe(data.probes.health, 200), "check")}${stat("Anonymous /mcp", data.probes.anonymousMcp ?? "—", probe(data.probes.anonymousMcp, 401), "shield")}${stat("Copilot connector", ssoUri ? "SSO ready" : "Not registered", ssoUri ? `Registration ${registrationId}` : "Teams Developer Portal step pending", "bot")}</div>`
    + `<section class="card"><div class="card-head"><h2>MCP server</h2><div class="head-actions">${badge(app.state)}${power}</div></div><dl class="key-values" style="grid-template-columns:minmax(140px,.4fr) minmax(0,1fr);margin-top:0"><dt>Endpoint (to paste in Microsoft 365)</dt><dd class="mono">${e(data.endpoint)}</dd><dt>Web App</dt><dd>${app.portal ? link(app.portal, data.appName) : e(data.appName)}</dd><dt>HTTPS only</dt><dd>${e(app.httpsOnly === undefined ? "—" : app.httpsOnly ? "Yes" : "No")}</dd><dt>Identity</dt><dd class="mono">${e(app.identity)}${app.userAssignedClientIds?.length ? ` · UAMI ${e(app.userAssignedClientIds.join(", "))}` : ""}</dd><dt>Entra app (client ID)</dt><dd class="mono">${e(configured.MCP_CLIENT_ID)}</dd><dt>Token audiences</dt><dd class="mono">${audiences.map(e).join("<br>") || "—"}</dd><dt>Allowed clients</dt><dd class="mono">${e(configured.MCP_ALLOWED_CLIENT_IDS)}</dd><dt>Required scope</dt><dd class="mono">${e(configured.MCP_REQUIRED_SCOPE)}</dd><dt>Search endpoint</dt><dd class="mono">${e(configured.SEARCH_ENDPOINT)}</dd><dt>Tenant</dt><dd class="mono">${e(data.tenantId)}</dd></dl>${data.settings.status === "error" ? notice(`App settings unavailable: ${data.settings.error}`, true) : ""}${notice("Stopping the server immediately breaks the Copilot connector for every user; nothing else is changed. Users only ever see documents allowed for their own Entra groups.")}</section>`
    + guide("1 · Azure prerequisites", "Checked live", [
      step("MCP Web App running", `The App Service hosting the MCP server must be <strong>Running</strong>. Use the button above to start it.`, data.app.status === "ok" ? running : null),
      step("Health endpoint responds", `<span class="mono">GET https://${e(app.host || "…")}/healthz</span> must return 200.`, running ? data.probes.health === 200 : false),
      step("Anonymous calls rejected", `An unauthenticated <span class="mono">POST /mcp</span> must return 401: only Entra tokens issued for the connector are accepted.`, running ? data.probes.anonymousMcp === 401 : false),
      step("Hosted mode with on-behalf-of (no secret)", `<span class="mono">MCP_MODE=hosted</span> and <span class="mono">MCP_FIC_IDENTITY_CLIENT_ID</span> set: the server exchanges each user's token through a federated credential on its managed identity, so Search filters documents per user.`, check(configured.MCP_MODE === "hosted" && Boolean(configured.MCP_FIC_IDENTITY_CLIENT_ID))),
      step("Legacy client secret removed", `After a successful Copilot test, delete the <span class="mono">MCP_CLIENT_SECRET</span> app setting, its Key Vault secret and the Entra app secret. ${data.settings.data?.legacyClientSecret ? "<strong>Still present (fallback).</strong>" : "Not present."}`, check(!data.settings.data?.legacyClientSecret)),
      step("Users and groups assigned to the Entra app", `Entra admin center → Enterprise applications → the MCP app (client <span class="mono">${e(configured.MCP_CLIENT_ID)}</span>, not the <em>id-…</em> managed identity) and the file download app → <em>Users and groups</em>: assign the document groups (RAG-articles, RAG-invoices). Assignment is required, so unassigned users cannot sign in to the connector or open files. ${link("https://entra.microsoft.com/#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview", "Open Entra")}`, null),
    ])
    + guide("2 · Teams Developer Portal — SSO registration", "One time", [
      step("Register the Entra SSO client", `${link("https://dev.teams.microsoft.com/tools/entra-sso", "dev.teams.microsoft.com")} → Tools → <em>Microsoft Entra SSO client ID registration</em> → New: client ID <span class="mono">${e(configured.MCP_CLIENT_ID)}</span>, base URL <span class="mono">https://${e(app.host || "…")}</span>, restrict to your organization. Copy the <strong>registration ID</strong>.`, check(Boolean(ssoUri))),
      step("Add the generated Application ID URI", `Add <span class="mono">api://auth-&lt;registration ID&gt;/${e(configured.MCP_CLIENT_ID)}</span> to the Entra app's <em>Expose an API → Application ID URIs</em>, and to the <span class="mono">MCP_TOKEN_AUDIENCES</span> app setting.${ssoUri ? ` Current: <span class="mono">${e(ssoUri)}</span>.` : ""}`, check(Boolean(ssoUri))),
      step("Allow the Microsoft 365 client", `<span class="mono">MCP_ALLOWED_CLIENT_IDS</span> must contain <span class="mono">${M365_CLIENT_ID}</span> (Microsoft 365 / Teams), and the scope <span class="mono">access_as_user</span> must be exposed and pre-authorized for it.`, check(allowed.includes(M365_CLIENT_ID))),
    ])
    + guide("3 · Microsoft 365 admin center — create the connector", "Global or AI Administrator", [
      step("Create the MCP connector", `${link("https://admin.microsoft.com/#/copilot/connectors", "admin.microsoft.com")} → Copilot → Connectors → Gallery → <em>Create a new connector</em> → <em>Connect to MCP server</em>. Name (e.g. <strong>JLO RAG</strong>), server URL <span class="mono">${e(data.endpoint)}</span>, authentication <em>Microsoft Entra SSO</em> with registration ID <span class="mono">${e(registrationId || "<registration ID>")}</span>.`, null),
      step("Roll out to a pilot group", `Under <em>Rollout</em>, target a pilot group first (members of RAG-articles / RAG-invoices), then everyone once validated. Activation can take a few hours.`, null),
      step("Licenses", `Each user needs a <strong>Microsoft 365 Copilot</strong> license (Copilot Studio or pay-as-you-go alone is not enough). Assign it in admin center → Billing → Licenses.`, null),
    ])
    + guide("4 · End user", "Each user", [
      step("Enable the connector in Copilot", `Open ${link("https://m365.cloud.microsoft/chat", "Microsoft 365 Copilot")} (or Copilot in Teams) → <em>Sources / Connectors</em> → enable <strong>JLO RAG</strong> and accept the sign-in consent on first use.`, null),
      step("Ask a question", `Example: <em>"Using JLO RAG, find invoice DRP-2026-2350 and give me the total"</em> or <em>"List the Excel files about budgets"</em>. Answers only include documents the user's groups allow; file links open through the secured download Function.`, null),
      step("If nothing is returned", `Check the user's license, group membership, Entra app assignment, and that this server is Running. Each tool call is logged in the Web App logs.`, null),
    ])
    + footer();
}

function renderFoundry(data) {
  const s = data.sections;
  const connection = s.connections.data?.find(item => item.target?.includes(`/knowledgebases/${data.knowledgeBase}/mcp`));
  const agent = s.agents.data?.find(item => item.name === data.agentName);
  const mcpTool = agent?.tools.find(tool => tool.type === "mcp");
  const known = section => section.status === "ok";
  const project = s.project.data || {};
  return header()
    + `<div class="grid-4">${stat("Foundry account", s.account.data?.state || "Unavailable", data.accountName, "layers")}${stat("Project", known(s.project) ? "Available" : "Unavailable", data.projectName, "folder")}${stat("Agent version", agent?.version ?? "—", agent ? `${agent.name} · ${agent.model}` : s.agents.error || "Agent not found", "bot")}${stat("Model deployments", s.deployments.data?.length ?? "—", (s.deployments.data || []).map(item => item.name).join(", "), "database")}</div>`
    + `<div class="grid-2" style="margin-top:0">${sectionCard("Foundry resources", s.account, account => [["Account", link(account.portal, account.name)], ["Region", e(account.location)], ["State", e(account.state)], ["Project", known(s.project) ? link(project.portal, data.projectName) : e(s.project.error)], ["Project endpoint", e(project.endpoint)]])}${sectionCard("Agent", s.agents, () => agent ? [["Name", e(agent.name)], ["Version (latest)", e(agent.version)], ["Kind", e(agent.kind)], ["Model", e(agent.model)], ["Instructions", agent.hasInstructions ? "Set" : "Missing"], ["Tools", agent.tools.map(tool => `${e(tool.type)}${tool.label ? ` · ${e(tool.label)}` : ""}${tool.connection ? ` → ${e(tool.connection)}` : ""}`).join("<br>") || "None"]] : [["Agent", `Not found: ${e(data.agentName)}`]])}</div>`
    + `<div class="grid-2">${sectionCard("Knowledge-base connections", s.connections, rows => rows.length ? rows.map(item => [item.name, `${e(item.category)} · ${e(item.authType)}<br>${e(item.target)}`]) : [["—", "No connection"]])}${sectionCard("Model deployments", s.deployments, rows => rows.length ? rows.map(item => [item.name, `${e(item.model)} ${e(item.version)} · ${e(item.sku)} · capacity ${e(item.capacity)}`]) : [["—", "No deployment"]])}</div>`
    + notice("The Foundry agent and the MCP connector are two independent ways to reach the same knowledge base. Both always filter documents with the signed-in user's Entra identity.")
    + guide("1 · Foundry project and knowledge base", "Checked live", [
      step("Foundry account provisioned", `Account <span class="mono">${e(data.accountName)}</span> must be in the <em>Succeeded</em> state.`, known(s.account) ? s.account.data.state === "Succeeded" : null),
      step("Project available", `Project <span class="mono">${e(data.projectName)}</span>; its managed identity needs <strong>Search Index Data Reader</strong> on <span class="mono">${e(data.searchService)}</span>.`, known(s.project) ? true : null),
      step("Chat and embedding models deployed", `A chat model (gpt-5-mini) and <span class="mono">text-embedding-3-small</span>. Increase capacity if Copilot shows <em>tool_user_error</em> or 429.`, known(s.deployments) ? s.deployments.data.length >= 2 : null),
      step("Knowledge-base MCP connection", `Foundry portal → project → <em>Connected resources</em> → New connection → <em>Remote tool (MCP)</em>: target <span class="mono">https://${e(data.searchService)}.search.windows.net/knowledgebases/${e(data.knowledgeBase)}/mcp?api-version=…</span>, authentication <em>Project managed identity</em>.${connection ? ` Found: <span class="mono">${e(connection.name)}</span>.` : ""}`, known(s.connections) ? Boolean(connection) : null),
    ])
    + guide("2 · Agent", "Foundry portal", [
      step("Agent created with the knowledge-base tool", `${link("https://ai.azure.com", "ai.azure.com")} → project → Agents → <span class="mono">${e(data.agentName)}</span>: model gpt-5-mini, instructions from <span class="mono">agent-instructions.txt</span>, tool <em>MCP</em> using the connection above, approval <em>never</em>.`, known(s.agents) ? Boolean(agent && mcpTool) : null),
      step("Test in the playground", `Ask a question in the agent playground. The playground uses your own identity, so you only get the documents of your groups. A successful test there does not validate Copilot or Teams.`, null),
    ])
    + guide("3 · Publish to Microsoft 365 Copilot and Teams", "Foundry + M365 admin center", [
      step("Publish the agent", `Agent → <em>Publish</em> → <em>Microsoft 365 Copilot and Teams</em>: fill in name, descriptions, icons, then choose <strong>Organization</strong> scope (Individual is for testing only). Current publication: Document Assistant v1.0.2.`, null),
      step("Approve in the Microsoft 365 admin center", `${link("https://admin.microsoft.com/#/agents/all/requested", "admin.microsoft.com")} → Agents → <em>Requests</em> (or Settings → Integrated apps): approve the agent and assign users or groups (RAG-articles, RAG-invoices).`, null),
      step("Instruction changes", `The published endpoint serves the latest agent version: new instructions go live without re-approval. Republish only to change the name, description or icons.`, null),
    ])
    + guide("4 · End user", "Each user", [
      step("Open the agent", `In ${link("https://m365.cloud.microsoft/chat", "Microsoft 365 Copilot")} or Teams, open <strong>Document Assistant</strong> from the agents list (Agent Store → Built for your org if it is not pinned yet).`, null),
      step("Ask a question", `Example: <em>"Which invoices mention DRP-2026-2350?"</em>. The answer starts with a table of documents and ends with <strong>Open files</strong> links.`, null),
      step("If nothing is returned", `Check the user's Copilot license, group membership and agent assignment; for 429 errors increase the model capacity.`, null),
    ])
    + footer();
}

async function load(silent = false) {
  if (busy || !account) return;
  busy = true;
  const request = ++loadSequence;
  if (!silent) main.innerHTML = header() + '<div class="loading"><span class="spinner"></span>Loading Azure resources…</div>';
  try {
    const inventory = ["purges", "maintenance", "insights", "configuration"].includes(page)
      ? api("/api/sources").then(data => { cachedSources = data; sourceInventoryError = ""; })
        .catch(error => { cachedSources = null; sourceInventoryError = error.message; })
      : undefined;
    if (page === "overview") {
      cachedOverview = await api("/api/overview");
      cachedSources = cachedOverview.sections.sources?.data;
      sourceInventoryError = cachedOverview.sections.sources?.error || "";
    } else if (page === "sources" || page === "sync") {
      const stateRequest = api("/api/state").then(data => {
        if (purgeScheduleLabel(data) === "Unavailable") throw new Error("The purge schedule is invalid or missing.");
        cachedState = data;
        purgeStateError = "";
      }).catch(error => {
        cachedState = null;
        purgeStateError = error.message;
      });
      [cachedSources] = await Promise.all([api("/api/sources"), stateRequest]);
    } else if (page === "groups") {
      [cachedSources, cachedGroups, cachedState] = await Promise.all([api("/api/sources"), api("/api/groups"), api("/api/state")]);
    } else if (page === "purges" || page === "maintenance") {
      cachedState = await api("/api/state");
    } else if (page === "insights") {
      cachedMetrics = await api(`/api/metrics?hours=${hours}`);
    } else if (page === "mcp") {
      cachedMcp = await api("/api/integrations/mcp");
    } else if (page === "foundry") {
      cachedFoundry = await api("/api/integrations/foundry");
    } else {
      [cachedConfiguration, cachedState] = await Promise.all([api("/api/configuration"), api("/api/state")]);
    }
    await inventory;
    cachedSources?.indexers.forEach(row => {
      if (["PT5M", "PT15M", "PT30M", "PT1H"].includes(row.schedule)) previousSchedules.set(row.name, row.schedule);
    });
    if (!["purges", "maintenance", "insights", "configuration", "overview"].includes(page)) sourceInventoryError = "";
    renderCurrent();
  } catch (error) {
    if (silent) toast(error.message, true);
    else main.innerHTML = header() + `<div class="banner-error"><strong>Unable to load this page.</strong><p style="margin-top:8px">${e(error.message)}</p></div><button data-action="refresh">Retry</button>`;
  } finally { if (request === loadSequence) busy = false; }
}

function renderCurrent() {
  if (page === "overview") main.innerHTML = renderOverview(cachedOverview);
  else if (page === "sources") main.innerHTML = renderSources(cachedSources, cachedState, purgeStateError);
  else if (page === "sync") main.innerHTML = renderSync(cachedSources, cachedState, purgeStateError);
  else if (page === "groups") main.innerHTML = renderGroups(cachedSources, cachedGroups, cachedState);
  else if (page === "purges") main.innerHTML = header() + notice("Global purges affect ALL shares, including paused shares, regardless of the filters above.") + renderRebuild(cachedState) + renderOperations(cachedState, true) + footer();
  else if (page === "maintenance") main.innerHTML = header() + notice("Access changes affect the shared index for ALL shares, regardless of the filters above.") + renderOperations(cachedState) + renderAudit(cachedState) + footer();
  else if (page === "insights") main.innerHTML = renderInsights(cachedMetrics);
  else if (page === "mcp") main.innerHTML = renderMcp(cachedMcp);
  else if (page === "foundry") main.innerHTML = renderFoundry(cachedFoundry);
  else main.innerHTML = renderConfiguration(cachedConfiguration, cachedState);
}

function navigate(next) {
  if (!pages.some(([key]) => key === next) || busy) return;
  page = next;
  history.replaceState(null, "", `${location.pathname}#${page}`);
  document.querySelectorAll("[data-nav]").forEach(button => button.classList.toggle("active", button.dataset.nav === page));
  document.querySelector("#breadcrumb").textContent = pages.find(([key]) => key === page)[1];
  load();
}

function openDialog(html, keepFiles = false) {
  if (!keepFiles) { fileDialog = null; fileRequestSequence++; }
  modal.innerHTML = html;
  modal.showModal();
}

function actionErrorMessage(error) {
  const secret = typeof error.setting === "string" && /^FILES_CONNECTION_[A-Z0-9]+$/.test(error.setting)
    ? `files-connection-${error.setting.slice("FILES_CONNECTION_".length).toLowerCase()}` : "";
  if (error.code === "missing_storage_connection" && secret) {
    return `Connection not started: no Key Vault connection is configured for this account (${error.setting}). An Azure administrator must create and reference the ${secret} secret in the console vault. No key is required here.`;
  }
  if (error.code === "storage_connection_unresolved" && secret) {
    return `Connection not started: the Key Vault reference ${error.setting} could not be resolved. Verify that the console's managed identity has the Key Vault Secrets User role for ${secret}, then restart the console.`;
  }
  if (error.code === "key_vault_not_configured") {
    return "Connection not started: the console's Key Vault (KEY_VAULT_NAME) is not configured.";
  }
  if (error.code === "downloader_not_ready") {
    return "Connection not started: the file access service is not ready for multiple accounts. An administrator must deploy and verify the compatible Function before setting its readiness marker.";
  }
  return `The request could not be confirmed: ${error.message}. Check its status in Purges or Logs before retrying.`;
}

async function submitDialogAction(button, action, options = {}) {
  if (button.dataset.pending) return;
  const feedback = modal.querySelector("[data-dialog-feedback]");
  const closeButton = modal.querySelector("[data-close]");
  const label = button.textContent;
  const closeLabel = closeButton.textContent;
  const controls = Array.from(modal.querySelectorAll("input, select, textarea"), element => ({
    element, disabled: element.disabled,
  }));
  controls.forEach(({ element }) => { element.disabled = true; });
  button.dataset.pending = "true";
  button.disabled = true;
  button.textContent = "Please wait…";
  closeButton.textContent = "Close";
  feedback.setAttribute("role", "status");
  feedback.innerHTML = `<div class="notice"><span class="spinner" aria-hidden="true"></span><div>Checking and saving. Closing this window does not cancel a request already sent.</div></div>`;
  try {
    await action();
  } catch (error) {
    if (modal.open && modal.contains(button)) {
      button.disabled = false;
      button.textContent = label;
      closeButton.textContent = closeLabel;
      feedback.setAttribute("role", "alert");
      feedback.innerHTML = notice(actionErrorMessage(error), true);
    } else {
      toast(actionErrorMessage(error), true);
    }
    return;
  } finally {
    delete button.dataset.pending;
    controls.forEach(({ element, disabled }) => { element.disabled = disabled; });
  }
  const currentDialog = modal.open && modal.contains(button);
  if (currentDialog) {
    modal.close();
    if (options.onSuccess) options.onSuccess();
  }
  toast(options.successMessage || "Request submitted.");
  if (currentDialog || !modal.open) await load();
}

async function sourceDialog() {
  openDialog('<h2 id="modal-title">Connect a share</h2><p>Loading Azure inventory…</p><div class="modal-actions"><button data-close>Cancel</button></div>');
  try {
    const [resources, groups] = await Promise.all([api("/api/resources/accounts"), api("/api/groups")]);
    if (!modal.open) return;
    resourceAccounts = resources.accounts;
    const resourceGroups = [...new Set(resourceAccounts.map(item => item.resourceGroup))].sort();
    modal.innerHTML = `<h2 id="modal-title">Connect an Azure file share</h2><p>Subscription: <span class="mono">${e(resources.subscriptionId)}</span>. Select an existing SMB share.</p>${notice("Prerequisites: the account connection is stored in Key Vault (secret files-connection-<account>) and readable by the console's managed identity; the multi-account Function and agent instructions are up to date. No Azure permissions are granted automatically.")}<form id="source-form"><div class="field"><label for="source-rg">Resource group</label><select id="source-rg" required><option value="">Choose a resource group</option>${resourceGroups.map(value => `<option value="${e(value)}">${e(value)}</option>`).join("")}</select></div><div class="field"><label for="source-account">Storage account</label><select id="source-account" required disabled><option value="">Choose an account</option></select></div><div class="field"><label for="source-share">SMB share</label><select id="source-share" required disabled><option value="">Choose a share</option></select><p id="source-error" class="field-help error-text" role="alert"></p></div>${renderGroupPicker("source-group", groups)}<div class="field"><label for="source-interval">Refresh frequency for this share</label><select id="source-interval">${["PT5M", "PT15M", "PT30M", "PT1H", ""].map(value => `<option value="${value}">${e(scheduleLabel(value))}</option>`).join("")}</select></div><p class="small muted">The connection creates a source, skillset, and indexer for each slot, configures links, then indexes the files (embedding costs apply). It does not create an Azure account, share, or group.</p><div class="modal-actions"><button type="button" data-close>Cancel</button><button type="submit" class="primary">Review connection</button></div></form>`;
  } catch (error) {
    if (modal.open) modal.innerHTML = `<h2 id="modal-title">Inventory unavailable</h2>${notice(error.message, true)}<p>Check Reader permissions on the relevant resource groups or subscription.</p><div class="modal-actions"><button data-close>Close</button></div>`;
  }
}

function confirmation(title, text, action, options = {}) {
  openDialog(`<h2 id="modal-title">${e(title)}</h2><p>${e(text)}</p><div data-dialog-feedback role="status" aria-live="polite"></div><div class="modal-actions"><button data-close>Cancel</button><button class="primary" id="confirm-action">Confirm</button></div>`);
  const button = document.querySelector("#confirm-action");
  button.onclick = () => submitDialogAction(button, action, options);
}

document.addEventListener("click", event => {
  const toggle = event.target.closest?.("[data-file-toggle]");
  if (toggle?.dataset?.fileToggle !== undefined && fileDialog?.view) {
    const id = Number(toggle.dataset.fileToggle), expanded = fileDialog.view.expanded;
    expanded.has(id) ? expanded.delete(id) : expanded.add(id);
    renderFileExplorer(fileDialog);
    return;
  }
  const folder = event.target.closest?.("[data-file-folder]");
  if (folder?.dataset?.fileFolder !== undefined) {
    selectFileFolder(Number(folder.dataset.fileFolder));
    return;
  }
  const button = event.target.closest("button");
  if (!button) return;
  if (button.disabled) return;
  if (button.hasAttribute("data-file-offset")) {
    if (fileDialog?.view) {
      fileDialog.view.offset = Number(button.dataset.fileOffset);
      document.querySelector("[data-file-results]").innerHTML = renderFileResults(fileDialog.view);
    }
    return;
  }
  if (button.hasAttribute("data-file-retry")) {
    showFilePage();
    return;
  }
  if (button.dataset.jobFiles) {
    fileDialog = { kind: button.dataset.jobFiles, sourceKey: button.dataset.sourceKey, slot: button.dataset.slot || "" };
    showFilePage();
    return;
  }
  if (button.dataset.failedFiles) {
    showFailures(button.dataset.failedFiles);
    return;
  }
  if (button.dataset.purgeFiles) {
    fileDialog = { kind: "purge", operationId: button.dataset.purgeFiles };
    showFilePage();
    return;
  }
  if (button.hasAttribute("data-group-more")) {
    requestGroupPage(button.closest("[data-group-picker]"), true);
    return;
  }
  if (button.hasAttribute("data-group-retry")) {
    requestGroupPage(button.closest("[data-group-picker]"), button.dataset.append === "true");
    return;
  }
  if (button.dataset.nav) navigate(button.dataset.nav);
  if (button.hasAttribute("data-close")) modal.close();
  if (button.dataset.action === "refresh") load();
  if (button.dataset.action === "add-source") sourceDialog();
  if (button.dataset.mcpPower) {
    const action = button.dataset.mcpPower;
    confirmation(action === "stop" ? "Stop the MCP server?" : "Start the MCP server?", action === "stop"
      ? "The Microsoft 365 Copilot connector stops answering for every user until the server is started again. The index and the Foundry agent are not affected."
      : "Start the App Service hosting the MCP server. It can take up to a minute before /healthz answers.",
    () => api("/api/integrations/mcp/power", { action, confirmed: true }),
    { successMessage: action === "stop" ? "MCP server stopped." : "MCP server started." });
  }
  if (button.dataset.action === "reset-scope" && !busy) {
    scope = { resourceGroup: "", account: "", sourceKey: "" };
    renderCurrent();
  }
  if (button.dataset.run) {
    const name = button.dataset.run;
    const paused = cachedSources?.indexers.find(row => row.name === name)?.schedule === null;
    confirmation(paused ? "Manually refresh a paused share?" : "Run refresh?", `${paused ? "This share is paused. Manual refresh is still possible; confirming starts one run without resuming its schedule. " : ""}Run incremental indexing for ${name}. Embedding costs may apply; deleted files are not purged.`, () => api(`/api/indexers/${encodeURIComponent(name)}/actions`, { action: "run", operationId: crypto.randomUUID(), confirmed: true }));
  }
  if (button.dataset.refreshPause || button.dataset.refreshResume) {
    const name = button.dataset.refreshPause || button.dataset.refreshResume;
    const pause = Boolean(button.dataset.refreshPause);
    const schedule = cachedSources?.indexers.find(row => row.name === name)?.schedule;
    const interval = previousSchedules.get(name) || "PT5M";
    confirmation(pause ? "Pause scheduled refresh?" : "Resume scheduled refresh?", pause
      ? `${name}: pause scheduled refreshes. A run already in progress is not interrupted. Confirmed manual refresh is still possible. Global purge still includes this share.`
      : `${name}: ${scheduleLabel(interval)}. Global purge includes this share whether paused or resumed.`,
    () => api(`/api/indexers/${encodeURIComponent(name)}/actions`, { action: pause ? "pause" : "schedule", interval: pause ? undefined : interval, operationId: crypto.randomUUID(), confirmed: true }),
    { onSuccess: () => { if (pause && ["PT5M", "PT15M", "PT30M", "PT1H"].includes(schedule)) previousSchedules.set(name, schedule); } });
  }
  if (button.dataset.resume) {
    const type = cachedState?.operations.find(op => op.id === button.dataset.resume)?.type;
    confirmation(type === "source_add" ? "Resume connection?" : "Resume maintenance?", type === "source_add" ? "Fix the reported error and prerequisites first. The connection resumes from its saved step without overwriting a different configuration." : type === "rebuild" ? "Before the switch, the rebuild restarts in the inactive index while the agent uses the current index. After the switch, only schedule restoration resumes." : "The operation resumes from its saved phase. Fix the Azure error first; search may remain unavailable and links restricted.", () => api(`/api/operations/${button.dataset.resume}/resume`, { confirmed: true }));
  }
  if (button.dataset.removeSource) {
    const key = button.dataset.removeSource;
    confirmation(`Remove share ${key}?`, `${key}: deletes its indexers, skillsets, Search data source and file-link mapping, then starts a purge that deletes its chunks from the agent index. Files in Azure Files are not deleted. The share can be connected again later.`, () => api("/api/sources/remove", { sourceKey: key, operationId: crypto.randomUUID(), confirmed: true }));
  }
  if (button.hasAttribute("data-rebuild-now")) {
    confirmation("Purge deleted files now?", "All shares, including paused shares, will be listed and the index chunks of files that no longer exist will be deleted. Nothing is reindexed. Source files are not deleted.", () => api("/api/rebuild/run", { operationId: crypto.randomUUID(), confirmed: true }));
  }
  if (button.hasAttribute("data-clear-logs")) {
    confirmation("Clear the log?", "All operations and the administrative history are removed from Logs. Purge history remains in the Purges tab. Azure resources are not changed.", () => api("/api/logs/clear", { confirmed: true }));
  }
  if (button.dataset.cancel) {
    confirmation("Cancel the unstarted operation?", "No Azure resource must have been changed yet. Once maintenance has started, only a controlled resume is allowed.", () => api(`/api/operations/${button.dataset.cancel}/cancel`, { confirmed: true }));
  }
  if (button.dataset.tab) { configurationTab = button.dataset.tab; load(); }
});

document.addEventListener("input", event => {
  const input = event.target;
  if (input.hasAttribute("data-file-search") && fileDialog?.view) {
    Object.assign(fileDialog.view, { query: input.value, offset: 0 });
    document.querySelector("[data-file-results]").innerHTML = renderFileResults(fileDialog.view);
    return;
  }
  if (!input.hasAttribute("data-group-query")) return;
  const picker = input.closest("[data-group-picker]");
  picker.dataset.requestId = String(++groupSearchSequence);
  clearTimeout(groupSearchTimers.get(input));
  picker.querySelector("[data-group-select]").innerHTML = '<option value="">Searching…</option>';
  const more = picker.querySelector("[data-group-more]");
  more.hidden = true;
  more.dataset.cursor = "";
  picker.querySelector("[data-group-retry]").hidden = true;
  const status = picker.querySelector("[data-group-status]");
  status.classList.remove("error-text");
  status.setAttribute("role", "status");
  status.textContent = "Searching groups…";
  groupSearchTimers.set(input, setTimeout(() => {
    if (picker.isConnected) requestGroupPage(picker);
  }, 300));
});

document.addEventListener("change", async event => {
  if (event.target.dataset.scope && !busy) {
    scope = changeScope(scope, event.target.dataset.scope, event.target.value);
    renderCurrent();
    return;
  }
  if (event.target.id === "source-rg") {
    const accountSelect = document.querySelector("#source-account");
    const values = resourceAccounts.filter(item => item.resourceGroup === event.target.value);
    accountSelect.innerHTML = '<option value="">Choose an account</option>' + values.map(item => `<option value="${e(item.account)}">${e(item.account)}</option>`).join("");
    accountSelect.disabled = !values.length;
    document.querySelector("#source-share").innerHTML = '<option value="">Choose a share</option>';
    document.querySelector("#source-share").disabled = true;
    document.querySelector("#source-error").textContent = "";
    return;
  }
  if (event.target.id === "source-account") {
    const accountName = event.target.value, rg = document.querySelector("#source-rg").value;
    const shareSelect = document.querySelector("#source-share"), errorLabel = document.querySelector("#source-error");
    shareSelect.disabled = true;
    shareSelect.innerHTML = '<option value="">Loading…</option>';
    errorLabel.textContent = "";
    if (!accountName) { shareSelect.innerHTML = '<option value="">Choose an account</option>'; return; }
    try {
      const data = await api(`/api/resources/shares?resourceGroup=${encodeURIComponent(rg)}&account=${encodeURIComponent(accountName)}`);
      if (!modal.contains(shareSelect) || document.querySelector("#source-account").value !== accountName || document.querySelector("#source-rg").value !== rg) return;
      const connected = cachedSources.indexers.filter(row => row.active);
      const shares = data.shares.filter(item => item.protocol === "SMB" && !connected.some(row => row.account === accountName && row.share === item.share));
      shareSelect.innerHTML = '<option value="">Choose a share</option>' + shares.map(item => `<option value="${e(item.share)}">${e(item.share)}</option>`).join("");
      shareSelect.disabled = !shares.length;
      if (!shares.length) errorLabel.textContent = "No unconnected SMB shares in this account.";
    } catch (error) {
      if (modal.contains(errorLabel) && document.querySelector("#source-account").value === accountName && document.querySelector("#source-rg").value === rg) {
        errorLabel.textContent = error.message;
        shareSelect.innerHTML = '<option value="">Unable to load shares</option>';
      }
    }
    return;
  }
  if (event.target.id === "time-range") { hours = event.target.value; load(); }
  if (event.target.dataset.schedule) {
    const name = event.target.dataset.schedule, interval = event.target.value;
    const previous = cachedSources.indexers.find(row => row.name === name).schedule || "";
    event.target.value = previous;
    confirmation("Change refresh schedule?", `${name}: ${scheduleLabel(interval)}. A run already in progress is not interrupted. Confirmed manual refresh is still possible when paused; global purge still includes paused shares.`, () => api(`/api/indexers/${encodeURIComponent(name)}/actions`, { action: interval ? "schedule" : "pause", interval: interval || undefined, operationId: crypto.randomUUID(), confirmed: true }),
      { onSuccess: () => { if (["PT5M", "PT15M", "PT30M", "PT1H"].includes(previous)) previousSchedules.set(name, previous); } });
  }
});

document.addEventListener("submit", async event => {
  if (event.target.id === "source-form") {
    event.preventDefault();
    const groupSelect = document.querySelector("#source-group");
    const groupName = groupSelect.selectedOptions[0]?.dataset.groupName || groupSelect.value;
    const body = {
      operationId: crypto.randomUUID(), confirmed: true,
      resourceGroup: document.querySelector("#source-rg").value,
      account: document.querySelector("#source-account").value,
      share: document.querySelector("#source-share").value,
      groupId: groupSelect.value,
      interval: document.querySelector("#source-interval").value || null,
    };
    confirmation("Connect this share?", `${body.resourceGroup} · ${body.account} / ${body.share} → ${groupName} (${body.groupId}). ${scheduleLabel(body.interval)}. Files will be indexed in the shared index.`, () => api("/api/sources", body), {
      successMessage: "Connection request accepted.",
      onSuccess: () => {
        scope = { resourceGroup: "", account: "", sourceKey: "" };
      },
    });
    return;
  }
  if (event.target.id === "rebuild-form") {
    event.preventDefault();
    const enabled = document.querySelector("#rebuild-enabled").checked;
    const intervalHours = Number(document.querySelector("#rebuild-interval").value);
    const startHour = Number(document.querySelector("#rebuild-hour").value);
    confirmation("Save the purge schedule?", enabled ? `Purge ${intervalLabel(intervalHours).toLowerCase()} from ${hourLabel(startHour)} (Paris time). Each run lists all shares, including paused shares, and deletes the index chunks of missing files without reindexing. No immediate run is started.` : "The scheduled purge will be disabled. Deleted files will remain in the index until a manual purge.", () => api("/api/rebuild/schedule", { enabled, intervalHours, startHour, confirmed: true }));
    return;
  }
  if (event.target.id !== "group-form") return;
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  const groupSelect = document.querySelector("#group-id");
  const selectedGroupName = groupSelect.selectedOptions[0]?.dataset.groupName || groupSelect.value;
  button.disabled = true;
  try {
    draft = await api("/api/group-changes/preview", {
      share: document.querySelector("#group-share").value,
      groupId: groupSelect.value,
    });
    const oldGroupName = cachedGroups.groups.find(group => group.id === draft.oldGroup)?.displayName;
    openDialog(`<h2 id="modal-title">Review access change</h2><p>Source: <strong>${e(draft.share)}</strong>. This operation requires maintenance, not just a metadata update.</p><div class="diff"><div><div class="diff-label">Current group</div>${oldGroupName && oldGroupName !== draft.oldGroup ? `<div>${e(oldGroupName)}</div>` : ""}<div class="mono">${e(draft.oldGroup)}</div></div><div><div class="diff-label">New group</div>${selectedGroupName !== draft.newGroup ? `<div>${e(selectedGroupName)}</div>` : ""}<div class="mono">${e(draft.newGroup)}</div></div></div>${notice("Search will be unavailable while BOTH application indexes are rebuilt. Existing chunks, including the rollback slot, will be removed and active sources reindexed. No source files are deleted.")}${notice("Links for the affected share are restricted during maintenance. Previously issued SAS links may remain valid for five minutes. If maintenance fails, the restriction remains until an explicit resume.")}<p class="small muted" style="margin-bottom:16px">The operation is saved in Azure and can continue after the browser is closed. Test final access with the affected users.</p><label for="maintenance-confirm">Type REBUILD to confirm</label><input id="maintenance-confirm" autocomplete="off" placeholder="REBUILD"><div data-dialog-feedback role="status" aria-live="polite"></div><div class="modal-actions"><button data-close>Cancel</button><button class="primary" id="apply-group" disabled>Apply and rebuild</button></div>`);
    const confirmationInput = document.querySelector("#maintenance-confirm");
    const apply = document.querySelector("#apply-group");
    confirmationInput.oninput = () => { apply.disabled = !!apply.dataset.pending || confirmationInput.value !== "REBUILD"; };
    apply.onclick = () => submitDialogAction(apply,
      () => api("/api/group-changes/apply", { draftId: draft.id, confirmation: confirmationInput.value }),
      { successMessage: "Maintenance request accepted." });
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

function signInScreen(message) {
  main.innerHTML = `<div class="auth-screen"><section class="card auth-card"><div class="brand-mark">${icon("layers")}</div><div class="eyebrow">Document management</div><h1 style="margin-top:12px">Your entire RAG pipeline.<br>One console.</h1><p class="muted">Monitor sources, manage synchronization, and control access associations without exposing Azure keys.</p>${message ? `<div class="banner-error">${e(message)}</div>` : ""}<button class="primary" id="signin">${icon("shield")}Sign in with Microsoft</button><div class="small muted">Access is restricted to explicitly authorized administrators.<br>No fictitious data or demo login.</div></section></div>`;
  document.querySelector("#signin").onclick = async () => {
    try { await msal.loginRedirect({ scopes: [config.scope], extraScopesToConsent: [config.searchScope], prompt: "select_account" }); }
    catch (error) { toast(`Sign-in failed (${error.errorCode || "authentication_error"}).`, true); }
  };
}

async function initialize() {
  document.querySelector("#brand-icon").innerHTML = icon("layers");
  document.querySelector("#theme-button").innerHTML = icon("sun");
  document.querySelector("#logout").innerHTML = icon("logout");
  document.querySelector("#theme-button").onclick = () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  };
  document.querySelector("#navigation").innerHTML = pages.map(([key, title, symbol]) => `<button data-nav="${key}" class="${key === page ? "active" : ""}">${icon(symbol)}${title}</button>`).join("");
  const response = await fetch("/public-config", { cache: "no-store" });
  if (!response.ok) throw new Error("The console's public configuration is unavailable.");
  config = await response.json();
  document.querySelector("#environment").textContent = config.resourceGroup;
  msal = new PublicClientApplication({
    auth: { clientId: config.clientId, authority: `https://login.microsoftonline.com/${config.tenantId}`, redirectUri: `${location.origin}/auth/callback`, postLogoutRedirectUri: location.origin, navigateToLoginRequestUrl: false },
    cache: { cacheLocation: "sessionStorage" },
  });
  await msal.initialize();
  const redirect = await msal.handleRedirectPromise();
  account = redirect?.account || msal.getAllAccounts()[0];
  if (!account) { signInScreen(); return; }
  msal.setActiveAccount(account);
  try {
    const me = await api("/api/me");
    document.querySelector("#user").hidden = false;
    document.querySelector("#user-name").textContent = me.name;
    document.querySelector("#avatar").textContent = me.name.split(/\s+/).slice(0, 2).map(part => part[0]).join("").toUpperCase();
    document.querySelector("#logout").onclick = () => msal.logoutRedirect({ account });
    const requestedPage = location.hash.slice(1);
    navigate(pages.some(([key]) => key === requestedPage) ? requestedPage : "overview");
  } catch (error) {
    document.querySelector("#user").hidden = false;
    document.querySelector("#user-name").textContent = "Access denied";
    document.querySelector("#logout").onclick = () => msal.logoutRedirect({ account });
    signInScreen(error.message);
  }
}

setInterval(() => {
  if (account && !modal.open && !busy && ["purges", "maintenance", "sources", "sync", "overview"].includes(page)) load(true);
}, 60000);

initialize().catch(error => {
  main.innerHTML = `<div class="auth-screen"><section class="card auth-card"><h1>Console unavailable</h1><p class="error-text">${e(error.message)}</p><button onclick="location.reload()">Retry</button></section></div>`;
});
