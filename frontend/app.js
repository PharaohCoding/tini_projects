const API_BASE = "http://127.0.0.1:8000";

const $ = (id) => document.getElementById(id);

const banner = $("banner");
const pageList = $("page-list");
const pageProject = $("page-project");
const projectListEl = $("project-list");
const messagesEl = $("messages");
const runningCards = $("running-cards");
const queuedCards = $("queued-cards");
const otherWorkers = $("other-workers");
const workerDetail = $("worker-detail");
const contextTree = $("context-tree");
const filePane = $("file-pane");
const filePathEl = $("file-path");
const fileEditor = $("file-editor");
const fileView = $("file-view");
const fileSave = $("file-save");
const attachPending = $("attach-pending");
const importPane = $("import-pane");
const chatInput = $("chat-input");

const ROLE_LABEL = {
  user: "用户",
  coordinator: "Coordinator",
  system: "系统",
};

let currentProjectId = null;
let eventSource = null;
let pendingAttachmentIds = [];
let workersById = {};
let workersList = [];
let lastScheduler = null;
let currentFilePath = null;

function showBanner(text) {
  if (!text) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = text;
}

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "className") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function listFrom(data, ...keys) {
  if (Array.isArray(data)) return data;
  if (data && typeof data === "object") {
    for (const key of keys) {
      if (Array.isArray(data[key])) return data[key];
    }
  }
  return [];
}

function unfinishedCount(project) {
  const keys = [
    "unfinished_workers",
    "unfinished_worker_count",
    "open_workers",
    "pending_worker_count",
  ];
  for (const key of keys) {
    if (typeof project[key] === "number") return project[key];
  }
  return null;
}

async function api(method, path, body, isForm) {
  const opts = { method, headers: {} };
  if (isForm) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(API_BASE + "/api" + path, opts);
  } catch (err) {
    throw new Error("无法连接 " + API_BASE + "（" + err.message + "）");
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text, raw: text };
    }
  }
  if (!res.ok) {
    const msg = (data && data.error) || res.status + " " + res.statusText;
    throw new Error(msg);
  }
  return data;
}

