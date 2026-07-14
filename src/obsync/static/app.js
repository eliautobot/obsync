const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  authMode: "login",
  session: null,
  view: "overview",
  overview: null,
  agents: [],
  roots: [],
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
  $("#logout-button").textContent = (session?.username || "O").slice(0, 1).toUpperCase();
  $("#logout-button").setAttribute(
    "aria-label",
    temporary ? "Secure administrator account" : "Sign out",
  );
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
      } else if (status.legacy_migration_required) {
        showSetup(true);
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
      ${statCard("Connected devices", `${stats.online_agents}/${stats.agents}`, "◎")}
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
  const [agentData, rootData] = await Promise.all([
    api("/api/v1/admin/agents"), api("/api/v1/admin/roots"),
  ]);
  state.agents = agentData.items;
  state.roots = rootData.items;
  const cards = state.agents.map((agent) => {
    const roots = state.roots.filter((root) => root.agent_id === agent.id);
    const rootRows = roots.map((root) => `<div class="root-row"><strong>${escapeHtml(root.name)} · ${root.document_count || 0} docs</strong><code>${escapeHtml(root.path)}</code></div>`).join("");
    return `<article class="source-card">
      <div class="source-top"><span class="device-icon">${agent.os_name === "Windows" ? "▣" : "◫"}</span><div><h3>${escapeHtml(agent.name)}</h3><p>${escapeHtml(agent.os_name)} · seen ${relativeTime(agent.last_seen_at)}</p></div><span class="status-pill ${agent.status}">${agent.status}</span></div>
      ${rootRows || '<div class="root-row">No watched folders registered yet.</div>'}
      <div class="source-stats"><span>${agent.document_count || 0} documents</span><button class="quiet scan-device" data-agent="${agent.id}">Scan now</button></div>
    </article>`;
  }).join("") || '<div class="empty"><div class="empty-icon">◎</div><p>No devices yet. Pair your first computer to begin.</p></div>';
  $("#content").innerHTML = `<div class="section-head"><div><h2>Connected devices</h2><p>Agents connect outbound to this server and never expose watched PCs.</p></div><button class="primary" id="add-device">+ Add device</button></div><div class="source-grid">${cards}</div>`;
  $("#add-device").addEventListener("click", openEnrollment);
  $$(".scan-device").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/api/v1/admin/agents/${button.dataset.agent}/scan`, { method: "POST" }); toast("Scan queued. The device will begin within 30 seconds."); }
    catch (error) { toast(error.message, true); }
  }));
}

async function openEnrollment() {
  const modal = $("#modal");
  $("#modal-title").textContent = "Add a computer";
  $("#modal-body").innerHTML = '<p class="modal-note">Name this device so it is easy to recognize.</p><div class="field"><label>Device label</label><input id="device-label" placeholder="Office PC"></div><br><button class="primary full" id="create-code">Create pairing code</button>';
  modal.showModal();
  $("#create-code").addEventListener("click", async () => {
    try {
      const label = $("#device-label").value;
      const enrollment = await api("/api/v1/admin/enrollments", { method: "POST", body: { label } });
      const server = location.origin;
      $("#modal-body").innerHTML = `
        <p class="modal-note">This one-time code expires in 20 minutes.</p><div class="pair-code">${escapeHtml(enrollment.code)}</div>
        <p><strong>1.</strong> Install Obsync Agent on the other computer.</p>
        <p><strong>2.</strong> Pair it:</p><div class="code-block">obsync agent pair --server ${escapeHtml(server)} --code ${escapeHtml(enrollment.code)} --name "${escapeHtml(label || "My PC")}"</div>
        <p><strong>3.</strong> Add any folder and start:</p><div class="code-block">obsync agent add-folder "PATH_TO_FOLDER"<br>obsync agent run</div>
        <p class="modal-note">The agent makes outbound HTTPS requests only. Repeat this for every Windows, Linux, macOS, NAS, or network-share host.</p>`;
    } catch (error) { toast(error.message, true); }
  });
}

function statusClass(status) {
  return ["error", "processing"].includes(status) ? status : "";
}

async function renderDocuments(reviewOnly) {
  const title = reviewOnly ? "Review queue" : "Knowledge documents";
  $("#content").innerHTML = `<div class="section-head"><div><h2>${title}</h2><p>${reviewOnly ? "Approve classifications that fell below your confidence threshold." : "Every generated note and its current sync state."}</p></div></div><div class="toolbar"><div class="search"><input id="doc-search" placeholder="Search title, source, or summary…"></div><select id="doc-status"><option value="">All states</option><option value="synced">Synced</option><option value="error">Errors</option><option value="processing">Processing</option></select></div><div id="doc-results"><div class="skeleton"></div></div>`;
  const load = async () => {
    const query = new URLSearchParams({ search: $("#doc-search").value, status: $("#doc-status").value, limit: "200" });
    if (reviewOnly) query.set("review", "true");
    const data = await api(`/api/v1/admin/documents?${query}`);
    const rows = data.items.map((doc) => `<tr>
      <td><div class="doc-title">${escapeHtml(doc.title || doc.source_name)}</div><div class="doc-path" title="${escapeHtml(doc.source_path)}">${escapeHtml(doc.source_path)}</div></td>
      <td>${escapeHtml(doc.agent_name)}<br><small>${escapeHtml(doc.root_name)}</small></td>
      <td>${(doc.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</td>
      <td><span class="state ${statusClass(doc.status)}">${escapeHtml(doc.status)}</span>${doc.missing ? '<br><small>source missing</small>' : ""}</td>
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
  const settings = await api("/api/v1/admin/settings");
  const enabled = settings.llm_enabled === "true";
  $("#content").innerHTML = `<form class="settings-layout" id="settings-form">
    <section class="settings-card"><h3>Obsidian vault</h3><p>The container path where generated Markdown is written.</p><div class="field"><label>Mounted vault path</label><input value="${escapeHtml(settings.vault_path)}" disabled><small>Change the Docker volume mount to point at a different vault.</small></div></section>
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
      <div class="settings-actions"><button class="primary" type="submit">Save settings</button><button class="secondary" type="button" id="test-llm">Test connection</button></div>
    </section>
    <section class="settings-card"><h3>Safety defaults</h3><p>Obsync never edits, moves, or deletes source files. Missing sources are marked in their note and kept. Manual text below “My notes” is preserved on every update.</p></section>
  </form>`;
  $("#llm-provider").value = settings.llm_provider || "ollama";
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await api("/api/v1/admin/settings", { method: "PUT", body: settingsPayload() }); toast("Settings saved."); }
    catch (error) { toast(error.message, true); }
  });
  $("#test-llm").addEventListener("click", async () => {
    const button = $("#test-llm"); button.disabled = true; button.textContent = "Testing…";
    try { const result = await api("/api/v1/admin/settings/test-llm", { method: "POST", body: settingsPayload() }); toast(result.message, !result.ok); }
    catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = "Test connection"; }
  });
}

function settingsPayload() {
  return {
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
$("#logout-button").addEventListener("click", async () => {
  if (state.session?.temporary) {
    await openSecuritySetup();
    return;
  }
  try { await api("/api/v1/auth/logout", { method: "POST" }); }
  catch (_error) { /* A missing/expired session still means the user is signed out. */ }
  showLogin();
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
