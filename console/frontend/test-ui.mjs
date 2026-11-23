import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import { changeScope, scopeRows, sourceKey } from "./scope.mjs";

const row = (account, resourceGroup, share, groupId) => ({
  account, resourceGroup, share, sourceKey: `${account}/${share}`, groupId, groupName: `Team ${groupId}`,
  active: true, source: `${account}-${share}`, name: `${account}-${share}-a`,
  index: "af-index-all", skillset: "skills", status: "success",
  schedule: "PT5M", errors: [], warnings: [], itemsFailed: 0, itemsProcessed: 3,
  lastPurge: { at: "2026-11-11T11:00:00Z", indexedCount: 10, purgedCount: 2 },
  indexed: { count: 10, at: "2026-11-11T11:00:00Z", from: "purge", listed: true }, lastSyncListed: false,
});
const sources = { activeIndex: "af-index-all", indexers: [
  row("firstfiles", "first-rg", "documents", "group-a"),
  row("secondfiles", "second-rg", "documents", "group-b"),
  row("secondfiles", "second-rg", "reports", "group-c"),
] };
const groups = { groups: [], directoryStatus: "not_configured", message: "Directory disabled." };
const state = {
  operations: [{ type: "group_change", id: "operation-1", share: "firstfiles/documents", status: "failed", error: "Test failure" }],
  worker: { running: true }, rebuild: { enabled: false, intervalHours: 24, startHour: 2 }, audit: [],
};

function harness(selected = {}) {
  const elements = new Map();
  const handlers = {};
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) {
        elements.set(selector, {
          innerHTML: "", textContent: "", className: "", dataset: {}, attributes: {}, disabled: false,
          setAttribute(name, value) { this.attributes[name] = value; },
        });
      }
      return elements.get(selector);
    },
    addEventListener(name, handler) { handlers[name] = handler; },
  };
  const modal = document.querySelector("#modal");
  Object.assign(modal, {
    open: false,
    listeners: {},
    showModal() { this.open = true; },
    close() { this.open = false; this.listeners.close?.(); },
    contains(element) { return this.open && !element.detached; },
    querySelector(selector) { return document.querySelector(selector); },
    querySelectorAll() { return []; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
  });
  const context = vm.createContext({
    sourceKey, scopeRows, changeScope, console, URLSearchParams, Intl, Date,
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    document,
  });
  let script = readFileSync(new URL("./main.js", import.meta.url), "utf8").replace(/^import .*;\r?$/gm, "");
  script = script.slice(0, script.lastIndexOf("initialize().catch"));
  vm.runInContext(script, context);
  vm.runInContext(`
    config = { subscriptionId: "test-subscription", resourceGroup: "common-rg" };
    cachedSources = ${JSON.stringify(sources)};
    cachedGroups = ${JSON.stringify(groups)};
    cachedState = ${JSON.stringify(state)};
    scope = { resourceGroup: "", account: "", sourceKey: "", ...${JSON.stringify(selected)} };
  `, context);
  return { context, elements, handlers, run: expression => vm.runInContext(expression, context) };
}

test("global scope includes every connected share", () => {
  assert.equal(scopeRows(sources.indexers, {}).length, 3);
});

test("RG, account and share scopes intersect, including empty results", () => {
  assert.equal(scopeRows(sources.indexers, { resourceGroup: "second-rg" }).length, 2);
  assert.equal(scopeRows(sources.indexers, { account: "firstfiles" }).length, 1);
  assert.equal(scopeRows(sources.indexers, { account: "firstfiles", sourceKey: "secondfiles/documents" }).length, 0);
});

test("identical share names in different accounts stay distinct", () => {
  const rows = scopeRows(sources.indexers, { sourceKey: "secondfiles/documents" });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].groupId, "group-b");
});

test("parent selection clears descendant filters without mutating old scope", () => {
  const before = { resourceGroup: "first-rg", account: "firstfiles", sourceKey: "firstfiles/documents" };
  assert.deepEqual(changeScope(before, "resourceGroup", "second-rg"), { resourceGroup: "second-rg", account: "", sourceKey: "" });
  assert.deepEqual(changeScope(before, "account", "secondfiles"), { resourceGroup: "first-rg", account: "secondfiles", sourceKey: "" });
  assert.equal(before.sourceKey, "firstfiles/documents");
});

test("filter controls cascade and disambiguate shares by account", () => {
  const app = harness({ resourceGroup: "second-rg", account: "secondfiles" });
  const html = app.run("renderFilters()");
  assert.match(html, /secondfiles \/ documents/);
  assert.match(html, /secondfiles \/ reports/);
  assert.doesNotMatch(html, /value="firstfiles"/);
  assert.match(html, /2 \/ 3 shares/);
});

test("filtered source settings only expose selected share actions", () => {
  const app = harness({ sourceKey: "secondfiles/documents" });
  const html = app.run("renderSources(cachedSources, cachedState)");
  assert.match(html, /secondfiles/);
  assert.match(html, /group-b/);
  assert.doesNotMatch(html, /group-a|group-c/);
  assert.match(html, /Connect a share/);
  assert.doesNotMatch(html, /data-focus-source|data-nav=/);
});

test("each share shows its own refresh frequency and the single global purge frequency", () => {
  const app = harness();
  app.run(`
    cachedState.rebuild = { enabled: true, intervalHours: 6, startHour: 2 };
    cachedSources.indexers[1].schedule = "PT15M";
    cachedSources.indexers[2].schedule = null;
  `);
  const sourceHtml = app.run("renderSources(cachedSources, cachedState)");
  assert.equal((sourceHtml.match(/<dt>Purge frequency \(global\)<\/dt><dd>Every 6 hours<\/dd>/g) || []).length, 3);
  assert.match(sourceHtml, /<dt>Refresh frequency<\/dt><dd>Every 5 min<\/dd>/);
  assert.match(sourceHtml, /<dt>Refresh frequency<\/dt><dd>Every 15 min<\/dd>/);
  assert.match(sourceHtml, /<dt>Refresh frequency<\/dt><dd>Paused<\/dd>/);

  const syncHtml = app.run("renderSync(cachedSources, cachedState)");
  assert.match(syncHtml, /<th>Refresh frequency<\/th><th>Purge frequency \(global\)<\/th>/);
  assert.equal((syncHtml.match(/<td>Every 6 hours<\/td>/g) || []).length, 3);
  assert.match(syncHtml, /value="PT5M" selected>Every 5 min/);
  assert.match(syncHtml, /value="PT15M" selected>Every 15 min/);
  assert.match(syncHtml, /same schedule for every share/);
  assert.match(syncHtml, /<th>LAST SYNC<\/th><th>INDEXED<\/th><th>PURGED<\/th><th>FAILED<\/th>/);
  assert.doesNotMatch(syncHtml, /files indexed \/ failed|0 \/ 0/);
  assert.doesNotMatch(syncHtml, /inactive-slot/i);
});

