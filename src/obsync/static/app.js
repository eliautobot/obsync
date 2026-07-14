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
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
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
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 4000);
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
      <div class="field"><label for="account-username">Username</label><input id="account-username" autocomplete="username" maxlength="64" value="${escapeHtml(state.session?.username || "admin")}" required></div>
      <div class="field"><label for="account-current-password">Current password</label><input id="account-current-password" type="password" autocomplete="current-password" required></div>
      <div class="field"><label for="account-new-password">New password <small>(optional)</small></label><input id="account-new-password" type="password" autocomplete="new-password" placeholder="Leave blank to keep current password"></div>
      <div class="field"><label for="account-confirm-password">Confirm new password</label><input id="account-confirm-password" type="password" autocomplete="new-password"></div>
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
      <div class="field"><label for="secure-username">Username</label><input id="secure-username" autocomplete="username" maxlength="64" value="admin" required></div>
      <div class="field"><label for="secure-password">Password</label><input id="secure-password" type="password" autocomplete="new-password" placeholder="At least 10 characters" required></div>
      <div class="field"><label for="secure-confirm">Confirm password</label><input id="secure-confirm" type="password" autocomplete="new-password" placeholder="Repeat your password" required></div>
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
  } catch (error) {
    $("#content").innerHTML = `<div class="empty"><div class="empty-icon">!</div><p>${escapeHtml(error.message)}</p><button class="secondary" id="try-again">Try again</button></div>`;
    $("#try-again")?.addEventListener("click", () => navigate(view));
  }
}

