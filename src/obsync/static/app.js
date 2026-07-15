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
};

function cookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

const viewMeta = {
  overview: ["Overview", "Your knowledge pipeline at a glance."],
  sources: ["Sources", "Every computer and folder feeding your vault."],
  documents: ["Documents", "Search and inspect synchronized knowledge."],
  review: ["Review", "Confirm items where automated classification was uncertain."],
  settings: ["Settings", "Vault behavior, local models, and processing controls."],
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
  modal.showModal();
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
  modal.showModal();
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
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  updateSecurityState(session);
  await navigate("overview");
  if (session?.temporary && promptToSecure) await openSecuritySetup();
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
    const session = await api("/api/v1/admin/session", { authRequest: true });
    if (session.authenticated) await openApp(session);
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

async function navigate(view) {
  state.view = view;
  const [title, subtitle] = viewMeta[view];
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("#sidebar").classList.remove("open");
  $("#menu-button").setAttribute("aria-expanded", "false");
  loading();
  try {
    if (view === "overview") await renderOverview();
    if (view === "sources") await renderSources();
    if (view === "documents") await renderDocuments(false);
    if (view === "review") await renderDocuments(true);
    if (view === "settings") await renderSettings();
    if (view === "help") await renderHelp();
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

async function openRootFiles(root) {
  const modal = $("#modal");
  $("#modal-title").textContent = root.name;
  $("#modal-body").innerHTML = '<div class="skeleton"></div>';
  modal.showModal();
  try {
    const data = await api(`/api/v1/admin/documents?root_id=${encodeURIComponent(root.id)}&limit=500`);
    const rows = data.items.map((doc) => `<div class="inventory-file">
      ${comparisonBadge(doc.comparison_status)}
      <div><strong>${escapeHtml(doc.source_name)}</strong><small title="${escapeHtml(doc.source_path)}">${escapeHtml(doc.source_path)}</small></div>
    </div>`).join("");
    $("#modal-body").innerHTML = `${rows || '<div class="empty">No files found in the latest scan.</div>'}
      <div class="status-legend">${comparisonBadge("in-sync")}${comparisonBadge("modified")}${comparisonBadge("new")}${comparisonBadge("vault-missing")}</div>`;
  } catch (error) {
    $("#modal-body").innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`;
  }
}

async function renderOverview() {
  const data = await api("/api/v1/admin/overview");
  state.overview = data;
  const stats = data.stats;
  $("#review-badge").hidden = !stats.review;
  $("#review-badge").textContent = stats.review;
  const events = data.recent_events.map((event) => `
    <li class="event ${escapeHtml(event.level)}"><i class="event-dot"></i><p>${escapeHtml(event.message)}</p><small>${relativeTime(event.created_at)}</small></li>
  `).join("") || '<li class="empty">No activity yet.</li>';
  $("#content").innerHTML = `
    <div class="stats">
      ${statCard("Synced documents", stats.synced, "↗", "good", "Files whose current source content is represented by an Obsync-managed note in the vault.")}
      ${statCard("Connected computers", `${stats.online_computers}/${stats.computers}`, "◎", "", "Online computers divided by all known computers. The central Obsync server counts as one computer.")}
      ${statCard("Needs review", stats.review, "◇", stats.review ? "warn" : "", "Documents whose automated classification confidence is below your review threshold.")}
      ${statCard("Errors", stats.errors, "!", stats.errors ? "bad" : "", "Documents that could not be extracted, classified, compared, or written.")}
    </div>
    <div class="grid-two">
      <section class="panel"><div class="panel-head"><h3>${headingWithHelp("Recent activity", "The latest scans, syncs, warnings, and connection events.")}</h3><button class="quiet" data-go="documents" title="Open the complete document list">View all</button></div><ul class="event-list">${events}</ul></section>
      <section class="panel"><div class="panel-head"><h3>${headingWithHelp("System", "A quick check of the active vault, watched folders, and missing sources.")}</h3></div>
        <div class="event-list">
          <div class="event"><i class="event-dot"></i><p>Obsidian vault<br><small>${escapeHtml(data.vault.path)}</small></p><small>${data.vault.writable ? "Ready" : "Unavailable"}</small></div>
          <div class="event"><i class="event-dot"></i><p>Watched folders</p><small>${stats.roots}</small></div>
          <div class="event"><i class="event-dot"></i><p>Missing sources</p><small>${stats.missing}</small></div>
        </div>
      </section>
    </div>`;
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.go)));
}

async function renderSources() {
  const [server, agentData, rootData] = await Promise.all([
    api("/api/v1/admin/server"), api("/api/v1/admin/agents"), api("/api/v1/admin/roots"),
  ]);
  state.server = server;
  state.agents = agentData.items;
  state.roots = rootData.items;
  const cards = state.agents.map((agent) => {
    const roots = state.roots.filter((root) => root.agent_id === agent.id);
    const rootRows = roots.map((root) => `<div class="root-row inventory-root">
      <div class="root-title"><strong>${escapeHtml(root.name)} ${helpIcon("A watched source folder. Scan compares it with Obsidian; Sync changes writes only new or modified knowledge notes.", `About ${root.name}`)}</strong><span>${root.file_count || root.document_count || 0} files</span></div>
      <code>${escapeHtml(root.path)}</code>
      <div class="root-comparison">
        ${comparisonBadge("in-sync", root.in_sync_count || 0)}
        ${comparisonBadge("modified", root.modified_count || 0)}
        ${comparisonBadge("new", root.new_count || 0)}
        ${comparisonBadge("vault-missing", root.missing_count || 0)}
        ${root.checking_count ? comparisonBadge("checking", root.checking_count) : ""}
      </div>
      <div class="root-actions"><button class="quiet view-root" data-root="${root.id}" title="Inspect every file and its current comparison status">View files</button><button class="quiet scan-root" data-root="${root.id}" title="Compare the folder with Obsidian without writing notes">Scan</button><button class="secondary sync-root" data-root="${root.id}" title="Process only new, modified, or missing items">Sync changes</button></div>
    </div>`).join("");
    return `<article class="source-card">
      <div class="source-top"><span class="device-icon">${agent.os_name === "Windows" ? "▣" : "◫"}</span><div><h3>${escapeHtml(agent.name)} ${helpIcon("A paired desktop companion that gives Obsync safe access to folders on this computer.", `About ${agent.name}`)}</h3><p>${escapeHtml(agent.os_name)} · seen ${relativeTime(agent.last_seen_at)}</p>${agent.vault_ready ? '<span class="device-role">VAULT WRITER READY</span>' : ""}</div><span class="status-pill ${agent.status}" title="Whether the companion is currently communicating with the server">${agent.status}</span></div>
      ${rootRows || '<div class="root-row">No watched folders registered yet.</div>'}
      ${agent.vault_path ? `<div class="root-row"><strong>Obsidian vault</strong><code>${escapeHtml(agent.vault_path)}</code></div>` : ""}
      <div class="source-stats"><span>${agent.document_count || 0} files indexed</span><button class="primary add-folder" data-agent="${agent.id}" title="Open this computer's folder browser and add a directory to watch">+ Add folder</button></div>
    </article>`;
  }).join("");
  const serverPath = server.vault_host_path || server.vault_path;
  const serverCard = `<article class="source-card server-card">
    <div class="source-top"><span class="device-icon">◆</span><div><h3>${escapeHtml(server.name)} ${helpIcon("The central Obsync server processes files, stores the sync ledger, and coordinates every desktop companion.", "About the Obsync server")}</h3><p>${server.container ? "Docker container" : escapeHtml(server.os_name)} · ${escapeHtml(server.hostname)}</p><span class="device-role">ALWAYS CONNECTED</span></div><span class="status-pill" title="The central server is running">online</span></div>
    <div class="root-row"><strong>Server vault mount</strong><code>${escapeHtml(serverPath)}</code></div>
    <div class="source-stats"><span>Processes files and coordinates every desktop</span></div>
  </article>`;
  const emptyHelp = state.agents.length ? "" : '<div class="panel source-help"><strong>1 connected computer means this Obsync server.</strong><p>Pair the Windows, Linux, or macOS computer that contains your source folders. It will then appear here and in the vault computer selector.</p></div>';
  $("#content").innerHTML = `<div class="section-head"><div><h2>${headingWithHelp("Computers and folders", "Pair a desktop companion, choose one or more source folders, compare them with the vault, then sync only what changed.")}</h2><p>Add folders, scan them against Obsidian, inspect differences, then sync only what changed.</p></div><button class="primary" id="add-device" title="Pair a Windows, Linux, or macOS computer">+ Add another computer</button></div>${emptyHelp}<div class="source-grid">${serverCard}${cards}</div>`;
  $("#add-device").addEventListener("click", openEnrollment);
  $$(".add-folder").forEach((button) => button.addEventListener("click", () => {
    const agent = state.agents.find((item) => item.id === button.dataset.agent);
    if (agent?.status !== "online") { toast("That computer is offline. Start its Obsync agent first.", true); return; }
    openAddFolder(agent);
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
    <label class="check-row"><input id="device-has-vault" type="checkbox"> This computer contains the Obsidian vault ${helpIcon("Select this when the real vault is stored on this computer, such as in Windows Documents. The Companion will ask you to choose it.", "About the vault computer option")}</label>
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
      const download = "https://github.com/eliautobot/obsync/releases/download/v0.6.0/obsync-companion-windows-x64.exe";
      $("#modal-body").innerHTML = `
        <p class="modal-note">This one-time code expires in 20 minutes. The Windows Companion installs for your account, runs silently, and starts automatically when you sign in.</p><div class="pair-code">${escapeHtml(enrollment.code)}</div>
        <div class="pair-steps">
          <div class="pair-step"><p><strong>1.</strong> On the Windows PC, download and open the Companion.</p><a class="primary download-button" href="${download}" target="_blank" rel="noreferrer">Download Windows Companion</a></div>
          <div class="pair-step"><p><strong>2.</strong> Enter these three details in the setup window.</p>
            <div class="pair-detail"><span>Server</span><code>${escapeHtml(server)}</code><button class="quiet copy-pair-value" type="button" data-copy="${escapeHtml(server)}">Copy</button></div>
            <div class="pair-detail"><span>Code</span><code>${escapeHtml(enrollment.code)}</code><button class="quiet copy-pair-value" type="button" data-copy="${escapeHtml(enrollment.code)}">Copy</button></div>
            <div class="pair-detail"><span>Name</span><code>${escapeHtml(label)}</code><button class="quiet copy-pair-value" type="button" data-copy="${escapeHtml(label)}">Copy</button></div>
          </div>
          <div class="pair-step"><p><strong>3.</strong> Click <strong>Connect and install</strong>${hasVault ? " and select the Obsidian vault when asked" : ""}. You may close the setup window after it confirms the connection.</p></div>
        </div>
        <p class="inline-status" id="pairing-status"><span class="connection-wait"><i></i>Waiting for ${escapeHtml(label)} to connect…</span></p>
        <p class="modal-note">No PowerShell window stays open and no Administrator access is required. Linux and macOS users can follow the manual steps in the in-app Help page.</p>`;
      $$(".copy-pair-value").forEach((button) => button.addEventListener("click", () => copyText(button.dataset.copy)));
      pollEnrollment(enrollment.id);
    } catch (error) { toast(error.message, true); }
  });
}

function statusClass(status) {
  return ["error", "processing"].includes(status) ? status : "";
}

async function renderDocuments(reviewOnly) {
  const title = reviewOnly ? "Review queue" : "Knowledge documents";
  const titleHelp = reviewOnly
    ? "Low-confidence classifications wait here until you approve them. Approval confirms the current category and tags."
    : "A searchable record of every scanned source file, its generated note, tags, and source-to-vault state.";
  $("#content").innerHTML = `<div class="section-head"><div><h2>${headingWithHelp(title, titleHelp)}</h2><p>${reviewOnly ? "Approve classifications that fell below your confidence threshold." : "Every source file and how it compares with Obsidian."}</p></div></div><div class="toolbar"><div class="search field">${labelWithHelp("doc-search", "Search documents", "Searches generated titles, source paths, extracted summaries, and tags.")}<input id="doc-search" placeholder="Search title, source, or summary…"></div><div class="field status-filter">${labelWithHelp("doc-status", "Comparison status", "Filters files by whether they match Obsidian, changed, are new, or are missing.")}<select id="doc-status"><option value="">All comparisons</option><option value="in-sync">In Obsidian</option><option value="modified">Modified</option><option value="new">New</option><option value="vault-missing">Missing from Obsidian</option><option value="source-missing">Source missing</option></select></div></div><div id="doc-results"><div class="skeleton"></div></div>`;
  const load = async () => {
    const query = new URLSearchParams({ search: $("#doc-search").value, comparison_status: $("#doc-status").value, limit: "200" });
    if (reviewOnly) query.set("review", "true");
    const data = await api(`/api/v1/admin/documents?${query}`);
    const rows = data.items.map((doc) => `<tr>
      <td><div class="doc-title">${escapeHtml(doc.title || doc.source_name)}</div><div class="doc-path" title="${escapeHtml(doc.source_path)}">${escapeHtml(doc.source_path)}</div></td>
      <td>${escapeHtml(doc.agent_name)}<br><small>${escapeHtml(doc.root_name)}</small></td>
      <td>${(doc.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</td>
      <td>${comparisonBadge(doc.comparison_status)}<br><small>${escapeHtml(doc.status)}</small></td>
      <td>${Math.round((doc.confidence || 0) * 100)}%</td>
      <td>${reviewOnly ? `<button class="quiet approve" data-id="${doc.id}">Approve</button>` : ""}${doc.status === "error" ? `<button class="quiet retry" data-id="${doc.id}">Retry</button>` : ""}</td>
    </tr>`).join("");
    $("#doc-results").innerHTML = rows ? `<div class="table-wrap"><table><thead><tr><th>${headingWithHelp("Document", "The generated title and original file path.")}</th><th>${headingWithHelp("Source", "The paired computer and watched folder containing the original file.")}</th><th>${headingWithHelp("Tags", "Keywords assigned by the local model or deterministic fallback.")}</th><th>${headingWithHelp("State", "The current comparison between the source file and its managed Obsidian note.")}</th><th>${headingWithHelp("Confidence", "How confident Obsync is in the assigned classification. Items below the threshold enter Review.")}</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="panel empty"><div class="empty-icon">◇</div><p>Nothing here.</p></div>';
    $$(".approve").forEach((button) => button.addEventListener("click", async () => {
      await api(`/api/v1/admin/documents/${button.dataset.id}/approve`, { method: "POST" });
      toast("Classification approved.");
      await Promise.all([load(), refreshReviewBadge()]);
    }));
    $$(".retry").forEach((button) => button.addEventListener("click", async () => {
      await api(`/api/v1/admin/documents/${button.dataset.id}/retry`, { method: "POST" }); toast("Retry queued on the source device.");
    }));
  };
  let debounce;
  $("#doc-search").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(load, 250); });
  $("#doc-status").addEventListener("change", load);
  await load();
}

async function renderSettings() {
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
        <label class="vault-choice"><input type="radio" name="vault-mode" value="agent" ${vaultMode === "agent" ? "checked" : ""}><span><strong>Vault on a desktop</strong><small>Best when the vault is in Documents on Windows while the server runs elsewhere.</small></span>${helpIcon("A paired Companion performs safe vault reads and writes on the selected desktop.", "About a desktop vault")}</label>
      </div>
      <div id="local-vault-settings">
        <div class="field">${labelWithHelp("", settings.runtime === "docker" ? "Host vault folder" : "Vault folder", "The host folder currently mapped to the Obsync vault. Docker mounts are changed in Compose, not from a web browser.")}<input value="${escapeHtml(hostVault)}" disabled><small>${settings.runtime === "docker" ? `Inside Docker this is mounted as ${escapeHtml(settings.vault_path)}. Docker mounts can only be changed when the container is created.` : "This native Obsync process writes directly to this folder."}</small></div>
      </div>
      <div id="agent-vault-settings" hidden>
        <div class="field">${labelWithHelp("vault-agent", "Computer containing the vault", "Choose the online Companion that can access the real Obsidian vault folder.")}<select id="vault-agent"><option value="">${state.agents.length ? "Choose a paired computer…" : "No desktop computers paired"}</option>${vaultOptions}</select><small>The Overview computer count includes the Obsync server. A Windows PC appears here only after its desktop agent is paired and connected.</small></div>
        <div class="settings-actions"><button class="secondary" type="button" id="browse-vault" title="Open a native folder browser on the selected computer">Browse for vault on that computer</button><button class="quiet" type="button" id="add-vault-computer" title="Pair another desktop Companion">+ Add computer</button></div>
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

async function renderHelp() {
  $("#content").innerHTML = `
    <div class="help-layout">
      <section class="help-hero panel">
        <div><span class="eyebrow">START HERE</span><h2>From a folder to organized Obsidian notes</h2><p>Obsync uses one central server, a quiet Companion on each desktop, one Obsidian vault, and any number of watched folders.</p></div>
        <button class="primary" id="help-add-computer">Add a computer</button>
      </section>
      <section class="help-steps" aria-label="Quick start">
        <article class="help-step"><span>1</span><div><h3>Secure the server</h3><p>Create the local administrator username and password. The server stores settings and coordinates every computer.</p></div></article>
        <article class="help-step"><span>2</span><div><h3>Connect the vault computer</h3><p>Open <strong>Sources → Add another computer</strong>, download the Windows Companion, and enter the server, code, and computer name. It runs silently and starts at sign-in.</p></div></article>
        <article class="help-step"><span>3</span><div><h3>Select the vault</h3><p>In <strong>Settings → Obsidian vault</strong>, choose either a server-mounted vault or the connected desktop that contains your real vault.</p></div></article>
        <article class="help-step"><span>4</span><div><h3>Add folders</h3><p>On the Sources page, click <strong>Add folder</strong> on a connected computer. Choose any external folder you want Obsync to monitor.</p></div></article>
        <article class="help-step"><span>5</span><div><h3>Scan, inspect, then sync</h3><p>Scan compares every file without writing. View files shows the results. Sync changes extracts, organizes, and writes only what needs attention.</p></div></article>
      </section>
      <section class="help-grid">
        <article class="settings-card"><h3>What each page does</h3>
          <dl class="help-list">
            <div><dt>Overview</dt><dd>Health, totals, recent activity, and vault availability.</dd></div>
            <div><dt>Sources</dt><dd>Connect computers, add watched folders, compare files, and sync changes.</dd></div>
            <div><dt>Documents</dt><dd>Search every indexed source and inspect tags, confidence, and sync state.</dd></div>
            <div><dt>Review</dt><dd>Approve classifications that fall below the configured confidence threshold.</dd></div>
            <div><dt>Settings</dt><dd>Select the vault, connect a local model, and adjust review behavior.</dd></div>
          </dl>
        </article>
        <article class="settings-card"><h3>File status colors</h3>
          <div class="help-status">${comparisonBadge("in-sync")}<p>The source hash matches the managed note in Obsidian.</p></div>
          <div class="help-status">${comparisonBadge("modified")}<p>The source or its managed note changed and should be synced again.</p></div>
          <div class="help-status">${comparisonBadge("new")}<p>The source has not been written to Obsidian yet.</p></div>
          <div class="help-status">${comparisonBadge("vault-missing")}<p>The source is known, but its expected managed note is missing.</p></div>
          <div class="help-status">${comparisonBadge("source-missing")}<p>The original file disappeared. Obsync keeps the note and marks it missing.</p></div>
        </article>
        <article class="settings-card"><h3>Windows Companion</h3><p>The Companion is the safe bridge between Docker and folders stored on Windows. Installation is per-user, needs no Administrator rights, creates an automatic sign-in task, and leaves no PowerShell window open.</p><p>If a computer shows offline, open the downloaded Companion again. It can repair the automatic-start entry using the existing pairing.</p></article>
        <article class="settings-card"><h3>Server vs. desktop</h3><p>The central server always appears as one connected computer. Docker can access only folders mounted into its container. Pair the physical desktop whenever the vault or source folders live in Windows Documents, another user folder, a Mac, or a different PC.</p></article>
        <article class="settings-card"><h3>Local AI</h3><p>AI is optional. Choose Ollama, LM Studio, or an OpenAI-compatible endpoint, enter the exact model name, then use <strong>Check connection</strong>. The check lists models quickly without starting inference. If AI is offline, deterministic organization keeps syncing.</p></article>
        <article class="settings-card"><h3>Safety</h3><p>Obsync never edits, moves, or deletes source files. It owns only marked generated sections in Markdown and preserves text under <strong>My notes</strong>. Missing files are recorded rather than propagated as deletions.</p></article>
      </section>
      <section class="settings-card help-troubleshooting"><h3>Troubleshooting</h3>
        <details><summary>A Windows PC does not appear in a selector</summary><p>Confirm the Companion setup window reported success and the Sources card says online. The Overview count includes the central server, which is not a desktop selector option.</p></details>
        <details><summary>Windows warns that the Companion is unrecognized</summary><p>Early community builds are not code-signed. Confirm the file came from the official Obsync GitHub release before choosing <strong>More info → Run anyway</strong> in Windows SmartScreen.</p></details>
        <details><summary>The folder browser does not open</summary><p>The selected desktop must be online. Open the Companion again to repair automatic startup, then retry Add folder or Browse for vault.</p></details>
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
  return {
    vault_mode: $('input[name="vault-mode"]:checked')?.value || "local",
    vault_agent_id: $("#vault-agent")?.value || "",
    llm_enabled: $("#llm-enabled").checked,
    llm_provider: $("#llm-provider").value,
    llm_base_url: $("#llm-url").value.trim(),
    llm_model: $("#llm-model").value.trim(),
    llm_api_key: $("#llm-key").value,
    review_threshold: $("#review-threshold").value,
    llm_timeout_seconds: $("#llm-timeout").value,
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
$("#theme-button").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("#help-button").addEventListener("click", () => navigate("help"));
$("#refresh-button").addEventListener("click", () => navigate(state.view));
$("#menu-button").addEventListener("click", () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#menu-button").setAttribute("aria-expanded", String(open));
});
$("#modal-close").addEventListener("click", () => $("#modal").close());
$$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));

const storedTheme = localStorage.getItem("obsync_theme");
setTheme(storedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
bootstrapAuth();
