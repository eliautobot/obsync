const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  authMode: "login",
  session: null,
  view: "overview",
  overview: null,
  agents: [],
  roots: [],
  server: null,
  pipeline: { enabled: true, state: "running", active_jobs: 0 },
  documentsQuery: { search: "", comparison: "" },
  liveTimer: null,
  liveRefreshing: false,
  liveSignature: "",
  aiActivity: null,
  aiProfiles: null,
  aiSelectedProfileId: "",
  aiEventSource: null,
  aiStreamRevision: -1,
  aiFollow: new Map(),
  aiScrollPositions: new Map(),
  aiScrollTimers: new Map(),
};

function captureScrollState() {
  const panels = {};
  $$('[data-preserve-scroll="true"]').forEach((element, index) => {
    const key = element.dataset.scrollKey || String(index);
    panels[key] = { top: element.scrollTop, left: element.scrollLeft };
  });
  return { windowY: window.scrollY, panels };
}

function restoreScrollState(snapshot) {
  if (!snapshot) return;
  requestAnimationFrame(() => {
    window.scrollTo({ top: snapshot.windowY, left: 0, behavior: "auto" });
    $$('[data-preserve-scroll="true"]').forEach((element, index) => {
      const saved = snapshot.panels[element.dataset.scrollKey || String(index)];
      if (saved) {
        element.scrollTop = saved.top;
        element.scrollLeft = saved.left;
      }
    });
  });
}

function cookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

const viewMeta = {
  overview: ["Overview", "Your knowledge pipeline at a glance."],
  sources: ["Sources", "Every computer and folder feeding your vault."],
  vault: ["Obsidian Vault", "Choose exactly where Obsync is allowed to write notes."],
  "local-ai": ["Local AI", "Control local classification, duplicate protection, and organization."],
  documents: ["Documents", "Search and inspect synchronized knowledge."],
  review: ["Review", "Confirm items where automated classification was uncertain."],
  settings: ["Settings", "Obsync server and Desktop app connections."],
  help: ["Help", "Simple guidance for setting up and using Obsync."],
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function helpIcon(message, label = "More information") {
  return `<button class="help-tip" type="button" data-help="${escapeHtml(message)}" aria-label="${escapeHtml(label)}">?</button>`;
}

function labelWithHelp(forId, label, message) {
  return `<div class="field-label-row"><label${forId ? ` for="${escapeHtml(forId)}"` : ""}>${escapeHtml(label)}</label>${helpIcon(message, `About ${label}`)}</div>`;
}

function headingWithHelp(label, message) {
  return `<span class="heading-with-help">${escapeHtml(label)}${helpIcon(message, `About ${label}`)}</span>`;
}

function relativeTime(value) {
  if (!value) return "Never";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(seconds, "second");
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = cookie("obsync_csrf");
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  }
  if (options.body && typeof options.body !== "string" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401 && !options.authRequest) {
    showLogin();
    throw new Error("Your session expired. Sign in again.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = false;
  if (typeof element.showPopover === "function" && !element.matches(":popover-open")) {
    element.showPopover();
  }
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    if (typeof element.hidePopover === "function" && element.matches(":popover-open")) {
      element.hidePopover();
    }
    element.hidden = true;
  }, 4000);
}

function clearLegacyToken() {
  localStorage.removeItem("obsync_token");
  sessionStorage.removeItem("obsync_token");
}

function showLogin(message = "") {
  stopLiveUpdates();
  state.aiActivity = null;
  state.aiFollow.clear();
  state.aiScrollPositions.clear();
  state.authMode = "login";
  state.session = null;
  $("#app").hidden = true;
  $("#login-screen").hidden = false;
  $("#auth-fields").hidden = false;
  $("#setup-help").hidden = true;
  $("#auth-title").textContent = "Sign in to Obsync";
  $("#auth-description").textContent = "Use the administrator login for this Obsync server.";
  $("#legacy-token-field").hidden = true;
  $("#confirm-password-field").hidden = true;
  $("#password-input").autocomplete = "current-password";
  $("#remember-login").checked = false;
  $("#auth-submit").textContent = "Sign in";
  $("#login-error").textContent = message;
}

function showSetup(legacyMigrationRequired) {
  stopLiveUpdates();
  state.authMode = "setup";
  state.session = null;
  $("#app").hidden = true;
  $("#login-screen").hidden = false;
  $("#auth-fields").hidden = false;
  $("#setup-help").hidden = true;
  $("#auth-title").textContent = legacyMigrationRequired ? "Upgrade your login" : "Create your admin account";
  $("#auth-description").textContent = legacyMigrationRequired
    ? "Choose an easier username and password. Your current token is needed only this once."
    : "Choose the login you will use to manage this Obsync server.";
  $("#legacy-token-field").hidden = !legacyMigrationRequired;
  $("#confirm-password-field").hidden = false;
  $("#password-input").autocomplete = "new-password";
  $("#remember-login").checked = true;
  $("#auth-submit").textContent = "Create account";
  $("#login-error").textContent = "";
  $("#username-input").value ||= "admin";
  if (legacyMigrationRequired) {
    $("#legacy-token-input").value = localStorage.getItem("obsync_token")
      || sessionStorage.getItem("obsync_token") || "";
  }
}

function showSetupRequired() {
  stopLiveUpdates();
  state.authMode = "blocked";
  state.session = null;
  $("#app").hidden = true;
  $("#login-screen").hidden = false;
  $("#auth-title").textContent = "Finish setup on the Obsync server";
  $("#auth-description").textContent = "The passwordless Admin is limited to the computer running Obsync.";
  $("#auth-fields").hidden = true;
  $("#setup-help").hidden = false;
  $("#local-setup-url").textContent = `http://localhost:${location.port || "7769"}`;
  $("#login-error").textContent = "";
}

function updateSecurityState(session) {
  const temporary = Boolean(session?.temporary);
  $("#security-banner").hidden = !temporary;
  $("#account-button").textContent = (session?.username || "O").slice(0, 1).toUpperCase();
  $("#account-menu-name").textContent = session?.username || "Admin";
  $("#account-settings-button").textContent = temporary
    ? "Secure administrator account"
    : "Account settings";
}

function closeAccountMenu() {
  $("#account-menu").hidden = true;
  $("#account-button").setAttribute("aria-expanded", "false");
}

async function performLogout() {
  closeAccountMenu();
  if (state.session?.temporary) {
    showLogin();
    return;
  }
  try { await api("/api/v1/auth/logout", { method: "POST" }); }
  catch (_error) { /* A missing/expired session still means the user is signed out. */ }
  showLogin();
}