function parseRoute() {
  const hash = (location.hash || "#/").replace(/^#/, "");
  const match = hash.match(/^\/p\/([^/?]+)/);
  if (match) return { page: "project", id: decodeURIComponent(match[1]) };
  return { page: "list" };
}

function closeEvents() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function connectEvents(projectId) {
  closeEvents();
  const url = API_BASE + "/api/projects/" + encodeURIComponent(projectId) + "/events";
  eventSource = new EventSource(url);

  const onAny = (ev) => {
    loadScheduler(projectId);
    loadWorkers(projectId);
    const name = ev && ev.type ? ev.type : "";
    if (name === "message" || name.startsWith("message")) {
      loadMessages(projectId);
    }
    if (name === "context" || name.startsWith("context")) {
      loadContext(projectId);
    }
  };

  [
    "message",
    "worker",
    "scheduler",
    "context",
    "worker.queued",
    "worker.started",
    "worker.done",
    "scheduler.updated",
  ].forEach((type) => {
    eventSource.addEventListener(type, onAny);
  });

  eventSource.onmessage = () => {
    loadScheduler(projectId);
    loadWorkers(projectId);
    loadMessages(projectId);
    loadContext(projectId);
  };
}

async function loadList() {
  pageList.hidden = false;
  pageProject.hidden = true;
  closeEvents();
  currentProjectId = null;
  try {
    const data = await api("GET", "/projects");
    const projects = listFrom(data, "projects", "items");
    showBanner("");
    renderProjectList(projects);
  } catch (err) {
    showBanner(err.message);
    projectListEl.replaceChildren(
      el("p", { className: "empty" }, "暂时无法读取列表。确认后端已在 " + API_BASE + " 监听。")
    );
  }
}

function renderProjectList(projects) {
  if (!projects.length) {
    projectListEl.replaceChildren(el("p", { className: "empty" }, "还没有 Project。先在左侧新建。"));
    return;
  }
  projectListEl.replaceChildren(
    ...projects.map((p) => {
      const count = unfinishedCount(p);
      const status = p.status || "active";
      const meta = [
        el("span", { className: "badge status-" + status }, status),
      ];
      if (count != null) {
        meta.push(el("span", { className: "badge" }, "未完成 Worker " + count));
      }
      return el(
        "a",
        { className: "project-card", href: "#/p/" + encodeURIComponent(p.id) },
        el("h3", { text: p.name || p.id }),
        el("p", { className: "goal" }, p.goal || ""),
        el("div", { className: "meta" }, ...meta)
      );
    })
  );
}

async function openProject(projectId) {
  currentProjectId = projectId;
  pageList.hidden = true;
  pageProject.hidden = false;
  pendingAttachmentIds = [];
  workersById = {};
  workersList = [];
  lastScheduler = null;
  currentFilePath = null;
  renderPendingAttachments();
  importPane.hidden = true;
  filePane.hidden = true;
  workerDetail.hidden = true;
  messagesEl.replaceChildren(el("p", { className: "empty" }, "加载消息…"));
  runningCards.replaceChildren();
  queuedCards.replaceChildren();
  otherWorkers.replaceChildren();
  contextTree.replaceChildren();

  try {
    const project = await api("GET", "/projects/" + encodeURIComponent(projectId));
    $("proj-name").textContent = project.name || projectId;
    $("proj-goal").textContent = project.goal || "";
    const status = project.status || "active";
    $("proj-status").textContent = status;
    $("proj-status").className = "badge status-" + status;
    showBanner("");
  } catch (err) {
    $("proj-name").textContent = projectId;
    $("proj-goal").textContent = "";
    showBanner(err.message);
  }

  await Promise.all([
    loadMessages(projectId),
    loadContext(projectId),
    loadScheduler(projectId),
    loadWorkers(projectId),
  ]);
  connectEvents(projectId);
}

async function loadMessages(projectId) {
  if (projectId !== currentProjectId) return;
  try {
    const data = await api("GET", "/projects/" + encodeURIComponent(projectId) + "/messages");
    const messages = listFrom(data, "messages", "items");
    renderMessages(messages);
  } catch (err) {
    showBanner(err.message);
  }
}

function renderMessages(messages) {
  if (!messages.length) {
    messagesEl.replaceChildren(el("p", { className: "empty" }, "还没有消息。向 Coordinator 打个招呼，或点「演示调度」。"));
    return;
  }
  messagesEl.replaceChildren(
    ...messages.map((m) => {
      const role = m.role || "coordinator";
      return el(
        "article",
        { className: "bubble " + role },
        el("div", { className: "who" }, ROLE_LABEL[role] || role),
        el("div", { className: "body" }, m.content || m.text || "")
      );
    })
  );
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function loadContext(projectId) {
  if (projectId !== currentProjectId) return;
  try {
    const data = await api("GET", "/projects/" + encodeURIComponent(projectId) + "/context");
    const nodes = listFrom(data, "tree", "entries", "children", "items");
    let roots = nodes;
    if (!roots.length && data && typeof data === "object") {
      if (data.tree && typeof data.tree === "object") roots = [data.tree];
      else if (data.path != null || data.name != null) roots = [data];
    }
    renderTree(roots);
  } catch (err) {
    contextTree.replaceChildren(el("p", { className: "empty" }, err.message));
  }
}

function isDir(node) {
  if (!node || typeof node !== "object") return false;
  if (node.type === "file" || node.is_dir === false) return false;
  if (node.type === "dir" || node.type === "directory" || node.is_dir === true) return true;
  return Array.isArray(node.children) || Array.isArray(node.entries);
}

function nodePath(node) {
  return node.path || node.relpath || node.name || "";
}

function renderTree(nodes) {
  if (!nodes.length) {
    contextTree.replaceChildren(el("p", { className: "empty" }, "Context 为空。"));
    return;
  }

  const walk = (list) => {
    const ul = el("ul");
    for (const node of list) {
      const li = el("li");
      const path = nodePath(node);
      const name = node.name || path;
      if (isDir(node)) {
        const kids = listFrom(node, "children", "entries");
        const details = el("details", { open: true }, el("summary", { text: name || path || "/" }));
        if (kids.length) details.append(walk(kids));
        li.append(details);
      } else {
        const btn = el("button", {
          type: "button",
          className: "file" + (path === currentFilePath ? " active" : ""),
          dataset: { path },
          text: name,
          onclick: () => openContextFile(path),
        });
        li.append(btn);
      }
      ul.append(li);
    }
    return ul;
  };

  contextTree.replaceChildren(walk(nodes));
}

async function openContextFile(path) {
  if (!currentProjectId || !path) return;
  currentFilePath = path;
  filePane.hidden = false;
  filePathEl.textContent = path;
  fileSave.hidden = true;
  fileEditor.hidden = true;
  fileView.hidden = false;
  fileView.textContent = "加载中…";
  contextTree.querySelectorAll(".file").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.path === path);
  });

  const url =
    API_BASE +
    "/api/projects/" +
    encodeURIComponent(currentProjectId) +
    "/context/file?path=" +
    encodeURIComponent(path);
  try {
    const res = await fetch(url);
    const ctype = (res.headers.get("content-type") || "").toLowerCase();
    const raw = await res.text();
    let payload = null;
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = null;
      }
    }
    if (!res.ok) {
      throw new Error((payload && payload.error) || raw || res.statusText);
    }
    const text =
      payload && typeof payload === "object"
        ? payload.content ?? payload.text ?? raw
        : raw;
    const looksBinary =
      ctype.includes("octet-stream") ||
      ctype.startsWith("image/") ||
      ctype.startsWith("audio/") ||
      ctype.startsWith("video/");
    if (looksBinary) {
      fileEditor.hidden = true;
      fileSave.hidden = true;
      fileView.hidden = false;
      fileView.textContent = "二进制文件，无法在此预览。路径：" + path;
      return;
    }
    fileEditor.hidden = false;
    fileSave.hidden = false;
    fileView.hidden = true;
    fileEditor.value = typeof text === "string" ? text : String(text ?? "");
  } catch (err) {
    fileEditor.hidden = true;
    fileSave.hidden = true;
    fileView.hidden = false;
    fileView.textContent = err.message;
  }
}

