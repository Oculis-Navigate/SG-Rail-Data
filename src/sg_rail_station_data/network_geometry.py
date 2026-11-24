"""Preview-only rail network geometry derived from LTA GTFS and OpenStreetMap.

LTA GTFS determines active services, stop order and station locations. OSM route
relations provide physical geometry. Outputs from this module are explicitly not
approved release data; they are intended for visual and contract review before a
future pipeline stage is introduced.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import StringIO
import json
import re
import math
from pathlib import Path
import shutil
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile
import time
from typing import Callable, Mapping

from .sources.lta_gtfs import active_inventory
from .station_catalog import station_codes_for_display
from .network_continuity import check_continuity
from .storage import atomic_write_bytes, atomic_write_json, read_json


LINE_ORDER = ("NSL", "EWL", "NEL", "CCL", "DTL", "TEL", "BPLRT", "SKLRT", "PGLRT")
LINE_DETAILS = {
    "NSL": ("North-South Line", "#d42e12"),
    "EWL": ("East-West Line", "#009645"),
    "NEL": ("North East Line", "#9900aa"),
    "CCL": ("Circle Line", "#fa9e0d"),
    "DTL": ("Downtown Line", "#005ec4"),
    "TEL": ("Thomson-East Coast Line", "#9d5a25"),
    "BPLRT": ("Bukit Panjang LRT", "#748477"),
    "SKLRT": ("Sengkang LRT", "#748477"),
    "PGLRT": ("Punggol LRT", "#748477"),
}
OSM_ROUTE_TYPES = frozenset({"subway", "light_rail", "monorail"})
MAX_HTTP_BYTES = 32 * 1024 * 1024
BBOX = (1.13, 103.60, 1.48, 104.10)


class NetworkGeometryError(RuntimeError):
    """Raised when GTFS or OSM cannot produce a safe preview artifact."""


@dataclass(frozen=True, slots=True)
class NetworkPreviewResult:
    output_directory: Path
    geojson_path: Path
    network_path: Path
    coverage_path: Path
    changes_path: Path
    preview_path: Path
    line_count: int
    feature_count: int
    station_count: int
    change_count: int


def _line_id(route_id: str) -> str:
    # Recent MRT feeds encode the two pattern endpoints in the route ID.
    # Match only known families and endpoint formats; the complete route_id
    # still keys each service pattern (including the EWL Changi branch).
    for family, endpoint in (("NSL", r"NS\d+"), ("NEL", r"NE\d+"),
                             ("DTL", r"DT\d+"), ("TEL", r"TE\d+"),
                             ("EWL", r"EW\d+"), ("EWL_CGL", r"CG\d*")):
        if re.fullmatch(fr"{family}_{endpoint}_{endpoint}", route_id):
            return "EWL" if family == "EWL_CGL" else family
    if route_id == "NSL":
        return "NSL"
    if route_id in {"EWL", "EWL_CGL"}:
        return "EWL"
    if route_id == "NEL":
        return "NEL"
    if route_id.startswith("CCL_"):
        return "CCL"
    if route_id == "DTL":
        return "DTL"
    if route_id == "TEL":
        return "TEL"
    if route_id == "BP":
        return "BPLRT"
    if route_id == "SK":
        return "SKLRT"
    if route_id == "PG":
        return "PGLRT"
    raise NetworkGeometryError(f"Unsupported active GTFS rail route {route_id!r}")


def _csv_rows(archive: ZipFile, name: str) -> list[dict[str, str]]:
    try:
        text = archive.read(name).decode("utf-8-sig", errors="strict")
    except KeyError as error:
        raise NetworkGeometryError(f"GTFS archive is missing {name}") from error
    reader = csv.DictReader(StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise NetworkGeometryError(f"{name}: missing header")
    rows = []
    for number, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise NetworkGeometryError(f"{name}:{number}: invalid column count")
        rows.append({key: value.strip() for key, value in row.items()})
    return rows


def _gtfs_date(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _active_services(
    calendar_rows: list[dict[str, str]],
    exception_rows: list[dict[str, str]],
    service_date: date,
) -> set[str]:
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    active = {
        row["service_id"]
        for row in calendar_rows
        if _gtfs_date(row["start_date"]) <= service_date <= _gtfs_date(row["end_date"])
        and row[weekdays[service_date.weekday()]] == "1"
    }
    for row in exception_rows:
        if _gtfs_date(row["date"]) != service_date:
            continue
        if row["exception_type"] == "1":
            active.add(row["service_id"])
        elif row["exception_type"] == "2":
            active.discard(row["service_id"])
    return active


def build_gtfs_network(gtfs_zip: Path, inventory_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Validate a retained GTFS source and derive active station points/patterns."""

    raw = gtfs_zip.read_bytes()
    inventory = read_json(inventory_path)
    if type(inventory) is not dict:
        raise NetworkGeometryError("GTFS active inventory must be an object")
    try:
        selected_date = date.fromisoformat(str(inventory["service_date"]))
    except (KeyError, ValueError) as error:
        raise NetworkGeometryError("GTFS active inventory has an invalid service_date") from error
    rebuilt = active_inventory(raw, selected_date)
    if rebuilt != inventory:
        raise NetworkGeometryError(
            "GTFS ZIP does not reproduce the retained active-stations.json exactly"
        )

    with ZipFile(gtfs_zip) as archive:
        stops_rows = _csv_rows(archive, "stops.txt")
        routes_rows = _csv_rows(archive, "routes.txt")
        trips_rows = _csv_rows(archive, "trips.txt")
        times_rows = _csv_rows(archive, "stop_times.txt")
        calendar_rows = _csv_rows(archive, "calendar.txt")
        exception_rows = _csv_rows(archive, "calendar_dates.txt")

    stops = {row["stop_id"]: row for row in stops_rows}
    routes = {row["route_id"]: row for row in routes_rows}
    active_services = _active_services(calendar_rows, exception_rows, selected_date)
    active_trips = {
        row["trip_id"]: row for row in trips_rows if row["service_id"] in active_services
    }
    if not active_trips:
        raise NetworkGeometryError("No active GTFS trips are available for the service date")

    times_by_trip: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in times_rows:
        if row["trip_id"] in active_trips:
            times_by_trip[row["trip_id"]].append((int(row["stop_sequence"]), row["stop_id"]))

    patterns: dict[tuple[object, ...], dict[str, object]] = {}
    active_line_ids: set[str] = set()
    for trip_id, trip in active_trips.items():
        route_id = trip["route_id"]
        line_id = _line_id(route_id)
        active_line_ids.add(line_id)
        parent_stops: list[str] = []
        for _sequence, child_id in sorted(times_by_trip[trip_id]):
            child = stops[child_id]
            parent_id = child.get("parent_station") or child_id
            if not parent_stops or parent_stops[-1] != parent_id:
                parent_stops.append(parent_id)
        if len(parent_stops) < 2:
            raise NetworkGeometryError(f"GTFS trip {trip_id} has fewer than two stations")
        direction = trip.get("direction_id", "")
        key = (line_id, route_id, direction, tuple(parent_stops))
        if key not in patterns:
            encoded = "|".join((line_id, route_id, direction, *parent_stops)).encode()
            patterns[key] = {
                "id": sha256(encoded).hexdigest()[:16],
                "line_id": line_id,
                "gtfs_route_id": route_id,
                "direction_id": direction or None,
                "station_stop_ids": parent_stops,
                "trip_count": 0,
                "headsigns": [],
            }
        pattern = patterns[key]
        pattern["trip_count"] = int(pattern["trip_count"]) + 1
        headsign = trip.get("trip_headsign", "")
        if headsign and headsign not in pattern["headsigns"]:
            pattern["headsigns"].append(headsign)

    station_features = []
    stations = []
    for row in inventory["stations"]:
        stop_id = row["stop_id"]
        stop = stops[stop_id]
        try:
            latitude = float(stop["stop_lat"])
            longitude = float(stop["stop_lon"])
        except (KeyError, ValueError) as error:
            raise NetworkGeometryError(f"GTFS station {stop_id} has invalid coordinates") from error
        if not (1.0 <= latitude <= 1.6 and 103.5 <= longitude <= 104.2):
            raise NetworkGeometryError(f"GTFS station {stop_id} is outside Singapore bounds")
        line_ids = sorted({_line_id(route_id) for route_id in row["route_ids"]}, key=LINE_ORDER.index)
        station = {
            "stop_id": stop_id,
            "name": row["name"],
            "codes": row["codes"],
            "display_codes": station_codes_for_display(row["codes"]),
            "line_ids": line_ids,
            "coordinates": [longitude, latitude],
        }
        stations.append(station)
        station_features.append({
            "type": "Feature",
            "id": stop_id,
            "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
            "properties": {key: value for key, value in station.items() if key != "coordinates"},
        })

    lines = []
    for line_id in LINE_ORDER:
        if line_id not in active_line_ids:
            continue
        name, colour = LINE_DETAILS[line_id]
        route_ids = sorted({
            pattern["gtfs_route_id"] for pattern in patterns.values()
            if pattern["line_id"] == line_id
        })
        lines.append({
            "id": line_id,
            "name": name,
            "colour": colour,
            "gtfs_route_ids": route_ids,
        })

    network = {
        "schema_version": 1,
        "kind": "preview_lta_gtfs_rail_network",
        "status": "preview_not_approved",
        "publication_allowed": False,
        "service_date": selected_date.isoformat(),
        "gtfs_zip_sha256": sha256(raw).hexdigest(),
        "feed_info": inventory["feed_info"],
        "lines": lines,
        "stations": stations,
        "patterns": sorted(patterns.values(), key=lambda item: (LINE_ORDER.index(item["line_id"]), item["id"])),
        "notice": "LTA GTFS determines active services and station order; it does not provide this preview's line geometry.",
    }
    station_geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "preview_lta_gtfs_active_station_points",
            "status": "preview_not_approved",
            "publication_allowed": False,
            "service_date": selected_date.isoformat(),
            "feature_count": len(station_features),
        },
        "features": station_features,
    }
    return network, station_geojson