async function openAccountSettings() {
  closeAccountMenu();
  if (state.session?.temporary) {
    await openSecuritySetup();
    return;
  }
  const modal = $("#modal");
  $("#modal-title").textContent = "Account settings";
  $("#modal-body").innerHTML = `
    <form id="account-form">
      <div class="field">${labelWithHelp("account-username", "Username", "The administrator name used to sign in to this Obsync server.")}<input id="account-username" autocomplete="username" maxlength="64" value="${escapeHtml(state.session?.username || "admin")}" required></div>
      <div class="field">${labelWithHelp("account-current-password", "Current password", "Confirms that you are authorized to change this account.")}<input id="account-current-password" type="password" autocomplete="current-password" required></div>
      <div class="field">${labelWithHelp("account-new-password", "New password (optional)", "Leave this blank to keep the existing password. New passwords must contain at least 10 characters.")}<input id="account-new-password" type="password" autocomplete="new-password" placeholder="Leave blank to keep current password"></div>
      <div class="field">${labelWithHelp("account-confirm-password", "Confirm new password", "Enter the new password again to prevent typing mistakes.")}<input id="account-confirm-password" type="password" autocomplete="new-password"></div>
      <p class="form-error" id="account-error" role="alert"></p>
      <div class="modal-actions"><button class="secondary" type="button" id="account-cancel">Cancel</button><button class="primary" type="submit">Save changes</button></div>
    </form>`;
  if (!modal.open) modal.showModal();
  $("#account-cancel").addEventListener("click", () => modal.close());
  $("#account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const newPassword = $("#account-new-password").value;
    if (newPassword !== $("#account-confirm-password").value) {
      $("#account-error").textContent = "New passwords do not match.";
      return;
    }
    const submit = $('#account-form button[type="submit"]');
    submit.disabled = true;
    try {
      const account = await api("/api/v1/admin/account", {
        method: "PUT",
        body: {
          username: $("#account-username").value.trim(),
          current_password: $("#account-current-password").value,
          new_password: newPassword,
        },
      });
      state.session.username = account.username;
      updateSecurityState(state.session);
      modal.close();
      toast("Account settings updated.");
    } catch (error) {
      $("#account-error").textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
}

async function openSecuritySetup() {
  const modal = $("#modal");
  $("#modal-title").textContent = "Secure administrator account";
  $("#modal-body").innerHTML = `
    <p class="modal-note security-copy">The temporary <strong>Admin</strong> login has no password. Create a local username and password before exposing Obsync to other devices.</p>
    <form id="secure-admin-form">
      <div class="field">${labelWithHelp("secure-username", "Username", "The local administrator name you will use to sign in.")}<input id="secure-username" autocomplete="username" maxlength="64" value="admin" required></div>
      <div class="field">${labelWithHelp("secure-password", "Password", "Protects access to Obsync computers, settings, and your vault. Use at least 10 characters.")}<input id="secure-password" type="password" autocomplete="new-password" placeholder="At least 10 characters" required></div>
      <div class="field">${labelWithHelp("secure-confirm", "Confirm password", "Enter the new password again to prevent typing mistakes.")}<input id="secure-confirm" type="password" autocomplete="new-password" placeholder="Repeat your password" required></div>
      <label class="check-row"><input id="secure-remember" type="checkbox" checked> Keep me signed in</label>
      <p class="form-error" id="secure-admin-error" role="alert"></p>
      <div class="modal-actions"><button class="secondary" id="continue-temporary" type="button">Continue for now</button><button class="primary" type="submit">Secure account</button></div>
    </form>`;
  if (!modal.open) modal.showModal();
  $("#continue-temporary").addEventListener("click", () => modal.close());
  $("#secure-admin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = $("#secure-username").value.trim();
    const password = $("#secure-password").value;
    const confirm = $("#secure-confirm").value;
    const submit = $('#secure-admin-form button[type="submit"]');
    $("#secure-admin-error").textContent = "";
    if (password !== confirm) {
      $("#secure-admin-error").textContent = "Passwords do not match.";
      return;
    }
    submit.disabled = true;
    try {
      const session = await api("/api/v1/auth/setup", {
        method: "POST",
        authRequest: true,
        body: { username, password, remember: $("#secure-remember").checked },
      });
      state.session = { ...session, temporary: false, account_registered: true };
      updateSecurityState(state.session);
      clearLegacyToken();
      modal.close();
      toast("Administrator account secured.");
    } catch (error) {
      $("#secure-admin-error").textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
}

async function openApp(session, promptToSecure = false) {
  state.session = session;
  clearLegacyToken();
  const meta = await api("/api/v1/meta", { authRequest: true });
  $("#version-label").textContent = `v${meta.version}`;
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  updateSecurityState(session);
  await Promise.all([refreshPipelineStatus(), navigate("overview")]);
  startLiveUpdates();
  if (session?.temporary && promptToSecure) await openSecuritySetup();
}

function updateShellStatus(data) {
  if (!data) return;
  updatePipelineControl(data.pipeline);
  const review = data.stats?.review || 0;
  $("#review-badge").hidden = !review;
  $("#review-badge").textContent = review;
  const vaultMissing = data.vault?.configured === false;
  $("#vault-badge").hidden = !vaultMissing;
}

function overviewSignature(data) {
  return JSON.stringify({
    stats: data.stats,
    pipeline: data.pipeline,
    vault: [data.vault?.configured, data.vault?.writable, data.vault?.path],
    event: data.recent_events?.[0]?.id || 0,
    active: (data.active_work || []).map((item) => [
      item.document_id, item.phase, item.updated_at, item.outcome,
    ]),
    sweep: data.vault_sweep ? [data.vault_sweep.id, data.vault_sweep.status, data.vault_sweep.processed_notes, data.vault_sweep.current_note] : null,
  });
}

async function liveRefresh() {
  if (state.liveRefreshing || !state.session || document.hidden || $("#modal").open) return;
  const active = document.activeElement;
  if (active && $("#content").contains(active) && ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName)) return;
  state.liveRefreshing = true;
  try {
    const overview = await api("/api/v1/admin/overview");
    const signature = overviewSignature(overview);
    const changed = signature !== state.liveSignature;
    state.liveSignature = signature;
    updateShellStatus(overview);
    if (changed && ["overview", "sources", "vault", "documents", "review", "settings"].includes(state.view)) {
      await navigate(state.view, { silent: true });
    }
  } catch (_error) {
    // The next interval retries. Authentication failures are handled by api().
  } finally {
    state.liveRefreshing = false;
  }
}

function stopAiActivityStream() {
  if (state.aiEventSource) state.aiEventSource.close();
  state.aiEventSource = null;
  state.aiStreamRevision = -1;
}

function startAiActivityStream() {
  stopAiActivityStream();
  if (!state.session || typeof EventSource === "undefined") return;
  const source = new EventSource("/api/v1/admin/ai/activity/stream");
  state.aiEventSource = source;
  source.addEventListener("ai-activity", (event) => {
    if (state.aiEventSource !== source) return;
    try {
      const activity = JSON.parse(event.data);
      const revision = Number(activity.revision ?? event.lastEventId ?? 0);
      if (revision < state.aiStreamRevision) return;
      state.aiStreamRevision = revision;
      state.aiActivity = activity;
      if (state.view === "local-ai") updateAiActivity(activity);
    } catch (_error) {
      // Ignore a malformed event. EventSource keeps the last valid activity visible.
    }
  });
}

function stopLiveUpdates() {
  if (state.liveTimer) clearInterval(state.liveTimer);
  state.liveTimer = null;
  stopAiActivityStream();
  state.aiScrollTimers.forEach((timer) => clearTimeout(timer));
  state.aiScrollTimers.clear();
}

function startLiveUpdates() {
  stopLiveUpdates();
  state.liveTimer = setInterval(liveRefresh, 3000);
  startAiActivityStream();
}

function updatePipelineControl(status) {
  state.pipeline = status;
  const button = $("#pipeline-toggle");
  if (!button) return;
  button.classList.toggle("running", status.enabled);
  button.classList.toggle("stopped", !status.enabled);
  $("span", button).textContent = status.enabled ? "Stop Global Sync" : "Start Global Sync";
  button.title = status.enabled
    ? "Stop all folder syncing and cancel active AI work"
    : "Start all running folders and reconcile missed changes";
  $$('[data-pipeline-work="true"]').forEach((control) => { control.disabled = !status.enabled; });
}

async function refreshPipelineStatus() {
  const status = await api("/api/v1/admin/pipeline");
  updatePipelineControl(status);
  return status;
}

async function togglePipeline() {
  const button = $("#pipeline-toggle");
  button.disabled = true;
  const stopping = state.pipeline.enabled;
  try {
    const status = await api(
      stopping ? "/api/v1/admin/pipeline/stop" : "/api/v1/admin/pipeline/start",
      { method: "POST" },
    );
    updatePipelineControl(status);
    toast(stopping
      ? "Global sync stopped. Active sync and AI work was cancelled."
      : "Global sync started. Running folders will reconcile missed changes.");
    await navigate(state.view, { silent: true });
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function bootstrapAuth() {
  try {
    const status = await api("/api/v1/auth/status", { authRequest: true });
    if (status.setup_required) {
      if (status.temporary_admin_available) {
        await openApp({
          authenticated: true,
          username: "Admin",
          temporary: true,
          account_registered: false,
        }, true);
      } else {
        showSetupRequired();
      }
      return;
    }
    clearLegacyToken();
    if (status.authenticated) await openApp(status);
    else showLogin();
  } catch (error) {
    showLogin(error.message === "Sign in required" ? "" : error.message);
  }
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("obsync_theme", theme);
  $("#theme-button").textContent = theme === "dark" ? "☀" : "◐";
}

async function refreshReviewBadge() {
  const data = await api("/api/v1/admin/overview");
  const count = data.stats.review;
  $("#review-badge").hidden = !count;
  $("#review-badge").textContent = count;
}

function loading() {
  $("#content").innerHTML = '<div class="skeleton"></div><br><div class="skeleton"></div>';
}

async function navigate(view, { silent = false } = {}) {
  const scrollState = silent ? captureScrollState() : null;
  state.view = view;
  const [title, subtitle] = viewMeta[view];
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("#sidebar").classList.remove("open");
  $("#menu-button").setAttribute("aria-expanded", "false");
  if (!silent) loading();
  try {
    if (view === "overview") await renderOverview();
    if (view === "sources") await renderSources();
    if (view === "vault") await renderVault();
    if (view === "local-ai") await renderLocalAI();
    if (view === "documents") await renderDocuments(false);
    if (view === "review") await renderDocuments(true);
    if (view === "settings") await renderSettings();
    if (view === "help") await renderHelp();
    restoreScrollState(scrollState);
  } catch (error) {
    $("#content").innerHTML = `<div class="empty"><div class="empty-icon">!</div><p>${escapeHtml(error.message)}</p><button class="secondary" id="try-again">Try again</button></div>`;
    $("#try-again")?.addEventListener("click", () => navigate(view));
  }
}

function statCard(label, value, icon, style = "", help = "") {
  return `<article class="stat-card ${style}"><span class="label">${escapeHtml(label)}${help ? helpIcon(help, `About ${label}`) : ""}</span><span class="stat-icon">${icon}</span><strong>${value}</strong></article>`;
}

const comparisonMeta = {
  "in-sync": ["In Obsidian", "good"],
  modified: ["Modified", "warn"],
  new: ["New", "bad"],
  "vault-missing": ["Missing from Obsidian", "bad"],
  "source-missing": ["Source missing", "bad"],
  "possible-duplicate": ["Possible duplicate", "warn"],
  ignored: ["Disregarded", "neutral"],
  checking: ["Checking vault", "neutral"],
};

function comparisonBadge(status, count = null) {
  const [label, style] = comparisonMeta[status] || [status || "Unknown", "neutral"];
  const suffix = count === null ? "" : ` ${count}`;
  return `<span class="comparison-badge ${style}"><i></i>${escapeHtml(label)}${suffix}</span>`;
}

async function pollCommand(commandId, message = "Working on the desktop…") {
  toast(message);
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const command = await api(`/api/v1/admin/commands/${commandId}`);
    if (command.status === "completed") return command;
    if (command.status === "failed") throw new Error(command.result || "Desktop command failed.");
    if (command.status === "cancelled") throw new Error(command.result || "Stopped by user.");
  }
  throw new Error("The desktop did not finish the request. Make sure its Obsync agent is running.");
}

async function waitForRootStable(rootId) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const data = await api(`/api/v1/admin/documents?root_id=${encodeURIComponent(rootId)}&limit=500`);
    const busy = data.items.some((item) => ["checking"].includes(item.comparison_status)
      || ["processing", "pending-write"].includes(item.status));
    if (!busy) return data;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error("The vault comparison is still pending. Make sure the selected vault computer is online.");
}

async function openAddFolder(agent) {
  const modal = $("#modal");
  $("#modal-title").textContent = "Add folder to sync";
  $("#modal-body").innerHTML = `
    <p class="modal-note">The folder browser will open on <strong>${escapeHtml(agent.name)}</strong>. Obsync scans the folder first and compares every file with the vault before anything is written.</p>
    <form id="add-folder-form">
      <div class="field">${labelWithHelp("folder-name", "Folder name (optional)", "A friendly label for this watched folder. It does not rename the real folder.")}<input id="folder-name" placeholder="Client files"></div>
      <div class="field">${labelWithHelp("folder-destination", "Obsidian destination", "The top-level vault folder where Obsync creates Markdown notes for this source.")}<input id="folder-destination" value="Obsync" required><small>Generated notes stay under this folder in the vault.</small></div>
      <p class="inline-status">After selecting the folder, red files are new or missing, orange files changed, and green files already match Obsidian.</p>
      <div class="modal-actions"><button class="secondary" type="button" id="folder-cancel">Cancel</button><button class="primary" type="submit">Open folder browser</button></div>
    </form>`;
  modal.showModal();
  $("#folder-cancel").addEventListener("click", () => modal.close());
  $("#add-folder-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = $('#add-folder-form button[type="submit"]');
    submit.disabled = true;
    try {
      const command = await api(`/api/v1/admin/agents/${agent.id}/select-source`, {
        method: "POST",
        body: {
          name: $("#folder-name").value.trim(),
          destination: $("#folder-destination").value.trim() || "Obsync",
        },
      });
      $("#modal-body").innerHTML = `<p class="inline-status"><span class="connection-wait"><i></i>Choose the folder on ${escapeHtml(agent.name)}. Obsync will scan and compare it immediately.</span></p><p class="modal-note">Keep the Obsync agent running on that computer while the picker is open.</p>`;
      const completed = await pollCommand(command.id, `Folder browser requested on ${agent.name}.`);
      const result = JSON.parse(completed.result || "{}");
      const rootId = result.inventory?.root_id;
      if (rootId) await waitForRootStable(rootId);
      modal.close();
      toast("Folder added and compared with Obsidian.");
      await navigate("sources");
    } catch (error) {
      modal.close();
      toast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });
}

function openDisconnectComputer(agent) {
  const modal = $("#modal");
  $("#modal-title").textContent = "Disconnect computer";
  $("#modal-body").innerHTML = `
    <div class="warning-box"><strong>Disconnect ${escapeHtml(agent.name)}?</strong><p>This immediately revokes this computer's access and removes its watched-folder and file records from Obsync.</p></div>
    <ul class="plain-list">
      <li>Original files on the computer are never changed or deleted.</li>
      <li>Existing Markdown notes in Obsidian are kept.</li>
      <li>The computer needs a new one-time code before it can reconnect.</li>
    </ul>
    <p class="modal-note">If this computer is the active vault writer, choose another vault in Settings first.</p>
    <div class="modal-actions"><button class="secondary" type="button" id="disconnect-cancel">Cancel</button><button class="danger" type="button" id="disconnect-confirm">Disconnect computer</button></div>`;
  modal.showModal();
  $("#disconnect-cancel").addEventListener("click", () => modal.close());
  $("#disconnect-confirm").addEventListener("click", async () => {
    const button = $("#disconnect-confirm");
    button.disabled = true;
    button.textContent = "Disconnecting…";
    try {
      const result = await api(`/api/v1/admin/agents/${agent.id}`, { method: "DELETE" });
      modal.close();
      toast(`${result.name} disconnected. Source files and Obsidian notes were kept.`);
      await navigate("sources");
    } catch (error) {
      button.disabled = false;
      button.textContent = "Disconnect computer";
      toast(error.message, true);
    }
  });
}

async function openReconnectComputer(agent) {
  const modal = $("#modal");
  $("#modal-title").textContent = `Reconnect ${agent.name}`;
  $("#modal-body").innerHTML = '<p class="modal-note">Preparing a safe reconnect code…</p>';
  modal.showModal();
  try {
    const enrollment = await api(`/api/v1/admin/agents/${agent.id}/reconnect`, {
      method: "POST",
      body: { minutes: 20 },
    });
    const server = state.server?.public_url || location.origin;
    const setupDetails = JSON.stringify({ server, code: enrollment.code, name: agent.name });
    $("#modal-body").innerHTML = `
      <div class="good-box"><strong>Your existing computer record will be preserved.</strong><p>Watched folders, document history, vault assignment, and generated notes stay attached to ${escapeHtml(agent.name)}.</p></div>
      <div class="pair-steps">
        <div class="pair-step"><p><strong>1.</strong> On ${escapeHtml(agent.name)}, open the installed Obsync Desktop and choose <strong>Start this PC</strong>. A valid saved connection will reconnect automatically.</p><button class="primary full" id="open-reconnect-desktop" type="button">Open installed Desktop</button></div>
        <div class="pair-step admin-required"><p><strong>2.</strong> If the saved connection is damaged or unauthorized, download the latest Desktop, right-click it, choose <strong>Run as administrator</strong>, and paste these repair details.</p><a class="secondary button-link full" href="/api/v1/downloads/windows-desktop">Download latest Desktop</a></div>
        <div class="pair-step"><button class="secondary full copy-reconnect-setup" type="button" data-copy="${escapeHtml(setupDetails)}">Copy reconnect details</button><details class="manual-pair-details"><summary>Or enter the repair details manually</summary><div class="pair-detail"><span>Server</span><code>${escapeHtml(server)}</code></div><div class="pair-detail"><span>Code</span><code>${escapeHtml(enrollment.code)}</code></div><div class="pair-detail"><span>Name</span><code>${escapeHtml(agent.name)}</code></div></details></div>
      </div>
      <p class="inline-status" id="pairing-status"><span class="connection-wait"><i></i>Waiting for ${escapeHtml(agent.name)} to reconnect…</span></p>
      <p class="modal-note">The repair code expires in 20 minutes. It updates this PC in place; it does not create a duplicate computer.</p>`;
    $("#open-reconnect-desktop").addEventListener("click", () => {
      window.location.href = "obsync://open";
      toast("Windows was asked to open Obsync Desktop.");
    });
    $(".copy-reconnect-setup").addEventListener("click", () => copyText(setupDetails));
    pollEnrollment(enrollment.id);
  } catch (error) {
    modal.close();
    toast(error.message, true);
  }
}

function openRemoveFolder(root) {
  const modal = $("#modal");
  $("#modal-title").textContent = "Remove synced folder";
  $("#modal-body").innerHTML = `
    <div class="warning-box"><strong>Stop syncing ${escapeHtml(root.name)}?</strong><p>The folder will be removed from this computer's Obsync folder list.</p></div>
    <ul class="plain-list">
      <li>The real folder and every original file on the PC are kept.</li>
      <li>Existing Markdown notes in Obsidian are kept.</li>
      <li>Obsync removes its file records for this folder. Adding it again starts a fresh comparison.</li>
    </ul>
    <div class="modal-actions"><button class="secondary" type="button" id="remove-folder-cancel">Cancel</button><button class="danger" type="button" id="remove-folder-confirm">Remove folder</button></div>`;
  modal.showModal();
  $("#remove-folder-cancel").addEventListener("click", () => modal.close());
  $("#remove-folder-confirm").addEventListener("click", async () => {
    const button = $("#remove-folder-confirm");
    button.disabled = true;
    button.textContent = "Removing…";
    try {
      const result = await api(`/api/v1/admin/roots/${root.id}`, { method: "DELETE" });
      modal.close();
      toast(`${result.name} removed. Original files and Obsidian notes were kept.`);
      await navigate("sources");
    } catch (error) {
      button.disabled = false;
      button.textContent = "Remove folder";
      toast(error.message, true);
    }
  });
}

async function openRootFiles(root) {
  const modal = $("#modal");
  $("#modal-title").textContent = root.name;
  $("#modal-body").innerHTML = '<div class="skeleton"></div>';
  modal.showModal();
  try {
    const data = await api(`/api/v1/admin/documents?root_id=${encodeURIComponent(root.id)}&limit=500`);
    const byId = new Map(data.items.map((item) => [item.id, item]));
    const rows = data.items.map((doc) => `<div class="inventory-file">
      ${comparisonBadge(doc.comparison_status)}
      <div><strong>${escapeHtml(doc.source_name)}</strong><small title="${escapeHtml(doc.source_path)}">${escapeHtml(doc.source_path)}</small>${doc.duplicate_path ? `<small class="duplicate-match">Possible existing note: ${escapeHtml(doc.duplicate_title || doc.duplicate_path)} · ${escapeHtml(doc.duplicate_path)}</small>` : ""}</div>
      ${doc.needs_review ? `<div class="document-actions inventory-actions">${doc.status === "ai-stopped" ? "" : `<button class="secondary approve" data-id="${doc.id}">Approve</button>`}<button class="quiet redo-review" data-id="${doc.id}">Redo AI review</button><button class="danger disregard-review" data-id="${doc.id}">Disregard</button>${doc.comparison_status === "possible-duplicate" ? `<button class="quiet allow-duplicate" data-id="${doc.id}">Create separate note</button>` : ""}</div>` : ""}
    </div>`).join("");
    $("#modal-body").innerHTML = `<div class="inventory-scroll" data-preserve-scroll="true" data-scroll-key="folder-inventory">${rows || '<div class="empty">No files found in the latest scan.</div>'}</div>
      <div class="status-legend">${comparisonBadge("in-sync")}${comparisonBadge("modified")}${comparisonBadge("new")}${comparisonBadge("vault-missing")}</div>`;
    const reload = async () => { modal.close(); await openRootFiles(root); await refreshReviewBadge(); };
    $$(".approve", $("#modal-body")).forEach((button) => button.addEventListener("click", async () => {
      await api(`/api/v1/admin/documents/${button.dataset.id}/approve`, { method: "POST" }); toast("Review approved."); await reload();
    }));
    $$(".disregard-review", $("#modal-body")).forEach((button) => button.addEventListener("click", () => openDisregardReview(byId.get(button.dataset.id), async () => openRootFiles(root))));
    $$(".redo-review", $("#modal-body")).forEach((button) => button.addEventListener("click", () => openRedoReview(byId.get(button.dataset.id), async () => openRootFiles(root))));
    $$(".allow-duplicate", $("#modal-body")).forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api(`/api/v1/admin/documents/${button.dataset.id}/allow-duplicate`, { method: "POST" });
        toast("Duplicate warning dismissed. A separate note is now allowed.");
        modal.close();
        await openRootFiles(root);
      } catch (error) { toast(error.message, true); button.disabled = false; }
    }));
  } catch (error) {
    $("#modal-body").innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`;
  }
}

async function renderOverview() {
  const data = await api("/api/v1/admin/overview");
  state.overview = data;
  state.liveSignature = overviewSignature(data);
  updateShellStatus(data);
  const stats = data.stats;
  $("#review-badge").hidden = !stats.review;
  $("#review-badge").textContent = stats.review;
  const events = data.recent_events.map((event) => `
    <li class="event ${escapeHtml(event.level)}"><i class="event-dot"></i><p>${escapeHtml(event.message)}</p><small>${relativeTime(event.created_at)}</small></li>
  `).join("") || '<li class="empty">No activity yet.</li>';
  const activeRows = (data.active_work || []).map((work) => `
    <div class="active-work-row">
      <span class="activity-spinner" aria-hidden="true"></span>
      <div><strong>${escapeHtml(work.source_name)}</strong><small title="${escapeHtml(work.source_path)}">${escapeHtml(work.agent_name)} · ${escapeHtml(work.root_name)} · ${escapeHtml(work.source_path)}</small></div>
      <div class="active-work-stage"><span>${escapeHtml(work.phase_label || work.phase)}</span><small>${work.elapsed_seconds || 0}s</small></div>
    </div>
  `).join("") || '<div class="active-work-empty"><span>✓</span><div><strong>No file is active</strong><small>Obsync is waiting for the next source change or manual sync.</small></div></div>';
  $("#content").innerHTML = `
    ${data.vault.configured === false ? '<div class="pipeline-banner vault-required"><div><strong>Choose your Obsidian Vault before syncing</strong><p>Obsync will not write anything until you confirm the destination in the Obsidian Vault tab.</p></div><button class="primary" data-go="vault">Choose vault</button></div>' : ""}
    ${data.pipeline.enabled ? "" : '<div class="pipeline-banner"><div><strong>Global sync is stopped</strong><p>All folder watching, scans, syncs, and AI classification are paused. Source files and existing notes are unchanged.</p></div></div>'}
    <div class="stats">
      ${statCard("Synced documents", stats.synced, "↗", "good", "Files whose current source content is represented by an Obsync-managed note in the vault.")}
      ${statCard("Connected computers", `${stats.online_computers}/${stats.computers}`, "◎", "", "Online computers divided by all known computers. The central Obsync server counts as one computer.")}
      ${statCard("Needs review", stats.review, "◇", stats.review ? "warn" : "", "Documents whose automated classification confidence is below your review threshold.")}
      ${statCard("Errors", stats.errors, "!", stats.errors ? "bad" : "", "Documents that could not be extracted, classified, compared, or written.")}
    </div>
    <section class="panel active-work-panel"><div class="panel-head"><h3>${headingWithHelp("Current active file", "The exact source file Obsync is extracting, reviewing with Local AI, checking, or writing right now.")}</h3><span class="active-count">${(data.active_work || []).length ? `${data.active_work.length} active` : "Idle"}</span></div><div class="panel-scroll active-work-list" data-preserve-scroll="true" data-scroll-key="overview-active">${activeRows}</div></section>
    <div class="grid-two">
      <section class="panel"><div class="panel-head"><h3>${headingWithHelp("Recent activity", "The latest scans, syncs, warnings, and connection events.")}</h3><button class="quiet" data-go="documents" title="Open the complete document list">View all</button></div><ul class="event-list panel-scroll" data-preserve-scroll="true" data-scroll-key="overview-events">${events}</ul></section>
      <section class="panel"><div class="panel-head"><h3>${headingWithHelp("System", "A quick check of the active vault, watched folders, and missing sources.")}</h3></div>
        <div class="event-list panel-scroll" data-preserve-scroll="true" data-scroll-key="overview-system">
          <div class="event"><i class="event-dot"></i><p>Obsidian vault<br><small>${escapeHtml(data.vault.path)}</small></p><small>${data.vault.writable ? "Ready" : "Unavailable"}</small></div>
          <div class="event"><i class="event-dot"></i><p>Watched folders</p><small>${stats.roots}</small></div>
          <div class="event"><i class="event-dot"></i><p>Missing sources</p><small>${stats.missing}</small></div>
        </div>
      </section>
    </div>`;
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.go)));
}

async function renderSources() {
  const [server, agentData, rootData, pipeline, overview] = await Promise.all([
    api("/api/v1/admin/server"), api("/api/v1/admin/agents"), api("/api/v1/admin/roots"), api("/api/v1/admin/pipeline"), api("/api/v1/admin/overview"),
  ]);
  updateShellStatus(overview);
  state.server = server;
  state.agents = agentData.items;
  state.roots = rootData.items;
  const cards = state.agents.map((agent) => {
    const roots = state.roots.filter((root) => root.agent_id === agent.id);
    const rootRows = roots.map((root) => {
      const rootState = root.sync_state || "running";
      const canWork = pipeline.enabled && rootState === "running" && overview.vault.configured !== false;
      return `<div class="root-row inventory-root">
      <div class="root-title"><strong>${escapeHtml(root.name)} ${helpIcon("A watched source folder with independent Start, Pause, and Stop controls.", `About ${root.name}`)}</strong><span class="root-state ${rootState}">${escapeHtml(rootState)}</span><span>${root.file_count || root.document_count || 0} files</span></div>
      <code>${escapeHtml(root.path)}</code>
      <div class="root-comparison">
        ${comparisonBadge("in-sync", root.in_sync_count || 0)}
        ${comparisonBadge("modified", root.modified_count || 0)}
        ${comparisonBadge("new", root.new_count || 0)}
        ${comparisonBadge("vault-missing", root.missing_count || 0)}
        ${root.duplicate_count ? comparisonBadge("possible-duplicate", root.duplicate_count) : ""}
        ${root.checking_count ? comparisonBadge("checking", root.checking_count) : ""}
      </div>
      <div class="root-control-row" aria-label="Sync controls for ${escapeHtml(root.name)}"><button class="root-state-button start-root" data-root="${root.id}" data-state="running" ${rootState === "running" ? "disabled" : ""}>Start</button><button class="root-state-button pause-root" data-root="${root.id}" data-state="paused" ${rootState !== "running" ? "disabled" : ""}>Pause</button><button class="root-state-button stop-root" data-root="${root.id}" data-state="stopped" ${rootState === "stopped" ? "disabled" : ""}>Stop</button></div>
      <div class="root-actions"><button class="danger remove-root" data-root="${root.id}" title="Remove this folder from Obsync without deleting originals or notes">Remove</button><button class="quiet view-root" data-root="${root.id}" title="Inspect every file and its current comparison status">View files</button><button class="quiet scan-root" data-pipeline-work="true" data-root="${root.id}" title="Compare the folder with Obsidian without writing notes" ${canWork ? "" : "disabled"}>Scan</button><button class="secondary sync-root" data-pipeline-work="true" data-root="${root.id}" title="Process only new, modified, or missing items" ${canWork ? "" : "disabled"}>Sync changes</button></div>
    </div>`;
    }).join("");
    return `<article class="source-card">
      <div class="source-top"><span class="device-icon">${agent.os_name === "Windows" ? "▣" : "◫"}</span><div><h3>${escapeHtml(agent.name)} ${helpIcon("A paired Obsync Desktop app that gives Obsync safe access to folders on this computer.", `About ${agent.name}`)}</h3><p>${escapeHtml(agent.os_name)} · seen ${relativeTime(agent.last_seen_at)}</p>${agent.vault_ready ? '<span class="device-role">VAULT WRITER READY</span>' : ""}</div><span class="status-pill ${agent.status}" title="Whether Obsync Desktop is currently communicating with the server">${agent.status}</span></div>
      ${rootRows || '<div class="root-row">No watched folders registered yet.</div>'}
      <div class="source-stats"><span>${agent.document_count || 0} files indexed</span><div class="source-actions">${agent.status === "online" ? "" : `<button class="secondary reconnect-computer" data-agent="${agent.id}" title="Repair this PC without removing its folders or history">Reconnect</button>`}<button class="danger disconnect-computer" data-agent="${agent.id}" title="Revoke this computer and remove it from Obsync">Disconnect</button><button class="primary add-folder" data-pipeline-work="true" data-agent="${agent.id}" title="Open this computer's folder browser and add a directory to watch" ${pipeline.enabled && overview.vault.configured !== false ? "" : "disabled"}>+ Add folder</button></div></div>
    </article>`;
  }).join("");
  const serverCard = `<article class="source-card server-card">
    <div class="source-top"><span class="device-icon">◆</span><div><h3>${escapeHtml(server.name)} ${helpIcon("The central Obsync server processes files, stores the sync ledger, and coordinates every Obsync Desktop app.", "About the Obsync server")}</h3><p>${server.container ? "Docker container" : escapeHtml(server.os_name)} · ${escapeHtml(server.hostname)}</p><span class="device-role">ALWAYS CONNECTED</span></div><span class="status-pill" title="The central server is running">online</span></div>
    <div class="source-stats"><span>Processes files and coordinates every desktop. Vault selection lives in the Obsidian Vault tab.</span></div>
  </article>`;
  const emptyHelp = state.agents.length ? "" : '<div class="panel source-help"><strong>1 connected computer means this Obsync server.</strong><p>Pair the Windows, Linux, or macOS computer that contains your source folders. It will then appear here and in the vault computer selector.</p></div>';
  $("#content").innerHTML = `<div class="section-head"><div><h2>${headingWithHelp("Computers and folders", "Connect Obsync Desktop, choose one or more source folders, compare them with the vault, then sync only what changed.")}</h2><p>Add folders, scan them against Obsidian, inspect differences, then sync only what changed.</p></div><button class="primary" id="add-device" title="Connect a Windows, Linux, or macOS computer">+ Add another computer</button></div>${emptyHelp}<div class="source-grid">${serverCard}${cards}</div>`;
  $("#add-device").addEventListener("click", openEnrollment);
  $$(".add-folder").forEach((button) => button.addEventListener("click", () => {
    const agent = state.agents.find((item) => item.id === button.dataset.agent);
    if (agent?.status !== "online") { toast("That computer is offline. Start its Obsync agent first.", true); return; }
    openAddFolder(agent);
  }));
  $$(".disconnect-computer").forEach((button) => button.addEventListener("click", () => {
    const agent = state.agents.find((item) => item.id === button.dataset.agent);
    if (agent) openDisconnectComputer(agent);
  }));
  $$(".reconnect-computer").forEach((button) => button.addEventListener("click", () => {
    const agent = state.agents.find((item) => item.id === button.dataset.agent);
    if (agent) openReconnectComputer(agent);
  }));
  $$(".remove-root").forEach((button) => button.addEventListener("click", () => {
    const root = state.roots.find((item) => item.id === button.dataset.root);
    if (root) openRemoveFolder(root);
  }));
  $$(".root-state-button").forEach((button) => button.addEventListener("click", async () => {
    const requested = button.dataset.state;
    button.disabled = true;
    try {
      const result = await api(`/api/v1/admin/roots/${button.dataset.root}/state`, {
        method: "POST",
        body: { sync_state: requested },
      });
      toast(`${result.name} is ${requested}.`);
      await navigate("sources", { silent: true });
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }));
  $$(".view-root").forEach((button) => button.addEventListener("click", () => {
    openRootFiles(state.roots.find((root) => root.id === button.dataset.root));
  }));
  $$(".scan-root").forEach((button) => button.addEventListener("click", async () => {
    try {
      const command = await api(`/api/v1/admin/roots/${button.dataset.root}/scan`, { method: "POST" });
      await pollCommand(command.id, "Comparing folder with Obsidian…");
      await waitForRootStable(button.dataset.root);
      toast("Scan complete."); await navigate("sources");
    } catch (error) { toast(error.message, true); }
  }));
  $$(".sync-root").forEach((button) => button.addEventListener("click", async () => {
    try {
      const command = await api(`/api/v1/admin/roots/${button.dataset.root}/sync`, { method: "POST" });
      await pollCommand(command.id, "Syncing new and modified files…");
      await waitForRootStable(button.dataset.root);
      toast("Folder sync complete."); await navigate("sources");
    } catch (error) { toast(error.message, true); }
  }));
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
  } catch (_error) {
    const input = document.createElement("textarea");
    input.value = value;
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  toast("Copied to clipboard.");
}

async function pollEnrollment(enrollmentId) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (!$("#modal").open || !$("#pairing-status")) return;
    try {
      const status = await api(`/api/v1/admin/enrollments/${enrollmentId}`);
      if (status.connected) {
        $("#pairing-status").className = "inline-status good";
        $("#pairing-status").textContent = `${status.agent.name} connected successfully. It will appear on this page after you close this window.`;
        await navigate("sources");
        return;
      }
    } catch (_error) { /* Keep polling while the one-time code is active. */ }
  }
}

async function openEnrollment() {
  const modal = $("#modal");
  $("#modal-title").textContent = "Add a computer";
  const suggestedServer = state.server?.public_url || location.origin;
  $("#modal-body").innerHTML = `
    <p class="modal-note">The Obsync server is already connected. Add the Windows PC, Mac, or Linux computer whose folders are outside the server.</p>
    <div class="field">${labelWithHelp("device-label", "Computer name", "A friendly name such as Office PC. It helps you identify the computer in folder and vault selectors.")}<input id="device-label" placeholder="Office PC"></div>
    <div class="field">${labelWithHelp("pair-server-url", "Server address", "The address this computer uses to reach Obsync. Use a LAN, VPN, or HTTPS address—not localhost for a different computer.")}<input id="pair-server-url" value="${escapeHtml(suggestedServer)}"><small>Do not use localhost for a different computer.</small></div>
    <label class="check-row"><input id="device-has-vault" type="checkbox"> This computer contains the Obsidian vault ${helpIcon("Select this when the real vault is stored on this computer, such as in Windows Documents. Obsync Desktop will ask you to choose it.", "About the vault computer option")}</label>
    <button class="primary full" id="create-code">Create pairing code</button>`;
  modal.showModal();
  $("#create-code").addEventListener("click", async () => {
    try {
      const label = $("#device-label").value.trim() || "Windows PC";
      const server = $("#pair-server-url").value.trim().replace(/\/$/, "");
      if (!/^https?:\/\//i.test(server)) throw new Error("Enter a complete http:// or https:// server address.");
      if (/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|\/|$)/i.test(server)) {
        throw new Error("localhost points back to the other computer. Use this server's LAN or Tailscale address.");
      }
      const hasVault = $("#device-has-vault").checked;
      const enrollment = await api("/api/v1/admin/enrollments", { method: "POST", body: { label } });
      const setupDetails = JSON.stringify({ server, code: enrollment.code, name: label });
      const download = "/api/v1/downloads/windows-desktop";
      $("#modal-body").innerHTML = `
        <p class="modal-note">This one-time code expires in 20 minutes. Obsync Desktop installs for your account, contains the folder watcher, runs silently, and starts automatically when you sign in.</p><div class="pair-code">${escapeHtml(enrollment.code)}</div>
        <div class="pair-steps">
          <div class="pair-step admin-required"><p><strong>1.</strong> On the Windows PC, download Obsync Desktop. Then right-click it and choose <strong>Run as administrator</strong>. Administrator access is required only for setup and automatic startup.</p><a class="primary download-button" href="${download}">Download Obsync Desktop</a></div>
          <div class="pair-step"><p><strong>2.</strong> Copy all setup details, then click <strong>Paste setup details</strong> in Obsync Desktop.</p><button class="secondary full copy-pair-setup" type="button" data-copy="${escapeHtml(setupDetails)}">Copy all setup details</button>
            <details class="manual-pair-details"><summary>Or enter the details manually</summary>
            <div class="pair-detail"><span>Server</span><code>${escapeHtml(server)}</code><button class="quiet copy-pair-value" type="button" data-copy="${escapeHtml(server)}">Copy</button></div>
            <div class="pair-detail"><span>Code</span><code>${escapeHtml(enrollment.code)}</code><button class="quiet copy-pair-value" type="button" data-copy="${escapeHtml(enrollment.code)}">Copy</button></div>
            <div class="pair-detail"><span>Name</span><code>${escapeHtml(label)}</code><button class="quiet copy-pair-value" type="button" data-copy="${escapeHtml(label)}">Copy</button></div>
            </details>
          </div>
          <div class="pair-step"><p><strong>3.</strong> Click <strong>Connect and install</strong>${hasVault ? " and select the Obsidian vault when asked" : ""}. The desktop app includes Start this PC, Stop this PC, and Open Obsync controls.</p></div>
        </div>
        <p class="inline-status" id="pairing-status"><span class="connection-wait"><i></i>Waiting for ${escapeHtml(label)} to connect…</span></p>
        <p class="modal-note"><strong>Windows:</strong> open the downloaded Desktop app as Administrator. No PowerShell window stays open afterward. Linux and macOS users can follow the manual steps in Help.</p>`;
      $$(".copy-pair-value").forEach((button) => button.addEventListener("click", () => copyText(button.dataset.copy)));
      $(".copy-pair-setup").addEventListener("click", () => copyText(setupDetails));
      pollEnrollment(enrollment.id);
    } catch (error) { toast(error.message, true); }
  });
}

function statusClass(status) {
  return ["error", "processing"].includes(status) ? status : "";
}

function openDisregardReview(documentItem, onComplete) {
  const modal = $("#modal");
  $("#modal-title").textContent = "Disregard this file";
  $("#modal-body").innerHTML = `<div class="warning-box"><strong>${escapeHtml(documentItem.title || documentItem.source_name)}</strong><p>Obsync will keep the source file unchanged, create no new note, never delete an existing managed note, and remove this item from Review. You can still request a new AI review later from Documents.</p></div><div class="modal-actions"><button class="secondary" id="disregard-cancel" type="button">Cancel</button><button class="danger" id="disregard-confirm" type="button">Disregard file</button></div>`;
  if (!modal.open) modal.showModal();
  $("#disregard-cancel").addEventListener("click", () => modal.close());
  $("#disregard-confirm").addEventListener("click", async () => {
    const button = $("#disregard-confirm"); button.disabled = true;
    try {
      await api(`/api/v1/admin/documents/${documentItem.id}/disregard`, { method: "POST" });
      modal.close(); toast("File disregarded. No note will be created."); await onComplete();
    } catch (error) { toast(error.message, true); button.disabled = false; }
  });
}

function openRedoReview(documentItem, onComplete) {
  const modal = $("#modal");
  $("#modal-title").textContent = "Redo AI review";
  $("#modal-body").innerHTML = `<p class="modal-note">Local AI will re-read <strong>${escapeHtml(documentItem.source_name)}</strong> from the source computer. Add specific corrections such as the category, title style, or tag the model should use.</p><form id="redo-review-form"><div class="field">${labelWithHelp("review-feedback", "Notes for the AI (optional)", "Your feedback is added to this one re-review and stored with the document for audit history.")}<textarea id="review-feedback" rows="6" maxlength="4000" placeholder="Example: Use the tag permit-renewal instead of permit, and categorize this under Licenses.">${escapeHtml(documentItem.review_feedback || "")}</textarea><small>Up to 4,000 characters. The protected safety and JSON rules still apply.</small></div><div class="modal-actions"><button class="secondary" id="redo-cancel" type="button">Cancel</button><button class="primary" type="submit">Run AI review again</button></div></form>`;
  if (!modal.open) modal.showModal();
  $("#redo-cancel").addEventListener("click", () => modal.close());
  $("#redo-review-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $('#redo-review-form button[type="submit"]'); button.disabled = true; button.textContent = "Queueing…";
    try {
      await api(`/api/v1/admin/documents/${documentItem.id}/redo-review`, { method: "POST", body: { feedback: $("#review-feedback").value.trim() } });
      modal.close(); toast("New AI review queued. Open Local AI to watch it live."); await onComplete();
    } catch (error) { toast(error.message, true); button.disabled = false; button.textContent = "Run AI review again"; }
  });
}

async function openVaultChangeDiff(changeId) {
  const change = await api(`/api/v1/admin/vault/changes/${changeId}`);
  const modal = $("#modal");
  $("#modal-title").textContent = `Proposed vault change · ${change.path}`;
  const relationships = change.decision?.relationships || [];
  $("#modal-body").innerHTML = `<p class="modal-note"><strong>Reason:</strong> ${escapeHtml(change.reason)}</p>${relationships.length ? `<h3>Evidence-backed relationships</h3><ul class="plain-list">${relationships.map((item) => `<li><strong>${escapeHtml(item.target)}</strong> — ${escapeHtml(item.relationship)} (${Math.round((item.confidence || 0) * 100)}%)<ul>${(item.evidence || []).map((fact) => `<li>${escapeHtml(fact)}</li>`).join("")}</ul></li>`).join("")}</ul>` : '<p class="modal-note">The AI found no supported relationships; this change removes or avoids a generated link block.</p>'}<div class="diff-grid"><section><h3>Before</h3><pre>${escapeHtml(change.before_content)}</pre></section><section><h3>After</h3><pre>${escapeHtml(change.after_content)}</pre></section></div><div class="modal-actions"><button class="secondary" id="diff-close" type="button">Close</button></div>`;
  if (!modal.open) modal.showModal();
  $("#diff-close").addEventListener("click", () => modal.close());
}

async function renderVaultRecommendations() {
  const host = $("#vault-review-results");
  if (!host) return;
  const data = await api("/api/v1/admin/vault/changes?status=pending&limit=200");
  if (!data.items.length) {
    host.innerHTML = '<section class="panel empty"><div class="empty-icon">✓</div><p>No vault-maintenance recommendations need review.</p></section>';
    return;
  }
  host.innerHTML = `<div class="section-head"><div><h2>Vault maintenance recommendations</h2><p>${data.total} proposed whole-vault change(s), each with evidence and a before/after diff.</p></div><div class="settings-actions compact"><button class="secondary" id="apply-all-vault-changes" type="button">Apply all</button><button class="danger" id="reject-all-vault-changes" type="button">Disregard all</button></div></div><div class="vault-review-grid">${data.items.map((change) => `<article class="settings-card vault-change-card"><div><span class="eyebrow">${escapeHtml(change.sweep_type)} SWEEP</span><h3>${escapeHtml(change.path)}</h3><p>${escapeHtml(change.reason)}</p></div><div class="confidence-meter"><span style="width:${Math.round(change.confidence * 100)}%"></span></div><small>${Math.round(change.confidence * 100)}% confidence · ${(change.decision?.relationships || []).length} relationship(s) · ${(change.evidence || []).length} evidence item(s)</small><div class="document-actions"><button class="quiet view-vault-change" data-id="${change.id}" type="button">View evidence & diff</button><button class="secondary approve-vault-change" data-id="${change.id}" type="button">Apply</button><button class="danger reject-vault-change" data-id="${change.id}" type="button">Disregard</button></div></article>`).join("")}</div>`;
  const reload = async () => { await Promise.all([renderVaultRecommendations(), refreshReviewBadge()]); };
  $$(".view-vault-change", host).forEach((button) => button.addEventListener("click", () => openVaultChangeDiff(button.dataset.id).catch((error) => toast(error.message, true))));
  $$(".approve-vault-change", host).forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "Applying…";
    try { await api(`/api/v1/admin/vault/changes/${button.dataset.id}/approve`, { method: "POST" }); toast("Vault change applied."); await reload(); }
    catch (error) { toast(error.message, true); button.disabled = false; button.textContent = "Apply"; }
  }));
  $$(".reject-vault-change", host).forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try { await api(`/api/v1/admin/vault/changes/${button.dataset.id}/reject`, { method: "POST" }); toast("Vault recommendation disregarded."); await reload(); }
    catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  $("#apply-all-vault-changes").addEventListener("click", async (event) => {
    if (!window.confirm(`Apply all ${data.items.length} currently displayed vault recommendations?`)) return;
    event.currentTarget.disabled = true;
    for (const change of data.items) {
      try { await api(`/api/v1/admin/vault/changes/${change.id}/approve`, { method: "POST" }); }
      catch (error) { toast(`${change.path}: ${error.message}`, true); break; }
    }
    await reload();
  });
  $("#reject-all-vault-changes").addEventListener("click", async (event) => {
    if (!window.confirm(`Disregard all ${data.items.length} currently displayed recommendations?`)) return;
    event.currentTarget.disabled = true;
    await Promise.all(data.items.map((change) => api(`/api/v1/admin/vault/changes/${change.id}/reject`, { method: "POST" })));
    await reload();
  });
}

async function renderDocuments(reviewOnly) {
  const title = reviewOnly ? "Review queue" : "Knowledge documents";
  const titleHelp = reviewOnly
    ? "Low-confidence classifications wait here until you approve them. Approval confirms the current category and tags."
    : "A searchable record of every scanned source file, its generated note, tags, and source-to-vault state.";
  $("#content").innerHTML = `<div class="section-head"><div><h2>${headingWithHelp(title, titleHelp)}</h2><p>${reviewOnly ? "Review document decisions and whole-vault maintenance recommendations." : "Every source file and how it compares with Obsidian. This page updates live."}</p></div></div><div class="toolbar"><div class="search field">${labelWithHelp("doc-search", "Search documents", "Searches generated titles, source paths, extracted summaries, and tags.")}<input id="doc-search" value="${escapeHtml(state.documentsQuery.search)}" placeholder="Search title, source, or summary…"></div><div class="field status-filter">${labelWithHelp("doc-status", "Comparison status", "Filters files by whether they match Obsidian, changed, are new, duplicated, or missing.")}<select id="doc-status"><option value="">All comparisons</option><option value="in-sync">In Obsidian</option><option value="modified">Modified</option><option value="new">New</option><option value="possible-duplicate">Possible duplicate</option><option value="vault-missing">Missing from Obsidian</option><option value="source-missing">Source missing</option></select></div></div><div id="doc-results"><div class="skeleton"></div></div>${reviewOnly ? '<div id="vault-review-results"><div class="skeleton"></div></div>' : ""}`;
  $("#doc-status").value = state.documentsQuery.comparison;
  const load = async () => {
    state.documentsQuery = { search: $("#doc-search").value, comparison: $("#doc-status").value };
    const query = new URLSearchParams({ search: $("#doc-search").value, comparison_status: $("#doc-status").value, limit: "200" });
    if (reviewOnly) query.set("review", "true");
    const data = await api(`/api/v1/admin/documents?${query}`);
    const byId = new Map(data.items.map((item) => [item.id, item]));
    const rows = data.items.map((doc) => {
      const reviewActions = reviewOnly ? `<div class="document-actions">${doc.status === "ai-stopped" ? "" : `<button class="secondary approve" data-id="${doc.id}" title="${doc.comparison_status === "possible-duplicate" ? "Adopt and update the matched existing note" : "Accept the current AI organization"}">Approve</button>`}<button class="quiet redo-review" data-id="${doc.id}">Redo AI review</button><button class="danger disregard-review" data-id="${doc.id}">Disregard</button>${doc.comparison_status === "possible-duplicate" ? `<button class="quiet allow-duplicate" data-id="${doc.id}">Create separate note</button>` : ""}</div>` : "";
      return `<tr>
        <td data-label="Document"><div class="doc-title">${escapeHtml(doc.title || doc.source_name)}</div><div class="doc-path" title="${escapeHtml(doc.source_path)}">${escapeHtml(doc.source_path)}</div>${doc.summary ? `<div class="doc-summary">${escapeHtml(doc.summary)}</div>` : ""}</td>
        <td data-label="Source"><strong>${escapeHtml(doc.agent_name)}</strong><small>${escapeHtml(doc.root_name)}</small><div class="doc-tags">${(doc.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div></td>
        <td data-label="State">${comparisonBadge(doc.comparison_status)}<small>${escapeHtml(doc.status)} · ${Math.round((doc.confidence || 0) * 100)}% confidence</small>${doc.duplicate_path ? `<small class="duplicate-match">Possible match: ${escapeHtml(doc.duplicate_title || doc.duplicate_path)}</small>` : ""}${(doc.match_evidence || []).length ? `<small>${escapeHtml(doc.match_evidence.slice(0, 3).join(" · "))}</small>` : ""}</td>
        <td data-label="Actions">${reviewActions}${doc.status === "error" ? `<button class="quiet retry" data-id="${doc.id}">Retry sync</button>` : ""}${doc.status === "ignored" && !reviewOnly ? `<button class="quiet redo-review" data-id="${doc.id}">Redo AI review</button>` : ""}</td>
      </tr>`;
    }).join("");
    $("#doc-results").innerHTML = rows ? `<div class="table-wrap document-table-panel" data-preserve-scroll="true" data-scroll-key="documents-table"><table class="document-table"><colgroup><col class="document-column"><col class="source-column"><col class="state-column"><col class="actions-column"></colgroup><thead><tr><th>${headingWithHelp("Document", "The generated title, original path, and summary.")}</th><th>${headingWithHelp("Source and tags", "The source computer, watched folder, and assigned tags.")}</th><th>${headingWithHelp("State", "The source-to-vault comparison, processing state, confidence, and possible match.")}</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="panel empty"><div class="empty-icon">◇</div><p>Nothing here.</p></div>';
    $$(".approve").forEach((button) => button.addEventListener("click", async () => {
      await api(`/api/v1/admin/documents/${button.dataset.id}/approve`, { method: "POST" });
      toast("Classification approved.");
      await Promise.all([load(), refreshReviewBadge()]);
    }));
    $$(".disregard-review").forEach((button) => button.addEventListener("click", () => openDisregardReview(byId.get(button.dataset.id), async () => Promise.all([load(), refreshReviewBadge()]))));
    $$(".redo-review").forEach((button) => button.addEventListener("click", () => openRedoReview(byId.get(button.dataset.id), async () => Promise.all([load(), refreshReviewBadge()]))));
    $$(".retry").forEach((button) => button.addEventListener("click", async () => {
      await api(`/api/v1/admin/documents/${button.dataset.id}/retry`, { method: "POST" }); toast("Retry queued on the source device.");
    }));
    $$(".allow-duplicate").forEach((button) => button.addEventListener("click", async () => {
      await api(`/api/v1/admin/documents/${button.dataset.id}/allow-duplicate`, { method: "POST" });
      toast("A separate note is now allowed and queued.");
      await Promise.all([load(), refreshReviewBadge()]);
    }));
    if (reviewOnly) await renderVaultRecommendations();
  };
  let debounce;
  $("#doc-search").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(load, 250); });
  $("#doc-status").addEventListener("change", load);
  await load();
}

