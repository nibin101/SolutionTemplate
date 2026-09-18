/*
 * Chair-side capture view.
 *
 * Deliberately dependency-free: this UI runs on a clinic PC that may be old and
 * locked down, served straight from the backend as three static files. No build
 * step, no framework, nothing to install.
 *
 * Live updates arrive over Server-Sent Events, so a photo appears here within a
 * second of the shutter without anyone refreshing anything.
 */

const API = "";

const state = {
  operatory: "OP-1",
  patients: [],
  session: null,
  captures: [],
  unassigned: [],
  // "live" follows the open capture session; "chart" shows one patient's
  // record; "recent" shows everything charted lately, whoever it belongs to;
  // "folders" browses the on-disk photo tree without leaving the browser.
  view: "live",
  chartPatient: null,
  chartImages: [],
  chartFolder: null,
  recentImages: [],
  folderTree: [],
  openFolderName: null,
  openFolderDate: null,
};

const el = (id) => document.getElementById(id);

/* ---------------- api helpers ---------------- */

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(await errorText(response));
  return response.status === 204 ? null : response.json();
}

async function errorText(response) {
  try {
    const body = await response.json();
    return body.detail || JSON.stringify(body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

/* ---------------- rendering helpers ---------------- */

function timeOf(image) {
  const stamp = image.captured_at || image.received_at;
  if (!stamp) return "";
  return new Date(stamp).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function dateOf(image) {
  const stamp = image.captured_at || image.received_at;
  return stamp ? new Date(stamp).toLocaleDateString() : "";
}

function cameraOf(image) {
  return [image.camera_make, image.camera_model].filter(Boolean).join(" ") || image.source;
}

function patientNameFor(image) {
  if (!image.patient_id) return null;
  const match = state.patients.find((p) => p.id === image.patient_id);
  return match ? match.display_name : null;
}

function tile(image, { assignable = false, withDate = false, withPatient = false } = {}) {
  const node = document.createElement("figure");
  node.className = "tile";
  node.dataset.id = image.id;

  if (image.thumb_url) {
    const img = document.createElement("img");
    img.src = image.thumb_url;
    img.alt = `Capture ${image.filename}`;
    img.loading = "lazy";
    img.addEventListener("click", () => openLightbox(image));
    node.appendChild(img);
  } else {
    // No thumbnail means the file could not be decoded here - a RAW body file,
    // or HEIC. Handing the browser the original would just render as a broken
    // image, so say what it is instead. The capture itself is safely stored.
    const placeholder = document.createElement("div");
    placeholder.className = "tile-placeholder";
    const kind = (image.filename.split(".").pop() || "file").toUpperCase();
    placeholder.innerHTML = `<span class="tile-kind">${kind}</span>
      <span class="tile-note">stored, no preview</span>`;
    placeholder.title = "Original is charted; this format cannot be previewed in a browser";
    node.appendChild(placeholder);
  }

  const meta = document.createElement("figcaption");
  meta.className = "tile-meta";
  const when = withDate ? `${dateOf(image)} ${timeOf(image)}` : timeOf(image);
  let line = `<strong>${image.filename}</strong>${when} &middot; ${cameraOf(image)}`;
  if (withPatient) {
    const name = patientNameFor(image);
    line += name
      ? `<span class="tile-patient">${name}</span>`
      : `<span class="tile-patient unassigned">Needs assignment</span>`;
  }
  meta.innerHTML = line;
  node.appendChild(meta);

  if (assignable) node.appendChild(assignControls(image));
  return node;
}

function assignControls(image) {
  const fragment = el("assign-template").content.cloneNode(true);
  const select = fragment.querySelector(".assign-select");
  const button = fragment.querySelector(".assign-button");
  const message = fragment.querySelector(".assign-message");

  for (const patient of state.patients) {
    const option = document.createElement("option");
    option.value = patient.id;
    option.textContent = `${patient.display_name} (${patient.chart_number})`;
    select.appendChild(option);
  }

  // The button stays disabled until a patient is actually picked - clicking it
  // with nothing chosen used to fail silently, which looked exactly like a
  // broken assign button.
  select.addEventListener("change", () => {
    button.disabled = !select.value;
  });

  const showMessage = (text, kind) => {
    message.textContent = text;
    message.className = `assign-message ${kind}`;
    message.hidden = false;
  };

  button.addEventListener("click", async () => {
    if (!select.value) return;
    const chosen = select.selectedOptions[0].textContent;
    button.disabled = true;
    select.disabled = true;
    button.textContent = "Assigning...";
    message.hidden = true;

    try {
      const result = await api(`/api/images/${image.id}/assign`, {
        method: "POST",
        body: JSON.stringify({ patient_id: select.value }),
      });

      if (result.warning) {
        // The chart is correctly assigned; the file itself did not make it
        // into the folder yet. Say so and offer to try the move again,
        // rather than silently reporting success either way.
        showMessage(result.warning, "warn");
        button.textContent = "Retry filing";
        button.disabled = false;
        select.disabled = false;
        return;
      }

      showMessage(`Assigned to ${chosen}.`, "ok");
      await refreshUnassigned();
      if (state.view === "chart" && state.chartPatient.id === select.value) {
        await openChart(state.chartPatient);
      }
    } catch (error) {
      showMessage(`Could not assign: ${error.message}`, "error");
      button.textContent = "Assign";
      button.disabled = false;
      select.disabled = false;
    }
  });

  return fragment;
}

/* ---------------- patients ---------------- */

function renderPatients() {
  const query = el("patient-search").value.trim().toLowerCase();
  const list = el("patient-list");
  list.innerHTML = "";

  const visible = state.patients.filter(
    (patient) =>
      !query ||
      patient.display_name.toLowerCase().includes(query) ||
      patient.chart_number.toLowerCase().includes(query),
  );

  for (const patient of visible) {
    const item = document.createElement("li");
    const capturing = state.session && state.session.patient_id === patient.id;
    if (capturing) item.classList.add("active");
    if (state.view === "chart" && state.chartPatient.id === patient.id) {
      item.classList.add("viewing");
    }

    const label = document.createElement("span");
    label.className = "patient-label";
    label.innerHTML = `<span class="patient-name">${patient.display_name}</span>
      <span class="patient-chart">${patient.chart_number}</span>`;
    label.title = "Open this patient's photo record";
    label.addEventListener("click", () => openChart(patient));
    item.appendChild(label);

    const actions = document.createElement("span");
    actions.className = "patient-actions";

    const captureButton = document.createElement("button");
    captureButton.className = "btn btn-small";
    captureButton.textContent = capturing ? "Capturing" : "Capture";
    captureButton.title = "Start a capture session for this patient";
    captureButton.addEventListener("click", (event) => {
      event.stopPropagation();
      startSession(patient);
    });
    actions.appendChild(captureButton);

    item.appendChild(actions);
    list.appendChild(item);
  }
}

async function addPatient(event) {
  event.preventDefault();
  const form = el("add-patient");
  const error = el("add-patient-error");
  const data = Object.fromEntries(new FormData(form).entries());

  try {
    const patient = await api("/api/patients", {
      method: "POST",
      body: JSON.stringify({
        chart_number: data.chart_number.trim(),
        first_name: data.first_name.trim(),
        last_name: data.last_name.trim(),
        date_of_birth: data.date_of_birth || null,
      }),
    });
    state.patients = await api("/api/patients");
    form.reset();
    form.hidden = true;
    error.hidden = true;
    renderPatients();
    await openChart(patient);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
}

/* ---------------- capture from the connected device ---------------- */

/*
 * There is no "fetch from the camera" button, because there is nothing to ask
 * for. Start capture opens a session; the bridge is watching whatever camera or
 * phone is plugged into this PC and sends across every photograph taken between
 * Start and Stop, on its own. The clinician's whole job is to take the photos.
 */

function phoneCaptureLink(patient) {
  const link = document.createElement("a");
  link.className = "btn btn-small btn-ghost";
  link.href = `./capture.html?patient=${encodeURIComponent(patient.id)}`;
  link.target = "_blank";
  link.textContent = "Send from a phone";
  link.title = "Open this on a phone to send photos straight to this patient";
  return link;
}

/* ---------------- views ---------------- */

function renderSession() {
  const banner = el("session-banner");
  const endButton = el("end-session");
  const live = state.view === "live";
  const chart = state.view === "chart";
  const folders = state.view === "folders";

  banner.hidden = !live;
  // Only the chart view has a chart banner to show - the recent and folders
  // views must not leave an empty one on screen.
  el("chart-banner").hidden = !chart;
  el("back-to-live").hidden = live;
  el("show-recent").hidden = !live;
  el("show-folders").hidden = !live;
  el("folder-breadcrumb").hidden = !folders;
  if (!folders) {
    // Which of these two is visible while browsing folders is decided by
    // renderFolders() itself, since it depends on how deep into the tree the
    // view currently is (roster, dates, or the photos in one date).
    el("folder-list").hidden = true;
    el("capture-grid").hidden = false;
  }
  el("capture-title").textContent = live
    ? "Live capture"
    : chart
      ? "Patient record"
      : folders
        ? "Photo folders"
        : "Recent captures";

  if (folders) el("capture-empty").hidden = true;

  if (!live) {
    endButton.hidden = true;
    return;
  }

  if (state.session) {
    banner.className = "session-banner live";
    banner.innerHTML = `<strong>Capturing for ${state.session.patient_name}</strong>
      <span>Operatory ${state.session.operatory} &middot; started
      ${timeOf({ captured_at: state.session.started_at })} &middot; every photo taken
      between now and Stop is charted to this patient, from whatever camera or
      phone is plugged into this PC.</span>`;
    endButton.hidden = false;
  } else {
    banner.className = "session-banner idle";
    banner.innerHTML = `<strong>No capture session open.</strong>
      <span>Press Start capture on a patient, then take the photos. Nothing is
      taken off the camera while nobody is in the chair.</span>`;
    endButton.hidden = true;
  }
}

function renderChartBanner() {
  const banner = el("chart-banner");
  const patient = state.chartPatient;
  if (!patient) return;

  banner.innerHTML = `<strong>${patient.display_name} &middot; ${patient.chart_number}</strong>
    <span>${state.chartImages.length} photograph(s) on file${
      state.chartFolder ? ` &middot; stored in <code>${state.chartFolder.folder}</code>` : ""
    }</span>
    <span class="chart-status"></span>`;

  const capturing = state.session && state.session.patient_id === patient.id;
  const status = banner.querySelector(".chart-status");
  status.textContent = capturing
    ? "Capturing now - photos taken until you press Stop appear here on their own."
    : "Press Start capture, take the photos, then press Stop.";

  const button = document.createElement("button");
  button.className = "btn btn-small";
  button.textContent = capturing ? "Capturing now" : "Start capture";
  button.disabled = Boolean(capturing);
  button.title = "Photos taken from now on are charted to this patient";
  button.addEventListener("click", () => startSession(patient));
  banner.appendChild(button);
  banner.appendChild(phoneCaptureLink(patient));
}

function renderGrid() {
  if (state.view === "folders") return; // renderFolders() owns this view

  const grid = el("capture-grid");
  const chart = state.view === "chart";
  const recent = state.view === "recent";
  const images = chart ? state.chartImages : recent ? state.recentImages : state.captures;

  grid.innerHTML = "";
  for (const image of images) {
    // The recent view spans patients and days, so it is the one place both the
    // date and who the photograph belongs to have to be on the tile.
    grid.appendChild(tile(image, { withDate: chart || recent, withPatient: recent }));
  }

  const empty = el("capture-empty");
  empty.hidden = images.length > 0;
  empty.textContent = chart
    ? "No photographs on file yet. Press Start capture, then take the photos."
    : recent
      ? "Nothing has been captured yet."
      : "Nothing captured yet in this session. Take a photo - it appears here on its own.";
}

function renderUnassigned() {
  const grid = el("unassigned-grid");
  grid.innerHTML = "";
  for (const image of state.unassigned) {
    grid.appendChild(tile(image, { assignable: true }));
  }
  el("unassigned-count").textContent = String(state.unassigned.length);
  el("unassigned-empty").hidden = state.unassigned.length > 0;
}

/* ---------------- folder browser ----------------
 *
 * The same tree the disk actually has - photos/<Name>_<Chart>/<date>/ - shown
 * without opening Explorer. Three levels: the roster of folders, the dated
 * sub-folders inside one, and the photographs inside one of those (which
 * reuses the ordinary tile grid, so the lightbox works exactly as it does
 * everywhere else).
 *
 * Deliberately not wired to the SSE stream: this is a browse-and-look-up view
 * a clinician opens occasionally, not the live capture view, and updating a
 * nested tree correctly from a single incoming image event is a lot of
 * complexity for a view nobody is staring at during a shoot. Reopening it
 * picks up anything new.
 */

function folderThumb(dates) {
  for (const day of dates) {
    const found = day.images.find((image) => image.thumb_url);
    if (found) return found.thumb_url;
  }
  return null;
}

function folderRow(thumbUrl, name, count, onClick) {
  const item = document.createElement("li");
  item.className = "folder-row";

  const thumb = document.createElement("span");
  thumb.className = "folder-thumb";
  if (thumbUrl) {
    const img = document.createElement("img");
    img.src = thumbUrl;
    img.alt = "";
    img.loading = "lazy";
    thumb.appendChild(img);
  }
  item.appendChild(thumb);

  const label = document.createElement("span");
  label.className = "folder-label";
  label.innerHTML = `<span class="folder-name">${name}</span>
    <span class="folder-count">${count} photo${count === 1 ? "" : "s"}</span>`;
  item.appendChild(label);

  item.addEventListener("click", onClick);
  return item;
}

function renderFolderBreadcrumb() {
  const nav = el("folder-breadcrumb");
  nav.innerHTML = "";

  const root = document.createElement("a");
  root.href = "#";
  root.textContent = "All folders";
  root.addEventListener("click", (event) => {
    event.preventDefault();
    openFolders();
  });
  nav.appendChild(root);

  const folder = state.folderTree.find((f) => f.name === state.openFolderName);
  if (folder) {
    nav.append(" / ");
    const link = document.createElement("a");
    link.href = "#";
    link.textContent = folder.display_name;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      state.openFolderDate = null;
      renderFolders();
    });
    nav.appendChild(link);
  }
  if (state.openFolderDate) {
    nav.append(" / ", state.openFolderDate);
  }
}

function renderFolders() {
  renderFolderBreadcrumb();
  const list = el("folder-list");
  const grid = el("capture-grid");
  list.innerHTML = "";

  const folder = state.folderTree.find((f) => f.name === state.openFolderName);

  if (!folder) {
    // Top level: the roster of folders.
    list.hidden = false;
    grid.hidden = true;
    for (const entry of state.folderTree) {
      list.appendChild(
        folderRow(folderThumb(entry.dates), entry.display_name, entry.image_count, () => {
          state.openFolderName = entry.name;
          state.openFolderDate = null;
          renderFolders();
        }),
      );
    }
    el("capture-empty").hidden = state.folderTree.length > 0;
    el("capture-empty").textContent = "No photographs on file yet.";
    return;
  }

  if (!state.openFolderDate) {
    // One folder open: the dated sub-folders inside it.
    list.hidden = false;
    grid.hidden = true;
    for (const day of folder.dates) {
      const thumb = day.images.find((image) => image.thumb_url)?.thumb_url;
      list.appendChild(
        folderRow(thumb, day.date, day.images.length, () => {
          state.openFolderDate = day.date;
          renderFolders();
        }),
      );
    }
    return;
  }

  // A date open: the photographs themselves, via the ordinary tile grid.
  list.hidden = true;
  grid.hidden = false;
  grid.innerHTML = "";
  const day = folder.dates.find((d) => d.date === state.openFolderDate);
  for (const image of day ? day.images : []) {
    grid.appendChild(tile(image, {}));
  }
}

async function openFolders() {
  state.view = "folders";
  state.chartPatient = null;
  state.openFolderName = null;
  state.openFolderDate = null;
  state.folderTree = await api("/api/images/folders");
  renderSession();
  renderFolders();
  renderPatients();
}

function renderBridge(status) {
  const dot = el("bridge-dot");
  const text = el("bridge-text");

  if (!status || !status.online) {
    dot.className = "dot offline";
    text.textContent = "Camera bridge offline";
    return;
  }

  const queue = status.queue || {};
  const ready = (status.sources || []).filter((source) => source.available);
  const device = ready.map((source) => source.device).find(Boolean);

  if (queue.failed) {
    dot.className = "dot offline";
    text.textContent = `${queue.failed} transfer(s) failed`;
  } else if (status.paused) {
    dot.className = "dot busy";
    text.textContent = "Capture paused at the bridge";
  } else if (queue.pending) {
    dot.className = "dot busy";
    text.textContent = `Transferring ${queue.pending} image(s)`;
  } else {
    dot.className = "dot online";
    text.textContent = device
      ? `Camera ready - ${device}`
      : `Camera link ready (${ready.length} sources)`;
  }
}

/* ---------------- lightbox ---------------- */

function openLightbox(image) {
  // The preview is a 2048px JPEG; the original can be 24 MP and is often RAW,
  // which a browser cannot render at all. `preview_url` is null when the
  // original is already small enough, so falling back to it is correct.
  el("lightbox-image").src = image.preview_url || image.file_url;
  el("lightbox-caption").textContent =
    `${image.filename} - ${cameraOf(image)} - ${dateOf(image)} ${timeOf(image)} - via ${image.source}`;
  el("lightbox").hidden = false;
}

/* ---------------- actions ---------------- */

async function openChart(patient) {
  state.view = "chart";
  state.chartPatient = patient;
  state.chartImages = await api(`/api/patients/${patient.id}/images`);
  try {
    state.chartFolder = await api(`/api/patients/${patient.id}/folder`);
  } catch {
    state.chartFolder = null;
  }
  renderSession();
  renderChartBanner();
  renderGrid();
  renderPatients();
}

async function openRecent() {
  /* Everything charted lately, whoever it belongs to.
   *
   * The live grid only ever shows the open session, and the patient record
   * only one person - so without this there is no way to see that the system
   * has been working at all until someone opens a session. */
  state.view = "recent";
  state.chartPatient = null;
  state.recentImages = await api("/api/images/recent?limit=60");
  renderSession();
  renderGrid();
  renderPatients();
}

function backToLive() {
  state.view = "live";
  state.chartPatient = null;
  renderSession();
  renderGrid();
  renderPatients();
}

async function startSession(patient) {
  if (!state.session || state.session.patient_id !== patient.id) {
    state.session = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ patient_id: patient.id, operatory: state.operatory }),
    });
    state.captures = [];
  }
  backToLive();
}