def _validate_endpoint(endpoint: str, *, host: str, context: str) -> str:
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https" or parts.hostname != host or parts.username or parts.password
        or parts.port not in (None, 443) or parts.query or parts.fragment
    ):
        raise NetworkGeometryError(f"{context} must be an unqualified https://{host} URL")
    return endpoint.rstrip("/")


def _default_http_get(url: str, *, timeout: float, max_bytes: int) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "sg-rail-station-data/0.1 network-geometry-preview",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise NetworkGeometryError(f"{url}: response exceeds {max_bytes} bytes")
        if response.status != 200:
            raise NetworkGeometryError(f"{url}: unexpected HTTP status {response.status}")
        return body


def _download_json(
    url: str,
    *,
    timeout_seconds: float,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    http_get: Callable[..., bytes],
) -> tuple[bytes, dict[str, object]]:
    delay = initial_backoff_seconds
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            raw = http_get(url, timeout=timeout_seconds, max_bytes=MAX_HTTP_BYTES)
            document = json.loads(raw)
            if type(document) is not dict:
                raise NetworkGeometryError(f"{url}: expected a JSON object")
            return raw, document
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, NetworkGeometryError) as error:
            last_error = error
            if attempt == max_attempts:
                break
            if delay:
                time.sleep(delay)
            delay = min(maximum_backoff_seconds, max(delay * 2, initial_backoff_seconds))
    raise NetworkGeometryError(
        f"Unable to collect OSM evidence after {max_attempts} attempts: {last_error}"
    ) from last_error