async function renderLegacySettings() {
  const [settings, agentData] = await Promise.all([
    api("/api/v1/admin/settings"), api("/api/v1/admin/agents"),
  ]);
  state.agents = agentData.items;
  const enabled = settings.llm_enabled === "true";
  const vaultMode = settings.vault_mode || "local";
  const vaultOptions = state.agents.map((agent) => `<option value="${agent.id}" ${agent.id === settings.vault_agent_id ? "selected" : ""}>${escapeHtml(agent.name)} — ${agent.status}${agent.vault_ready ? ` — ${escapeHtml(agent.vault_path)}` : ""}</option>`).join("");
  const hostVault = settings.vault_host_path || settings.vault_path;
  $("#content").innerHTML = `<form class="settings-layout" id="settings-form">
    <section class="settings-card"><h3>${headingWithHelp("Obsidian vault", "The one vault where Obsync creates and updates managed Markdown notes.")}</h3><p>Choose where Obsync writes generated Markdown. The server mount is the default; a paired desktop can write directly to a vault on Windows or another computer.</p>
      <div class="vault-mode-grid">
        <label class="vault-choice"><input type="radio" name="vault-mode" value="local" ${vaultMode === "local" ? "checked" : ""}><span><strong>Server-mounted vault</strong><small>Best when the vault is mounted into Docker or Obsync runs natively beside it.</small></span>${helpIcon("The Docker or native server writes directly to a folder it can access.", "About a server-mounted vault")}</label>
        <label class="vault-choice"><input type="radio" name="vault-mode" value="agent" ${vaultMode === "agent" ? "checked" : ""}><span><strong>Vault on a desktop</strong><small>Best when the vault is in Documents on Windows while the server runs elsewhere.</small></span>${helpIcon("Obsync Desktop performs safe vault reads and writes on the selected computer.", "About a desktop vault")}</label>
      </div>
      <div id="local-vault-settings">
        <div class="field">${labelWithHelp("", settings.runtime === "docker" ? "Host vault folder" : "Vault folder", "The host folder currently mapped to the Obsync vault. Docker mounts are changed in Compose, not from a web browser.")}<input value="${escapeHtml(hostVault)}" disabled><small>${settings.runtime === "docker" ? `Inside Docker this is mounted as ${escapeHtml(settings.vault_path)}. Docker mounts can only be changed when the container is created.` : "This native Obsync process writes directly to this folder."}</small></div>
      </div>
      <div id="agent-vault-settings" hidden>
        <div class="field">${labelWithHelp("vault-agent", "Computer containing the vault", "Choose the online Obsync Desktop app that can access the real Obsidian vault folder.")}<select id="vault-agent"><option value="">${state.agents.length ? "Choose a paired computer…" : "No desktop computers paired"}</option>${vaultOptions}</select><small>The Overview computer count includes the Obsync server. A Windows PC appears here only after Obsync Desktop is connected.</small></div>
        <div class="settings-actions"><button class="secondary" type="button" id="browse-vault" title="Open a native folder browser on the selected computer">Browse for vault on that computer</button><button class="quiet" type="button" id="add-vault-computer" title="Connect another computer">+ Add computer</button></div>
        <p class="inline-status" id="vault-agent-status">${state.agents.length ? "Choose a paired computer." : "The Obsync server is connected, but no desktop computer is paired yet."}</p>
      </div>
    </section>
    <section class="settings-card"><h3>${headingWithHelp("Local AI organization", "Optional local model settings used to title, summarize, categorize, tag, and link extracted content.")}</h3><p>Use Ollama, LM Studio, or another OpenAI-compatible local server. Obsync keeps syncing with deterministic rules if the model is offline.</p>
      <div class="field-grid">
        <label class="check-row full-width"><input id="llm-enabled" type="checkbox" ${enabled ? "checked" : ""}> Enable AI classification ${helpIcon("When enabled, Obsync asks the selected local model to organize extracted content. Syncing still works if the model is offline.", "About AI classification")}</label>
        <div class="field">${labelWithHelp("llm-provider", "Provider", "Select the API format exposed by Ollama, LM Studio, or another compatible local model server.")}<select id="llm-provider"><option value="ollama">Ollama</option><option value="lmstudio">LM Studio</option><option value="openai-compatible">OpenAI-compatible</option></select></div>
        <div class="field">${labelWithHelp("llm-model", "Model", "The exact model identifier reported by the provider. Check connection can suggest a discovered model.")}<input id="llm-model" value="${escapeHtml(settings.llm_model)}" placeholder="qwen3:8b"></div>
        <div class="field full-width">${labelWithHelp("llm-url", "Base URL", "The network address of the local model server as seen by Obsync. Docker often reaches the host through host.docker.internal.")}<input id="llm-url" value="${escapeHtml(settings.llm_base_url)}" placeholder="http://host.docker.internal:11434"><small>From Docker, host.docker.internal usually reaches an LLM running on the host.</small></div>
        <div class="field full-width">${labelWithHelp("llm-key", "API key (optional)", "Only required when the selected local or compatible model server requires authentication.")}<input id="llm-key" type="password" value="" placeholder="${settings.llm_api_key === "configured" ? "Configured — leave blank to keep" : "Not required for default Ollama/LM Studio"}"></div>
        <div class="field">${labelWithHelp("review-threshold", "Review below", "Classifications below this confidence score enter the Review queue. 0.65 means 65 percent.")}<input id="review-threshold" type="number" min="0" max="1" step="0.05" value="${escapeHtml(settings.review_threshold || ".65")}"><small>0.65 means below 65% confidence.</small></div>
        <div class="field">${labelWithHelp("llm-timeout", "Model timeout (seconds)", "The maximum time allowed for real classification. The quick connection check uses a separate 15-second limit.")}<input id="llm-timeout" type="number" min="5" max="600" value="${escapeHtml(settings.llm_timeout_seconds || "120")}"></div>
      </div>
      <div class="settings-actions"><button class="primary" type="submit" title="Save vault and AI settings">Save settings</button><button class="secondary" type="button" id="test-llm" title="Quickly list available models without running inference">Check connection</button></div>
      <p class="inline-status" id="llm-test-status" hidden></p>
    </section>
    <section class="settings-card"><h3>${headingWithHelp("Safety defaults", "Non-destructive rules that protect source files and preserve your manual Obsidian writing.")}</h3><p>Obsync never edits, moves, or deletes source files. Missing sources are marked in their note and kept. Manual text below “My notes” is preserved on every update.</p></section>
  </form>`;
  $("#llm-provider").value = settings.llm_provider || "ollama";
  const updateVaultMode = () => {
    const mode = $('input[name="vault-mode"]:checked').value;
    $("#local-vault-settings").hidden = mode !== "local";
    $("#agent-vault-settings").hidden = mode !== "agent";
  };
  $$('input[name="vault-mode"]').forEach((input) => input.addEventListener("change", updateVaultMode));
  updateVaultMode();
  $("#vault-agent").addEventListener("change", () => {
    const agent = state.agents.find((item) => item.id === $("#vault-agent").value);
    $("#vault-agent-status").className = `inline-status ${agent?.vault_ready ? "good" : ""}`;
    $("#vault-agent-status").textContent = agent?.vault_ready
      ? `Ready: ${agent.vault_path}`
      : agent ? "No vault selected on this computer yet." : "Choose a paired computer.";
  });
  $("#vault-agent").dispatchEvent(new Event("change"));
  $("#browse-vault").addEventListener("click", async () => {
    const agentId = $("#vault-agent").value;
    if (!agentId) { toast("Choose a computer first.", true); return; }
    try {
      await api(`/api/v1/admin/agents/${agentId}/select-vault`, { method: "POST" });
      $("#vault-agent-status").className = "inline-status";
      $("#vault-agent-status").textContent = "Folder browser requested. Choose the vault on that computer, then refresh Settings.";
      toast("Folder browser sent to the desktop.");
    } catch (error) { toast(error.message, true); }
  });
  $("#add-vault-computer").addEventListener("click", openEnrollment);
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await api("/api/v1/admin/settings", { method: "PUT", body: settingsPayload() }); toast("Settings saved."); }
    catch (error) { toast(error.message, true); }
  });
  $("#test-llm").addEventListener("click", async () => {
    const button = $("#test-llm"); button.disabled = true; button.textContent = "Testing…";
    const status = $("#llm-test-status"); status.hidden = false; status.className = "inline-status"; status.textContent = "Checking the model server…";
    try {
      const result = await api("/api/v1/admin/settings/test-llm", { method: "POST", body: settingsPayload() });
      status.className = `inline-status ${result.ok ? "good" : "bad"}`;
      status.textContent = result.message;
      if (!$("#llm-model").value && result.suggested_model) $("#llm-model").value = result.suggested_model;
    } catch (error) { status.className = "inline-status bad"; status.textContent = error.message; }
    finally { button.disabled = false; button.textContent = "Check connection"; }
  });
}