async function saveContextFile() {
  if (!currentProjectId || !currentFilePath) return;
  try {
    await api(
      "PUT",
      "/projects/" +
        encodeURIComponent(currentProjectId) +
        "/context/file?path=" +
        encodeURIComponent(currentFilePath),
      { content: fileEditor.value }
    );
    showBanner("");
    fileSave.textContent = "已保存";
    setTimeout(() => {
      fileSave.textContent = "保存";
    }, 1200);
  } catch (err) {
    showBanner(err.message);
  }
}

async function loadScheduler(projectId) {
  if (projectId !== currentProjectId) return;
  try {
    const snap = await api("GET", "/projects/" + encodeURIComponent(projectId) + "/scheduler");
    lastScheduler = snap;
    renderScheduler(snap);
  } catch (err) {
    showBanner(err.message);
  }
}

function renderScheduler(snap) {
  const running = Array.isArray(snap.running) ? snap.running : [];
  const queued = Array.isArray(snap.queued) ? snap.queued : [];
  const maxRunning = snap.max_running != null ? snap.max_running : 2;
  $("sched-running").textContent = String(running.length);
  $("sched-max").textContent = String(maxRunning);
  const policy = snap.policy || "";
  $("sched-policy").textContent = policy ? "策略 " + policy : "";

  if (!running.length) {
    runningCards.replaceChildren(el("p", { className: "empty" }, "没有 running。"));
  } else {
    runningCards.replaceChildren(
      ...running.map((item) => workerCard(item, { status: "running" }))
    );
  }

  if (!queued.length) {
    queuedCards.replaceChildren(el("p", { className: "empty" }, "队列为空。"));
  } else {
    queuedCards.replaceChildren(
      ...queued.map((item) => workerCard(item, { status: "queued" }))
    );
  }

  const shown = new Set([...running, ...queued].map((w) => w.id));
  const rest = workersList.filter((w) => w && w.id && !shown.has(w.id));
  if (!rest.length) {
    otherWorkers.replaceChildren(el("p", { className: "empty" }, "没有已结束的 Worker。"));
  } else {
    otherWorkers.replaceChildren(...rest.map((item) => workerCard(item, { status: item.status })));
  }
}