test("disabled and unavailable purge schedules are not confused with a running schedule", () => {
  const app = harness();
  const disabled = app.run("renderSources(cachedSources, cachedState)");
  assert.equal((disabled.match(/<dt>Purge frequency \(global\)<\/dt><dd>Disabled \(configured: Daily\)<\/dd>/g) || []).length, 3);
  const unavailable = app.run('renderSync(cachedSources, null, "State storage unavailable")');
  assert.equal((unavailable.match(/<td>Unavailable<\/td>/g) || []).length, 3);
  assert.match(unavailable, /Purge schedule unavailable: State storage unavailable/);
  assert.doesNotMatch(unavailable, /<td>Disabled/);
  assert.equal(app.run('purgeScheduleLabel({ rebuild: { enabled: false, intervalHours: 7 } })'), "Unavailable");
});

test("an externally configured refresh interval is shown instead of being mistaken for paused", () => {
  const app = harness();
  app.run('cachedSources.indexers[0].schedule = "PT2H"');
  const html = app.run("renderSync(cachedSources, cachedState)");
  assert.match(html, /<option value="PT2H" selected disabled>PT2H \(current\)<\/option>/);
  assert.doesNotMatch(html, /value="" selected>Paused<\/option>.*value="PT2H"/);
});

test("source and sync pages load the current purge schedule and surface state failures", async () => {
  for (const page of ["sources", "sync"]) {
    const app = harness();
    const calls = [];
    app.context.mockApi = async path => {
      calls.push(path);
      if (path === "/api/state") return { ...state, rebuild: { enabled: true, intervalHours: 8, startHour: 2 } };
      if (path === "/api/sources") return sources;
      throw new Error(`Unexpected request: ${path}`);
    };
    app.run(`page = "${page}"; account = {}; api = mockApi`);
    await app.run("load()");
    assert.deepEqual(calls.sort(), ["/api/sources", "/api/state"]);
    assert.match(app.elements.get("#main").innerHTML, /Every 8 hours/);

    app.context.mockApi = async path => {
      if (path === "/api/state") throw new Error("State storage unavailable");
      if (path === "/api/sources") return sources;
      throw new Error(`Unexpected request: ${path}`);
    };
    app.run("api = mockApi");
    await app.run("load()");
    const html = app.elements.get("#main").innerHTML;
    assert.match(html, /Purge schedule unavailable: State storage unavailable/);
    assert.match(html, /Unavailable/);
    assert.doesNotMatch(html, /Every 8 hours/);
    assert.equal(app.run("cachedState"), null);
  }
});

test("overview loads in one request and shows indexed files from the last purge", async () => {
  const app = harness();
  const calls = [];
  app.context.mockApi = async path => {
    calls.push(path);
    if (path === "/api/overview") return { sections: { sources: { status: "ok", data: sources } } };
    throw new Error(`Unexpected request: ${path}`);
  };
  app.run("account = {}; api = mockApi");
  await app.run("load()");
  assert.deepEqual(calls, ["/api/overview"]);
  const html = app.elements.get("#main").innerHTML;
  assert.match(html, /From source to answer/);
  assert.match(html, /Indexed files · last jobs/);
  assert.match(html, />30</);
});

test("operation history is concise, accurate, and retains actionable failures", () => {
  const app = harness();
  app.context.historyState = {
    worker: { running: true },
    operations: [
      { type: "source_add", account: "shareiarag", share: "test4share", status: "awaiting_access_verification",
        message: "Source connected and indexing succeeded. Validate authorized and unauthorized users." },
      { type: "source_add", account: "shareiarag", share: "active", status: "running", phase: "wait" },
      { type: "group_change", share: "shareiarag/test4share", id: "failed-id", phase: "snapshot",
        status: "failed", error: "Azure rejected the request" },
    ],
  };
  const html = app.run("renderOperations(historyState)");
  assert.match(html, /<th>Scope<\/th><th>Type<\/th><th>Subject<\/th><th>Start<\/th><th>End<\/th><th>Duration<\/th><th>Status<\/th>/);
  assert.match(html, /Share<\/span> <span class="mono">shareiarag \/ test4share</);
  assert.match(html, />Connect share<\/td><td class="mono">shareiarag \/ test4share</);
  assert.match(html, />Published<\/span>/);
  assert.match(html, />Indexing<\/p>/);
  assert.match(html, /Azure rejected the request/);
  assert.match(html, /data-resume="failed-id"/);
  assert.match(html, /data-cancel="failed-id"/);
  assert.match(html, /Access verification required/);
  assert.doesNotMatch(html, /phase-track|>Verified<\/span>/);
  assert.match(html, /data-clear-logs/);
  app.context.timedState = { operations: [{ type: "indexer", action: "run", target: "rc-indexer-x", status: "completed",
    createdAt: "2026-11-12T14:00:00Z", updatedAt: "2026-11-12T14:00:42Z" }] };
  const timed = app.run("renderOperations(timedState)");
  assert.match(timed, />Refresh<\/td><td class="mono">rc-indexer-x</);
  assert.match(timed, /<td>42 s<\/td>/);
  app.context.purgeState = { operations: [{ type: "rebuild", trigger: "schedule", status: "completed" }] };
  assert.match(app.run("renderOperations(purgeState)"), /Global<\/span><\/td><td class="cell-title">Purge deleted files/);
  app.context.clearedState = { logsClearedAt: "2026-11-12T15:00:00Z", operations: [
    { type: "rebuild", id: "old-purge", status: "completed", createdAt: "2026-11-12T14:00:00Z" },
    { type: "source_add", id: "old-run", status: "running", createdAt: "2026-11-12T14:30:00Z" },
    { type: "rebuild", id: "new-purge", status: "completed", createdAt: "2026-11-12T16:00:00Z" }] };
  const cleared = app.run("renderOperations(clearedState)");
  assert.match(cleared, /<span class="page-count">1<\/span>/);
});

test("navigation between pages is only offered by the sidebar", () => {
  const app = harness();
  app.context.overviewData = { sections: { sources: { data: sources }, index: { data: {} } } };
  app.context.configurationData = {};
  for (const expression of [
    "renderOverview(overviewData)",
    "renderSources(cachedSources, cachedState)",
    "renderSync(cachedSources, cachedState)",
    "renderGroups(cachedSources, cachedGroups, cachedState)",
    "renderConfiguration(configurationData, cachedState)",
  ]) {
    assert.doesNotMatch(app.run(expression), /data-nav=|data-focus-source=/);
  }
});