function sweepScheduleFields(prefix, settings) {
  const settingPrefix = prefix.replaceAll("-", "_");
  const enabled = settings[`${settingPrefix}_schedule_enabled`] === "true";
  const scheduleLabel = prefix.includes("index") ? "Schedule Index Sweep" : "Schedule Maintenance Sweep";
  return `<div class="sweep-schedule">
    <label class="check-row"><input id="${prefix}-schedule-enabled" type="checkbox" ${enabled ? "checked" : ""}> ${scheduleLabel}</label>
    <div class="field-grid compact-grid">
      <div class="field"><label for="${prefix}-frequency">Frequency</label><select id="${prefix}-frequency"><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option><option value="custom">Custom interval</option></select></div>
      <div class="field"><label for="${prefix}-time">Start time</label><input id="${prefix}-time" type="time" value="${escapeHtml(settings[`${settingPrefix}_schedule_time`] || "02:00")}"></div>
      <div class="field"><label for="${prefix}-interval">Custom hours</label><input id="${prefix}-interval" type="number" min="1" max="8760" value="${escapeHtml(settings[`${settingPrefix}_schedule_interval_hours`] || "24")}"></div>
      <div class="field"><label for="${prefix}-weekday">Weekly day</label><select id="${prefix}-weekday"><option value="0">Monday</option><option value="1">Tuesday</option><option value="2">Wednesday</option><option value="3">Thursday</option><option value="4">Friday</option><option value="5">Saturday</option><option value="6">Sunday</option></select></div>
      <div class="field"><label for="${prefix}-month-day">Monthly day</label><input id="${prefix}-month-day" type="number" min="1" max="28" value="${escapeHtml(settings[`${settingPrefix}_schedule_month_day`] || "1")}"></div>
    </div>
  </div>`;
}

