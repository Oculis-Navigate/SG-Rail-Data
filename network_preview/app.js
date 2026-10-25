"use strict";

(() => {
  const data = window.RAIL_PREVIEW_DATA;
  if (!data) throw new Error("Preview data is unavailable.");

  const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
  const details = Object.fromEntries(data.network.lines.map(line => [line.id, line]));
  const enabled = new Set(data.network.lines.map(line => line.id));
  const map = L.map("map", { preferCanvas: true }).setView([1.3521, 103.8198], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 20,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  const lineGroup = L.layerGroup().addTo(map);
  const chordGroup = L.layerGroup();
  const stationGroup = L.layerGroup().addTo(map);
  const lineLayers = [];
  const stationLayers = [];
  const chordLayers = [];
  const reviewKeys = new Set(
    data.coverage.checks.filter(row => row.status === "review").map(row => `${row.stop_id}:${row.line_id}`)
  );

  for (const feature of data.geometry.features) {
    const lineId = feature.properties.primary_line_id;
    const detail = details[lineId];
    const layer = L.geoJSON(feature, {
      style: { color: detail.colour, weight: 5, opacity: 0.88, lineCap: "round", lineJoin: "round" },
    });
    layer.bindPopup(
      `<strong>${escapeHtml(detail.name)}</strong><br>` +
      `OSM way <a href="${encodeURI(feature.properties.source_url)}" target="_blank" rel="noreferrer">${feature.properties.way_id}</a><br>` +
      `Relations: ${feature.properties.relation_ids.join(", ")}<br>` +
      `Railway: ${escapeHtml(feature.properties.railway || "not tagged")}<br>` +
      `Updated: ${escapeHtml(feature.properties.source_timestamp || "unknown")}`
    );
    lineLayers.push({ lineIds: feature.properties.line_ids, layer });
  }

  const stations = Object.fromEntries(data.network.stations.map(station => [station.stop_id, station]));
  for (const station of data.network.stations) {
    const hasReview = station.line_ids.some(lineId => reviewKeys.has(`${station.stop_id}:${lineId}`));
    const colour = details[station.line_ids[0]].colour;
    const marker = L.circleMarker([station.coordinates[1], station.coordinates[0]], {
      radius: 4.5, color: hasReview ? "#c0392b" : "#ffffff", weight: hasReview ? 3 : 2,
      fillColor: colour, fillOpacity: 1,
    });
    marker.bindTooltip(`${escapeHtml(station.codes.join("/"))} ${escapeHtml(station.name)}`, {
      permanent: false, direction: "top", className: "station-label",
    });
    const checks = data.coverage.checks.filter(row => row.stop_id === station.stop_id);
    marker.bindPopup(
      `<strong>${escapeHtml(station.name)}</strong> (${escapeHtml(station.codes.join(", "))})<br>` +
      checks.map(row => `${escapeHtml(row.line_id)}: ${row.distance_metres.toFixed(1)} m from geometry`).join("<br>")
    );
    stationLayers.push({ lineIds: station.line_ids, layer: marker });
  }

  const seenPatterns = new Set();
  for (const pattern of data.network.patterns) {
    const coordinates = pattern.station_stop_ids.map(id => stations[id]).filter(Boolean).map(s => [s.coordinates[1], s.coordinates[0]]);
    if (coordinates.length < 2) continue;
    const signature = `${pattern.line_id}:${coordinates.map(c => c.join(",")).join(";")}`;
    if (seenPatterns.has(signature)) continue;
    seenPatterns.add(signature);
    const layer = L.polyline(coordinates, {
      color: details[pattern.line_id].colour, weight: 2, opacity: .75, dashArray: "7 6",
    }).bindPopup(`<strong>${escapeHtml(details[pattern.line_id].name)}</strong><br>Straight station-to-station chord<br>GTFS route: ${escapeHtml(pattern.gtfs_route_id)}`);
    chordLayers.push({ lineIds: [pattern.line_id], layer });
  }

  function renderLayers() {
    lineGroup.clearLayers();
    stationGroup.clearLayers();
    chordGroup.clearLayers();
    for (const item of lineLayers) if (item.lineIds.some(id => enabled.has(id))) item.layer.addTo(lineGroup);
    if (document.getElementById("stationsToggle").checked) {
      for (const item of stationLayers) if (item.lineIds.some(id => enabled.has(id))) item.layer.addTo(stationGroup);
    }
    if (document.getElementById("chordsToggle").checked) {
      chordGroup.addTo(map);
      for (const item of chordLayers) if (item.lineIds.some(id => enabled.has(id))) item.layer.addTo(chordGroup);
    } else if (map.hasLayer(chordGroup)) {
      map.removeLayer(chordGroup);
    }
  }

  const filters = document.getElementById("lineFilters");
  for (const line of data.network.lines) {
    const label = document.createElement("label");
    label.className = "switch";
    label.innerHTML = `<input type="checkbox" checked value="${escapeHtml(line.id)}"><span class="swatch" style="background:${escapeHtml(line.colour)}"></span><span>${escapeHtml(line.id)} — ${escapeHtml(line.name)}</span>`;
    label.querySelector("input").addEventListener("change", event => {
      event.target.checked ? enabled.add(line.id) : enabled.delete(line.id);
      renderLayers();
    });
    filters.appendChild(label);
  }

  document.getElementById("stationsToggle").addEventListener("change", renderLayers);
  document.getElementById("chordsToggle").addEventListener("change", renderLayers);
  document.getElementById("fitButton").addEventListener("click", () => {
    const layers = lineLayers.filter(item => item.lineIds.some(id => enabled.has(id))).map(item => item.layer);
    if (layers.length) map.fitBounds(L.featureGroup(layers).getBounds(), { padding: [20, 20] });
  });

  const facts = [
    ["GTFS service date", data.manifest.service_date],
    ["Active lines", data.manifest.line_count],
    ["GTFS stations", data.manifest.station_count],
    ["OSM route relations", data.manifest.osm_relation_count],
    ["OSM rail ways", data.manifest.geometry_feature_count],
    ["Geometry changes", data.manifest.geometry_change_count],
    ["Proximity reviews", data.manifest.coverage_review_count],
    ["Maximum distance", `${data.coverage.maximum_distance_metres.toFixed(1)} m`],
  ];
  const summary = document.getElementById("summary");
  for (const [term, value] of facts) summary.insertAdjacentHTML("beforeend", `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd>`);
  const limitations = document.getElementById("limitations");
  for (const text of data.coverage.limitations) limitations.insertAdjacentHTML("beforeend", `<li>${escapeHtml(text)}</li>`);

  renderLayers();
  document.getElementById("fitButton").click();
})();
