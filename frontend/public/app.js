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

  node.appendChild(deleteButton(image));

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

/*
 * Every capture carries its own delete control.
 *
 * Cameras fire off lens caps, blurred frames and accidental shots, and a chart
 * padded with those is a chart nobody scrolls. It asks first, by file name -
 * this removes the photograph from disk as well as from the chart, and there
 * is no undo.
 */
function deleteButton(image) {
  const button = document.createElement("button");
  button.className = "tile-delete";
  button.type = "button";
  button.title = "Delete this capture";
  button.setAttribute("aria-label", `Delete ${image.filename}`);
  button.innerHTML = "&times;";

  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    const owner = patientNameFor(image);
    const confirmed = window.confirm(
      `Delete ${image.filename}${owner ? ` from ${owner}'s record` : ""}?\n\n` +
        "The photograph is removed from the chart and from disk. This cannot be undone.",
    );
    if (!confirmed) return;

    button.disabled = true;
    try {
      await api(`/api/images/${image.id}`, { method: "DELETE" });
      // The server broadcasts image.deleted too, but this tab must not wait on
      // its own round trip to show that the tile has gone.
      forgetImage(image.id);
    } catch (error) {
      button.disabled = false;
      alert(`Could not delete this capture: ${error.message}`);
    }
  });

  return button;
}