function statCard(label, value, icon, style = "") {
  return `<article class="stat-card ${style}"><span class="label">${label}</span><span class="stat-icon">${icon}</span><strong>${value}</strong></article>`;
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
      <div class="field"><label for="folder-name">Folder name <small>(optional)</small></label><input id="folder-name" placeholder="Client files"></div>
      <div class="field"><label for="folder-destination">Obsidian destination</label><input id="folder-destination" value="Obsync" required><small>Generated notes stay under this folder in the vault.</small></div>
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
      ${statCard("Synced documents", stats.synced, "↗", "good")}
      ${statCard("Connected computers", `${stats.online_computers}/${stats.computers}`, "◎")}
      ${statCard("Needs review", stats.review, "◇", stats.review ? "warn" : "")}
      ${statCard("Errors", stats.errors, "!", stats.errors ? "bad" : "")}
    </div>
    <div class="grid-two">
      <section class="panel"><div class="panel-head"><h3>Recent activity</h3><button class="quiet" data-go="documents">View all</button></div><ul class="event-list">${events}</ul></section>
      <section class="panel"><div class="panel-head"><h3>System</h3></div>
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
      <div class="root-title"><strong>${escapeHtml(root.name)}</strong><span>${root.file_count || root.document_count || 0} files</span></div>
      <code>${escapeHtml(root.path)}</code>
      <div class="root-comparison">
        ${comparisonBadge("in-sync", root.in_sync_count || 0)}
        ${comparisonBadge("modified", root.modified_count || 0)}
        ${comparisonBadge("new", root.new_count || 0)}
        ${comparisonBadge("vault-missing", root.missing_count || 0)}
        ${root.checking_count ? comparisonBadge("checking", root.checking_count) : ""}
      </div>
      <div class="root-actions"><button class="quiet view-root" data-root="${root.id}">View files</button><button class="quiet scan-root" data-root="${root.id}">Scan</button><button class="secondary sync-root" data-root="${root.id}">Sync changes</button></div>
    </div>`).join("");
    return `<article class="source-card">
      <div class="source-top"><span class="device-icon">${agent.os_name === "Windows" ? "▣" : "◫"}</span><div><h3>${escapeHtml(agent.name)}</h3><p>${escapeHtml(agent.os_name)} · seen ${relativeTime(agent.last_seen_at)}</p>${agent.vault_ready ? '<span class="device-role">VAULT WRITER READY</span>' : ""}</div><span class="status-pill ${agent.status}">${agent.status}</span></div>
      ${rootRows || '<div class="root-row">No watched folders registered yet.</div>'}
      ${agent.vault_path ? `<div class="root-row"><strong>Obsidian vault</strong><code>${escapeHtml(agent.vault_path)}</code></div>` : ""}
      <div class="source-stats"><span>${agent.document_count || 0} files indexed</span><button class="primary add-folder" data-agent="${agent.id}">+ Add folder</button></div>
    </article>`;
  }).join("");
  const serverPath = server.vault_host_path || server.vault_path;
  const serverCard = `<article class="source-card server-card">
    <div class="source-top"><span class="device-icon">◆</span><div><h3>${escapeHtml(server.name)}</h3><p>${server.container ? "Docker container" : escapeHtml(server.os_name)} · ${escapeHtml(server.hostname)}</p><span class="device-role">ALWAYS CONNECTED</span></div><span class="status-pill">online</span></div>
    <div class="root-row"><strong>Server vault mount</strong><code>${escapeHtml(serverPath)}</code></div>
    <div class="source-stats"><span>Processes files and coordinates every desktop</span></div>
  </article>`;
  const emptyHelp = state.agents.length ? "" : '<div class="panel source-help"><strong>1 connected computer means this Obsync server.</strong><p>Pair the Windows, Linux, or macOS computer that contains your source folders. It will then appear here and in the vault computer selector.</p></div>';
  $("#content").innerHTML = `<div class="section-head"><div><h2>Computers and folders</h2><p>Add folders, scan them against Obsidian, inspect differences, then sync only what changed.</p></div><button class="primary" id="add-device">+ Add another computer</button></div>${emptyHelp}<div class="source-grid">${serverCard}${cards}</div>`;
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

function powerShellQuote(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
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
  toast("Command copied.");
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
    <p class="modal-note">The Obsync server is already connected. Use this only to add a Windows, Linux, or macOS computer whose folders are outside the server.</p>
    <div class="field"><label for="device-label">Device label</label><input id="device-label" placeholder="Office PC"></div>
    <div class="field"><label for="pair-server-url">Server address reachable from that computer</label><input id="pair-server-url" value="${escapeHtml(suggestedServer)}"><small>Do not use localhost for a different computer.</small></div>
    <label class="check-row"><input id="device-has-vault" type="checkbox"> This computer contains the Obsidian vault</label>
    <button class="primary full" id="create-code">Create Windows setup command</button>`;
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
      const download = "https://github.com/eliautobot/obsync/releases/latest/download/obsync-agent-windows-x64.exe";
      const lines = [
        `$exe = Join-Path $env:USERPROFILE 'Downloads\\obsync-agent-windows-x64.exe'`,
        `Invoke-WebRequest -Uri ${powerShellQuote(download)} -OutFile $exe`,
        `& $exe agent pair --server ${powerShellQuote(server)} --code ${powerShellQuote(enrollment.code)} --name ${powerShellQuote(label)}`,
      ];
      if (hasVault) lines.push("& $exe agent set-vault --browse");
      lines.push("& $exe agent run");
      const command = lines.join("\n");
      $("#modal-body").innerHTML = `
        <p class="modal-note">This one-time code expires in 20 minutes.</p><div class="pair-code">${escapeHtml(enrollment.code)}</div>
        <div class="pair-steps">
          <div class="pair-step"><p><strong>1.</strong> On the Windows PC, open PowerShell.</p></div>
          <div class="pair-step"><p><strong>2.</strong> Paste this complete setup command. It downloads the agent, pairs this computer, and keeps it connected.</p><div class="code-block" id="pair-command">${escapeHtml(command)}</div><button class="secondary copy-command" id="copy-pair-command" type="button">Copy setup command</button></div>
          <div class="pair-step"><p><strong>3.</strong> Keep that PowerShell window open during this alpha release.</p></div>
        </div>
        <p class="inline-status" id="pairing-status"><span class="connection-wait"><i></i>Waiting for ${escapeHtml(label)} to connect…</span></p>
        <p class="modal-note">Prefer a manual download? <a href="${download}" target="_blank" rel="noreferrer">Download the Windows agent</a>. Linux users can install the Python package and use the same <code>agent pair</code> command.</p>`;
      $("#copy-pair-command").addEventListener("click", () => copyText(command));
      pollEnrollment(enrollment.id);
    } catch (error) { toast(error.message, true); }
  });
}

function statusClass(status) {
  return ["error", "processing"].includes(status) ? status : "";
}