test("insights cards summarize the whole period, not the last bucket", () => {
  const app = harness();
  const points = [{ value: 3 }, { value: 1 }, { value: 2 }];
  assert.deepEqual(Array.from(app.run(`metricSummary({ aggregation: "Count" }, ${JSON.stringify(points)})`)), [6, "Total over the period"]);
  assert.deepEqual(Array.from(app.run(`metricSummary({ aggregation: "P95" }, ${JSON.stringify(points)})`)), [3, "Peak over the period"]);
  assert.deepEqual(Array.from(app.run(`metricSummary({ aggregation: "Average" }, ${JSON.stringify(points)})`)), [2, "Average over the period"]);
});

test("the web shell and formatting use English", () => {
  const shell = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
  assert.match(shell, /<html lang="en">/);
  assert.match(shell, /aria-label="Main navigation"/);
  const app = harness();
  assert.equal(app.run("number(1234.5)"), "1,234.5");
  assert.equal(app.run("date(null)"), "Never");
});

test("sync and access pages no longer embed global maintenance history", () => {
  const app = harness({ sourceKey: "secondfiles/documents" });
  for (const expression of ["renderSync(cachedSources, cachedState)", "renderGroups(cachedSources, cachedGroups, cachedState)"]) {
    const html = app.run(expression);
    assert.doesNotMatch(html, /operation-1|id="rebuild-form"/);
    assert.doesNotMatch(html, /data-nav=/);
  }
  assert.match(app.run("renderGroups(cachedSources, cachedGroups, cachedState)"), /value="secondfiles\/documents"/);
});

test("maintenance remains global, excludes rebuilds, and includes audit", () => {
  const app = harness({ sourceKey: "secondfiles/documents" });
  app.run('page = "maintenance"; renderCurrent()');
  const html = app.elements.get("#main").innerHTML;
  assert.match(html, /operation-1/);
  assert.doesNotMatch(html, /id="rebuild-form"/);
  assert.match(html, /Administrative history/);
  assert.match(html, /ALL shares/);
  assert.match(html, /Share filters do not limit/);
});

test("unmatched filters render an explicit empty state", () => {
  const app = harness({ sourceKey: "missing/source" });
  assert.match(app.run("renderSources(cachedSources)"), /No shares match/);
  assert.match(app.run("renderGroups(cachedSources, cachedGroups, cachedState)"), /No shares match/);
});

test("resource labels and selectors escape untrusted names", () => {
  const app = harness();
  app.run('cachedSources.indexers[0].resourceGroup = "<img onerror=alert(1)>";');
  const html = app.run("renderSources(cachedSources)");
  assert.doesNotMatch(html, /<img onerror/);
  assert.match(html, /&lt;img onerror/);
});

test("changing a filter re-renders cached data without a request", async () => {
  const app = harness();
  app.run('page = "sources";');
  await app.handlers.change({ target: { dataset: { scope: "account" }, value: "secondfiles" } });
  assert.match(app.elements.get("#main").innerHTML, /2 \/ 3 shares/);
  assert.doesNotMatch(app.elements.get("#main").innerHTML, /group-a/);
});

test("pending confirmation shows progress and a missing connection error remains inside the dialog", async () => {
  const app = harness();
  let reject;
  let calls = 0;
  app.context.pendingAction = () => {
    calls += 1;
    return new Promise((_resolve, fail) => { reject = fail; });
  };
  app.run('confirmation("Confirm", "Connect a share", pendingAction)');
  const modal = app.elements.get("#modal");
  const button = app.elements.get("#confirm-action");
  button.textContent = "Confirm";
  const closeButton = app.context.document.querySelector("[data-close]");
  closeButton.textContent = "Cancel";
  assert.match(modal.innerHTML, /data-dialog-feedback/);
  const first = button.onclick();
  const feedback = app.elements.get("[data-dialog-feedback]");
  await button.onclick();
  assert.equal(calls, 1);
  assert.equal(button.disabled, true);
  assert.equal(closeButton.textContent, "Close");
  assert.match(feedback.innerHTML, /Checking and saving/);
  assert.equal(feedback.attributes.role, "status");

  reject(Object.assign(new Error("server error"), {
    code: "missing_storage_connection", setting: "FILES_CONNECTION_SHAREIARAG",
  }));
  await first;
  assert.equal(modal.open, true);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Confirm");
  assert.equal(closeButton.textContent, "Cancel");
  assert.equal(feedback.attributes.role, "alert");
  assert.match(feedback.innerHTML, /Connection not started/);
  assert.match(feedback.innerHTML, /FILES_CONNECTION_SHAREIARAG/);
  assert.match(feedback.innerHTML, /files-connection-shareiarag/);
  assert.match(feedback.innerHTML, /No key is required here/);
});

test("unresolved Key Vault reference explains the managed identity role", async () => {
  const app = harness();
  const message = app.run(`actionErrorMessage(Object.assign(new Error("unresolved"), {
    code: "storage_connection_unresolved", setting: "FILES_CONNECTION_SHAREIARAG",
  }))`);
  assert.match(message, /Key Vault Secrets User/);
  assert.match(message, /files-connection-shareiarag/);
});

test("other prerequisite and unknown failures are visible and safely escaped", async () => {
  const app = harness();
  app.context.failAction = async () => {
    throw Object.assign(new Error("not ready"), { code: "downloader_not_ready" });
  };
  app.run('confirmation("Confirm", "Connect", failAction)');
  const button = app.elements.get("#confirm-action");
  button.textContent = "Confirm";
  await button.onclick();
  const feedback = app.elements.get("[data-dialog-feedback]");
  assert.match(feedback.innerHTML, /file access service is not ready/);
  assert.match(feedback.innerHTML, /Connection not started/);

  app.context.failAction = async () => { throw new Error("<unsafe>"); };
  app.run('confirmation("Confirm", "Connect", failAction)');
  await button.onclick();
  assert.match(feedback.innerHTML, /&lt;unsafe&gt;/);
  assert.doesNotMatch(feedback.innerHTML, /<unsafe>/);
  assert.match(feedback.innerHTML, /Check its status/);
});

