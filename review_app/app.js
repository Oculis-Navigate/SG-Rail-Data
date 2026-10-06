/* global L */
"use strict";

const SOURCE_META = Object.freeze({
  kartographer: { label: "SgWiki Kartographer", short: "K" },
  onemap: { label: "OneMap", short: "1M" },
  osm: { label: "OpenStreetMap", short: "OSM" },
  lta: { label: "LTA", short: "LTA" },
  manual: { label: "Manual candidate", short: "M" },
});

const SOURCE_ALIASES = Object.freeze({
  sgwiki_kartographer: "kartographer",
  sgwiki: "kartographer",
  one_map: "onemap",
  openstreetmap: "osm",
  lta_geojson: "lta",
});

const OSM_BASEMAP = Object.freeze({
  id: "osm",
  label: "OpenStreetMap (full colour)",
  url_template: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
  max_zoom: 19,
  is_default: false,
  neutral_style: false,
});

const CATEGORY_LABELS = Object.freeze({
  source_correction: "Source correction",
  physical_relocation: "Physical relocation",
  new_exit: "New exit",
  manual_gap_fill: "Manual gap fill",
  source_conflict: "Source conflict",
  revision_revalidation: "Revision revalidation",
});

const SINGAPORE_BOUNDS = Object.freeze({ west: 103.4, east: 104.2, south: 1.1, north: 1.6 });

const state = {
  response: null,
  bundle: null,
  exits: [],
  drafts: [],
  map: null,
  coordinateLayer: null,
  proposalLayer: null,
  visibleMarkers: [],
  basemaps: new Map(),
  basemapLayer: null,
  selectedKey: null,
  draftPoint: null,
  draftEvidence: [],
  proposalMarker: null,
  returnFocus: null,
};

const $ = (selector) => document.querySelector(selector);

function fail(message) {
  throw new Error(message);
}

function requireValue(condition, message) {
  if (!condition) fail(message);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function validHttpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password;
  } catch (_) {
    return false;
  }
}

function normalizeSource(value) {
  const normalized = String(value || "").trim().toLowerCase().replaceAll("-", "_");
  return SOURCE_ALIASES[normalized] || normalized;
}

function sourceLabel(value) {
  const source = normalizeSource(value);
  return SOURCE_META[source]?.label || String(value || "Unknown source").replaceAll("_", " ");
}

function point(value, context) {
  requireValue(Array.isArray(value) && value.length === 2 && value.every(Number.isFinite),
    `${context} must contain [longitude, latitude].`);
  const [longitude, latitude] = value;
  requireValue(longitude >= SINGAPORE_BOUNDS.west && longitude <= SINGAPORE_BOUNDS.east &&
    latitude >= SINGAPORE_BOUNDS.south && latitude <= SINGAPORE_BOUNDS.north,
  `${context} is outside Singapore.`);
  return [Number(longitude), Number(latitude)];
}

function coordinateRecord(value, context) {
  if (value === null) return null;
  requireValue(value && typeof value === "object" && !Array.isArray(value), `${context} must be an object or null.`);
  requireValue(typeof value.source === "string" && value.source, `${context}.source is required.`);
  return { ...value, coordinates: point(value.coordinates, `${context}.coordinates`) };
}

function exitKey(item) {
  return `${item.station_id}\u0000${item.exit_id}`;
}

function shortExitLabel(item) {
  const raw = String(item.label || item.exit_id.split(":").at(-1) || "?").trim();
  return raw.replace(/^exit\s+/i, "") || "?";
}

function samePoint(left, right) {
  return Boolean(left && right) && Math.abs(left[0] - right[0]) <= 1e-10 &&
    Math.abs(left[1] - right[1]) <= 1e-10;
}