def _overpass_url(endpoint: str) -> str:
    south, west, north, east = BBOX
    query = (
        "[out:json][timeout:120];"
        f'rel["type"="route"]["route"~"^(subway|light_rail|monorail)$"]'
        f"({south},{west},{north},{east});out tags;"
    )
    return f"{endpoint}?{urlencode({'data': query})}"


def collect_osm_geometry(
    *,
    active_line_ids: set[str],
    overpass_endpoint: str,
    api_endpoint: str,
    source_directory: Path,
    timeout_seconds: float,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    minimum_interval_seconds: float,
    http_get: Callable[..., bytes] = _default_http_get,
) -> tuple[dict[str, object], dict[str, object]]:
    """Collect route relations and normalize their member ways to GeoJSON."""

    overpass = _validate_endpoint(
        overpass_endpoint, host="overpass-api.de", context="OSM Overpass endpoint"
    )
    api = _validate_endpoint(api_endpoint, host="api.openstreetmap.org", context="OSM API endpoint")
    source_directory.mkdir(parents=True, exist_ok=False)
    responses = source_directory / "relations"
    responses.mkdir()

    discovery_raw, discovery = _download_json(
        _overpass_url(overpass), timeout_seconds=timeout_seconds,
        max_attempts=max_attempts, initial_backoff_seconds=initial_backoff_seconds,
        maximum_backoff_seconds=maximum_backoff_seconds, http_get=http_get,
    )
    atomic_write_bytes(source_directory / "discovery.json", discovery_raw)
    elements = discovery.get("elements")
    if type(elements) is not list:
        raise NetworkGeometryError("OSM discovery response has no elements array")

    selected: list[dict[str, object]] = []
    for element in elements:
        if type(element) is not dict or element.get("type") != "relation":
            continue
        tags = element.get("tags")
        if type(tags) is not dict:
            continue
        ref = tags.get("ref")
        if ref not in active_line_ids:
            continue
        if tags.get("route") not in OSM_ROUTE_TYPES or tags.get("type") != "route":
            continue
        if type(element.get("id")) is not int:
            raise NetworkGeometryError("OSM discovery relation has an invalid ID")
        selected.append(element)

    found = {str(row["tags"]["ref"]) for row in selected}
    missing = sorted(active_line_ids - found)
    if missing:
        raise NetworkGeometryError(f"OSM has no route relation for active lines: {missing}")
    selected.sort(key=lambda row: (LINE_ORDER.index(str(row["tags"]["ref"])), int(row["id"])))

    relation_documents: list[tuple[dict[str, object], dict[str, object], str]] = []
    response_records = []
    for index, summary in enumerate(selected):
        relation_id = int(summary["id"])
        if index and minimum_interval_seconds:
            time.sleep(minimum_interval_seconds)
        url = f"{api}/relation/{relation_id}/full.json"
        raw, document = _download_json(
            url, timeout_seconds=timeout_seconds, max_attempts=max_attempts,
            initial_backoff_seconds=initial_backoff_seconds,
            maximum_backoff_seconds=maximum_backoff_seconds, http_get=http_get,
        )
        filename = f"{relation_id}.json"
        atomic_write_bytes(responses / filename, raw)
        digest = sha256(raw).hexdigest()
        relation_documents.append((summary, document, digest))
        response_records.append({
            "relation_id": relation_id,
            "line_id": summary["tags"]["ref"],
            "file": f"relations/{filename}",
            "sha256": digest,
            "url": url,
        })

    way_records: dict[int, dict[str, object]] = {}
    relation_records = []
    for summary, document, digest in relation_documents:
        line_id = str(summary["tags"]["ref"])
        document_elements = document.get("elements")
        if type(document_elements) is not list:
            raise NetworkGeometryError(f"OSM relation {summary['id']} has no elements array")
        nodes = {
            int(row["id"]): row for row in document_elements
            if type(row) is dict and row.get("type") == "node" and type(row.get("id")) is int
        }
        ways = {
            int(row["id"]): row for row in document_elements
            if type(row) is dict and row.get("type") == "way" and type(row.get("id")) is int
        }
        relations = [
            row for row in document_elements
            if type(row) is dict and row.get("type") == "relation" and row.get("id") == summary["id"]
        ]
        if len(relations) != 1:
            raise NetworkGeometryError(f"OSM full response does not contain relation {summary['id']} exactly once")
        relation = relations[0]
        tags = relation.get("tags")
        if type(tags) is not dict or tags.get("ref") != line_id or tags.get("route") not in OSM_ROUTE_TYPES:
            raise NetworkGeometryError(f"OSM relation {summary['id']} changed identity")
        way_ids = []
        for member in relation.get("members", []):
            if type(member) is not dict or member.get("type") != "way":
                continue
            role = member.get("role", "")
            if role not in {"", "forward", "backward"}:
                continue
            way_id = member.get("ref")
            if type(way_id) is not int or way_id not in ways:
                raise NetworkGeometryError(f"OSM relation {summary['id']} has an unresolved member way")
            way = ways[way_id]
            way_tags = way.get("tags", {})
            if type(way_tags) is not dict:
                raise NetworkGeometryError(f"OSM way {way_id} has invalid tags")
            railway = way_tags.get("railway")
            if railway in {"construction", "proposed", "abandoned", "disused"}:
                raise NetworkGeometryError(f"Operational OSM relation uses non-operational way {way_id}")
            if railway not in {"rail", "subway", "light_rail", "monorail"}:
                raise NetworkGeometryError(f"OSM relation member {way_id} has unsupported railway={railway!r}")
            coordinates = []
            for node_id in way.get("nodes", []):
                node = nodes.get(node_id)
                if node is None or type(node.get("lon")) not in {int, float} or type(node.get("lat")) not in {int, float}:
                    raise NetworkGeometryError(f"OSM way {way_id} has an unresolved node")
                coordinates.append([node["lon"], node["lat"]])
            if len(coordinates) < 2:
                raise NetworkGeometryError(f"OSM way {way_id} has fewer than two points")
            way_ids.append(way_id)
            if way_id not in way_records:
                way_records[way_id] = {
                    "way": way,
                    "coordinates": coordinates,
                    "line_ids": set(),
                    "relation_ids": set(),
                }
            record = way_records[way_id]
            if record["coordinates"] != coordinates:
                raise NetworkGeometryError(f"OSM way {way_id} differs between relation responses")
            record["line_ids"].add(line_id)
            record["relation_ids"].add(int(summary["id"]))
        if not way_ids:
            raise NetworkGeometryError(f"OSM relation {summary['id']} has no usable rail ways")
        relation_records.append({
            "relation_id": int(summary["id"]),
            "line_id": line_id,
            "name": tags.get("name"),
            "route": tags.get("route"),
            "direction": tags.get("direction"),
            "from": tags.get("from"),
            "to": tags.get("to"),
            "roundtrip": tags.get("roundtrip"),
            "version": relation.get("version"),
            "timestamp": relation.get("timestamp"),
            "source_sha256": digest,
            "way_ids": way_ids,
        })

    features = []
    for way_id, record in sorted(way_records.items()):
        way = record["way"]
        tags = way.get("tags", {})
        line_ids = sorted(record["line_ids"], key=LINE_ORDER.index)
        properties = {
            "way_id": way_id,
            "line_ids": line_ids,
            "primary_line_id": line_ids[0],
            "relation_ids": sorted(record["relation_ids"]),
            "source_kind": "openstreetmap_route_relation_member",
            "source_url": f"https://www.openstreetmap.org/way/{way_id}",
            "source_version": way.get("version"),
            "source_timestamp": way.get("timestamp"),
            "railway": tags.get("railway"),
            "name": tags.get("name"),
            "tunnel": tags.get("tunnel"),
            "bridge": tags.get("bridge"),
            "layer": tags.get("layer"),
            "start_date": tags.get("start_date"),
        }
        features.append({
            "type": "Feature",
            "id": f"osm-way-{way_id}",
            "geometry": {"type": "LineString", "coordinates": record["coordinates"]},
            "properties": properties,
        })

    timestamp = None
    if type(discovery.get("osm3s")) is dict:
        timestamp = discovery["osm3s"].get("timestamp_osm_base")
    geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "preview_osm_operational_rail_geometry",
            "status": "preview_not_approved",
            "publication_allowed": False,
            "source": "OpenStreetMap",
            "source_timestamp": timestamp,
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "feature_count": len(features),
            "line_ids": [line_id for line_id in LINE_ORDER if line_id in active_line_ids],
            "relation_count": len(relation_records),
            "derivation": "Member rail ways of OSM route relations selected by LTA GTFS active line identity.",
        },
        "features": features,
    }
    source_manifest = {
        "schema_version": 1,
        "kind": "preview_osm_rail_source_collection",
        "status": "complete",
        "publication_allowed": False,
        "overpass_endpoint": overpass_endpoint,
        "api_endpoint": api_endpoint,
        "discovery_file": "discovery.json",
        "discovery_sha256": sha256(discovery_raw).hexdigest(),
        "source_timestamp": timestamp,
        "active_line_ids": [line_id for line_id in LINE_ORDER if line_id in active_line_ids],
        "relations": relation_records,
        "responses": response_records,
    }
    atomic_write_json(source_directory / "collection.json", source_manifest)
    return geojson, source_manifest