test("dialog inputs remain locked during a request and regain their prior state", async () => {
  const app = harness();
  let reject;
  app.context.pendingAction = () => new Promise((_resolve, fail) => { reject = fail; });
  app.run('confirmation("Confirm", "Change", pendingAction)');
  const modal = app.elements.get("#modal");
  const input = { disabled: false };
  const alreadyDisabled = { disabled: true };
  modal.querySelectorAll = () => [input, alreadyDisabled];
  const button = app.elements.get("#confirm-action");
  button.textContent = "Confirm";
  const pending = button.onclick();
  assert.equal(input.disabled, true);
  assert.equal(alreadyDisabled.disabled, true);
  reject(new Error("not available"));
  await pending;
  assert.equal(input.disabled, false);
  assert.equal(alreadyDisabled.disabled, true);
});

test("accepted confirmation closes the dialog without claiming the operation is complete", async () => {
  const app = harness();
  let navigated = false;
  app.context.successAction = async () => {};
  app.context.onSuccess = () => { navigated = true; };
  app.run('load = async () => {}; confirmation("Confirm", "Connect", successAction, { onSuccess, successMessage: "Request accepted. The result is not yet confirmed." })');
  const button = app.elements.get("#confirm-action");
  button.textContent = "Confirm";
  await button.onclick();
  assert.equal(app.elements.get("#modal").open, false);
  assert.equal(navigated, true);
  assert.match(app.elements.get("#toast").textContent, /result is not yet confirmed/);
});

test("a request finishing after its dialog was closed does not close a new dialog", async () => {
  const app = harness();
  let resolve;
  let navigated = false;
  app.context.pendingAction = () => new Promise(done => { resolve = done; });
  app.context.onSuccess = () => { navigated = true; };
  app.run('confirmation("Confirm", "Connect", pendingAction, { onSuccess })');
  const modal = app.elements.get("#modal");
  const button = app.elements.get("#confirm-action");
  button.textContent = "Confirm";
  const pending = button.onclick();
  modal.close();
  button.detached = true;
  modal.showModal();
  resolve();
  await pending;
  assert.equal(modal.open, true);
  assert.equal(navigated, false);
  assert.equal(app.elements.has("#toast"), false);
  modal.close();
  assert.match(app.elements.get("#toast").textContent, /Request submitted/);
});

test("API errors retain structured prerequisite details for the dialog", async () => {
  const app = harness();
  app.context.fetch = async () => ({
    ok: false, status: 400,
    headers: { get: () => "application/json" },
    json: async () => ({
      error: "Connection missing", code: "missing_storage_connection", setting: "FILES_CONNECTION_SHAREIARAG",
    }),
  });
  app.run('account = {}; msal = { acquireTokenSilent: async () => ({ accessToken: "test-token" }) }');
  await assert.rejects(app.run('api("/api/sources", {})'), error => {
    assert.equal(error.code, "missing_storage_connection");
    assert.equal(error.setting, "FILES_CONNECTION_SHAREIARAG");
    return true;
  });
});

test("both group selection surfaces search and select names without entering a GUID", async () => {
  const app = harness();
  const directory = {
    directoryStatus: "ok", moreAvailable: true, nextCursor: "next",
    groups: [{ id: "group-a", displayName: "Équipe Alpha" }],
  };
  app.context.directory = directory;
  app.run("cachedGroups = directory");
  const groupsPage = app.run("renderGroups(cachedSources, cachedGroups, cachedState)");
  assert.match(groupsPage, /id="group-id-search" data-group-query/);
  assert.match(groupsPage, /<select id="group-id"[^>]*required/);
  assert.match(groupsPage, /value="group-a"[^>]*>Équipe Alpha<\/option>/);
  assert.match(groupsPage, /Load more groups/);
  assert.doesNotMatch(groupsPage, /Identifiant du groupe \(GUID\)/);

  app.context.mockApi = async path => path === "/api/resources/accounts"
    ? { subscriptionId: "subscription", accounts: [] } : directory;
  app.run("api = mockApi");
  await app.run("sourceDialog()");
  const dialog = app.elements.get("#modal").innerHTML;
  assert.match(dialog, /id="source-group-search" data-group-query/);
  assert.match(dialog, /<select id="source-group"[^>]*required/);
  assert.match(dialog, /value="group-a"[^>]*>Équipe Alpha<\/option>/);
});

test("group names are escaped and duplicate names expose their full identifiers", () => {
  const app = harness();
  app.context.directory = { directoryStatus: "ok", groups: [
    { id: "id-1", displayName: "<Admin>" }, { id: "id-2", displayName: "<Admin>" },
  ] };
  const html = app.run('renderGroupPicker("group-id", directory)');
  assert.doesNotMatch(html, /<Admin>/);
  assert.match(html, /&lt;Admin&gt; · id-1/);
  assert.match(html, /&lt;Admin&gt; · id-2/);
});

test("a disabled directory still offers only the configured IDs", () => {
  const app = harness();
  app.context.directory = {
    directoryStatus: "not_configured", groups: [{ id: "configured-id", displayName: "configured-id" }],
  };
  const html = app.run('renderGroupPicker("group-id", directory)');
  assert.match(html, /data-group-query[^>]*disabled/);
  assert.match(html, /value="configured-id"/);
  assert.match(html, /Only groups already associated with sources are available/);
});