function sweepProgress(active, type) {
  if (!active || active.sweep_type !== type) return "";
  const percent = active.total_notes ? Math.round((active.processed_notes / active.total_notes) * 100) : 0;
  return `<div class="sweep-progress"><div><strong>${escapeHtml(active.status)}</strong><span>${active.processed_notes} / ${active.total_notes || "?"} notes · ${percent}%</span></div><progress max="100" value="${percent}"></progress><small>${escapeHtml(active.current_note || "Preparing vault sweep…")}</small><button class="danger stop-sweep" data-id="${active.id}" type="button">Stop Sweep</button></div>`;
}

function sweepFailure(recent, type) {
  const latest = (recent || []).find((item) => item.sweep_type === type);
  if (!latest || latest.status !== "failed") return "";
  return `<div class="sweep-failure" role="alert"><strong>Last ${escapeHtml(type)} sweep failed</strong><p>${escapeHtml(latest.error || "The sweep stopped before it could complete.")}</p><small>${latest.processed_notes} of ${latest.total_notes || "?"} notes were inspected. No unapproved changes were applied.</small></div>`;
}

async function renderVault() {
  const [settings, agentData, sweeps] = await Promise.all([
    api("/api/v1/admin/settings"), api("/api/v1/admin/agents"), api("/api/v1/admin/vault/sweeps"),
  ]);
  state.agents = agentData.items;
  const vaultMode = settings.vault_mode || "local";
  const confirmed = settings.vault_confirmed === "true";
  const vaultOptions = state.agents.map((agent) => `<option value="${agent.id}" ${agent.id === settings.vault_agent_id ? "selected" : ""}>${escapeHtml(agent.name)} — ${agent.status}${agent.vault_ready ? ` — ${escapeHtml(agent.vault_path)}` : ""}</option>`).join("");
  const hostVault = settings.vault_host_path || settings.vault_path;
  const vaultModel = sweeps.model || { status: "not-learned", model: {} };
  const learnedModel = vaultModel.model || {};
  const learnedPatterns = (learnedModel.note_patterns || []).slice(0, 8);
  const recentSweeps = (sweeps.recent || []).slice(0, 6).map((item) => `<tr><td>${escapeHtml(item.sweep_type)}</td><td><strong>${escapeHtml(item.status)}</strong>${item.error ? `<small class="sweep-row-error">${escapeHtml(item.error)}</small>` : ""}</td><td>${item.processed_notes} / ${item.total_notes}</td><td>${item.recommendations}</td><td>${item.applied_changes}</td><td>${relativeTime(item.finished_at || item.created_at)}</td><td>${item.applied_changes ? `<button class="quiet undo-sweep" type="button" data-id="${item.id}">Undo</button>` : "—"}</td></tr>`).join("");
  $("#content").innerHTML = `
    <section class="vault-choice-hero ${confirmed ? "confirmed" : "required"}">
      <span class="eyebrow">REQUIRED DESTINATION</span>
      <h2>Please choose which Obsidian Vault your files will be synced to</h2>
      <p>Obsync will not write notes until you confirm this choice. Choose the vault folder itself—never the hidden <code>.obsidian</code> settings folder.</p>
    </section>
    <form class="settings-layout" id="vault-settings-form">
      <section class="settings-card"><h3>${headingWithHelp("Choose the vault location", "Exactly one destination is active at a time.")}</h3>
        <div class="vault-mode-grid">
          <label class="vault-choice"><input type="radio" name="vault-mode" value="local" ${vaultMode === "local" ? "checked" : ""}><span><strong>Vault mounted into the server</strong><small>Use only when this displayed folder is your real vault.</small></span></label>
          <label class="vault-choice"><input type="radio" name="vault-mode" value="agent" ${vaultMode === "agent" ? "checked" : ""}><span><strong>Vault on a desktop computer</strong><small>Recommended when the vault is in Windows Documents.</small></span></label>
        </div>
        <div id="local-vault-settings">
          <div class="field">${labelWithHelp("", "Mounted vault folder", "This path was fixed when the server container was created.")}<input value="${escapeHtml(hostVault)}" disabled><small>Confirm this only if it is the real Obsidian vault you intend to use.</small></div>
        </div>
        <div id="agent-vault-settings" hidden>
          <div class="field">${labelWithHelp("vault-agent", "Computer containing the vault", "Choose the computer running Obsync Desktop that can access your real vault.")}<select id="vault-agent"><option value="">${state.agents.length ? "Choose a computer…" : "No desktop computers paired"}</option>${vaultOptions}</select></div>
          <div class="settings-actions"><button class="secondary" type="button" id="browse-vault">Choose vault folder on that computer</button><button class="quiet" type="button" id="add-vault-computer">+ Add computer</button></div>
          <p class="inline-status" id="vault-agent-status"></p>
        </div>
        <div class="settings-actions"><button class="primary" type="submit">Save and confirm this vault</button></div>
        <p class="inline-status ${confirmed ? "good" : "bad"}">${confirmed ? "A vault choice is saved. Review the exact path above before syncing." : "No vault choice has been confirmed yet."}</p>
      </section>
      <section class="settings-card full-width sweep-overview"><div class="section-head"><div><span class="eyebrow">WHOLE-VAULT INTELLIGENCE</span><h3>Vault index</h3><p>Vault cache: <strong>${sweeps.indexed_notes}</strong> Markdown notes. Last successful Index Sweep: <strong>${sweeps.last_indexed_at ? relativeTime(sweeps.last_indexed_at) : "Never"}</strong>.</p></div><div class="settings-actions compact"><button class="secondary start-sweep" data-type="index" type="button">Start Index Sweep</button><button class="quiet start-sweep" data-type="index" data-full="true" type="button">Rebuild Whole Index</button></div></div>
        ${sweepProgress(sweeps.active, "index")}
        ${sweepFailure(sweeps.recent, "index")}
        <div class="field"><label for="vault-index-change-mode">Index sweep changes</label><select id="vault-index-change-mode"><option value="index-only">Index only — do not recommend changes</option><option value="review">Send all recommended changes to Review</option><option value="auto">Allow AI Agent to apply all recommended changes</option></select><small class="danger-copy">Automatic mode can change existing entries without human approval. Every change is logged and can be undone.</small></div>
        ${sweepScheduleFields("vault-index", settings)}
      </section>
      <section class="settings-card full-width sweep-overview"><div class="section-head"><div><span class="eyebrow">ACTIVE VAULT CARE</span><h3>Maintenance Sweep</h3><p>Uses Local AI and this vault's learned organization model to recommend only specific, evidence-backed relationships and tags. Retrieval similarity alone can never create a link.</p></div><div class="settings-actions compact"><button class="secondary start-sweep" data-type="maintenance" type="button">Start Maintenance Sweep</button></div></div>
        ${sweepProgress(sweeps.active, "maintenance")}
        ${sweepFailure(sweeps.recent, "maintenance")}
        <div class="field"><label for="vault-maintenance-change-mode">Maintenance sweep changes</label><select id="vault-maintenance-change-mode"><option value="review">Send all recommended changes to Review</option><option value="auto">Allow AI Agent to apply all recommended changes</option></select><small class="danger-copy">Warning: automatic mode may modify existing notes, links, tags, and organization without human approval.</small></div>
        <div class="behavior-grid maintenance-categories">
          ${[["links", "Relationship links and backlinks"], ["tags", "Shared relationship tags"]].map(([value, label]) => `<label class="check-row"><input class="maintenance-category" type="checkbox" value="${value}"> ${label}</label>`).join("")}
        </div>
        ${sweepScheduleFields("vault-maintenance", settings)}
      </section>
      <section class="settings-card full-width"><h3>Adaptive vault model</h3><p><strong>${escapeHtml(vaultModel.status || "not-learned")}</strong>${vaultModel.learned_at ? ` · learned ${relativeTime(vaultModel.learned_at)}` : ""}${vaultModel.model_name ? ` · ${escapeHtml(vaultModel.model_name)}` : ""}</p><p>${escapeHtml(learnedModel.vault_summary || "The model is learned during the first AI-assisted sweep and refreshed when vault content or review feedback changes. No business categories are hardcoded.")}</p>${learnedPatterns.length ? `<ul class="plain-list">${learnedPatterns.map((pattern) => `<li><strong>${escapeHtml(pattern.name)}</strong> — ${escapeHtml((pattern.signals || []).join("; "))}</li>`).join("")}</ul>` : ""}${vaultModel.error ? `<p class="danger-copy">${escapeHtml(vaultModel.error)}</p>` : ""}</section>
      <section class="settings-card full-width"><h3>Whole-vault matching and links</h3><div class="field-grid compact-grid"><div class="field"><label for="existing-note-policy">Strong existing-note matches</label><select id="existing-note-policy"><option value="review">Send first adoption to Review</option><option value="auto">Adopt strong matches automatically</option></select><small>Exact source/content matches update automatically. Ambiguous matches always require review.</small></div><div class="field"><label for="vault-candidate-limit">Candidate notes per AI decision</label><input id="vault-candidate-limit" type="number" min="5" max="50" value="${escapeHtml(settings.vault_relationship_candidate_limit || "20")}"><small>Retrieval only shortlists candidates; it never creates links.</small></div><div class="field"><label for="vault-relationship-confidence">Minimum AI relationship confidence</label><input id="vault-relationship-confidence" type="number" min="0.5" max="1" step="0.01" value="${escapeHtml(settings.vault_relationship_min_confidence || "0.72")}"><small>Every accepted link also requires specific source and target evidence.</small></div><div class="field"><label for="vault-link-limit">Maximum evidence-backed links per note</label><input id="vault-link-limit" type="number" min="1" max="50" value="${escapeHtml(settings.vault_link_limit || "20")}"><small>A safety ceiling, not a target.</small></div><div class="field"><label for="vault-timezone">Schedule timezone</label><input id="vault-timezone" value="${escapeHtml(settings.vault_schedule_timezone || "America/New_York")}"></div></div></section>
      <section class="settings-card full-width"><h3>Sweep history and rollback</h3>${recentSweeps ? `<div class="table-wrap sweep-history"><table><thead><tr><th>Type</th><th>Status</th><th>Notes</th><th>Recommendations</th><th>Applied</th><th>Finished</th><th>Rollback</th></tr></thead><tbody>${recentSweeps}</tbody></table></div>` : '<p class="inline-status">No vault sweeps have run yet.</p>'}</section>
      <section class="settings-card"><h3>Vault safety</h3><p>Obsync never writes inside <code>.obsidian</code>. Document adoption preserves the entire prior note outside the managed section. Sweeps use expected hashes, stop on concurrent edits, keep before/after content, and never auto-delete or auto-merge notes.</p></section>
    </form>`;
  const updateVaultMode = () => {
    const mode = $('input[name="vault-mode"]:checked').value;
    $("#local-vault-settings").hidden = mode !== "local";
    $("#agent-vault-settings").hidden = mode !== "agent";
  };
  $$('input[name="vault-mode"]').forEach((input) => input.addEventListener("change", updateVaultMode));
  updateVaultMode();
  const updateAgentStatus = () => {
    const agent = state.agents.find((item) => item.id === $("#vault-agent").value);
    const badPath = agent?.vault_path?.replaceAll("\\", "/").toLowerCase().endsWith("/.obsidian");
    $("#vault-agent-status").className = `inline-status ${agent?.vault_ready && !badPath ? "good" : "bad"}`;
    $("#vault-agent-status").textContent = badPath
      ? "Wrong folder selected: choose the parent vault folder, not .obsidian."
      : agent?.vault_ready ? `Ready: ${agent.vault_path}`
        : agent ? "No valid vault selected on this computer yet." : "Choose a computer.";
  };
  $("#vault-agent").addEventListener("change", updateAgentStatus);
  updateAgentStatus();
  $("#vault-index-change-mode").value = settings.vault_index_change_mode || "index-only";
  $("#vault-maintenance-change-mode").value = settings.vault_maintenance_change_mode || "review";
  $("#existing-note-policy").value = settings.existing_note_policy || "review";
  ["vault-index", "vault-maintenance"].forEach((prefix) => {
    $(`#${prefix}-frequency`).value = settings[`${prefix.replaceAll("-", "_")}_schedule_frequency`] || "weekly";
    $(`#${prefix}-weekday`).value = settings[`${prefix.replaceAll("-", "_")}_schedule_weekday`] || "6";
  });
  let maintenanceCategories = [];
  try { maintenanceCategories = JSON.parse(settings.vault_maintenance_categories || "[]"); } catch (_error) { maintenanceCategories = ["links", "tags"]; }
  $$(".maintenance-category").forEach((input) => { input.checked = maintenanceCategories.includes(input.value); });
  $$(".start-sweep").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const type = button.dataset.type;
      const changeMode = type === "index" ? $("#vault-index-change-mode").value : $("#vault-maintenance-change-mode").value;
      await api(`/api/v1/admin/vault/sweeps/${type}/start`, { method: "POST", body: { change_mode: changeMode, full_rebuild: button.dataset.full === "true" } });
      toast(`${type === "index" ? "Index" : "Maintenance"} Sweep started.`);
      await navigate("vault", { silent: true });
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  $$(".stop-sweep").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/v1/admin/vault/sweeps/${button.dataset.id}/stop`, { method: "POST" });
      toast("Vault sweep is stopping safely.");
      await navigate("vault", { silent: true });
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  $$(".undo-sweep").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Undo every still-current note change applied by this sweep?")) return;
    button.disabled = true;
    try {
      const result = await api(`/api/v1/admin/vault/sweeps/${button.dataset.id}/undo`, { method: "POST" });
      toast(`Reverted ${result.reverted} sweep change(s).`, !result.ok);
      await navigate("vault", { silent: true });
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  $("#browse-vault").addEventListener("click", async () => {
    const agentId = $("#vault-agent").value;
    if (!agentId) { toast("Choose a computer first.", true); return; }
    const button = $("#browse-vault");
    button.disabled = true;
    try {
      const command = await api(`/api/v1/admin/agents/${agentId}/select-vault`, { method: "POST" });
      $("#vault-agent-status").className = "inline-status";
      $("#vault-agent-status").textContent = "Choose the vault folder in the window on that computer…";
      await pollCommand(command.id, "Waiting for the vault folder selection…");
      toast("Vault folder selected. Confirm the exact path, then save it.");
      await navigate("vault", { silent: true });
    } catch (error) { toast(error.message, true); button.disabled = false; }
  });
  $("#add-vault-computer").addEventListener("click", openEnrollment);
  $("#vault-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/v1/admin/settings", { method: "PUT", body: vaultSettingsPayload() });
      toast("Obsidian Vault confirmed.");
      await navigate("vault", { silent: true });
    } catch (error) { toast(error.message, true); }
  });
}

function aiSessions(activity) {
  return activity.active?.length ? activity.active : activity.last ? [activity.last] : [];
}

function aiSessionIsActive(activity, documentId) {
  return Boolean((activity.active || []).find((item) => item.document_id === documentId));
}

function aiEventLabel(kind) {
  if (kind === "reasoning") return "MODEL THINKING";
  if (kind === "output") return "MODEL OUTPUT";
  return String(kind || "stage").toUpperCase();
}

function aiTraceEvents(session) {
  return session.events?.length
    ? session.events
    : [{ kind: "stage", message: "Waiting for model activity…" }];
}

function aiTraceMarkup(session) {
  return aiTraceEvents(session).map((event) => `<div class="ai-trace-event ${escapeHtml(event.kind)}"><span>${escapeHtml(aiEventLabel(event.kind))}</span><pre>${escapeHtml(event.message)}</pre></div>`).join("");
}

function aiSessionHeadMarkup(session, isActive) {
  return `<div><span class="eyebrow">${isActive ? "LIVE AI SESSION" : "LAST AI SESSION"}</span><h3>${escapeHtml(session.source_name)}</h3><p title="${escapeHtml(session.source_path)}">${escapeHtml(session.agent_name)} · ${escapeHtml(session.root_name)} · ${escapeHtml(session.source_path)}</p></div><span class="status-pill ${isActive ? "" : "offline"}">${isActive ? "working" : escapeHtml(session.outcome || "finished")}</span>`;
}

function aiSessionMetaMarkup(session, activity) {
  return `<div><small>Stage</small><strong>${escapeHtml(session.phase_label || session.phase || "Finished")}</strong></div><div><small>AI profile</small><strong>${escapeHtml(session.profile_name || "Full document transfer")}</strong></div><div><small>Model</small><strong>${escapeHtml(session.provider || activity.provider)}${session.model || activity.model ? ` · ${escapeHtml(session.model || activity.model)}` : ""}</strong></div><div><small>Elapsed</small><strong>${session.elapsed_seconds || 0}s</strong></div>`;
}

function aiSessionActionsMarkup(session, isActive) {
  return `<small>This is a read-only view of model-emitted activity and Obsync's processing stages.</small>${isActive && session.cancellable ? `<button class="danger stop-inference" type="button" data-document="${escapeHtml(session.document_id)}">Stop inference</button>` : ""}`;
}