def validate_geometry_baseline(document: object) -> dict[str, object]:
    """Validate the approved comparison baseline used by preview builds."""

    if type(document) is not dict or set(document) != {"type", "metadata", "features"}:
        raise NetworkGeometryError("Geometry baseline must be a GeoJSON FeatureCollection")
    if document["type"] != "FeatureCollection" or type(document["metadata"]) is not dict:
        raise NetworkGeometryError("Geometry baseline has an invalid root")
    metadata = document["metadata"]
    required = {
        "schema_version", "kind", "status", "publication_allowed", "approval_scope",
        "decision_id", "service_date", "gtfs_zip_sha256", "osm_snapshot_timestamp",
        "license", "attribution", "feature_count", "line_ids", "relation_count",
        "source_relations", "reconstruction", "limitations",
    }
    if set(metadata) != required:
        raise NetworkGeometryError(
            "Geometry baseline metadata fields changed; "
            f"missing={sorted(required - set(metadata))}, extra={sorted(set(metadata) - required)}"
        )
    if (
        metadata["schema_version"] != 1
        or metadata["kind"] != "stage3_approved_rail_geometry_baseline"
        or metadata["status"] != "approved"
        or metadata["publication_allowed"] is not False
        or metadata["approval_scope"] != "comparison_baseline_not_production_release"
        or metadata["decision_id"] != "D-028"
    ):
        raise NetworkGeometryError("Geometry baseline approval metadata is invalid")
    if type(document["features"]) is not list or metadata["feature_count"] != len(document["features"]):
        raise NetworkGeometryError("Geometry baseline feature count differs from its features")
    if metadata["line_ids"] != list(LINE_ORDER):
        raise NetworkGeometryError("Geometry baseline does not contain the expected line identities")
    seen = set()
    for index, feature in enumerate(document["features"]):
        if type(feature) is not dict or set(feature) != {"type", "id", "geometry", "properties"}:
            raise NetworkGeometryError(f"Geometry baseline feature {index} has an invalid shape")
        if feature["type"] != "Feature" or type(feature["id"]) is not str or feature["id"] in seen:
            raise NetworkGeometryError(f"Geometry baseline feature {index} has an invalid identity")
        seen.add(feature["id"])
        geometry = feature["geometry"]
        if type(geometry) is not dict or geometry.get("type") != "LineString" or type(geometry.get("coordinates")) is not list:
            raise NetworkGeometryError(f"Geometry baseline feature {feature['id']} is not a LineString")
        properties = feature["properties"]
        if type(properties) is not dict or properties.get("source_kind") != "openstreetmap_route_relation_member":
            raise NetworkGeometryError(f"Geometry baseline feature {feature['id']} has invalid provenance")
    return document