/** Drop a deleted capture out of every list it could be sitting in. */
function forgetImage(imageId) {
  const without = (images) => images.filter((image) => image.id !== imageId);
  state.captures = without(state.captures);
  state.chartImages = without(state.chartImages);
  state.recentImages = without(state.recentImages);

  const waiting = state.unassigned.length;
  state.unassigned = without(state.unassigned);
  if (state.unassigned.length !== waiting) renderUnassigned();

  for (const folder of state.folderTree) {
    for (const day of folder.dates) day.images = without(day.images);
    folder.dates = folder.dates.filter((day) => day.images.length > 0);
    folder.image_count = folder.dates.reduce((total, day) => total + day.images.length, 0);
  }
  state.folderTree = state.folderTree.filter((folder) => folder.image_count > 0);

  if (state.view === "folders") {
    // The folder or date we were looking at may have just emptied out.
    const folder = state.folderTree.find((f) => f.name === state.openFolderName);
    if (!folder) {
      state.openFolderName = null;
      state.openFolderDate = null;
    } else if (state.openFolderDate && !folder.dates.some((d) => d.date === state.openFolderDate)) {
      state.openFolderDate = null;
    }
    renderFolders();
  } else {
    renderGrid();
    if (state.view === "chart") renderChartBanner();
  }
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

/*
 * The new-patient fields stay collapsed until they are asked for.
 *
 * Nearly every visit to this panel is someone looking up a patient who is
 * already on the list; a permanently open four-field form pushes the search
 * box and the list itself down the screen for the rare case instead of the
 * common one.
 */
function toggleAddPatient(open) {
  const form = el("add-patient");
  const next = open === undefined ? form.hidden : open;

  form.hidden = !next;
  el("show-add-patient").setAttribute("aria-expanded", String(next));
  el("show-add-patient").textContent = next ? "Close" : "Add patient";

  if (next) {
    form.querySelector("input").focus();
  } else {
    form.reset();
    el("add-patient-error").hidden = true;
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
    toggleAddPatient(false);
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

/*
 * Stop sits beside Start, in red, rather than up in the toolbar.
 *
 * Starting and stopping a capture session are the same decision made twice,
 * and a clinician mid-shoot should not have to hunt across the panel for the
 * second half of it. Red because ending a session is the one action here that
 * silently changes where the next photograph lands.
 */
function endSessionButton() {
  const button = document.createElement("button");
  button.className = "btn btn-small btn-danger";
  button.type = "button";
  button.textContent = "End capture";
  button.title = "Stop charting photos to this patient";
  button.addEventListener("click", () => {
    endSession().catch((error) => alert(`Could not end the session: ${error.message}`));
  });
  return button;
}

function renderSession() {
  const banner = el("session-banner");
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

  if (!live) return;

  if (state.session) {
    banner.className = "session-banner live";
    banner.innerHTML = `<strong>Capturing for ${state.session.patient_name}</strong>
      <span>Operatory ${state.session.operatory} &middot; started
      ${timeOf({ captured_at: state.session.started_at })} &middot; every photo taken
      between now and Stop is charted to this patient, from whatever camera or
      phone is plugged into this PC.</span>`;
    const actions = document.createElement("div");
    actions.className = "banner-actions";
    actions.appendChild(endSessionButton());
    banner.appendChild(actions);
  } else {
    banner.className = "session-banner idle";
    banner.innerHTML = `<strong>No capture session open.</strong>
      <span>Press Start capture on a patient, then take the photos. Nothing is
      taken off the camera while nobody is in the chair.</span>`;
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

  const actions = document.createElement("div");
  actions.className = "banner-actions";

  const button = document.createElement("button");
  button.className = "btn btn-small";
  button.textContent = capturing ? "Capturing now" : "Start capture";
  button.disabled = Boolean(capturing);
  button.title = "Photos taken from now on are charted to this patient";
  button.addEventListener("click", () => startSession(patient));
  actions.appendChild(button);

  // Stop lives next to Start, not in the toolbar - see endSessionButton().
  if (capturing) actions.appendChild(endSessionButton());

  actions.appendChild(phoneCaptureLink(patient));
  banner.appendChild(actions);
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

  // The badge is the whole signal now that the panel itself is folded away in
  // the top bar, so it shows a count only when there is something to act on.
  const badge = el("unassigned-count");
  badge.textContent = String(state.unassigned.length);
  badge.hidden = state.unassigned.length === 0;
  el("inbox-toggle").classList.toggle("has-waiting", state.unassigned.length > 0);
  el("unassigned-empty").hidden = state.unassigned.length > 0;
}

/* ---------------- needs-assignment inbox ---------------- */

function toggleInbox(open) {
  const panel = el("inbox-panel");
  const next = open === undefined ? panel.hidden : open;
  panel.hidden = !next;
  el("inbox-toggle").setAttribute("aria-expanded", String(next));
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

/*
 * Which operatory (treatment room) this screen belongs to.
 *
 * Not a choice any more. The bridge running on this PC is configured for
 * exactly one room in `bridge/.env`, and stamps that operatory onto every
 * photograph it uploads; the backend then maps operatory + time onto the open
 * session to decide whose chart a photo lands in. A picker here could only
 * ever disagree with that - and a screen watching OP-2 while the camera beside
 * it uploads as OP-1 opens sessions nothing is ever charted to, quarantining a
 * whole visit while looking perfectly healthy. Reading the room off the bridge
 * makes that disagreement unrepresentable. It is not shown on this screen
 * either - a single-room practice never needs to think about it, and the
 * live banner already names the room of the session it is describing.
 */
function adoptOperatory(status) {
  const reported = status && status.operatory;
  if (!reported || reported === state.operatory) return false;
  state.operatory = reported;
  return true;
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
  const wasFor = state.session.patient_id;
  await api(`/api/sessions/${state.session.id}/end`, { method: "POST" });
  state.session = null;

  // Land on the record of whoever just got up from the chair, which puts their
  // Start capture button back within reach. Ending a session and starting
  // another one on the same patient is the ordinary way a visit goes - a
  // forgotten shot, a second angle - and sending the clinician back to an empty
  // live view to hunt for the same name again would be the wrong default.
  const patient = state.patients.find((p) => p.id === wasFor);
  if (patient) {
    await openChart(patient);
    return;
  }

  renderSession();
  renderPatients();
  if (state.view === "chart") renderChartBanner();
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
  let status = null;
  try {
    status = await api("/api/bridge/status");
  } catch {
    status = null;
  }
  const moved = adoptOperatory(status);
  renderBridge(status);
  // The bridge has named a different room than the one we were watching, so
  // the open session we are showing belongs to somebody else's chair.
  if (moved) await refreshSession();
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
    } else if (type === "image.deleted") {
      // Another tab (or another operatory) got rid of a capture; drop it here
      // too rather than leaving a tile that 404s the moment it is opened.
      forgetImage(data.id);
    } else if (type === "bridge.status") {
      const status = { online: true, ...data };
      if (adoptOperatory(status)) {
        refreshSession().catch(() => {});
      }
      renderBridge(status);
    } else if (type === "session.opened" && data.operatory === state.operatory) {
      state.session = data;
      renderSession();
      renderPatients();
    } else if (type === "session.closed" && state.session && data.id === state.session.id) {
      state.session = null;
      renderSession();
      renderPatients();
      if (state.view === "chart") renderChartBanner();
    }
  };

  // EventSource reconnects on its own; this only surfaces the gap in the UI.
  stream.onerror = () => renderBridge(null);
}

/* ---------------- boot ---------------- */

async function boot() {
  el("patient-search").addEventListener("input", renderPatients);
  el("back-to-live").addEventListener("click", backToLive);
  el("show-recent").addEventListener("click", () => {
    openRecent().catch((error) => alert(`Could not load recent captures: ${error.message}`));
  });
  el("show-folders").addEventListener("click", () => {
    openFolders().catch((error) => alert(`Could not load the photo folders: ${error.message}`));
  });
  el("add-patient").addEventListener("submit", addPatient);
  el("show-add-patient").addEventListener("click", () => toggleAddPatient());
  el("cancel-add-patient").addEventListener("click", () => toggleAddPatient(false));

  el("inbox-toggle").addEventListener("click", (event) => {
    event.stopPropagation();
    toggleInbox();
  });
  el("inbox-close").addEventListener("click", () => toggleInbox(false));
  // A notifications tray closes when you look somewhere else. Clicks inside it
  // must not count - assigning a photo takes several of them.
  el("inbox-panel").addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => toggleInbox(false));

  el("lightbox-close").addEventListener("click", () => (el("lightbox").hidden = true));
  el("lightbox").addEventListener("click", (event) => {
    if (event.target === el("lightbox")) el("lightbox").hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    el("lightbox").hidden = true;
    toggleInbox(false);
  });
  state.patients = await api("/api/patients");
  // The bridge names the room before anything that depends on it is asked for:
  // looking up the open session is keyed on the operatory.
  await refreshBridge();
  await refreshSession();
  await refreshUnassigned();
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