function aiActivityMarkup(activity) {
  const sessions = aiSessions(activity);
  if (!sessions.length) {
    return `<section class="settings-card ai-live-card"><div class="ai-live-head"><div><span class="eyebrow">LIVE AI SESSION</span><h3>Local AI is idle</h3></div><span class="status-pill offline">idle</span></div><div class="ai-idle"><span>✦</span><p>No model inference is active. Start a folder sync or request a re-review to see the file, model activity, streamed response, and decision here.</p></div></section>`;
  }
  return sessions.map((session) => {
    const isActive = aiSessionIsActive(activity, session.document_id);
    return `<section class="settings-card ai-live-card ${isActive ? "active" : "completed"}" data-ai-session-id="${escapeHtml(session.document_id)}">
      <div class="ai-live-head" data-ai-session-head>${aiSessionHeadMarkup(session, isActive)}</div>
      <div class="ai-session-meta" data-ai-session-meta>${aiSessionMetaMarkup(session, activity)}</div>
      <div class="ai-trace-shell">
        <div class="ai-trace" data-document="${escapeHtml(session.document_id)}" data-preserve-scroll="true" data-scroll-key="ai-trace-${escapeHtml(session.document_id)}" aria-live="polite" role="log">${aiTraceMarkup(session)}</div>
        <button class="ai-jump-latest" type="button" data-document="${escapeHtml(session.document_id)}" aria-label="Jump to latest AI activity" title="Jump to latest AI activity" hidden>↓</button>
      </div>
      <div class="ai-live-actions" data-ai-session-actions>${aiSessionActionsMarkup(session, isActive)}</div>
    </section>`;
  }).join("");
}

function updateAiJumpButton(documentId) {
  const host = $("#ai-live-host");
  if (!host) return;
  $$(".ai-jump-latest", host).forEach((button) => {
    if (button.dataset.document === documentId) button.hidden = state.aiFollow.get(documentId) !== false;
  });
}

function setAiFollow(documentId, follow) {
  state.aiFollow.set(documentId, follow);
  updateAiJumpButton(documentId);
}

function scrollAiTraceToLatest(trace, documentId, behavior = "auto") {
  setAiFollow(documentId, true);
  trace.dataset.autoScrolling = behavior;
  trace.scrollTo({ top: trace.scrollHeight, behavior });
  clearTimeout(state.aiScrollTimers.get(documentId));
  const delay = behavior === "smooth" ? 450 : 80;
  state.aiScrollTimers.set(documentId, setTimeout(() => {
    if (state.aiFollow.get(documentId) !== false) trace.scrollTop = trace.scrollHeight;
    trace.dataset.autoScrolling = "false";
    state.aiScrollPositions.set(documentId, trace.scrollTop);
    state.aiScrollTimers.delete(documentId);
  }, delay));
}

function wireAiTrace(trace) {
  if (trace.dataset.scrollWired === "true") return;
  trace.dataset.scrollWired = "true";
  const documentId = trace.dataset.document;
  trace.addEventListener("wheel", (event) => {
    if (event.deltaY < 0) {
      trace.dataset.autoScrolling = "false";
      setAiFollow(documentId, false);
    }
  }, { passive: true });
  trace.addEventListener("scroll", () => {
    state.aiScrollPositions.set(documentId, trace.scrollTop);
    const atLatest = trace.scrollHeight - trace.scrollTop - trace.clientHeight <= 8;
    if (trace.dataset.autoScrolling === "smooth") return;
    if (trace.dataset.autoScrolling === "auto" && atLatest) return;
    trace.dataset.autoScrolling = "false";
    setAiFollow(documentId, atLatest);
  }, { passive: true });
}

function initializeAiActivityPanels() {
  const host = $("#ai-live-host");
  if (!host) return;
  $$(".ai-trace", host).forEach((trace) => {
    const documentId = trace.dataset.document;
    wireAiTrace(trace);
    if (!state.aiFollow.has(documentId)) state.aiFollow.set(documentId, true);
    if (state.aiFollow.get(documentId) !== false) {
      requestAnimationFrame(() => scrollAiTraceToLatest(trace, documentId));
    } else {
      trace.scrollTop = state.aiScrollPositions.get(documentId) || 0;
      updateAiJumpButton(documentId);
    }
  });
}

function updateAiTrace(trace, session) {
  const documentId = session.document_id;
  const follow = state.aiFollow.get(documentId) !== false;
  const scrollTop = trace.scrollTop;
  const events = aiTraceEvents(session);
  const rows = [...trace.children];
  events.forEach((event, index) => {
    let row = rows[index];
    if (!row) {
      row = document.createElement("div");
      row.innerHTML = "<span></span><pre></pre>";
      trace.append(row);
    }
    row.className = `ai-trace-event ${event.kind || "stage"}`;
    $("span", row).textContent = aiEventLabel(event.kind);
    $("pre", row).textContent = event.message || "";
  });
  rows.slice(events.length).forEach((row) => row.remove());
  if (follow) requestAnimationFrame(() => scrollAiTraceToLatest(trace, documentId));
  else {
    trace.scrollTop = scrollTop;
    state.aiScrollPositions.set(documentId, scrollTop);
    updateAiJumpButton(documentId);
  }
}

function pruneAiPanelState(documentIds) {
  const visible = new Set(documentIds);
  [...state.aiFollow.keys()].forEach((documentId) => {
    if (visible.has(documentId)) return;
    clearTimeout(state.aiScrollTimers.get(documentId));
    state.aiScrollTimers.delete(documentId);
    state.aiFollow.delete(documentId);
    state.aiScrollPositions.delete(documentId);
  });
}

function updateAiActivity(activity) {
  state.aiActivity = activity;
  const host = $("#ai-live-host");
  if (!host) return;
  const sessions = aiSessions(activity);
  const currentIds = $$("[data-ai-session-id]", host).map((card) => card.dataset.aiSessionId);
  const nextIds = sessions.map((session) => String(session.document_id));
  pruneAiPanelState(nextIds);
  if (currentIds.join("\u0000") !== nextIds.join("\u0000")) {
    host.innerHTML = aiActivityMarkup(activity);
    initializeAiActivityPanels();
    return;
  }
  sessions.forEach((session, index) => {
    const card = $$("[data-ai-session-id]", host)[index];
    const isActive = aiSessionIsActive(activity, session.document_id);
    card.classList.toggle("active", isActive);
    card.classList.toggle("completed", !isActive);
    $("[data-ai-session-head]", card).innerHTML = aiSessionHeadMarkup(session, isActive);
    $("[data-ai-session-meta]", card).innerHTML = aiSessionMetaMarkup(session, activity);
    $("[data-ai-session-actions]", card).innerHTML = aiSessionActionsMarkup(session, isActive);
    updateAiTrace($(".ai-trace", card), session);
  });
}