def compare_geometry(
    candidate: dict[str, object], baseline: dict[str, object] | None
) -> dict[str, object]:
    """Compare a current OSM-derived candidate with the approved geometry."""

    if baseline is None:
        return {
            "schema_version": 1,
            "kind": "preview_rail_geometry_changes",
            "status": "baseline_unavailable",
            "count": 1,
            "items": [{"severity": "WARNING", "reason": "approved_geometry_baseline_unavailable"}],
        }
    approved = validate_geometry_baseline(baseline)
    current_by = {feature["id"]: feature for feature in candidate["features"]}
    baseline_by = {feature["id"]: feature for feature in approved["features"]}
    if len(current_by) != len(candidate["features"]):
        raise NetworkGeometryError("Candidate geometry has duplicate feature IDs")
    items = []
    for feature_id in sorted(set(current_by) - set(baseline_by)):
        items.append({"severity": "WARNING", "reason": "osm_way_added", "feature_id": feature_id})
    for feature_id in sorted(set(baseline_by) - set(current_by)):
        items.append({"severity": "WARNING", "reason": "osm_way_removed", "feature_id": feature_id})
    for feature_id in sorted(set(current_by) & set(baseline_by)):
        current = current_by[feature_id]
        previous = baseline_by[feature_id]
        changed = []
        if current["geometry"] != previous["geometry"]:
            changed.append("geometry")
        for field in ("line_ids", "relation_ids", "source_version", "source_timestamp", "railway"):
            if current["properties"].get(field) != previous["properties"].get(field):
                changed.append(field)
        if changed:
            items.append({
                "severity": "WARNING",
                "reason": "osm_way_changed",
                "feature_id": feature_id,
                "way_id": current["properties"].get("way_id"),
                "fields": changed,
            })
    return {
        "schema_version": 1,
        "kind": "preview_rail_geometry_changes",
        "status": "complete" if not items else "review_required",
        "count": len(items),
        "items": items,
    }