function fakeGroupPicker(value = "") {
  const classes = new Set();
  const input = {
    value,
    hasAttribute(name) { return name === "data-group-query"; },
    closest() { return picker; },
  };
  const select = {
    innerHTML: "initial",
    options: [{ value: "" }, { value: "old-id", dataset: { groupName: "Old" }, textContent: "Old" }],
    value: "old-id",
    insertAdjacentHTML(_position, html) { this.inserted = html; },
  };
  const status = {
    textContent: "", attributes: {},
    classList: { add(name) { classes.add(name); }, remove(name) { classes.delete(name); } },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
  const more = { dataset: { cursor: "" }, hidden: true, disabled: false };
  const retry = { dataset: {}, hidden: true };
  const elements = {
    "[data-group-query]": input, "[data-group-select]": select, "[data-group-status]": status,
    "[data-group-more]": more, "[data-group-retry]": retry,
  };
  const picker = { dataset: { requestId: "0" }, isConnected: true, querySelector: key => elements[key] };
  return { picker, input, select, status, more, retry, classes };
}

test("changing the search clears the previous selection immediately", () => {
  const app = harness();
  const { picker, input, select, more, status } = fakeGroupPicker("Team");
  more.dataset.cursor = "old-cursor";
  more.hidden = false;
  app.handlers.input({ target: input });
  assert.equal(picker.dataset.requestId, "1");
  assert.match(select.innerHTML, /Searching/);
  assert.equal(more.dataset.cursor, "");
  assert.equal(more.hidden, true);
  assert.match(status.textContent, /Searching groups/);
});

test("search requests ignore stale responses and show the latest matching names", async () => {
  const app = harness();
  const { picker, input, select, status } = fakeGroupPicker("Al");
  const pending = [];
  app.context.picker = picker;
  app.context.mockApi = path => new Promise(resolve => pending.push({ path, resolve }));
  app.run("api = mockApi");
  const first = app.run("requestGroupPage(picker)");
  input.value = "Alpha";
  picker.dataset.requestId = "1";
  const second = app.run("requestGroupPage(picker)");
  assert.match(pending[1].path, /search=Alpha/);
  pending[1].resolve({ directoryStatus: "ok", moreAvailable: false, nextCursor: null,
    groups: [{ id: "new-id", displayName: "Alpha récent" }] });
  await second;
  pending[0].resolve({ directoryStatus: "ok", moreAvailable: false, nextCursor: null,
    groups: [{ id: "old-id", displayName: "Ancien résultat" }] });
  await first;
  assert.match(select.innerHTML, /Alpha récent/);
  assert.doesNotMatch(select.innerHTML, /Ancien résultat/);
  assert.match(status.textContent, /Select an existing security group/);
});

test("paging preserves the selection and sends the opaque cursor", async () => {
  const app = harness();
  const { picker, select, more } = fakeGroupPicker("Team");
  more.dataset.cursor = "next +/=";
  more.hidden = false;
  let path;
  app.context.picker = picker;
  app.context.mockApi = async value => {
    path = value;
    return { directoryStatus: "ok", moreAvailable: false, nextCursor: null, groups: [
      { id: "old-id", displayName: "Old" }, { id: "new-id", displayName: "Team Two" },
    ] };
  };
  app.run("api = mockApi");
  await app.run("requestGroupPage(picker, true)");
  const query = new URLSearchParams(path.split("?")[1]);
  assert.equal(query.get("search"), "Team");
  assert.equal(query.get("cursor"), "next +/=");
  assert.equal(select.value, "old-id");
  assert.match(select.inserted, /Team Two/);
  assert.doesNotMatch(select.inserted, /value="old-id"/);
  assert.equal(more.hidden, true);
});

test("group lookup failures are visible and can be retried", async () => {
  const app = harness();
  const { picker, status, retry, more, classes } = fakeGroupPicker("Team");
  more.dataset.cursor = "next";
  app.context.picker = picker;
  app.context.mockApi = async () => { throw new Error("Graph indisponible"); };
  app.run("api = mockApi");
  await app.run("requestGroupPage(picker, true)");
  assert.match(status.textContent, /Graph indisponible/);
  assert.equal(status.attributes.role, "alert");
  assert.equal(classes.has("error-text"), true);
  assert.equal(retry.hidden, false);
  assert.equal(retry.dataset.append, "true");
  assert.equal(more.hidden, true);
});

test("share confirmation sends the selected group ID and shows its name", async () => {
  const app = harness();
  app.context.crypto = { randomUUID: () => "operation-id" };
  const values = {
    "#source-rg": "rg", "#source-account": "account", "#source-share": "share",
    "#source-interval": "PT5M",
  };
  for (const [selector, value] of Object.entries(values)) app.context.document.querySelector(selector).value = value;
  const select = app.context.document.querySelector("#source-group");
  select.value = "selected-id";
  select.selectedOptions = [{ dataset: { groupName: "Équipe choisie" } }];
  let sent;
  app.context.mockApi = async (path, body) => { sent = { path, body }; throw new Error("test rejection"); };
  app.run("api = mockApi");
  await app.handlers.submit({ target: { id: "source-form" }, preventDefault() {} });
  assert.match(app.elements.get("#modal").innerHTML, /Équipe choisie \(selected-id\)/);
  await app.elements.get("#confirm-action").onclick();
  assert.equal(sent.path, "/api/sources");
  assert.equal(sent.body.groupId, "selected-id");
});

test("group-change preview uses the selected ID and displays its name", async () => {
  const app = harness();
  app.run('cachedGroups = { groups: [{ id: "group-a", displayName: "Ancien groupe" }] }');
  app.context.document.querySelector("#group-share").value = "firstfiles/documents";
  const select = app.context.document.querySelector("#group-id");
  select.value = "selected-id";
  select.selectedOptions = [{ dataset: { groupName: "Nouveau groupe" } }];
  let sent;
  app.context.mockApi = async (path, body) => {
    sent = { path, body };
    return { id: "draft", share: body.share, oldGroup: "group-a", newGroup: body.groupId };
  };
  app.run("api = mockApi");
  const submitButton = { disabled: false };
  await app.handlers.submit({
    target: { id: "group-form", querySelector: () => submitButton },
    preventDefault() {},
  });
  assert.equal(sent.path, "/api/group-changes/preview");
  assert.equal(sent.body.groupId, "selected-id");
  assert.match(app.elements.get("#modal").innerHTML, /Ancien groupe/);
  assert.match(app.elements.get("#modal").innerHTML, /Nouveau groupe/);
});

test("accepted share connections stay on the sources page", async () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.context.crypto = { randomUUID: () => "operation-id" };
  const values = {
    "#source-rg": "rg", "#source-account": "account", "#source-share": "share",
    "#source-interval": "PT5M",
  };
  for (const [selector, value] of Object.entries(values)) app.context.document.querySelector(selector).value = value;
  const select = app.context.document.querySelector("#source-group");
  select.value = "group-id";
  select.selectedOptions = [{ dataset: { groupName: "Team" } }];
  app.context.mockApi = async () => ({ status: "queued" });
  app.run('page = "sources"; api = mockApi; load = async () => {}');
  await app.handlers.submit({ target: { id: "source-form" }, preventDefault() {} });
  app.elements.get("#confirm-action").textContent = "Confirm";
  await app.elements.get("#confirm-action").onclick();
  assert.equal(app.run("page"), "sources");
  assert.equal(app.run("scope.sourceKey"), "");
  assert.match(app.elements.get("#toast").textContent, /Connection request accepted/);
});

test("access maintenance uses an English confirmation and stays on the groups page", async () => {
  const app = harness();
  app.context.document.querySelector("#group-share").value = "firstfiles/documents";
  const select = app.context.document.querySelector("#group-id");
  select.value = "new-group";
  select.selectedOptions = [{ dataset: { groupName: "New team" } }];
  const calls = [];
  app.context.mockApi = async (path, body) => {
    calls.push({ path, body });
    if (path === "/api/group-changes/preview") {
      return { id: "draft", share: body.share, oldGroup: "group-a", newGroup: body.groupId };
    }
    return { status: "queued" };
  };
  app.run('page = "groups"; api = mockApi; load = async () => {}');
  await app.handlers.submit({
    target: { id: "group-form", querySelector: () => ({ disabled: false }) },
    preventDefault() {},
  });
  assert.match(app.elements.get("#modal").innerHTML, /Type REBUILD to confirm/);
  const input = app.elements.get("#maintenance-confirm");
  const button = app.elements.get("#apply-group");
  input.value = "REBUILD";
  input.oninput();
  assert.equal(button.disabled, false);
  button.textContent = "Apply and rebuild";
  await button.onclick();
  assert.equal(calls[1].path, "/api/group-changes/apply");
  assert.equal(calls[1].body.confirmation, "REBUILD");
  assert.equal(app.run("page"), "groups");
  assert.match(app.elements.get("#toast").textContent, /Maintenance request accepted/);
});