async function loadWorkers(projectId) {
  if (projectId !== currentProjectId) return;
  try {
    const data = await api("GET", "/projects/" + encodeURIComponent(projectId) + "/workers");
    const workers = listFrom(data, "workers", "items");
    const next = {};
    for (const w of workers) {
      if (w && w.id) next[w.id] = w;
    }
    workersById = next;
    workersList = workers;
    if (lastScheduler) renderScheduler(lastScheduler);
    else await loadScheduler(projectId);
  } catch (err) {
    showBanner(err.message);
  }
}

function workerCard(item, { status } = {}) {
  const fromList = (item.id && workersById[item.id]) || {};
  const merged = { ...fromList, ...item };
  const st = status || merged.status || "queued";
  const title = merged.title || merged.kind || merged.id;
  const card = el("article", { className: "wcard status-" + st });

  const headKids = [];
  if (st === "queued" && merged.position != null && merged.position !== "") {
    headKids.push(el("span", { className: "pos" }, "#" + merged.position));
  }
  headKids.push(el("span", { className: "title" }, title));
  card.append(el("header", null, ...headKids));

  const bits = [];
  if (merged.kind) bits.push(el("span", { className: "kind" }, merged.kind));
  if (merged.priority) bits.push(el("span", { className: "priority" }, merged.priority));
  if (merged.enqueue_seq != null) bits.push(el("span", { className: "seq" }, "seq " + merged.enqueue_seq));
  if (bits.length) card.append(el("div", { className: "row" }, ...bits));

  if (merged.schedule_reason) {
    card.append(el("div", { className: "reason" }, merged.schedule_reason));
  }
  if (merged.started_at) {
    card.append(el("div", { className: "kind" }, merged.started_at));
  }

  const actions = el("div", { className: "row" });
  const detailBtn = el("button", {
    type: "button",
    text: "详情",
    onclick: (ev) => {
      ev.stopPropagation();
      showWorkerDetail(merged.id);
    },
  });
  actions.append(detailBtn);
  if (st === "queued" || st === "running") {
    actions.append(
      el("button", {
        type: "button",
        className: "cancel",
        text: "取消",
        onclick: (ev) => {
          ev.stopPropagation();
          cancelWorker(merged.id);
        },
      })
    );
  }
  card.append(actions);
  return card;
}

async function showWorkerDetail(wid) {
  if (!currentProjectId || !wid) return;
  workerDetail.hidden = false;
  workerDetail.replaceChildren(el("p", { className: "empty" }, "加载 Worker…"));
  try {
    const data = await api(
      "GET",
      "/projects/" + encodeURIComponent(currentProjectId) + "/workers/" + encodeURIComponent(wid)
    );
    const w = data.worker || data;
    const parts = [
      el("p", null, (w.title || w.kind || w.id) + " · " + (w.status || "")),
      w.assignment ? el("pre", null, w.assignment) : null,
      w.result_summary ? el("p", null, w.result_summary) : null,
      w.report_path ? el("p", null, "报告：" + w.report_path) : null,
      w.schedule_reason ? el("div", { className: "reason" }, w.schedule_reason) : null,
      w.error ? el("p", null, w.error) : null,
    ];
    workerDetail.replaceChildren(...parts.filter(Boolean));
    if (w.report_path) openContextFile(w.report_path);
  } catch (err) {
    workerDetail.replaceChildren(el("p", { className: "empty" }, err.message));
  }
}