function haversineMetres(left, right) {
  if (!left || !right) return null;
  const radians = (degrees) => degrees * Math.PI / 180;
  const dLat = radians(right[1] - left[1]);
  const dLon = radians(right[0] - left[0]);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(radians(left[1])) *
    Math.cos(radians(right[1])) * Math.sin(dLon / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function formatPoint(value) {
  return value ? value.map((number) => number.toFixed(7)).join(", ") : "None";
}

function validateResponse(raw) {
  requireValue(raw && typeof raw === "object" && !Array.isArray(raw), "Review API response must be an object.");
  requireValue(raw.schema_version === 1 && raw.kind === "mrt_wayfinding_review_state",
    "Review API returned an unsupported schema.");
  requireValue(["editable", "read_only"].includes(raw.status), "Review API returned an invalid status.");
  requireValue(typeof raw.bundle_sha256 === "string" && raw.bundle_sha256,
    "Review API returned no state hash.");
  requireValue(raw.bundle && typeof raw.bundle === "object", "Review API returned no review bundle.");
  requireValue(Array.isArray(raw.drafts), "Review API drafts must be an array.");
  return raw;
}

function normalizeBundle(bundle) {
  requireValue(bundle.schema_version === 1 && bundle.kind === "mrt_wayfinding_review_bundle",
    "The review bundle has an unsupported schema.");
  requireValue(bundle.run && typeof bundle.run === "object" && typeof bundle.run.id === "string",
    "The review bundle has no run identity.");
  requireValue(bundle.summary && typeof bundle.summary === "object", "The review bundle has no summary.");
  requireValue(Array.isArray(bundle.exits), "The review bundle exits must be an array.");
  const seen = new Set();
  return bundle.exits.map((raw, index) => {
    const context = `Review bundle exits[${index}]`;
    requireValue(raw && typeof raw === "object" && !Array.isArray(raw), `${context} must be an object.`);
    for (const field of ["station_id", "station_name", "exit_id", "label"]) {
      requireValue(typeof raw[field] === "string" && raw[field], `${context}.${field} is required.`);
    }
    requireValue(Array.isArray(raw.station_codes) && raw.station_codes.every((value) => typeof value === "string"),
      `${context}.station_codes must be a string array.`);
    requireValue(Array.isArray(raw.scopes) && raw.scopes.every((value) => typeof value === "string"),
      `${context}.scopes must be a string array.`);
    requireValue(Array.isArray(raw.candidates), `${context}.candidates must be an array.`);
    requireValue(Array.isArray(raw.evidence), `${context}.evidence must be an array.`);
    requireValue(Array.isArray(raw.findings), `${context}.findings must be an array.`);
    const item = {
      ...raw,
      approved_coordinate: coordinateRecord(raw.approved_coordinate, `${context}.approved_coordinate`),
      current_coordinate: coordinateRecord(raw.current_coordinate, `${context}.current_coordinate`),
      candidates: raw.candidates.map((candidate, candidateIndex) => {
        requireValue(candidate && typeof candidate === "object", `${context}.candidates[${candidateIndex}] is invalid.`);
        requireValue(typeof candidate.id === "string" && candidate.id, `${context}.candidates[${candidateIndex}].id is required.`);
        requireValue(typeof candidate.source === "string" && candidate.source,
          `${context}.candidates[${candidateIndex}].source is required.`);
        requireValue(typeof candidate.selected_in_candidate_run === "boolean",
          `${context}.candidates[${candidateIndex}].selected_in_candidate_run must be Boolean.`);
        requireValue(Array.isArray(candidate.evidence_urls) && candidate.evidence_urls.every((url) => typeof url === "string"),
          `${context}.candidates[${candidateIndex}].evidence_urls must be a string array.`);
        return { ...candidate, source: normalizeSource(candidate.source),
          coordinates: point(candidate.coordinates, `${context}.candidates[${candidateIndex}].coordinates`) };
      }),
    };
    const key = exitKey(item);
    requireValue(!seen.has(key), `${context} repeats ${raw.station_id}/${raw.exit_id}.`);
    seen.add(key);
    return item;
  });
}

function hasDraft(item) {
  const key = exitKey(item);
  return state.drafts.some((draft) => draft && typeof draft === "object" &&
    `${draft.station_id}\u0000${draft.exit_id}` === key);
}

function changed(item) {
  const approved = item.approved_coordinate?.coordinates || null;
  const current = item.current_coordinate?.coordinates || null;
  if (!approved && current) return true;
  if (approved && !current) return true;
  if (approved && current && !samePoint(approved, current)) return true;
  return item.findings.some((finding) => String(finding.report || "").endsWith("_changes"));
}

function isConflict(item) {
  return item.findings.some((finding) => {
    const text = `${finding.severity || ""} ${finding.reason || ""}`.toLowerCase();
    return text.includes("conflict") || text.includes("duplicate") || text.includes("error") || text.includes("critical");
  });
}

function isReviewItem(item) {
  return changed(item) || hasDraft(item) || item.findings.length > 0;
}

function matchesFilters(item) {
  const station = $("#stationFilter").value;
  if (station && item.station_id !== station) return false;
  const scope = $("#scopeFilter").value;
  if (scope === "review" && !isReviewItem(item)) return false;
  if (scope === "changed" && !changed(item)) return false;
  if (scope === "pending" && !hasDraft(item)) return false;
  if (scope === "conflict" && !isConflict(item)) return false;
  const query = $("#search").value.trim().toLowerCase();
  if (!query) return true;
  const haystack = [item.station_id, item.station_name, item.exit_id, item.label,
    ...item.station_codes, ...item.scopes].join(" ").toLowerCase();
  return haystack.includes(query);
}

function selectedSources() {
  return new Set([...document.querySelectorAll(".source-fieldset input:checked")]
    .map((input) => input.value));
}

function approvedIcon(item, active = false) {
  return L.divIcon({
    className: `approved-marker${active ? " is-active" : ""}`,
    html: `<span class="approved-pin"><span>${escapeHtml(shortExitLabel(item))}</span></span>`,
    iconSize: [34, 39],
    iconAnchor: [17, 36],
    tooltipAnchor: [0, -32],
  });
}

function alternateIcon(source) {
  const meta = SOURCE_META[source];
  return L.divIcon({
    className: "alternate-marker",
    html: `<span class="alt-marker alt-marker-${escapeHtml(source)}"><span>${escapeHtml(meta.short)}</span></span>`,
    iconSize: [32, 28],
    iconAnchor: [16, 14],
    tooltipAnchor: [0, -13],
  });
}

function proposalIcon() {
  return L.divIcon({
    className: "proposal-marker",
    html: '<span class="proposal-pin" aria-hidden="true"></span>',
    iconSize: [31, 31],
    iconAnchor: [15, 15],
    tooltipAnchor: [0, -17],
  });
}

function tooltip(item, source, coordinates) {
  return `<strong>${escapeHtml(item.station_name)} · ${escapeHtml(item.exit_id)}</strong><br>` +
    `<span class="tooltip-source">${escapeHtml(sourceLabel(source))} · ${escapeHtml(formatPoint(coordinates))}</span>`;
}

function selectedExit() {
  return state.exits.find((item) => exitKey(item) === state.selectedKey) || null;
}

function addCoordinateMarker(item, coordinates, source, approved) {
  const active = exitKey(item) === state.selectedKey;
  const icon = approved ? approvedIcon(item, active) : alternateIcon(source);
  const marker = L.marker([coordinates[1], coordinates[0]], {
    icon,
    title: `${item.station_name}, ${item.exit_id}, ${approved ? "approved" : sourceLabel(source)} coordinate`,
    alt: `${item.station_name}, ${item.exit_id}`,
    keyboard: true,
    riseOnHover: true,
    zIndexOffset: approved ? 700 : 100,
  });
  marker.bindTooltip(tooltip(item, approved ? "Approved" : source, coordinates), {
    className: "exit-tooltip",
    direction: "top",
  });
  marker.on("click", () => selectItem(item));
  marker.addTo(state.coordinateLayer);
  state.visibleMarkers.push(marker);
}

function renderMap() {
  state.coordinateLayer.clearLayers();
  state.proposalLayer.clearLayers();
  state.visibleMarkers = [];
  const sources = selectedSources();
  const filtered = state.exits.filter(matchesFilters);
  for (const item of filtered) {
    if (sources.has("approved") && item.approved_coordinate) {
      addCoordinateMarker(item, item.approved_coordinate.coordinates, item.approved_coordinate.source, true);
    }
    for (const candidate of item.candidates) {
      if (SOURCE_META[candidate.source] && sources.has(candidate.source)) {
        addCoordinateMarker(item, candidate.coordinates, candidate.source, false);
      }
    }
  }
  if (state.selectedKey && state.draftPoint) {
    state.proposalMarker = L.marker([state.draftPoint[1], state.draftPoint[0]], {
      icon: proposalIcon(),
      title: "Draft proposal. Drag to change the proposed coordinate.",
      alt: "Draft coordinate proposal",
      keyboard: true,
      draggable: true,
      zIndexOffset: 1200,
    }).addTo(state.proposalLayer);
    state.proposalMarker.bindTooltip("Draft proposal · not approved", { className: "exit-tooltip", direction: "top" });
    state.proposalMarker.on("dragend", (event) => {
      const location = event.target.getLatLng();
      setDraftPoint([location.lng, location.lat], "Proposal moved. Save it as a draft when the evidence is ready.");
    });
    state.visibleMarkers.push(state.proposalMarker);
  } else {
    state.proposalMarker = null;
  }
}

function appendFact(root, term, description) {
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  dt.textContent = term;
  dd.textContent = description;
  root.append(dt, dd);
}

function renderRunFacts() {
  const root = $("#runFacts");
  root.replaceChildren();
  appendFact(root, "Run", state.bundle.run.id);
  appendFact(root, "Status", state.bundle.run.status);
  appendFact(root, "Approved", `${state.bundle.summary.approved_coordinate_count} exits`);
  appendFact(root, "Findings", String(state.bundle.summary.finding_count));
  appendFact(root, "Drafts", String(state.drafts.length));
}

function itemStatus(item) {
  if (isConflict(item)) return { label: "Conflict", className: "danger" };
  if (hasDraft(item)) return { label: "Draft pending", className: "warning" };
  if (!item.approved_coordinate) return { label: "No approved coordinate", className: "warning" };
  if (changed(item)) return { label: "Changed", className: "warning" };
  if (item.findings.length) return { label: "Review", className: "warning" };
  return { label: "Approved", className: "" };
}

function renderResults() {
  const items = state.exits.filter(matchesFilters).sort((left, right) =>
    left.station_name.localeCompare(right.station_name) ||
    left.exit_id.localeCompare(right.exit_id, undefined, { numeric: true }));
  $("#resultCount").textContent = String(items.length);
  const root = $("#resultList");
  root.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No exits match these filters.";
    root.append(empty);
    return;
  }
  for (const item of items) {
    const status = itemStatus(item);
    const card = document.createElement("button");
    card.type = "button";
    card.className = "result-card";
    if (changed(item)) card.classList.add("changed");
    if (hasDraft(item)) card.classList.add("pending");
    if (isConflict(item)) card.classList.add("conflict");
    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("span");
    title.textContent = `${item.station_name} · ${item.exit_id}`;
    const code = document.createElement("span");
    code.className = "card-code";
    code.textContent = item.station_codes.join(" / ");
    head.append(title, code);
    const summary = document.createElement("div");
    summary.className = "card-summary";
    const currentVisible = item.current_coordinate && selectedSources().has(item.current_coordinate.source);
    const distance = currentVisible ?
      haversineMetres(item.approved_coordinate?.coordinates, item.current_coordinate.coordinates) : null;
    summary.textContent = !item.approved_coordinate ? "No approved coordinate" :
      (!currentVisible ? "Approved coordinate available" :
        (distance === null ? "Visible candidate has no approved comparison" :
          (distance < .1 ? "Visible candidate matches approved coordinate" :
            `Visible candidate moved ${distance.toFixed(1)} m`)));
    const pills = document.createElement("div");
    pills.className = "pill-row";
    const statusNode = document.createElement("span");
    statusNode.className = `pill ${status.className}`;
    statusNode.textContent = status.label;
    pills.append(statusNode);
    if (item.findings.length) {
      const finding = document.createElement("span");
      finding.className = "pill";
      finding.textContent = `${item.findings.length} finding${item.findings.length === 1 ? "" : "s"}`;
      pills.append(finding);
    }
    card.append(head, summary, pills);
    card.addEventListener("click", () => selectItem(item));
    root.append(card);
  }
}

function clearDraftForm() {
  $("#changeCategory").value = "";
  $("#rationale").value = "";
  $("#evidenceUrl").value = "";
  $("#evidenceTitleInput").value = "";
  $("#evidenceNote").value = "";
  $("#alternativesRejected").value = "";
  state.draftEvidence = [];
  renderDraftEvidence();
  setDraftStatus("", "");
}

function draftFor(item) {
  const key = exitKey(item);
  const matching = state.drafts.filter((draft) => draft && `${draft.station_id}\u0000${draft.exit_id}` === key);
  matching.sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
  return matching[0] || null;
}

function applyExistingDraft(item) {
  const draft = draftFor(item);
  if (!draft) return;
  const payload = draft.request && typeof draft.request === "object" ? draft.request : draft;
  if (CATEGORY_LABELS[payload.change_category]) $("#changeCategory").value = payload.change_category;
  if (typeof payload.rationale === "string") $("#rationale").value = payload.rationale;
  if (typeof payload.alternatives_rejected === "string") $("#alternativesRejected").value = payload.alternatives_rejected;
  if (Array.isArray(payload.evidence)) state.draftEvidence = payload.evidence.filter((entry) =>
    entry && typeof entry.url === "string" && typeof entry.title === "string" && typeof entry.note === "string");
  if (Array.isArray(payload.proposed_coordinates)) {
    try {
      state.draftPoint = point(payload.proposed_coordinates, "Saved draft proposed_coordinates");
      $("#longitude").value = state.draftPoint[0].toFixed(7);
      $("#latitude").value = state.draftPoint[1].toFixed(7);
    } catch (_) { /* backend validates it */ }
  }
  renderDraftEvidence();
  setDraftStatus("This exit has a saved draft. Saving again creates a new draft revision.", "");
}

function selectItem(item) {
  if (document.activeElement instanceof HTMLElement && document.activeElement.classList.contains("result-card")) {
    state.returnFocus = document.activeElement;
  }
  state.selectedKey = exitKey(item);
  clearDraftForm();
  state.draftPoint = null;
  $("#longitude").value = "";
  $("#latitude").value = "";
  applyExistingDraft(item);
  $("#reviewListPane").hidden = true;
  $("#detailPane").hidden = false;
  renderDetail(item);
  renderMap();
  const points = [];
  if (item.approved_coordinate) points.push(item.approved_coordinate.coordinates);
  if (state.draftPoint) points.push(state.draftPoint);
  for (const candidate of item.candidates) {
    if (selectedSources().has(candidate.source)) points.push(candidate.coordinates);
  }
  fitPoints(points, 19);
  $(".review-panel").scrollTo({ top: 0, behavior: "smooth" });
  requestAnimationFrame(() => $("#backButton").focus());
}

function closeDetail() {
  const returnFocus = state.returnFocus;
  state.selectedKey = null;
  state.draftPoint = null;
  state.draftEvidence = [];
  $("#detailPane").hidden = true;
  $("#reviewListPane").hidden = false;
  renderMap();
  state.returnFocus = null;
  requestAnimationFrame(() => {
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    else $("#map").focus();
  });
}

function evidenceLink(entry) {
  const card = document.createElement("article");
  card.className = "evidence-card";
  const title = document.createElement("strong");
  title.textContent = entry.title;
  card.append(title);
  if (validHttpsUrl(entry.url)) {
    const link = document.createElement("a");
    link.href = entry.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = entry.url;
    card.append(document.createElement("br"), link);
  } else {
    const invalid = document.createElement("p");
    invalid.className = "hint";
    invalid.textContent = "Invalid evidence URL retained in source data.";
    card.append(invalid);
  }
  return card;
}

function candidateCard(item, candidate) {
  const card = document.createElement("article");
  card.className = "evidence-card";
  const head = document.createElement("div");
  head.className = "evidence-head";
  const source = document.createElement("strong");
  source.textContent = sourceLabel(candidate.source);
  const badge = document.createElement("span");
  badge.className = "pill";
  badge.textContent = candidate.selected_in_candidate_run ? "Run selection" : "Alternative";
  head.append(source, badge);
  const coordinate = document.createElement("div");
  coordinate.className = "coordinate";
  coordinate.textContent = formatPoint(candidate.coordinates);
  const use = document.createElement("button");
  use.type = "button";
  use.className = "secondary";
  use.textContent = "Use as proposal";
  use.addEventListener("click", () => setDraftPoint(candidate.coordinates,
    `Proposal now uses the ${sourceLabel(candidate.source)} candidate. This is not an approval.`));
  card.append(head, coordinate, use);
  for (const url of candidate.evidence_urls) {
    if (!validHttpsUrl(url)) continue;
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = url;
    card.append(document.createElement("br"), link);
  }
  return card;
}

function renderDetail(item) {
  $("#detailTitle").textContent = `${item.station_name} · ${item.exit_id}`;
  const status = itemStatus(item);
  $("#detailStatus").textContent = status.label;
  $("#detailStatus").className = `status-badge ${status.className}`;
  const facts = $("#changeFacts");
  facts.replaceChildren();
  appendFact(facts, "Codes", item.station_codes.join(" / ") || "None");
  appendFact(facts, "Scope", item.scopes.join(", ") || "Unspecified");
  appendFact(facts, "Approved", item.approved_coordinate ? formatPoint(item.approved_coordinate.coordinates) : "No approved coordinate");
  appendFact(facts, "Approved source", item.approved_coordinate ? sourceLabel(item.approved_coordinate.source) : "None");
  const sources = selectedSources();
  const currentVisible = item.current_coordinate && sources.has(item.current_coordinate.source);
  if (currentVisible) {
    appendFact(facts, "Visible candidate", formatPoint(item.current_coordinate.coordinates));
    appendFact(facts, "Candidate source", sourceLabel(item.current_coordinate.source));
    const distance = haversineMetres(item.approved_coordinate?.coordinates, item.current_coordinate.coordinates);
    appendFact(facts, "Distance", distance === null ? "Not comparable" : `${distance.toFixed(1)} m`);
  }
  $("#changeSummary").textContent = item.findings.length ?
    item.findings.map((finding) => `${finding.severity}: ${finding.reason}`).join(" ") :
    "No pipeline finding is attached to this exit.";
  $("#useApprovedButton").hidden = !item.approved_coordinate;

  const evidence = $("#evidenceList");
  evidence.replaceChildren();
  for (const entry of item.evidence) evidence.append(evidenceLink(entry));
  const visibleCandidates = item.candidates.filter((candidate) => sources.has(candidate.source));
  for (const candidate of visibleCandidates) evidence.append(candidateCard(item, candidate));
  const hidden = item.candidates.length - visibleCandidates.length;
  $("#hiddenEvidenceNote").textContent = hidden > 0 ?
    `${hidden} coordinate candidate${hidden === 1 ? " is" : "s are"} hidden. Turn on its source in the map filters to inspect it.` : "";
  $("#saveDraftButton").disabled = state.response.status !== "editable";
}

function setDraftPoint(value, message = "Proposal changed. This is not an approval.") {
  try {
    state.draftPoint = point([Number(value[0]), Number(value[1])], "Draft coordinate");
    $("#longitude").value = state.draftPoint[0].toFixed(7);
    $("#latitude").value = state.draftPoint[1].toFixed(7);
    setDraftStatus(message, "");
    renderMap();
  } catch (error) {
    setDraftStatus(error.message, "error");
  }
}

function coordinateInputsChanged() {
  setDraftPoint([Number($("#longitude").value), Number($("#latitude").value)]);
}

function setDraftStatus(message, kind) {
  const node = $("#draftStatus");
  node.textContent = message;
  node.className = `draft-status${kind ? ` ${kind}` : ""}`;
}

function addEvidence() {
  const url = $("#evidenceUrl").value.trim();
  const title = $("#evidenceTitleInput").value.trim();
  const note = $("#evidenceNote").value.trim();
  if (!validHttpsUrl(url) || !title || !note) {
    setDraftStatus("Evidence needs a credential-free HTTPS URL, a title, and a note that states what it proves.", "error");
    return;
  }
  state.draftEvidence.push({ url, title, note });
  $("#evidenceUrl").value = "";
  $("#evidenceTitleInput").value = "";
  $("#evidenceNote").value = "";
  renderDraftEvidence();
  setDraftStatus("Evidence added to this draft.", "");
}

function renderDraftEvidence() {
  const root = $("#draftEvidenceList");
  root.replaceChildren();
  state.draftEvidence.forEach((entry, index) => {
    const row = document.createElement("div");
    row.className = "draft-evidence-row";
    const text = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = entry.title;
    const note = document.createElement("div");
    note.textContent = entry.note;
    const url = document.createElement("div");
    url.className = "coordinate";
    url.textContent = entry.url;
    text.append(strong, note, url);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove evidence: ${entry.title}`);
    remove.addEventListener("click", () => {
      state.draftEvidence.splice(index, 1);
      renderDraftEvidence();
    });
    row.append(text, remove);
    root.append(row);
  });
}

async function saveDraft() {
  const item = selectedExit();
  if (!item || state.response.status !== "editable") return;
  const category = $("#changeCategory").value;
  const rationale = $("#rationale").value.trim();
  const alternatives = $("#alternativesRejected").value.trim();
  if (!state.draftPoint) {
    setDraftStatus("Set a valid proposed coordinate.", "error");
    return;
  }
  if (!CATEGORY_LABELS[category]) {
    setDraftStatus("Select a change category.", "error");
    return;
  }
  if (!rationale) {
    setDraftStatus("Explain why the proposed coordinate is correct.", "error");
    return;
  }
  if (!state.draftEvidence.length) {
    setDraftStatus("Add at least one evidence reference.", "error");
    return;
  }
  if (!alternatives) {
    setDraftStatus("Explain which alternatives were not selected. Enter 'None' when there were no alternatives.", "error");
    return;
  }
  const body = {
    schema_version: 1,
    bundle_sha256: state.response.bundle_sha256,
    station_id: item.station_id,
    exit_id: item.exit_id,
    proposed_coordinates: state.draftPoint,
    change_category: category,
    rationale,
    evidence: state.draftEvidence,
    alternatives_rejected: alternatives,
  };
  const button = $("#saveDraftButton");
  button.disabled = true;
  setDraftStatus("Saving draft…", "");
  try {
    const response = await fetch("/api/draft-decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let result = {};
    try { result = await response.json(); } catch (_) { /* handled by status */ }
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    await reloadState();
    setDraftStatus("Draft saved. It still needs a separate audit sign-off before it can affect approved data.", "success");
  } catch (error) {
    setDraftStatus(`Draft save failed: ${error.message}`, "error");
  } finally {
    button.disabled = state.response.status !== "editable";
  }
}

function fitPoints(points, maxZoom = 18) {
  if (!points.length) return;
  const bounds = L.latLngBounds(points.map(([longitude, latitude]) => [latitude, longitude]));
  if (bounds.isValid()) state.map.fitBounds(bounds.pad(.12), { maxZoom });
}

function fitVisible() {
  const locations = state.visibleMarkers.map((marker) => marker.getLatLng());
  if (!locations.length) return;
  const bounds = L.latLngBounds(locations);
  if (bounds.isValid()) state.map.fitBounds(bounds.pad(.08), { maxZoom: 18 });
}

function configuredBasemaps(response) {
  const configured = Array.isArray(response.basemaps) ? response.basemaps :
    (Array.isArray(response.bundle.basemaps) ? response.bundle.basemaps : []);
  return configured.map((entry, index) => {
    requireValue(entry && typeof entry === "object", `Basemap ${index} must be an object.`);
    requireValue(typeof entry.id === "string" && entry.id && typeof entry.label === "string" && entry.label,
      `Basemap ${index} needs an id and label.`);
    requireValue(typeof entry.url_template === "string" && entry.url_template.startsWith("https://"),
      `Basemap ${index} must use an HTTPS URL template.`);
    requireValue(typeof entry.attribution === "string" && entry.attribution,
      `Basemap ${index} needs attribution.`);
    requireValue(Number.isInteger(entry.max_zoom) && entry.max_zoom >= 1 && entry.max_zoom <= 24,
      `Basemap ${index} has an invalid max_zoom.`);
    requireValue(typeof entry.is_default === "boolean", `Basemap ${index}.is_default must be Boolean.`);
    requireValue(entry.neutral_style === undefined || typeof entry.neutral_style === "boolean",
      `Basemap ${index}.neutral_style must be Boolean when present.`);
    return { ...entry, neutral_style: entry.neutral_style === true };
  });
}

function initializeBasemaps(response) {
  const select = $("#basemapSelect");
  select.replaceChildren();
  const configured = configuredBasemaps(response);
  const all = configured.some((entry) => entry.id === OSM_BASEMAP.id) ? configured : [...configured, OSM_BASEMAP];
  requireValue(new Set(all.map((entry) => entry.id)).size === all.length, "Basemap identifiers must be unique.");
  for (const entry of all) {
    state.basemaps.set(entry.id, entry);
    const option = document.createElement("option");
    option.value = entry.id;
    option.textContent = entry.label;
    select.append(option);
  }
  const preferred = configured.find((entry) => entry.is_default) ||
    configured.find((entry) => entry.id.toLowerCase().includes("positron")) || OSM_BASEMAP;
  select.value = preferred.id;
  setBasemap(preferred.id);
}

function setBasemap(id) {
  const config = state.basemaps.get(id);
  if (!config) return;
  if (state.basemapLayer) state.map.removeLayer(state.basemapLayer);
  let pane = "tilePane";
  if (config.neutral_style) {
    if (!state.map.getPane("neutralBasemapPane")) {
      state.map.createPane("neutralBasemapPane");
      state.map.getPane("neutralBasemapPane").classList.add("neutral-basemap-pane");
      state.map.getPane("neutralBasemapPane").style.zIndex = 200;
    }
    pane = "neutralBasemapPane";
  }
  state.basemapLayer = L.tileLayer(config.url_template, {
    maxZoom: config.max_zoom,
    attribution: config.attribution,
    pane,
  }).addTo(state.map);
  state.basemapLayer.bringToBack();
}

function initializeMap() {
  state.map = L.map("map", { zoomControl: true }).setView([1.3521, 103.8198], 12);
  state.coordinateLayer = L.layerGroup().addTo(state.map);
  state.proposalLayer = L.layerGroup().addTo(state.map);
  state.map.on("click", (event) => {
    if (!selectedExit()) return;
    setDraftPoint([event.latlng.lng, event.latlng.lat],
      "Proposal set from the map. This is not an approval.");
  });
}

function populateStations() {
  const select = $("#stationFilter");
  while (select.options.length > 1) select.remove(1);
  const stations = [...new Map(state.exits.map((item) => [item.station_id, item])).values()]
    .sort((left, right) => left.station_name.localeCompare(right.station_name));
  for (const station of stations) {
    const option = document.createElement("option");
    option.value = station.station_id;
    option.textContent = `${station.station_name}${station.station_codes.length ? ` (${station.station_codes.join(", ")})` : ""}`;
    select.append(option);
  }
}

function render() {
  renderRunFacts();
  renderResults();
  renderMap();
  const visible = state.exits.filter(matchesFilters).length;
  $("#runSummary").textContent = `${state.bundle.run.id} · ${visible} exits shown · ` +
    `${state.bundle.summary.finding_count} findings · ${state.drafts.length} drafts`;
  const item = selectedExit();
  if (item) renderDetail(item);
  $("#baselineWarning").hidden = state.bundle.approved_baseline.status === "available";
}

function resetFilters() {
  $("#search").value = "";
  $("#stationFilter").value = "";
  $("#scopeFilter").value = "all";
  document.querySelectorAll(".source-fieldset input").forEach((input) => {
    input.checked = input.value === "approved";
  });
  closeDetail();
  render();
  fitVisible();
}

async function fetchState() {
  const response = await fetch("/api/review-state", { cache: "no-store" });
  let data = {};
  try { data = await response.json(); } catch (_) { /* error below */ }
  if (!response.ok) throw new Error(data.error || `Review API failed with HTTP ${response.status}.`);
  return validateResponse(data);
}

async function reloadState() {
  const selected = state.selectedKey;
  state.response = await fetchState();
  state.bundle = state.response.bundle;
  state.exits = normalizeBundle(state.bundle);
  state.drafts = state.response.drafts;
  state.selectedKey = selected && state.exits.some((item) => exitKey(item) === selected) ? selected : null;
  render();
}

async function load() {
  initializeMap();
  try {
    state.response = await fetchState();
    state.bundle = state.response.bundle;
    state.exits = normalizeBundle(state.bundle);
    state.drafts = state.response.drafts;
    initializeBasemaps(state.response);
    populateStations();
    render();
    fitVisible();
  } catch (error) {
    $("#runSummary").textContent = "Review data failed validation";
    $("#error").hidden = false;
    $("#error").textContent = `${error.message}\n\nStart the review server with a valid pipeline run, then reload.`;
  }
}

$("#search").addEventListener("input", render);
$("#stationFilter").addEventListener("change", () => { render(); fitVisible(); });
$("#scopeFilter").addEventListener("change", () => { render(); fitVisible(); });
document.querySelectorAll(".source-fieldset input").forEach((input) => input.addEventListener("change", () => {
  render();
  const item = selectedExit();
  if (item) renderDetail(item);
}));
$("#basemapSelect").addEventListener("change", (event) => setBasemap(event.target.value));
$("#fitButton").addEventListener("click", fitVisible);
$("#resetButton").addEventListener("click", resetFilters);
$("#backButton").addEventListener("click", closeDetail);
$("#useApprovedButton").addEventListener("click", () => {
  const item = selectedExit();
  if (item?.approved_coordinate) {
    setDraftPoint(item.approved_coordinate.coordinates,
      "Proposal starts at the approved coordinate. Drag or click the map to change it.");
  }
});
$("#longitude").addEventListener("change", coordinateInputsChanged);
$("#latitude").addEventListener("change", coordinateInputsChanged);
$("#addEvidenceButton").addEventListener("click", addEvidence);
$("#saveDraftButton").addEventListener("click", saveDraft);
$("#resetDraftButton").addEventListener("click", () => {
  const item = selectedExit();
  if (item) selectItem(item);
});

window.MrtWayfindingReview = Object.freeze({
  escapeHtml,
  haversineMetres,
  normalizeSource,
  samePoint,
  normalizeBundle,
  validateResponse,
});
load();