async function renderDocuments(reviewOnly) {
  const title = reviewOnly ? "Review queue" : "Knowledge documents";
  $("#content").innerHTML = `<div class="section-head"><div><h2>${title}</h2><p>${reviewOnly ? "Approve classifications that fell below your confidence threshold." : "Every source file and how it compares with Obsidian."}</p></div></div><div class="toolbar"><div class="search"><input id="doc-search" placeholder="Search title, source, or summary…"></div><select id="doc-status"><option value="">All comparisons</option><option value="in-sync">In Obsidian</option><option value="modified">Modified</option><option value="new">New</option><option value="vault-missing">Missing from Obsidian</option><option value="source-missing">Source missing</option></select></div><div id="doc-results"><div class="skeleton"></div></div>`;
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
    $("#doc-results").innerHTML = rows ? `<div class="table-wrap"><table><thead><tr><th>Document</th><th>Source</th><th>Tags</th><th>State</th><th>Confidence</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="panel empty"><div class="empty-icon">◇</div><p>Nothing here.</p></div>';
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
    <section class="settings-card"><h3>Obsidian vault</h3><p>Choose where Obsync writes generated Markdown. The server mount is the default; a paired desktop can write directly to a vault on Windows or another computer.</p>
      <div class="vault-mode-grid">
        <label class="vault-choice"><input type="radio" name="vault-mode" value="local" ${vaultMode === "local" ? "checked" : ""}><span><strong>Server-mounted vault</strong><small>Best when the vault is mounted into Docker or Obsync runs natively beside it.</small></span></label>
        <label class="vault-choice"><input type="radio" name="vault-mode" value="agent" ${vaultMode === "agent" ? "checked" : ""}><span><strong>Vault on a desktop</strong><small>Best when the vault is in Documents on Windows while the server runs elsewhere.</small></span></label>
      </div>
      <div id="local-vault-settings">
        <div class="field"><label>${settings.runtime === "docker" ? "Host vault folder" : "Vault folder"}</label><input value="${escapeHtml(hostVault)}" disabled><small>${settings.runtime === "docker" ? `Inside Docker this is mounted as ${escapeHtml(settings.vault_path)}. Docker mounts can only be changed when the container is created.` : "This native Obsync process writes directly to this folder."}</small></div>
      </div>
      <div id="agent-vault-settings" hidden>
        <div class="field"><label for="vault-agent">Computer containing the vault</label><select id="vault-agent"><option value="">${state.agents.length ? "Choose a paired computer…" : "No desktop computers paired"}</option>${vaultOptions}</select><small>The Overview computer count includes the Obsync server. A Windows PC appears here only after its desktop agent is paired and connected.</small></div>
        <div class="settings-actions"><button class="secondary" type="button" id="browse-vault">Browse for vault on that computer</button><button class="quiet" type="button" id="add-vault-computer">+ Add computer</button></div>
        <p class="inline-status" id="vault-agent-status">${state.agents.length ? "Choose a paired computer." : "The Obsync server is connected, but no desktop computer is paired yet."}</p>
      </div>
    </section>
    <section class="settings-card"><h3>Local AI organization</h3><p>Use Ollama, LM Studio, or another OpenAI-compatible local server. Obsync keeps syncing with deterministic rules if the model is offline.</p>
      <div class="field-grid">
        <label class="check-row full-width"><input id="llm-enabled" type="checkbox" ${enabled ? "checked" : ""}> Enable AI classification</label>
        <div class="field"><label>Provider</label><select id="llm-provider"><option value="ollama">Ollama</option><option value="lmstudio">LM Studio</option><option value="openai-compatible">OpenAI-compatible</option></select></div>
        <div class="field"><label>Model</label><input id="llm-model" value="${escapeHtml(settings.llm_model)}" placeholder="qwen3:8b"></div>
        <div class="field full-width"><label>Base URL</label><input id="llm-url" value="${escapeHtml(settings.llm_base_url)}" placeholder="http://host.docker.internal:11434"><small>From Docker, host.docker.internal usually reaches an LLM running on the host.</small></div>
        <div class="field full-width"><label>API key (optional)</label><input id="llm-key" type="password" value="" placeholder="${settings.llm_api_key === "configured" ? "Configured — leave blank to keep" : "Not required for default Ollama/LM Studio"}"></div>
        <div class="field"><label>Review below</label><input id="review-threshold" type="number" min="0" max="1" step="0.05" value="${escapeHtml(settings.review_threshold || ".65")}"><small>0.65 means below 65% confidence.</small></div>
        <div class="field"><label>Model timeout (seconds)</label><input id="llm-timeout" type="number" min="5" max="600" value="${escapeHtml(settings.llm_timeout_seconds || "120")}"></div>
      </div>
      <div class="settings-actions"><button class="primary" type="submit">Save settings</button><button class="secondary" type="button" id="test-llm">Check connection</button></div>
      <p class="inline-status" id="llm-test-status" hidden></p>
    </section>
    <section class="settings-card"><h3>Safety defaults</h3><p>Obsync never edits, moves, or deletes source files. Missing sources are marked in their note and kept. Manual text below “My notes” is preserved on every update.</p></section>
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
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeAccountMenu();
});
$("#secure-admin-button").addEventListener("click", openSecuritySetup);
$("#theme-button").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
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