test("job counters come from the last sync and purge, scoped by filters, never from chunks", () => {
  const app = harness();
  assert.equal(app.run("indexedTotal(scopeRows(cachedSources.indexers, scope))"), 30);
  app.run('scope = { resourceGroup: "second-rg", account: "", sourceKey: "" }');
  assert.equal(app.run("indexedTotal(scopeRows(cachedSources.indexers, scope))"), 20);
  app.context.overviewData = { sections: { sources: { status: "ok", data: sources }, index: { data: { documentCount: 999 } } } };
  const html = app.run('scope = {}; renderOverview(overviewData)');
  assert.match(html, /Indexed files · last jobs/);
  assert.match(html, /<div class="stat-value">30<\/div>/);
  assert.match(html, /Indexed chunks · global/);
  assert.match(html, /999/);
  const sync = app.run("renderSync(cachedSources, cachedState)");
  assert.match(sync, /<span class="muted">—<\/span><div class="cell-sub">New or modified · list recorded about 1 min after the sync<\/div>/);
  assert.match(sync, /Counters come from the last jobs only/);
  assert.match(app.run("renderSources(cachedSources, cachedState)"), /<dt>Indexed files \(last job\)<\/dt><dd><button class="file-counter" data-job-files="indexed" data-source-key="firstfiles\/documents">10<\/button>/);
});

test("indexed follows the latest sync and counters open their file lists", () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.run('Object.assign(cachedSources.indexers[0], { indexed: { count: 11, at: "2026-11-11T12:30:00Z", from: "sync", listed: true }, lastSyncListed: true, lastSyncCount: 1, itemsFailed: 1, errors: ["<bad.pdf> failed"] })');
  const html = app.run("renderSync(cachedSources, cachedState)");
  assert.match(html, /data-job-files="lastSync" data-source-key="firstfiles\/documents">1<\/button>/);
  assert.match(html, /\d{2}:\d{2}/);
  assert.doesNotMatch(html, /\b(AM|PM)\b/);
  assert.match(html, /data-job-files="indexed" data-source-key="firstfiles\/documents">11<\/button><div class="cell-sub">Sync /);
  assert.match(html, /data-failed-files="firstfiles-documents-a">1<\/button>/);
  app.run('showFailures("firstfiles-documents-a")');
  assert.match(app.run("modal.innerHTML"), /&lt;bad.pdf&gt; failed/);
  app.run('cachedSources.indexers[0].lastSyncListed = false; cachedSources.indexers[0].indexed.listed = false');
  const plain = app.run("renderSync(cachedSources, cachedState)");
  assert.doesNotMatch(plain, /data-job-files="(lastSync|indexed)"/);
  assert.match(plain, /list recorded about 1 min after the sync/);
  app.run('Object.assign(cachedSources.indexers[0], { itemsProcessed: 0, lastSyncListed: true, lastSyncCount: 0, itemsFailed: 0, errors: [], lastPurge: { at: "2026-11-11T11:00:00Z", indexedCount: 10, purgedCount: 0 } })');
  const zeros = app.run("renderSync(cachedSources, cachedState)");
  assert.match(zeros, /data-job-files="lastSync" data-source-key="firstfiles\/documents">0<\/button>/);
  assert.match(zeros, /data-job-files="purged" data-source-key="firstfiles\/documents">0<\/button>/);
  assert.match(zeros, /data-failed-files="firstfiles-documents-a">0<\/button>/);
  app.run('showFailures("firstfiles-documents-a")');
  assert.match(app.run("modal.innerHTML"), /No failed files in the latest refresh/);
});

test("sync highlights index roles and counts within the selected share", () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.run(`
    cachedSources.indexers[0].schedule = null;
    cachedSources.indexers.push(
      { ...cachedSources.indexers[0], active: false, index: "<standby&index>", name: "firstfiles-documents-b" },
      { ...cachedSources.indexers[1], active: false, index: "hidden-standby", name: "secondfiles-documents-b" }
    );
  `);
  const html = app.run("renderSync(cachedSources, cachedState)");
  const summary = html.slice(html.indexOf('aria-label="Active and inactive indexes"'), html.indexOf("<h2>Active indexers</h2>"));
  assert.match(summary, /Used by the agent/);
  assert.match(summary, /Not queried/);
  assert.match(summary, /af-index-all/);
  assert.match(summary, /&lt;standby&amp;index&gt;/);
  assert.match(summary, /1 active indexer in view/);
  assert.match(summary, /1 inactive indexer in view/);
  assert.doesNotMatch(html, /hidden-standby/);
  assert.match(html, /Refresh paused/);

  app.run('scope = { resourceGroup: "", account: "", sourceKey: "" }');
  const global = app.run("renderIndexSummary(cachedSources)");
  assert.match(global, /3 active indexers in view/);
  assert.match(global, /2 inactive indexers in view/);
  assert.match(global, /hidden-standby/);
});

test("sync explicitly identifies absent standby indexers and empty filters", () => {
  const app = harness();
  const html = app.run("renderSync(cachedSources, cachedState)");
  assert.match(html, /None in view/);
  assert.match(html, /0 inactive indexers in view/);
  assert.doesNotMatch(html, /<h2>Inactive indexers<\/h2>/);
  app.run('scope.sourceKey = "missing/share"');
  const filtered = app.run("renderSync(cachedSources, cachedState)");
  assert.match(filtered, /0 active indexers in view/);
  assert.match(filtered, /No shares match the selected filters/);
});

test("sync follows an index switch and keeps standby rows visible without active rows", () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.run(`
    cachedSources.indexers[0].active = false;
    cachedSources.activeIndex = "af-index-all-b";
  `);
  const html = app.run("renderSync(cachedSources, cachedState)");
  assert.match(html, /<div class="index-name mono">af-index-all-b<\/div>/);
  assert.match(html, /0 active indexers in view/);
  assert.match(html, /1 inactive indexer in view/);
  assert.match(html, /<h2>Inactive indexers<\/h2>/);
  assert.doesNotMatch(html, /data-run=/);
});