async function cancelWorker(wid) {
  if (!currentProjectId || !wid) return;
  try {
    await api(
      "POST",
      "/projects/" + encodeURIComponent(currentProjectId) + "/workers/" + encodeURIComponent(wid) + "/cancel",
      {}
    );
    await loadScheduler(currentProjectId);
    await loadWorkers(currentProjectId);
  } catch (err) {
    showBanner(err.message);
  }
}

function renderPendingAttachments() {
  if (!pendingAttachmentIds.length) {
    attachPending.replaceChildren();
    return;
  }
  attachPending.replaceChildren(
    ...pendingAttachmentIds.map((id, idx) =>
      el(
        "span",
        { className: "chip" },
        id,
        el("button", {
          type: "button",
          text: "×",
          onclick: () => {
            pendingAttachmentIds.splice(idx, 1);
            renderPendingAttachments();
          },
        })
      )
    )
  );
}

async function uploadFile(file) {
  if (!currentProjectId || !file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const data = await api(
      "POST",
      "/projects/" + encodeURIComponent(currentProjectId) + "/attachments",
      fd,
      true
    );
    const id = data.id || data.attachment_id || (data.attachment && data.attachment.id);
    if (id) pendingAttachmentIds.push(id);
    renderPendingAttachments();
    await loadContext(currentProjectId);
    showBanner("");
  } catch (err) {
    showBanner(err.message);
  }
}

async function importTranscript() {
  const text = $("import-text").value.trim();
  if (!currentProjectId || !text) return;
  try {
    await api("POST", "/projects/" + encodeURIComponent(currentProjectId) + "/attached-chats", {
      content: text,
    });
    $("import-text").value = "";
    importPane.hidden = true;
    await loadContext(currentProjectId);
    showBanner("");
  } catch (err) {
    showBanner(err.message);
  }
}

async function sendChat(ev) {
  ev.preventDefault();
  if (!currentProjectId) return;
  const content = chatInput.value.trim();
  if (!content) return;
  const attachment_ids = pendingAttachmentIds.slice();
  chatInput.value = "";
  pendingAttachmentIds = [];
  renderPendingAttachments();
  messagesEl.append(
    el(
      "article",
      { className: "bubble user" },
      el("div", { className: "who" }, "用户"),
      el("div", { className: "body" }, content)
    )
  );
  messagesEl.scrollTop = messagesEl.scrollHeight;
  try {
    const body = { content };
    if (attachment_ids.length) body.attachment_ids = attachment_ids;
    await api("POST", "/projects/" + encodeURIComponent(currentProjectId) + "/messages", body);
    await loadMessages(currentProjectId);
    await loadScheduler(currentProjectId);
    await loadWorkers(currentProjectId);
    showBanner("");
  } catch (err) {
    showBanner(err.message);
  }
}

$("create-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("new-name").value.trim();
  const goal = $("new-goal").value.trim();
  if (!name || !goal) return;
  try {
    const data = await api("POST", "/projects", { name, goal });
    const id = data.id || (data.project && data.project.id);
    $("new-name").value = "";
    $("new-goal").value = "";
    if (id) location.hash = "#/p/" + encodeURIComponent(id);
    else await loadList();
  } catch (err) {
    showBanner(err.message);
  }
});

$("demo-btn").addEventListener("click", () => {
  chatInput.value = "/demo-schedule";
  chatInput.focus();
});

$("upload-btn").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (ev) => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  uploadFile(file);
});

$("import-toggle").addEventListener("click", () => {
  importPane.hidden = !importPane.hidden;
});
$("import-submit").addEventListener("click", importTranscript);
$("file-save").addEventListener("click", saveContextFile);
$("chat-form").addEventListener("submit", sendChat);

chatInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    $("chat-form").requestSubmit();
  }
});

function route() {
  const r = parseRoute();
  if (r.page === "project") openProject(r.id);
  else loadList();
}

window.addEventListener("hashchange", route);
route();