def _point_segment_metres(point: list[float], start: list[float], end: list[float]) -> float:
    latitude = math.radians(point[1])
    scale_x = 111_320.0 * math.cos(latitude)
    scale_y = 110_540.0
    px, py = point[0] * scale_x, point[1] * scale_y
    ax, ay = start[0] * scale_x, start[1] * scale_y
    bx, by = end[0] * scale_x, end[1] * scale_y
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    factor = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + factor * dx), py - (ay + factor * dy))


def build_coverage(network: dict[str, object], geometry: dict[str, object]) -> dict[str, object]:
    """Calculate preview proximity checks without claiming release approval."""

    by_line: dict[str, list[list[list[float]]]] = defaultdict(list)
    for feature in geometry["features"]:
        for line_id in feature["properties"]["line_ids"]:
            by_line[line_id].append(feature["geometry"]["coordinates"])
    checks = []
    for station in network["stations"]:
        for line_id in station["line_ids"]:
            distance = min(
                _point_segment_metres(station["coordinates"], coordinates[index - 1], coordinates[index])
                for coordinates in by_line[line_id]
                for index in range(1, len(coordinates))
            )
            checks.append({
                "stop_id": station["stop_id"],
                "station_name": station["name"],
                "line_id": line_id,
                "distance_metres": round(distance, 1),
                "status": "review" if distance > 250 else "within_preview_tolerance",
            })
    review_checks = [row for row in checks if row["status"] == "review"]
    return {
        "schema_version": 1,
        "kind": "preview_rail_geometry_coverage",
        "status": "review_required" if review_checks else "preview_complete",
        "publication_allowed": False,
        "tolerance_metres": 250,
        "station_line_check_count": len(checks),
        "review_count": len(review_checks),
        "maximum_distance_metres": max((row["distance_metres"] for row in checks), default=0),
        "checks": checks,
        "limitations": [
            "See continuity.json for physical passenger-track paths. Physical connectivity alone does not verify directional service.",
            "OSM relations are selected by an active LTA line identity; relation stops are not yet matched one-by-one to each GTFS service pattern, so extra same-line segments require review.",
            "OSM may map parallel tracks separately; this preview retains physical member ways and does not derive a surveyed centreline.",
            "Human review and an approved baseline are required before publication.",
        ],
    }