async function endSession() {
  if (!state.session) return;
  await api(`/api/sessions/${state.session.id}/end`, { method: "POST" });
  state.session = null;
  renderSession();
  renderPatients();
}

async function refreshUnassigned() {
  state.unassigned = await api("/api/images/unassigned");
  renderUnassigned();
}

async function refreshSession() {
  state.session = await api(
    `/api/sessions/active?operatory=${encodeURIComponent(state.operatory)}`,
  );
  state.captures = state.session
    ? await api(`/api/sessions/${state.session.id}/images`)
    : [];
  renderSession();
  renderGrid();
  renderPatients();
}

async function refreshBridge() {
  try {
    renderBridge(await api("/api/bridge/status"));
  } catch {
    renderBridge(null);
  }
}

/* ---------------- live events ---------------- */

function connectEvents() {
  const stream = new EventSource("/api/events");

  stream.onmessage = (event) => {
    const { type, data } = JSON.parse(event.data);

    if (type === "image.captured" || type === "image.assigned") {
      if (state.view === "chart" && data.patient_id === state.chartPatient.id) {
        if (!state.chartImages.some((image) => image.id === data.id)) {
          state.chartImages.unshift(data);
          renderChartBanner();
          renderGrid();
        }
      }
      if (state.session && data.session_id === state.session.id) {
        if (!state.captures.some((image) => image.id === data.id)) {
          state.captures.unshift(data);
          if (state.view === "live") renderGrid();
        }
      }
      // Recent is a live view too - a photograph taken while it is open should
      // appear there, and an assignment should update the tile already on it.
      const seen = state.recentImages.findIndex((image) => image.id === data.id);
      if (seen >= 0) {
        state.recentImages[seen] = data;
      } else {
        state.recentImages.unshift(data);
      }
      if (state.view === "recent") renderGrid();
      if (data.status === "unassigned") {
        if (!state.unassigned.some((image) => image.id === data.id)) {
          state.unassigned.unshift(data);
          renderUnassigned();
        }
      } else {
        state.unassigned = state.unassigned.filter((image) => image.id !== data.id);
        renderUnassigned();
      }
    } else if (type === "bridge.status") {
      renderBridge({ online: true, ...data });
    } else if (type === "session.opened" && data.operatory === state.operatory) {
      state.session = data;
      renderSession();
      renderPatients();
    } else if (type === "session.closed" && state.session && data.id === state.session.id) {
      state.session = null;
      renderSession();
      renderPatients();
    }
  };

  // EventSource reconnects on its own; this only surfaces the gap in the UI.
  stream.onerror = () => renderBridge(null);
}

