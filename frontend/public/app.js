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
  // "live" follows the open capture session; "chart" shows one patient's record.
  view: "live",
  chartPatient: null,
  chartImages: [],
  chartFolder: null,
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

function tile(image, { assignable = false, withDate = false } = {}) {
  const node = document.createElement("figure");
  node.className = "tile";
  node.dataset.id = image.id;

  const img = document.createElement("img");
  img.src = image.thumb_url || image.file_url;
  img.alt = `Capture ${image.filename}`;
  img.loading = "lazy";
  img.addEventListener("click", () => openLightbox(image));
  node.appendChild(img);

  const meta = document.createElement("figcaption");
  meta.className = "tile-meta";
  const when = withDate ? `${dateOf(image)} ${timeOf(image)}` : timeOf(image);
  meta.innerHTML = `<strong>${image.filename}</strong>${when} &middot; ${cameraOf(image)}`;
  node.appendChild(meta);

  if (assignable) node.appendChild(assignControls(image));
  return node;
}

function assignControls(image) {
  const fragment = el("assign-template").content.cloneNode(true);
  const select = fragment.querySelector(".assign-select");
  const button = fragment.querySelector(".assign-button");

  select.innerHTML = '<option value="">Assign to...</option>';
  for (const patient of state.patients) {
    const option = document.createElement("option");
    option.value = patient.id;
    option.textContent = `${patient.display_name} (${patient.chart_number})`;
    select.appendChild(option);
  }

  button.addEventListener("click", async () => {
    if (!select.value) return;
    button.disabled = true;
    try {
      await api(`/api/images/${image.id}/assign`, {
        method: "POST",
        body: JSON.stringify({ patient_id: select.value }),
      });
      await refreshUnassigned();
      if (state.view === "chart" && state.chartPatient.id === select.value) {
        await openChart(state.chartPatient);
      }
    } catch (error) {
      button.disabled = false;
      alert(`Could not assign that image: ${error.message}`);
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

  banner.hidden = !live;
  el("chart-banner").hidden = live;
  el("back-to-live").hidden = live;
  el("capture-title").textContent = live ? "Live capture" : "Patient record";

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
  const grid = el("capture-grid");
  const chart = state.view === "chart";
  const images = chart ? state.chartImages : state.captures;

  grid.innerHTML = "";
  for (const image of images) grid.appendChild(tile(image, { withDate: chart }));

  const empty = el("capture-empty");
  empty.hidden = images.length > 0;
  empty.textContent = chart
    ? "No photographs on file yet. Press Start capture, then take the photos."
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
      : `Camera link ready (${ready.length} source)`;
  }
}

/* ---------------- lightbox ---------------- */

function openLightbox(image) {
  el("lightbox-image").src = image.file_url;
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