def _reviewed_display_paths(network: dict, continuity: dict) -> dict:
    """Waypoints for drawing tracks only. Never rewrite scheduled stops/times."""
    if network['gtfs_zip_sha256'] != '7a4f56076869e1c71d4019d34b23cec90fe0137d1a729967c66bfc8c18793d7f':
        return {}
    patterns = {p['id']: p for p in network['patterns']}
    short = patterns.get('36315303a59b4eb2', {})
    via = ['NS14', 'NS15', 'NS16', 'CC15']  # Bishan's GTFS parent ID is CC15.
    ids = short.get('station_stop_ids', [])
    expanded = ids[:1] + via + ids[1:]
    if (short.get('gtfs_route_id') != 'NSL_NS13_NS28' or ids[:2] != ['NS13', 'NS18']
            or expanded != patterns.get('126cee7ebdde9e4d', {}).get('station_stop_ids')
            or not any(short['id'] in row['pattern_ids'] and row['from_stop'] == 'NS13'
                       and row['to_stop'] == 'NS18' and any(e['direction'] == 'ordered' and e['via'] == via
                                                            for e in row['evidence'])
                       for row in continuity['pairs'])):
        raise NetworkGeometryError('Reviewed Yishun–Braddell physical path evidence changed')
    return {short['id']: expanded}


def _write_preview_assets(output: Path, *, repo_root: Path, payload: dict[str, object]) -> Path:
    app_source = repo_root / "network_preview"
    if not app_source.is_dir():
        raise NetworkGeometryError(f"Network preview assets are missing: {app_source}")
    for filename in ("index.html", "app.js", "styles.css"):
        shutil.copyfile(app_source / filename, output / filename)
    for filename in ("stage-switch.css", "stage-switch.js"):
        shutil.copyfile(repo_root / "review_app" / filename, output / filename)
    vendor_source = repo_root / "review_app/vendor/leaflet"
    vendor_output = output / "vendor/leaflet"
    vendor_output.mkdir(parents=True)
    for filename in ("leaflet.css", "leaflet.js", "LICENSE"):
        shutil.copyfile(vendor_source / filename, vendor_output / filename)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    atomic_write_bytes(output / "preview-data.js", f"window.RAIL_PREVIEW_DATA={encoded};\n".encode())
    return output / "index.html"


