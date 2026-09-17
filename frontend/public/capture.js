/*
 * Phone capture.
 *
 * The phone opens this page over the practice Wi-Fi and pushes photographs
 * straight into the patient's folder on the practice PC. Nothing is installed
 * on the handset, nothing is copied by hand, and no file has to be dropped
 * somewhere for a watcher to notice.
 *
 * Routing matches the USB bridge exactly: the phone says which room it is in,
 * and the server decides which patient that means. Choosing a patient here
 * overrides that for the shots that follow.
 */

const state = {
  operatory: localStorage.getItem("snapchart.operatory") || "OP-1",
  patients: [],
  // ?patient=<id> locks this phone to one patient - that is how the chair-side
  // view hands off when no camera is plugged into the PC.
  override: new URLSearchParams(location.search).get("patient") || "",
  session: null,
  sent: [],
};

const el = (id) => document.getElementById(id);

async function getJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

/* ---------------- who the photo is for ---------------- */

function renderTarget() {
  const box = el("target");
  const name = el("target-name");
  const note = el("target-note");

  if (state.override) {
    const patient = state.patients.find((p) => p.id === state.override);
    box.className = "target";
    name.textContent = patient ? patient.display_name : "Selected patient";
    note.textContent = patient
      ? `Chart ${patient.chart_number} - charted directly, ignoring the room.`
      : "";
    return;
  }

  if (state.session) {
    box.className = "target";
    name.textContent = state.session.patient_name;
    note.textContent = `Open session in ${state.session.operatory}.`;
    return;
  }

  // Fail loudly rather than let someone shoot into a void they think is a chart.
  box.className = "target holding";
  name.textContent = "Nobody in the chair";
  note.textContent =
    `No session open in ${state.operatory}. Photos will be held for review, not charted.`;
}

async function refreshTarget() {
  try {
    state.session = await getJSON(
      `/api/sessions/active?operatory=${encodeURIComponent(state.operatory)}`,
    );
    setLink(true);
  } catch {
    state.session = null;
    setLink(false);
  }
  renderTarget();
}

function setLink(online) {
  const line = el("link-state");
  line.textContent = online ? `Connected - ${state.operatory}` : "Cannot reach the practice PC";
  line.className = online ? "ok" : "bad";
}

/* ---------------- sending ---------------- */

function addRow(file) {
  const entry = {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    file,
    name: file.name || "photo.jpg",
    state: "sending",
    detail: "Sending...",
    preview: URL.createObjectURL(file),
  };
  state.sent.unshift(entry);
  renderSent();
  return entry;
}

function renderSent() {
  const list = el("sent-list");
  list.innerHTML = "";

  for (const entry of state.sent) {
    const item = document.createElement("li");

    const image = document.createElement("img");
    image.src = entry.preview;
    image.alt = "";
    item.appendChild(image);

    const body = document.createElement("div");
    body.className = "sent-body";
    body.innerHTML = `<strong>${entry.name}</strong>${entry.detail}`;
    item.appendChild(body);

    const status = document.createElement("span");
    status.className = `sent-state ${entry.state}`;
    status.textContent =
      entry.state === "sending" ? "..." : entry.state === "done" ? "Sent" : "Failed";
    item.appendChild(status);

    if (entry.state === "failed") {
      const retry = document.createElement("button");
      retry.className = "btn btn-small";
      retry.textContent = "Retry";
      retry.addEventListener("click", () => send(entry));
      item.appendChild(retry);
    }

    list.appendChild(item);
  }

  el("sent-empty").hidden = state.sent.length > 0;
}

async function send(entry) {
  entry.state = "sending";
  entry.detail = "Sending...";
  renderSent();

  const form = new FormData();
  form.append("files", entry.file, entry.name);
  form.append("operatory", state.operatory);
  form.append("device", navigator.userAgent.includes("iPhone") ? "iPhone" : "Phone");
  if (state.override) form.append("patient_id", state.override);

  try {
    const response = await fetch("/api/capture", { method: "POST", body: form });
    if (!response.ok) throw new Error(`${response.status}`);
    const body = await response.json();

    if (body.rejected.length) {
      entry.state = "failed";
      entry.detail = body.rejected[0].reason || "Rejected by the server";
    } else if (body.duplicates) {
      entry.state = "done";
      entry.detail = "Already on file";
    } else {
      const image = body.images[0];
      entry.state = "done";
      entry.detail =
        image && image.status === "assigned" ? "Charted" : "Held for review";
    }
  } catch (error) {
    // The bytes are still in the browser, so a retry costs the user nothing.
    entry.state = "failed";
    entry.detail = "No connection - tap Retry";
  }
  renderSent();
  refreshTarget();
}

function handleFiles(input) {
  for (const file of Array.from(input.files || [])) send(addRow(file));
  input.value = "";
}

/* ---------------- boot ---------------- */

async function boot() {
  const operatory = el("operatory");
  operatory.value = state.operatory;
  operatory.addEventListener("change", () => {
    state.operatory = operatory.value;
    localStorage.setItem("snapchart.operatory", state.operatory);
    refreshTarget();
  });

  el("patient-override").addEventListener("change", (event) => {
    state.override = event.target.value;
    renderTarget();
  });

  el("take-photo").addEventListener("change", (event) => handleFiles(event.target));
  el("pick-photos").addEventListener("change", (event) => handleFiles(event.target));

  try {
    state.patients = await getJSON("/api/patients");
    const select = el("patient-override");
    for (const patient of state.patients) {
      const option = document.createElement("option");
      option.value = patient.id;
      option.textContent = `${patient.display_name} (${patient.chart_number})`;
      select.appendChild(option);
    }
    if (state.override) select.value = state.override;
  } catch {
    setLink(false);
  }

  await refreshTarget();
  renderSent();

  // The chair-side PC may open or close a session at any moment; keep up.
  setInterval(refreshTarget, 10000);
}

boot();