function wireAiActivityControls() {
  const host = $("#ai-live-host");
  if (!host || host.dataset.controlsWired === "true") return;
  host.dataset.controlsWired = "true";
  host.addEventListener("click", async (event) => {
    const jumpButton = event.target.closest(".ai-jump-latest");
    if (jumpButton) {
      const trace = $(`.ai-trace[data-document="${CSS.escape(jumpButton.dataset.document)}"]`, host);
      if (trace) scrollAiTraceToLatest(trace, jumpButton.dataset.document, "smooth");
      return;
    }
    const button = event.target.closest(".stop-inference");
    if (!button) return;
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      const result = await api("/api/v1/admin/ai/stop", { method: "POST", body: { document_id: button.dataset.document } });
      toast(result.stopped ? "AI inference stop requested. The file was moved to Review." : "That inference already finished.");
      await refreshAiActivity();
      await refreshReviewBadge();
    } catch (error) { toast(error.message, true); button.disabled = false; button.textContent = "Stop inference"; }
  });
}

async function refreshAiActivity() {
  if (!$("#ai-live-host")) return;
  const activity = await api("/api/v1/admin/ai/activity");
  state.aiStreamRevision = Math.max(state.aiStreamRevision, Number(activity.revision || 0));
  updateAiActivity(activity);
}

function aiProfileCard(profile, activeId, selectedId) {
  const active = profile.id === activeId;
  const selected = profile.id === selectedId;
  return `<article class="ai-profile-card ${active ? "active" : ""} ${selected ? "selected" : ""}">
    <button class="ai-profile-select" type="button" data-profile-select="${escapeHtml(profile.id)}">
      <span><strong>${escapeHtml(profile.name)}</strong><small>${profile.builtin ? "Built-in · read-only" : "Custom · editable"}</small></span>
      ${active ? '<span class="status-pill online">Active</span>' : ""}
    </button>
    <p>${escapeHtml(profile.description)}</p>
    <div class="settings-actions compact">
      ${active ? "" : `<button class="primary activate-profile" type="button" data-profile-id="${escapeHtml(profile.id)}">Use profile</button>`}
      <button class="secondary copy-profile" type="button" data-profile-id="${escapeHtml(profile.id)}">Copy to custom</button>
      ${profile.builtin ? "" : `<button class="danger delete-profile" type="button" data-profile-id="${escapeHtml(profile.id)}">Delete</button>`}
    </div>
  </article>`;
}

function profileEditorMarkup(profile, profileData) {
  const locked = profile.builtin;
  const readonly = locked ? "readonly" : "";
  const disabled = locked ? "disabled" : "";
  const checked = (value) => value ? "checked" : "";
  return `<form class="settings-card profile-editor" id="profile-editor-form">
    <div class="profile-editor-head"><div><span class="eyebrow">${locked ? "BUILT-IN PROFILE" : "CUSTOM PROFILE"}</span><h3>${escapeHtml(profile.name)}</h3><p>${locked ? "Built-in profiles are protected. Copy this profile to customize every field." : "Every behavior below is saved with this profile and used by its next inference."}</p></div>${locked ? `<button class="secondary copy-profile" type="button" data-profile-id="${escapeHtml(profile.id)}">Copy to custom</button>` : ""}</div>
    <div class="field-grid">
      <div class="field">${labelWithHelp("profile-name", "Profile name", "The name shown in the profile picker and live inference view.")}<input id="profile-name" maxlength="80" value="${escapeHtml(profile.name)}" ${readonly}></div>
      <div class="field full-width">${labelWithHelp("profile-description", "Description", "Explains when this profile should be used.")}<textarea id="profile-description" rows="3" maxlength="500" ${readonly}>${escapeHtml(profile.description)}</textarea></div>
      <div class="field full-width">${labelWithHelp("profile-content-mode", "Note content behavior", "Full transfer writes complete extracted text. Full plus summary writes both. Brief summary omits the extracted body.")}<select id="profile-content-mode" ${disabled}><option value="full">Full document transfer — complete extracted text</option><option value="full-and-summary">Full document plus AI summary</option><option value="summary">Brief summary only</option></select></div>
    </div>
    <h4>Inference prompts</h4>
    <div class="field">${labelWithHelp("profile-role-prompt", "AI role prompt", "The editable profile-specific system role. The protected safety/schema prompt below is always applied first.")}<textarea id="profile-role-prompt" rows="10" maxlength="20000" ${readonly}>${escapeHtml(profile.role_prompt)}</textarea></div>
    <div class="field">${labelWithHelp("profile-user-template", "User prompt template", `Controls exactly how document data reaches the model. Available placeholders: ${profileData.prompt_placeholders.join(", ")}.`)}<textarea id="profile-user-template" rows="13" maxlength="20000" ${readonly}>${escapeHtml(profile.user_prompt_template)}</textarea></div>
    <details class="prompt-boundary"><summary>Protected inference prompt (visible, not editable)</summary><p>This prompt enforces JSON, treats documents as untrusted, and protects the vault. It is shown in full so the actual inference behavior is inspectable.</p><textarea rows="18" readonly>${escapeHtml(profileData.protected_system_prompt)}</textarea></details>
    <h4>Model parameters</h4>
    <div class="field-grid compact-grid">
      <div class="field">${labelWithHelp("profile-temperature", "Temperature", "Lower values are more consistent; higher values are more varied.")}<input id="profile-temperature" type="number" min="0" max="2" step="0.05" value="${escapeHtml(profile.temperature)}" ${readonly}></div>
      <div class="field">${labelWithHelp("profile-top-p", "Top P", "Limits token sampling by cumulative probability.")}<input id="profile-top-p" type="number" min="0" max="1" step="0.05" value="${escapeHtml(profile.top_p)}" ${readonly}></div>
      <div class="field">${labelWithHelp("profile-output-tokens", "Maximum output tokens", "Maximum tokens allowed for the model's metadata JSON response.")}<input id="profile-output-tokens" type="number" min="128" max="32768" value="${escapeHtml(profile.max_output_tokens)}" ${readonly}></div>
      <div class="field">${labelWithHelp("profile-input-chars", "Document input characters", "Maximum extracted characters sent to the model and transferred by a full-content note.")}<input id="profile-input-chars" type="number" min="1000" max="2000000" step="1000" value="${escapeHtml(profile.input_char_limit)}" ${readonly}></div>
      <div class="field">${labelWithHelp("profile-candidates", "Vault note candidates", "Maximum existing notes supplied for related-note selection.")}<input id="profile-candidates" type="number" min="0" max="500" value="${escapeHtml(profile.candidate_limit)}" ${readonly}></div>
      <div class="field">${labelWithHelp("profile-tag-limit", "Tag limit", "Maximum validated AI tags stored on the note.")}<input id="profile-tag-limit" type="number" min="0" max="30" value="${escapeHtml(profile.tag_limit)}" ${readonly}></div>
      <div class="field">${labelWithHelp("profile-related-limit", "Related-note limit", "Maximum validated existing-note wikilinks. Relevant links are not restricted to one or two.")}<input id="profile-related-limit" type="number" min="0" max="250" value="${escapeHtml(profile.related_notes_limit)}" ${readonly}></div>
    </div>
    <h4>Obsidian behaviors</h4>
    <p>Choose how this profile uses Obsidian's native Markdown, YAML, tags, folders, and wikilinks. Internal Obsync identity properties remain protected so updates stay safe.</p>
    <div class="behavior-grid">
      <label class="check-row"><input id="profile-vault-context" type="checkbox" ${checked(profile.use_vault_context)} ${disabled}> Search the whole-vault index, including content, headings, properties, entities, links, and backlinks</label>
      <label class="check-row"><input id="profile-wikilinks" type="checkbox" ${checked(profile.use_wikilinks)} ${disabled}> Add validated <code>[[wikilinks]]</code> to clearly related existing notes</label>
      <label class="check-row"><input id="profile-tags" type="checkbox" ${checked(profile.use_tags)} ${disabled}> Generate searchable Obsidian tags</label>
      <label class="check-row"><input id="profile-properties" type="checkbox" ${checked(profile.use_properties)} ${disabled}> Generate type and category YAML properties</label>
      <label class="check-row"><input id="profile-folders" type="checkbox" ${checked(profile.organize_folders)} ${disabled}> Organize new notes into AI-selected category folders</label>
      <label class="check-row"><input id="profile-source-details" type="checkbox" ${checked(profile.include_source_details)} ${disabled}> Include source file details and integrity hash</label>
    </div>
    <div class="info-box"><strong>How Obsync connects to Obsidian</strong><p>${escapeHtml(profileData.implementation)}</p></div>
    ${locked ? "" : '<div class="settings-actions"><button class="primary" type="submit">Save custom profile</button></div>'}
  </form>`;
}

function profilePayload() {
  return {
    name: $("#profile-name").value.trim(),
    description: $("#profile-description").value.trim(),
    note_content_mode: $("#profile-content-mode").value,
    role_prompt: $("#profile-role-prompt").value.trim(),
    user_prompt_template: $("#profile-user-template").value,
    temperature: $("#profile-temperature").value,
    top_p: $("#profile-top-p").value,
    max_output_tokens: $("#profile-output-tokens").value,
    input_char_limit: $("#profile-input-chars").value,
    candidate_limit: $("#profile-candidates").value,
    tag_limit: $("#profile-tag-limit").value,
    related_notes_limit: $("#profile-related-limit").value,
    use_vault_context: $("#profile-vault-context").checked,
    use_wikilinks: $("#profile-wikilinks").checked,
    use_tags: $("#profile-tags").checked,
    use_properties: $("#profile-properties").checked,
    organize_folders: $("#profile-folders").checked,
    include_source_details: $("#profile-source-details").checked,
  };
}