def build_network_preview(
    *,
    gtfs_directory: Path,
    output_directory: Path,
    repo_root: Path,
    overpass_endpoint: str,
    api_endpoint: str,
    timeout_seconds: float,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    minimum_interval_seconds: float,
    approved_baseline_path: Path | None = None,
    replay_reviewed_osm: bool = False,
    http_get: Callable[..., bytes] = _default_http_get,
) -> NetworkPreviewResult:
    """Build immutable preview artifacts and a static local map."""

    gtfs_root = gtfs_directory.resolve()
    gtfs_zip = gtfs_root / "GTFSScheduleTrain.zip"
    inventory_path = gtfs_root / "active-stations.json"
    if not gtfs_zip.is_file() or not inventory_path.is_file():
        raise NetworkGeometryError(
            f"GTFS directory must contain GTFSScheduleTrain.zip and active-stations.json: {gtfs_root}"
        )
    output = output_directory.resolve()
    output.mkdir(parents=True, exist_ok=False)
    network, stations = build_gtfs_network(gtfs_zip, inventory_path)
    active_line_ids = {line["id"] for line in network["lines"]}
    if replay_reviewed_osm:
        approval = read_json(repo_root / 'audit/evidence/2026-10-25-network-geometry-baseline-approval.json')
        evidence = approval['retained_evidence']
        archive_path = repo_root / evidence['path']
        if sha256(archive_path.read_bytes()).hexdigest() != evidence['sha256']:
            raise NetworkGeometryError('Reviewed OSM evidence archive hash mismatch')

        def replay_get(url: str, *, timeout: float, max_bytes: int) -> bytes:
            if url == _overpass_url(overpass_endpoint):
                member = 'current-osm/discovery.json'
            else:
                match = re.fullmatch(re.escape(api_endpoint.rstrip('/')) + r'/relation/(\d+)/full\.json', url)
                if not match:
                    raise NetworkGeometryError('Unexpected OSM replay request')
                member = f'current-osm/relations/{match[1]}.json'
            with ZipFile(archive_path) as archive:
                if archive.getinfo(member).file_size > max_bytes:
                    raise NetworkGeometryError('Retained OSM response exceeds size limit')
                return archive.read(member)

        http_get = replay_get
        minimum_interval_seconds = 0
    geometry, source_manifest = collect_osm_geometry(
        active_line_ids=active_line_ids,
        overpass_endpoint=overpass_endpoint,
        api_endpoint=api_endpoint,
        source_directory=output / "sources/osm",
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        initial_backoff_seconds=initial_backoff_seconds,
        maximum_backoff_seconds=maximum_backoff_seconds,
        minimum_interval_seconds=minimum_interval_seconds,
        http_get=http_get,
    )
    continuity = check_continuity(network, source_manifest, output / 'sources/osm')
    baseline_document = (
        read_json(approved_baseline_path)
        if approved_baseline_path is not None and approved_baseline_path.is_file()
        else None
    )
    raw_changes = compare_geometry(geometry, baseline_document)
    override = None
    if replay_reviewed_osm:
        # The hash-checked archive contains the response shortly AFTER the
        # approved October snapshot. Retain the later response for evidence,
        # but use D-028's reviewed coordinates for its one documented move.
        if (baseline_document is None or sha256(approved_baseline_path.read_bytes()).hexdigest()
                != approval['candidate']['file_sha256'] or raw_changes['count'] != 1
                or raw_changes['items'][0]['way_id'] != 380508287
                or raw_changes['items'][0]['fields'] != ['geometry']):
            raise NetworkGeometryError('October fallback no longer matches the reviewed D-028 difference')
        fid = 'osm-way-380508287'
        prior = next(f for f in baseline_document['features'] if f['id'] == fid)
        selected = next(f for f in geometry['features'] if f['id'] == fid)
        selected['geometry'] = prior['geometry']
        override = {'decision': 'D-028', 'feature': fid, 'archive_sha256': evidence['sha256'],
                    'baseline_sha256': approval['candidate']['file_sha256'], 'raw_changes': raw_changes}
    changes = compare_geometry(geometry, baseline_document)
    coverage = build_coverage(network, geometry)
    atomic_write_json(output / 'continuity.json', continuity)
    network_path = atomic_write_json(output / "rail-network.json", network)
    geojson_path = atomic_write_json(output / "rail-lines.geojson", geometry)
    atomic_write_json(output / "stations.geojson", stations)
    coverage_path = atomic_write_json(output / "coverage.json", coverage)
    changes_path = atomic_write_json(output / "changes.json", changes)
    manifest = {
        "schema_version": 1,
        "kind": "rail_network_geometry_preview",
        "status": "review_required",
        "publication_allowed": False,
        "service_date": network["service_date"],
        "line_count": len(network["lines"]),
        "station_count": len(network["stations"]),
        "geometry_feature_count": len(geometry["features"]),
        "osm_relation_count": len(source_manifest["relations"]),
        "osm_source_mode": "retained October 2026 evidence" if replay_reviewed_osm else "live collection",
        "coverage_review_count": coverage["review_count"],
        "geometry_override": override,
        "physical_pair_count": continuity['pair_count'],
        "physical_pair_review_count": continuity['review_count'],
        "geometry_change_count": changes["count"],
        "approved_geometry_baseline": (
            {
                "path": str(approved_baseline_path),
                "sha256": sha256(approved_baseline_path.read_bytes()).hexdigest(),
            }
            if approved_baseline_path is not None and approved_baseline_path.is_file()
            else None
        ),
        "files": {
            "network": "rail-network.json",
            "geometry": "rail-lines.geojson",
            "stations": "stations.geojson",
            "coverage": "coverage.json",
            "continuity": "continuity.json",
            "changes": "changes.json",
            "preview": "index.html",
        },
        "notice": "Temporary review artifact only. Do not use in production or publish as approved data.",
    }
    atomic_write_json(output / "manifest.json", manifest)
    preview_path = _write_preview_assets(
        output,
        repo_root=repo_root,
        payload={
            "manifest": manifest,
            "network": network,
            "display_paths": _reviewed_display_paths(network, continuity),
            "geometry": geometry,
            "stations": stations,
            "coverage": coverage,
            "changes": changes,
        },
    )
    return NetworkPreviewResult(
        output_directory=output,
        geojson_path=geojson_path,
        network_path=network_path,
        coverage_path=coverage_path,
        changes_path=changes_path,
        preview_path=preview_path,
        line_count=len(network["lines"]),
        feature_count=len(geometry["features"]),
        station_count=len(network["stations"]),
        change_count=changes["count"],
    )


__all__ = [
    "NetworkGeometryError",
    "NetworkPreviewResult",
    "build_coverage",
    "build_gtfs_network",
    "build_network_preview",
    "collect_osm_geometry",
    "compare_geometry",
    "validate_geometry_baseline",
]