/* ---------------- boot ---------------- */

async function boot() {
  el("patient-search").addEventListener("input", renderPatients);
  el("end-session").addEventListener("click", endSession);
  el("back-to-live").addEventListener("click", backToLive);
  el("show-recent").addEventListener("click", () => {
    openRecent().catch((error) => alert(`Could not load recent captures: ${error.message}`));
  });
  el("show-folders").addEventListener("click", () => {
    openFolders().catch((error) => alert(`Could not load the photo folders: ${error.message}`));
  });
  el("add-patient").addEventListener("submit", addPatient);
  el("show-add-patient").addEventListener("click", () => {
    const form = el("add-patient");
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector("input").focus();
  });
  el("cancel-add-patient").addEventListener("click", () => {
    el("add-patient").hidden = true;
    el("add-patient-error").hidden = true;
  });

  el("lightbox-close").addEventListener("click", () => (el("lightbox").hidden = true));
  el("lightbox").addEventListener("click", (event) => {
    if (event.target === el("lightbox")) el("lightbox").hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") el("lightbox").hidden = true;
  });
  el("operatory").addEventListener("change", async (event) => {
    state.operatory = event.target.value;
    await refreshSession();
  });

  state.patients = await api("/api/patients");
  await refreshSession();
  await refreshUnassigned();
  await refreshBridge();
  connectEvents();

  // The heartbeat drives the badge over SSE; this poll is the safety net for a
  // bridge that has gone quiet entirely (no heartbeat means no event either).
  setInterval(refreshBridge, 15000);
}

boot().catch((error) => {
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<p style="padding:16px;color:#c0392b">Could not reach the SnapChart backend: ${error.message}</p>`,
  );
});