test("inactive indexers are listed below the active ones with read-only counters", () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.run(`cachedSources.indexers.push({ ...cachedSources.indexers[0], active: false, index: "af-index-all-b", name: "firstfiles-documents-b",
    schedule: null, itemsProcessed: 4, lastSyncListed: false, lastSyncCount: 4,
    indexed: { count: 6, at: "2026-11-10T09:00:00Z", from: "purge", listed: true } })`);
  const html = app.run("renderSync(cachedSources, cachedState)");
  const standby = html.slice(html.indexOf("<h2>Inactive indexers</h2>"));
  assert.ok(html.indexOf("<h2>Active indexers</h2>") < html.indexOf("<h2>Inactive indexers</h2>"));
  assert.match(standby, /af-index-all-b/);
  assert.match(standby, /<th>LAST SYNC<\/th><th>INDEXED<\/th><th>PURGED<\/th><th>FAILED<\/th>/);
  assert.match(standby, />4<div class="cell-sub">Azure count/);
  assert.match(standby, /data-job-files="indexed" data-source-key="firstfiles\/documents" data-slot="standby">6<\/button><div class="cell-sub">Built by purge /);
  assert.match(standby, /data-job-files="purged" data-source-key="firstfiles\/documents" data-slot="standby">2<\/button>/);
  assert.match(standby, /Standby · not queried/);
  assert.doesNotMatch(standby, /data-run=|data-remove-source=|data-schedule=/);
  assert.doesNotMatch(app.run("renderSourceTable(cachedSources, true)"), /Inactive indexers/);
});

test("missing job counters show unknown, not zero, and explain the purge baseline", () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.run('cachedSources.indexers[0].lastPurge = { at: "2026-11-11T11:00:00Z", indexedCount: 4, purgedCount: null }; cachedSources.indexers[0].itemsProcessed = undefined');
  const html = app.run("renderSync(cachedSources, cachedState)");
  assert.match(html, /Counted from next purge/);
  assert.match(html, /<span class="muted">—<\/span><div class="cell-sub">New or modified/);
  app.run("cachedSources.indexers[1].indexed = { count: null, at: null, from: null, listed: false }");
  assert.equal(app.run("indexedTotal(cachedSources.indexers)"), null);
});

test("source group labels use verified escaped names and never fallback to GUIDs", () => {
  const app = harness({ sourceKey: "firstfiles/documents" });
  app.run('cachedSources.indexers[0].groupId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"; cachedSources.indexers[0].groupName = "<Verified & Team>"');
  let html = app.run("renderSources(cachedSources, cachedState)");
  assert.match(html, /&lt;Verified &amp; Team&gt;/);
  assert.doesNotMatch(html, /<Verified|aaaaaaaa-bbbb/);
  app.run('cachedSources.indexers[0].groupNameError = "<Directory denied>"');
  html = app.run("renderSources(cachedSources, cachedState)");
  assert.match(html, /Group name unavailable: &lt;Directory denied&gt;/);
  assert.doesNotMatch(html, /aaaaaaaa-bbbb|Verified &amp;/);
  app.run("cachedSources.indexers[0].groupName = null; cachedSources.indexers[0].groupNameError = null");
  assert.match(app.run("renderSources(cachedSources, cachedState)"), /Group name unavailable/);
});

test("closing or replacing a file modal prevents stale file responses from overwriting confirmations", async () => {
  const app = harness();
  let resolve;
  app.context.mockApi = () => new Promise(done => { resolve = done; });
  app.run('api = mockApi; fileDialog = { kind: "files", scope: {} }');
  const pending = app.run("showFilePage()");
  app.run('confirmation("Run refresh?", "Confirm before running", async () => {})');
  resolve({ kind: "files", total: 1, offset: 0, limit: 100, files: [{ path: "stale" }] });
  await pending;
  assert.match(app.elements.get("#modal").innerHTML, /Confirm before running/);
  assert.doesNotMatch(app.elements.get("#modal").innerHTML, /stale/);
});

function clickButton(app, dataset) {
  const button = {
    dataset, disabled: false,
    hasAttribute(name) {
      const field = name.replace(/^data-/, "").replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      return Object.hasOwn(dataset, field);
    },
  };
  app.handlers.click({ target: { closest: () => button } });
}

test("pause and resume schedules require confirmed actions and retain supported frequency", async () => {
  const app = harness();
  app.context.crypto = { randomUUID: () => "operation-id" };
  const calls = [];
  app.context.mockApi = async (path, body) => { calls.push({ path, body }); return {}; };
  app.run('api = mockApi; load = async () => {}; cachedSources.indexers[0].schedule = "PT15M"');
  assert.match(app.run("renderSync(cachedSources, cachedState)"), /data-refresh-pause="firstfiles-documents-a"/);
  clickButton(app, { refreshPause: "firstfiles-documents-a" });
  assert.equal(calls.length, 0);
  assert.match(app.elements.get("#modal").innerHTML, /Confirmed manual refresh is still possible/);
  assert.match(app.elements.get("#modal").innerHTML, /Global purge still includes/);
  await app.elements.get("#confirm-action").onclick();
  assert.equal(calls[0].body.action, "pause");
  assert.equal(calls[0].body.confirmed, true);
  app.run("cachedSources.indexers[0].schedule = null");
  assert.match(app.run("renderSync(cachedSources, cachedState)"), /data-refresh-resume="firstfiles-documents-a"/);
  clickButton(app, { refreshResume: "firstfiles-documents-a" });
  assert.equal(calls.length, 1);
  assert.match(app.elements.get("#modal").innerHTML, /Every 15 min/);
  await app.elements.get("#confirm-action").onclick();
  assert.equal(calls[1].body.action, "schedule");
  assert.equal(calls[1].body.interval, "PT15M");
  assert.equal(calls[1].body.confirmed, true);
});

test("paused manual refresh requires a clear confirmation; resume defaults to five minutes", async () => {
  const app = harness();
  app.context.crypto = { randomUUID: () => "operation-id" };
  const calls = [];
  app.context.mockApi = async (_path, body) => { calls.push(body); return {}; };
  app.run("api = mockApi; load = async () => {}; cachedSources.indexers[0].schedule = null");
  clickButton(app, { run: "firstfiles-documents-a" });
  assert.equal(calls.length, 0);
  assert.match(app.elements.get("#modal").innerHTML, /Manually refresh a paused share/);
  assert.match(app.elements.get("#modal").innerHTML, /without resuming its schedule/);
  await app.elements.get("#confirm-action").onclick();
  assert.equal(calls[0].action, "run");
  assert.equal(calls[0].confirmed, true);
  clickButton(app, { refreshResume: "firstfiles-documents-a" });
  await app.elements.get("#confirm-action").onclick();
  assert.equal(calls[1].interval, "PT5M");
});