async function renderLocalAI() {
  const [settings, activity, profiles] = await Promise.all([
    api("/api/v1/admin/settings"), api("/api/v1/admin/ai/activity"), api("/api/v1/admin/ai/profiles"),
  ]);
  const enabled = settings.llm_enabled === "true";
  state.aiActivity = activity;
  state.aiProfiles = profiles;
  if (!profiles.items.some((item) => item.id === state.aiSelectedProfileId)) state.aiSelectedProfileId = profiles.active_profile_id;
  const selectedProfile = profiles.items.find((item) => item.id === state.aiSelectedProfileId) || profiles.items[0];
  state.aiStreamRevision = Math.max(state.aiStreamRevision, Number(activity.revision || 0));
  pruneAiPanelState(aiSessions(activity).map((session) => String(session.document_id)));
  const profileCards = profiles.items.map((profile) => aiProfileCard(profile, profiles.active_profile_id, selectedProfile.id)).join("");
  $("#content").innerHTML = `<div class="settings-layout"><div id="ai-live-host">${aiActivityMarkup(activity)}</div>
    <section class="settings-card"><div class="section-head compact"><div><h3>${headingWithHelp("AI profiles", "The active profile controls the role, prompt template, note content, inference parameters, and Obsidian behaviors.")}</h3><p>Choose a protected default or copy one into a fully editable custom profile.</p></div><button class="primary" type="button" id="new-ai-profile">+ New custom from active</button></div><div class="ai-profile-grid">${profileCards}</div></section>
    ${profileEditorMarkup(selectedProfile, profiles)}
    <form class="settings-layout" id="ai-settings-form">
    <section class="settings-card"><h3>${headingWithHelp("Local AI connection", "Optional local classification through Ollama, LM Studio, or another OpenAI-compatible server.")}</h3>
      <div class="field-grid">
        <label class="check-row full-width"><input id="llm-enabled" type="checkbox" ${enabled ? "checked" : ""}> Enable Local AI organization</label>
        <div class="field">${labelWithHelp("llm-provider", "Provider", "The API format exposed by your local model server.")}<select id="llm-provider"><option value="ollama">Ollama</option><option value="lmstudio">LM Studio</option><option value="openai-compatible">OpenAI-compatible</option></select></div>
        <div class="field">${labelWithHelp("llm-model", "Model", "The exact model identifier reported by the provider.")}<input id="llm-model" value="${escapeHtml(settings.llm_model || "")}" placeholder="qwen3:8b"></div>
        <div class="field full-width">${labelWithHelp("llm-url", "Base URL", "The model-server address as seen by Obsync.")}<input id="llm-url" value="${escapeHtml(settings.llm_base_url || "")}" placeholder="http://host.docker.internal:11434"></div>
        <div class="field full-width">${labelWithHelp("llm-key", "API key (optional)", "Required only when the provider requires authentication.")}<input id="llm-key" type="password" value="" placeholder="${settings.llm_api_key === "configured" ? "Configured — leave blank to keep" : "Not required for default local servers"}"></div>
        <div class="field">${labelWithHelp("review-threshold", "Review below", "Lower-confidence classifications are held for review.")}<input id="review-threshold" type="number" min="0" max="1" step="0.05" value="${escapeHtml(settings.review_threshold || ".65")}"></div>
        <div class="field">${labelWithHelp("llm-timeout", "Model timeout (seconds)", "Maximum time for one local classification.")}<input id="llm-timeout" type="number" min="5" max="600" value="${escapeHtml(settings.llm_timeout_seconds || "120")}"></div>
      </div>
      <div class="settings-actions"><button class="secondary" type="button" id="test-llm">Check connection</button></div>
      <p class="inline-status" id="llm-test-status" hidden></p>
    </section>
    <section class="settings-card"><h3>Vault matching and duplicate protection</h3>
      <div class="field">${labelWithHelp("duplicate-policy", "Possible duplicate handling", "A conservative title and Obsync-metadata scan runs before automatic writing.")}<select id="duplicate-policy"><option value="review">Hold possible duplicates for review (recommended)</option><option value="allow">Allow separate notes automatically</option></select></div>
      <div class="settings-actions"><button class="primary" type="submit">Save Local AI settings</button></div>
    </section>
  </form></div>`;
  wireAiActivityControls();
  initializeAiActivityPanels();
  $("#profile-content-mode").value = selectedProfile.note_content_mode;
  $("#llm-provider").value = settings.llm_provider || "ollama";
  $("#duplicate-policy").value = settings.duplicate_policy || "review";
  $$('[data-profile-select]').forEach((button) => button.addEventListener("click", async () => {
    state.aiSelectedProfileId = button.dataset.profileSelect;
    await renderLocalAI();
  }));
  $$(".activate-profile").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/api/v1/admin/ai/profiles/${encodeURIComponent(button.dataset.profileId)}/activate`, { method: "POST" }); toast("AI profile activated. New inference will use it."); await renderLocalAI(); }
    catch (error) { toast(error.message, true); }
  }));
  $$(".copy-profile").forEach((button) => button.addEventListener("click", async () => {
    try { const created = await api("/api/v1/admin/ai/profiles", { method: "POST", body: { source_profile_id: button.dataset.profileId } }); state.aiSelectedProfileId = created.id; toast("Editable custom profile created."); await renderLocalAI(); }
    catch (error) { toast(error.message, true); }
  }));
  $$(".delete-profile").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Delete this custom AI profile? Built-in profiles and documents are unaffected.")) return;
    try { await api(`/api/v1/admin/ai/profiles/${encodeURIComponent(button.dataset.profileId)}`, { method: "DELETE" }); state.aiSelectedProfileId = ""; toast("Custom profile deleted."); await renderLocalAI(); }
    catch (error) { toast(error.message, true); }
  }));
  $("#new-ai-profile").addEventListener("click", async () => {
    try { const created = await api("/api/v1/admin/ai/profiles", { method: "POST", body: { source_profile_id: profiles.active_profile_id, name: "Custom AI profile" } }); state.aiSelectedProfileId = created.id; toast("Editable custom profile created."); await renderLocalAI(); }
    catch (error) { toast(error.message, true); }
  });
  if (!selectedProfile.builtin) $("#profile-editor-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await api(`/api/v1/admin/ai/profiles/${encodeURIComponent(selectedProfile.id)}`, { method: "PUT", body: profilePayload() }); toast("Custom AI profile saved."); await renderLocalAI(); }
    catch (error) { toast(error.message, true); }
  });
  $("#ai-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await api("/api/v1/admin/settings", { method: "PUT", body: aiSettingsPayload() }); toast("Local AI settings saved."); }
    catch (error) { toast(error.message, true); }
  });
  $("#test-llm").addEventListener("click", async () => {
    const button = $("#test-llm"); button.disabled = true; button.textContent = "Testing…";
    const status = $("#llm-test-status"); status.hidden = false; status.className = "inline-status"; status.textContent = "Checking the model server…";
    try {
      const result = await api("/api/v1/admin/settings/test-llm", { method: "POST", body: aiSettingsPayload() });
      status.className = `inline-status ${result.ok ? "good" : "bad"}`;
      status.textContent = result.message;
      if (!$("#llm-model").value && result.suggested_model) $("#llm-model").value = result.suggested_model;
    } catch (error) { status.className = "inline-status bad"; status.textContent = error.message; }
    finally { button.disabled = false; button.textContent = "Check connection"; }
  });
}

async function renderSettings() {
  const [server, agentData, meta, pipeline] = await Promise.all([
    api("/api/v1/admin/server"), api("/api/v1/admin/agents"), api("/api/v1/meta"), api("/api/v1/admin/pipeline"),
  ]);
  state.server = server;
  state.agents = agentData.items;
  updatePipelineControl(pipeline);
  const desktopRows = state.agents.map((agent) => `<div class="connection-row"><span class="status-pill ${agent.status}">${agent.status}</span><div><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.os_name)} · Desktop ${escapeHtml(agent.agent_version || "unknown")} · seen ${relativeTime(agent.last_seen_at)}</small></div></div>`).join("") || '<div class="empty">No Desktop apps connected.</div>';
  $("#content").innerHTML = `<div class="settings-layout">
    <section class="settings-card"><h3>Obsync server</h3><div class="connection-row"><span class="status-pill online">online</span><div><strong>Version ${escapeHtml(meta.version)}</strong><small>${server.container ? "Docker" : escapeHtml(server.os_name)} · ${escapeHtml(location.origin)}</small></div></div><div class="settings-actions"><button class="secondary" id="verify-connections">Verify all connections</button><button class="quiet" id="copy-server-address">Copy server address</button></div><p class="inline-status" id="connection-check-status">Live connection checks run automatically.</p></section>
    <section class="settings-card"><h3>Obsync Desktop apps</h3>${desktopRows}<div class="settings-actions"><button class="primary" id="open-desktop-app">Open Obsync Desktop</button><a class="secondary button-link" href="/api/v1/downloads/windows-desktop">Download or repair Desktop</a><button class="quiet" id="add-settings-computer">+ Add computer</button></div><small>Open Desktop works after v0.9.0 or later has been installed and Windows has registered the <code>obsync://</code> app link.</small></section>
    <section class="settings-card"><h3>Application controls</h3><p>Global sync is <strong>${pipeline.enabled ? "running" : "stopped"}</strong>. Use <strong>${pipeline.enabled ? "Stop Global Sync" : "Start Global Sync"}</strong> in the top bar. Individual folder controls are under Sources.</p><div class="settings-actions"><button class="secondary" id="account-settings-link">Account settings</button><button class="quiet" id="open-help-link">Open Help</button></div></section>
  </div>`;
  $("#verify-connections").addEventListener("click", async () => {
    const status = $("#connection-check-status"); status.textContent = "Checking server and Desktop apps…";
    try { await Promise.all([api("/api/v1/health"), api("/api/v1/admin/agents")]); status.className = "inline-status good"; status.textContent = "Server and connection records verified just now."; }
    catch (error) { status.className = "inline-status bad"; status.textContent = error.message; }
  });
  $("#copy-server-address").addEventListener("click", () => copyText(location.origin));
  $("#open-desktop-app").addEventListener("click", () => { window.location.href = "obsync://open"; toast("Windows was asked to open Obsync Desktop."); });
  $("#add-settings-computer").addEventListener("click", openEnrollment);
  $("#account-settings-link").addEventListener("click", openAccountSettings);
  $("#open-help-link").addEventListener("click", () => navigate("help"));
}

async function renderHelp() {
  $("#content").innerHTML = `
    <div class="help-layout">
      <section class="help-hero panel">
        <div><span class="eyebrow">START HERE</span><h2>From a folder to organized Obsidian notes</h2><p>Obsync uses one central server, the Obsync Desktop app on each computer, one Obsidian vault, and any number of watched folders.</p></div>
        <button class="primary" id="help-add-computer">Add a computer</button>
      </section>
      <section class="help-steps" aria-label="Quick start">
        <article class="help-step"><span>1</span><div><h3>Secure the server</h3><p>Create the local administrator username and password. The server stores settings and coordinates every computer.</p></div></article>
        <article class="help-step"><span>2</span><div><h3>Connect the vault computer</h3><p>Open <strong>Sources → Add another computer</strong>, download Obsync Desktop once, copy all setup details, and paste them into the app. Its built-in watcher runs silently and starts at sign-in.</p></div></article>
        <article class="help-step"><span>3</span><div><h3>Select the vault</h3><p>Open the dedicated <strong>Obsidian Vault</strong> tab, choose the exact destination, and save it before syncing.</p></div></article>
        <article class="help-step"><span>4</span><div><h3>Add folders</h3><p>On the Sources page, click <strong>Add folder</strong> on a connected computer. Choose any external folder you want Obsync to monitor.</p></div></article>
        <article class="help-step"><span>5</span><div><h3>Scan, inspect, then sync</h3><p>Scan compares every file without writing. View files shows the results. Sync changes extracts, organizes, and writes only what needs attention.</p></div></article>
      </section>
      <section class="help-grid">
        <article class="settings-card"><h3>What each page does</h3>
          <dl class="help-list">
            <div><dt>Overview</dt><dd>Health, totals, current active files, recent activity, and vault availability.</dd></div>
            <div><dt>Sources</dt><dd>Connect computers, add watched folders, compare files, and sync changes.</dd></div>
            <div><dt>Obsidian Vault</dt><dd>Choose and confirm the one vault where managed notes are written.</dd></div>
            <div><dt>Local AI</dt><dd>Configure the model and watch the active file, streamed model activity, and final decision.</dd></div>
            <div><dt>Documents</dt><dd>Search every indexed source, inspect its state, or request a new AI review.</dd></div>
            <div><dt>Review</dt><dd>Approve, disregard, redo with feedback, or explicitly separate a possible duplicate.</dd></div>
            <div><dt>Settings</dt><dd>Verify the server and Desktop apps, open Desktop, and manage the application.</dd></div>
          </dl>
        </article>
        <article class="settings-card"><h3>File status colors</h3>
          <div class="help-status">${comparisonBadge("in-sync")}<p>The source hash matches the managed note in Obsidian.</p></div>
          <div class="help-status">${comparisonBadge("modified")}<p>The source or its managed note changed and should be synced again.</p></div>
          <div class="help-status">${comparisonBadge("new")}<p>The source has not been written to Obsidian yet.</p></div>
          <div class="help-status">${comparisonBadge("vault-missing")}<p>The source is known, but its expected managed note is missing.</p></div>
          <div class="help-status">${comparisonBadge("source-missing")}<p>The original file disappeared. Obsync keeps the note and marks it missing.</p></div>
        </article>
        <article class="settings-card"><h3>Why Windows needs Obsync Desktop</h3><p>Docker and web browsers are intentionally isolated from arbitrary Windows files. Obsync Desktop includes the safe local bridge, native folder picker, background watcher, and start/stop controls in one app. Run it as Administrator for the one-time setup; background syncing then runs with limited permissions.</p><p>If a computer shows offline, use <strong>Reconnect</strong> on its Sources card. Start the installed Desktop first; if its saved credential or installation is damaged, use the generated repair details. Reconnecting in place preserves watched folders, document history, and vault assignment.</p></article>
        <article class="settings-card"><h3>Stopping and removing</h3><p>Use <strong>Stop inference</strong> on Local AI to end only the active model request and move that file to Review. Use <strong>Stop Global Sync</strong> to cancel all active sync and AI work. Each source folder also has independent Start, Pause, and Stop controls. Remove forgets a folder; Disconnect revokes a whole computer. Original files and existing Obsidian notes are always kept.</p></article>
        <article class="settings-card"><h3>Server vs. desktop</h3><p>The central server always appears as one connected computer. Docker can access only folders mounted into its container. Pair the physical desktop whenever the vault or source folders live in Windows Documents, another user folder, a Mac, or a different PC.</p></article>
        <article class="settings-card"><h3>Local AI and the whole vault</h3><p>AI is optional. Obsync searches the complete whole-vault index, then sends bounded content from only the most relevant notes—not unrestricted filesystem or Obsidian API access. Obsync validates matching, folders, tags, properties, and path-qualified links before writing. The live activity view is read-only and its bounded model trace is not stored as chat history.</p></article>
        <article class="settings-card"><h3>Safety</h3><p>Obsync never edits, moves, or deletes source files. It owns only marked generated sections in Markdown and preserves text under <strong>My notes</strong>. Missing files are recorded rather than propagated as deletions.</p></article>
      </section>
      <section class="settings-card help-troubleshooting"><h3>Troubleshooting</h3>
        <details><summary>A Windows PC does not appear in a selector</summary><p>Confirm Obsync Desktop reported success and the Sources card says online. The Overview count includes the central server, which is not a desktop selector option.</p></details>
        <details><summary>A Windows PC is offline or cannot reconnect</summary><p>Open <strong>Sources</strong> and choose <strong>Reconnect</strong> on that PC. First choose <strong>Open installed Desktop</strong> and <strong>Start this PC</strong>. If the saved connection is damaged, download the latest Desktop and use the repair details. Do not Disconnect the PC unless you intend to remove its watched-folder and document ledger.</p></details>
        <details><summary>Windows reports Errno 13 or Permission denied</summary><p>Close every visible Obsync Desktop window, wait a few seconds, and retry with the latest Desktop. Obsync stops its background task before replacing a changed executable and does not consume the reconnect code unless the local repair succeeds.</p></details>
        <details><summary>Obsync Desktop says the pairing code was already used</summary><p>Close duplicate Desktop windows and reopen one copy. Obsync reuses a valid saved connection and repairs startup. If the computer was disconnected, create a new code.</p></details>
        <details><summary>Windows warns that Obsync Desktop is unrecognized</summary><p>Early community builds are not code-signed. Confirm the file came from the official Obsync GitHub release before choosing <strong>More info → Run anyway</strong> in Windows SmartScreen.</p></details>
        <details><summary>The folder browser does not open</summary><p>The selected desktop must be online. Open Obsync Desktop and choose Start this PC, then retry Add folder or Browse for vault.</p></details>
        <details><summary>The model check fails</summary><p>Confirm the provider, exact model name, and base URL. From Docker Desktop, <code>host.docker.internal</code> usually reaches Ollama or LM Studio on the host.</p></details>
        <details><summary>A file remains red or orange</summary><p>Open View files for the exact reason, confirm the vault computer is online, then choose Sync changes. Errors also appear under Documents.</p></details>
      </section>
    </div>`;
  $("#help-add-computer").addEventListener("click", async () => {
    if (!state.server) state.server = await api("/api/v1/admin/server");
    openEnrollment();
  });
}

function settingsPayload() {
  return { ...vaultSettingsPayload(), ...aiSettingsPayload() };
}

function vaultSettingsPayload() {
  const payload = {
    vault_mode: $('input[name="vault-mode"]:checked')?.value || "local",
    vault_agent_id: $("#vault-agent")?.value || "",
  };
  if (!$("#vault-index-change-mode")) return payload;
  return {
    ...payload,
    existing_note_policy: $("#existing-note-policy").value,
    vault_index_schedule_enabled: $("#vault-index-schedule-enabled").checked,
    vault_index_schedule_frequency: $("#vault-index-frequency").value,
    vault_index_schedule_time: $("#vault-index-time").value,
    vault_index_schedule_weekday: $("#vault-index-weekday").value,
    vault_index_schedule_month_day: $("#vault-index-month-day").value,
    vault_index_schedule_interval_hours: $("#vault-index-interval").value,
    vault_index_change_mode: $("#vault-index-change-mode").value,
    vault_maintenance_schedule_enabled: $("#vault-maintenance-schedule-enabled").checked,
    vault_maintenance_schedule_frequency: $("#vault-maintenance-frequency").value,
    vault_maintenance_schedule_time: $("#vault-maintenance-time").value,
    vault_maintenance_schedule_weekday: $("#vault-maintenance-weekday").value,
    vault_maintenance_schedule_month_day: $("#vault-maintenance-month-day").value,
    vault_maintenance_schedule_interval_hours: $("#vault-maintenance-interval").value,
    vault_maintenance_change_mode: $("#vault-maintenance-change-mode").value,
    vault_schedule_timezone: $("#vault-timezone").value.trim(),
    vault_maintenance_categories: $$(".maintenance-category:checked").map((input) => input.value),
    vault_relationship_candidate_limit: $("#vault-candidate-limit").value,
    vault_relationship_min_confidence: $("#vault-relationship-confidence").value,
    vault_link_limit: $("#vault-link-limit").value,
  };
}

function aiSettingsPayload() {
  return {
    llm_enabled: $("#llm-enabled").checked,
    llm_provider: $("#llm-provider").value,
    llm_base_url: $("#llm-url").value.trim(),
    llm_model: $("#llm-model").value.trim(),
    llm_api_key: $("#llm-key").value,
    review_threshold: $("#review-threshold").value,
    llm_timeout_seconds: $("#llm-timeout").value,
    duplicate_policy: $("#duplicate-policy")?.value || "review",
  };
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  const username = $("#username-input").value.trim();
  const password = $("#password-input").value;
  const remember = $("#remember-login").checked;
  const button = $("#auth-submit");
  button.disabled = true;
  try {
    if (state.authMode === "setup") {
      if (password !== $("#confirm-password-input").value) {
        throw new Error("Passwords do not match.");
      }
      await api("/api/v1/auth/setup", {
        method: "POST",
        authRequest: true,
        body: {
          username,
          password,
          remember,
          legacy_token: $("#legacy-token-input").value.trim(),
        },
      });
    } else {
      await api("/api/v1/auth/login", {
        method: "POST",
        authRequest: true,
        body: { username, password, remember },
      });
    }
    $("#password-input").value = "";
    $("#confirm-password-input").value = "";
    $("#legacy-token-input").value = "";
    const session = await api("/api/v1/admin/session", { authRequest: true });
    await openApp(session);
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("#account-button").addEventListener("click", (event) => {
  event.stopPropagation();
  const menu = $("#account-menu");
  menu.hidden = !menu.hidden;
  $("#account-button").setAttribute("aria-expanded", String(!menu.hidden));
});
$("#account-settings-button").addEventListener("click", openAccountSettings);
$("#sign-out-button").addEventListener("click", performLogout);
document.addEventListener("click", (event) => {
  if (!event.target.closest(".account-control")) closeAccountMenu();
  const help = event.target.closest(".help-tip");
  if (help) {
    event.preventDefault();
    event.stopPropagation();
    help.focus();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeAccountMenu();
});
$("#secure-admin-button").addEventListener("click", openSecuritySetup);
$("#pipeline-toggle").addEventListener("click", togglePipeline);
$("#theme-button").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("#help-button").addEventListener("click", () => navigate("help"));
$("#refresh-button").addEventListener("click", () => navigate(state.view));
$("#menu-button").addEventListener("click", () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#menu-button").setAttribute("aria-expanded", String(open));
});
$("#modal-close").addEventListener("click", () => $("#modal").close());
$$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
window.addEventListener("pagehide", stopLiveUpdates);
window.addEventListener("pageshow", (event) => {
  if (event.persisted && state.session) startLiveUpdates();
});

const storedTheme = localStorage.getItem("obsync_theme");
setTheme(storedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
bootstrapAuth();