test("separate navigation restricts purge history to rebuilds and maintenance retains recovery and audit", () => {
  const app = harness({ sourceKey: "secondfiles/documents" });
  app.run(`cachedState.operations.push({ type: "rebuild", id: "purge-1", status: "failed", switched: true, purgedFileCount: 15 });
    cachedState.audit = [{ time: "2026-11-11", action: "<pause>", actor: "admin", detail: "firstfiles/documents" }];
    page = "purges"; renderCurrent()`);
  let html = app.elements.get("#main").innerHTML;
  assert.match(html, /id="rebuild-form"/);
  assert.match(html, /data-purge-files="purge-1">Purged files: 15/);
  assert.match(html, /data-resume="purge-1"/);
  assert.doesNotMatch(html, /operation-1|Administrative history/);
  assert.match(html, /including paused shares/);
  app.run('page = "maintenance"; renderCurrent()');
  html = app.elements.get("#main").innerHTML;
  assert.match(html, /data-resume="operation-1"/);
  assert.match(html, /Administrative history/);
  assert.match(html, /&lt;pause&gt;/);
  assert.doesNotMatch(html, /id="rebuild-form"/);
  assert.equal(app.run('pages.find(item => item[0] === "purges")[1]'), "Purges");
  assert.equal(app.run('pages.find(item => item[0] === "maintenance")[1]'), "Logs");
});

test("purged file historical, pre-switch, and capture failures are explicit, not zero", () => {
  const app = harness();
  assert.match(app.run('purgeFiles({ switched: true })'), /Unavailable/);
  assert.match(app.run('purgeFiles({ status: "completed", switched: true })'), /baseline recorded; counted from the next purge/);
  assert.match(app.run('purgeFiles({ purgedFileCount: 10, switched: false })'), /Unavailable/);
  assert.doesNotMatch(app.run('purgeFiles({ purgedFileCount: 10, switched: false })'), /data-purge-files/);
  assert.match(app.run('purgeFiles({ inventoryError: "<Capture denied>", purgedFileCount: 0, switched: true })'), /&lt;Capture denied&gt;/);
  assert.match(app.run('purgeFiles({ id: "zero", purgedFileCount: 0, switched: true })'), /Purged files: 0/);
  assert.match(app.run('purgeFiles({ id: "targeted", purgedAt: "2026-11-12", purgedFileCount: 3 })'), /data-purge-files="targeted">Purged files: 3/);
  assert.match(app.run('purgeFiles({ id: "fallback", switched: true, purgedFileCount: 1, fallbackReason: "<HTTP 403>" })'), /&lt;HTTP 403&gt;/);
});

test("purged file pagination always requests the global operation endpoint", async () => {
  const app = harness({ sourceKey: "secondfiles/documents" });
  const paths = [];
  app.context.mockApi = async value => {
    paths.push(value);
    const offset = Number(new URLSearchParams(value.split("?")[1]).get("offset"));
    return { total: 110, offset, limit: 100, files: Array.from({ length: offset ? 10 : 100 }, (_, index) => ({ path: `removed-${offset + index}`, account: "firstfiles", share: "documents" })) };
  };
  app.run('api = mockApi; modal.open = true; fileDialog = { kind: "purge", operationId: "operation & one" }');
  await app.run("showFilePage()");
  assert.deepEqual(paths, ["/api/operations/operation%20%26%20one/files?offset=0&limit=100", "/api/operations/operation%20%26%20one/files?offset=100&limit=100"]);
  assert.match(app.elements.get("#modal").innerHTML, /1–100 of 110/);
  assert.match(app.elements.get("#modal").innerHTML, /including paused shares/);
  clickButton(app, { fileOffset: "100" });
  assert.match(app.elements.get("[data-file-results]").innerHTML, /101–110 of 110/);
});

test("file lists open as an explorer with a folder tree, folder view and search", async () => {
  const app = harness();
  app.context.mockApi = async () => ({ total: 4, offset: 0, limit: 100, at: "2026-11-12T16:08:00Z", files: [
    { sourceKey: "shareiarag/invoices", path: "facture_test_01.pdf" },
    { sourceKey: "shareiarag/invoices", path: "2026/Q4/<x>.docx" },
    { sourceKey: "shareiarag/invoices", path: "2026/Q4/b.xlsx" },
    { sourceKey: "shareiarag/invoices", path: "2026\\notes.txt" },
  ] });
  app.run('api = mockApi; modal.open = true; fileDialog = { kind: "indexed", sourceKey: "shareiarag/invoices" }');
  await app.run("showFilePage()");
  let html = app.elements.get("#modal").innerHTML;
  assert.match(html, /class="explorer-tree"/);
  assert.match(html, /<span class="tree-name">All files<\/span><span class="tree-count">4<\/span>/);
  assert.match(html, /<span class="tree-name">shareiarag\/invoices<\/span><span class="tree-count">4<\/span>/);
  assert.match(html, /<span class="tree-name">2026<\/span><span class="tree-count">3<\/span>/);
  assert.match(html, /<th>Location<\/th>/);
  assert.match(html, /&lt;x&gt;\.docx/);
  assert.doesNotMatch(html, /<th>Error<\/th>/);
  const folder = app.run('fileDialog.view.nodes.find(node => node.name === "2026").id');
  clickButton(app, { fileFolder: String(folder) });
  html = app.elements.get("#modal").innerHTML;
  assert.match(html, /data-file-folder="\d+"><td><span class="tree-icon">.*Q4<\/span>/);
  assert.match(html, /notes\.txt/);
  assert.doesNotMatch(html, /b\.xlsx|<th>Location<\/th>/);
  assert.match(html, /<span class="crumb current">2026<\/span>/);
  assert.match(html, /<span class="tree-name">Q4<\/span>/);
  app.handlers.input({ target: { value: "xlsx", hasAttribute: name => name === "data-file-search" } });
  const results = app.elements.get("[data-file-results]").innerHTML;
  assert.match(results, /b\.xlsx/);
  assert.doesNotMatch(results, /notes\.txt/);
  assert.match(results, /1 file matching/);
});

test("France Central is absent from the shell and frontend environment", () => {
  for (const file of ["../static/index.html", "./main.js"]) {
    assert.doesNotMatch(readFileSync(new URL(file, import.meta.url), "utf8"), /France Central|francecentral/i);
  }
});
